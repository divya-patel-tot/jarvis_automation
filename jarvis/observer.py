"""Observer (plan 4.1.9, 10.2.9): watches every agent event live.

* feeds the activity feed (event bus) and the audit log
* enforces budgets and the three timeouts (first event, idle, wall clock)
* re-checks every tool call against the policy engine. The Cursor hook already
  blocks violations *before* execution; if the observer ever sees a violating
  tool call that was NOT blocked (hook missing/broken), it kills the process
  tree immediately and quarantines the task (defence in depth)
* tails the hook decision log so blocked attempts are audited and surfaced
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Callable

from jarvis import paths
from jarvis.audit import AuditLog
from jarvis.config import BudgetSettings
from jarvis.events import bus
from jarvis.models import AgentEvent, AgentEventKind, ToolCategory
from jarvis.safety import policy
from jarvis.safety.budgets import BudgetExceeded, TaskBudget

log = logging.getLogger(__name__)


class Observer:
    def __init__(self, task_id: str, audit: AuditLog, limits: BudgetSettings, pctx: policy.PolicyContext,
                 cancel: Callable[[], None], kill: Callable[[], None], phase: str) -> None:
        self.task_id = task_id
        self.audit = audit
        self.budget = TaskBudget(limits)
        self.pctx = pctx
        self._cancel = cancel
        self._kill = kill
        self.phase = phase
        self.stop_reason = ""
        self.violation = ""
        self.blocked: list[dict] = []
        self.files_touched: list[str] = []
        self._done = threading.Event()
        self._hook_log = paths.hook_decisions_path()
        self._hook_offset = self._hook_log.stat().st_size if self._hook_log.exists() else 0
        self._watchdog = threading.Thread(target=self._watch, name=f"watchdog-{task_id}", daemon=True)

    def __enter__(self) -> "Observer":
        self._watchdog.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._done.set()
        self._watchdog.join(timeout=2)
        self._drain_hook_log()

    # ------------------------------------------------------------------ events
    def on_event(self, ev: AgentEvent) -> None:
        try:
            self.budget.on_event()
            if ev.kind == AgentEventKind.TOOL_STARTED:
                self.budget.on_tool_call()
                self._check_tool(ev)
                bus.publish("activity", task_id=self.task_id, text=ev.summary())
                self.audit.write("tool_started", self.task_id, phase=self.phase, tool=ev.tool,
                                 category=ev.category.value, args=ev.args)
            elif ev.kind == AgentEventKind.TOOL_FINISHED:
                if ev.category == ToolCategory.FILE_WRITE and ev.ok:
                    path = str(ev.args.get("path") or ev.args.get("file_path") or "")
                    if path:
                        self.files_touched.append(path)
                        self.budget.on_file_created()
                        self._check_tool(ev, completed=True)
                text = ev.summary()
                if not ev.ok:
                    err = ev.result.get("error", {}) if isinstance(ev.result, dict) else {}
                    msg = err.get("message", "") if isinstance(err, dict) else str(err)
                    if "blocked by a hook" in msg.lower() or "jarvis policy" in msg.lower():
                        text = f"Blocked by policy: {ev.tool} {str(ev.args.get('command') or ev.args.get('path') or '')[:100]}"
                bus.publish("activity", task_id=self.task_id, text=text)
                self.audit.write("tool_finished", self.task_id, phase=self.phase, tool=ev.tool, ok=ev.ok,
                                 result=str(ev.result)[:2000])
            elif ev.kind == AgentEventKind.TEXT_DELTA:
                bus.publish("agent_text", task_id=self.task_id, text=ev.text)
            elif ev.kind == AgentEventKind.THINKING:
                bus.publish("state", state="thinking")
            elif ev.kind == AgentEventKind.PLAN:
                self.audit.write("plan_proposed", self.task_id, plan=ev.text)
            elif ev.kind == AgentEventKind.USAGE:
                bus.publish("usage_delta", task_id=self.task_id, usage=ev.usage)
            elif ev.kind == AgentEventKind.ERROR:
                self.audit.write("agent_error", self.task_id, text=ev.text)
        except BudgetExceeded as exc:
            self._stop(f"budget: {exc}")
        self._drain_hook_log()

    def _check_tool(self, ev: AgentEvent, completed: bool = False) -> None:
        d = policy.check_tool(ev.tool, ev.args, self.pctx, self.pctx.allowed_mcp_tools)
        if d.allowed:
            return
        if not completed:
            # Expected: the hook will deny this call before it runs. Record it.
            log.info("tool call will be denied by hook: %s (%s)", ev.tool, d.reason)
            return
        # A violating call COMPLETED: the hook did not stop it -> hard stop.
        self.violation = f"{ev.tool}: {d.reason}"
        self.audit.write("policy_violation", self.task_id, tool=ev.tool, args=ev.args, reason=d.reason)
        bus.publish("error", text=f"Safety stop: {d.reason}")
        self._stop(f"policy violation: {d.reason}", hard=True)

    # ------------------------------------------------------------------ watchdog
    def _watch(self) -> None:
        while not self._done.wait(0.5):
            try:
                self.budget.check_time()
            except BudgetExceeded as exc:
                self._stop(f"timeout: {exc}")
                return
            self._drain_hook_log()

    def _stop(self, reason: str, hard: bool = False) -> None:
        if self.stop_reason:
            return
        self.stop_reason = reason
        log.warning("[%s] stopping run: %s", self.task_id, reason)
        self.audit.write("run_stopped", self.task_id, reason=reason, hard=hard)
        bus.publish("activity", task_id=self.task_id, text=f"Stopping: {reason}")
        if hard:
            self._kill()
            return
        self._cancel()
        # escalate if graceful cancel does not end the run quickly
        def escalate() -> None:
            if not self._done.wait(3.0):
                log.warning("[%s] graceful cancel too slow; hard kill", self.task_id)
                self._kill()
        threading.Thread(target=escalate, daemon=True).start()

    def _drain_hook_log(self) -> None:
        p: Path = self._hook_log
        try:
            if not p.exists() or p.stat().st_size <= self._hook_offset:
                return
            with p.open("r", encoding="utf-8", errors="replace") as f:
                f.seek(self._hook_offset)
                data = f.read()
                self._hook_offset = f.tell()
        except OSError:
            return
        for line in data.splitlines():
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("action") == "deny":
                self.blocked.append(d)
                if policy.DELETE_MARK in str(d.get("reason", "")):
                    self.audit.write("delete_held", self.task_id, input=d.get("input"))
                    bus.publish("activity", task_id=self.task_id, text=f"Delete held for your OK: {str(d.get('input'))[:120]}")
                    continue
                self.audit.write("hook_denied", self.task_id, tool=d.get("tool") or d.get("event"),
                                 input=d.get("input"), reason=d.get("reason"), tier=d.get("tier"))
                bus.publish("activity", task_id=self.task_id, text=f"Blocked: {d.get('reason')}")
                if int(d.get("tier") or 0) >= 3:
                    bus.publish("notify", title="Jarvis blocked a forbidden action", text=str(d.get("reason")))
