"""Cluely-style always-on-top bar. Friendly titles only; raw tool calls stay in the log."""

from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from jarvis.ui import winutil
from jarvis.ui.theme import CORNER, GLASS, OVERLAY_QSS, STATE_COLOR

log = logging.getLogger("jarvis.ui")

_COMMANDISH = re.compile(
    r"(\.ps1|\.py\b|powershell|pwsh\b|cmd\.exe|\bpython\b|get-childitem|remove-item|\$env:)", re.I)

COLLAPSED_H = 64
EXPANDED_MAX = 560
WIDTH = 440


def friendly_detail(status: str, detail: str) -> str:
    """Hide shell command lines. Failures keep the error text."""
    detail = (detail or "").strip()
    if not detail:
        return ""
    if status in ("failed", "blocked", "held"):
        return detail[:240]
    if _COMMANDISH.search(detail):
        return ""
    return detail[:180]


class Orb(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setFixedSize(16, 16)
        self._color = QColor(STATE_COLOR["idle"])
        self._phase = 0.0
        self.pulsing = False

    def set_state(self, state: str) -> None:
        self._color = QColor(STATE_COLOR.get(state, STATE_COLOR["idle"]))
        self.pulsing = state in ("listening", "thinking", "planning", "acting", "speaking")
        self.update()

    def advance(self) -> None:
        if not self.pulsing:
            return
        self._phase += 0.18
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._color)
        p.drawEllipse(1, 1, 14, 14)


