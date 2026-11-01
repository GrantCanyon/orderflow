import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.schema import CreateSchema, DropSchema

from orderflow.db import Database
from orderflow.service import RentalService


@pytest.fixture
def clock():
    class Clock:
        now = datetime(2030, 1, 1, 8, tzinfo=UTC)

        def __call__(self):
            return self.now

    return Clock()


@pytest.fixture
def db(tmp_path):
    url = os.getenv("ORDERFLOW_TEST_DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    database = Database(url)
    schema = None
    if database.engine.dialect.name == "postgresql":
        # Даже ошибочный URL не даёт тестам удалять таблицы приложения.
        schema = f"test_{uuid4().hex}"
        with database.engine.begin() as connection:
            connection.execute(CreateSchema(schema))
        database.engine = database.engine.execution_options(schema_translate_map={None: schema})
    elif os.getenv("ORDERFLOW_TEST_DATABASE_URL"):
        raise RuntimeError("Внешний тестовый URL должен указывать на PostgreSQL")
    try:
        database.initialize()
        yield database
    finally:
        if schema:
            with database.engine.begin() as connection:
                connection.execute(DropSchema(schema, cascade=True))
        database.engine.dispose()


@pytest.fixture
def service(db, clock):
    return RentalService(db, clock)


@pytest.fixture
def product(service):
    return service.create_product(
        "admin",
        sku="SPK-01",
        name="Активная колонка",
        category="Звук",
        capacity=3,
        daily_rate_cents=2500,
    )


def request(product_id, start="2030-01-02T10:00:00Z", end="2030-01-02T14:00:00Z", quantity=1):
    return {
        "label": "Учебное мероприятие",
        "start_at": start,
        "end_at": end,
        "items": [{"product_id": product_id, "quantity": quantity}],
        "ttl_minutes": 15,
    }
