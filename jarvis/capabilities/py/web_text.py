"""tier 0 - Fetch an allowlisted web page as untrusted text into the task's ingested folder.

Usage: & $env:JARVIS_PY lib\\py\\web_text.py <url> [--out <task>\\ingested]
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.environ.get("JARVIS_HOME", str(Path(__file__).resolve().parents[3])))

from jarvis.safety import policy  # noqa: E402
from jarvis.senses import documents, web  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    try:
        state = Path(os.environ.get("JARVIS_POLICY_STATE", ""))
        ctx = policy.PolicyContext.from_dict(json.loads(state.read_text(encoding="utf-8"))) if state.is_file() \
            else policy.PolicyContext(mode="plan")
        out = Path(a.out) if a.out else Path(ctx.task_folder or ".") / "ingested"
        if ctx.task_folder and not policy.is_within(str(out), ctx.task_folder):
            raise PermissionError("output must be inside the task folder")
        title, text = web.fetch_text(a.url, ctx.web_allowlist)
        manifest = documents.write_ingested(f"Title: {title}\n\n{text}", out, "web_" + title[:40], a.url, "web")
        print(json.dumps({"ok": True, "title": title, **manifest}))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
