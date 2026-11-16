"""Desktop tool robustness: argument aliases, custom-tool event normalization (live log 11:31)."""

from jarvis.backend.sdk_backend import SdkBackend
from jarvis.desktop.tools import normalize_args
from jarvis.models import AgentEventKind, ToolCategory


def test_arg_aliases():
    assert normalize_args({"title_pattern": "Cursor"})["window"] == "Cursor"
    assert normalize_args({"element_id": "[e12]"})["element"] == "e12"
    assert normalize_args({"message": "hi"})["text"] == "hi"
    assert normalize_args({"hotkey": "ctrl+enter"})["keys"] == "ctrl+enter"
    assert normalize_args({"window": "Teams", "title": "x"})["window"] == "Teams"   # canonical wins


def test_custom_tool_event_is_named_and_categorised():
    from cursor_sdk.types import SDKToolUseMessage
    from cursor_sdk import RunStreamEvent

    msg = SDKToolUseMessage(type="tool_call", agent_id="a", run_id="r", call_id="c1", name="mcp", status="running",
                            args={"args": {"element": "e79", "text": "hi"}, "providerIdentifier": "custom-user-tools",
                                  "toolName": "desktop_type"})
    ev = SdkBackend._normalize(RunStreamEvent(kind="sdk_message", sdk_message=msg))[0]
    assert ev.kind == AgentEventKind.TOOL_STARTED
    assert ev.tool == "MCP:desktop_type" and ev.category == ToolCategory.MCP
    assert ev.args == {"element": "e79", "text": "hi"}
    assert "e79" in ev.summary() and "hi" in ev.summary()


def test_observer_policy_allows_named_desktop_tool():
    from jarvis.safety import policy

    ctx = policy.PolicyContext(mode="direct", autonomy="full", allowed_mcp_tools=["desktop_type"])
    assert policy.check_tool("MCP:desktop_type", {"text": "hi"}, ctx, ctx.allowed_mcp_tools).allowed
    assert not policy.check_tool("MCP:evil_tool", {}, ctx, ctx.allowed_mcp_tools).allowed
