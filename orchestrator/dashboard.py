from __future__ import annotations

import json
import re
import threading
import urllib.request
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

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _static(self, path: Path, content_type: str) -> None:
        raw = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        route = urlparse(self.path).path
        if route == "/":
            self._static(self.static_dir / "index.html", "text/html; charset=utf-8")
            return
        if route == "/web/style.css":
            self._static(self.static_dir / "style.css", "text/css; charset=utf-8")
            return
        if route == "/web/app.js":
            self._static(self.static_dir / "app.js", "application/javascript; charset=utf-8")
            return
        if route == "/api/health":
            self._json({"ok": True})
            return
        if route == "/api/status":
            self._json(self.engine.snapshot())
            return
        if route == "/api/config/agy-models":
            self._json({
                "current": self.engine.get_agy_model(),
                "available": self.engine.get_available_agy_models(),
            })
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        route = urlparse(self.path).path
        if self.headers.get("X-Orchestrator-UI") != "1":
            self._json({"ok": False, "error": "missing local dashboard control header"}, 403)
            return
        try:
            if route == "/api/projects/add":
                payload = self._read_json()
                raw_sources = payload.get("repositories")
                if not raw_sources:
                    single = str(payload.get("repository") or payload.get("source") or "")
                    if "\n" in single or "," in single:
                        raw_sources = [s.strip() for s in re.split(r"[\r\n,]+", single) if s.strip()]
                    elif single.strip():
                        raw_sources = [single.strip()]
                    else:
                        raw_sources = []
                elif isinstance(raw_sources, str):
                    raw_sources = [s.strip() for s in re.split(r"[\r\n,]+", raw_sources) if s.strip()]

                if not raw_sources:
                    raise ValueError("No repository specified for import")

                results = []
                for src in raw_sources:
                    try:
                        entry = self.engine.add_managed_project(src)
                        results.append({"ok": True, "source": src, "project": entry})
                    except Exception as err:
                        results.append({"ok": False, "source": src, "error": str(err)})

                first_successful = next((r["project"] for r in results if r.get("ok")), None)
                all_failed = all(not r.get("ok") for r in results)
                if all_failed and len(results) == 1:
                    raise RuntimeError(results[0].get("error") or "Failed to add project")

                self._json({
                    "ok": not all_failed,
                    "results": results,
                    "projects": [r["project"] for r in results if r.get("ok")],
                    "project": first_successful or (results[0].get("project") if results else None),
                    "total": len(results),
                    "succeeded": sum(1 for r in results if r.get("ok")),
                    "failed": sum(1 for r in results if not r.get("ok")),
                })
                return
            if route == "/api/projects/remove":
                payload = self._read_json()
                project_id = str(payload.get("project_id") or payload.get("id") or "").strip()
                if not project_id:
                    raise ValueError("Missing project_id")
                removed = self.engine.remove_managed_project(project_id)
                self._json({"ok": True, "removed": removed})
                return
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
            if route == "/api/config/agy-model":
                payload = self._read_json()
                model = str(payload.get("model") or "").strip()
                if not model:
                    raise ValueError("Model identifier cannot be empty")
                current = self.engine.set_agy_model(model)
                self._json({"ok": True, "model": current})
                return
            prefix = "/api/projects/"
            suffix_graphify = "/graphify/update"
            suffix_sync = "/sync"
            if route.startswith(prefix) and route.endswith(suffix_graphify):
                project_id = route[len(prefix) : -len(suffix_graphify)].strip("/")
                result = self.engine.update_graphify(project_id)
                self._json({"ok": True, "graphify": result})
                return
            if route.startswith(prefix) and route.endswith(suffix_sync):
                project_id = route[len(prefix) : -len(suffix_sync)].strip("/")
                result = self.engine.sync_single_project(project_id)
                self._json({"ok": True, "project": result})
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


def verify_dashboard(engine: OrchestratorEngine, host: str, port: int) -> str:
    url_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    address = f"http://{url_host}:{port}"
    for route in ("/api/health", "/api/status"):
        with urllib.request.urlopen(address + route, timeout=3) as response:
            if response.status != 200:
                raise RuntimeError(f"Dashboard smoke check failed: {route} -> HTTP {response.status}")
            payload = json.loads(response.read())
            if route == "/api/health" and payload.get("ok") is not True:
                raise RuntimeError("Dashboard health endpoint did not return ok=true")
    engine.state.mark_dashboard_verified(address)
    engine.state.add_event("dashboard_verified", {"address": address})
    return address


def serve_dashboard(engine: OrchestratorEngine, host: str, port: int, *, with_engine_loop: bool) -> None:
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("Dashboard is local-only; bind to 127.0.0.1/localhost/::1")
    stop = threading.Event()
    server = make_server(engine, host, port)
    server_thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True)
    server_thread.start()
    try:
        actual_host, actual_port = server.server_address[:2]
        probe_host = host if port != 0 else actual_host
        address = verify_dashboard(engine, probe_host, int(actual_port))
        print(f"Orchestrator dashboard verified: {address}")
        if with_engine_loop:
            # Only after dashboard bind + /api/health + /api/status succeed may
            # the worker create labels, poll Issues or claim tasks.
            engine.ensure_labels()
            threading.Thread(target=engine.serve_loop, args=(stop,), daemon=True).start()
        while server_thread.is_alive():
            server_thread.join(timeout=1)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
