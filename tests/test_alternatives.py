from datetime import timedelta

import pytest
from conftest import request

from orderflow.errors import DomainError


def search(product_id, **changes):
    payload = {
        "start_at": "2030-01-02T10:00:00Z",
        "end_at": "2030-01-02T11:00:00Z",
        "search_until": "2030-01-02T18:00:00Z",
        "items": [{"product_id": product_id, "quantity": 1}],
        "limit": 3,
    }
    payload.update(changes)
    return payload


def reserve(service, product_id, key, start, end, quantity=3):
    return service.create_booking("alice", key, request(product_id, start, end, quantity))


def test_free_original_interval_is_first_and_search_has_no_side_effects(service, product):
    result = service.alternatives(search(product["id"]))
    assert result["advisory"] is True
    assert result["duration_microseconds"] == 3_600_000_000
    assert result["generated_at"] == "2030-01-01T08:00:00Z"
    assert len(result["alternatives"]) == 1
    option = result["alternatives"][0]
    assert option["start_at"] == "2030-01-02T10:00:00Z"
    assert option["end_at"] == "2030-01-02T11:00:00Z"
    assert option["latest_start"] == "2030-01-02T17:00:00Z"
    assert option["total_cents"] == 2500
    assert option["items"][0]["minimum_available"] == 3
    assert service.list_bookings() == []
    assert len(service.audit()) == 1


def test_exact_microsecond_gap_is_not_lost_to_grid_or_rounding(service, product):
    reserve(service, product["id"], "before", "2030-01-02T10:00:00Z", "2030-01-02T11:00:00.000003Z")
    reserve(service, product["id"], "after", "2030-01-02T12:00:00.000004Z", "2030-01-02T18:00:00Z")
    result = service.alternatives(
        search(
            product["id"],
            start_at="2030-01-02T10:00:00.000001Z",
            end_at="2030-01-02T11:00:00.000002Z",
        )
    )
    assert result["duration_microseconds"] == 3_600_000_001
    assert len(result["alternatives"]) == 1
    option = result["alternatives"][0]
    assert option["start_at"] == option["latest_start"] == "2030-01-02T11:00:00.000003Z"
    assert option["end_at"] == "2030-01-02T12:00:00.000004Z"


def test_all_items_share_one_window_and_adjacent_blockers_merge(service, product):
    other = service.create_product(
        "admin", sku="OTHER", name="Микрофон", category="Звук", capacity=3, daily_rate_cents=1000
    )
    reserve(service, product["id"], "first", "2030-01-02T10:00:00Z", "2030-01-02T12:00:00Z")
    reserve(service, other["id"], "second", "2030-01-02T12:00:00Z", "2030-01-02T14:00:00Z")
    result = service.alternatives(
        search(
            product["id"],
            items=[
                {"product_id": product["id"], "quantity": 1},
                {"product_id": other["id"], "quantity": 2},
            ],
        )
    )
    assert len(result["alternatives"]) == 1
    assert result["alternatives"][0]["start_at"] == "2030-01-02T14:00:00Z"
    assert result["alternatives"][0]["total_cents"] == 4500


def test_search_uses_peak_and_returns_distinct_free_components_in_time_order(service, product):
    reserve(service, product["id"], "morning", "2030-01-02T10:00:00Z", "2030-01-02T12:00:00Z", 2)
    reserve(service, product["id"], "midday", "2030-01-02T12:00:00Z", "2030-01-02T14:00:00Z", 2)
    result = service.alternatives(search(product["id"], end_at="2030-01-02T14:00:00Z"))
    assert result["alternatives"][0]["start_at"] == "2030-01-02T10:00:00Z"
    assert result["alternatives"][0]["items"][0]["minimum_available"] == 1
    reserve(service, product["id"], "bridge", "2030-01-02T11:00:00Z", "2030-01-02T12:00:00Z", 1)
    reserve(service, product["id"], "bridge-2", "2030-01-02T13:00:00Z", "2030-01-02T14:00:00Z", 1)
    result = service.alternatives(search(product["id"], limit=2))
    assert [row["start_at"] for row in result["alternatives"]] == [
        "2030-01-02T10:00:00Z",
        "2030-01-02T12:00:00Z",
    ]
    assert result["alternatives"][0]["latest_start"] == "2030-01-02T10:00:00Z"


def test_search_never_offers_a_window_crossing_a_busy_segment(service, product):
    reserve(service, product["id"], "a", "2030-01-02T10:00:00Z", "2030-01-02T11:00:00Z")
    reserve(service, product["id"], "b", "2030-01-02T12:00:00Z", "2030-01-02T14:00:00Z")
    result = service.alternatives(
        search(product["id"], end_at="2030-01-02T12:00:00Z", search_until="2030-01-02T14:00:00Z")
    )
    assert result["alternatives"] == []
    assert result["reason"] == "no_window"


