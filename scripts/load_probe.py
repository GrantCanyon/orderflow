"""Повторяемый локальный HTTP-стенд; измерения не являются производственным бенчмарком."""

import hashlib
import importlib.metadata
import json
import math
import os
import platform
import random
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from time import perf_counter
from uuid import uuid4

import httpx
from sqlalchemy import select

from orderflow.api import create_app
from orderflow.cli import RussianParser
from orderflow.db import audit_events, bookings, idempotency, items
from orderflow.service import RentalService

if __package__:
    from .load_support import isolated_database, live_server
else:
    from load_support import isolated_database, live_server

PROJECT = Path(__file__).resolve().parents[1]


def percentiles(values):
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "p50_ms": ordered[math.ceil(0.50 * len(ordered)) - 1] if ordered else None,
        "p95_ms": ordered[math.ceil(0.95 * len(ordered)) - 1] if ordered else None,
    }


def reconcile(database, products, rows):
    with database.transaction() as connection:
        stored = connection.execute(select(bookings)).mappings().all()
        lines = connection.execute(select(items)).mappings().all()
        keys = connection.execute(select(idempotency)).mappings().all()
        audit = (
            connection.execute(
                select(audit_events).where(audit_events.c.action == "booking.create")
            )
            .mappings()
            .all()
        )
    expected = {
        f"probe-{row['request_index']}": row["booking_id"]
        for row in rows
        if row["phase"] == "original" and row["outcome"] == "created"
    }
    expected_pools = {
        row["booking_id"]: row["product_id"]
        for row in rows
        if row["phase"] == "original" and row["outcome"] == "created"
    }
    issues = []
    if any(row["principal"] != "load-operator" for row in keys):
        issues.append("Ключ повтора принадлежит другому пользователю")
    if any(row["actor"] != "load-operator" for row in audit):
        issues.append("Автор аудита не совпал с пользователем испытания")
    actual = {row["key"]: row["booking_id"] for row in keys}
    if actual != expected or len(keys) != len(expected):
        issues.append("Набор зафиксированных ключей не совпал с успешными исходными запросами")
    if len(stored) != len(expected) or {row["id"] for row in stored} != set(expected.values()):
        issues.append("Число или идентификаторы броней не совпали с уникальными фиксациями")
    lines_by_booking, audits_by_booking = defaultdict(list), Counter()
    for row in lines:
        lines_by_booking[row["booking_id"]].append(row)
    for row in audit:
        audits_by_booking[row["booking_id"]] += 1
    for booking in stored:
        selected = lines_by_booking[booking["id"]]
        if len(selected) != 1 or selected[0]["quantity"] != 1 or selected[0]["rate_cents"] != 1000:
            issues.append("Нарушена атомарность состава брони")
        if len(selected) == 1 and selected[0]["product_id"] != expected_pools.get(booking["id"]):
            issues.append("Бронь записана для другого пула оборудования")
        if booking["total_cents"] != 1000 or booking["created_by"] != "load-operator":
            issues.append("Нарушена стоимость или авторство брони")
        if audits_by_booking[booking["id"]] != 1:
            issues.append("На бронь приходится не одно событие создания")
    if len(lines) != len(stored) or len(audit) != len(stored):
        issues.append("Обнаружены неполные или лишние связанные записи")
    reserved = Counter()
    for row in lines:
        reserved[row["product_id"]] += row["quantity"]
    capacities = [
        {
            "product_id": product["id"],
            "capacity": product["capacity"],
            "reserved": reserved[product["id"]],
        }
        for product in products
    ]
    if any(row["reserved"] > row["capacity"] for row in capacities):
        issues.append("Превышен парк оборудования на общем интервале")
    if any(row["phase"] == "replay" and row["outcome"] != "replayed" for row in rows):
        issues.append("Хотя бы один повтор не вернул исходную бронь")
    return {
        "booking_count": len(stored),
        "idempotency_count": len(keys),
        "booking_create_audit_count": len(audit),
        "item_count": len(lines),
        "capacities": capacities,
        "issues": sorted(set(issues)),
    }


