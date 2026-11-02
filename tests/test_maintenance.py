from datetime import timedelta

import pytest
from conftest import request
from test_concurrency import race

from orderflow.errors import DomainError
from orderflow.identity import Actor


def block(product_id, quantity=1, start="2030-01-02T10:00:00Z", end="2030-01-02T14:00:00Z"):
    return dict(
        product_id=product_id,
        quantity=quantity,
        start_at=start,
        end_at=end,
        reason="Проверка\nоборудования",
    )


def test_calendar_blocks_booking_and_alternatives(service, product):
    item = service.create_maintenance("admin", "m", block(product["id"], 3))
    with pytest.raises(DomainError) as error:
        service.create_booking("alice", "b", request(product["id"]))
    assert error.value.code == "capacity_conflict"
    assert error.value.details["conflicts"][0]["maintenance_ids"] == [item["id"]]
    row = service.availability(item["start_at"], item["end_at"])[0]
    assert row["available"] == 0 and row["physical_available"] == 3
    result = service.alternatives(
        {**request(product["id"]), "search_until": "2030-01-03T10:00:00Z"}
    )
    assert result["alternatives"][0]["start_at"] == item["end_at"]
    service.create_booking(
        "alice",
        "adjacent",
        request(product["id"], start=item["end_at"], end="2030-01-02T18:00:00Z", quantity=3),
    )


def test_create_preserves_bookings_and_capacity(service, product):
    booking = service.create_booking("alice", "b", request(product["id"], quantity=2))
    with pytest.raises(DomainError) as error:
        service.create_maintenance("admin", "m", block(product["id"], 2))
    assert error.value.code == "capacity_conflict"
    assert service.list_maintenance() == []
    assert service.get_booking(booking["id"])["status"] == "hold"
    item = service.create_maintenance("admin", "m", block(product["id"]))
    with pytest.raises(DomainError):
        service.update_product("admin", product["id"], capacity=2)
    service.transition_maintenance("admin", item["id"], "cancel")
    assert service.update_product("admin", product["id"], capacity=2)["capacity"] == 2


def test_overdue_physical_block_and_early_complete(service, product, clock):
    item = service.create_maintenance("admin", "m", block(product["id"], 3))
    with pytest.raises(DomainError) as error:
        service.transition_maintenance("admin", item["id"], "start")
    assert error.value.code == "outside_window"
    clock.now = clock.now.replace(day=2, hour=10)
    service.transition_maintenance("admin", item["id"], "start")
    clock.now = clock.now.replace(hour=14)
    assert service.get_maintenance(item["id"])["overdue"]
    assert service.transition_maintenance("admin", item["id"], "start")["status"] == "active"
    later = request(
        product["id"], start="2030-01-02T14:00:00Z", end="2030-01-02T18:00:00Z", quantity=3
    )
    booking = service.create_booking("alice", "b", later)
    service.transition("alice", booking["id"], "confirm")
    with pytest.raises(DomainError) as error:
        service.transition("alice", booking["id"], "check_out")
    assert error.value.code == "physical_stock_conflict"
    row = service.availability(later["start_at"], later["end_at"])[0]
    assert row["physical_available"] == 0 and row["maintenance_overdue_quantity"] == 3
    with pytest.raises(DomainError):
        service.update_product("admin", product["id"], capacity=2)
    service.transition_maintenance("admin", item["id"], "complete")
    assert service.transition("alice", booking["id"], "check_out")["status"] == "checked_out"


def test_early_complete_releases_calendar_and_repeated_actions_have_one_event(
    service, product, clock
):
    item = service.create_maintenance("admin", "m", block(product["id"], 3))
    clock.now = clock.now.replace(day=2, hour=10)
    for action in ("start", "start", "complete", "complete"):
        service.transition_maintenance("admin", item["id"], action)
    assert service.availability(item["start_at"], item["end_at"])[0]["available"] == 3
    assert (
        len([event for event in service.audit() if event["action"].startswith("maintenance.")]) == 3
    )
    with pytest.raises(DomainError):
        service.transition_maintenance("admin", item["id"], "start")


