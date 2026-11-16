"""Friendly UI steps from raw agent tool calls (raw stays in logs; UI shows these)."""

import pytest

from jarvis.events import bus
from jarvis.models import AgentEvent, AgentEventKind, ToolCategory
from jarvis.steps import describe


def ev(tool, cat, **args):
    return AgentEvent(AgentEventKind.TOOL_STARTED, tool=tool, category=cat, args=args, call_id="c1")


@pytest.mark.parametrize("event,title,internal", [
    (ev("shell", ToolCategory.SHELL, command='powershell -File "C:\\x\\lib\\ps\\launch_app.ps1" -Target notepad'),
     "Opening notepad", False),
    (ev("shell", ToolCategory.SHELL, command='& $env:JARVIS_PY lib\\py\\send_mail.py --to "a@b.com" --subject x'),
     "Sending email to a@b.com", False),
    (ev("shell", ToolCategory.SHELL, command="powershell -File lib\\ps\\recycle.ps1 -Paths C:\\x.txt"),
     "Moving to Recycle Bin", False),
    (ev("MCP:desktop_focus", ToolCategory.MCP, window="Microsoft Teams"), "Switching to Microsoft Teams", False),
    (ev("MCP:desktop_type", ToolCategory.MCP, text="are you free?", press_enter=True),
     "Typing \u201care you free?\u201d", False),
    (ev("MCP:desktop_wait_for_text", ToolCategory.MCP, text="are you free?"),
     "Verifying \u201care you free?\u201d appears", False),
    (ev("read", ToolCategory.FILE_READ, path="C:\\Users\\u\\JarvisWorkspace\\tasks\\t1\\context.md"),
     "Reading context.md", True),
    (ev("read", ToolCategory.FILE_READ, path="C:\\Users\\u\\Downloads\\report.pdf"), "Reading report.pdf", False),
    (ev("shell", ToolCategory.SHELL, command="Get-Process | Sort CPU"), "Running a command", False),
])
def test_describe(event, title, internal):
    d = describe(event)
    assert d["title"] == title and d["internal"] == internal, d


def test_observer_publishes_steps(tmp_path):
    from jarvis.audit import AuditLog
    from jarvis.config import BudgetSettings
    from jarvis.observer import Observer
    from jarvis.safety import policy

    got = []
    unsub = bus.subscribe("step", got.append)
    try:
        obs = Observer("t1", AuditLog(tmp_path / "a.jsonl"), BudgetSettings(), policy.PolicyContext(mode="direct"),
                       lambda: None, lambda: None, "direct", request_id="req1")
        e = ev("MCP:desktop_type", ToolCategory.MCP, text="hello")
        obs.on_event(e)
        done = AgentEvent(AgentEventKind.TOOL_FINISHED, tool=e.tool, category=e.category, args=e.args,
                          call_id="c1", ok=True, result={"ok": True})
        obs.on_event(done)
    finally:
        unsub()
    assert [g["status"] for g in got] == ["running", "done"]
    assert got[0]["task_id"] == "req1" and got[0]["step_id"] == got[1]["step_id"] == "c1"
    assert got[0]["title"] == "Typing \u201chello\u201d"
