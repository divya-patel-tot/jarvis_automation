"""tier 0 - Extract text from a document into the task's ingested folder (untrusted, chunked).

Usage: & $env:JARVIS_PY lib\\py\\extract_text.py <file> [--out <task>\\ingested] [--max-chars 400000]
Prints one JSON object.
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.environ.get("JARVIS_HOME", str(Path(__file__).resolve().parents[3])))

from jarvis.safety import policy  # noqa: E402
from jarvis.senses import documents  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--out", default="")
    ap.add_argument("--max-chars", type=int, default=400_000)
    a = ap.parse_args()
    try:
        state = Path(os.environ.get("JARVIS_POLICY_STATE", ""))
        ctx = policy.PolicyContext.from_dict(json.loads(state.read_text(encoding="utf-8"))) if state.is_file() \
            else policy.PolicyContext(mode="plan")
        src = Path(a.file).expanduser().resolve()
        d = policy.check_read(str(src), ctx)
        if not d.allowed:
            raise PermissionError(d.reason)
        out = Path(a.out) if a.out else Path(ctx.task_folder or ".") / "ingested"
        if ctx.task_folder and not policy.is_within(str(out), ctx.task_folder):
            raise PermissionError("output must be inside the task folder")
        ex = documents.extract(src)
        manifest = documents.write_ingested(ex.text, out, src.stem, str(src), ex.kind, a.max_chars)
        print(json.dumps({"ok": True, "pages": ex.pages, **manifest}))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
