"""E2E for full autonomy against the REAL Cursor agent (uses plan usage).

1. A normal action (create a file on the Desktop) runs with NO approval prompt.
2. A delete asks exactly once; after approval the file is gone (Recycle Bin).

    .venv\\Scripts\\python.exe -m tests.e2e_autonomy
"""

import threading
import time

from jarvis import config, logging_setup, paths
from jarvis.events import bus
from jarvis.models import UserRequest
from jarvis.orchestrator import Orchestrator


def main() -> None:
    logging_setup.setup("WARNING")
    s = config.load()
    assert s.autonomy == "full", "set autonomy to full in settings.json"
    orch = Orchestrator(s, ask_user=lambda q: "")
    assert orch.backends.select() is not None, orch.backends.degraded_reason
    approvals: list[str] = []
    bus.subscribe("activity", lambda e: print("   ·", e.get("text")))

    def on_approval(e):
        approvals.append(e["confirm_text"])
        print("   APPROVAL ASKED:\n" + e["confirm_text"])
        threading.Timer(0.5, orch.confirm.click, (e["nonce"], "approve")).start()
    bus.subscribe("approval_request", on_approval)

    target = paths.known_folders()["desktop"] / "jarvis_full_test.txt"
    if target.exists():
        target.unlink()
    out = {}
    t0 = time.time()
    r = orch.handle(UserRequest(f"create a file named jarvis_full_test.txt on my desktop containing hello from jarvis"))
    out["1_create_no_approval"] = dict(status=r.status, exists=target.exists(), approvals=len(approvals),
                                       spoken=r.spoken, s=round(time.time() - t0, 1))
    n = len(approvals)
    t0 = time.time()
    r = orch.handle(UserRequest("delete jarvis_full_test.txt from my desktop"))
    out["2_delete_one_approval"] = dict(status=r.status, gone=not target.exists(), approvals=len(approvals) - n,
                                        spoken=r.spoken, s=round(time.time() - t0, 1))
    orch.shutdown()
    print("\n=== FULL AUTONOMY E2E ===")
    for k, v in out.items():
        print(k, "=>", v)
    ok = (out["1_create_no_approval"]["exists"] and out["1_create_no_approval"]["approvals"] == 0
          and out["2_delete_one_approval"]["gone"] and out["2_delete_one_approval"]["approvals"] == 1)
    print("RESULT:", "PASS" if ok else "CHECK OUTPUT")


if __name__ == "__main__":
    main()
