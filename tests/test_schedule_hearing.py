"""Scheduling any command + sound-alike app names (live: 'antigravity' heard as 'integrity')."""

from datetime import datetime, timedelta

import pytest

from jarvis.fastpath.apps import AppEntry, AppIndex, phonetic_key
from jarvis.router.normalize import AmbiguousTime, split_schedule
from jarvis.router.router import Router

NOW = datetime(2026, 11, 16, 11, 0)


@pytest.mark.parametrize("text,cmd,due", [
    ("send a mail to mitesh about the report at 12 pm", "send a mail to mitesh about the report", NOW.replace(hour=12)),
    ("after 5 hours message nilesh on teams that the build is ready",
     "message nilesh on teams that the build is ready", NOW + timedelta(hours=5)),
    ("in 30 minutes open teams", "open teams", NOW + timedelta(minutes=30)),
    ("open teams in 2 hours", "open teams", NOW + timedelta(hours=2)),
    ("tomorrow at 9 am send the weekly report to ravi", "send the weekly report to ravi",
     (NOW + timedelta(days=1)).replace(hour=9)),
    ("send the weekly report to ravi tomorrow", "send the weekly report to ravi",
     (NOW + timedelta(days=1)).replace(hour=9)),
    ("check my downloads after 2 days", "check my downloads", NOW + timedelta(days=2)),
    ("send mail to mitesh at 10:30 am", "send mail to mitesh", (NOW + timedelta(days=1)).replace(hour=10, minute=30)),
])
def test_split_schedule(text, cmd, due):
    got = split_schedule(text, NOW)
    assert got is not None, text
    assert got[0] == cmd and abs((got[1] - due).total_seconds()) < 1, got


@pytest.mark.parametrize("text", ["open notepad", "look at the report", "what time is it", "meet at 5 pm",
                                  "send mail to mitesh about the 5 hours meeting"])
def test_no_schedule(text):
    assert split_schedule(text, NOW) is None


def test_ambiguous_time_asks():
    with pytest.raises(AmbiguousTime):
        split_schedule("send the report to ravi at 5", NOW)
    r = Router().route("send the report to ravi at 5")
    assert r.path == "clarify" and "AM or PM" in r.question and r.params["command"] == "send the report to ravi"


def test_router_schedules_and_lists():
    r = Router().route("send a mail to mitesh about the report at 12 pm")
    assert r.path == "fast" and r.intent == "schedule_task" and r.params["command"].startswith("send a mail")
    assert Router().route("what's scheduled").intent == "list_schedule"
    assert Router().route("cancel the scheduled task for mitesh").params.get("what") == "mitesh"
    assert Router().route("remind me to call mom in 10 minutes").intent == "reminder"     # unchanged


def test_phonetic_keys():
    assert phonetic_key("integrity")[:4] == phonetic_key("antigravity")[:4]
    assert phonetic_key("cursor") != phonetic_key("antigravity")


def test_sound_alike_resolves_installed_app():
    idx = AppIndex.__new__(AppIndex)
    import threading
    idx._lock = threading.Lock()
    idx.entries = [AppEntry("Antigravity", "x", "shortcut"), AppEntry("Cursor", "y", "shortcut"),
                   AppEntry("Microsoft Teams", "z", "appx")]
    e, conf = idx.resolve("integrity")
    assert e is not None and e.name == "Antigravity" and conf >= 0.82
    e, _ = idx.resolve("cursor")
    assert e.name == "Cursor"
