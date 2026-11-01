from datetime import timedelta

import pytest
from conftest import request

from orderflow.errors import DomainError


def test_adjacent_bookings_reuse_capacity(service, product):
    service.create_booking("alice", "first", request(product["id"], quantity=3))
    result = service.create_booking(
        "alice",
        "next",
        request(
            product["id"], start="2030-01-02T14:00:00Z", end="2030-01-02T18:00:00Z", quantity=3
        ),
    )
    assert result["status"] == "hold"


def test_staggered_intersections_use_peak_not_sum(service, product):
    service.create_booking(
        "alice",
        "morning",
        request(
            product["id"], start="2030-01-02T10:00:00Z", end="2030-01-02T12:00:00Z", quantity=2
        ),
    )
    service.create_booking(
        "alice",
        "afternoon",
        request(
            product["id"], start="2030-01-02T12:00:00Z", end="2030-01-02T14:00:00Z", quantity=2
        ),
    )
    result = service.create_booking("alice", "bridge", request(product["id"]))
    assert result["total_cents"] == 2500
    row = service.availability("2030-01-02T10:00:00Z", "2030-01-02T14:00:00Z")[0]
    assert row["available"] == 0
    assert max(segment["reserved"] for segment in row["segments"]) == 3


def test_conflict_reports_interval_and_rolls_back_all_items(service, product):
    other = service.create_product(
        "admin", sku="MIC", name="Микрофон", category="Звук", capacity=1, daily_rate_cents=1000
    )
    occupied = service.create_booking("alice", "taken", request(other["id"]))
    payload = request(product["id"], quantity=3)
    payload["items"].append({"product_id": other["id"], "quantity": 1})
    with pytest.raises(DomainError) as error:
        service.create_booking("alice", "atomic", payload)
    assert error.value.code == "capacity_conflict"
    conflict = error.value.details["conflicts"][0]
    assert conflict["product_id"] == other["id"]
    assert occupied["id"] in conflict["booking_ids"]
    assert len(service.list_bookings()) == 1
    assert service.availability(payload["start_at"], payload["end_at"])[0]["available"] == 3
    assert len(service.audit()) == 3  # Две позиции каталога и одна успешная бронь


def test_expiry_is_effective_exactly_at_deadline(service, product, clock):
    hold = service.create_booking("alice", "ttl", request(product["id"], quantity=3))
    clock.now += timedelta(minutes=15)
    assert service.get_booking(hold["id"])["status"] == "expired"
    assert service.availability(hold["start_at"], hold["end_at"])[0]["available"] == 3
    with pytest.raises(DomainError, match="expired"):
        service.transition("alice", hold["id"], "confirm")
    assert service.expire_holds("admin") == 1
    assert service.expire_holds("admin") == 0


def test_idempotency_replays_normalized_input_and_rejects_reuse(service, product):
    original = service.create_booking("alice", "key", request(product["id"]))
    equivalent = request(product["id"], start="2030-01-02T13:00:00+03:00")
    assert service.create_booking("alice", "key", equivalent)["id"] == original["id"]
    with pytest.raises(DomainError) as error:
        service.create_booking("alice", "key", request(product["id"], quantity=2))
    assert error.value.code == "idempotency_conflict"
    assert service.create_booking("bob", "key", request(product["id"]))["id"] != original["id"]
    assert len(service.list_bookings()) == 2


def test_failed_request_does_not_consume_idempotency_key(service, product):
    with pytest.raises(DomainError):
        service.create_booking("alice", "retry", request(product["id"], quantity=4))
    assert service.create_booking("alice", "retry", request(product["id"]))["status"] == "hold"


def test_quote_snapshots_prices_and_rounds_partial_days(service, product):
    payload = request(product["id"], end="2030-01-03T10:00:01Z", quantity=2)
    booking = service.create_booking("alice", "quote", payload)
    assert booking["total_cents"] == 10000
    service.update_product("admin", product["id"], daily_rate_cents=9900)
    assert service.get_booking(booking["id"])["total_cents"] == 10000
    assert service.get_booking(booking["id"])["items"][0]["rate_cents"] == 2500


def test_state_machine_and_audit(service, product, clock):
    booking = service.create_booking("alice", "flow", request(product["id"]))
    with pytest.raises(DomainError):
        service.transition("alice", booking["id"], "return")
    confirmed = service.transition("alice", booking["id"], "confirm")
    assert confirmed["status"] == "confirmed"
    with pytest.raises(DomainError):
        service.transition("alice", booking["id"], "check_out")
    clock.now = clock.now.replace(day=2, hour=10)
    assert service.transition("alice", booking["id"], "check_out")["status"] == "checked_out"
    with pytest.raises(DomainError):
        service.transition("alice", booking["id"], "cancel")
    assert service.transition("alice", booking["id"], "return")["status"] == "returned"
    assert [event["action"] for event in service.audit(booking_id=booking["id"])] == [
        "booking.return",
        "booking.check_out",
        "booking.confirm",
        "booking.create",
    ]


