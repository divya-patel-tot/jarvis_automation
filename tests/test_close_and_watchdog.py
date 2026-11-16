"""close-app fast path + mic stall watchdog (live log 2026-11-16 11:02)."""

import threading
import time

import pytest

from jarvis.router.router import Router


@pytest.mark.parametrize("text,app", [
    ("close the notepad", "notepad"), ("ok close the notepad", "notepad"), ("close notepad", "notepad"),
    ("quit chrome", "chrome"), ("notepad band karo", "notepad"), ("close all notepad windows", "notepad"),
    ("exit excel", "excel"),
])
def test_close_app_fast(text, app):
    r = Router().route(text)
    assert r.path == "fast" and r.intent == "close_app" and r.params["app"] == app, r


@pytest.mark.parametrize("text", ["close the deal email to ravi", "close notepad and open chrome",
                                  "shut down the computer", "dont close notepad"])
def test_close_app_not_fast(text):
    assert Router().route(text).intent != "close_app"


def test_listener_watchdog_reopens_on_stall():
    from jarvis.voice.listener import AudioListener

    class W:
        ready = threading.Event()

    li = AudioListener(lambda t, c: None, W())
    li.STALL_S = 0.3
    li._oww = type("M", (), {"predict": lambda self, f: {"hey_jarvis": 0.0}, "reset": lambda self: None})()
    li._last_frame = time.monotonic() - 5           # no audio for 5 s
    t0 = time.monotonic()
    th = threading.Thread(target=li._loop, daemon=True)
    th.start()
    th.join(3)
    assert not th.is_alive(), "loop must return so the stream gets reopened"
    assert time.monotonic() - t0 < 2.5
