"""Settings window. Every toggle is saved immediately and applied to the live runtime."""

from __future__ import annotations

import json
import logging

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFormLayout, QGroupBox, QLabel, QLineEdit,
    QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget,
)

from jarvis.ui.theme import DIALOG_QSS

log = logging.getLogger("jarvis.ui")


class _DoctorThread(QThread):
    finished_ok = Signal(list)

    def run(self) -> None:
        try:
            from jarvis.__main__ import doctor_checks

            self.finished_ok.emit(doctor_checks())
        except Exception as exc:
            log.exception("doctor failed")
            self.finished_ok.emit([("Doctor", False, str(exc))])


class SettingsDialog(QDialog):
    def __init__(self, runtime, overlay=None) -> None:
        super().__init__(None)
        self.runtime = runtime
        self._overlay = overlay
        self.setWindowTitle("Jarvis settings")
        self.resize(480, 640)
        self.setStyleSheet(DIALOG_QSS)
        self._building = True

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        host = QWidget()
        root = QVBoxLayout(host)

        switches = QGroupBox("Switches")
        form = QFormLayout(switches)
        s = runtime.settings
        self.master = self._check("Jarvis enabled", s.ui.enabled, self._master)
        self.listening = self._check("Listening", s.voice.enabled, self._listening)
        self.speak = self._check("Speak replies", s.voice.tts_enabled, self._speak)
        self.autonomy = self._check("Full autonomy", s.autonomy == "full", self._autonomy)
        self.private = self._check("Screen and clipboard reading off", s.private_mode, self._private)
        self.desktop = self._check("Desktop control", s.desktop_control_enabled, self._desktop)
        self.startup = self._check("Start with Windows", s.ui.start_with_windows, self._startup)
        self.capture = self._check("Hide from screen share", s.ui.hide_from_capture, self._capture)
        for box in (self.master, self.listening, self.speak, self.autonomy, self.private,
                    self.desktop, self.startup, self.capture):
            form.addRow(box)
        root.addWidget(switches)

        voice = QGroupBox("Voice")
        vf = QFormLayout(voice)
        self.threshold = QDoubleSpinBox()
        self.threshold.setRange(0.1, 0.95)
        self.threshold.setSingleStep(0.05)
        self.threshold.setValue(s.voice.wake_threshold)
        self.threshold.valueChanged.connect(self._voice_tuned)
        self.barge = QComboBox()
        self.barge.addItems(["voice", "wake", "off"])
        self.barge.setCurrentText(s.voice.barge_in if s.voice.barge_in in ("voice", "wake", "off") else "voice")
        self.barge.currentTextChanged.connect(self._voice_tuned)
        self.model = QComboBox()
        self.model.addItems(["base.en", "small.en", "base", "small"])
        if s.voice.whisper_model not in ("base.en", "small.en", "base", "small"):
            self.model.addItem(s.voice.whisper_model)
        self.model.setCurrentText(s.voice.whisper_model)
        self.model.currentTextChanged.connect(self._model_changed)
        self.tts_voice = QLineEdit(s.voice.tts_voice)
        self.tts_voice.setPlaceholderText("part of a SAPI voice name, blank = default")
        self.tts_voice.editingFinished.connect(self._tts_voice_changed)
        self.rate = QSpinBox()
        self.rate.setRange(-10, 10)
        self.rate.setValue(s.voice.tts_rate)
        self.rate.editingFinished.connect(self._tts_voice_changed)
        vf.addRow("Wake threshold", self.threshold)
        vf.addRow("Barge-in", self.barge)
        vf.addRow("Whisper model", self.model)
        vf.addRow("TTS voice", self.tts_voice)
        vf.addRow("TTS rate", self.rate)
        root.addWidget(voice)

        models = QGroupBox("Models")
        mf = QFormLayout(models)
        self.fast = QLineEdit(", ".join(s.models.fast))
        self.strong = QLineEdit(", ".join(s.models.strong))
        self.fast.editingFinished.connect(self._models)
        self.strong.editingFinished.connect(self._models)
        self.available = QLabel(self._available_text())
        self.available.setWordWrap(True)
        mf.addRow("Fast preference", self.fast)
        mf.addRow("Strong preference", self.strong)
        mf.addRow("Account has", self.available)
        root.addWidget(models)

        budgets = QGroupBox("Budgets")
        bf = QFormLayout(budgets)
        b = s.budgets
        self.daily_tokens = self._budget(b.daily_tokens, 1_000_000, 100_000_000)
        self.daily_runs = self._budget(b.daily_agent_runs, 1, 5000)
        self.wall = self._budget(b.task_wall_clock_s, 30, 3600)
        self.tools = self._budget(b.max_tool_calls, 5, 400)
        bf.addRow("Daily tokens", self.daily_tokens)
        bf.addRow("Daily runs", self.daily_runs)
        bf.addRow("Task seconds", self.wall)
        bf.addRow("Tool calls / task", self.tools)
        self.usage = QLabel("")
        bf.addRow("Today", self.usage)
        root.addWidget(budgets)

        secrets = QGroupBox("Accounts")
        sf = QFormLayout(secrets)
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText("paste a new Cursor key to replace the saved one")
        key_btn = QPushButton("Save key")
        key_btn.clicked.connect(self._save_key)
        self.key_state = QLabel(self._key_state())
        sf.addRow("Cursor API key", self.key)
        sf.addRow("", key_btn)
        sf.addRow("", self.key_state)
        self.mail_addr = QLineEdit()
        self.mail_name = QLineEdit()
        self.mail_host = QLineEdit("smtp.gmail.com")
        self.mail_port = QSpinBox()
        self.mail_port.setRange(1, 65535)
        self.mail_port.setValue(587)
        self.mail_pw = QLineEdit()
        self.mail_pw.setEchoMode(QLineEdit.EchoMode.Password)
        self.mail_pw.setPlaceholderText("app password, blank keeps the saved one")
        self._load_mail()
        mail_btn = QPushButton("Save mail")
        mail_btn.clicked.connect(self._save_mail)
        sf.addRow("Email", self.mail_addr)
        sf.addRow("Display name", self.mail_name)
        sf.addRow("SMTP host", self.mail_host)
        sf.addRow("SMTP port", self.mail_port)
        sf.addRow("App password", self.mail_pw)
        sf.addRow("", mail_btn)
        root.addWidget(secrets)

        doctor = QGroupBox("Doctor")
        dl = QVBoxLayout(doctor)
        self.doctor_out = QPlainTextEdit()
        self.doctor_out.setReadOnly(True)
        self.doctor_out.setFixedHeight(140)
        run = QPushButton("Run doctor")
        run.clicked.connect(self._run_doctor)
        dl.addWidget(run)
        dl.addWidget(self.doctor_out)
        root.addWidget(doctor)

        scroll.setWidget(host)
        outer = QVBoxLayout(self)
        outer.addWidget(scroll)
        self._building = False
        self._doctor: _DoctorThread | None = None
        runtime.watch_settings(self.refresh)
        self._refresh_usage()

    def _check(self, label: str, checked: bool, slot) -> QCheckBox:
        box = QCheckBox(label)
        box.setChecked(checked)
        box.toggled.connect(slot)
        return box

    def _budget(self, value: int, low: int, high: int) -> QSpinBox:
        box = QSpinBox()
        box.setRange(low, high)
        box.setValue(int(value))
        box.valueChanged.connect(self._budgets)
        return box

    def refresh(self) -> None:
        if self._building:
            return
        s = self.runtime.settings
        self._building = True
        self.master.setChecked(s.ui.enabled)
        self.listening.setChecked(s.voice.enabled)
        self.speak.setChecked(s.voice.tts_enabled)
        self.autonomy.setChecked(s.autonomy == "full")
        self.private.setChecked(s.private_mode)
        self.desktop.setChecked(s.desktop_control_enabled)
        self.startup.setChecked(s.ui.start_with_windows)
        self.capture.setChecked(s.ui.hide_from_capture)
        self._building = False

    def _guard(self, fn) -> None:
        if self._building:
            return
        try:
            fn()
        except Exception:
            log.exception("settings apply failed")

    def _master(self, on: bool) -> None:
        self._guard(lambda: self.runtime.set_enabled(on))

    def _listening(self, on: bool) -> None:
        self._guard(lambda: self.runtime.set_listening(on))

    def _speak(self, on: bool) -> None:
        self._guard(lambda: self.runtime.set_speak(on))

    def _autonomy(self, on: bool) -> None:
        self._guard(lambda: self.runtime.set_autonomy(on))

    def _private(self, on: bool) -> None:
        self._guard(lambda: self.runtime.set_private_mode(on))

    def _desktop(self, on: bool) -> None:
        self._guard(lambda: self.runtime.set_desktop_control(on))

    def _startup(self, on: bool) -> None:
        self._guard(lambda: self.runtime.set_start_with_windows(on))

    def _capture(self, on: bool) -> None:
        def apply() -> None:
            self.runtime.set_hide_from_capture(on)
            if self._overlay is not None:
                self._overlay._apply_capture()
        self._guard(apply)

    def _voice_tuned(self, *_args) -> None:
        def apply() -> None:
            voice = self.runtime.settings.voice
            voice.wake_threshold = float(self.threshold.value())
            voice.barge_in = self.barge.currentText()
            self.runtime.apply_voice_tuning()
        self._guard(apply)

    def _model_changed(self, name: str) -> None:
        def apply() -> None:
            self.runtime.settings.voice.whisper_model = name
            self.runtime.reload_whisper()
        self._guard(apply)

    def _tts_voice_changed(self) -> None:
        def apply() -> None:
            self.runtime.set_tts_voice(self.tts_voice.text().strip(), int(self.rate.value()))
        self._guard(apply)

    def _models(self) -> None:
        def apply() -> None:
            fast = [p.strip() for p in self.fast.text().split(",") if p.strip()]
            strong = [p.strip() for p in self.strong.text().split(",") if p.strip()]
            self.runtime.set_model_prefs(fast, strong)
        self._guard(apply)

    def _budgets(self) -> None:
        def apply() -> None:
            self.runtime.set_budgets(
                daily_tokens=self.daily_tokens.value(),
                daily_agent_runs=self.daily_runs.value(),
                task_wall_clock_s=self.wall.value(),
                max_tool_calls=self.tools.value(),
            )
        self._guard(apply)

    def _available_text(self) -> str:
        try:
            names = self.runtime.orch.backends.models() if self.runtime.orch else []
        except Exception:
            names = []
        return ", ".join(names) if names else "available after the agent connects"

    def _refresh_usage(self) -> None:
        try:
            snap = self.runtime.orch.ledger.snapshot() if self.runtime.orch else {}
        except Exception:
            snap = {}
        if snap:
            self.usage.setText(f"{snap.get('runs', 0)} runs, {snap.get('tokens', 0)} tokens")

    def _key_state(self) -> str:
        try:
            from jarvis.secrets import get_api_key
            return "A key is saved." if get_api_key() else "No key saved."
        except Exception:
            return ""

    def _save_key(self) -> None:
        value = self.key.text().strip()
        if not value:
            return
        try:
            from jarvis.secrets import set_api_key
            set_api_key(value)
            self.key.clear()
            self.key_state.setText("A key is saved.")
        except Exception as exc:
            log.exception("key save failed")
            self.key_state.setText(str(exc)[:160])

    def _load_mail(self) -> None:
        try:
            import keyring
            raw = keyring.get_password("jarvis-mail", "account")
            if not raw:
                return
            data = json.loads(raw)
        except Exception:
            return
        self.mail_addr.setText(data.get("address") or "")
        self.mail_name.setText(data.get("from_name") or "")
        self.mail_host.setText(data.get("smtp_host") or "smtp.gmail.com")
        self.mail_port.setValue(int(data.get("smtp_port") or 587))

    def _save_mail(self) -> None:
        try:
            import keyring
            previous = {}
            raw = keyring.get_password("jarvis-mail", "account")
            if raw:
                previous = json.loads(raw)
            password = self.mail_pw.text().replace(" ", "") or previous.get("app_password") or ""
            if not self.mail_addr.text().strip() or not password:
                return
            keyring.set_password("jarvis-mail", "account", json.dumps({
                "address": self.mail_addr.text().strip(),
                "from_name": self.mail_name.text().strip(),
                "smtp_host": self.mail_host.text().strip() or "smtp.gmail.com",
                "smtp_port": int(self.mail_port.value()),
                "app_password": password,
            }))
            self.mail_pw.clear()
        except Exception:
            log.exception("mail save failed")

    def _run_doctor(self) -> None:
        if self._doctor is not None and self._doctor.isRunning():
            return
        self.doctor_out.setPlainText("Running…")
        self._doctor = _DoctorThread(self)
        self._doctor.finished_ok.connect(self._show_doctor)
        self._doctor.start()

    def _show_doctor(self, checks: list) -> None:
        lines = [f"{'OK' if good else '!!'}  {name}  {detail}" for name, good, detail in checks]
        self.doctor_out.setPlainText("\n".join(lines))

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.available.setText(self._available_text())
        self._refresh_usage()
        self.refresh()
