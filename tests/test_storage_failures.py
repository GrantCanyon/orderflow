from time import monotonic

import pytest
from conftest import request
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select

from orderflow.api import create_app
from orderflow.db import Database, audit_events, bookings, idempotency, items, products
from orderflow.errors import DomainError


def counts(db):
    with db.transaction() as connection:
        return {
            table.name: connection.execute(select(func.count()).select_from(table)).scalar_one()
            for table in (bookings, items, idempotency, audit_events)
        }


@pytest.mark.parametrize("stage", ["bookings", "booking_items", "idempotency", "audit"])
def test_database_failure_between_writes_rolls_back_and_key_can_retry(service, db, product, stage):
    initial = counts(db)
    fired = False

    def fail_after_insert(connection, cursor, statement, parameters, context, many):
        nonlocal fired
        compiled = context.compiled
        if (
            not fired
            and compiled is not None
            and context.isinsert
            and compiled.statement.table.name == stage
        ):
            fired = True
            # Реальная ошибка SQL в БД; точка отказа существует только в этом тесте.
            cursor.execute('SELECT * FROM "__orderflow_injected_fault__"')

    event.listen(db.engine, "after_cursor_execute", fail_after_insert)
    try:
        with pytest.raises(DomainError) as failure:
            service.create_booking("alice", "fault-key", request(product["id"]))
        assert failure.value.code == "storage_error"
        assert failure.value.status == 500
        assert "SELECT" not in str(failure.value)
    finally:
        event.remove(db.engine, "after_cursor_execute", fail_after_insert)
    assert fired
    assert counts(db) == initial
    booking = service.create_booking("alice", "fault-key", request(product["id"]))
    assert booking["status"] == "hold"
    assert counts(db) == {"bookings": 1, "booking_items": 1, "idempotency": 1, "audit": 2}


def test_real_lock_timeout_returns_safe_http_and_retry_succeeds(db, product, clock):
    limited = Database(db.engine.url, lock_timeout_ms=100)
    limited.engine = limited.engine.execution_options(**db.engine.get_execution_options())
    credentials = {"operator-key": {"id": "operator", "name": "Оператор", "role": "operator"}}
    headers = {"X-API-Key": "operator-key", "Idempotency-Key": "wait-key"}
    try:
        with TestClient(create_app(db=limited, clock=clock, principals=credentials)) as api:
            with db.transaction(write=True) as holder:
                holder.execute(
                    select(products).where(products.c.id == product["id"]).with_for_update()
                )
                start = monotonic()
                response = api.post("/api/bookings", headers=headers, json=request(product["id"]))
                elapsed = monotonic() - start
                assert response.status_code == 503
                assert response.json()["error"]["code"] == "storage_busy"
                assert response.json()["error"]["details"]["retryable"] is True
                assert response.headers["Retry-After"] == "1"
                assert 0.05 <= elapsed < 3
                for forbidden in (
                    "SELECT",
                    "INSERT",
                    "operator-key",
                    "wait-key",
                    "psycopg",
                    "sqlite",
                ):
                    assert forbidden not in response.text
            assert counts(db) == {"bookings": 0, "booking_items": 0, "idempotency": 0, "audit": 1}
            retry = api.post("/api/bookings", headers=headers, json=request(product["id"]))
            assert retry.status_code == 201
            replay = api.post("/api/bookings", headers=headers, json=request(product["id"]))
            assert replay.json()["id"] == retry.json()["id"]
            assert counts(db) == {"bookings": 1, "booking_items": 1, "idempotency": 1, "audit": 2}
    finally:
        limited.engine.dispose()


@pytest.mark.parametrize("value", [0, 49, 30001, True, 1.5, "100"])
def test_timeout_configuration_rejects_invalid_values(tmp_path, value):
    with pytest.raises(ValueError):
        Database(f"sqlite:///{tmp_path / 'invalid.db'}", lock_timeout_ms=value)


def test_timeout_configuration_can_come_from_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("ORDERFLOW_LOCK_TIMEOUT_MS", "125")
    database = Database(f"sqlite:///{tmp_path / 'timeout.db'}")
    assert database.lock_timeout_ms == 125
    database.engine.dispose()


@pytest.mark.parametrize(
    "kind, expected_code, expected_status",
    [
        ("deadlock", "transaction_retry", 503),
        ("invalidated", "storage_unavailable", 503),
        ("unknown", "storage_error", 500),
    ],
)
def test_storage_classification_does_not_expose_sql_or_driver_messages(
    kind, expected_code, expected_status
):
    from psycopg import OperationalError as DriverError
    from psycopg.errors import DeadlockDetected
    from sqlalchemy.exc import OperationalError

    from orderflow.errors import storage_error

    original = (
        DeadlockDetected("secret-driver-message")
        if kind == "deadlock"
        else DriverError("secret-driver-message")
    )
    wrapped = OperationalError(
        "secret-sql",
        {"api_key": "secret-parameter"},
        original,
        connection_invalidated=kind == "invalidated",
    )
    mapped = storage_error(wrapped)
    assert mapped.code == expected_code
    assert mapped.status == expected_status
    assert mapped.details["retryable"] is (expected_status == 503)
    assert "secret" not in str(mapped)
    assert "api_key" not in str(mapped.details)
