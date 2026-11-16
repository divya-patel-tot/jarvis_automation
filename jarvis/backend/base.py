"""Agent backend contract (plan 6.1). No other module knows whether the CLI or
the SDK is underneath."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from jarvis.models import AgentEvent, RunOutcome

EventSink = Callable[[AgentEvent], None]


class BackendUnavailable(Exception):
    """Backend cannot be used (not installed, not authorized, offline...)."""

    def __init__(self, message: str, code: str = "unavailable") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class RunSpec:
    prompt: str
    cwd: Path
    mode: str = "agent"                # "plan" (propose only) | "agent" (execute)
    model: str = "default"
    session_key: str = ""              # "" -> fresh one-shot session
    mcp_servers: dict = field(default_factory=dict)
    extra_env: dict[str, str] = field(default_factory=dict)


class AgentBackend(abc.ABC):
    name: str = "base"

    @abc.abstractmethod
    def probe(self) -> tuple[bool, str]:
        """(usable, reason). Cheap; may start helper processes."""

    @abc.abstractmethod
    def run(self, spec: RunSpec, on_event: EventSink) -> RunOutcome:
        """Blocking run; streams normalized events to on_event."""

    @abc.abstractmethod
    def cancel(self) -> None:
        """Graceful cancel of the current run (thread-safe)."""

    @abc.abstractmethod
    def kill(self) -> None:
        """Hard kill: terminate the whole process tree (kill switch)."""

    def list_models(self) -> list[str]:
        return []

    def drop_session(self, key: str) -> None:
        pass

    def health(self) -> dict:
        ok, reason = self.probe()
        return {"backend": self.name, "ok": ok, "reason": reason}

    def close(self) -> None:
        pass


def pick_model(preferences: list[str], available: list[str]) -> str:
    """First preference the account exposes; else 'default' (Auto)."""
    avail = set(available)
    for m in preferences:
        if m in avail:
            return m
    return "default" if (not available or "default" in avail) else available[0]
