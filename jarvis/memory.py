"""Durable user memory: facts and aliases (plan 7.1, phase 7).

Stored in <workspace>/memory/memory.json (written ONLY by the app; the policy
hook makes the folder read-only for the agent) and mirrored to facts.md, which
the agent may read. Users can list and delete entries.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path

ALIAS_RE = re.compile(
    r"^(?:my\s+)?(?P<name>[\w .\-]{2,40}?)\s+(?:folder\s+)?(?:is|=|hai)\s+(?:at\s+|in\s+)?(?P<path>[a-zA-Z]:\\[^\n]*|~\\[^\n]*|\\\\[^\n]*)$",
    re.I)


class Memory:
    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.path = folder / "memory.json"
        self._lock = threading.Lock()
        self.data = self._load()

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {"facts": [], "aliases": {}}

    def _save(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)
        lines = ["# What Jarvis remembers about the user (managed by the app; read-only)", ""]
        for f in self.data["facts"]:
            lines.append(f"- {f['text']}")
        if self.data["aliases"]:
            lines += ["", "## Aliases (name -> path)"]
            lines += [f"- {k} -> {v}" for k, v in sorted(self.data["aliases"].items())]
        (self.folder / "facts.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def remember(self, fact: str) -> str:
        """Store a fact; 'my project folder is D:\\work' also creates an alias."""
        fact = fact.strip().rstrip(".")
        with self._lock:
            m = ALIAS_RE.match(fact)
            if m:
                name = re.sub(r"\s+folder$", "", m.group("name").strip().lower())
                self.data["aliases"][name] = m.group("path").strip().rstrip("\\") or m.group("path")
            for f in self.data["facts"]:
                if f["text"].lower() == fact.lower():
                    return f["id"]
            fid = uuid.uuid4().hex[:8]
            self.data["facts"].append({"id": fid, "text": fact, "ts": time.strftime("%Y-%m-%d %H:%M")})
            self._save()
            return fid

    def forget(self, key: str) -> int:
        key = key.lower().strip()
        with self._lock:
            before = len(self.data["facts"]) + len(self.data["aliases"])
            self.data["facts"] = [f for f in self.data["facts"] if key not in f["text"].lower() and f["id"] != key]
            self.data["aliases"] = {k: v for k, v in self.data["aliases"].items() if key not in k}
            removed = before - len(self.data["facts"]) - len(self.data["aliases"])
            if removed:
                self._save()
            return removed

    def alias_path(self, name: str) -> str | None:
        name = re.sub(r"\s+folder$", "", name.lower().strip())
        name = re.sub(r"^my\s+", "", name)
        return self.data["aliases"].get(name)

    def facts(self) -> list[dict]:
        return list(self.data["facts"])

    def relevant(self, text: str, limit: int = 8) -> list[str]:
        words = {w for w in re.findall(r"\w{3,}", text.lower())}
        scored = []
        for f in self.data["facts"]:
            overlap = len(words & set(re.findall(r"\w{3,}", f["text"].lower())))
            if overlap:
                scored.append((overlap, f["text"]))
        scored.sort(reverse=True)
        out = [t for _, t in scored[:limit]]
        for k, v in self.data["aliases"].items():
            if k in text.lower():
                out.append(f"'{k}' means {v}")
        return out
