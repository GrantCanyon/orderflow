from datetime import UTC, datetime

from orderflow.seed import seed_demo
from orderflow.service import RentalService


def test_seed_is_repeatable_and_exposes_meaningful_states(db):
    now = datetime(2030, 4, 2, 12, tzinfo=UTC)
    assert seed_demo(db, now) is True
    service = RentalService(db, lambda: now)
    before = service.list_bookings()
    assert len(service.list_products()) == 8
    assert {"hold", "confirmed", "checked_out", "returned"} <= {row["status"] for row in before}
    assert any(row["overdue"] for row in before)
    assert seed_demo(db, now) is False
    assert len(service.list_bookings()) == len(before)


def test_maintenance_seed_includes_planned_and_overdue(db):
    now = datetime(2030, 4, 2, 12, tzinfo=UTC)
    seed_demo(db, now)
    rows = RentalService(db, lambda: now).list_maintenance()
    assert {row["status"] for row in rows} == {"planned", "active"}
    assert any(row["overdue"] for row in rows)
