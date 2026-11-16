"""Human-readable live steps for the UI ("Opening Teams", "Typing message…", "Verifying it was sent").

The observer turns every agent tool call into a `step` bus event; fast-path actions emit one step too.
Bus payloads (UI contract):
  step:  {task_id, step_id, status: running|done|failed|blocked|held, title, detail, icon, internal, ts}
  task:  {task_id, status: started|queued|finished, request, source, result_status?, spoken?, ts}
  mic_level: {level: 0..1}  (~10/s while listening)
`internal` steps (reading Jarvis's own context/rules, workspace greps) are noise for the user: show collapsed.
"""

from __future__ import annotations

import re
from pathlib import PureWindowsPath

from jarvis.models import AgentEvent, ToolCategory


def _short(text: str, n: int = 60) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def _name(path: str) -> str:
    try:
        return PureWindowsPath(path).name or path
    except Exception:
        return path


INTERNAL_PATH = re.compile(r"(\\JarvisWorkspace(\\tasks\\[^\\]+\\(context|request|plan)\.md|\\lib\\README\.md|"
                           r"\\AGENTS\.md|\\memory\\)|\\JarvisWorkspace\\?$)", re.I)


def describe(ev: AgentEvent) -> dict:
    """-> {title, detail, icon, internal} for one agent tool call."""
    a = ev.args or {}
    tool = ev.tool or ""
    low = tool.lower()
    if low.startswith("mcp:desktop_"):
        kind = low.split("desktop_", 1)[1]
        win = _short(a.get("window") or "", 40)
        text = _short(a.get("text") or "", 50)
        return {
            "windows": {"title": "Looking at open windows", "icon": "windows"},
            "focus": {"title": f"Switching to {win}" if win else "Switching window", "icon": "window"},
            "read": {"title": f"Reading {win or 'the window'}", "icon": "eye",
                     "detail": f"filter: {a['filter_text']}" if a.get("filter_text") else ""},
            "click": {"title": "Clicking", "icon": "click", "detail": str(a.get("element") or "")},
            "type": {"title": f"Typing \u201c{text}\u201d" if text else "Typing", "icon": "keyboard",
                     "detail": "then Enter" if a.get("press_enter") else ""},
            "keys": {"title": f"Pressing {a.get('keys', '')}", "icon": "keyboard"},
            "wait_for_text": {"title": f"Verifying \u201c{text}\u201d appears" if not a.get("gone")
                              else f"Verifying \u201c{text}\u201d is gone", "icon": "check"},
            "screenshot": {"title": "Taking a screenshot to look closer", "icon": "camera"},
        }.get(kind, {"title": f"Desktop: {kind}", "icon": "window"}) | {"internal": False}
    if ev.category == ToolCategory.SHELL:
        cmd = str(a.get("command") or "")
        m = re.search(r"launch_app\.ps1[\"']?\s+-Target\s+[\"']?([^\"'\-]+?)[\"']?(\s+-|$)", cmd, re.I)
        if m:
            return {"title": f"Opening {_short(m.group(1).strip(), 40)}", "icon": "app", "internal": False}
        to = re.search(r"--to\s+[\"']?([^\"'\s]+)", cmd)
        recipient = _short(to.group(1), 40) if to else "someone"
        rules = [
            (r"send_mail\.py", lambda: f"Sending email to {recipient}", "mail"),
            (r"recycle\.ps1", lambda: "Moving to Recycle Bin", "trash"),
            (r"zip_files\.ps1|compress-archive", lambda: "Zipping files", "archive"),
            (r"move_files\.ps1|move-item", lambda: "Moving files", "folder"),
            (r"copy_files\.ps1|copy-item", lambda: "Copying files", "folder"),
            (r"find_files\.ps1|get-childitem|\bdir\b|\bls\b", lambda: "Searching files", "search"),
            (r"list_windows\.ps1", lambda: "Checking open windows", "windows"),
            (r"system_info\.ps1", lambda: "Checking the system", "chip"),
            (r"extract_text\.py", lambda: "Reading the document", "doc"),
            (r"web_text\.py|invoke-webrequest|\biwr\b|\bcurl\b", lambda: "Fetching a web page", "globe"),
            (r"stop-process|taskkill", lambda: "Closing an app", "app"),
            (r"start-process|^\s*start\s", lambda: "Starting a program", "app"),
            (r"set-content|out-file|add-content", lambda: "Writing a file", "doc"),
            (r"get-content|\bcat\b|\btype\b", lambda: "Reading a file", "doc"),
            (r"start-sleep", lambda: "Waiting a moment", "clock"),
        ]
        for pat, title, icon in rules:
            if re.search(pat, cmd, re.I):
                try:
                    t = title()
                except Exception:
                    t = "Running a command"
                return {"title": t, "icon": icon, "detail": _short(cmd, 140), "internal": False}
        return {"title": "Running a command", "icon": "terminal", "detail": _short(cmd, 140), "internal": False}
    path = str(a.get("path") or a.get("file_path") or a.get("target_file") or "")
    if ev.category == ToolCategory.FILE_READ:
        internal = bool(INTERNAL_PATH.search(path)) or tool.lower() in ("glob", "grep") and "JarvisWorkspace" in path
        return {"title": f"Reading {_name(path)}" if path else "Looking through files", "icon": "doc",
                "detail": _short(path, 140), "internal": internal or not path}
    if ev.category == ToolCategory.FILE_WRITE:
        verb = "Deleting" if "delete" in low else "Writing"
        return {"title": f"{verb} {_name(path)}" if path else f"{verb} a file", "icon": "doc",
                "detail": _short(path, 140), "internal": "\\tasks\\" in path}
    if ev.category == ToolCategory.WEB_FETCH:
        url = str(a.get("url") or a.get("query") or "")
        host = re.sub(r"^https?://", "", url).split("/")[0]
        return {"title": f"Opening {host}" if host else "Searching the web", "icon": "globe", "internal": False}
    if ev.category == ToolCategory.PLAN:
        return {"title": "Planning the steps", "icon": "plan", "internal": False}
    return {"title": _short(tool.replace("MCP:", ""), 40) or "Working", "icon": "gear", "internal": True}
