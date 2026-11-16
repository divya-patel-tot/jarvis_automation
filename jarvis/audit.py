"""Append-only, hash-chained audit trail (plan 10.2.11).

Each line is a JSON object with ``prev`` = sha256 of the previous line, so any
edit or deletion in the middle breaks ``verify()``. Secrets are redacted. The
file lives in app data, outside the agent workspace, and the policy hook denies
agent access to that directory.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any

from jarvis import paths
from jarvis.secrets import redact

_GENESIS = "0" * 64


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)[:8000]
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value][:500]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact(str(value))[:8000]


class AuditLog:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or paths.audit_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._prev = self._last_hash()

    def _last_hash(self) -> str:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return _GENESIS
        with self.path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 65536))
            lines = f.read().splitlines()
        for line in reversed(lines):
            if line.strip():
                return hashlib.sha256(line).hexdigest()
        return _GENESIS

    def write(self, event: str, task_id: str = "", **data: Any) -> None:
        with self._lock:
            entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, "task_id": task_id,
                     "data": _clean(data), "prev": self._prev}
            line = json.dumps(entry, ensure_ascii=False, sort_keys=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
            self._prev = hashlib.sha256(line.encode("utf-8")).hexdigest()

    def tail(self, n: int = 200, task_id: str | None = None) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        out = []
        for line in reversed(lines):
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if task_id and e.get("task_id") != task_id:
                continue
            out.append(e)
            if len(out) >= n:
                break
        return list(reversed(out))

    def verify(self) -> tuple[bool, int]:
        """Return (ok, first_bad_line_number or total_lines)."""
        prev = _GENESIS
        if not self.path.exists():
            return True, 0
        with self.path.open("rb") as f:
            for i, raw in enumerate(f, start=1):
                raw = raw.rstrip(b"\r\n")
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    return False, i
                if entry.get("prev") != prev:
                    return False, i
                prev = hashlib.sha256(raw).hexdigest()
        return True, i if self.path.stat().st_size else 0
