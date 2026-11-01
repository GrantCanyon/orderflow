"""Проверка живого учебного окружения: чтение данных и запрет изменения наблюдателем."""

import json
import os
import time
from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE = os.getenv("ORDERFLOW_SMOKE_URL", "http://127.0.0.1:8000").rstrip("/")


def fetch(path, key=None, payload=None):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["X-API-Key"] = key
    if payload is not None:
        headers["Idempotency-Key"] = "smoke-viewer-must-not-create"
    request = Request(
        BASE + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers=headers,
    )
    with urlopen(request, timeout=5) as response:
        return response.read()


def main():
    for attempt in range(45):
        try:
            assert json.loads(fetch("/health"))["status"] == "ok"
            break
        except (URLError, OSError):
            if attempt == 44:
                raise RuntimeError("Сервис не стал доступен за время ожидания") from None
            time.sleep(1)
    assert "Доступность" in fetch("/").decode("utf-8")
    for path in ("/static/app.js", "/static/style.css", "/static/time.mjs"):
        assert fetch(path), f"Не загружен ресурс {path}"
    catalog = json.loads(fetch("/api/products", "demo-operator"))
    assert len(catalog) == 8, "Ожидалось восемь учебных позиций"
    before = json.loads(fetch("/api/bookings", "demo-viewer"))
    start = datetime.now(UTC) + timedelta(days=1)
    try:
        fetch(
            "/api/bookings",
            "demo-viewer",
            {
                "label": "Проверка запрета записи",
                "start_at": start.isoformat(),
                "end_at": (start + timedelta(hours=1)).isoformat(),
                "items": [{"product_id": catalog[0]["id"], "quantity": 1}],
            },
        )
    except HTTPError as error:
        assert error.code == 403, f"Ожидался запрет 403, получен {error.code}"
    else:
        raise AssertionError("Наблюдатель смог создать бронирование")
    after = json.loads(fetch("/api/bookings", "demo-viewer"))
    assert {row["id"] for row in before} == {row["id"] for row in after}
    print("Проверка пройдена: сервер, интерфейс, ресурсы, каталог; запись наблюдателя запрещена.")


if __name__ == "__main__":
    main()
