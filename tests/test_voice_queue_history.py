"""Tests for the fixes from the 2026-11-16 live voice logs: wake stripping, hallucination filter,
noisy-prefix routing, SQLite history, FIFO queue + duplicate merge."""

import threading
import time

import pytest

from jarvis.history import History
from jarvis.router.router import Router
from jarvis.voice.whisper_stt import clean_transcript, strip_wake


@pytest.mark.parametrize("heard,command", [
    ("Hey Jarvis, open notepad.", "open notepad"),
    ("Jarvis, Please Open, Notepad,", "Please Open, Notepad"),
    ("Service Open the Notepad", "Open the Notepad"),
    ("ok jarvis what time is it?", "what time is it"),
    ("Hey Jarvis.", ""),
    ("open notepad", "open notepad"),
    ("Edge, Jarvis. Can you open Teams?", "Can you open Teams"),     # live log 11:09
    ("A Jarvis, close the notepad", "close the notepad"),
    ("open the jarvis folder in documents", "open the jarvis folder in documents"),   # jarvis later = content
])
def test_strip_wake(heard, command):
    assert strip_wake(heard) == command


def test_hallucination_filter():
    assert clean_transcript("File Manager, Client Key Manager, Client Key Manager, Client Key Manager") == ""
    assert clean_transcript("Thank you.") == ""
    assert clean_transcript(" ".join(["word"] * 60)) == ""
    assert clean_transcript("Open the notepad and type hello") == "Open the notepad and type hello"


class _Apps:
    def resolve(self, name):
        from jarvis.fastpath.apps import AppEntry
        return (AppEntry("Notepad", "notepad.exe", "exe"), 1.0) if name in ("notepad",) else (None, 0.0)


@pytest.mark.parametrize("text", ["service open the notepad", "uh open notepad", "please open the notepad",
                                  "open up the notepad", "open notepad for me", "edge jarvis can you open notepad",
                                  "can you open notepad"])
def test_noisy_prefix_still_fast(text):
    r = Router(app_index=_Apps()).route(text)
    assert r.path == "fast" and r.intent == "open_app", r


def test_negation_never_dropped():
    assert Router(app_index=_Apps()).route("dont open notepad").path == "agent"


def test_history_roundtrip(tmp_path):
    h = History(tmp_path / "h.sqlite3")
    h.add("open notepad", source="voice", route="fast", intent="open_app", status="success", spoken="Opening Notepad.")
    h.add("write hello in it", source="voice", route="agent", status="success", spoken="Typed hello.",
          files=[r"C:\x.txt"])
    ctx = h.as_context()
    assert "open notepad" in ctx and "write hello in it" in ctx and r"C:\x.txt" in ctx
    assert ctx.index("open notepad") < ctx.index("write hello")      # oldest first
    h.close()
    h2 = History(tmp_path / "h.sqlite3")                              # survives restart
    assert len(h2.recent()) == 2


class _FakeOrch:
    """Just the queue part of Orchestrator._handle, driven with a slow fake agent task."""

    def __init__(self):
        from jarvis.orchestrator import Orchestrator
        self.MAX_QUEUE = Orchestrator.MAX_QUEUE
        self.busy = threading.Lock()
        self._queue_lock = threading.Lock()
        self._waiting, self._recent = [], {}
        self._cancel_queue = threading.Event()
        self.order: list[str] = []


def test_queue_fifo_and_duplicates(monkeypatch):
    from jarvis import orchestrator as orch_mod
    from jarvis.models import TaskResult, UserRequest

    o = orch_mod.Orchestrator.__new__(orch_mod.Orchestrator)
    fake = _FakeOrch()
    o.__dict__.update(fake.__dict__)
    o._tl = threading.local()
    o.current = None
    o.router = Router(app_index=_Apps())
    o.confirm = type("C", (), {"pending": lambda self: [], "voice": lambda self, *a: False})()
    o.audit = type("A", (), {"write": lambda self, *a, **k: None})()
    o._say = lambda r: r
    done: list[str] = []

    def slow_task(req, fg):
        time.sleep(0.3)
        done.append(req.text)
        return TaskResult("success", req.text)
    o._agent_task = slow_task

    results: dict[str, TaskResult] = {}

    def submit(text):
        results[text] = o._handle(UserRequest(text))

    threads = [threading.Thread(target=submit, args=(t,)) for t in
               ("summarize my downloads folder", "write a poem file on desktop")]
    for t in threads:
        t.start()
        time.sleep(0.05)
    dup = o._handle(UserRequest("summarize my downloads folder"))       # same as the running one
    for t in threads:
        t.join(5)
    assert dup.spoken == "Already on it."
    assert done == ["summarize my downloads folder", "write a poem file on desktop"]   # FIFO, nothing rejected
