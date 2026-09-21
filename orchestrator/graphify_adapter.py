from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable


class GraphifyAdapter:
    def __init__(self, binary: str = "graphify"):
        self.binary = binary

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def config(self, manifest: dict[str, Any]) -> dict[str, Any]:
        raw = dict(manifest.get("graphify") or {})
        return {
            "enabled": bool(raw.get("enabled", False)),
            "required": bool(raw.get("required", False)),
            "auto_update": raw.get("auto_update", "before_task"),
            "query_context": bool(raw.get("query_context", True)),
            "output_dir": raw.get("output_dir", "graphify-out"),
            "max_context_chars": int(raw.get("max_context_chars", 12000)),
            "update_timeout": int(raw.get("update_timeout", 300)),
            "query_timeout": int(raw.get("query_timeout", 60)),
        }

    def status(self, root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
        cfg = self.config(manifest)
        graph = root / cfg["output_dir"] / "graph.json"
        return {
            "enabled": cfg["enabled"],
            "required": cfg["required"],
            "cli_available": self.available(),
            "graph_exists": graph.is_file(),
            "graph_path": graph.as_posix(),
            "graph_mtime": graph.stat().st_mtime if graph.is_file() else None,
        }

    def _run(
        self,
        args: list[str],
        root: Path,
        timeout: int,
        heartbeat: Callable[[], None] | None = None,
    ) -> tuple[int, str, bool]:
        with tempfile.NamedTemporaryFile(mode="w+b", delete=False) as fh:
            out_path = Path(fh.name)
        stream = out_path.open("wb")
        try:
            proc = subprocess.Popen(args, cwd=root, stdout=stream, stderr=subprocess.STDOUT)
            started = time.time()
            timed_out = False
            while proc.poll() is None:
                if heartbeat:
                    heartbeat()
                if time.time() - started > timeout:
                    timed_out = True
                    proc.kill()
                    break
                time.sleep(1)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            stream.close()
            output = out_path.read_text(encoding="utf-8", errors="replace")
            return proc.returncode if proc.returncode is not None else -9, output, timed_out
        finally:
            try:
                stream.close()
            except Exception:
                pass
            out_path.unlink(missing_ok=True)

    def ensure_graph(
        self,
        root: Path,
        manifest: dict[str, Any],
        heartbeat: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        cfg = self.config(manifest)
        status = self.status(root, manifest)
        if not cfg["enabled"]:
            return status
        if not self.available():
            if cfg["required"]:
                raise RuntimeError("Graphify is required by project manifest but graphify CLI is not installed")
            return status
        graph = root / cfg["output_dir"] / "graph.json"
        if cfg["auto_update"] in (False, "off", "never") and graph.is_file():
            return status
        cmd = [self.binary, "update", "."] if graph.is_file() else [self.binary, ".", "--no-viz"]
        started = time.time()
        code, output, timed_out = self._run(cmd, root, cfg["update_timeout"], heartbeat)
        if code:
            message = "Graphify update timed out" if timed_out else f"Graphify update failed: {output[-4000:]}"
            if cfg["required"]:
                raise RuntimeError(message)
            return {**self.status(root, manifest), "warning": message, "duration": time.time() - started}
        return {**self.status(root, manifest), "duration": time.time() - started}

    def query(
        self,
        root: Path,
        manifest: dict[str, Any],
        question: str,
        heartbeat: Callable[[], None] | None = None,
    ) -> str:
        cfg = self.config(manifest)
        if not cfg["enabled"] or not cfg["query_context"] or not self.available():
            return ""
        graph = root / cfg["output_dir"] / "graph.json"
        if not graph.is_file():
            return ""
        code, output, _ = self._run(
            [self.binary, "query", question, "--graph", str(graph)],
            root,
            cfg["query_timeout"],
            heartbeat,
        )
        if code:
            return ""
        return output.strip()[: cfg["max_context_chars"]]