def test_idempotency_identity_and_cancel(service, product):
    first = service.create_maintenance(Actor("one", "Имя"), "m", block(product["id"]))
    replay = service.create_maintenance(Actor("one", "Новое имя"), "m", block(product["id"]))
    second = service.create_maintenance(Actor("two", "Имя"), "m", block(product["id"]))
    assert first["id"] == replay["id"] != second["id"]
    assert replay["created_by_name"] == "Имя"
    assert "key" not in replay and "fingerprint" not in replay
    with pytest.raises(DomainError) as error:
        service.create_maintenance(Actor("one", "Имя"), "m", block(product["id"], 2))
    assert error.value.code == "idempotency_conflict"
    for _ in range(2):
        service.transition_maintenance("admin", first["id"], "cancel")
    assert len([e for e in service.audit() if e["action"] == "maintenance.cancel"]) == 1


def test_create_checks_clock_after_product_lock(service, product, clock, monkeypatch):
    lock = service._lock_products

    def advance(*args):
        result = lock(*args)
        clock.now += timedelta(seconds=2)
        return result

    monkeypatch.setattr(service, "_lock_products", advance)
    start = clock.now + timedelta(seconds=1)
    with pytest.raises(DomainError) as error:
        service.create_maintenance(
            "admin", "m", block(product["id"], start=start, end=start + timedelta(hours=1))
        )
    assert error.value.code == "past_interval"
    assert service.list_maintenance() == []