def test_reschedule_checks_new_interval_and_keeps_original_on_conflict(service, product):
    first = service.create_booking("alice", "first", request(product["id"], quantity=3))
    second = service.create_booking(
        "alice",
        "second",
        request(
            product["id"], start="2030-01-02T14:00:00Z", end="2030-01-02T18:00:00Z", quantity=3
        ),
    )
    with pytest.raises(DomainError):
        service.reschedule("alice", second["id"], first["start_at"], first["end_at"])
    assert service.get_booking(second["id"])["start_at"] == "2030-01-02T14:00:00Z"
    changed = service.reschedule(
        "alice", second["id"], "2030-01-03T10:00:00Z", "2030-01-05T10:00:00Z"
    )
    assert changed["total_cents"] == 15000


def test_capacity_reduction_uses_future_peak(service, product):
    service.create_booking("alice", "morning", request(product["id"], quantity=2))
    with pytest.raises(DomainError) as error:
        service.update_product("admin", product["id"], capacity=1)
    assert error.value.code == "capacity_conflict"
    assert service.update_product("admin", product["id"], capacity=2)["capacity"] == 2


@pytest.mark.parametrize(
    "change",
    [
        {"start_at": "2030-01-02T10:00:00"},
        {"end_at": "2030-01-02T10:00:00Z"},
        {"start_at": "2029-01-01T10:00:00Z"},
        {"ttl_minutes": 0},
        {"items": []},
        {"items": [{"product_id": 1, "quantity": 0}]},
        {"items": [{"product_id": 1, "quantity": True}]},
        {"items": [{"product_id": 1, "quantity": 1}, {"product_id": 1, "quantity": 1}]},
    ],
)
def test_reject_invalid_requests(service, product, change):
    payload = request(product["id"])
    payload.update(change)
    with pytest.raises(DomainError):
        service.create_booking("alice", "invalid", payload)
    assert service.list_bookings() == []


def test_reschedule_rechecks_future_after_waiting_for_product_lock(
    service, product, clock, monkeypatch
):
    booking = service.create_booking("alice", "wait", request(product["id"]))
    locked_booking = service._locked_booking

    def delayed_lock(connection, booking_id):
        result = locked_booking(connection, booking_id)
        clock.now += timedelta(seconds=2)
        return result

    monkeypatch.setattr(service, "_locked_booking", delayed_lock)
    with pytest.raises(DomainError) as error:
        service.reschedule("alice", booking["id"], "2030-01-01T08:00:01Z", "2030-01-01T10:00:00Z")
    assert error.value.code == "past_interval"
    assert service.get_booking(booking["id"])["start_at"] == "2030-01-02T10:00:00Z"
    assert len(service.audit(booking_id=booking["id"])) == 1


def test_total_larger_than_32_bit_integer_is_supported(service):
    product = service.create_product(
        "admin",
        sku="LARGE",
        name="Большой комплект",
        category="Комплекты",
        capacity=100,
        daily_rate_cents=100_000_000,
    )
    booking = service.create_booking("alice", "large", request(product["id"], quantity=100))
    assert booking["total_cents"] == 10_000_000_000
    assert service.get_booking(booking["id"])["total_cents"] == 10_000_000_000


def test_total_outside_exact_json_integer_range_is_rejected_atomically(service):
    product = service.create_product(
        "admin",
        sku="OVERFLOW",
        name="Большой комплект",
        category="Комплекты",
        capacity=1_000_000,
        daily_rate_cents=100_000_000,
    )
    payload = request(product["id"], end="2031-01-02T10:00:00Z", quantity=1_000_000)
    with pytest.raises(DomainError) as error:
        service.create_booking("alice", "overflow", payload)
    assert error.value.code == "quote_too_large"
    assert service.list_bookings() == []
    assert len(service.audit()) == 1
    payload["end_at"] = "2030-01-03T10:00:00Z"
    assert (
        service.create_booking("alice", "overflow", payload)["total_cents"] == 100_000_000_000_000
    )


def test_reschedule_cannot_overflow_quote_and_preserves_booking(service):
    product = service.create_product(
        "admin",
        sku="OVERFLOW",
        name="Большой комплект",
        category="Комплекты",
        capacity=1_000_000,
        daily_rate_cents=100_000_000,
    )
    booking = service.create_booking("alice", "initial", request(product["id"], quantity=1_000_000))
    with pytest.raises(DomainError) as error:
        service.reschedule("alice", booking["id"], "2030-01-02T10:00:00Z", "2031-01-02T10:00:00Z")
    assert error.value.code == "quote_too_large"
    assert service.get_booking(booking["id"])["end_at"] == booking["end_at"]
    assert service.get_booking(booking["id"])["total_cents"] == 100_000_000_000_000
    assert len(service.audit(booking_id=booking["id"])) == 1
