"""Thread-safe in-process event bus (core <-> UI boundary, plan 12).

Topics used across the app:
  state            {"state": idle|listening|transcribing|thinking|planning|awaiting_approval|acting|speaking|error}
  transcript       {"text", "final": bool}
  activity         {"task_id", "text"}
  plan             {"task_id", "plan": Plan, "confirm_text"}
  approval_request {"task_id", "nonce", "confirm_text", "tier", "timeout_s"}
  result           {"task_id", "result": TaskResult}
  say              {"text"}
  notify           {"title", "text"}
  error            {"text"}
  usage            {"runs", "tokens"}
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from typing import Any, Callable

log = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], None]


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[str, list[Handler]] = defaultdict(list)
        self._lock = threading.RLock()

    def subscribe(self, topic: str, handler: Handler) -> Callable[[], None]:
        with self._lock:
            self._subs[topic].append(handler)

        def unsubscribe() -> None:
            with self._lock:
                try:
                    self._subs[topic].remove(handler)
                except ValueError:
                    pass
        return unsubscribe

    def publish(self, topic: str, **payload: Any) -> None:
        with self._lock:
            handlers = list(self._subs.get(topic, ())) + list(self._subs.get("*", ()))
        payload.setdefault("topic", topic)
        for h in handlers:
            try:
                h(payload)
            except Exception:  # a broken subscriber must never break the core
                log.exception("event handler failed for %s", topic)


bus = EventBus()
