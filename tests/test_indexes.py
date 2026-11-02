from conftest import request
from sqlalchemy import inspect, select

from orderflow import db as schema

INDEX_NAME = "ix_booking_items_product_booking_quantity"


def indexes(database):
    with database.transaction() as conn:
        return inspect(conn).get_indexes(
            "booking_items", schema=conn.schema_for_object(schema.items)
        )


def test_new_database_has_covering_pool_index(db):
    matches = [row for row in indexes(db) if row["name"] == INDEX_NAME]
    assert len(matches) == 1
    assert matches[0]["column_names"] == ["product_id", "booking_id", "quantity"]


def test_existing_database_explicit_index_preserves_data_and_startup_does_not_build_it(
    db, service, product
):
    booking = service.create_booking("alice", "same", request(product["id"]))
    with db.transaction(write=True) as conn:
        schema.booking_pool_index.drop(conn)
    before = service.audit()
    db.initialize()
    assert INDEX_NAME not in {row["name"] for row in indexes(db)}
    for _ in range(2):
        db.create_indexes()
    assert INDEX_NAME in {row["name"] for row in indexes(db)}
    assert service.audit() == before
    assert service.get_booking(booking["id"]) == booking
    assert service.create_booking("alice", "same", request(product["id"])) == booking
    with db.transaction() as conn:
        assert len(conn.execute(select(schema.idempotency)).all()) == 1


def test_create_indexes_cli_uses_explicit_database_without_starting_server(db, monkeypatch, capsys):
    from orderflow.cli import main

    monkeypatch.setattr(
        "sys.argv", ["orderflow", "create-indexes", "--database", "sqlite:///explicit-test.db"]
    )
    monkeypatch.setenv("ORDERFLOW_DATABASE_URL", "sqlite:///original-test.db")
    received = []
    monkeypatch.setattr("orderflow.cli.Database", lambda url: received.append(url) or db)
    monkeypatch.setattr(
        "orderflow.cli.uvicorn.run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Сервер не должен запускаться")
        ),
    )
    main()
    assert received == ["sqlite:///explicit-test.db"]
    assert "Индекс проверен и создан при необходимости" in capsys.readouterr().out


def test_index_cli_busy_message_does_not_require_idempotency_key(db, monkeypatch, capsys):
    import pytest

    from orderflow.cli import main
    from orderflow.errors import DomainError

    monkeypatch.setenv("ORDERFLOW_DATABASE_URL", "sqlite:///test.db")
    monkeypatch.setattr("sys.argv", ["orderflow", "create-indexes"])
    monkeypatch.setattr("orderflow.cli.Database", lambda url: db)

    def busy():
        raise DomainError("storage_busy", "Повторите создание с тем же ключом запроса", status=503)

    monkeypatch.setattr(db, "create_indexes", busy)
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 1
    message = capsys.readouterr().err
    assert "storage_busy" in message and "повторите create-indexes" in message
    assert "ключ" not in message
