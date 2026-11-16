"""Qt side of the event bus. Widgets only see signals, never the bus thread."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from jarvis.events import bus

TOPICS = (
    "state", "mic_level", "transcript", "task", "step", "result",
    "approval_request", "approval_closed", "say", "notify", "error",
    "voice_status", "usage", "question", "show_overlay",
)


class BusBridge(QObject):
    state = Signal(dict)
    mic_level = Signal(dict)
    transcript = Signal(dict)
    task = Signal(dict)
    step = Signal(dict)
    result = Signal(dict)
    approval_request = Signal(dict)
    approval_closed = Signal(dict)
    say = Signal(dict)
    notify = Signal(dict)
    error = Signal(dict)
    voice_status = Signal(dict)
    usage = Signal(dict)
    question = Signal(dict)
    show_overlay = Signal(dict)

    def __init__(self) -> None:
        super().__init__()
        self._unsub = []
        signals = {name: getattr(self, name) for name in TOPICS}
        for topic, signal in signals.items():
            self._unsub.append(bus.subscribe(topic, self._forward(signal)))

    @staticmethod
    def _forward(signal):
        def handler(payload: dict) -> None:
            signal.emit(dict(payload))
        return handler

    def close(self) -> None:
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()
