from __future__ import annotations

import os
import threading
import time
import webbrowser
from pathlib import Path

from .dashboard import make_server, verify_dashboard
from .engine import OrchestratorEngine
from .models import Settings, discover_github_token


def load_env(path: Path = Path(".env")) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class AllInOneApplication:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.engine = OrchestratorEngine(settings)
        self.stop = threading.Event()
        self.projects_ready = threading.Event()
        self.sync_interval = max(5, int(os.getenv("ORCH_SYNC_INTERVAL", "15")))
        self.open_browser = os.getenv("ORCH_OPEN_BROWSER", "1").strip().lower() not in {"0", "false", "no", "off"}

    def _auth_forever(self) -> None:
        while not self.stop.is_set():
            try:
                if not self.engine.github_auth_status().get("connected"):
                    token = discover_github_token()
                    if token:
                        self.engine.github.set_token(token)
                    self.engine.refresh_github_auth()
            except Exception as exc:
                self.engine.state.add_event("github_auth_error", {"error": str(exc)})
            self.stop.wait(10)

    def _sync_forever(self) -> None:
        while not self.stop.is_set():
            try:
                result = self.engine.sync_projects()
                ok = bool(result) and all(row.get("ok") for row in result)
                if ok:
                    if not self.projects_ready.is_set():
                        self.engine.state.add_event("initial_sync_ready", {"projects": result})
                    self.projects_ready.set()
                else:
                    self.engine.state.add_event("initial_sync_waiting", {"projects": result})
            except Exception as exc:
                self.engine.state.add_event("sync_error", {"error": str(exc)})
            self.stop.wait(self.sync_interval)

    def _worker_after_sync(self) -> None:
        while not self.stop.is_set():
            projects_ok = self.projects_ready.is_set()
            github_ok = bool(self.engine.github_auth_status().get("connected"))
            if projects_ok and github_ok:
                break
            self.stop.wait(1)
        if self.stop.is_set():
            return

        while not self.stop.is_set():
            try:
                self.engine.ensure_labels()
                break
            except Exception as exc:
                self.engine.state.add_event("label_bootstrap_waiting", {"error": str(exc)})
                self.stop.wait(5)
        if self.stop.is_set():
            return

        self.engine.state.add_event(
            "worker_enabled",
            {"reason": "dashboard_verified_projects_synced_and_github_connected"},
        )
        self.engine.serve_loop(self.stop)

    def run(self) -> None:
        host = self.settings.dashboard_host
        port = self.settings.dashboard_port
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("Dashboard is local-only; bind to 127.0.0.1/localhost/::1")

        server = make_server(self.engine, host, port)
        server_thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.5},
            daemon=True,
            name="orchestrator-dashboard",
        )
        server_thread.start()

        try:
            actual_host, actual_port = server.server_address[:2]
            probe_host = host if port != 0 else actual_host
            address = verify_dashboard(self.engine, probe_host, int(actual_port))
            print(f"Orchestrator dashboard verified: {address}")
            print(f"Auto sync interval: {self.sync_interval}s")
            print("GitHub authentication will be auto-detected; otherwise connect it from the dashboard.")
            print("Managed-project Issue worker starts automatically after GitHub auth + first successful project sync.")

            if self.open_browser:
                try:
                    webbrowser.open(address)
                except Exception:
                    pass

            threading.Thread(
                target=self._auth_forever,
                daemon=True,
                name="orchestrator-github-auth",
            ).start()
            threading.Thread(
                target=self._sync_forever,
                daemon=True,
                name="orchestrator-auto-sync",
            ).start()
            threading.Thread(
                target=self._worker_after_sync,
                daemon=True,
                name="orchestrator-worker",
            ).start()

            while server_thread.is_alive():
                server_thread.join(timeout=1)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop.set()
            server.shutdown()
            server.server_close()


def run() -> None:
    load_env()
    settings = Settings.from_env()
    AllInOneApplication(settings).run()
