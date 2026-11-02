"""Повтор исторического SQL-исследования 25ad49c только в собственной тестовой схеме."""

import argparse
import hashlib
import ipaddress
import json
import os
import platform
import random
import re
import socket
import statistics
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import MetaData, Table, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema, DropSchema

ROOT = Path(__file__).resolve().parents[1]
SOURCE_COMMIT = "25ad49ca74f565a1b8a00c6dcc304bcdfd92e468"
SEED = 20261102
NOW = datetime(2030, 1, 1, 8, tzinfo=UTC)


class ProbeError(ValueError):
    pass


class Parser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, add_help=False, **kwargs)
        self.add_argument("-h", "--help", action="help", help="Показать справку и завершить работу")
        self._positionals.title = "Позиционные параметры"
        self._optionals.title = "Параметры"

    def format_help(self):
        return super().format_help().replace("usage:", "Использование:", 1)

    def error(self, message):
        raise ProbeError("arguments")


def validate_url(raw):
    # Эти параметры libpq могут переопределять адрес, сервис или настройки сессии.
    # Пользовательское окружение не меняется: исследование отказывается до connect.
    if any(
        name in os.environ for name in ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE", "PGOPTIONS")
    ):
        raise ProbeError("database_environment")
    try:
        url = make_url(raw)
        if url.drivername not in ("postgresql", "postgresql+psycopg") or url.query:
            raise ValueError()
        if not url.database or not re.fullmatch(r"[A-Za-z0-9_]+_test", url.database):
            raise ValueError()
        if not url.host:
            raise ValueError()
        if url.host.lower() == "localhost":
            addresses = [
                item[4][0]
                for item in socket.getaddrinfo(url.host, url.port or 5432, type=socket.SOCK_STREAM)
            ]
            if not addresses or not all(
                ipaddress.ip_address(address).is_loopback for address in addresses
            ):
                raise ValueError()
        elif not ipaddress.ip_address(url.host).is_loopback:
            raise ValueError()
        if url.port is not None and not 1 <= url.port <= 65535:
            raise ValueError()
        return url.set(drivername="postgresql+psycopg", port=url.port or 5432)
    except Exception:
        raise ProbeError("database_url") from None


def prepare_output(raw, test_output=False):
    path = Path(raw)
    if not path.is_absolute():
        if test_output:
            raise ProbeError("output")
        path = ROOT / path
    # Запрет обхода через .., символьные ссылки и junction.
    if path.absolute() != path.resolve():
        raise ProbeError("output")
    path = path.resolve()
    if test_output:
        if (
            not path.name.startswith("test_query_plans_")
            or not path.is_dir()
            or any(path.iterdir())
        ):
            raise ProbeError("output")
    else:
        if (
            not path.is_relative_to(ROOT / "artifacts")
            or path == ROOT / "artifacts"
            or path.exists()
        ):
            raise ProbeError("output")
        path.mkdir(parents=True, exist_ok=False)
    return path


def save(folder, name, value):
    # Каждый артефакт создаётся один раз, существующее содержимое не перезаписывается.
    with (folder / name).open("x", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, indent=2, default=str)
        output.write("\n")


