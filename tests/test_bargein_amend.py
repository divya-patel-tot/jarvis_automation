"""Barge-in while speaking and mid-task amendments (conversational behaviour)."""

import threading
import time

import numpy as np
import pytest

from jarvis.orchestrator import Orchestrator


@pytest.mark.parametrize("text", ["actually send it to mitesh", "wait use teams instead", "also add the deadline",
                                  "no, make it tomorrow", "send it to nilesh instead", "change the subject to hello"])
def test_amendments_detected(text):
    assert Orchestrator.AMEND.search(text)


@pytest.mark.parametrize("text", ["open notepad", "what is the weather", "summarize my downloads folder",
                                  "send an email to ravi"])
def test_new_tasks_not_amendments(text):
    assert not Orchestrator.AMEND.search(text)


def test_barge_in_by_voice_while_speaking(monkeypatch):
    from jarvis.voice import listener as L

    class W:
        ready = threading.Event()

    li = L.AudioListener(lambda t, c: None, W())
    li.barge_in = "voice"
    li._oww = type("M", (), {"predict": lambda self, f: {"hey_jarvis": 0.0}, "reset": lambda self: None})()
    li._last_frame = time.monotonic()
    captured = []
    li._capture = lambda **kw: (captured.append(kw), li._stop.set())
    monkeypatch.setattr(L, "_speaking", lambda: True)
    loud = (np.ones(L.FRAME) * 3000).astype(np.int16)            # user talking over Jarvis
    for _ in range(8):
        li._frames.put(loud)
    li._loop()
    assert captured and captured[0]["reason"] == "barge-in"


def test_no_barge_in_when_off(monkeypatch):
    from jarvis.voice import listener as L

    class W:
        ready = threading.Event()

    li = L.AudioListener(lambda t, c: None, W())
    li.barge_in = "off"
    li._oww = type("M", (), {"predict": lambda self, f: {"hey_jarvis": 0.9}, "reset": lambda self: None})()
    li._last_frame = time.monotonic()
    captured = []
    li._capture = lambda **kw: captured.append(kw)
    monkeypatch.setattr(L, "_speaking", lambda: True)
    for _ in range(5):
        li._frames.put((np.ones(L.FRAME) * 3000).astype(np.int16))
    li.STALL_S = 0.2
    t = threading.Thread(target=li._loop, daemon=True)
    t.start()
    time.sleep(0.4)
    li._last_frame = 0          # end the loop via the stall path
    t.join(2)
    assert not captured
