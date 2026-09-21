from __future__ import annotations

import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .engine import OrchestratorEngine


class DashboardHandler(BaseHTTPRequestHandler):
    engine: OrchestratorEngine
    static_dir: Path

    def log_message(self, fmt: str, *args) -> None:
        return

    def _json(self, payload, status: int = 200) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _html(self, path: Path) -> None:
        raw = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        route = urlparse(self.path).path
        if route == "/":
            self._html(self.static_dir / "index.html")
            return
        if route == "/api/health":
            self._json({"ok": True})
            return
        if route == "/api/status":
            self._json(self.engine.snapshot())
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        route = urlparse(self.path).path
        if self.headers.get("X-Orchestrator-UI") != "1":
            self._json({"ok": False, "error": "missing local dashboard control header"}, 403)
            return
        try:
            if route == "/api/control/pause":
                self.engine.pause()
                self._json({"ok": True})
                return
            if route in ("/api/control/resume", "/api/control/start"):
                self.engine.resume()
                self._json({"ok": True})
                return
            if route == "/api/control/once":
                threading.Thread(target=self.engine.tick, daemon=True).start()
                self._json({"ok": True, "scheduled": "tick"})
                return
            if route == "/api/control/sync":
                result = self.engine.sync_projects()
                self._json({"ok": True, "projects": result})
                return
            if route == "/api/control/retry":
                self.engine.request_retry()
                self._json({"ok": True, "message": "retry command posted to active GitHub Issue"})
                return
            prefix = "/api/projects/"
            suffix = "/graphify/update"
            if route.startswith(prefix) and route.endswith(suffix):
                project_id = route[len(prefix) : -len(suffix)].strip("/")
                result = self.engine.update_graphify(project_id)
                self._json({"ok": True, "graphify": result})
                return
            self._json({"error": "not found"}, 404)
        except Exception as exc:
            self._json({"ok": False, "error": str(exc)}, 409)


def make_server(engine: OrchestratorEngine, host: str, port: int) -> ThreadingHTTPServer:
    static_dir = Path(__file__).with_name("static")

    class Handler(DashboardHandler):
        pass

    Handler.engine = engine
    Handler.static_dir = static_dir
    return ThreadingHTTPServer((host, port), Handler)


def serve_dashboard(engine: OrchestratorEngine, host: str, port: int, *, with_engine_loop: bool) -> None:
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("Dashboard is local-only; bind to 127.0.0.1/localhost/::1")
    stop = threading.Event()
    if with_engine_loop:
        threading.Thread(target=engine.serve_loop, args=(stop,), daemon=True).start()
    server = make_server(engine, host, port)
    try:
        print(f"Orchestrator dashboard: http://{host}:{port}")
        server.serve_forever(poll_interval=0.5)
    finally:
        stop.set()
        server.server_close()
