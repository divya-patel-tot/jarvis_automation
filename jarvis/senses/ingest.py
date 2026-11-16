"""Context/evidence assembly for a task (plan 4.1.4 and 8).

Decides from the request what evidence the agent needs, gathers it locally,
and writes it as labelled UNTRUSTED files under <task>/ingested/. Any
ingestion marks the task tainted (plan 10.2.7).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from jarvis.config import Settings
from jarvis.models import TaskContext
from jarvis.senses import documents, screen

log = logging.getLogger(__name__)

SCREEN_CUES = re.compile(r"\b(my screen|this screen|on (the |my )?screen|this window|current window|what am i (looking at|seeing)"
                         r"|screen (pe|par) kya|is window|this page|this article|this tab|this email|this document|"
                         r"what.?s on screen|summari[sz]e (this|that|it)\b)", re.I)
CLIPBOARD_CUES = re.compile(r"\b(clipboard|what i copied|i (just )?copied|copied text|jo copy kiya)\b", re.I)
UNTRUSTED_TASK_CUES = re.compile(r"\b(pdf|document|docx|excel|xlsx|web ?page|website|article|email|url|http|summari[sz]e|read)\b", re.I)


@dataclass
class IngestReport:
    files: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    refused: str = ""
    window: dict = field(default_factory=dict)


def gather(ctx: TaskContext, settings: Settings, fg_window: dict | None = None) -> IngestReport:
    """fg_window: the window that was foreground when the user spoke (captured before our overlay showed)."""
    rep = IngestReport()
    text = ctx.request.text
    out = ctx.folder / "ingested"
    win = fg_window or screen.foreground_window()
    rep.window = {k: win.get(k) for k in ("title", "process", "elevated")}

    wants_screen = bool(SCREEN_CUES.search(text))
    wants_clip = bool(CLIPBOARD_CUES.search(text))
    if (wants_screen or wants_clip) and settings.private_mode:
        rep.refused = "Private mode is on, so I won't read the screen or clipboard."
        return rep
    if wants_screen:
        if screen.is_sensitive(win):
            rep.refused = (f"The active window ({win.get('title', '')[:40]}) looks sensitive, so I won't capture it. "
                           "Say 'override' in settings if you really want this.")
            return rep
        if win.get("elevated"):
            rep.notes.append("Foreground window runs as administrator; its contents are not readable.")
        else:
            body = screen.uia_text(win["hwnd"]) if win.get("hwnd") else ""
            if body.strip():
                m = documents.write_ingested(f"Window title: {win.get('title')}\nProcess: {win.get('process')}\n\n{body}",
                                             out, "screen_text", f"window '{win.get('title')}'", "uia-text")
                rep.files += m["chunks"]
            if len(body.strip()) < 200:
                try:
                    png = screen.capture_window(out / "screen.png", win)
                    rep.files.append(str(png))
                    rep.notes.append("Screenshot attached because window text was sparse.")
                except Exception as exc:
                    log.warning("screenshot failed: %s", exc)
    if wants_clip:
        try:
            import pyperclip

            clip = pyperclip.paste() or ""
        except Exception:
            clip = ""
        if clip.strip():
            m = documents.write_ingested(clip, out, "clipboard", "clipboard", "clipboard-text")
            rep.files += m["chunks"]
        else:
            rep.notes.append("Clipboard is empty or not text.")
    if rep.files:
        ctx.tainted = True
        ctx.ingested = rep.files
    elif UNTRUSTED_TASK_CUES.search(text):
        # The agent will read documents/web itself via library scripts -> treat as tainted up front.
        ctx.tainted = True
    return rep
