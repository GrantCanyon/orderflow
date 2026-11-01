import hmac
import json
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import text as sql_text

from .db import Database
from .errors import DomainError
from .identity import Actor
from .service import RentalService


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BookingItem(Payload):
    product_id: Annotated[StrictInt, Field(gt=0)]
    quantity: Annotated[StrictInt, Field(gt=0, le=1_000_000)]


class BookingCreate(Payload):
    label: Annotated[str, Field(min_length=1, max_length=120)]
    start_at: str
    end_at: str
    items: Annotated[list[BookingItem], Field(min_length=1, max_length=50)]
    ttl_minutes: Annotated[StrictInt, Field(ge=1, le=60)] = 15


class Reschedule(Payload):
    start_at: str
    end_at: str


class ProductCreate(Payload):
    sku: Annotated[str, Field(min_length=1, max_length=40)]
    name: Annotated[str, Field(min_length=1, max_length=120)]
    category: Annotated[str, Field(min_length=1, max_length=80)]
    capacity: Annotated[StrictInt, Field(ge=0, le=1_000_000)]
    daily_rate_cents: Annotated[StrictInt, Field(ge=0, le=100_000_000)]


class ProductUpdate(Payload):
    name: str | None = None
    category: str | None = None
    capacity: StrictInt | None = None
    daily_rate_cents: StrictInt | None = None


def create_app(db=None, clock=None, principals=None, demo=False):
    db = db or Database(os.getenv("ORDERFLOW_DATABASE_URL", "sqlite:///orderflow.db"))
    principals = dict(principals or {})
    if demo:
        principals.update(
            {
                f"demo-{role}": {"id": f"demo-{role}", "name": name, "role": role}
                for role, name in (
                    ("admin", "Демо · администратор"),
                    ("operator", "Демо · оператор"),
                    ("viewer", "Демо · наблюдатель"),
                )
            }
        )
    for key, principal in principals.items():
        if (
            not isinstance(principal, dict)
            or not isinstance(principal.get("id"), str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}", principal["id"])
        ):
            raise ValueError(
                "Для каждого ключа нужен устойчивый id пользователя: 1–80 латинских символов"
            )
        if (
            not key
            or not isinstance(principal, dict)
            or principal.get("role") not in {"admin", "operator", "viewer"}
        ):
            raise ValueError("Некорректная конфигурация ключей доступа")
        if not isinstance(principal.get("name"), str) or not 1 <= len(principal["name"]) <= 80:
            raise ValueError("Укажите имя пользователя длиной от 1 до 80 символов")
    service = RentalService(db, clock)

    @asynccontextmanager
    async def lifespan(app):
        db.initialize()
        yield

    app = FastAPI(
        title="OrderFlow · пункт проката",
        docs_url=None,
        redoc_url=None,
        version="0.1.0",
        lifespan=lifespan,
        description="Интервальная доступность, временные резервы и безопасная выдача.",
    )
    app.state.service = service

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, error: DomainError):
        return JSONResponse(
            status_code=error.status,
            content={
                "error": {"code": error.code, "message": str(error), "details": error.details}
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "invalid_input",
                    "message": "Проверьте поля запроса",
                    "details": {
                        "fields": [
                            {"path": list(issue["loc"]), "type": issue["type"]}
                            for issue in error.errors()
                        ]
                    },
                }
            },
        )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    def identity(x_api_key: Annotated[str | None, Header()] = None):
        if x_api_key:
            for key, principal in principals.items():
                if hmac.compare_digest(x_api_key.encode(), key.encode()):
                    return principal
        raise DomainError("unauthorized", "Введите действующий ключ доступа", status=401)

    def operator(principal=Depends(identity)):
        if principal["role"] not in {"admin", "operator"}:
            raise DomainError("forbidden", "Для изменения нужен доступ оператора", status=403)
        return principal

    def admin(principal=Depends(identity)):
        if principal["role"] != "admin":
            raise DomainError("forbidden", "Нужен доступ администратора", status=403)
        return principal

    @app.get("/health")
    def health():
        with db.transaction() as conn:
            conn.execute(sql_text("SELECT 1"))
        return {"status": "ok"}

    @app.get("/api/config")
    def config():
        return {"demo": demo, "currency": "RUB", "default_ttl_minutes": 15}

    @app.get("/api/me")
    def me(principal=Depends(identity)):
        return principal

    @app.get("/api/products")
    def catalog(principal=Depends(identity)):
        return service.list_products()

    @app.post("/api/products", status_code=201)
    def add_product(body: ProductCreate, principal=Depends(admin)):
        return service.create_product(
            Actor(principal["id"], principal["name"]), **body.model_dump()
        )

    @app.patch("/api/products/{product_id}")
    def edit_product(product_id: int, body: ProductUpdate, principal=Depends(admin)):
        return service.update_product(
            Actor(principal["id"], principal["name"]),
            product_id,
            **body.model_dump(exclude_unset=True),
        )

    @app.get("/api/availability")
    def availability(start: str, end: str, principal=Depends(identity)):
        return service.availability(start, end)

    @app.get("/api/bookings")
    def booking_list(
        status: Literal[
            "hold", "confirmed", "checked_out", "returned", "cancelled", "expired", "overdue"
        ]
        | None = None,
        search: Annotated[str | None, Query(max_length=120)] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
        principal=Depends(identity),
    ):
        return service.list_bookings(status, search, limit, offset)

    @app.get("/api/bookings/{booking_id}")
    def booking_detail(booking_id: str, principal=Depends(identity)):
        return service.get_booking(booking_id)

    @app.post("/api/bookings", status_code=201)
    def reserve(
        body: BookingCreate,
        idempotency_key: Annotated[str, Header(min_length=1, max_length=120)],
        principal=Depends(operator),
    ):
        return service.create_booking(
            Actor(principal["id"], principal["name"]), idempotency_key, body.model_dump()
        )

    @app.patch("/api/bookings/{booking_id}/schedule")
    def reschedule(booking_id: str, body: Reschedule, principal=Depends(operator)):
        return service.reschedule(
            Actor(principal["id"], principal["name"]), booking_id, body.start_at, body.end_at
        )

    @app.post("/api/bookings/{booking_id}/{action}")
    def action(booking_id: str, action: str, principal=Depends(operator)):
        return service.transition(Actor(principal["id"], principal["name"]), booking_id, action)

    @app.get("/api/audit")
    def audit(
        booking_id: str | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        before_id: Annotated[int | None, Query(ge=1)] = None,
        principal=Depends(identity),
    ):
        return service.audit(booking_id, limit, before_id)

    @app.post("/api/admin/expire")
    def expire(principal=Depends(admin)):
        return {"expired": service.expire_holds(Actor(principal["id"], principal["name"]))}

    static = Path(__file__).parent / "static"
    if static.is_dir():
        app.mount("/static", StaticFiles(directory=static), name="static")

        @app.get("/", include_in_schema=False)
        def workspace():
            return FileResponse(static / "index.html")

    return app


def from_env():
    demo = os.getenv("ORDERFLOW_DEMO") == "1"
    raw = os.getenv("ORDERFLOW_API_KEYS", "{}")
    principals = json.loads(raw)
    if not demo and not principals:
        raise RuntimeError("Задайте ORDERFLOW_API_KEYS или явно включите ORDERFLOW_DEMO=1")
    return create_app(principals=principals, demo=demo)