def generate_rows(rows=30000, seed=SEED):
    if not 10 <= rows <= 30000:
        raise ProbeError("rows")
    rng = random.Random(seed)
    product_rows = [
        {
            "id": i,
            "sku": f"SYN-{i}",
            "name": f"Синтетическая позиция {i}",
            "category": "Испытание",
            "capacity": 100,
            "daily_rate_cents": 1000,
        }
        for i in range(1, 301)
    ]
    counts = [
        ("returned", rows * 60 // 100),
        ("cancelled", rows * 15 // 100),
        ("expired", rows * 5 // 100),
        ("confirmed", rows * 10 // 100),
        ("hold", rows * 6 // 100),
    ]
    counts.append(("checked_out", rows - sum(count for _, count in counts)))
    statuses = [status for status, count in counts for _ in range(count)]
    rng.shuffle(statuses)
    booking_rows, item_rows = [], []
    for i, status in enumerate(statuses):
        booking_id = str(UUID(int=rng.getrandbits(128)))
        if status in ("returned", "cancelled", "expired"):
            start = NOW - timedelta(days=rng.randint(10, 365), hours=rng.randint(0, 23))
        elif status == "checked_out":
            start = NOW - timedelta(days=rng.randint(0, 5), hours=rng.randint(0, 23))
        else:
            start = NOW + timedelta(days=rng.randint(0, 29), hours=rng.randint(0, 23))
        end = start + timedelta(days=rng.randint(1, 4))
        expires = NOW + timedelta(minutes=15 if i % 2 else -15)
        booking_rows.append(
            {
                "id": booking_id,
                "reference": f"SYN-{i:08d}",
                "label": "Синтетическое испытание",
                "start_at": start,
                "end_at": end,
                "status": status,
                "expires_at": expires,
                "created_at": min(start - timedelta(days=1), NOW - timedelta(days=1)),
                "created_by": "synthetic",
                "created_by_name": "Испытание",
                "total_cents": 1000,
            }
        )
        for product_id in rng.sample(range(1, 301), rng.randint(1, 3)):
            item_rows.append(
                {
                    "booking_id": booking_id,
                    "product_id": product_id,
                    "quantity": rng.randint(1, 3),
                    "rate_cents": 1000,
                }
            )
    digest = hashlib.sha256(
        json.dumps([booking_rows, item_rows], sort_keys=True, default=str).encode()
    ).hexdigest()
    return {
        "products": product_rows,
        "bookings": booking_rows,
        "items": item_rows,
        "rows_sha256": digest,
        "status_counts": dict(counts),
    }


def owned(conn, state, database):
    schema = state["schema"]
    if not re.fullmatch(r"test_[0-9a-f]{32}", schema):
        raise ProbeError("schema")
    if conn.scalar(text("SELECT current_database()")) != database:
        raise ProbeError("database")
    row = (
        conn.execute(
            text(
                "SELECT n.nspowner=r.oid AS owned, "
                "obj_description(n.oid,'pg_namespace') AS marker "
                "FROM pg_namespace n JOIN pg_roles r ON r.rolname=current_user "
                "WHERE n.nspname=:schema"
            ),
            {"schema": schema},
        )
        .mappings()
        .one_or_none()
    )
    if row is None or row["owned"] is not True or row["marker"] != state["marker"]:
        raise ProbeError("schema_owner")
    conn.exec_driver_sql(f'SET search_path TO "{schema}"')
    conn.exec_driver_sql("SET statement_timeout TO '30s'")
    conn.exec_driver_sql("SET lock_timeout TO '2s'")


def canonical(rows):
    values = sorted(json.dumps(list(row), default=str, ensure_ascii=False) for row in rows)
    return values, hashlib.sha256("\n".join(values).encode()).hexdigest()


def measure(conn, folder, statements, stage, mode, repeats, expected):
    conn.exec_driver_sql("DEALLOCATE ALL")
    conn.exec_driver_sql(f"SET plan_cache_mode={mode}")
    result = {}
    for name, statement in statements.items():
        conn.exec_driver_sql(statement["prepare"])
        values, digest = canonical(conn.exec_driver_sql(statement["execute"]).all())
        if name not in expected:
            expected[name] = values
        if values != expected[name]:
            raise ProbeError("result_mismatch")
        prefix = f"{stage}_{mode}_{name}"
        save(folder, prefix + "_result.json", values)
        sql = "EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + statement["execute"]
        conn.exec_driver_sql(sql).scalar_one()
        plans = [conn.exec_driver_sql(sql).scalar_one()[0] for _ in range(repeats)]
        save(folder, prefix + "_plans.json", plans)
        counts = dict(
            conn.execute(
                text(
                    "SELECT generic_plans,custom_plans FROM pg_prepared_statements WHERE name=:name"
                ),
                {"name": "qp_" + name},
            )
            .mappings()
            .one()
        )
        selected = "generic_plans" if mode == "force_generic_plan" else "custom_plans"
        other = "custom_plans" if mode == "force_generic_plan" else "generic_plans"
        if counts[selected] != repeats + 2 or counts[other] != 0:
            raise ProbeError("plan_mode")
        result[name] = {
            "rows": len(values),
            "result_sha256": digest,
            "result_equal": True,
            "execution_ms": [p["Execution Time"] for p in plans],
            "median_execution_ms": statistics.median(p["Execution Time"] for p in plans),
            "shared_hit_blocks": [p["Plan"].get("Shared Hit Blocks", 0) for p in plans],
            "shared_read_blocks": [p["Plan"].get("Shared Read Blocks", 0) for p in plans],
            "prepared_plan_counts": counts,
        }
    return result


INDEXES = {
    "qp_items_product_cover": "booking_items (product_id,booking_id) INCLUDE (quantity)",
    "qp_bookings_checked_out": "bookings (id) WHERE status='checked_out'",
    "qp_bookings_status_id": "bookings (status,id)",
    "qp_items_product_portable": "booking_items (product_id,booking_id,quantity)",
}
SETS = {
    "baseline": [],
    "items": ["qp_items_product_cover"],
    "partial": ["qp_items_product_cover", "qp_bookings_checked_out"],
    "status": ["qp_items_product_cover", "qp_bookings_status_id"],
    "include": ["qp_items_product_cover"],
    "portable": ["qp_items_product_portable"],
}


def run(args, url, folder):
    schema = "test_" + uuid4().hex
    state = {"schema": schema, "marker": "query_plans_" + uuid4().hex}
    data = generate_rows(args.rows, args.seed)
    statements = json.loads((ROOT / "resources/queries.json").read_text(encoding="utf-8"))
    ddl = (ROOT / "resources/schema.sql").read_text(encoding="utf-8")
    save(folder, "state.json", state)
    save(
        folder,
        "dataset.json",
        {
            "seed": args.seed,
            "products": len(data["products"]),
            "bookings": len(data["bookings"]),
            "booking_items": len(data["items"]),
            "rows_sha256": data["rows_sha256"],
            "status_counts": data["status_counts"],
            "now": NOW,
        },
    )
    save(folder, "queries.json", statements)
    db = create_engine(
        url,
        hide_parameters=True,
        connect_args={"connect_timeout": 5, "prepare_threshold": None},
    )
    created = False
    completed = False
    results = {}
    expected = {}
    try:
        with db.begin() as conn:
            if conn.scalar(text("SELECT current_database()")) != url.database:
                raise ProbeError("database")
            conn.execute(CreateSchema(schema))
            conn.exec_driver_sql(f"COMMENT ON SCHEMA \"{schema}\" IS '{state['marker']}'")
        created = True
        with db.begin() as conn:
            owned(conn, state, url.database)
            for command in ddl.split(";"):
                if command.strip():
                    conn.exec_driver_sql(command)
            for key, table_name in (
                ("products", "products"),
                ("bookings", "bookings"),
                ("items", "booking_items"),
            ):
                table = Table(table_name, MetaData(), schema=schema, autoload_with=conn)
                for offset in range(0, len(data[key]), 1000):
                    conn.execute(table.insert(), data[key][offset : offset + 1000])
            version = conn.scalar(text("SELECT version()"))
        with db.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            owned(conn, state, url.database)
            for table in ("products", "bookings", "booking_items"):
                conn.exec_driver_sql(f'VACUUM (ANALYZE) "{schema}"."{table}"')
        stages = []
        if args.comparison in ("prepared", "all"):
            stages += ["baseline", "items", "partial", "status"]
        if args.comparison in ("portable", "all"):
            stages += ["include", "portable"]
        active = []
        for stage in stages:
            with db.begin() as conn:
                owned(conn, state, url.database)
                for name in active:
                    conn.exec_driver_sql(f'DROP INDEX "{schema}"."{name}"')
                for name in SETS[stage]:
                    conn.exec_driver_sql(f'CREATE INDEX "{name}" ON "{schema}".{INDEXES[name]}')
                active = SETS[stage]
            results[stage] = {}
            for mode in ("force_custom_plan", "force_generic_plan"):
                with db.begin() as conn:
                    owned(conn, state, url.database)
                    results[stage][mode] = measure(
                        conn, folder, statements, stage, mode, args.repeats, expected
                    )
            with db.begin() as conn:
                owned(conn, state, url.database)
                sizes = [
                    dict(row)
                    for row in conn.execute(
                        text(
                            "SELECT indexname,pg_relation_size((quote_ident(schemaname)||'.'||"
                            "quote_ident(indexname))::regclass) AS bytes "
                            "FROM pg_indexes WHERE schemaname=:schema ORDER BY indexname"
                        ),
                        {"schema": schema},
                    ).mappings()
                ]
            save(
                folder,
                stage + "_summary.json",
                {"queries": results[stage], "index_sizes": sizes},
            )
        save(
            folder,
            "results.json",
            {
                "schema_version": 1,
                "source_commit": SOURCE_COMMIT,
                "historical_sql": True,
                "python_version": platform.python_version(),
                "postgres_version": version,
                "comparison": args.comparison,
                "repeats": args.repeats,
                "warmup_explain": 1,
                "dataset_sha256": data["rows_sha256"],
                "all_results_equal": True,
                "automatic_driver_prepare": False,
                "background_load_controlled": False,
                "method": (
                    "Прогретый кэш после VACUUM ANALYZE; серверное Execution Time EXPLAIN, "
                    "не задержка API; стоимость записи не измеряется"
                ),
                "results": results,
            },
        )
        completed = True
    finally:
        try:
            if created:
                with db.begin() as conn:
                    owned(conn, state, url.database)
                    conn.execute(DropSchema(schema, cascade=True))
                with db.connect() as conn:
                    if (
                        conn.scalar(
                            text("SELECT count(*) FROM pg_namespace WHERE nspname=:schema"),
                            {"schema": schema},
                        )
                        != 0
                    ):
                        raise ProbeError("cleanup")
                save(
                    folder,
                    "cleanup.json",
                    {
                        "owner_and_marker_verified": True,
                        "removed": True,
                        "cleanup_scope": "owned_schema",
                        "experiment_completed": completed,
                    },
                )
        finally:
            db.dispose()


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = Parser(
        description=(
            "Повторить историческое исследование SQL 25ad49c на синтетике; "
            "только локальная тестовая PostgreSQL."
        )
    )
    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument("--output", help="Новый каталог внутри artifacts проекта")
    destination.add_argument(
        "--test-output",
        help="Абсолютный существующий пустой каталог test_query_plans_* для проверок",
    )
    parser.add_argument(
        "--comparison",
        choices=["prepared", "portable", "all"],
        default="all",
        help="Наборы индексов: prepared, portable или оба",
    )
    parser.add_argument("--rows", type=int, default=30000, help="Броней: 10..30000")
    parser.add_argument("--repeats", type=int, default=3, help="Измерений после прогрева: 1..5")
    parser.add_argument("--seed", type=int, default=SEED, help="Начальное состояние генератора")
    args = parser.parse_args()
    if not 10 <= args.rows <= 30000 or not 1 <= args.repeats <= 5:
        raise ProbeError("limits")
    url = validate_url(os.environ.get("ORDERFLOW_QUERY_DATABASE_URL", ""))
    folder = prepare_output(args.test_output or args.output, bool(args.test_output))
    run(args, url, folder)
    print("Проверки завершены: результаты равны, собственная схема удалена.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("Исследование остановлено.", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        reason = str(exc) if isinstance(exc, ProbeError) else "execution"
        print("Исследование не завершено: " + reason, file=sys.stderr)
        sys.exit(2)
