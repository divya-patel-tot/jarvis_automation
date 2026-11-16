"""Installed-app index for the fast path: Start Menu shortcuts + Store apps,
with English/Hinglish aliases and fuzzy matching. Cached in app data."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from jarvis import paths

log = logging.getLogger(__name__)

BUILTIN = {
    "notepad": "notepad.exe", "calculator": "calc.exe", "calc": "calc.exe", "paint": "mspaint.exe",
    "command prompt": "cmd.exe", "cmd": "cmd.exe", "powershell": "powershell.exe", "file explorer": "explorer.exe",
    "explorer": "explorer.exe", "task manager": "taskmgr.exe", "control panel": "control.exe",
    "snipping tool": "SnippingTool.exe", "wordpad": "write.exe", "settings": "ms-settings:",
    "clock": "ms-clock:", "alarms": "ms-clock:", "camera": "microsoft.windows.camera:", "photos": "ms-photos:",
    "store": "ms-windows-store:", "mail": "outlookmail:", "calendar": "outlookcal:",
}
ALIASES = {
    "chrome": "google chrome", "google": "google chrome", "edge": "microsoft edge", "browser": "microsoft edge",
    "word": "word", "excel": "excel", "powerpoint": "powerpoint", "ppt": "powerpoint", "outlook": "outlook",
    "vs code": "visual studio code", "vscode": "visual studio code", "code": "visual studio code",
    "teams": "microsoft teams", "whatsapp": "whatsapp", "spotify": "spotify", "zoom": "zoom",
    "cursor": "cursor", "terminal": "windows terminal", "firefox": "firefox", "slack": "slack",
}
SENSITIVE_APPS = ("keepass", "1password", "bitwarden", "lastpass", "regedit", "registry", "uninstall", "credential")


def phonetic_key(text: str) -> str:
    """Consonant skeleton for sound-alike matching (no extra dependency):
    'integrity' -> 'ntgrt', 'antigravity' -> 'ntgrft', 'cursor' -> 'krsr'."""
    import re as _re

    t = _re.sub(r"[^a-z]", "", text.lower())
    for a, b in (("ph", "f"), ("ck", "k"), ("qu", "kw"), ("x", "ks"), ("wh", "w"), ("th", "t"), ("sh", "s"),
                 ("ch", "c"), ("gh", "g"), ("dg", "j")):
        t = t.replace(a, b)
    t = t.translate(str.maketrans("cqzvd", "kksft"))          # c/q->k, z->s, v->f, d->t (voicing-insensitive)
    t = _re.sub(r"[aeiouyhw]", "", t)                           # vowels and soft letters carry little in ASR errors
    return _re.sub(r"(.)\1+", r"\1", t)                         # collapse doubles


@dataclass
class AppEntry:
    name: str
    target: str
    kind: str          # exe | shortcut | appx | uri


class AppIndex:
    def __init__(self) -> None:
        self.cache = paths.state_dir() / "app_index.json"
        self.entries: list[AppEntry] = []
        self._lock = threading.Lock()
        self._load_cache()

    def _load_cache(self) -> None:
        try:
            data = json.loads(self.cache.read_text(encoding="utf-8"))
            self.entries = [AppEntry(**e) for e in data.get("entries", [])]
        except Exception:
            self.entries = []

    def refresh(self) -> None:
        entries: list[AppEntry] = []
        dirs = [Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs",
                Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / r"Microsoft\Windows\Start Menu\Programs"]
        for d in dirs:
            if d.exists():
                for lnk in d.rglob("*.lnk"):
                    entries.append(AppEntry(lnk.stem, str(lnk), "shortcut"))
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-Command",
                                "Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress"],
                               capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace",
                               creationflags=0x08000000 if os.name == "nt" else 0)
            apps = json.loads(r.stdout or "[]")
            if isinstance(apps, dict):
                apps = [apps]
            known = {e.name.lower() for e in entries}
            for a in apps:
                if a.get("Name") and a["Name"].lower() not in known:
                    entries.append(AppEntry(a["Name"], f"shell:AppsFolder\\{a['AppID']}", "appx"))
        except Exception as exc:
            log.debug("Get-StartApps failed: %s", exc)
        entries = [e for e in entries if not any(s in e.name.lower() for s in ("uninstall", "readme", "help", "website"))]
        with self._lock:
            self.entries = entries
        try:
            self.cache.parent.mkdir(parents=True, exist_ok=True)
            self.cache.write_text(json.dumps({"ts": time.time(), "entries": [e.__dict__ for e in entries]}),
                                  encoding="utf-8")
        except OSError:
            pass

    def refresh_async(self) -> None:
        threading.Thread(target=self.refresh, name="app-index", daemon=True).start()

    def names(self, limit: int = 200) -> list[str]:
        with self._lock:
            return sorted({e.name for e in self.entries})[:limit]

    def resolve(self, spoken: str) -> tuple[AppEntry | None, float]:
        """Text match, then sound-alike match (speech recognition mishears app names:
        'integrity' -> Antigravity, 'ward' -> Word)."""
        entry, conf = self._resolve_text(spoken)
        if conf >= 0.82:
            return entry, conf
        p_entry, p_conf = self._resolve_phonetic(spoken)
        if p_entry is not None and p_conf > conf:
            return p_entry, p_conf
        return entry, conf

    def _resolve_phonetic(self, spoken: str) -> tuple[AppEntry | None, float]:
        q = phonetic_key(ALIASES.get(spoken.lower().strip(), spoken))
        if len(q) < 3:
            return None, 0.0
        try:
            from rapidfuzz import fuzz
        except ImportError:
            return None, 0.0
        with self._lock:
            entries = list(self.entries)
        best, score = None, 0.0
        for e in entries:
            for part in [e.name] + e.name.split():
                k = phonetic_key(part)
                if len(k) < 3:
                    continue
                s = fuzz.ratio(q, k) / 100.0
                if s > score:
                    best, score = e, s
        # Require a strong sound match; never resolve sensitive apps.
        if best is None or score < 0.85 or any(x in best.name.lower() for x in SENSITIVE_APPS):
            return None, 0.0
        return best, round(score * 0.97, 3)

    def _resolve_text(self, spoken: str) -> tuple[AppEntry | None, float]:
        """Return (entry, confidence 0..1)."""
        q = spoken.lower().strip()
        q = ALIASES.get(q, q)
        if any(s in q for s in SENSITIVE_APPS):
            return None, 0.0
        if q in BUILTIN:
            target = BUILTIN[q]
            return AppEntry(q, target, "uri" if target.endswith(":") else "exe"), 1.0
        with self._lock:
            entries = list(self.entries)
        for e in entries:
            if e.name.lower() == q:
                return e, 1.0
        try:
            from rapidfuzz import fuzz, process

            names = [e.name for e in entries]
            best = process.extractOne(q, names, scorer=fuzz.WRatio)
            if best:
                name, score, idx = best
                # Penalise when the query is much shorter than the matched name (e.g. "go" -> "Google Earth").
                conf = score / 100.0
                if len(q) < 4 and q not in name.lower().split():
                    conf *= 0.7
                return entries[idx], conf
        except ImportError:
            for e in entries:
                if q in e.name.lower():
                    return e, 0.85
        return None, 0.0