def run_trial(
    directory,
    *,
    database_url=None,
    workers=8,
    requests=200,
    mode="shared",
    seed=1,
    lock_timeout_ms=2000,
):
    if (
        mode not in {"shared", "independent"}
        or not 1 <= workers <= 32
        or not workers <= requests <= 5000
    ):
        raise ValueError("Недопустимые параметры испытания")
    rng = random.Random(seed)
    budgets = [max(1, len(range(worker, requests, workers)) // 2) for worker in range(workers)]
    capacities = [sum(budgets)] if mode == "shared" else budgets
    with isolated_database(directory, database_url, lock_timeout_ms) as database:
        service = RentalService(database)
        products = [
            service.create_product(
                "load-admin",
                sku=f"LOAD-{index}",
                name=f"Испытательный пул {index + 1}",
                category="Испытание",
                capacity=capacity,
                daily_rate_cents=1000,
            )
            for index, capacity in enumerate(capacities)
        ]
        start = datetime.now(UTC) + timedelta(hours=1)
        end = start + timedelta(hours=1)
        app = create_app(
            db=database,
            principals={
                "synthetic-load-key": {
                    "id": "load-operator",
                    "name": "Испытатель",
                    "role": "operator",
                }
            },
        )
        rows = []
        with live_server(app) as base_url:

            def payload(index):
                pool = 0 if mode == "shared" else index % workers
                return {
                    "label": f"Испытание {index}",
                    "start_at": start.isoformat(),
                    "end_at": end.isoformat(),
                    "ttl_minutes": 60,
                    "items": [{"product_id": products[pool]["id"], "quantity": 1}],
                }

            def wave(jobs, phase):
                count = min(workers, len(jobs))
                barrier = Barrier(count)
                batches = [jobs[worker::count] for worker in range(count)]

                def client_worker(worker, batch):
                    batch_rows = []
                    with httpx.Client(
                        base_url=base_url,
                        headers={"X-API-Key": "synthetic-load-key"},
                        timeout=15,
                        trust_env=False,
                    ) as client:
                        client.get("/health").raise_for_status()
                        barrier.wait(timeout=20)
                        for index, original_id in batch:
                            began = perf_counter()
                            code, booking_id, outcome = None, None, "transport_error"
                            try:
                                response = client.post(
                                    "/api/bookings",
                                    json=payload(index),
                                    headers={"Idempotency-Key": f"probe-{index}"},
                                )
                                code = response.status_code
                                body = response.json()
                                if code == 201:
                                    booking_id = body["id"]
                                    outcome = (
                                        "created"
                                        if phase == "original"
                                        else (
                                            "replayed"
                                            if booking_id == original_id
                                            else "unexpected"
                                        )
                                    )
                                else:
                                    outcome = body.get("error", {}).get("code", "unexpected")
                            except (httpx.HTTPError, ValueError, KeyError):
                                pass
                            batch_rows.append(
                                {
                                    "request_index": index,
                                    "product_id": payload(index)["items"][0]["product_id"],
                                    "worker": worker,
                                    "phase": phase,
                                    "status_code": code,
                                    "outcome": outcome,
                                    "latency_ms": (perf_counter() - began) * 1000,
                                    "booking_id": booking_id,
                                }
                            )
                    return batch_rows

                with ThreadPoolExecutor(max_workers=count) as executor:
                    futures = [
                        executor.submit(client_worker, worker, batch)
                        for worker, batch in enumerate(batches)
                    ]
                    return [row for future in futures for row in future.result(timeout=300)]

            jobs = [(index, None) for index in range(requests)]
            # Разные seed меняют порядок, не меняя связь запроса с его пулом.
            rng.shuffle(jobs)
            if mode == "independent":
                # Каждому клиенту принадлежит свой пул, без общей блокировки с соседями.
                ordered = [
                    [job for job in jobs if job[0] % workers == worker] for worker in range(workers)
                ]
                jobs = [
                    job
                    for column in range(max(map(len, ordered)))
                    for batch in ordered
                    if column < len(batch)
                    for job in [batch[column]]
                ]
            rows.extend(wave(jobs, "original"))
            successful = [
                (row["request_index"], row["booking_id"])
                for row in rows
                if row["outcome"] == "created"
            ]
            if successful:
                replay_jobs = rng.sample(successful, min(len(successful), max(1, requests // 5)))
                rows.extend(wave(replay_jobs, "replay"))
        checked = reconcile(database, products, rows)
        outcomes = dict(Counter(row["outcome"] for row in rows))
        latencies = {
            outcome: percentiles([row["latency_ms"] for row in rows if row["outcome"] == outcome])
            for outcome in sorted(outcomes)
        }
        unexpected = set(outcomes) - {
            "created",
            "replayed",
            "capacity_conflict",
            "storage_busy",
            "transaction_retry",
            "storage_unavailable",
        }
        report = {
            "mode": mode,
            "seed": seed,
            "workers": workers,
            "original_requests": requests,
            "lock_timeout_ms": lock_timeout_ms,
            "backend": database.engine.dialect.name,
            "database_version": ".".join(
                map(str, database.engine.dialect.server_version_info or ())
            ),
            "outcomes": outcomes,
            "unique_commits": checked["booking_count"],
            "latency_ms_by_outcome": latencies,
            "percentile_method": "nearest-rank",
            "invariants": checked,
            "passed": not checked["issues"] and not unexpected,
        }
        return report, rows


def environment():
    digest = hashlib.sha256()
    for path in sorted(
        [*PROJECT.joinpath("src").rglob("*.py"), *PROJECT.joinpath("scripts").glob("*.py")]
    ):
        digest.update(str(path.relative_to(PROJECT)).replace("\\", "/").encode())
        digest.update(path.read_bytes())
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "processor": platform.processor(),
        "logical_cpu_count": os.cpu_count(),
        "versions": {
            name: importlib.metadata.version(name)
            for name in ("fastapi", "sqlalchemy", "psycopg", "uvicorn", "httpx")
        },
        "source_sha256": digest.hexdigest(),
        "transport": "HTTP loopback / Uvicorn / keep-alive",
        "latency_units": "milliseconds",
        "warmup": "GET /health на клиента перед каждой фазой",
        "limits": "Не производственная нагрузка; ожидание соединения "
        "и нескольких блокировок может суммироваться.",
    }


def main():
    parser = RussianParser(description="Повторяемое HTTP-испытание OrderFlow")
    parser.add_argument("--workers", type=int, default=8, help="Параллельные клиенты: 1–32")
    parser.add_argument(
        "--requests", type=int, default=200, help="Исходные запросы: от workers до 5000"
    )
    parser.add_argument("--repeats", type=int, default=3, help="Повторы каждого сценария: 1–10")
    parser.add_argument("--seed", type=int, default=42, help="Начальное число генератора порядка")
    parser.add_argument(
        "--mode",
        choices=["shared", "independent", "both"],
        default="both",
        help="Общий пул, независимые пулы или оба сценария",
    )
    parser.add_argument("--output", help="Новая папка результатов внутри проекта")
    parser.add_argument(
        "--lock-timeout-ms",
        type=int,
        default=2000,
        help="Ожидание отдельной блокировки: 50–30000 мс",
    )
    args = parser.parse_args()
    if (
        not 1 <= args.workers <= 32
        or not args.workers <= args.requests <= 5000
        or not 1 <= args.repeats <= 10
    ):
        parser.error("workers: 1–32; requests: workers–5000; repeats: 1–10")
    if not 50 <= args.lock_timeout_ms <= 30000:
        parser.error("Ожидание блокировки должно быть от 50 до 30000 мс")
    name = args.output or f"artifacts/load-{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid4().hex[:6]}"
    output = (PROJECT / name).resolve()
    if not output.is_relative_to(PROJECT) or output == PROJECT or output.exists():
        parser.error("Укажите новую папку результатов внутри проекта")
    output.mkdir(parents=True)
    summary = {"environment": environment(), "runs": []}
    modes = ["shared", "independent"] if args.mode == "both" else [args.mode]
    try:
        with (output / "timings.jsonl").open("w", encoding="utf-8") as raw:
            for mode in modes:
                for repeat in range(args.repeats):
                    report, rows = run_trial(
                        output / f"{mode}-{repeat + 1}",
                        database_url=os.getenv("ORDERFLOW_LOAD_DATABASE_URL"),
                        workers=args.workers,
                        requests=args.requests,
                        mode=mode,
                        seed=args.seed,
                        lock_timeout_ms=args.lock_timeout_ms,
                    )
                    report["repeat"] = repeat + 1
                    summary["runs"].append(report)
                    for row in rows:
                        raw.write(
                            json.dumps(
                                {"mode": mode, "repeat": repeat + 1, **row}, ensure_ascii=False
                            )
                            + "\n"
                        )
                    raw.flush()
                    print(
                        f"{mode}, повтор {repeat + 1}: фиксаций {report['unique_commits']}, "
                        f"инварианты {'соблюдены' if report['passed'] else 'НАРУШЕНЫ'}"
                    )
    except Exception as error:
        # URL, ключи, SQL и сообщения DBAPI не включаем даже в собственную диагностику.
        summary["failure_type"] = type(error).__name__
        print("Испытание прервано; безопасный тип ошибки сохранён в сводке.")
    summary["passed"] = "failure_type" not in summary and all(
        row["passed"] for row in summary["runs"]
    )
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Результаты: {output.relative_to(PROJECT)}")
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
