"""Runtime wiring, persisted UI toggles, and the overlay event bridge."""

from __future__ import annotations

import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from jarvis import config
from jarvis.config import Settings
from jarvis.events import bus
from jarvis.models import RiskTier, Source, TaskResult
from jarvis.runtime import JarvisRuntime
from jarvis.safety.confirm import ConfirmationBroker


class FakeOrch:
    def __init__(self) -> None:
        self.confirm = ConfirmationBroker(True, "confirm")
        self.handled: list = []
        self.started = False
        self.stopped = False
        self.killed = False

    def start(self) -> None:
        self.started = True

    def shutdown(self) -> None:
        self.stopped = True

    def kill_switch(self) -> None:
        self.killed = True

    def handle(self, req, fg_window=None):
        self.handled.append(req)
        return TaskResult("success", "ok")


def test_runtime_start_submit_kill_and_answer(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_APP_DATA", str(tmp_path))
    fake = FakeOrch()
    rt = JarvisRuntime(Settings(), voice=False, console=False, orchestrator=fake)
    rt.start(hotkeys=False)
    assert fake.started

    rt.settings.ui.enabled = False
    rt.submit("open notepad", block=True)
    assert fake.handled == []

    rt.settings.ui.enabled = True
    rt.submit("mumble", Source.VOICE, 0.2, block=True)
    assert fake.handled == []

    rt.submit("open notepad", block=True)
    assert fake.handled[0].text == "open notepad"
    assert fake.handled[0].source == Source.TYPED

    asked = []
    bus.subscribe("question", asked.append)
    threading.Thread(target=lambda: (time.sleep(0.05), rt.answer("pm")), daemon=True).start()
    assert rt.ask_user("5 AM or PM?") == "pm"
    assert asked and asked[0]["text"].startswith("5")

    pending = fake.confirm.request("task", "delete a.txt", RiskTier.CONSEQUENTIAL, "origin", timeout_s=5)
    assert rt.approve(pending.nonce) is True
    assert pending.decision == "approve"
    denied = fake.confirm.request("task", "delete b.txt", RiskTier.CONSEQUENTIAL, "origin", timeout_s=5)
    assert rt.deny(denied.nonce) is True
    assert denied.decision == "deny"

    rt.kill()
    assert fake.killed
    rt.stop()
    assert fake.stopped


def test_every_toggle_persists(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_APP_DATA", str(tmp_path))
    rt = JarvisRuntime(Settings(), voice=False, console=False, orchestrator=FakeOrch())
    rt.set_enabled(False)
    rt.set_listening(False)
    rt.set_speak(False)
    rt.set_autonomy(False)
    rt.set_private_mode(True)
    rt.set_desktop_control(False)
    rt.set_hide_from_capture(True)
    rt.settings.voice.wake_threshold = 0.35
    rt.settings.voice.barge_in = "wake"
    rt.settings.voice.whisper_model = "small.en"
    rt.settings.voice.tts_rate = 2
    rt.apply_voice_tuning()
    rt.reload_whisper()
    rt.set_model_prefs(["composer-2.5"], ["default"])
    rt.set_budgets(daily_tokens=1_234_567, daily_agent_runs=12, task_wall_clock_s=90, max_tool_calls=11)
    folder = tmp_path / "startup"
    rt.set_start_with_windows(True, folder)
    assert (folder / "Jarvis.lnk").exists()
    rt.set_start_with_windows(False, folder)
    assert not (folder / "Jarvis.lnk").exists()

    again = config.load()
    assert again.ui.enabled is False
    assert again.voice.enabled is False
    assert again.voice.tts_enabled is False
    assert again.autonomy == "safe"
    assert again.private_mode is True
    assert again.desktop_control_enabled is False
    assert again.ui.hide_from_capture is True
    assert again.ui.start_with_windows is False
    assert again.voice.wake_threshold == 0.35
    assert again.voice.barge_in == "wake"
    assert again.voice.whisper_model == "small.en"
    assert again.voice.tts_rate == 2
    assert again.models.fast == ["composer-2.5"]
    assert again.models.strong == ["default"]
    assert again.budgets.daily_tokens == 1_234_567
    assert again.budgets.daily_agent_runs == 12
    assert again.budgets.task_wall_clock_s == 90
    assert again.budgets.max_tool_calls == 11


def test_settings_without_ui_block_keep_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_APP_DATA", str(tmp_path))
    path = tmp_path / "settings.json"
    path.write_text('{"autonomy": "safe", "voice": {"wake_threshold": 0.42}}', encoding="utf-8")
    loaded = config.load(path)
    assert loaded.autonomy == "safe"
    assert loaded.voice.wake_threshold == 0.42
    assert loaded.ui.enabled is True
    assert loaded.ui.hide_from_capture is False
    assert loaded.desktop_control_enabled is True


def test_bus_bridge_delivers_from_another_thread():
    from PySide6.QtWidgets import QApplication

    from jarvis.ui.bridge import BusBridge

    app = QApplication.instance() or QApplication([])
    bridge = BusBridge()
    got: list[dict] = []
    bridge.step.connect(got.append)
    try:
        threading.Thread(
            target=lambda: bus.publish("step", title="Opening Notepad", status="done", internal=False,
                                       task_id="t", step_id="s1"),
            daemon=True,
        ).start()
        deadline = time.time() + 2
        while time.time() < deadline and not got:
            app.processEvents()
            time.sleep(0.01)
        assert got and got[0]["title"] == "Opening Notepad" and got[0]["status"] == "done"
    finally:
        bridge.close()


def test_overlay_shows_titles_not_commands():
    from PySide6.QtWidgets import QApplication

    from jarvis.ui.bridge import BusBridge
    from jarvis.ui.overlay import Overlay, friendly_detail

    assert friendly_detail("done", "powershell -File lib\\ps\\launch_app.ps1 -Target notepad") == ""
    assert "could not" in friendly_detail("failed", "could not focus the window").lower()

    app = QApplication.instance() or QApplication([])
    bridge = BusBridge()
    overlay = Overlay(JarvisRuntime(Settings(), voice=False, console=False, orchestrator=FakeOrch()), bridge)
    try:
        bridge.step.emit({
            "task_id": "t1", "step_id": "open", "status": "done", "title": "Opening Notepad",
            "detail": "powershell -File lib\\ps\\launch_app.ps1 -Target notepad", "internal": False,
        })
        bridge.step.emit({
            "task_id": "t1", "step_id": "type", "status": "running", "title": "Typing \u201chello\u201d",
            "detail": "", "internal": False,
        })
        bridge.step.emit({
            "task_id": "t1", "step_id": "ctx", "status": "done", "title": "Reading context.md",
            "detail": "C:\\JarvisWorkspace\\tasks\\t1\\context.md", "internal": True,
        })
        app.processEvents()
        assert overlay._rows["open"]._title.text() == "Opening Notepad"
        assert overlay._rows["open"]._detail.text() == ""
        assert "type" in overlay._rows and "ctx" not in overlay._rows
        assert not overlay._hidden_btn.isHidden()
        assert "ps1" not in overlay._rows["open"]._title.text()

        bridge.result.emit({"result": TaskResult("success", "Wrote hello in Notepad.", verified=True,
                                                  files_changed=["C:/Users/x/Desktop/note.txt"])})
        app.processEvents()
        assert overlay._spoken_lbl.text() == "Wrote hello in Notepad."
        assert "verified" in overlay._badge.text().lower()
        assert overlay._files.count() == 1
        assert overlay.windowOpacity() == 1.0
    finally:
        bridge.close()
        overlay.close()


def test_overlay_stays_solid_and_keeps_the_task_during_an_interruption():
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from jarvis.ui.bridge import BusBridge
    from jarvis.ui.overlay import Overlay

    app = QApplication.instance() or QApplication([])
    bridge = BusBridge()
    overlay = Overlay(JarvisRuntime(Settings(), voice=False, console=False, orchestrator=FakeOrch()), bridge)
    try:
        assert overlay.windowOpacity() == 1.0
        assert overlay.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        bridge.transcript.emit({"text": "open notepad and type hello"})
        bridge.state.emit({"state": "thinking"})
        bridge.step.emit({
            "task_id": "t1", "step_id": "open", "status": "running", "title": "Opening Notepad",
            "detail": "", "internal": False,
        })
        app.processEvents()
        started = overlay._task_t0
        assert overlay._you.text() == "open notepad and type hello"
        assert "Speaking" not in overlay._hint.text()

        bridge.state.emit({"state": "speaking"})
        bridge.state.emit({"state": "idle"})
        bridge.task.emit({"task_id": "other", "status": "started", "request": "what time is it"})
        bridge.transcript.emit({"text": "actually make the text blue"})
        bridge.say.emit({"text": "Got it, updating the current task."})
        bridge.step.emit({
            "task_id": "clock", "step_id": "clock-fast", "status": "done", "title": "Checking the time",
            "detail": "Get-Date", "internal": False,
        })
        bridge.result.emit({"result": TaskResult("success", "It is 3:42.")})
        app.processEvents()

        assert overlay._task_t0 == started
        assert overlay._you.text() == "open notepad and type hello"
        assert "open" in overlay._rows
        assert "updating" in overlay._notice.text().lower()
        assert "3:42" in overlay._notice.text()
        assert overlay._spoken_lbl.text() != "It is 3:42."
        assert "Hey Jarvis" not in overlay._hint.text()
        assert "Speaking" not in overlay._hint.text()
        assert overlay.windowOpacity() == 1.0

        overlay.collapse()
        app.processEvents()
        assert overlay.windowOpacity() == 1.0
        assert "open" in overlay._rows
    finally:
        bridge.close()
        overlay.close()


def test_speaking_does_not_stick_and_the_panel_opens_in_one_step():
    from PySide6.QtWidgets import QApplication, QLineEdit, QPushButton

    from jarvis.ui.bridge import BusBridge
    from jarvis.ui.overlay import Overlay

    app = QApplication.instance() or QApplication([])
    bridge = BusBridge()
    overlay = Overlay(JarvisRuntime(Settings(), voice=False, console=False, orchestrator=FakeOrch()), bridge)
    try:
        bridge.result.emit({"result": TaskResult("success", "Opened Notepad.")})
        bridge.state.emit({"state": "idle"})
        app.processEvents()
        assert overlay._hint.text() == "Opened Notepad."
        bridge.state.emit({"state": "speaking"})
        app.processEvents()
        assert overlay._state == "idle"
        assert overlay._hint.text() == "Opened Notepad."
        overlay.expand()
        app.processEvents()
        assert overlay.height() == overlay._panel_h
        assert len(overlay.findChildren(QLineEdit)) == 2
        assert len([b for b in overlay.findChildren(QPushButton) if b.text() == "STOP"]) == 1
    finally:
        bridge.close()
        overlay.close()
