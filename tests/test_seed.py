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
