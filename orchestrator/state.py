from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


class StateStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, timeout=30, check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS runtime (singleton INTEGER PRIMARY KEY CHECK(singleton=1), paused INTEGER NOT NULL DEFAULT 0)"
        )
        self.db.execute("INSERT OR IGNORE INTO runtime(singleton, paused) VALUES(1,0)")
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS lease (
              singleton INTEGER PRIMARY KEY CHECK(singleton=1),
              instance_id TEXT NOT NULL,
              issue_number INTEGER NOT NULL,
              status TEXT NOT NULL,
              payload TEXT NOT NULL,
              heartbeat REAL NOT NULL,
              created_at REAL NOT NULL
            )
            """
        )
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS consumed_commands (
              comment_id INTEGER PRIMARY KEY,
              issue_number INTEGER NOT NULL,
              command TEXT NOT NULL,
              consumed_at REAL NOT NULL
            )
            """
        )
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              created_at REAL NOT NULL,
              event_type TEXT NOT NULL,
              payload TEXT NOT NULL
            )
            """
        )

    def _row_to_lease(self, row) -> dict[str, Any] | None:
        if not row:
            return None
        return {
            "instance_id": row[0],
            "issue_number": row[1],
            "status": row[2],
            "payload": json.loads(row[3]),
            "heartbeat": row[4],
            "created_at": row[5],
        }

    def get_lease(self) -> dict[str, Any] | None:
        with self._lock:
            row = self.db.execute(
                "SELECT instance_id, issue_number, status, payload, heartbeat, created_at FROM lease WHERE singleton=1"
            ).fetchone()
            return self._row_to_lease(row)

    def claim(self, instance_id: str, issue_number: int, status: str, payload: dict[str, Any]) -> bool:
        now = time.time()
        encoded = json.dumps(payload, ensure_ascii=False)
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                row = self.db.execute("SELECT issue_number FROM lease WHERE singleton=1").fetchone()
                if row:
                    self.db.execute("ROLLBACK")
                    return False
                self.db.execute(
                    "INSERT INTO lease(singleton,instance_id,issue_number,status,payload,heartbeat,created_at) VALUES(1,?,?,?,?,?,?)",
                    (instance_id, issue_number, status, encoded, now, now),
                )
                self.db.execute("COMMIT")
                return True
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def takeover_if_stale(self, instance_id: str, timeout_seconds: int) -> dict[str, Any] | None:
        now = time.time()
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                row = self.db.execute(
                    "SELECT instance_id, issue_number, status, payload, heartbeat, created_at FROM lease WHERE singleton=1"
                ).fetchone()
                lease = self._row_to_lease(row)
                if not lease or lease["instance_id"] == instance_id:
                    self.db.execute("COMMIT")
                    return lease
                if now - float(lease["heartbeat"]) <= timeout_seconds:
                    self.db.execute("COMMIT")
                    return None
                payload = dict(lease["payload"])
                payload["recovered_from_instance"] = lease["instance_id"]
                self.db.execute(
                    "UPDATE lease SET instance_id=?, status=?, payload=?, heartbeat=? WHERE singleton=1",
                    (instance_id, "RECOVERING", json.dumps(payload, ensure_ascii=False), now),
                )
                self.db.execute("COMMIT")
                lease.update({"instance_id": instance_id, "status": "RECOVERING", "payload": payload, "heartbeat": now})
                return lease
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def heartbeat(self, instance_id: str) -> None:
        with self._lock:
            self.db.execute(
                "UPDATE lease SET heartbeat=? WHERE singleton=1 AND instance_id=?",
                (time.time(), instance_id),
            )

    def update_lease(self, instance_id: str, *, status: str | None = None, payload: dict[str, Any] | None = None) -> None:
        with self._lock:
            current = self.get_lease()
            if not current or current["instance_id"] != instance_id:
                raise RuntimeError("Lease is not owned by this orchestrator instance")
            next_status = status or current["status"]
            next_payload = payload if payload is not None else current["payload"]
            self.db.execute(
                "UPDATE lease SET status=?, payload=?, heartbeat=? WHERE singleton=1 AND instance_id=?",
                (next_status, json.dumps(next_payload, ensure_ascii=False), time.time(), instance_id),
            )

    def release(self, instance_id: str | None = None) -> None:
        with self._lock:
            if instance_id:
                self.db.execute("DELETE FROM lease WHERE singleton=1 AND instance_id=?", (instance_id,))
            else:
                self.db.execute("DELETE FROM lease WHERE singleton=1")

    def consume_command(self, comment_id: int, issue_number: int, command: str) -> bool:
        with self._lock:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO consumed_commands(comment_id,issue_number,command,consumed_at) VALUES(?,?,?,?)",
                (comment_id, issue_number, command, time.time()),
            )
            return cur.rowcount == 1

    def is_command_consumed(self, comment_id: int) -> bool:
        row = self.db.execute("SELECT 1 FROM consumed_commands WHERE comment_id=?", (comment_id,)).fetchone()
        return bool(row)

    def set_paused(self, value: bool) -> None:
        with self._lock:
            self.db.execute("UPDATE runtime SET paused=? WHERE singleton=1", (1 if value else 0,))

    def is_paused(self) -> bool:
        row = self.db.execute("SELECT paused FROM runtime WHERE singleton=1").fetchone()
        return bool(row and row[0])

    def add_event(self, event_type: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self.db.execute(
                "INSERT INTO events(created_at,event_type,payload) VALUES(?,?,?)",
                (time.time(), event_type, json.dumps(payload, ensure_ascii=False)),
            )
            self.db.execute(
                "DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 1000)"
            )

    def recent_events(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT id,created_at,event_type,payload FROM events ORDER BY id DESC LIMIT ?", (max(1, min(limit, 500)),)
        ).fetchall()
        return [
            {"id": row[0], "created_at": row[1], "type": row[2], "payload": json.loads(row[3])}
            for row in rows
        ]
