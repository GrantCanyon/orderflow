import json
import socket
from http.client import HTTPConnection, RemoteDisconnected
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import urlsplit

import httpx
from conftest import request
from test_storage_failures import counts

from orderflow.api import create_app
from scripts.load_support import live_server


def test_lost_http_response_after_commit_replays_exact_booking(db, clock, product):
    app = create_app(
        db=db,
        clock=clock,
        principals={"test-key": {"id": "network-user", "name": "Сетевой тест", "role": "operator"}},
    )
    with live_server(app) as base_url:
        upstream = urlsplit(base_url)
        captured = []

        class DropResponse(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                connection = HTTPConnection(upstream.hostname, upstream.port, timeout=5)
                connection.request("POST", self.path, body, dict(self.headers))
                response = connection.getresponse()
                captured.append((response.status, json.loads(response.read())))
                connection.close()
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()

        proxy = ThreadingHTTPServer(("127.0.0.1", 0), DropResponse)
        thread = Thread(target=proxy.serve_forever, daemon=True)
        thread.start()
        headers = {
            "X-API-Key": "test-key",
            "Idempotency-Key": "lost-response",
            "Content-Type": "application/json",
        }
        payload = request(product["id"])
        try:
            downstream = HTTPConnection("127.0.0.1", proxy.server_port, timeout=5)
            downstream.request("POST", "/api/bookings", json.dumps(payload), headers)
            try:
                downstream.getresponse()
            except RemoteDisconnected:
                pass
            else:
                raise AssertionError("Прокси должен оборвать ответ до HTTP-заголовков")
            finally:
                downstream.close()
            assert captured[0][0] == 201
            original_id = captured[0][1]["id"]
            with httpx.Client(
                base_url=base_url, headers=headers, timeout=5, trust_env=False
            ) as client:
                replay = client.post("/api/bookings", json=payload)
                assert replay.status_code == 201
                assert replay.json()["id"] == original_id
                changed = client.post("/api/bookings", json=request(product["id"], quantity=2))
                assert changed.status_code == 409
                assert changed.json()["error"]["code"] == "idempotency_conflict"
            assert counts(db) == {"bookings": 1, "booking_items": 1, "idempotency": 1, "audit": 2}
        finally:
            proxy.shutdown()
            proxy.server_close()
            thread.join(timeout=5)
