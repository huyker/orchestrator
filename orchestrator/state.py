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
            CREATE TABLE IF NOT EXISTS bootstrap (
              singleton INTEGER PRIMARY KEY CHECK(singleton=1),
              dashboard_verified_at REAL,
              dashboard_address TEXT
            )
            """
        )
        self.db.execute("INSERT OR IGNORE INTO bootstrap(singleton,dashboard_verified_at,dashboard_address) VALUES(1,NULL,NULL)")
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
            CREATE TABLE IF NOT EXISTS task_leases (
              issue_number INTEGER PRIMARY KEY,
              instance_id TEXT NOT NULL,
              status TEXT NOT NULL,
              payload TEXT NOT NULL,
              heartbeat REAL NOT NULL,
              created_at REAL NOT NULL
            )
            """
        )
        try:
            old_rows = self.db.execute("SELECT instance_id, issue_number, status, payload, heartbeat, created_at FROM lease").fetchall()
            for old_row in old_rows:
                self.db.execute(
                    "INSERT OR IGNORE INTO task_leases(instance_id, issue_number, status, payload, heartbeat, created_at) VALUES(?,?,?,?,?,?)",
                    old_row,
                )
        except Exception:
            pass
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
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS config (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL,
              updated_at REAL NOT NULL
            )
            """
        )

    def set_config(self, key: str, value: str) -> None:
        with self._lock:
            self.db.execute(
                "INSERT INTO config(key, value, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, str(value), time.time()),
            )

    def get_config(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def delete_config(self, key: str) -> None:
        with self._lock:
            self.db.execute("DELETE FROM config WHERE key=?", (key,))

    def close(self) -> None:
        with self._lock:
            try:
                self.db.close()
            except Exception:
                pass

    def __del__(self) -> None:
        self.close()

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

    def get_lease(self, issue_number: int | None = None) -> dict[str, Any] | None:
        with self._lock:
            if issue_number is not None:
                row = self.db.execute(
                    "SELECT instance_id, issue_number, status, payload, heartbeat, created_at FROM task_leases WHERE issue_number = ?",
                    (int(issue_number),)
                ).fetchone()
            else:
                row = self.db.execute(
                    "SELECT instance_id, issue_number, status, payload, heartbeat, created_at FROM task_leases ORDER BY created_at ASC LIMIT 1"
                ).fetchone()
            if row:
                return self._row_to_lease(row)
            if issue_number is not None:
                legacy = self.db.execute(
                    "SELECT instance_id, issue_number, status, payload, heartbeat, created_at FROM lease WHERE singleton=1 AND issue_number = ?",
                    (int(issue_number),)
                ).fetchone()
            else:
                legacy = self.db.execute(
                    "SELECT instance_id, issue_number, status, payload, heartbeat, created_at FROM lease WHERE singleton=1"
                ).fetchone()
            return self._row_to_lease(legacy)

    def get_active_leases(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.db.execute(
                "SELECT instance_id, issue_number, status, payload, heartbeat, created_at FROM task_leases ORDER BY created_at ASC"
            ).fetchall()
            leases = [self._row_to_lease(r) for r in rows if r]
            if not leases:
                legacy = self.db.execute(
                    "SELECT instance_id, issue_number, status, payload, heartbeat, created_at FROM lease WHERE singleton=1"
                ).fetchone()
                if legacy:
                    lease = self._row_to_lease(legacy)
                    if lease:
                        leases.append(lease)
            return leases

    def active_lease_count(self) -> int:
        with self._lock:
            row = self.db.execute("SELECT COUNT(*) FROM task_leases").fetchone()
            count = row[0] if row else 0
            if count == 0:
                legacy = self.db.execute("SELECT COUNT(*) FROM lease WHERE singleton=1").fetchone()
                return legacy[0] if legacy else 0
            return count

    def claim(self, instance_id: str, issue_number: int, status: str, payload: dict[str, Any], max_workers: int = 1) -> bool:
        now = time.time()
        encoded = json.dumps(payload, ensure_ascii=False)
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                # Check if this issue is already claimed by another active instance
                row = self.db.execute("SELECT instance_id, heartbeat FROM task_leases WHERE issue_number = ?", (issue_number,)).fetchone()
                if row:
                    other_inst, hb = row
                    if other_inst != instance_id and (now - float(hb)) <= 90:
                        self.db.execute("ROLLBACK")
                        return False

                # Check max_workers capacity
                count_row = self.db.execute("SELECT COUNT(*) FROM task_leases WHERE issue_number != ?", (issue_number,)).fetchone()
                active_count = count_row[0] if count_row else 0
                if active_count >= max_workers:
                    self.db.execute("ROLLBACK")
                    return False

                self.db.execute(
                    "INSERT OR REPLACE INTO task_leases(issue_number, instance_id, status, payload, heartbeat, created_at) VALUES(?,?,?,?,?,?)",
                    (issue_number, instance_id, status, encoded, now, now),
                )
                self.db.execute(
                    "INSERT OR REPLACE INTO lease(singleton, instance_id, issue_number, status, payload, heartbeat, created_at) VALUES(1,?,?,?,?,?,?)",
                    (instance_id, issue_number, status, encoded, now, now),
                )
                self.db.execute("COMMIT")
                return True
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def takeover_if_stale(self, instance_id: str, timeout_seconds: int, issue_number: int | None = None) -> dict[str, Any] | None:
        now = time.time()
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                if issue_number is not None:
                    row = self.db.execute(
                        "SELECT instance_id, issue_number, status, payload, heartbeat, created_at FROM task_leases WHERE issue_number = ?",
                        (issue_number,)
                    ).fetchone()
                else:
                    row = self.db.execute(
                        "SELECT instance_id, issue_number, status, payload, heartbeat, created_at FROM task_leases WHERE instance_id != ? ORDER BY heartbeat ASC LIMIT 1",
                        (instance_id,)
                    ).fetchone()
                    legacy = self.db.execute(
                        "SELECT instance_id, issue_number, status, payload, heartbeat, created_at FROM lease WHERE singleton=1"
                    ).fetchone()
                    if legacy and row and legacy[4] < row[4]:
                        row = (row[0], row[1], row[2], row[3], legacy[4], row[5])
                    elif not row and legacy:
                        row = legacy
                lease = self._row_to_lease(row)
                if not lease or lease["instance_id"] == instance_id:
                    self.db.execute("COMMIT")
                    return lease
                if now - float(lease["heartbeat"]) <= timeout_seconds:
                    self.db.execute("COMMIT")
                    return None
                payload = dict(lease["payload"])
                payload["recovered_from_instance"] = lease["instance_id"]
                target_issue = lease["issue_number"]
                self.db.execute(
                    "UPDATE task_leases SET instance_id=?, status=?, payload=?, heartbeat=? WHERE issue_number=?",
                    (instance_id, "RECOVERING", json.dumps(payload, ensure_ascii=False), now, target_issue),
                )
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

    def heartbeat(self, instance_id: str, issue_number: int | None = None) -> None:
        with self._lock:
            now = time.time()
            if issue_number is not None:
                self.db.execute(
                    "UPDATE task_leases SET heartbeat=? WHERE issue_number=? AND instance_id=?",
                    (now, issue_number, instance_id),
                )
            else:
                self.db.execute(
                    "UPDATE task_leases SET heartbeat=? WHERE instance_id=?",
                    (now, instance_id),
                )
            self.db.execute(
                "UPDATE lease SET heartbeat=? WHERE singleton=1 AND instance_id=?",
                (now, instance_id),
            )

    def update_lease(
        self,
        instance_id: str,
        *,
        status: str | None = None,
        payload: dict[str, Any] | None = None,
        issue_number: int | None = None,
    ) -> None:
        with self._lock:
            current = self.get_lease(issue_number)
            if not current:
                return
            next_status = status or current["status"]
            next_payload = payload if payload is not None else current["payload"]
            target_issue = current["issue_number"]
            self.db.execute(
                "UPDATE task_leases SET status=?, payload=?, heartbeat=?, instance_id=? WHERE issue_number=?",
                (next_status, json.dumps(next_payload, ensure_ascii=False), time.time(), instance_id, target_issue),
            )
            legacy_row = self.db.execute("SELECT issue_number FROM lease WHERE singleton=1").fetchone()
            if legacy_row and legacy_row[0] == target_issue:
                self.db.execute(
                    "UPDATE lease SET status=?, payload=?, heartbeat=?, instance_id=? WHERE singleton=1",
                    (next_status, json.dumps(next_payload, ensure_ascii=False), time.time(), instance_id),
                )

    def release(self, instance_id: str | None = None, issue_number: int | None = None) -> None:
        with self._lock:
            if issue_number is not None:
                if instance_id:
                    self.db.execute("DELETE FROM task_leases WHERE issue_number=? AND instance_id=?", (issue_number, instance_id))
                    self.db.execute("DELETE FROM lease WHERE singleton=1 AND issue_number=? AND instance_id=?", (issue_number, instance_id))
                else:
                    self.db.execute("DELETE FROM task_leases WHERE issue_number=?", (issue_number,))
                    self.db.execute("DELETE FROM lease WHERE singleton=1 AND issue_number=?", (issue_number,))
            elif instance_id:
                self.db.execute("DELETE FROM task_leases WHERE instance_id=?", (instance_id,))
                self.db.execute("DELETE FROM lease WHERE singleton=1 AND instance_id=?", (instance_id,))
            else:
                self.db.execute("DELETE FROM task_leases")
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

    def mark_dashboard_verified(self, address: str) -> None:
        with self._lock:
            self.db.execute(
                "UPDATE bootstrap SET dashboard_verified_at=?, dashboard_address=? WHERE singleton=1",
                (time.time(), address),
            )

    def dashboard_verification(self) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT dashboard_verified_at,dashboard_address FROM bootstrap WHERE singleton=1"
        ).fetchone()
        return {
            "verified": bool(row and row[0]),
            "verified_at": row[0] if row else None,
            "address": row[1] if row else None,
        }

    def is_dashboard_verified(self) -> bool:
        return bool(self.dashboard_verification()["verified"])

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
