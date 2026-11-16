"""Cursor Python SDK backend (default; warm persistent bridge + agents).

Phase 0 facts this relies on (docs/DECISIONS.md):
* bridge launched via our Windows/3.11 shim (D-002), inside a Job Object
* ``mode="plan"`` makes no changes and returns the plan via a ``createPlan``
  tool call (D-003); ``local.force`` is NOT a safety lever in the SDK
* run.cancel() ends the stream in ~10 ms (D-004); hard kill ~0.1 s
* AGENTS.md + .cursor/hooks.json honoured with setting_sources=["project"] (D-005/6)
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from jarvis.backend.base import AgentBackend, BackendUnavailable, EventSink, RunSpec
from jarvis.models import AgentEvent, AgentEventKind, RunOutcome, ToolCategory
from jarvis.safety.policy import tool_category

log = logging.getLogger(__name__)


def _err_code(exc: Exception) -> str:
    name = exc.__class__.__name__
    return {
        "AuthenticationError": "auth",
        "PermissionDeniedError": "auth",
        "RateLimitError": "rate_limit",
        "NetworkError": "offline",
        "APITimeoutError": "offline",
        "IntegrationNotConnectedError": "not_enabled",
        "AgentBusyError": "busy",
    }.get(name, "error")


class SdkBackend(AgentBackend):
    name = "sdk"

    def __init__(self, workspace, api_key_getter, state_root, base_env: dict[str, str] | None = None,
                 setting_sources: tuple[str, ...] = ("project",)) -> None:
        self.workspace = workspace
        self._api_key_getter = api_key_getter
        self.state_root = state_root
        self.base_env = dict(base_env or {})
        self.setting_sources = list(setting_sources)
        self._mb = None
        self._lock = threading.RLock()
        self._agents: dict[str, tuple[Any, str, float]] = {}   # key -> (Agent, model, last_used)
        self._run = None
        self._models: list[str] = []

    # ------------------------------------------------------------------ bridge
    def _ensure_bridge(self):
        with self._lock:
            if self._mb is not None and self._mb.alive:
                return self._mb
            key = self._api_key_getter()
            if not key:
                raise BackendUnavailable("No Cursor API key configured (run: python -m jarvis set-key)", "auth")
            try:
                from jarvis.backend.sdk_bridge_win import launch_bridge_client
            except ImportError as exc:
                raise BackendUnavailable(f"cursor-sdk not installed: {exc}", "missing") from exc
            self._agents.clear()
            t0 = time.perf_counter()
            self._mb = launch_bridge_client(workspace=self.workspace, api_key=key, state_root=self.state_root,
                                            extra_env=self.base_env)
            log.info("bridge started in %.2fs", time.perf_counter() - t0)
            return self._mb

    def probe(self) -> tuple[bool, str]:
        try:
            mb = self._ensure_bridge()
            me = mb.client.me(api_key=self._api_key_getter())
            self._models = [m.id for m in mb.client.list_models(api_key=self._api_key_getter())]
            return True, f"authenticated as key '{me.api_key_name}', {len(self._models)} models"
        except BackendUnavailable as exc:
            return False, str(exc)
        except Exception as exc:
            return False, f"{exc.__class__.__name__}: {exc}"

    def list_models(self) -> list[str]:
        if not self._models:
            try:
                mb = self._ensure_bridge()
                self._models = [m.id for m in mb.client.list_models(api_key=self._api_key_getter())]
            except Exception as exc:
                log.warning("model discovery failed: %s", exc)
        return list(self._models)

    # ------------------------------------------------------------------ sessions
    def _agent_for(self, spec: RunSpec):
        from cursor_sdk import Agent, LocalAgentOptions

        mb = self._ensure_bridge()
        with self._lock:
            if spec.session_key and spec.session_key in self._agents:
                agent, model, _ = self._agents[spec.session_key]
                self._agents[spec.session_key] = (agent, model, time.time())
                return agent, False
            agent = Agent.create(
                client=mb.client, api_key=self._api_key_getter(), model=spec.model or "default",
                name=f"jarvis-{spec.session_key or 'oneshot'}",
                local=LocalAgentOptions(cwd=str(spec.cwd), setting_sources=self.setting_sources),
            )
            if spec.session_key:
                self._agents[spec.session_key] = (agent, spec.model, time.time())
            return agent, True

    def drop_session(self, key: str) -> None:
        with self._lock:
            entry = self._agents.pop(key, None)
        if entry:
            try:
                entry[0].close()
            except Exception:
                pass

    # ------------------------------------------------------------------ run
    def run(self, spec: RunSpec, on_event: EventSink) -> RunOutcome:
        from cursor_sdk import SendOptions

        t0 = time.monotonic()
        try:
            agent, fresh = self._agent_for(spec)
        except BackendUnavailable:
            raise
        except Exception as exc:
            code = _err_code(exc)
            if code in ("auth", "not_enabled", "offline"):
                raise BackendUnavailable(str(exc), code) from exc
            raise
        opts = SendOptions(
            mode="plan" if spec.mode == "plan" else "agent",
            model=spec.model or None,
            local={"force": spec.mode != "plan"},
            mcp_servers=spec.mcp_servers or None,
        )
        outcome = RunOutcome(status="error", session_id=getattr(agent, "agent_id", ""))
        text_parts: list[str] = []
        tool_calls = 0
        usage: dict[str, int] = {}
        try:
            run = agent.send(spec.prompt, opts)
            with self._lock:
                self._run = run
            for ev in run.events():
                for norm in self._normalize(ev):
                    if norm.kind == AgentEventKind.TEXT_DELTA:
                        text_parts.append(norm.text)
                    elif norm.kind == AgentEventKind.TOOL_STARTED:
                        tool_calls += 1
                    elif norm.kind == AgentEventKind.PLAN:
                        outcome.plan_text = norm.text
                    elif norm.kind == AgentEventKind.USAGE:
                        for k, v in norm.usage.items():
                            usage[k] = usage.get(k, 0) + int(v or 0)
                    on_event(norm)
            result = run.wait()
            outcome.status = {"finished": "finished", "cancelled": "cancelled", "expired": "timeout"}.get(
                str(result.status), "error")
            outcome.text = result.result or "".join(text_parts)
            outcome.error = result.error_message or ""
            outcome.error_code = result.error_code or ""
            if result.usage is not None and not usage:
                usage = {"input_tokens": result.usage.input_tokens, "output_tokens": result.usage.output_tokens,
                         "total_tokens": result.usage.total_tokens}
        except Exception as exc:
            code = _err_code(exc)
            outcome.status = "cancelled" if self._cancelled_flag() else "error"
            outcome.error = f"{exc.__class__.__name__}: {exc}"
            outcome.error_code = code
            outcome.text = "".join(text_parts)
            if code in ("auth", "not_enabled") and fresh:
                raise BackendUnavailable(outcome.error, code) from exc
        finally:
            with self._lock:
                self._run = None
                self._cancel_requested = False
            if not spec.session_key:
                try:
                    agent.close()
                except Exception:
                    pass
        outcome.duration_s = round(time.monotonic() - t0, 2)
        outcome.tool_calls = tool_calls
        outcome.usage = usage
        on_event(AgentEvent(AgentEventKind.FINISHED, text=outcome.status))
        return outcome

    _cancel_requested = False

    def _cancelled_flag(self) -> bool:
        return self._cancel_requested

    def cancel(self) -> None:
        with self._lock:
            run = self._run
            self._cancel_requested = True
        if run is not None:
            try:
                run.cancel()
            except Exception as exc:
                log.warning("graceful cancel failed (%s); hard kill", exc)
                self.kill()

    def kill(self) -> None:
        with self._lock:
            mb, self._mb = self._mb, None
            self._cancel_requested = True
            self._agents.clear()
        if mb is not None:
            mb.kill()
            log.warning("bridge process tree killed")

    def close(self) -> None:
        with self._lock:
            agents = list(self._agents.values())
            self._agents.clear()
            mb, self._mb = self._mb, None
        for agent, _, _ in agents:
            try:
                agent.close()
            except Exception:
                pass
        if mb is not None:
            mb.close()

    def rotate_idle(self, max_idle_s: float) -> None:
        now = time.time()
        for key, (_, _, last) in list(self._agents.items()):
            if now - last > max_idle_s:
                self.drop_session(key)

    # ------------------------------------------------------------------ normalize
    @staticmethod
    def _normalize(ev) -> list[AgentEvent]:
        out: list[AgentEvent] = []
        m = ev.sdk_message
        if m is not None:
            mtype = getattr(m, "type", "")
            if mtype == "status" and str(getattr(m, "status", "")).upper() == "RUNNING":
                out.append(AgentEvent(AgentEventKind.SESSION_STARTED, raw=m))
            elif mtype == "thinking":
                out.append(AgentEvent(AgentEventKind.THINKING, text=getattr(m, "text", "")))
            elif mtype == "assistant":
                for block in getattr(getattr(m, "message", None), "content", ()) or ():
                    t = getattr(block, "text", "")
                    if t:
                        out.append(AgentEvent(AgentEventKind.TEXT_DELTA, text=t))
            elif mtype == "tool_call":
                name = getattr(m, "name", "")
                args = m.args if isinstance(m.args, dict) else {"value": m.args}
                cat = ToolCategory(tool_category(name))
                status = getattr(m, "status", "")
                if status == "running":
                    out.append(AgentEvent(AgentEventKind.TOOL_STARTED, tool=name, category=cat,
                                          call_id=m.call_id, args=args))
                else:
                    res = m.result if isinstance(m.result, dict) else {}
                    ok = status == "completed" and res.get("status", "success") == "success"
                    out.append(AgentEvent(AgentEventKind.TOOL_FINISHED, tool=name, category=cat,
                                          call_id=m.call_id, args=args, result=m.result, ok=ok))
                    if cat == ToolCategory.PLAN and status == "completed":
                        plan = args.get("plan") or args.get("content") or ""
                        if plan:
                            out.append(AgentEvent(AgentEventKind.PLAN, text=str(plan)))
            elif mtype == "usage":
                u = m.usage
                out.append(AgentEvent(AgentEventKind.USAGE, usage={
                    "input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
                    "total_tokens": u.total_tokens}))
            elif mtype == "status" and str(getattr(m, "status", "")).upper() in ("ERROR", "FAILED"):
                out.append(AgentEvent(AgentEventKind.ERROR, text=getattr(m, "message", "") or "agent error"))
        return out
