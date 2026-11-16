"""Persistent conversation history (SQLite, app data) — Jarvis remembers what just happened.

Every turn (fast-path and agent, voice and typed) is stored. The last turns are
written into each task's context.md so follow-ups like "now write X in it" or
"send that to Ravi" work, also after a restart or in a fresh agent session.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from jarvis import paths

SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    source TEXT, request TEXT NOT NULL, route TEXT, intent TEXT,
    status TEXT, spoken TEXT, details TEXT, files TEXT, task_id TEXT
);
CREATE INDEX IF NOT EXISTS turns_ts ON turns(ts);
"""


class History:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (paths.state_dir() / "history.sqlite3")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.executescript(SCHEMA)
        self._db.commit()

    def add(self, request: str, *, source: str = "", route: str = "", intent: str = "", status: str = "",
            spoken: str = "", details: str = "", files: list[str] | None = None, task_id: str = "") -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO turns(ts, source, request, route, intent, status, spoken, details, files, task_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (time.time(), source, request[:2000], route, intent, status, spoken[:1000], details[:3000],
                 json.dumps(files or [])[:3000], task_id))
            self._db.commit()

    def recent(self, n: int = 8, within_s: float = 6 * 3600) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT ts, source, request, route, intent, status, spoken, details, files, task_id FROM turns "
                "WHERE ts > ? ORDER BY id DESC LIMIT ?", (time.time() - within_s, n)).fetchall()
        keys = ("ts", "source", "request", "route", "intent", "status", "spoken", "details", "files", "task_id")
        return [dict(zip(keys, r)) for r in reversed(rows)]

    def as_context(self, n: int = 8) -> str:
        lines = []
        for t in self.recent(n):
            when = time.strftime("%H:%M", time.localtime(t["ts"]))
            files = json.loads(t["files"] or "[]")
            line = f"- [{when}] user: \"{t['request']}\" -> {t['status'] or '?'}: {t['spoken'] or ''}"
            if files:
                line += f" (files: {', '.join(files[:5])})"
            lines.append(line)
        return "\n".join(lines)

    def search(self, text: str, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT ts, request, status, spoken FROM turns WHERE request LIKE ? OR spoken LIKE ? "
                "ORDER BY id DESC LIMIT ?", (f"%{text}%", f"%{text}%", limit)).fetchall()
        return [dict(zip(("ts", "request", "status", "spoken"), r)) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._db.close()
