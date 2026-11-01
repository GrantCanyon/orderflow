import hashlib
import json
import math
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import and_, func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from .db import audit_events, bookings, idempotency, items, products
from .errors import DomainError
from .identity import actor as identify


def utc(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise DomainError("invalid_input", "Некорректная дата", status=422) from exc
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise DomainError("invalid_input", "Укажите дату с часовым поясом", status=422)
    return value.astimezone(UTC)


def aware(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def iso(value):
    return aware(value).isoformat().replace("+00:00", "Z")


def integer(value, minimum=0, maximum=1_000_000):
    if type(value) is not int or not minimum <= value <= maximum:
        raise DomainError("invalid_input", "Число вне допустимого диапазона", status=422)
    return value


def text(value, maximum):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= maximum:
        raise DomainError("invalid_input", "Текст пуст или слишком длинный", status=422)
    return value.strip()


class RentalService:
    def __init__(self, db, clock=None):
        self.db = db
        self.clock = clock or (lambda: datetime.now(UTC))

    def _event(self, conn, actor, action, details, booking_id=None):
        actor = identify(actor)
        conn.execute(
            insert(audit_events).values(
                actor=actor.id,
                actor_name=actor.name,
                action=action,
                details=details,
                booking_id=booking_id,
                created_at=self.clock(),
            )
        )

    def _lock_products(self, conn, ids):
        rows = (
            conn.execute(
                select(products)
                .where(products.c.id.in_(sorted(ids)))
                .order_by(products.c.id)
                .with_for_update()
            )
            .mappings()
            .all()
        )
        if len(rows) != len(set(ids)):
            raise DomainError("not_found", "Оборудование не найдено", status=404)
        return {row["id"]: dict(row) for row in rows}

    def create_product(self, actor, **payload):
        data = {
            key: text(payload.get(key), size)
            for key, size in (("sku", 40), ("name", 120), ("category", 80))
        }
        data.update(
            capacity=integer(payload.get("capacity")),
            daily_rate_cents=integer(payload.get("daily_rate_cents"), maximum=100_000_000),
        )
        try:
            with self.db.transaction(write=True) as conn:
                product_id = conn.execute(insert(products).values(**data)).inserted_primary_key[0]
                self._event(conn, actor, "product.create", {"product_id": product_id, **data})
                return {"id": product_id, **data}
        except IntegrityError as exc:
            raise DomainError("sku_conflict", "Артикул уже используется") from exc

    def list_products(self):
        with self.db.transaction() as conn:
            return [
                dict(row)
                for row in conn.execute(select(products).order_by(products.c.id)).mappings()
            ]

    def _active(self, now):
        return or_(
            bookings.c.status.in_(["confirmed", "checked_out"]),
            and_(bookings.c.status == "hold", bookings.c.expires_at > now),
        )

    def _segments(self, conn, product_id, start, end, now, exclude=None):
        query = (
            select(bookings.c.id, bookings.c.start_at, bookings.c.end_at, items.c.quantity)
            .join(items, bookings.c.id == items.c.booking_id)
            .where(
                items.c.product_id == product_id,
                self._active(now),
                bookings.c.start_at < end,
                bookings.c.end_at > start,
            )
        )
        if exclude:
            query = query.where(bookings.c.id != exclude)
        rows = conn.execute(query).mappings().all()
        events = {start: [], end: []}
        for row in rows:
            left, right = max(start, aware(row["start_at"])), min(end, aware(row["end_at"]))
            events.setdefault(left, []).append((row["id"], row["quantity"]))
            events.setdefault(right, []).append((row["id"], -row["quantity"]))
        points, active, segments = sorted(events), {}, []
        for index, point in enumerate(points[:-1]):
            for booking_id, delta in events[point]:
                active[booking_id] = active.get(booking_id, 0) + delta
                if active[booking_id] == 0:
                    del active[booking_id]
            segments.append(
                {
                    "start_at": iso(point),
                    "end_at": iso(points[index + 1]),
                    "reserved": sum(active.values()),
                    "booking_ids": sorted(active),
                }
            )
        return segments

    def _interval(self, start, end, future=False):
        start, end = utc(start), utc(end)
        if end <= start or end - start > timedelta(days=366):
            raise DomainError(
                "invalid_interval", "Интервал должен быть положительным и не более года", status=422
            )
        if future and start < self.clock():
            raise DomainError("past_interval", "Начало бронирования уже прошло", status=422)
        return start, end

    def availability(self, start, end):
        start, end = self._interval(start, end)
        with self.db.transaction() as conn:
            result = []
            now = self.clock()
            for product in conn.execute(select(products).order_by(products.c.id)).mappings():
                segments = self._segments(conn, product["id"], start, end, now)
                peak = max((segment["reserved"] for segment in segments), default=0)
                result.append(
                    {
                        **product,
                        "available": product["capacity"] - peak,
                        "physical_available": product["capacity"]
                        - self._physical_out(conn, product["id"]),
                        "overdue_quantity": conn.execute(
                            select(func.coalesce(func.sum(items.c.quantity), 0))
                            .join(bookings, items.c.booking_id == bookings.c.id)
                            .where(
                                items.c.product_id == product["id"],
                                bookings.c.status == "checked_out",
                                bookings.c.end_at <= now,
                            )
                        ).scalar_one(),
                        "peak_reserved": peak,
                        "segments": segments,
                    }
                )
            return result

    def _check_capacity(self, conn, catalog, requested, start, end, now, exclude=None):
        conflicts = []
        for item in requested:
            product = catalog[item["product_id"]]
            for segment in self._segments(conn, product["id"], start, end, now, exclude):
                if segment["reserved"] + item["quantity"] > product["capacity"]:
                    conflicts.append(
                        {
                            "product_id": product["id"],
                            "name": product["name"],
                            "capacity": product["capacity"],
                            "requested": item["quantity"],
                            **segment,
                        }
                    )
        if conflicts:
            raise DomainError(
                "capacity_conflict",
                "Недостаточно оборудования на выбранный период",
                {"conflicts": conflicts},
            )

    def _normalize(self, payload):
        start, end = self._interval(payload.get("start_at"), payload.get("end_at"))
        requested = payload.get("items")
        if not isinstance(requested, list) or not 1 <= len(requested) <= 50:
            raise DomainError("invalid_input", "Выберите от 1 до 50 позиций", status=422)
        normalized = []
        for item in requested:
            if not isinstance(item, dict):
                raise DomainError("invalid_input", "Некорректная позиция", status=422)
            normalized.append(
                {
                    "product_id": integer(item.get("product_id"), 1),
                    "quantity": integer(item.get("quantity"), 1),
                }
            )
        if len({item["product_id"] for item in normalized}) != len(normalized):
            raise DomainError("invalid_input", "Позиции не должны повторяться", status=422)
        return {
            "label": text(payload.get("label"), 120),
            "start_at": iso(start),
            "end_at": iso(end),
            "ttl_minutes": integer(payload.get("ttl_minutes", 15), 1, 60),
            "items": sorted(normalized, key=lambda item: item["product_id"]),
        }

    def _replay(self, conn, actor, key, fingerprint):
        row = (
            conn.execute(
                select(idempotency).where(
                    idempotency.c.principal == actor.id, idempotency.c.key == key
                )
            )
            .mappings()
            .first()
        )
        if row:
            if row["fingerprint"] != fingerprint:
                raise DomainError(
                    "idempotency_conflict", "Этот ключ уже использован для другого запроса"
                )
            return self._booking(conn, row["booking_id"])
        return None

    def create_booking(self, actor, key, payload):
        actor = identify(actor)
        key = text(key, 120)
        normalized = self._normalize(payload)
        fingerprint = hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()
        try:
            with self.db.transaction(write=True) as conn:
                replay = self._replay(conn, actor, key, fingerprint)
                if replay:
                    return replay
                catalog = self._lock_products(
                    conn, [item["product_id"] for item in normalized["items"]]
                )
                # После ожидания блокировки проверяем ключ и TTL по свежему времени.
                replay = self._replay(conn, actor, key, fingerprint)
                if replay:
                    return replay
                start, end = self._interval(
                    normalized["start_at"], normalized["end_at"], future=True
                )
                now = self.clock()
                self._check_capacity(conn, catalog, normalized["items"], start, end, now)
                booking_id = str(uuid4())
                line_items = [
                    {
                        **item,
                        "booking_id": booking_id,
                        "rate_cents": catalog[item["product_id"]]["daily_rate_cents"],
                    }
                    for item in normalized["items"]
                ]
                total = self._price(line_items, start, end)
                conn.execute(
                    insert(bookings).values(
                        id=booking_id,
                        reference=f"OF-{uuid4().hex[:10].upper()}",
                        label=normalized["label"],
                        start_at=start,
                        end_at=end,
                        status="hold",
                        expires_at=now + timedelta(minutes=normalized["ttl_minutes"]),
                        created_at=now,
                        created_by=actor.id,
                        created_by_name=actor.name,
                        total_cents=total,
                    )
                )
                conn.execute(insert(items), line_items)
                conn.execute(
                    insert(idempotency).values(
                        principal=actor.id, key=key, fingerprint=fingerprint, booking_id=booking_id
                    )
                )
                self._event(conn, actor, "booking.create", normalized, booking_id)
                return self._booking(conn, booking_id)
        except IntegrityError:
            # Уникальность ключа защищает также гонку по разным пулам оборудования.
            with self.db.transaction() as conn:
                replay = self._replay(conn, actor, key, fingerprint)
                if replay:
                    return replay
            raise

    @staticmethod
    def _price(line_items, start, end):
        days = math.ceil((end - start).total_seconds() / 86400)
        total = days * sum(item["quantity"] * item["rate_cents"] for item in line_items)
        if total > 9_007_199_254_740_991:
            raise DomainError(
                "quote_too_large", "Сумма превышает безопасный предел расчёта", status=422
            )
        return total

    def _booking(self, conn, booking_id, lock=False):
        query = select(bookings).where(bookings.c.id == booking_id)
        row = conn.execute(query.with_for_update() if lock else query).mappings().first()
        if not row:
            raise DomainError("not_found", "Бронирование не найдено", status=404)
        result = dict(row)
        now = self.clock()
        if row["status"] == "hold" and aware(row["expires_at"]) <= now:
            result["status"] = "expired"
        result["overdue"] = row["status"] == "checked_out" and aware(row["end_at"]) <= now
        result["items"] = [
            dict(item)
            for item in conn.execute(
                select(
                    items.c.product_id,
                    items.c.quantity,
                    items.c.rate_cents,
                    products.c.name,
                    products.c.sku,
                )
                .join(products, items.c.product_id == products.c.id)
                .where(items.c.booking_id == booking_id)
                .order_by(items.c.product_id)
            ).mappings()
        ]
        for field in ("start_at", "end_at", "expires_at", "created_at"):
            result[field] = iso(result[field])
        return result

    def get_booking(self, booking_id):
        with self.db.transaction() as conn:
            return self._booking(conn, booking_id)

    def list_bookings(self, status=None, search=None, limit=100, offset=0):
        integer(limit, 1, 200)
        integer(offset, 0, 1_000_000)
        with self.db.transaction() as conn:
            query = select(bookings.c.id)
            now = self.clock()
            if status == "expired":
                query = query.where(
                    or_(
                        bookings.c.status == "expired",
                        and_(bookings.c.status == "hold", bookings.c.expires_at <= now),
                    )
                )
            elif status == "hold":
                query = query.where(bookings.c.status == "hold", bookings.c.expires_at > now)
            elif status == "overdue":
                query = query.where(bookings.c.status == "checked_out", bookings.c.end_at <= now)
            elif status:
                query = query.where(bookings.c.status == status)
            if search:
                query = query.where(
                    or_(
                        bookings.c.label.icontains(search, autoescape=True),
                        bookings.c.reference.icontains(search, autoescape=True),
                    )
                )
            ids = (
                conn.execute(
                    query.order_by(bookings.c.created_at.desc(), bookings.c.id)
                    .limit(limit)
                    .offset(offset)
                )
                .scalars()
                .all()
            )
            return [self._booking(conn, booking_id) for booking_id in ids]

    def _locked_booking(self, conn, booking_id):
        product_ids = (
            conn.execute(select(items.c.product_id).where(items.c.booking_id == booking_id))
            .scalars()
            .all()
        )
        catalog = self._lock_products(conn, product_ids)
        return self._booking(conn, booking_id, lock=True), catalog

    def _physical_out(self, conn, product_id):
        return conn.execute(
            select(func.coalesce(func.sum(items.c.quantity), 0))
            .join(bookings, items.c.booking_id == bookings.c.id)
            .where(items.c.product_id == product_id, bookings.c.status == "checked_out")
        ).scalar_one()

    def transition(self, actor, booking_id, action):
        transitions = {
            "confirm": ({"hold"}, "confirmed"),
            "cancel": ({"hold", "confirmed"}, "cancelled"),
            "check_out": ({"confirmed"}, "checked_out"),
            "return": ({"checked_out"}, "returned"),
        }
        if action not in transitions:
            raise DomainError("invalid_action", "Неизвестное действие", status=422)
        with self.db.transaction(write=True) as conn:
            booking, catalog = self._locked_booking(conn, booking_id)
            allowed, target = transitions[action]
            if booking["status"] not in allowed:
                raise DomainError("invalid_transition", f"Действие недоступно: {booking['status']}")
            if action == "check_out":
                if not utc(booking["start_at"]) <= self.clock() < utc(booking["end_at"]):
                    raise DomainError(
                        "outside_window", "Выдача доступна только в период бронирования"
                    )
                conflicts = []
                for item in booking["items"]:
                    outstanding = self._physical_out(conn, item["product_id"])
                    capacity = catalog[item["product_id"]]["capacity"]
                    if outstanding + item["quantity"] > capacity:
                        conflicts.append(
                            {
                                "product_id": item["product_id"],
                                "checked_out": outstanding,
                                "capacity": capacity,
                                "requested": item["quantity"],
                            }
                        )
                if conflicts:
                    raise DomainError(
                        "physical_stock_conflict",
                        "Оборудование ещё не возвращено",
                        {"conflicts": conflicts},
                    )
            conn.execute(update(bookings).where(bookings.c.id == booking_id).values(status=target))
            self._event(
                conn,
                actor,
                f"booking.{action}",
                {"from": booking["status"], "to": target},
                booking_id,
            )
            return self._booking(conn, booking_id)

    def reschedule(self, actor, booking_id, start, end):
        start, end = self._interval(start, end, future=True)
        with self.db.transaction(write=True) as conn:
            booking, catalog = self._locked_booking(conn, booking_id)
            start, end = self._interval(start, end, future=True)
            if booking["status"] not in {"hold", "confirmed"}:
                raise DomainError("invalid_transition", "Перенос доступен только до выдачи")
            self._check_capacity(
                conn, catalog, booking["items"], start, end, self.clock(), booking_id
            )
            conn.execute(
                update(bookings)
                .where(bookings.c.id == booking_id)
                .values(
                    start_at=start,
                    end_at=end,
                    total_cents=self._price(booking["items"], start, end),
                )
            )
            self._event(
                conn,
                actor,
                "booking.reschedule",
                {
                    "previous_start": booking["start_at"],
                    "previous_end": booking["end_at"],
                    "start_at": iso(start),
                    "end_at": iso(end),
                },
                booking_id,
            )
            return self._booking(conn, booking_id)

    def update_product(self, actor, product_id, **changes):
        if not changes or set(changes) - {"capacity", "daily_rate_cents", "name", "category"}:
            raise DomainError("invalid_input", "Неизвестные поля оборудования", status=422)
        for key, value in changes.items():
            changes[key] = (
                text(value, 120 if key == "name" else 80)
                if key in {"name", "category"}
                else integer(value, maximum=100_000_000 if key == "daily_rate_cents" else 1_000_000)
            )
        with self.db.transaction(write=True) as conn:
            product = self._lock_products(conn, [product_id])[product_id]
            if "capacity" in changes:
                if self._physical_out(conn, product_id) > changes["capacity"]:
                    raise DomainError(
                        "physical_stock_conflict", "Новый остаток меньше выданного оборудования"
                    )
                now = self.clock()
                segments = self._segments(
                    conn, product_id, now, datetime.max.replace(tzinfo=UTC), now
                )
                if any(segment["reserved"] > changes["capacity"] for segment in segments):
                    raise DomainError(
                        "capacity_conflict", "Новый остаток меньше уже занятого оборудования"
                    )
            conn.execute(update(products).where(products.c.id == product_id).values(**changes))
            self._event(
                conn,
                actor,
                "product.update",
                {"product_id": product_id, "before": product, "changes": changes},
            )
            return {**product, **changes}

    def expire_holds(self, actor):
        with self.db.transaction(write=True) as conn:
            ids = (
                conn.execute(
                    select(bookings.c.id)
                    .where(bookings.c.status == "hold", bookings.c.expires_at <= self.clock())
                    .order_by(bookings.c.id)
                )
                .scalars()
                .all()
            )
            # Единый порядок блокировок для пакета предотвращает взаимные ожидания.
            product_ids = (
                conn.execute(select(items.c.product_id).where(items.c.booking_id.in_(ids)))
                .scalars()
                .all()
            )
            self._lock_products(conn, product_ids)
            count = 0
            for booking_id in ids:
                row = (
                    conn.execute(
                        select(bookings).where(bookings.c.id == booking_id).with_for_update()
                    )
                    .mappings()
                    .one()
                )
                if row["status"] == "hold" and aware(row["expires_at"]) <= self.clock():
                    conn.execute(
                        update(bookings).where(bookings.c.id == booking_id).values(status="expired")
                    )
                    self._event(conn, actor, "booking.expire", {}, booking_id)
                    count += 1
            return count

    def audit(self, booking_id=None, limit=100, before_id=None):
        integer(limit, 1, 200)
        with self.db.transaction() as conn:
            query = select(audit_events)
            if booking_id:
                query = query.where(audit_events.c.booking_id == booking_id)
            if before_id:
                query = query.where(audit_events.c.id < before_id)
            rows = conn.execute(query.order_by(audit_events.c.id.desc()).limit(limit)).mappings()
            return [{**row, "created_at": iso(row["created_at"])} for row in rows]
