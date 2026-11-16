"""Cursor hook: pre-execution policy enforcement (Phase 0 D-006/D-007).

Registered in <workspace>/.cursor/hooks.json for preToolUse,
beforeShellExecution, beforeReadFile and beforeMCPExecution. Cursor runs it
before every tool call with a JSON payload on stdin; we print
``{"permission": "allow"|"deny", ...}``. A deny blocks the tool call.

Invoked as ``python -I -S policy_hook.py`` (isolated, stdlib only, fast).
Fails CLOSED: any error -> deny. The per-run policy context is read from
JARVIS_POLICY_STATE (a file in app data, outside the workspace).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jarvis.safety import policy  # noqa: E402  (stdlib-only module)


def _state_path() -> Path:
    env = os.environ.get("JARVIS_POLICY_STATE")
    if env:
        return Path(env)
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return base / "Jarvis" / "state" / "policy_state.json"


def _load_ctx(payload: dict) -> policy.PolicyContext:
    path = _state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return policy.PolicyContext.from_dict(data)
    except Exception:
        # No context (e.g. someone opened the workspace in Cursor manually):
        # most restrictive sane default - plan mode, workspace only.
        roots = payload.get("workspace_roots") or []
        return policy.PolicyContext(mode="plan", workspace=roots[0] if roots else "")


def _as_dict(value: object) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
            return loaded if isinstance(loaded, dict) else {"value": value}
        except json.JSONDecodeError:
            return {"command": value}
    return {}


def decide(payload: dict) -> policy.Decision:
    ctx = _load_ctx(payload)
    event = payload.get("hook_event_name", "")
    if event == "beforeShellExecution":
        return policy.check_shell(str(payload.get("command", "")), ctx)
    if event == "beforeReadFile":
        return policy.check_read(str(payload.get("file_path", "")), ctx)
    if event == "beforeMCPExecution":
        return policy.check_tool("mcp:" + str(payload.get("tool_name", "")), _as_dict(payload.get("tool_input")),
                                 ctx, ctx.allowed_mcp_tools)
    if event == "preToolUse":
        name = str(payload.get("tool_name", ""))
        return policy.check_tool(name, _as_dict(payload.get("tool_input")), ctx, ctx.allowed_mcp_tools)
    return policy.Decision("allow")


def _log(payload: dict, decision: policy.Decision) -> None:
    try:
        log_path = _state_path().with_name("hook_decisions.jsonl")
        entry = {
            "ts": time.time(), "event": payload.get("hook_event_name"), "tool": payload.get("tool_name"),
            "input": str(payload.get("tool_input") or payload.get("command") or payload.get("file_path") or "")[:600],
            "action": decision.action, "reason": decision.reason, "tier": decision.tier,
            "session": payload.get("session_id") or payload.get("conversation_id"),
        }
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def main() -> int:
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", errors="replace").lstrip("﻿")
        payload = json.loads(raw) if raw.strip() else {}
        decision = decide(payload)
    except Exception as exc:  # fail closed
        payload, decision = {}, policy.Decision("deny", f"policy hook error: {exc.__class__.__name__}", 3)
    _log(payload, decision)
    if decision.allowed:
        out = {"permission": "allow", "continue": True}
    else:
        msg = f"Blocked by Jarvis policy: {decision.reason}"
        out = {"permission": "deny", "continue": True, "userMessage": msg,
               "agentMessage": msg + ". Do not try to work around this; report it to the user in your result."}
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
