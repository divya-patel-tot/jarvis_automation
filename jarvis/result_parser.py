"""Parse the agent's structured result block (AGENTS.md result contract)."""

from __future__ import annotations

import json
import re

from jarvis.models import TaskResult

FENCE = re.compile(r"```\s*jarvis-result\s*\n(.*?)```", re.S | re.I)
LOOSE = re.compile(r"(\{[^{}]*\"status\"\s*:\s*\"[a-z_]+\"[^{}]*(?:\[[^\]]*\][^{}]*)*\})", re.S)


def _sentences(text: str, n: int = 2) -> str:
    clean = re.sub(r"[`*_#>|]+", "", text)
    clean = re.sub(r"\s+", " ", clean).strip()
    parts = re.split(r"(?<=[.!?।])\s+", clean)
    return " ".join(parts[:n])[:300]


def parse_result(text: str) -> TaskResult:
    text = text or ""
    raw = None
    matches = FENCE.findall(text)
    if matches:
        raw = matches[-1]
    else:
        loose = LOOSE.findall(text)
        if loose:
            raw = loose[-1]
    if raw:
        try:
            d = json.loads(raw)
            body = FENCE.sub("", text).strip()
            return TaskResult(
                status=str(d.get("status") or "success"),
                spoken=str(d.get("spoken") or _sentences(body) or "Done."),
                details=str(d.get("details") or body)[:20000],
                files_changed=[str(x) for x in (d.get("files_changed") or [])][:500],
                double_check=[str(x) for x in (d.get("double_check") or [])][:50],
                question=str(d.get("question") or ""),
                injection_warning=str(d.get("injection_warning") or ""),
                verified=d["verified"] if isinstance(d.get("verified"), bool) else None,
            )
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass
    # No contract block: degrade gracefully.
    return TaskResult(status="success" if text.strip() else "failed",
                      spoken=_sentences(text) or "I finished, but got no answer text.",
                      details=text[:20000])
