from datetime import UTC, datetime, timedelta

from .service import RentalService, iso


def seed_demo(db, now=None):
    now = now or datetime.now(UTC)
    cursor = now - timedelta(days=3)
    service = RentalService(db, lambda: cursor)
    if service.list_products():
        return False
    specs = [
        ("AUD-01", "Колонка активная · 1000 Вт", "Звук", 12, 240000),
        ("AUD-02", "Радиомикрофон · комплект", "Звук", 8, 120000),
        ("LGT-01", "Прожектор LED · RGBW", "Свет", 24, 80000),
        ("LGT-02", "Световая стойка · 3 м", "Свет", 10, 45000),
        ("VID-01", "Проектор · 6000 лм", "Видео", 4, 480000),
        ("VID-02", "Экран · 120 дюймов", "Видео", 4, 180000),
        ("PWR-01", "Кабельная катушка · 25 м", "Питание", 16, 35000),
        ("AUD-03", "Микшер · 12 каналов", "Звук", 5, 220000),
    ]
    catalog = [
        service.create_product(
            "Демо · система",
            sku=sku,
            name=name,
            category=category,
            capacity=capacity,
            daily_rate_cents=rate,
        )
        for sku, name, category, capacity, rate in specs
    ]

    def booking(label, start, end, selection, status):
        nonlocal cursor
        cursor = min(start, now) - timedelta(minutes=5)
        result = service.create_booking(
            "Демо · оператор",
            label,
            {
                "label": label,
                "start_at": iso(start),
                "end_at": iso(end),
                "items": [
                    {"product_id": catalog[index]["id"], "quantity": quantity}
                    for index, quantity in selection
                ],
                "ttl_minutes": 60,
            },
        )
        if status != "hold":
            service.transition("Демо · оператор", result["id"], "confirm")
        if status in {"checked_out", "returned"}:
            cursor = start
            service.transition("Демо · оператор", result["id"], "check_out")
        if status == "returned":
            service.transition("Демо · оператор", result["id"], "return")

    booking(
        "Фестиваль «Точка звука»",
        now - timedelta(hours=8),
        now - timedelta(hours=1),
        [(0, 4), (1, 2)],
        "checked_out",
    )
    booking(
        "Конференция «Новые идеи»",
        now - timedelta(hours=2),
        now + timedelta(hours=6),
        [(4, 2), (5, 2), (7, 1)],
        "checked_out",
    )
    booking(
        "Открытая сцена · вечер",
        now + timedelta(hours=2),
        now + timedelta(hours=12),
        [(0, 8), (2, 16), (3, 6)],
        "confirmed",
    )
    booking(
        "Камерный концерт",
        now + timedelta(hours=4),
        now + timedelta(days=1),
        [(1, 4), (7, 2), (6, 5)],
        "hold",
    )
    booking(
        "Лекция в пространстве",
        now + timedelta(days=1),
        now + timedelta(days=1, hours=6),
        [(4, 1), (5, 1), (0, 2)],
        "confirmed",
    )
    booking(
        "Съёмка каталога",
        now + timedelta(days=2),
        now + timedelta(days=3),
        [(2, 12), (3, 4), (6, 6)],
        "confirmed",
    )
    booking(
        "Репетиция ансамбля",
        now - timedelta(days=1),
        now - timedelta(hours=12),
        [(0, 2), (1, 2)],
        "returned",
    )
    # Учебные работы создаются через обычные проверки с управляемыми часами.
    cursor = now - timedelta(hours=4)
    overdue = service.create_maintenance(
        "Демо · администратор",
        "maintenance-overdue",
        {
            "product_id": catalog[6]["id"],
            "quantity": 3,
            "start_at": iso(cursor + timedelta(hours=1)),
            "end_at": iso(now - timedelta(hours=1)),
            "reason": "Проверка изоляции кабельных катушек · демонстрационная просрочка",
        },
    )
    cursor += timedelta(hours=1)
    service.transition_maintenance("Демо · техник", overdue["id"], "start")
    cursor = now
    service.create_maintenance(
        "Демо · администратор",
        "maintenance-planned",
        {
            "product_id": catalog[3]["id"],
            "quantity": 2,
            "start_at": iso(now + timedelta(hours=1)),
            "end_at": iso(now + timedelta(hours=4)),
            "reason": "Проверка фиксаторов световых стоек",
        },
    )
    return True
