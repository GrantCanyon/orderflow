from conftest import request
from fastapi.testclient import TestClient

from orderflow.api import create_app


def client(db, clock):
    return TestClient(
        create_app(
            db=db,
            clock=clock,
            principals={
                "admin-secret": {"id": "admin-01", "name": "Администратор", "role": "admin"},
                "operator-secret": {"id": "operator-01", "name": "Оператор", "role": "operator"},
                "viewer-secret": {"id": "viewer-01", "name": "Наблюдатель", "role": "viewer"},
            },
        )
    )


def headers(role):
    return {"X-API-Key": f"{role}-secret"}


def test_authentication_and_least_privilege(db, clock, product):
    with client(db, clock) as api:
        assert api.get("/api/products").status_code == 401
        assert api.get("/api/products", headers={"X-API-Key": "wrong"}).status_code == 401
        assert api.get("/api/products", headers=headers("viewer")).status_code == 200
        assert (
            api.post(
                "/api/bookings",
                headers={**headers("viewer"), "Idempotency-Key": "x"},
                json=request(product["id"]),
            ).status_code
            == 403
        )
        assert (
            api.patch(
                f"/api/products/{product['id']}", headers=headers("operator"), json={"capacity": 10}
            ).status_code
            == 403
        )
        assert api.post("/api/admin/expire", headers=headers("operator")).status_code == 403


def test_http_booking_conflict_replay_and_audit(db, clock, product):
    with client(db, clock) as api:
        auth = {**headers("operator"), "Idempotency-Key": "first"}
        response = api.post("/api/bookings", headers=auth, json=request(product["id"], quantity=3))
        assert response.status_code == 201
        booking = response.json()
        assert (
            api.post("/api/bookings", headers=auth, json=request(product["id"], quantity=3)).json()[
                "id"
            ]
            == booking["id"]
        )
        conflict = api.post(
            "/api/bookings",
            headers={**auth, "Idempotency-Key": "second"},
            json=request(product["id"]),
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "capacity_conflict"
        assert (
            api.post(f"/api/bookings/{booking['id']}/confirm", headers=headers("operator")).json()[
                "status"
            ]
            == "confirmed"
        )
        audit = api.get(
            "/api/audit", headers=headers("viewer"), params={"booking_id": booking["id"]}
        ).json()
        assert audit[0]["actor"] == "operator-01"
        assert audit[0]["actor_name"] == "Оператор"
        assert len(audit) == 2


def test_http_validation_does_not_coerce_boolean_or_accept_extra_fields(db, clock, product):
    with client(db, clock) as api:
        auth = {**headers("operator"), "Idempotency-Key": "invalid"}
        payload = request(product["id"])
        payload["items"][0]["quantity"] = True
        assert api.post("/api/bookings", headers=auth, json=payload).status_code == 422
        payload = request(product["id"])
        payload["status"] = "confirmed"
        assert api.post("/api/bookings", headers=auth, json=payload).status_code == 422
        assert api.get("/api/bookings?limit=10000", headers=headers("viewer")).status_code == 422
        assert (
            api.get("/api/availability?start=no&end=no", headers=headers("viewer")).status_code
            == 422
        )


def test_missing_key_and_unknown_booking_are_structured_errors(db, clock):
    with client(db, clock) as api:
        assert api.post("/api/bookings", headers=headers("operator"), json={}).status_code == 422
        response = api.get("/api/bookings/unknown", headers=headers("viewer"))
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"


def test_runtime_config_never_exposes_non_demo_secrets(db, clock):
    with client(db, clock) as api:
        config = api.get("/api/config").json()
        assert config["demo"] is False
        assert "secret" not in str(config)
        assert api.get("/health").json()["status"] == "ok"
        me = api.get("/api/me", headers=headers("viewer")).json()
        assert me == {"id": "viewer-01", "name": "Наблюдатель", "role": "viewer"}


def test_explicit_demo_identity_is_separate_from_production(db, clock):
    with TestClient(create_app(db=db, clock=clock, demo=True)) as api:
        assert api.get("/api/config").json()["demo"] is True
        assert api.get("/api/products", headers={"X-API-Key": "demo-operator"}).status_code == 200
    with client(db, clock) as api:
        assert api.get("/api/products", headers={"X-API-Key": "demo-operator"}).status_code == 401


def test_same_display_name_different_ids_do_not_share_idempotency(db, clock, product):
    principals = {
        "first": {"id": "user-a", "name": "Оператор", "role": "operator"},
        "second": {"id": "user-b", "name": "Оператор", "role": "operator"},
    }
    with TestClient(create_app(db=db, clock=clock, principals=principals)) as api:
        first = api.post(
            "/api/bookings",
            headers={"X-API-Key": "first", "Idempotency-Key": "key"},
            json=request(product["id"]),
        )
        second = api.post(
            "/api/bookings",
            headers={"X-API-Key": "second", "Idempotency-Key": "key"},
            json=request(product["id"]),
        )
        assert first.status_code == second.status_code == 201
        assert first.json()["id"] != second.json()["id"]
        assert first.json()["created_by"] == "user-a"
        assert second.json()["created_by"] == "user-b"
        audit = api.get("/api/audit", headers={"X-API-Key": "first"}).json()
        assert {row["actor"] for row in audit if row["action"] == "booking.create"} == {
            "user-a",
            "user-b",
        }


def test_rotated_key_and_renamed_display_preserve_identity_and_audit_snapshot(db, clock, product):
    principals = {
        "old": {"id": "stable-user", "name": "Старое имя", "role": "operator"},
        "new": {"id": "stable-user", "name": "Новое имя", "role": "operator"},
    }
    with TestClient(create_app(db=db, clock=clock, principals=principals)) as api:
        first = api.post(
            "/api/bookings",
            headers={"X-API-Key": "old", "Idempotency-Key": "key"},
            json=request(product["id"]),
        ).json()
        replay = api.post(
            "/api/bookings",
            headers={"X-API-Key": "new", "Idempotency-Key": "key"},
            json=request(product["id"]),
        ).json()
        assert replay["id"] == first["id"]
        assert replay["created_by"] == "stable-user"
        assert replay["created_by_name"] == "Старое имя"
        assert (
            api.post(
                f"/api/bookings/{first['id']}/confirm", headers={"X-API-Key": "new"}
            ).status_code
            == 200
        )
        audit = api.get(
            "/api/audit", params={"booking_id": first["id"]}, headers={"X-API-Key": "new"}
        ).json()
        assert [row["actor_name"] for row in audit] == ["Новое имя", "Старое имя"]
        assert {row["actor"] for row in audit} == {"stable-user"}


def test_configuration_requires_explicit_stable_principal_id(db, clock):
    import pytest

    with pytest.raises(ValueError):
        create_app(db=db, clock=clock, principals={"key": {"name": "Оператор", "role": "operator"}})
