"""Shared data types."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from pathlib import Path
from typing import Any


class RiskTier(IntEnum):
    READ_ONLY = 0      # list, read, summarize, search, system info
    REVERSIBLE = 1     # open apps, create files in outputs, volume, copy
    CONSEQUENTIAL = 2  # delete/move/overwrite, unknown scripts, send, install...
    FORBIDDEN = 3      # security, credentials, registry, boot, format, UAC

    @property
    def label(self) -> str:
        return ["read-only", "reversible", "needs approval", "forbidden"][int(self)]


class Source(str, Enum):
    VOICE = "voice"
    TYPED = "typed"
    ROUTINE = "routine"


class Mode(str, Enum):
    PLAN = "plan"
    EXECUTE = "execute"
    DIRECT = "direct"   # tier-0 read-only tasks executed without a plan phase


class ToolCategory(str, Enum):
    SHELL = "shell"
    FILE_READ = "file_read"
    FILE_WRITE = "file_write"
    WEB_FETCH = "web_fetch"
    MCP = "mcp"
    PLAN = "plan"
    OTHER = "other"


@dataclass
class UserRequest:
    text: str
    source: Source = Source.TYPED
    language: str = "en"
    confidence: float = 1.0
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created: float = field(default_factory=time.time)
    follow_up_of: str | None = None   # previous task id for "do that again" etc.


class AgentEventKind(str, Enum):
    SESSION_STARTED = "session_started"
    TEXT_DELTA = "text_delta"
    TEXT = "text"
    THINKING = "thinking"
    TOOL_STARTED = "tool_started"
    TOOL_FINISHED = "tool_finished"
    PLAN = "plan"
    USAGE = "usage"
    WARNING = "warning"
    ERROR = "error"
    FINISHED = "finished"


@dataclass
class AgentEvent:
    kind: AgentEventKind
    text: str = ""
    tool: str = ""
    category: ToolCategory = ToolCategory.OTHER
    call_id: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    result: Any = None
    ok: bool = True
    usage: dict[str, int] = field(default_factory=dict)
    raw: Any = None
    ts: float = field(default_factory=time.time)

    def summary(self) -> str:
        if self.kind in (AgentEventKind.TOOL_STARTED, AgentEventKind.TOOL_FINISHED):
            target = self.args.get("command") or self.args.get("path") or self.args.get("file_path") \
                or self.args.get("url") or ""
            verb = "Running" if self.kind == AgentEventKind.TOOL_STARTED else ("Done" if self.ok else "Failed")
            return f"{verb}: {self.tool} {str(target)[:120]}".strip()
        return self.text[:200]


@dataclass
class RunOutcome:
    """What the backend returns at the end of a run."""
    status: str                 # finished | error | cancelled | timeout | blocked
    text: str = ""
    plan_text: str = ""
    error: str = ""
    error_code: str = ""
    duration_s: float = 0.0
    tool_calls: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    session_id: str = ""


@dataclass
class TaskResult:
    """Structured final result (agent output contract, plan 7.2)."""
    status: str = "unknown"          # success | partial | failed | blocked | cancelled | needs_input
    spoken: str = ""
    details: str = ""
    files_changed: list[str] = field(default_factory=list)
    double_check: list[str] = field(default_factory=list)
    question: str = ""               # set when the agent needs clarification
    injection_warning: str = ""
    verified: bool | None = None     # agent's claim that it checked the real effect (None = not stated)


@dataclass
class PlanStep:
    number: int
    text: str
    tier: RiskTier = RiskTier.REVERSIBLE
    targets: list[str] = field(default_factory=list)


@dataclass
class Plan:
    raw: str
    steps: list[PlanStep]
    tier: RiskTier
    targets: list[str]
    summary: str = ""
    forbidden_reasons: list[str] = field(default_factory=list)


@dataclass
class TaskContext:
    request: UserRequest
    task_id: str
    folder: Path
    tier: RiskTier = RiskTier.READ_ONLY
    tainted: bool = False
    mode: Mode = Mode.PLAN
    plan: Plan | None = None
    approved: bool = False
    needs_desktop: bool = False
    ingested: list[str] = field(default_factory=list)