def test_holds_expire_at_exact_snapshot_and_future_expiry_is_not_predicted(service, product, clock):
    reserve(service, product["id"], "ttl", "2030-01-02T10:00:00Z", "2030-01-02T18:00:00Z")
    assert service.alternatives(search(product["id"]))["alternatives"] == []
    clock.now += timedelta(minutes=15)
    assert (
        service.alternatives(search(product["id"]))["alternatives"][0]["start_at"]
        == "2030-01-02T10:00:00Z"
    )


def test_quantity_above_total_fleet_returns_explanation(service, product):
    result = service.alternatives(
        search(product["id"], items=[{"product_id": product["id"], "quantity": 4}])
    )
    assert result["alternatives"] == []
    assert result["reason"] == "insufficient_fleet"
    assert result["shortages"][0]["capacity"] == 3
    assert result["shortages"][0]["requested"] == 4


def test_unknown_product_is_not_silently_ignored(service, product):
    with pytest.raises(DomainError) as error:
        service.alternatives(search(999999))
    assert error.value.code == "not_found"


@pytest.mark.parametrize(
    "changes",
    [
        {"start_at": "2029-01-02T10:00:00Z", "end_at": "2029-01-02T11:00:00Z"},
        {"start_at": "2030-01-02T10:00:00"},
        {"search_until": "2030-01-02T10:30:00Z"},
        {"search_until": "2030-02-02T10:00:00Z"},
        {"end_at": "2030-01-02T10:00:00Z"},
        {"items": []},
        {"limit": 0},
        {"limit": 6},
        {"limit": True},
    ],
)
def test_invalid_search_is_rejected(service, product, changes):
    with pytest.raises(DomainError):
        service.alternatives(search(product["id"], **changes))


def test_upper_calendar_boundary_does_not_overflow(service, product):
    result = service.alternatives(
        search(
            product["id"],
            start_at="9999-12-30T10:00:00Z",
            end_at="9999-12-30T11:00:00Z",
            search_until="9999-12-31T23:00:00Z",
        )
    )
    assert result["alternatives"][0]["latest_start"] == "9999-12-31T22:00:00Z"


def test_quote_uses_current_rates_and_rejects_unsafe_total(service, product):
    service.update_product("admin", product["id"], daily_rate_cents=4000)
    assert service.alternatives(search(product["id"]))["alternatives"][0]["total_cents"] == 4000
    large = [
        service.create_product(
            "admin",
            sku=f"LARGE-{index}",
            name="Комплект",
            category="Звук",
            capacity=1_000_000,
            daily_rate_cents=100_000_000,
        )
        for index in range(4)
    ]
    with pytest.raises(DomainError) as error:
        service.alternatives(
            search(
                product["id"],
                end_at="2030-02-01T10:00:00Z",
                search_until="2030-02-01T10:00:00Z",
                items=[{"product_id": row["id"], "quantity": 1_000_000} for row in large],
            )
        )
    assert error.value.code == "quote_too_large"


def test_calendar_boundary_outside_utc_range_is_validation_error(service, product):
    with pytest.raises(DomainError) as error:
        service.alternatives(
            search(
                product["id"],
                start_at="9999-12-31T23:30:00-01:00",
                end_at="9999-12-31T23:45:00-01:00",
            )
        )
    assert error.value.status == 422


def test_overdue_physical_stock_is_warning_not_infinite_future_block(service, product, clock):
    booking = reserve(service, product["id"], "out", "2030-01-02T08:00:00Z", "2030-01-02T10:00:00Z")
    service.transition("alice", booking["id"], "confirm")
    clock.now = clock.now.replace(day=2, hour=8)
    service.transition("alice", booking["id"], "check_out")
    clock.now = clock.now.replace(hour=10)
    result = service.alternatives(search(product["id"]))
    assert result["alternatives"][0]["start_at"] == "2030-01-02T10:00:00Z"
    assert result["items"][0]["physical_available"] == 0
    assert result["items"][0]["overdue_quantity"] == 3


def test_recommendation_can_become_stale_and_normal_create_rechecks(service, product):
    suggestion = service.alternatives(search(product["id"]))["alternatives"][0]
    reserve(service, product["id"], "competitor", suggestion["start_at"], suggestion["end_at"])
    with pytest.raises(DomainError) as error:
        reserve(service, product["id"], "stale", suggestion["start_at"], suggestion["end_at"], 1)
    assert error.value.code == "capacity_conflict"
    assert len(service.list_bookings()) == 1
