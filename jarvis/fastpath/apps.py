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

    def resolve(self, spoken: str) -> tuple[AppEntry | None, float]:
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
