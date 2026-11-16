"""Per-task budgets and the daily usage ledger (plan 10.2.8, 13.3)."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from jarvis import paths
from jarvis.config import BudgetSettings


class BudgetExceeded(Exception):
    pass


@dataclass
class TaskBudget:
    limits: BudgetSettings
    started: float = field(default_factory=time.monotonic)
    last_event: float = field(default_factory=time.monotonic)
    first_event_seen: bool = False
    events: int = 0
    tool_calls: int = 0
    screenshots: int = 0
    files_created: int = 0

    def on_event(self) -> None:
        self.events += 1
        self.first_event_seen = True
        self.last_event = time.monotonic()
        if self.events > self.limits.max_events:
            raise BudgetExceeded(f"event limit ({self.limits.max_events}) exceeded")

    def on_tool_call(self) -> None:
        self.tool_calls += 1
        if self.tool_calls > self.limits.max_tool_calls:
            raise BudgetExceeded(f"tool-call limit ({self.limits.max_tool_calls}) exceeded")

    def on_file_created(self) -> None:
        self.files_created += 1
        if self.files_created > self.limits.max_files_created:
            raise BudgetExceeded(f"files-created limit ({self.limits.max_files_created}) exceeded")

    def on_screenshot(self) -> None:
        self.screenshots += 1
        if self.screenshots > self.limits.max_screenshots:
            raise BudgetExceeded(f"screenshot limit ({self.limits.max_screenshots}) exceeded")

    def check_time(self) -> None:
        now = time.monotonic()
        if now - self.started > self.limits.task_wall_clock_s:
            raise BudgetExceeded(f"task took longer than {self.limits.task_wall_clock_s}s")
        if not self.first_event_seen and now - self.started > self.limits.first_event_timeout_s:
            raise BudgetExceeded(f"agent did not start within {self.limits.first_event_timeout_s}s")
        if self.first_event_seen and now - self.last_event > self.limits.idle_timeout_s:
            raise BudgetExceeded(f"agent silent for {self.limits.idle_timeout_s}s (hung)")


class UsageLedger:
    """Daily counters persisted in app data; gates agent use when exhausted."""

    def __init__(self, limits: BudgetSettings, path: Path | None = None) -> None:
        self.limits = limits
        self.path = path or paths.usage_ledger_path()
        self._lock = threading.Lock()
        self._data = self._load()

    def _today(self) -> str:
        return time.strftime("%Y-%m-%d")

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        if data.get("day") != self._today():
            data = {"day": self._today(), "runs": 0, "tokens": 0, "tool_calls": 0, "seconds": 0.0}
        return data

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data), encoding="utf-8")

    def record(self, *, runs: int = 0, tokens: int = 0, tool_calls: int = 0, seconds: float = 0.0) -> None:
        with self._lock:
            if self._data.get("day") != self._today():
                self._data = self._load()
            self._data["runs"] += runs
            self._data["tokens"] += tokens
            self._data["tool_calls"] += tool_calls
            self._data["seconds"] += seconds
            self._save()

    def snapshot(self) -> dict:
        with self._lock:
            if self._data.get("day") != self._today():
                self._data = self._load()
            return dict(self._data)

    def status(self) -> tuple[str, str]:
        """('ok'|'warn'|'exhausted', message)."""
        d = self.snapshot()
        frac = max(d["runs"] / max(1, self.limits.daily_agent_runs), d["tokens"] / max(1, self.limits.daily_tokens))
        if frac >= 1.0:
            return "exhausted", "Daily Cursor usage cap reached; only quick commands work until tomorrow."
        if frac >= self.limits.warn_fraction:
            return "warn", f"Cursor usage at {int(frac * 100)}% of today's cap."
        return "ok", ""
