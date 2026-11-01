from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from conftest import request

from orderflow.errors import DomainError


def race(*functions):
    barrier = Barrier(len(functions))

    def run(function):
        barrier.wait(timeout=10)
        try:
            return function()
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=len(functions)) as executor:
        futures = [executor.submit(run, function) for function in functions]
        return [future.result(timeout=40) for future in futures]


def test_competing_clients_cannot_oversell(service, product):
    results = race(
        *[
            lambda index=index: service.create_booking(
                "alice", f"race-{index}", request(product["id"], quantity=2)
            )
            for index in range(6)
        ]
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count("capacity_conflict") == 5
    assert len(service.list_bookings()) == 1
    assert len(service.audit()) == 2


def test_same_key_race_replays_one_booking(service, product):
    results = race(
        *[lambda: service.create_booking("alice", "same", request(product["id"])) for _ in range(5)]
    )
    assert len({result["id"] for result in results}) == 1
    assert len(service.list_bookings()) == 1
    assert len(service.audit()) == 2


def test_same_key_disjoint_products_rolls_back_loser(service, product):
    other = service.create_product(
        "admin",
        sku="OTHER",
        name="Вторая колонка",
        category="Звук",
        capacity=3,
        daily_rate_cents=1000,
    )
    results = race(
        lambda: service.create_booking("alice", "same", request(product["id"])),
        lambda: service.create_booking("alice", "same", request(other["id"])),
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count("idempotency_conflict") == 1
    assert len(service.list_bookings()) == 1
    assert len(service.audit()) == 3


def test_opposite_item_order_cannot_deadlock_or_oversell(service, product):
    other = service.create_product(
        "admin",
        sku="OTHER",
        name="Вторая колонка",
        category="Звук",
        capacity=1,
        daily_rate_cents=1000,
    )
    first = request(product["id"], quantity=3)
    first["items"].append({"product_id": other["id"], "quantity": 1})
    second = {**first, "items": list(reversed(first["items"]))}
    results = race(
        lambda: service.create_booking("alice", "a", first),
        lambda: service.create_booking("bob", "b", second),
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count("capacity_conflict") == 1


def checked_out_pair(service, product, clock):
    early = service.create_booking("alice", "early", request(product["id"], quantity=3))
    late = service.create_booking(
        "alice",
        "late",
        request(
            product["id"], start="2030-01-02T14:00:00Z", end="2030-01-02T18:00:00Z", quantity=3
        ),
    )
    service.transition("alice", early["id"], "confirm")
    service.transition("alice", late["id"], "confirm")
    clock.now = clock.now.replace(day=2, hour=10)
    service.transition("alice", early["id"], "check_out")
    clock.now = clock.now.replace(hour=14)
    return early, late


def test_late_return_blocks_physical_checkout_even_when_schedule_allows(service, product, clock):
    early, late = checked_out_pair(service, product, clock)
    assert service.get_booking(early["id"])["overdue"] is True
    with pytest.raises(DomainError) as error:
        service.transition("alice", late["id"], "check_out")
    assert error.value.code == "physical_stock_conflict"
    assert service.get_booking(late["id"])["status"] == "confirmed"
    service.transition("alice", early["id"], "return")
    assert service.transition("alice", late["id"], "check_out")["status"] == "checked_out"


def test_inventory_cannot_shrink_below_overdue_physical_stock(service, product, clock):
    early, late = checked_out_pair(service, product, clock)
    service.transition("alice", late["id"], "cancel")
    with pytest.raises(DomainError) as error:
        service.update_product("admin", product["id"], capacity=1)
    assert error.value.code == "physical_stock_conflict"
    service.transition("alice", early["id"], "return")
    assert service.update_product("admin", product["id"], capacity=1)["capacity"] == 1


def test_early_return_releases_schedule_without_refunding_quote(service, product, clock):
    booking = service.create_booking("alice", "early", request(product["id"], quantity=3))
    service.transition("alice", booking["id"], "confirm")
    clock.now = clock.now.replace(day=2, hour=10)
    service.transition("alice", booking["id"], "check_out")
    returned = service.transition("alice", booking["id"], "return")
    assert returned["total_cents"] == 7500
    assert service.availability(booking["start_at"], booking["end_at"])[0]["available"] == 3


def test_inventory_reduction_races_booking_under_same_lock(service, product):
    results = race(
        lambda: service.update_product("admin", product["id"], capacity=1),
        lambda: service.create_booking("alice", "race", request(product["id"], quantity=3)),
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count("capacity_conflict") == 1


def test_availability_separates_scheduled_and_physical_stock(service, product, clock):
    early, late = checked_out_pair(service, product, clock)
    service.transition("alice", late["id"], "cancel")
    row = service.availability(late["start_at"], late["end_at"])[0]
    assert row["available"] == 3
    assert row["physical_available"] == 0
    assert row["overdue_quantity"] == 3
