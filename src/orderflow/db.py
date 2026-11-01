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
)

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
    def __init__(self, url):
        options = (
            {"connect_args": {"check_same_thread": False, "timeout": 30}}
            if url.startswith("sqlite")
            else {}
        )
        self.engine = create_engine(url, pool_pre_ping=True, **options)
        self.sqlite = self.engine.dialect.name == "sqlite"
        if self.sqlite:

            @event.listens_for(self.engine, "connect")
            def sqlite_setup(connection, _):
                connection.execute("PRAGMA foreign_keys=ON")

    def initialize(self):
        metadata.create_all(self.engine)

    @contextmanager
    def transaction(self, write=False):
        with self.engine.connect() as connection:
            if write and self.sqlite:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                connection.begin()
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
