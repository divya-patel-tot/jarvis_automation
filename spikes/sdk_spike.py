"""Phase 0 spike: measure the Cursor Python SDK on this machine.

Measures: bridge start + auth, model list, agent-create latency, first event,
first text, total run time, event shapes, plan-mode behaviour (no changes),
write behaviour with and without local force, whether a workspace rule file
is honoured, and cancellation latency. Writes spikes/results/sdk_spike.json.

    .venv\\Scripts\\python.exe -m spikes.sdk_spike
"""

from __future__ import annotations

import dataclasses
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

from cursor_sdk import Agent, LocalAgentOptions, SendOptions

from jarvis.backend.sdk_bridge_win import launch_bridge_client
from jarvis.secrets import get_api_key

RESULTS = Path(__file__).parent / "results"
RESULTS.mkdir(exist_ok=True)


def _shape(obj: object, depth: int = 0) -> object:
    if depth > 6:
        return "..."
    if dataclasses.is_dataclass(obj):
        return {"__class__": type(obj).__name__, **{
            f.name: _shape(getattr(obj, f.name), depth + 1) for f in dataclasses.fields(obj)}}
    if isinstance(obj, dict):
        return {k: _shape(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_shape(v, depth + 1) for v in obj][:8]
    if isinstance(obj, str) and len(obj) > 400:
        return obj[:400] + "..."
    return obj


def timed_run(agent, prompt, opts=None):
    t0 = time.perf_counter()
    events, first_event, first_text = [], None, None
    run = agent.send(prompt, opts)
    for ev in run.events():
        now = time.perf_counter() - t0
        first_event = first_event if first_event is not None else now
        if first_text is None and getattr(ev.sdk_message, "type", "") == "assistant":
            first_text = now
        events.append({"t": round(now, 2), "event": _shape(ev)})
    res = run.wait()
    return {
        "total_s": round(time.perf_counter() - t0, 2),
        "first_event_s": round(first_event or -1, 2),
        "first_text_s": round(first_text or -1, 2),
        "status": res.status, "result": res.result[:1500],
        "usage": _shape(res.usage), "error_code": res.error_code,
        "events": events[:120],
    }


def main() -> None:
    key = get_api_key()
    if not key:
        sys.exit("no API key")
    out: dict[str, object] = {}
    ws = Path(tempfile.mkdtemp(prefix="jarvis_spike_"))
    (ws / "hello.txt").write_text("spike file", encoding="utf-8")
    (ws / "AGENTS.md").write_text(
        "# Rules\nAlways end every reply with the exact token JARVIS-RULES-OK on its own line.\n",
        encoding="utf-8")

    t0 = time.perf_counter()
    mb = launch_bridge_client(workspace=ws, api_key=key, state_root=ws / ".bridge-state")
    out["bridge_start_s"] = round(time.perf_counter() - t0, 2)
    c = mb.client
    try:
        t0 = time.perf_counter()
        me = c.me(api_key=key)
        out["auth_s"] = round(time.perf_counter() - t0, 2)
        out["key_name"] = me.api_key_name
        t0 = time.perf_counter()
        models = c.list_models(api_key=key)
        out["list_models_s"] = round(time.perf_counter() - t0, 2)
        out["models"] = [{"id": m.id, "name": m.display_name,
                          "variants": [v.display_name for v in m.variants][:6]} for m in models]

        local = LocalAgentOptions(cwd=str(ws), setting_sources=["project"])
        t0 = time.perf_counter()
        agent = Agent.create(client=c, api_key=key, local=local, name="jarvis-spike", model="default")
        out["agent_create_s"] = round(time.perf_counter() - t0, 2)

        out["run_shell_list"] = timed_run(
            agent, "List the files in the current directory using the shell, then answer in one sentence.")
        out["run_warm_same_agent"] = timed_run(agent, "Reply with the single word: ok")

        out["run_plan_mode_write"] = timed_run(
            agent, "Create a file named plan_test.txt containing 'x'.", SendOptions(mode="plan"))
        out["plan_mode_file_created"] = (ws / "plan_test.txt").exists()

        out["run_agent_write_noforce"] = timed_run(
            agent, "Create a file named noforce_test.txt containing 'x'.",
            SendOptions(mode="agent", local={"force": False}))
        out["noforce_file_created"] = (ws / "noforce_test.txt").exists()

        out["run_agent_write_force"] = timed_run(
            agent, "Create a file named force_test.txt containing 'x'.",
            SendOptions(mode="agent", local={"force": True}))
        out["force_file_created"] = (ws / "force_test.txt").exists()
        out["rules_honoured"] = "JARVIS-RULES-OK" in str(out["run_warm_same_agent"]["result"])

        # Cancellation latency on a long shell command.
        run = agent.send("Run this exact PowerShell command and wait for it: Start-Sleep -Seconds 60",
                         SendOptions(local={"force": True}))
        t_start = time.perf_counter()
        seen_tool = threading.Event()

        def consume():
            for ev in run.events():
                if getattr(ev.sdk_message, "type", "") == "tool_call":
                    seen_tool.set()
        th = threading.Thread(target=consume, daemon=True)
        th.start()
        seen_tool.wait(45)
        t0 = time.perf_counter()
        try:
            run.cancel()
        except Exception as exc:
            out["cancel_error"] = repr(exc)
        th.join(20)
        out["cancel"] = {"tool_seen_after_s": round(t0 - t_start, 2),
                         "cancel_to_stream_end_s": round(time.perf_counter() - t0, 2),
                         "status": run.status, "stream_ended": not th.is_alive()}
        agent.close()

        # Hard kill (kill switch path): job terminate time.
        t0 = time.perf_counter()
        pids_before = mb.job.process_ids()
        mb.kill()
        out["hard_kill"] = {"pids_in_job": len(pids_before), "kill_s": round(time.perf_counter() - t0, 3),
                            "bridge_alive_after": mb.alive}
    finally:
        if mb.alive:
            mb.kill()

    (RESULTS / "sdk_spike.json").write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    summary = {k: v for k, v in out.items() if not k.startswith("run_")}
    for k, v in out.items():
        if k.startswith("run_"):
            summary[k] = {x: v[x] for x in ("total_s", "first_event_s", "first_text_s", "status", "result")}
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()

