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


def test_alternatives_api_is_read_only_for_viewer_and_requires_auth(db, clock, product):
    payload = {
        "start_at": "2030-01-02T10:00:00Z",
        "end_at": "2030-01-02T11:00:00Z",
        "search_until": "2030-01-03T10:00:00Z",
        "items": [{"product_id": product["id"], "quantity": 1}],
        "limit": 3,
    }
    with client(db, clock) as api:
        assert api.post("/api/alternatives", json=payload).status_code == 401
        response = api.post("/api/alternatives", headers=headers("viewer"), json=payload)
        assert response.status_code == 200
        assert response.json()["alternatives"][0]["start_at"] == payload["start_at"]
        assert response.json()["duration_microseconds"] == 3_600_000_000
        assert api.get("/api/bookings", headers=headers("viewer")).json() == []
        assert len(api.get("/api/audit", headers=headers("viewer")).json()) == 1
        payload["exclude_booking"] = "not-supported"
        assert (
            api.post("/api/alternatives", headers=headers("viewer"), json=payload).status_code
            == 422
        )


def test_alternatives_api_does_not_coerce_boolean_limit(db, clock, product):
    payload = {
        "start_at": "2030-01-02T10:00:00Z",
        "end_at": "2030-01-02T11:00:00Z",
        "search_until": "2030-01-03T10:00:00Z",
        "items": [{"product_id": product["id"], "quantity": 1}],
        "limit": True,
    }
    with client(db, clock) as api:
        assert (
            api.post("/api/alternatives", headers=headers("viewer"), json=payload).status_code
            == 422
        )


def test_nul_text_is_rejected_before_database_write(db, clock, product):
    with client(db, clock) as api:
        payload = request(product["id"])
        payload["label"] = "Имя\x00события"
        response = api.post(
            "/api/bookings", headers={**headers("operator"), "Idempotency-Key": "nul"}, json=payload
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_input"
        for field in ["name", "category"]:
            response = api.patch(
                f"/api/products/{product['id']}",
                headers=headers("admin"),
                json={field: "Текст\x00поля"},
            )
            assert response.status_code == 422
        assert api.get("/api/bookings", headers=headers("viewer")).json() == []


def test_principal_name_requires_unicode_scalars_without_nul(db, clock):
    import pytest

    for name in ["Имя\x00", "Имя\ud800", "Имя\udfff"]:
        with pytest.raises(ValueError):
            create_app(
                db=db,
                clock=clock,
                principals={"key": {"id": "operator", "name": name, "role": "operator"}},
            )


def test_normal_cyrillic_and_newline_text_remain_supported(db, clock, product):
    with client(db, clock) as api:
        payload = request(product["id"])
        payload["label"] = "Событие\nВторая строка"
        response = api.post(
            "/api/bookings",
            headers={**headers("operator"), "Idempotency-Key": "unicode"},
            json=payload,
        )
        assert response.status_code == 201
        assert response.json()["label"] == payload["label"]


def test_integer_database_ids_are_bounded_before_sql(db, clock, product, service):
    import pytest

    from orderflow.errors import DomainError

    for invalid in [2**31, 2**64, 0, -1, True]:
        with client(db, clock) as api:
            audit_response = api.get(
                "/api/audit", params={"before_id": invalid}, headers=headers("viewer")
            )
            product_response = api.patch(
                f"/api/products/{invalid}", json={"capacity": 1}, headers=headers("admin")
            )
            payload = request(invalid)
            booking_response = api.post(
                "/api/bookings",
                json=payload,
                headers={**headers("operator"), "Idempotency-Key": f"id-{invalid}"},
            )
            alternative_response = api.post(
                "/api/alternatives",
                headers=headers("viewer"),
                json={
                    "start_at": payload["start_at"],
                    "end_at": payload["end_at"],
                    "items": payload["items"],
                    "search_until": "2030-01-03T10:00:00Z",
                },
            )
            assert [
                response.status_code
                for response in [
                    audit_response,
                    product_response,
                    booking_response,
                    alternative_response,
                ]
            ] == [422] * 4
        with pytest.raises(DomainError) as error:
            service.audit(before_id=invalid)
        assert error.value.status == 422
        with pytest.raises(DomainError) as error:
            service.update_product("admin", invalid, capacity=1)
        assert error.value.status == 422
    assert service.list_bookings() == []
    assert service.list_products()[0]["capacity"] == 3


def test_maximum_integer_id_is_a_valid_query_not_an_overflow(db, clock, service):
    import pytest

    from orderflow.errors import DomainError

    maximum = 2**31 - 1
    with client(db, clock) as api:
        assert (
            api.get(
                "/api/audit", params={"before_id": maximum}, headers=headers("viewer")
            ).status_code
            == 200
        )
        assert (
            api.patch(
                f"/api/products/{maximum}", json={"capacity": 1}, headers=headers("admin")
            ).status_code
            == 404
        )
        assert (
            api.post(
                "/api/bookings",
                json=request(maximum),
                headers={**headers("operator"), "Idempotency-Key": "valid-id-boundary"},
            ).status_code
            == 404
        )
    assert service.audit(before_id=maximum) == []
    with pytest.raises(DomainError) as error:
        service.update_product("admin", maximum, capacity=1)
    assert error.value.status == 404
