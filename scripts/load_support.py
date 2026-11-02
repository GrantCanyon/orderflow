"""Изолированная база и временный HTTP-сервер только для испытаний."""

import re
import socket
import time
from contextlib import contextmanager
from threading import Thread
from uuid import uuid4

import uvicorn
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema, DropSchema

from orderflow.db import Database


@contextmanager
def isolated_database(directory, database_url=None, lock_timeout_ms=2000):
    directory.mkdir(parents=True, exist_ok=True)
    if database_url and make_url(database_url).get_backend_name() != "postgresql":
        raise ValueError("Внешний URL испытания должен указывать на тестовый PostgreSQL")
    database = Database(
        database_url or f"sqlite:///{directory / 'trial.db'}", lock_timeout_ms=lock_timeout_ms
    )
    schema = None
    created = False
    try:
        if not database.sqlite:
            schema = f"test_{uuid4().hex}"
            with database.engine.begin() as connection:
                connection.execute(CreateSchema(schema))
            created = True
            database.engine = database.engine.execution_options(schema_translate_map={None: schema})
        database.initialize()
        yield database
    finally:
        try:
            if created:
                # Имя не приходит извне. Перед удалением проверяем и формат, и владельца.
                if not re.fullmatch(r"test_[0-9a-f]{32}", schema):
                    raise RuntimeError("Небезопасное имя испытательной схемы; очистка остановлена")
                with database.engine.begin() as connection:
                    owned = connection.execute(
                        text(
                            "SELECT n.nspowner = r.oid FROM pg_namespace n "
                            "JOIN pg_roles r ON r.rolname = current_user WHERE n.nspname = :schema"
                        ),
                        {"schema": schema},
                    ).scalar_one_or_none()
                    if owned is not True:
                        raise RuntimeError("Испытательная схема отсутствует или сменила владельца")
                    connection.execute(DropSchema(schema, cascade=True))
        finally:
            database.engine.dispose()


@contextmanager
def live_server(app):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="critical",
        access_log=False,
        log_config=None,
        lifespan="on",
    )
    server = uvicorn.Server(config)
    thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive() or time.monotonic() >= deadline:
                raise RuntimeError("Испытательный HTTP-сервер не запустился")
            time.sleep(0.01)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        if thread.is_alive():
            server.force_exit = True
            thread.join(timeout=2)
        listener.close()
        if thread.is_alive():
            raise RuntimeError("Испытательный HTTP-сервер не завершился вовремя")
