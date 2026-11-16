"""Windows + Python 3.11 compatible launcher for the cursor-sdk bridge.

Why this exists (Phase 0 finding, see docs/DECISIONS.md D-002):
``cursor_sdk.Bridge.launch`` (cursor-sdk 1.3.1) reads the bridge's discovery
line with ``os.get_blocking`` + ``selectors`` on the stderr pipe. On Windows,
``os.get_blocking`` does not exist before Python 3.12 and ``select()`` only
works on sockets, so the stock launcher crashes on our target (Win10 + 3.11).

This module re-implements only the *launch* step, using a reader thread, and
then hands the SDK its own ``Bridge``/``Client`` objects, so every other SDK
code path (agents, runs, streaming, custom tools) is the official one. It also:

* keeps draining the bridge's stderr after discovery (otherwise the pipe
  buffer fills and the bridge blocks), forwarding it to our log file;
* starts the bridge without a console window and inside a kill-on-close Job
  Object so the kill switch can terminate the bridge *and every shell it
  spawned* in one call;
* passes the API key to the bridge through the environment of the bridge
  process only, never through argv.
"""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from cursor_sdk import Bridge, BridgeEndpoint, Client
from cursor_sdk._bridge import _bridge_subprocess_env, parse_discovery_line
from cursor_sdk._tool_callback import ToolCallbackServer, tool_callback_bridge_argv
from cursor_sdk._vendor import resolve_bridge_path
from cursor_sdk.errors import CursorSDKError

from jarvis.supervisor.jobobject import JobObject

log = logging.getLogger(__name__)
bridge_log = logging.getLogger("jarvis.bridge")

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200


@dataclass
class ManagedBridge:
    client: Client
    bridge: Bridge
    job: JobObject
    process: subprocess.Popen
    started_at: float

    @property
    def alive(self) -> bool:
        return self.process.poll() is None

    def kill(self) -> None:
        """Hard kill: bridge + every descendant via the job object."""
        try:
            self.job.terminate()
        finally:
            try:
                self.client._owned_bridge = None  # don't let SDK try graceful shutdown
                self.client.close()
            except Exception:
                pass
            try:
                if self.bridge._tool_callback_server is not None:
                    self.bridge._tool_callback_server.close()
            except Exception:
                pass
            self.job.close()

    def close(self) -> None:
        """Graceful close (SDK shutdown RPC), then make sure the tree is gone."""
        try:
            self.client.close()  # closes owned bridge gracefully
        except Exception as exc:
            log.debug("graceful bridge close failed: %s", exc)
        finally:
            self.job.terminate()
            self.job.close()


def _pump_stderr(proc: subprocess.Popen, found: "queue.Queue[Mapping[str, Any] | Exception]") -> None:
    reported = False
    tail: list[str] = []
    assert proc.stderr is not None
    try:
        for line in proc.stderr:
            if not reported:
                tail.append(line)
                del tail[:-40]
                try:
                    discovery = parse_discovery_line(line)
                except CursorSDKError as exc:
                    found.put(exc)
                    reported = True
                    continue
                if discovery is not None:
                    found.put(discovery)
                    reported = True
                    continue
            # never log the discovery line (it carries an auth token)
            bridge_log.debug(line.rstrip())
    except (OSError, ValueError):
        pass
    if not reported:
        found.put(CursorSDKError(
            f"Bridge exited before discovery (code {proc.poll()}): " + "".join(tail)[-2000:]))


def launch_bridge_client(
    *,
    workspace: str | os.PathLike[str],
    api_key: str,
    state_root: str | os.PathLike[str] | None = None,
    timeout: float = 60.0,
    extra_env: Mapping[str, str] | None = None,
) -> ManagedBridge:
    """Start the bundled cursor-sdk-bridge and return a connected SDK Client."""
    argv = [resolve_bridge_path(), "--workspace", os.fspath(workspace)]
    if state_root:
        Path(state_root).mkdir(parents=True, exist_ok=True)
        argv += ["--state-root", os.fspath(state_root)]
    tool_server = ToolCallbackServer()
    argv += tool_callback_bridge_argv(tool_server.endpoint)

    env = dict(_bridge_subprocess_env())
    env["CURSOR_API_KEY"] = api_key
    # Don't leak our own bridge-override vars into the child.
    for var in ("CURSOR_SDK_BRIDGE_URL", "CURSOR_SDK_BRIDGE_TOKEN", "CURSOR_SDK_BRIDGE_AUTH_TOKEN"):
        env.pop(var, None)
    if extra_env:
        env.update(extra_env)

    job = JobObject()
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=os.fspath(workspace),
        creationflags=(CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0,
    )
    job.assign(proc)
    found: "queue.Queue[Mapping[str, Any] | Exception]" = queue.Queue()
    threading.Thread(target=_pump_stderr, args=(proc, found), name="bridge-stderr", daemon=True).start()
    try:
        try:
            item = found.get(timeout=timeout)
        except queue.Empty:
            raise CursorSDKError(f"Timed out after {timeout:.0f}s waiting for bridge discovery") from None
        if isinstance(item, Exception):
            raise item
        endpoint = BridgeEndpoint.from_discovery(item)
    except Exception:
        tool_server.close()
        job.terminate()
        job.close()
        raise
    bridge = Bridge(endpoint, proc, None, tool_server)
    # Local, SDK-owned bridge: the SDK itself uses env fallback=True here
    # (run-scoped RPCs such as CancelRun are refused otherwise - Phase 0 D-004).
    os.environ["CURSOR_API_KEY"] = api_key
    client = Client(bridge.endpoint, allow_api_key_env_fallback=True)
    client._owned_bridge = bridge  # same as Client.launch_bridge does
    log.info("cursor-sdk bridge ready (pid=%s, server=%s)", proc.pid, endpoint.server_version)
    return ManagedBridge(client=client, bridge=bridge, job=job, process=proc, started_at=time.time())
