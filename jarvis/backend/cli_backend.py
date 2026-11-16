"""Cursor CLI backend (fallback): ``agent -p --output-format stream-json``.

One process per run, placed in a kill-on-close Job Object. Events are parsed
from newline-delimited JSON. The same workspace hooks (.cursor/hooks.json)
enforce policy because the CLI reads project config from the workspace.

Stream-json shapes (Cursor docs; parser is tolerant of unknown fields):
  {"type":"system","subtype":"init","session_id":..., "model":..., "permissionMode":...}
  {"type":"assistant","message":{"content":[{"type":"text","text":"..."}]}, "session_id":...}
  {"type":"tool_call","subtype":"started"|"completed","call_id":...,
   "tool_call":{"shellToolCall":{"args":{...},"result":{...}}}}
  {"type":"result","subtype":"success","is_error":false,"result":"...","duration_ms":...,"session_id":...}
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from jarvis.backend.base import AgentBackend, BackendUnavailable, EventSink, RunSpec
from jarvis.models import AgentEvent, AgentEventKind, RunOutcome, ToolCategory
from jarvis.safety.policy import tool_category
from jarvis.supervisor.jobobject import JobObject

log = logging.getLogger(__name__)

CREATE_NO_WINDOW = 0x08000000
MAX_LINE = 2_000_000


def find_cli() -> str | None:
    for name in ("agent", "cursor-agent"):
        p = shutil.which(name)
        if p:
            return p
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "cursor-agent"
    for cand in (local / "agent.cmd", local / "agent.exe", local / "cursor-agent.cmd"):
        if cand.exists():
            return str(cand)
    return None


def parse_cli_event(obj: dict) -> list[AgentEvent]:
    t = obj.get("type")
    out: list[AgentEvent] = []
    if t == "system" and obj.get("subtype") == "init":
        out.append(AgentEvent(AgentEventKind.SESSION_STARTED, text=obj.get("model", ""), raw=obj))
    elif t == "assistant":
        for block in (obj.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                out.append(AgentEvent(AgentEventKind.TEXT_DELTA, text=block["text"]))
    elif t == "thinking":
        out.append(AgentEvent(AgentEventKind.THINKING, text=str(obj.get("text", ""))))
    elif t == "tool_call":
        tc = obj.get("tool_call") or {}
        key = next(iter(tc), "") if isinstance(tc, dict) else ""
        body = tc.get(key) if key else {}
        body = body if isinstance(body, dict) else {}
        name = key.replace("ToolCall", "") if key else str(obj.get("name", "tool"))
        if key == "function":
            name = str(body.get("name", "function"))
        args = body.get("args") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {"value": args}
        cat = ToolCategory(tool_category(name))
        if obj.get("subtype") == "started":
            out.append(AgentEvent(AgentEventKind.TOOL_STARTED, tool=name, category=cat,
                                  call_id=str(obj.get("call_id", "")), args=args))
        else:
            result = body.get("result")
            ok = not (isinstance(result, dict) and ("error" in result or "failure" in result
                                                    or result.get("rejected")))
            out.append(AgentEvent(AgentEventKind.TOOL_FINISHED, tool=name, category=cat,
                                  call_id=str(obj.get("call_id", "")), args=args, result=result, ok=ok))
            if cat == ToolCategory.PLAN:
                plan = args.get("plan") or args.get("content")
                if plan:
                    out.append(AgentEvent(AgentEventKind.PLAN, text=str(plan)))
    elif t == "result":
        usage = obj.get("usage") or {}
        if usage:
            out.append(AgentEvent(AgentEventKind.USAGE, usage={
                "input_tokens": int(usage.get("inputTokens", usage.get("input_tokens", 0)) or 0),
                "output_tokens": int(usage.get("outputTokens", usage.get("output_tokens", 0)) or 0)}))
    elif t == "error":
        out.append(AgentEvent(AgentEventKind.ERROR, text=str(obj.get("message") or obj)))
    return out


class CliBackend(AgentBackend):
    name = "cli"

    def __init__(self, workspace, api_key_getter, base_env: dict[str, str] | None = None) -> None:
        self.workspace = Path(workspace)
        self._api_key_getter = api_key_getter
        self.base_env = dict(base_env or {})
        self._proc: subprocess.Popen | None = None
        self._job: JobObject | None = None
        self._lock = threading.Lock()
        self._sessions: dict[str, str] = {}
        self._cancelled = False

    def probe(self) -> tuple[bool, str]:
        cli = find_cli()
        if not cli:
            return False, "Cursor CLI not installed (scripts\\install_cursor_cli.ps1)"
        if not self._api_key_getter():
            return False, "No Cursor API key configured"
        try:
            r = subprocess.run([cli, "--version"], capture_output=True, text=True, timeout=30,
                               env=self._env(), creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)
            return r.returncode == 0, (r.stdout or r.stderr).strip()[:200]
        except Exception as exc:
            return False, f"CLI probe failed: {exc}"

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(self.base_env)
        env["CURSOR_API_KEY"] = self._api_key_getter()
        return env

    def list_models(self) -> list[str]:
        cli = find_cli()
        if not cli:
            return []
        try:
            r = subprocess.run([cli, "models"], capture_output=True, text=True, timeout=60, env=self._env(),
                               creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)
            ids = []
            for line in r.stdout.splitlines():
                tok = line.strip().split(" ")[0].strip("-* ")
                if tok and not tok.lower().startswith(("available", "model")):
                    ids.append(tok)
            return ids
        except Exception:
            return []

    def run(self, spec: RunSpec, on_event: EventSink) -> RunOutcome:
        cli = find_cli()
        if not cli:
            raise BackendUnavailable("Cursor CLI not installed", "missing")
        argv = [cli, "-p", "--output-format", "stream-json", "--stream-partial-output",
                "--workspace", str(self.workspace), "--trust"]
        if spec.model:
            argv += ["--model", spec.model]
        if spec.mode == "plan":
            argv += ["--mode", "plan"]
        else:
            argv += ["--force"]
        resume = self._sessions.get(spec.session_key) if spec.session_key else None
        if resume:
            argv += ["--resume", resume]
        # Prompt via a file reference keeps argv small (stdin cap ~10MB, cmd line ~32K).
        prompt = spec.prompt
        if len(prompt) > 20_000:
            pf = spec.cwd / "prompt.md"
            pf.write_text(prompt, encoding="utf-8")
            prompt = f"Read and follow the instructions in {pf}."
        argv.append(prompt)

        t0 = time.monotonic()
        job = JobObject()
        env = self._env()
        env.update(spec.extra_env)
        proc = subprocess.Popen(argv, cwd=str(spec.cwd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", env=env,
                                creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)
        job.assign(proc)
        with self._lock:
            self._proc, self._job, self._cancelled = proc, job, False
        stderr_tail: list[str] = []

        def pump_err() -> None:
            for line in proc.stderr:  # type: ignore[union-attr]
                stderr_tail.append(line)
                del stderr_tail[:-50]
        threading.Thread(target=pump_err, daemon=True).start()

        outcome = RunOutcome(status="error")
        texts: list[str] = []
        tool_calls = 0
        usage: dict[str, int] = {}
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                line = line.strip()
                if not line or len(line) > MAX_LINE:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                sid = obj.get("session_id") or obj.get("chatId")
                if sid:
                    outcome.session_id = sid
                    if spec.session_key:
                        self._sessions[spec.session_key] = sid
                if obj.get("type") == "result":
                    outcome.text = str(obj.get("result", "")) or "".join(texts)
                    outcome.status = "error" if obj.get("is_error") or obj.get("subtype") not in (None, "success") \
                        else "finished"
                for ev in parse_cli_event(obj):
                    if ev.kind == AgentEventKind.TEXT_DELTA:
                        texts.append(ev.text)
                    elif ev.kind == AgentEventKind.TOOL_STARTED:
                        tool_calls += 1
                    elif ev.kind == AgentEventKind.PLAN:
                        outcome.plan_text = ev.text
                    elif ev.kind == AgentEventKind.USAGE:
                        usage.update(ev.usage)
                    on_event(ev)
            proc.wait(timeout=10)
        except Exception as exc:
            outcome.error = f"{exc.__class__.__name__}: {exc}"
        finally:
            job.terminate()
            job.close()
            with self._lock:
                cancelled = self._cancelled
                self._proc = self._job = None
        if cancelled:
            outcome.status = "cancelled"
        if outcome.status == "error" and not outcome.error:
            err = "".join(stderr_tail)[-1500:]
            outcome.error = err or f"exit code {proc.returncode}"
            low = err.lower()
            if "unauthorized" in low or "api key" in low or "not authenticated" in low:
                raise BackendUnavailable(err, "auth")
            outcome.error_code = "rate_limit" if "rate limit" in low or "usage limit" in low else "error"
        if not outcome.text:
            outcome.text = "".join(texts)
        outcome.duration_s = round(time.monotonic() - t0, 2)
        outcome.tool_calls = tool_calls
        outcome.usage = usage
        on_event(AgentEvent(AgentEventKind.FINISHED, text=outcome.status))
        return outcome

    def cancel(self) -> None:
        self.kill()

    def kill(self) -> None:
        with self._lock:
            self._cancelled = True
            job = self._job
        if job is not None:
            job.terminate()

    def drop_session(self, key: str) -> None:
        self._sessions.pop(key, None)
