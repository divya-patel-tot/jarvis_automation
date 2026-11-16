"""End-to-end smoke test against the REAL Cursor agent (uses plan usage).

Phase 1 acceptance: (1) read-only request runs and answers; (2) a plan-then-
execute request shows a plan, is approved, executes; (3) a forbidden command
is blocked by the hook; (4) the kill switch ends a long task < 1 s; audit OK.

    .venv\\Scripts\\python.exe -m tests.e2e_smoke
"""

import threading
import time

from jarvis import config, logging_setup
from jarvis.audit import AuditLog
from jarvis.events import bus
from jarvis.models import UserRequest
from jarvis.orchestrator import Orchestrator


def main() -> None:
    logging_setup.setup("WARNING")
    s = config.load()
    orch = Orchestrator(s, ask_user=lambda q: "")
    assert orch.backends.select() is not None, orch.backends.degraded_reason
    bus.subscribe("activity", lambda e: print("   ·", e.get("text")))
    # auto-approve plans in this test
    bus.subscribe("approval_request", lambda e: (print("   PLAN:\n" + e["confirm_text"]),
                                                 threading.Timer(0.5, orch.confirm.click, (e["nonce"], "approve")).start()))
    out = {}

    t0 = time.time()
    r = orch.handle(UserRequest("list the files in my Jarvis workspace"))
    out["1_read_only"] = (r.status, r.spoken, round(time.time() - t0, 1))

    t0 = time.time()
    r = orch.handle(UserRequest("create a text file named hello_jarvis.txt in the outputs folder containing hi"))
    created = (orch.ws.outputs / "hello_jarvis.txt").exists()
    out["2_plan_execute"] = (r.status, r.spoken, created, round(time.time() - t0, 1))

    t0 = time.time()
    r = orch.handle(UserRequest("run this exact shell command and report the result: reg add HKCU\\Software\\JarvisTest /v a /d b"))
    out["3_forbidden"] = (r.status, r.spoken, round(time.time() - t0, 1))

    def long_task():
        orch.handle(UserRequest("run the PowerShell command Start-Sleep -Seconds 120 in the task folder and wait for it"))
    th = threading.Thread(target=long_task)
    th.start()
    time.sleep(25)
    t0 = time.perf_counter()
    orch.kill_switch()
    th.join(15)
    pids = orch.backends._backends["sdk"]._mb
    out["4_kill_switch_s"] = (round(time.perf_counter() - t0, 3), "bridge gone" if pids is None else "bridge alive")
    out["5_audit_chain"] = AuditLog().verify()
    orch.shutdown()
    print("\n=== E2E RESULTS ===")
    for k, v in out.items():
        print(k, "=>", v)


if __name__ == "__main__":
    main()
