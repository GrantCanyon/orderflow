import os
from contextlib import contextmanager

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    create_engine,
    event,
    func,
    select,
)
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from .errors import storage_error

metadata = MetaData()
products = Table(
    "products",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("sku", String(40), unique=True, nullable=False),
    Column("name", String(120), nullable=False),
    Column("category", String(80), nullable=False),
    Column("capacity", Integer, nullable=False),
    Column("daily_rate_cents", BigInteger, nullable=False),
    CheckConstraint("capacity >= 0"),
    CheckConstraint("daily_rate_cents >= 0"),
)
bookings = Table(
    "bookings",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("reference", String(20), unique=True, nullable=False),
    Column("label", String(120), nullable=False),
    Column("start_at", DateTime(timezone=True), nullable=False),
    Column("end_at", DateTime(timezone=True), nullable=False),
    Column("status", String(20), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("created_by", String(80), nullable=False),
    Column("created_by_name", String(80), nullable=False),
    Column("total_cents", BigInteger, nullable=False),
    CheckConstraint("end_at > start_at"),
    CheckConstraint("total_cents >= 0 AND total_cents <= 9007199254740991"),
    CheckConstraint(
        "status IN ('hold','confirmed','checked_out','returned','cancelled','expired')"
    ),
)
items = Table(
    "booking_items",
    metadata,
    Column("booking_id", ForeignKey("bookings.id"), primary_key=True),
    Column("product_id", ForeignKey("products.id"), primary_key=True),
    Column("quantity", Integer, nullable=False),
    Column("rate_cents", BigInteger, nullable=False),
    CheckConstraint("quantity > 0"),
    CheckConstraint("rate_cents >= 0"),
)
idempotency = Table(
    "idempotency",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("principal", String(80), nullable=False),
    Column("key", String(120), nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("booking_id", ForeignKey("bookings.id"), nullable=False),
    UniqueConstraint("principal", "key", name="uq_idempotency_principal_key"),
)
audit_events = Table(
    "audit",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("booking_id", ForeignKey("bookings.id")),
    Column("actor", String(80), nullable=False),
    Column("actor_name", String(80), nullable=False),
    Column("action", String(80), nullable=False),
    Column("details", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)


class Database:
    def __init__(self, url, lock_timeout_ms=None):
        if lock_timeout_ms is None:
            raw = os.getenv("ORDERFLOW_LOCK_TIMEOUT_MS", "2000")
            if not raw.isascii() or not raw.isdecimal():
                raise ValueError(
                    "ORDERFLOW_LOCK_TIMEOUT_MS должен быть целым числом от 50 до 30000"
                )
            lock_timeout_ms = int(raw)
        if type(lock_timeout_ms) is not int or not 50 <= lock_timeout_ms <= 30000:
            raise ValueError("Ожидание блокировки должно быть от 50 до 30000 миллисекунд")
        self.lock_timeout_ms = lock_timeout_ms
        options = (
            {"connect_args": {"check_same_thread": False, "timeout": lock_timeout_ms / 1000}}
            if make_url(url).get_backend_name() == "sqlite"
            else {}
        )
        self.engine = create_engine(url, pool_pre_ping=True, hide_parameters=True, **options)
        self.sqlite = self.engine.dialect.name == "sqlite"
        if self.sqlite:

            @event.listens_for(self.engine, "connect")
            def sqlite_setup(connection, _):
                connection.execute("PRAGMA foreign_keys=ON")

    def initialize(self):
        metadata.create_all(self.engine)

    @contextmanager
    def transaction(self, write=False):
        try:
            with self.engine.connect() as connection:
                try:
                    if write and self.sqlite:
                        connection.exec_driver_sql("BEGIN IMMEDIATE")
                    else:
                        connection.begin()
                    if self.engine.dialect.name == "postgresql":
                        connection.execute(
                            select(
                                func.set_config("lock_timeout", f"{self.lock_timeout_ms}ms", True)
                            )
                        )
                    yield connection
                    connection.commit()
                except BaseException:
                    try:
                        connection.rollback()
                    except SQLAlchemyError:
                        # Потеря соединения при COMMIT не позволяет обещать откат.
                        pass
                    raise
        except IntegrityError:
            # Уникальные ограничения обрабатываются сервисом (артикул и ключ повтора).
            raise
        except SQLAlchemyError as error:
            raise storage_error(error) from None