class Waveform(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setFixedSize(76, 18)
        self._levels = [0.0] * 22

    def push(self, level: float) -> None:
        self._levels.pop(0)
        self._levels.append(max(0.0, min(1.0, float(level))))
        self.update()

    def clear(self) -> None:
        self._levels = [0.0] * 22
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#5EE0B5"))
        w = self.width() / len(self._levels)
        for i, level in enumerate(self._levels):
            h = max(2.0, level * self.height())
            x = i * w + 1
            p.drawRoundedRect(int(x), int((self.height() - h) / 2), max(2, int(w - 2)), int(h), 1, 1)


class CommandEdit(QLineEdit):
    def mousePressEvent(self, event) -> None:  # noqa: N802
        win = self.window()
        if hasattr(win, "allow_typing"):
            win.allow_typing()
        super().mousePressEvent(event)
        self.setFocus()

    def focusOutEvent(self, event) -> None:  # noqa: N802
        super().focusOutEvent(event)
        win = self.window()
        if hasattr(win, "release_typing"):
            win.release_typing()


class StepRow(QWidget):
    GLYPH = {"running": "●", "done": "✓", "failed": "✕", "blocked": "!", "held": "Ⅱ"}

    def __init__(self, title: str) -> None:
        super().__init__()
        self.status = "running"
        self.t0 = time.monotonic()
        self.elapsed: float | None = None
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(8)
        self._glyph = QLabel("·")
        self._glyph.setFixedWidth(18)
        self._glyph.setFont(QFont("Segoe UI", 11))
        self._title = QLabel(title)
        self._title.setWordWrap(True)
        self._detail = QLabel("")
        self._detail.setObjectName("detail")
        self._detail.setWordWrap(True)
        self._detail.hide()
        self._time = QLabel("")
        self._time.setObjectName("muted")
        text = QVBoxLayout()
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(0)
        text.addWidget(self._title)
        text.addWidget(self._detail)
        lay.addWidget(self._glyph, 0, Qt.AlignmentFlag.AlignTop)
        lay.addLayout(text, 1)
        lay.addWidget(self._time, 0, Qt.AlignmentFlag.AlignTop)

    def set_status(self, status: str, title: str, detail: str) -> None:
        self.status = status
        if status != "running" and self.elapsed is None:
            self.elapsed = time.monotonic() - self.t0
        self._glyph.setText(self.GLYPH.get(status, "·"))
        self._title.setText(title or "Working")
        shown = friendly_detail(status, detail)
        self._detail.setText(shown)
        self._detail.setVisible(bool(shown))
        self._paint_time()

    def tick(self) -> None:
        if self.status == "running":
            self._paint_time()

    def _paint_time(self) -> None:
        seconds = self.elapsed if self.elapsed is not None else time.monotonic() - self.t0
        self._time.setText(f"{seconds:.1f}s" if seconds < 60 else f"{int(seconds // 60)}m{int(seconds % 60):02d}s")


class Overlay(QWidget):
    def __init__(self, runtime, bridge, on_settings=None) -> None:
        super().__init__()
        self.runtime = runtime
        self._on_settings = on_settings
        self._expanded = False
        self._hover = False
        self._panel_h = COLLAPSED_H
        self._adjusting_focus = False
        self._drag: QPoint | None = None
        self._drag_origin: QPoint | None = None
        self._rows: dict[str, StepRow] = {}
        self._hidden: list[dict] = []
        self._show_hidden = False
        self._step_task = ""
        self._task_t0 = 0.0
        self._tasks: dict[str, dict] = {}
        self._state = "idle"
        self._nonce = ""
        self._approval_deadline = 0.0
        self._approval_total = 1.0
        self._request = ""
        self._working = False
        self._stopped = False
        self._side = False
        self._spoken = ""

        self.setObjectName("overlay")
        self.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)
        self.setWindowOpacity(1.0)
        self.setFixedWidth(WIDTH)
        self.setFixedHeight(COLLAPSED_H)
        self.setWindowTitle("Jarvis")
        font = QFont("Segoe UI", 10)
        font.setWeight(QFont.Weight.Medium)
        self.setFont(font)
        self.setStyleSheet(OVERLAY_QSS)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 14)
        root.setSpacing(10)

        header = QHBoxLayout()
        header.setSpacing(10)
        self._orb = Orb()
        self._hint = QLabel("Say \u201cHey Jarvis\u201d\u2026")
        self._hint.setObjectName("hint")
        self._hint.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._wave = Waveform()
        self._wave.hide()
        self._timer_lbl = QLabel("")
        self._timer_lbl.setObjectName("muted")
        icon_font = QFont("Segoe MDL2 Assets", 12)
        self._mic = QPushButton("\uE720")
        self._mic.setFont(icon_font)
        self._mic.setObjectName("icon")
        self._mic.setFixedSize(30, 30)
        self._mic.setToolTip("Talk")
        self._gear = QPushButton("\uE713")
        self._gear.setFont(icon_font)
        self._gear.setObjectName("icon")
        self._gear.setFixedSize(30, 30)
        self._gear.setToolTip("Settings")
        header.addWidget(self._orb, 0, Qt.AlignmentFlag.AlignVCenter)
        header.addWidget(self._hint, 1, Qt.AlignmentFlag.AlignVCenter)
        header.addWidget(self._wave, 0, Qt.AlignmentFlag.AlignVCenter)
        header.addWidget(self._timer_lbl, 0, Qt.AlignmentFlag.AlignVCenter)
        header.addWidget(self._mic, 0, Qt.AlignmentFlag.AlignVCenter)
        header.addWidget(self._gear, 0, Qt.AlignmentFlag.AlignVCenter)
        root.addLayout(header)
        self._header_bottom = 40

        self._body = QWidget()
        self._body.setObjectName("body")
        body = QVBoxLayout(self._body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(8)

        self._you = QLabel("")
        self._you.setObjectName("you")
        self._you.setWordWrap(True)
        self._you.hide()
        body.addWidget(self._you)
        self._notice = QLabel("")
        self._notice.setObjectName("notice")
        self._notice.setWordWrap(True)
        self._notice.hide()
        body.addWidget(self._notice)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        self._steps_lay = QVBoxLayout(inner)
        self._steps_lay.setContentsMargins(0, 0, 0, 0)
        self._steps_lay.setSpacing(2)
        self._hidden_btn = QPushButton("0 hidden steps")
        self._hidden_btn.setObjectName("icon")
        self._hidden_btn.hide()
        self._steps_lay.addWidget(self._hidden_btn)
        self._hidden_box = QVBoxLayout()
        self._steps_lay.addLayout(self._hidden_box)
        self._hint.installEventFilter(self)
        self._scroll.setWidget(inner)
        self._scroll.setMinimumHeight(0)
        self._scroll.setMaximumHeight(260)
        self._scroll.viewport().setAutoFillBackground(False)
        body.addWidget(self._scroll, 0)

        self._result = QWidget()
        self._result.setObjectName("card")
        rl = QVBoxLayout(self._result)
        rl.setContentsMargins(10, 8, 10, 8)
        top = QHBoxLayout()
        self._badge = QLabel("")
        self._copy = QPushButton("Copy")
        self._undo = QPushButton("Undo")
        top.addWidget(self._badge)
        top.addStretch(1)
        top.addWidget(self._copy)
        top.addWidget(self._undo)
        self._spoken_lbl = QLabel("")
        self._spoken_lbl.setWordWrap(True)
        self._details_btn = QPushButton("Details")
        self._details_btn.setObjectName("icon")
        self._details = QLabel("")
        self._details.setObjectName("detail")
        self._details.setWordWrap(True)
        self._details.hide()
        self._files = QVBoxLayout()
        rl.addLayout(top)
        rl.addWidget(self._spoken_lbl)
        rl.addWidget(self._details_btn)
        rl.addWidget(self._details)
        rl.addLayout(self._files)
        self._result.hide()
        body.addWidget(self._result)

        self._approval = QWidget()
        self._approval.setObjectName("card")
        al = QVBoxLayout(self._approval)
        al.setContentsMargins(10, 8, 10, 8)
        self._approval_text = QLabel("")
        self._approval_text.setWordWrap(True)
        row = QHBoxLayout()
        self._ring = QLabel("")
        self._ring.setObjectName("muted")
        self._approve = QPushButton("Approve")
        self._approve.setObjectName("approve")
        self._deny = QPushButton("Deny")
        self._deny.setObjectName("deny")
        row.addWidget(self._ring)
        row.addStretch(1)
        row.addWidget(self._deny)
        row.addWidget(self._approve)
        al.addWidget(self._approval_text)
        al.addLayout(row)
        self._approval.hide()
        body.addWidget(self._approval)

        self._question = QWidget()
        self._question.setObjectName("card")
        ql = QVBoxLayout(self._question)
        ql.setContentsMargins(10, 8, 10, 8)
        self._q_text = QLabel("")
        self._q_text.setWordWrap(True)
        chips = QHBoxLayout()
        self._am = QPushButton("AM")
        self._pm = QPushButton("PM")
        self._q_edit = CommandEdit()
        self._q_edit.setPlaceholderText("Type an answer")
        self._q_send = QPushButton("Send")
        chips.addWidget(self._am)
        chips.addWidget(self._pm)
        chips.addWidget(self._q_edit, 1)
        chips.addWidget(self._q_send)
        ql.addWidget(self._q_text)
        ql.addLayout(chips)
        self._question.hide()
        body.addWidget(self._question)

        self._queue = QLabel("")
        self._queue.setObjectName("muted")
        self._queue.setWordWrap(True)
        self._queue.hide()
        body.addWidget(self._queue)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self._input = CommandEdit()
        self._input.setPlaceholderText("Type a request")
        self._input.setFixedHeight(36)
        self._stop = QPushButton("STOP")
        self._stop.setObjectName("stop")
        self._stop.setFixedSize(76, 36)
        bar.addWidget(self._input, 1)
        bar.addWidget(self._stop)
        body.addLayout(bar)
        root.addWidget(self._body, 1)
        self._body.hide()

        self._mic.clicked.connect(self._talk)
        self._gear.clicked.connect(self._settings)
        self._input.returnPressed.connect(self._submit_text)
        self._stop.clicked.connect(self._stop_clicked)
        self._copy.clicked.connect(self._copy_result)
        self._undo.clicked.connect(lambda: self.runtime.submit("undo that"))
        self._details_btn.clicked.connect(lambda: self._details.setVisible(not self._details.isVisible()))
        self._hidden_btn.clicked.connect(self._toggle_hidden)
        self._approve.clicked.connect(lambda: self.runtime.approve(self._nonce))
        self._deny.clicked.connect(lambda: self.runtime.deny(self._nonce))
        self._am.clicked.connect(lambda: self._answer("AM"))
        self._pm.clicked.connect(lambda: self._answer("PM"))
        self._q_send.clicked.connect(lambda: self._answer(self._q_edit.text()))
        self._q_edit.returnPressed.connect(lambda: self._answer(self._q_edit.text()))

        self._tick = QTimer(self)
        self._tick.setInterval(33)
        self._tick.timeout.connect(self._on_tick)
        self._collapse_timer = QTimer(self)
        self._collapse_timer.setSingleShot(True)
        self._collapse_timer.setInterval(8000)
        self._collapse_timer.timeout.connect(self._maybe_collapse)

        bridge.state.connect(self._on_state)
        bridge.mic_level.connect(self._on_level)
        bridge.transcript.connect(self._on_transcript)
        bridge.task.connect(self._on_task)
        bridge.step.connect(self._on_step)
        bridge.result.connect(self._on_result)
        bridge.approval_request.connect(self._on_approval)
        bridge.approval_closed.connect(self._on_approval_closed)
        bridge.question.connect(self._on_question)
        bridge.error.connect(self._on_error)
        bridge.voice_status.connect(self._on_voice_status)
        bridge.notify.connect(self._on_notify)
        bridge.show_overlay.connect(lambda _e: self.reveal())
        bridge.say.connect(self._on_say)

        self._place()
        self.setWindowOpacity(1.0)

    # ------------------------------------------------------------------ window chrome
    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 0))
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        painter.setPen(QPen(QColor(255, 255, 255, 80)))
        painter.setBrush(QColor(*GLASS))
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -2, -2), CORNER, CORNER)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.setWindowOpacity(1.0)
        self._apply_shape()
        self._apply_capture()
        self._lock_focus()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._apply_shape()

    def _place(self) -> None:
        ui = self.runtime.settings.ui
        if ui.overlay_x >= 0 and ui.overlay_y >= 0:
            self.move(ui.overlay_x, ui.overlay_y)
            self._clamp()
            return
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        self.move(geo.x() + (geo.width() - self.width()) // 2, geo.y() + 18)

    def _clamp(self) -> None:
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        x = min(max(self.x(), geo.left()), geo.right() - self.width() + 1)
        y = min(max(self.y(), geo.top()), geo.bottom() - 48)
        self.move(x, y)

    def _save_pos(self) -> None:
        self.runtime.settings.ui.overlay_x = self.x()
        self.runtime.settings.ui.overlay_y = self.y()
        try:
            self.runtime.save_settings()
        except Exception:
            log.exception("could not save overlay position")

    def _apply_capture(self) -> None:
        try:
            winutil.set_exclude_from_capture(int(self.winId()), self.runtime.settings.ui.hide_from_capture)
        except Exception:
            log.exception("display affinity failed")

    def allow_typing(self) -> None:
        self._adjusting_focus = True
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
        handle = self.windowHandle()
        if handle is not None:
            handle.setFlag(Qt.WindowType.WindowDoesNotAcceptFocus, False)
        self.activateWindow()
        self._adjusting_focus = False

    def release_typing(self) -> None:
        if self._adjusting_focus:
            return
        if self._input.hasFocus() or self._q_edit.hasFocus():
            return
        self._lock_focus()

    def _lock_focus(self) -> None:
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        handle = self.windowHandle()
        if handle is not None:
            handle.setFlag(Qt.WindowType.WindowDoesNotAcceptFocus, True)

    def eventFilter(self, obj, event):  # noqa: N802
        if obj is self._hint and isinstance(event, QEvent):
            kind = event.type()
            if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                self._drag_origin = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                return True
            if kind == QEvent.Type.MouseMove and self._drag_origin is not None and event.buttons() & Qt.MouseButton.LeftButton:
                self.move(event.globalPosition().toPoint() - self._drag_origin)
                return True
            if kind == QEvent.Type.MouseButtonRelease and self._drag_origin is not None:
                self._drag_origin = None
                self._clamp()
                self._save_pos()
                return True
        return super().eventFilter(obj, event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and event.position().y() < 48:
            self._drag_origin = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        else:
            self._drag_origin = None
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_origin is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_origin)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._drag_origin is not None:
            self._clamp()
            self._save_pos()
        self._drag_origin = None
        super().mouseReleaseEvent(event)

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self._collapse_timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        if self._expanded and not self._busy() and self._state in ("idle", "", "error"):
            self._collapse_timer.start()
        super().leaveEvent(event)

    def changeEvent(self, event) -> None:  # noqa: N802
        super().changeEvent(event)
        if event.type() == QEvent.Type.ActivationChange and not self.isActiveWindow():
            self.release_typing()

    # ------------------------------------------------------------------ expand / collapse
    def reveal(self) -> None:
        self.show()
        self.raise_()
        self.expand()

    def toggle(self) -> None:
        if self.isVisible():
            self.hide()
        else:
            self.reveal()

    def expand(self) -> None:
        self._collapse_timer.stop()
        self._body.show()
        self._scroll.setVisible(bool(self._rows) or not self._hidden_btn.isHidden())
        self._expanded = True
        self._fit_content()
        self.repaint()

    def collapse(self) -> None:
        if self._approval.isVisible() or self._question.isVisible() or self._busy():
            return
        self._expanded = False
        self._body.hide()
        self._set_height(COLLAPSED_H)
        self.repaint()

    def _fit_content(self) -> None:
        layout = self.layout()
        if layout is None:
            return
        layout.activate()
        target = max(COLLAPSED_H, min(EXPANDED_MAX, layout.sizeHint().height()))
        if abs(self._panel_h - target) <= 2:
            self._apply_shape()
            return
        self._set_height(target)

    def _set_height(self, height: int) -> None:
        self._panel_h = int(height)
        self.setFixedHeight(self._panel_h)
        self._apply_shape()
        hwnd = int(self.internalWinId() or 0)
        if hwnd:
            self.repaint()
            winutil.redraw(hwnd)

    def _apply_shape(self) -> None:
        hwnd = int(self.internalWinId() or 0)
        if not hwnd:
            return
        self.setWindowOpacity(1.0)
        winutil.enable_glass(hwnd)

    def _maybe_collapse(self) -> None:
        if self._hover or self._busy() or self._question.isVisible():
            return
        if self._state not in ("idle", "", "error"):
            return
        self.collapse()

    def _sync_timer(self) -> None:
        need = (self._orb.pulsing or self._approval.isVisible()
                or any(row.status == "running" for row in self._rows.values()))
        if need and not self._tick.isActive():
            self._tick.start()
        elif not need and self._tick.isActive():
            self._tick.stop()

    def _on_tick(self) -> None:
        self._orb.advance()
        for row in self._rows.values():
            row.tick()
        if self._task_t0 and (self._working or self._state not in ("idle", "")):
            elapsed = int(time.monotonic() - self._task_t0)
            self._timer_lbl.setText(f"{elapsed // 60}:{elapsed % 60:02d}")
        if self._approval.isVisible():
            left = max(0, int(self._approval_deadline - time.monotonic()))
            self._ring.setText(f"{left}s")

    # ------------------------------------------------------------------ bus
    def _busy(self) -> bool:
        return self._working or self._has_running() or self._approval.isVisible()

    def _has_running(self) -> bool:
        return any(row.status == "running" for row in self._rows.values())

    def _show_notice(self, text: str) -> None:
        text = (text or "").strip()
        self._notice.setText(text)
        self._notice.setVisible(bool(text))

    def _notice_is_update(self) -> bool:
        text = self._notice.text().lower()
        return text.startswith("updating") or "updating the current task" in text

    @staticmethod
    def _is_fast(event: dict) -> bool:
        return str(event.get("step_id") or "").endswith("-fast")

    def _on_state(self, event: dict) -> None:
        try:
            state = event.get("state") or "idle"
            # Speech publishes "speaking" and never publishes idle when the utterance ends,
            # so that state must not stick on the header or keep the timer running.
            if state == "speaking":
                return
            if self._stopped and state == "idle":
                self._state = "idle"
                self._working = False
                self._orb.set_state("idle")
                self._wave.hide()
                self._wave.clear()
                self._hint.setText("Stopped.")
                self._timer_lbl.setText("")
                self._stopped = False
                self._sync_timer()
                return
            if self._working and state in ("speaking", "idle"):
                # "On it." and a quick command's idle must not replace the task on screen.
                self._orb.set_state("acting")
                self._hint.setText("Applying your change\u2026" if not self._notice.isHidden() else "Working\u2026")
                self._wave.hide()
                self._sync_timer()
                return
            if state == "listening" and self._busy():
                self._state = "listening"
                self._orb.set_state("listening")
                self._hint.setText("Hearing your change\u2026")
                self._wave.show()
                self.expand()
                self._sync_timer()
                return
            self._state = state
            self._orb.set_state(state)
            if state in ("thinking", "planning", "acting"):
                self._working = True
                self._stopped = False
            if state == "listening":
                self._hint.setText("Listening\u2026")
                self._wave.show()
                self.expand()
            elif state == "idle":
                self._wave.hide()
                self._wave.clear()
                if self._result.isHidden():
                    self._hint.setText("Say \u201cHey Jarvis\u201d\u2026")
                    self._timer_lbl.setText("")
            else:
                self._wave.hide()
                self._hint.setText({
                    "thinking": "Thinking\u2026",
                    "planning": "Planning\u2026",
                    "acting": "Working\u2026",
                    "awaiting_approval": "Waiting for you",
                    "error": "Something went wrong",
                }.get(state, state))
                if state in ("thinking", "planning", "acting", "awaiting_approval"):
                    self.expand()
            self._sync_timer()
        except Exception:
            log.exception("state slot")

    def _on_level(self, event: dict) -> None:
        try:
            if self._state == "listening":
                self._wave.show()
                self._wave.push(float(event.get("level") or 0))
        except Exception:
            log.exception("mic slot")

    def _on_transcript(self, event: dict) -> None:
        try:
            text = (event.get("text") or "").strip()
            if not text:
                return
            if self._busy() or self._state in ("thinking", "planning", "acting", "speaking"):
                self._show_notice("Updating \u2014 " + text)
                self._hint.setText("Applying your change\u2026")
                self.expand()
                return
            self._request = text
            self._you.setText(text)
            self._you.show()
            self._show_notice("")
            self.expand()
        except Exception:
            log.exception("transcript slot")

    def _on_say(self, event: dict) -> None:
        try:
            text = (event.get("text") or "").strip()
            low = text.lower()
            if not text or low.rstrip(".") in ("on it", "theek hai, kar raha hoon"):
                return
            if self._busy() and (
                "updating the current task" in low or low.startswith("got it")
                or low.startswith("queued") or low.startswith("already on it")
            ):
                if not (self._notice.text().lower().startswith("updating") and "updating the current task" in low):
                    self._show_notice(text)
                self._hint.setText("Applying your change\u2026" if "updating" in low or low.startswith("got it") else "Working\u2026")
                self.expand()
        except Exception:
            log.exception("say slot")

    def _on_task(self, event: dict) -> None:
        try:
            task_id = event.get("task_id") or ""
            if task_id:
                self._tasks[task_id] = dict(event)
            if event.get("status") == "started" and not self._busy():
                self._task_t0 = time.monotonic()
                request = (event.get("request") or "").strip()
                if request:
                    self._request = request
                    self._you.setText(request)
                    self._you.show()
                self.expand()
            self._refresh_queue()
        except Exception:
            log.exception("task slot")

    def _on_step(self, event: dict) -> None:
        try:
            if self._is_fast(event):
                self._side = self._working or self._has_running()
                title = (event.get("title") or "").strip()
                if self._side and title and not self._notice_is_update():
                    self._show_notice(title)
                return
            task_id = event.get("task_id") or ""
            if task_id and task_id != self._step_task:
                self._clear_steps()
                self._step_task = task_id
                self._show_notice("")
                if not self._task_t0:
                    self._task_t0 = time.monotonic()
            self._working = True
            self._stopped = False
            if event.get("internal") and not self._show_hidden:
                self._hidden.append(dict(event))
                self._hidden_btn.setText(f"{len(self._hidden)} hidden step{'s' if len(self._hidden) != 1 else ''}")
                self._hidden_btn.show()
            else:
                self._upsert(event, self._steps_lay if not event.get("internal") else self._hidden_box)
            self.expand()
            self._sync_timer()
            bar = self._scroll.verticalScrollBar()
            QTimer.singleShot(0, lambda: bar.setValue(bar.maximum()))
        except Exception:
            log.exception("step slot")

    def _upsert(self, event: dict, layout) -> None:
        step_id = event.get("step_id") or f"step-{len(self._rows)}"
        row = self._rows.get(step_id)
        if row is None:
            row = StepRow(event.get("title") or "Working")
            self._rows[step_id] = row
            layout.addWidget(row)
        row.set_status(event.get("status") or "running", event.get("title") or "", event.get("detail") or "")

    def _clear_steps(self) -> None:
        for row in self._rows.values():
            row.setParent(None)
            row.deleteLater()
        self._rows.clear()
        self._hidden.clear()
        self._hidden_btn.hide()
        self._result.hide()
        self._working = False

    def _toggle_hidden(self) -> None:
        self._show_hidden = not self._show_hidden
        if self._show_hidden:
            for event in self._hidden:
                self._upsert(event, self._hidden_box)
            self._hidden.clear()
            self._hidden_btn.setText("Hide internal steps")
        else:
            self._hidden_btn.setText("Hidden steps collapsed")

    def _on_result(self, event: dict) -> None:
        try:
            result = event.get("result")
            if result is None:
                return
            spoken = getattr(result, "spoken", "") or ""
            details = getattr(result, "details", "") or ""
            if not spoken and not details:
                return
            side = self._side
            self._side = False
            if side and self._working:
                extra = spoken or details[:160]
                if extra and self._notice_is_update():
                    self._show_notice(self._notice.text() + "\n" + extra)
                else:
                    self._show_notice(extra)
                self.expand()
                return
            self._working = False
            if not self._notice_is_update():
                self._show_notice("")
            self._spoken = spoken
            self._spoken_lbl.setText(spoken)
            verified = getattr(result, "verified", None)
            status = getattr(result, "status", "")
            if verified is True:
                self._badge.setText("Verified")
                self._badge.setObjectName("badgeOk")
            elif verified is False or status == "unverified":
                self._badge.setText("Unverified")
                self._badge.setObjectName("badgeWarn")
            else:
                self._badge.setText(status or "")
                self._badge.setObjectName("muted")
            self._badge.style().unpolish(self._badge)
            self._badge.style().polish(self._badge)
            extra = details.strip()
            checks = getattr(result, "double_check", None) or []
            if checks:
                extra = (extra + "\n" if extra else "") + "\n".join(f"• {c}" for c in checks)
            self._details.setText(extra[:4000])
            self._details.hide()
            self._details_btn.setVisible(bool(extra))
            while self._files.count():
                item = self._files.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
            for path in getattr(result, "files_changed", None) or []:
                btn = QPushButton(f"Open folder  {Path(path).name}")
                btn.clicked.connect(lambda _=False, p=path: self._open_folder(p))
                self._files.addWidget(btn)
            self._result.show()
            self._hint.setText(spoken[:90] or "Done")
            self.expand()
            self._collapse_timer.start()
        except Exception:
            log.exception("result slot")

    def _on_approval(self, event: dict) -> None:
        try:
            self._nonce = event.get("nonce") or ""
            self._approval_text.setText(event.get("confirm_text") or "Approve this?")
            self._approval_total = float(event.get("timeout_s") or 120)
            self._approval_deadline = time.monotonic() + self._approval_total
            self._approval.show()
            self.expand()
            self._sync_timer()
            if not self.isVisible():
                self.reveal()
        except Exception:
            log.exception("approval slot")

    def _on_approval_closed(self, _event: dict) -> None:
        try:
            self._approval.hide()
            self._nonce = ""
            self._sync_timer()
            if self._state in ("idle", ""):
                self._collapse_timer.start()
        except Exception:
            log.exception("approval closed slot")

    def _on_question(self, event: dict) -> None:
        try:
            text = event.get("text") or ""
            self._q_text.setText(text)
            low = text.lower()
            show_ampm = "am" in low and "pm" in low
            self._am.setVisible(show_ampm)
            self._pm.setVisible(show_ampm)
            self._q_edit.clear()
            self._question.show()
            self.expand()
        except Exception:
            log.exception("question slot")

    def _on_error(self, event: dict) -> None:
        try:
            self._state = "error"
            self._orb.set_state("error")
            self._hint.setText((event.get("text") or "Something went wrong")[:160])
        except Exception:
            log.exception("error slot")

    def _on_voice_status(self, event: dict) -> None:
        try:
            if self._busy():
                return
            text = (event.get("text") or "").strip()
            low = text.lower()
            if low.startswith("listening") or low.startswith("push-to-talk"):
                self._hint.setText(text[:80])
        except Exception:
            log.exception("voice status slot")

    def _on_notify(self, event: dict) -> None:
        try:
            if self._busy():
                return
            text = (event.get("text") or "").strip()
            if text:
                self._show_notice(f"{event.get('title') or 'Jarvis'}: {text}"[:200])
                self.expand()
        except Exception:
            log.exception("notify slot")

    def _refresh_queue(self) -> None:
        queued = [t for t in self._tasks.values() if t.get("status") == "queued"]
        parts = []
        if queued:
            parts.append(f"{len(queued)} queued")
        try:
            pending = self.runtime.orch.scheduler.pending() if self.runtime.orch else []
        except Exception:
            pending = []
        if pending:
            from datetime import datetime
            when = datetime.fromtimestamp(pending[0]["due"])
            clock = when.strftime("%I:%M %p").lstrip("0")
            parts.append(f"next scheduled {clock}: {pending[0]['text']}")
        self._queue.setText("  ·  ".join(parts))
        self._queue.setVisible(bool(parts))

    # ------------------------------------------------------------------ actions
    def _talk(self) -> None:
        try:
            self.runtime.push_to_talk()
        except Exception:
            log.exception("push to talk")

    def _settings(self) -> None:
        if self._on_settings is not None:
            self._on_settings()

    def _submit_text(self) -> None:
        text = self._input.text().strip()
        if not text:
            return
        self._input.clear()
        if self._busy() or self._state in ("thinking", "planning", "acting", "speaking"):
            self._show_notice("Updating \u2014 " + text)
            self._hint.setText("Applying your change\u2026")
        else:
            self._request = text
            self._you.setText(text)
            self._you.show()
        self.runtime.submit(text)

    def _stop_clicked(self) -> None:
        self._stopped = True
        self._working = False
        self._side = False
        self._show_notice("")
        for row in list(self._rows.values()):
            if row.status == "running":
                row.set_status("held", row._title.text(), "Stopped")
        self._hint.setText("Stopped.")
        self.runtime.kill()

    def _answer(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        self.runtime.answer(text)
        self._question.hide()

    def _copy_result(self) -> None:
        QGuiApplication.clipboard().setText(self._spoken)

    @staticmethod
    def _open_folder(path: str) -> None:
        folder = Path(path)
        target = folder if folder.is_dir() else folder.parent
        os.startfile(str(target))  # noqa: S606 - user clicked Open folder
