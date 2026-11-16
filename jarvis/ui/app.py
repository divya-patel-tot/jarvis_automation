"""Desktop entry: one instance, tray, overlay. The agent keeps running when the bar is hidden."""

from __future__ import annotations

import ctypes
import logging
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from jarvis import config
from jarvis.events import bus
from jarvis.runtime import JarvisRuntime
from jarvis.ui.bridge import BusBridge
from jarvis.ui.history_dialog import HistoryDialog
from jarvis.ui.overlay import Overlay
from jarvis.ui.settings_dialog import SettingsDialog

log = logging.getLogger("jarvis.ui")

SERVER_NAME = "JarvisApp"
ERROR_ALREADY_EXISTS = 183


def _icon() -> QIcon:
    pix = QPixmap(32, 32)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#8EB6FF"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(4, 4, 24, 24)
    p.end()
    return QIcon(pix)


def _claim_instance(app: QApplication) -> bool:
    """Second launch asks the first overlay to show, then exits. The mutex is kept alive on the app."""
    kernel32 = ctypes.windll.kernel32
    kernel32.SetLastError(0)
    handle = kernel32.CreateMutexW(None, False, "Local\\JarvisApp")
    already = kernel32.GetLastError() == ERROR_ALREADY_EXISTS
    if already:
        sock = QLocalSocket()
        sock.connectToServer(SERVER_NAME)
        if sock.waitForConnected(800):
            sock.write(b"show")
            sock.flush()
            sock.waitForBytesWritten(800)
            sock.disconnectFromServer()
        if handle:
            kernel32.CloseHandle(handle)
        return False
    app._jarvis_mutex = handle  # type: ignore[attr-defined]
    QLocalServer.removeServer(SERVER_NAME)
    server = QLocalServer(app)
    if not server.listen(SERVER_NAME):
        log.warning("single-instance server failed: %s", server.errorString())
    app._jarvis_server = server  # type: ignore[attr-defined]
    return True


def run_ui(minimized: bool = False, voice: bool = True) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("Jarvis")
    app.setFont(QFont("Segoe UI", 10))
    if not _claim_instance(app):
        return 0

    def _excepthook(etype, value, tb):
        log.error("unhandled %s", value, exc_info=(etype, value, tb))
        try:
            bus.publish("error", text=f"{etype.__name__}: {value}"[:300])
        except Exception:
            pass
    sys.excepthook = _excepthook

    runtime = JarvisRuntime(config.load(), voice=voice, console=False)
    bridge = BusBridge()
    settings = None
    history = None

    def open_settings() -> None:
        nonlocal settings
        if settings is None:
            settings = SettingsDialog(runtime, overlay)
        settings.show()
        settings.raise_()
        settings.activateWindow()

    def open_history() -> None:
        nonlocal history
        if history is None:
            history = HistoryDialog(runtime)
        history.show()
        history.raise_()
        history.activateWindow()

    overlay = Overlay(runtime, bridge, on_settings=open_settings)
    tray = QSystemTrayIcon(_icon(), app)
    tray.setToolTip("Jarvis")

    menu = QMenu()
    show_action = menu.addAction("Show overlay")
    enable_action = menu.addAction("Jarvis enabled")
    enable_action.setCheckable(True)
    enable_action.setChecked(runtime.settings.ui.enabled)
    listen_action = menu.addAction("Pause listening")
    listen_action.setCheckable(True)
    listen_action.setChecked(not runtime.settings.voice.enabled)
    menu.addSeparator()
    stop_action = menu.addAction("Stop")
    settings_action = menu.addAction("Settings")
    history_action = menu.addAction("History")
    menu.addSeparator()
    quit_action = menu.addAction("Quit")
    tray.setContextMenu(menu)

    def sync_menu() -> None:
        enable_action.blockSignals(True)
        listen_action.blockSignals(True)
        enable_action.setChecked(runtime.settings.ui.enabled)
        listen_action.setChecked(not runtime.settings.voice.enabled)
        enable_action.blockSignals(False)
        listen_action.blockSignals(False)

    runtime.watch_settings(sync_menu)
    show_action.triggered.connect(overlay.toggle)
    enable_action.toggled.connect(runtime.set_enabled)
    listen_action.toggled.connect(lambda on: runtime.set_listening(not on))
    stop_action.triggered.connect(runtime.kill)
    settings_action.triggered.connect(open_settings)
    history_action.triggered.connect(open_history)

    def quit_app() -> None:
        try:
            runtime.kill()
            runtime.stop()
        finally:
            bridge.close()
            app.quit()
    quit_action.triggered.connect(quit_app)
    tray.activated.connect(lambda reason: overlay.toggle()
                           if reason == QSystemTrayIcon.ActivationReason.Trigger else None)

    def on_state(event: dict) -> None:
        state = event.get("state") or "idle"
        tray.setToolTip(f"Jarvis — {state}")
    bridge.state.connect(on_state)

    def on_notify(event: dict) -> None:
        tray.showMessage(event.get("title") or "Jarvis", event.get("text") or "", QSystemTrayIcon.MessageIcon.Information, 4000)
    bridge.notify.connect(on_notify)

    def on_result(event: dict) -> None:
        if overlay.isVisible():
            return
        result = event.get("result")
        spoken = getattr(result, "spoken", "") if result is not None else ""
        if spoken:
            tray.showMessage("Jarvis", spoken[:180], QSystemTrayIcon.MessageIcon.Information, 4000)
    bridge.result.connect(on_result)

    def on_approval(event: dict) -> None:
        if not overlay.isVisible():
            tray.showMessage("Jarvis needs approval", (event.get("confirm_text") or "Say confirm, or open Jarvis.")[:180],
                             QSystemTrayIcon.MessageIcon.Warning, 6000)
    bridge.approval_request.connect(on_approval)

    server = app._jarvis_server  # type: ignore[attr-defined]

    def on_second_launch() -> None:
        sock = server.nextPendingConnection()
        overlay.reveal()
        if sock is not None:
            sock.disconnectFromServer()
    server.newConnection.connect(on_second_launch)

    runtime.start()
    tray.show()
    if minimized:
        overlay.hide()
    else:
        overlay.show()
    return app.exec()