def test_booking_maintenance_race(service, product):
    results = race(
        lambda: service.create_booking("alice", "b", request(product["id"], quantity=3)),
        lambda: service.create_maintenance("admin", "m", block(product["id"], 3)),
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count("capacity_conflict") == 1


def test_same_key_maintenance_race(service, product):
    results = race(
        *[
            lambda: service.create_maintenance("admin", "same", block(product["id"]))
            for _ in range(4)
        ]
    )
    assert len({result["id"] for result in results}) == 1
    assert len(service.list_maintenance()) == 1


def test_http_maintenance_roles_idempotency_and_validation(db, clock, product):
    from test_api import client, headers

    with client(db, clock) as api:
        body = block(product["id"])
        for role in ("viewer", "operator"):
            assert (
                api.post(
                    "/api/maintenance", json=body, headers={**headers(role), "Idempotency-Key": "m"}
                ).status_code
                == 403
            )
        auth = {**headers("admin"), "Idempotency-Key": "m"}
        response = api.post("/api/maintenance", json=body, headers=auth)
        assert response.status_code == 201
        item = response.json()
        assert api.post("/api/maintenance", json=body, headers=auth).json()["id"] == item["id"]
        assert api.get("/api/maintenance", headers=headers("viewer")).json()[0]["id"] == item["id"]
        assert (
            api.get(f"/api/maintenance/{item['id']}", headers=headers("operator")).status_code
            == 200
        )
        assert (
            api.post(
                f"/api/maintenance/{item['id']}/cancel", headers=headers("operator")
            ).status_code
            == 403
        )
        assert (
            api.post(f"/api/maintenance/{item['id']}/cancel", headers=headers("admin")).status_code
            == 200
        )
        for change in (
            {"product_id": 2**31},
            {"quantity": True},
            {"reason": "x\x00"},
            {"unexpected": 1},
        ):
            assert (
                api.post(
                    "/api/maintenance",
                    json={**body, **change},
                    headers={**auth, "Idempotency-Key": "invalid"},
                ).status_code
                == 422
            )
        assert (
            api.get("/api/maintenance?status=unknown", headers=headers("viewer")).status_code == 422
        )


@pytest.mark.parametrize(
    "path",
    [
        "/api/maintenance/%00",
        "/api/maintenance/not-uuid",
        "/api/bookings/%00",
        "/api/audit?booking_id=%00",
    ],
)
def test_invalid_resource_identifier_before_sql(db, clock, path):
    from test_api import client, headers

    with client(db, clock) as api:
        assert api.get(path, headers=headers("admin")).status_code == 422
        if "/api/maintenance/" in path:
            assert api.post(path + "/start", headers=headers("admin")).status_code == 422


def test_maintenance_identifier_canonicalization(service, product):
    item = service.create_maintenance("admin", "m", block(product["id"]))
    assert service.get_maintenance(item["id"].upper())["id"] == item["id"]
    for invalid in ("x\x00", "not-uuid", None, 1):
        with pytest.raises(DomainError) as error:
            service.get_maintenance(invalid)
        assert error.value.status == 422


def test_actual_maintenance_boundaries_preserved_on_replay(service, product, clock):
    item = service.create_maintenance(Actor("creator", "Автор"), "m", block(product["id"]))
    assert item["started_at"] is None and item["completed_at"] is None
    clock.now = clock.now.replace(day=2, hour=10)
    started = service.transition_maintenance(Actor("tech", "Техник"), item["id"], "start")
    assert started["started_at"] == "2030-01-02T10:00:00Z" and started["started_by"] == "tech"
    clock.now += timedelta(minutes=5)
    assert (
        service.transition_maintenance("other", item["id"], "start")["started_at"]
        == started["started_at"]
    )
    completed = service.transition_maintenance(
        Actor("admin", "Администратор"), item["id"], "complete"
    )
    clock.now += timedelta(minutes=5)
    replay = service.transition_maintenance("other", item["id"], "complete")
    assert replay["completed_at"] == completed["completed_at"] == "2030-01-02T10:05:00Z"
    assert replay["completed_by"] == "admin" and replay["completed_by_name"] == "Администратор"


def test_checkout_and_start_compete_for_last_physical_unit(service, product, clock):
    service.update_product("admin", product["id"], capacity=2)
    old = service.create_booking(
        "alice",
        "old",
        request(product["id"], start="2030-01-01T08:00:00Z", end="2030-01-01T09:00:00Z"),
    )
    service.transition("alice", old["id"], "confirm")
    service.transition("alice", old["id"], "check_out")
    current = service.create_booking("alice", "new", request(product["id"]))
    service.transition("alice", current["id"], "confirm")
    item = service.create_maintenance("admin", "m", block(product["id"]))
    clock.now = clock.now.replace(day=2, hour=10)
    results = race(
        lambda: service.transition("alice", current["id"], "check_out"),
        lambda: service.transition_maintenance("admin", item["id"], "start"),
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count("physical_stock_conflict") == 1
    assert service.availability(item["start_at"], item["end_at"])[0]["physical_available"] == 0


def test_start_cancel_race_has_one_terminal_decision(service, product, clock):
    item = service.create_maintenance("admin", "m", block(product["id"]))
    clock.now = clock.now.replace(day=2, hour=10)
    results = race(
        lambda: service.transition_maintenance("admin", item["id"], "start"),
        lambda: service.transition_maintenance("admin", item["id"], "cancel"),
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count("invalid_transition") == 1
    assert (
        len(
            [
                event
                for event in service.audit()
                if event["action"] in {"maintenance.start", "maintenance.cancel"}
            ]
        )
        == 1
    )


def test_maintenance_audit_failure_rolls_back_and_key_retries(service, product, db):
    from sqlalchemy import event

    triggered = False

    def fail_after_audit(conn, cursor, statement, parameters, context, executemany):
        nonlocal triggered
        if (
            not triggered
            and statement.lstrip().upper().startswith("INSERT")
            and "audit" in statement
        ):
            triggered = True
            conn.exec_driver_sql("SELECT * FROM intentionally_missing_relation")

    event.listen(db.engine, "after_cursor_execute", fail_after_audit)
    try:
        with pytest.raises(DomainError) as error:
            service.create_maintenance("admin", "m", block(product["id"]))
        assert error.value.code == "storage_error"
    finally:
        event.remove(db.engine, "after_cursor_execute", fail_after_audit)
    assert service.list_maintenance() == []
    assert [row["action"] for row in service.audit()] == ["product.create"]
    assert service.create_maintenance("admin", "m", block(product["id"]))["status"] == "planned"


def test_booking_identifier_validation_does_not_rewrite_mutation_target(service, product):
    booking = service.create_booking("alice", "b", request(product["id"]))
    with pytest.raises(DomainError) as error:
        service.transition("alice", " " + booking["id"], "confirm")
    assert error.value.status == 422
    assert service.get_booking(booking["id"])["status"] == "hold"
    assert len(service.audit(booking["id"])) == 1
