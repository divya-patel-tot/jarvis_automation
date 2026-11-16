"""Timers, reminders (persisted across restarts) and scheduled routines.

Routines run unattended in DIRECT policy mode: only tier 0/1 actions are
possible (the policy hook denies tier 2 outside an approved execute run), and
their results are queued for the user (plan 4A.4, phase 7).
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import datetime
from typing import Callable

from jarvis import paths
from jarvis.events import bus

log = logging.getLogger(__name__)

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class Scheduler:
    def __init__(self, routines: list[dict] | None = None,
                 run_routine: Callable[[dict], None] | None = None) -> None:
        self.path = paths.state_dir() / "reminders.json"
        self.items: list[dict] = self._load()
        self.routines = routines or []
        self.run_routine = run_routine
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._last_routine_minute: dict[str, str] = {}
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)

    def _load(self) -> list[dict]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.items, indent=1), encoding="utf-8")

    def start(self) -> None:
        # Reminders that fired while Jarvis was off: announce them now as missed.
        now = time.time()
        with self._lock:
            missed = [i for i in self.items if i["due"] < now]
        for i in missed:
            self._fire(i, missed=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def add(self, kind: str, text: str, due_ts: float) -> dict:
        item = {"id": uuid.uuid4().hex[:8], "kind": kind, "text": text, "due": due_ts,
                "created": time.time()}
        with self._lock:
            self.items.append(item)
            self._save()
        return item

    def cancel(self, item_id: str) -> bool:
        with self._lock:
            before = len(self.items)
            self.items = [i for i in self.items if i["id"] != item_id]
            self._save()
            return len(self.items) < before

    def pending(self) -> list[dict]:
        with self._lock:
            return sorted(self.items, key=lambda i: i["due"])

    def _fire(self, item: dict, missed: bool = False) -> None:
        with self._lock:
            self.items = [i for i in self.items if i["id"] != item["id"]]
            self._save()
        if item["kind"] == "timer":
            msg = "Your timer is done." if not missed else "A timer finished while I was off."
        else:
            msg = f"Reminder: {item['text']}" if not missed else f"Missed reminder: {item['text']}"
        log.info("scheduler fired: %s", msg)
        bus.publish("say", text=msg)
        bus.publish("notify", title="Jarvis", text=msg)

    def _loop(self) -> None:
        while not self._stop.wait(1.0):
            now = time.time()
            with self._lock:
                due = [i for i in self.items if i["due"] <= now]
            for i in due:
                self._fire(i)
            self._check_routines()

    def _check_routines(self) -> None:
        if not self.routines or self.run_routine is None:
            return
        now = datetime.now()
        stamp = now.strftime("%Y-%m-%d %H:%M")
        for r in self.routines:
            if not r.get("enabled", True):
                continue
            if r.get("time") != now.strftime("%H:%M"):
                continue
            days = [d[:3].lower() for d in r.get("days", DAYS)]
            if DAYS[now.weekday()] not in days:
                continue
            key = r.get("name", r.get("prompt", ""))[:40]
            if self._last_routine_minute.get(key) == stamp:
                continue
            self._last_routine_minute[key] = stamp
            threading.Thread(target=self.run_routine, args=(r,), name=f"routine-{key}", daemon=True).start()
