"""Conversation history, task folders, and scheduled jobs."""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout,
)

from jarvis.ui.theme import DIALOG_QSS

log = logging.getLogger("jarvis.ui")


class HistoryDialog(QDialog):
    def __init__(self, runtime) -> None:
        super().__init__(None)
        self.runtime = runtime
        self.setWindowTitle("Jarvis history")
        self.resize(520, 560)
        self.setStyleSheet(DIALOG_QSS)

        root = QVBoxLayout(self)
        root.addWidget(QLabel("Recent requests"))
        self.turns = QListWidget()
        root.addWidget(self.turns, 2)

        row = QHBoxLayout()
        undo = QPushButton("Undo last")
        undo.clicked.connect(lambda: runtime.submit("undo that"))
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        row.addWidget(undo)
        row.addWidget(refresh)
        row.addStretch(1)
        root.addLayout(row)

        root.addWidget(QLabel("Task folders"))
        self.folders = QListWidget()
        self.folders.itemDoubleClicked.connect(self._open_folder_item)
        root.addWidget(self.folders, 1)
        open_btn = QPushButton("Open folder")
        open_btn.clicked.connect(self._open_selected_folder)
        root.addWidget(open_btn)

        root.addWidget(QLabel("Scheduled"))
        self.scheduled = QListWidget()
        root.addWidget(self.scheduled, 1)
        cancel = QPushButton("Cancel selected")
        cancel.clicked.connect(self._cancel)
        root.addWidget(cancel)
        self.refresh()

    def refresh(self) -> None:
        self.turns.clear()
        self.folders.clear()
        self.scheduled.clear()
        orch = self.runtime.orch
        if orch is None:
            return
        try:
            for turn in orch.history.recent(200, within_s=365 * 24 * 3600):
                when = time.strftime("%m-%d %H:%M", time.localtime(turn["ts"]))
                spoken = (turn.get("spoken") or "").replace("\n", " ")
                item = QListWidgetItem(f"{when}  {turn.get('status') or ''}  {turn.get('request') or ''}")
                item.setToolTip(spoken[:400])
                item.setData(256, turn.get("task_id") or "")
                self.turns.addItem(item)
        except Exception:
            log.exception("history read failed")
        try:
            tasks = getattr(orch.ws, "tasks", None)
            if tasks is not None and Path(tasks).exists():
                for folder in sorted(Path(tasks).glob("*"), reverse=True)[:40]:
                    if folder.is_dir():
                        item = QListWidgetItem(folder.name)
                        item.setData(256, str(folder))
                        self.folders.addItem(item)
        except Exception:
            log.exception("task folders failed")
        try:
            for job in orch.scheduler.pending():
                when = datetime.fromtimestamp(job["due"])
                label = f"{when.strftime('%a %I:%M %p').lstrip('0')}  {job['kind']}: {job['text']}"
                item = QListWidgetItem(label)
                item.setData(256, job["id"])
                self.scheduled.addItem(item)
        except Exception:
            log.exception("schedule read failed")

    def _open_folder_item(self, item: QListWidgetItem) -> None:
        path = item.data(256)
        if path:
            os.startfile(str(path))  # noqa: S606

    def _open_selected_folder(self) -> None:
        item = self.folders.currentItem() or self.turns.currentItem()
        if item is None:
            return
        path = item.data(256) or ""
        if not path:
            return
        folder = Path(path)
        if not folder.is_dir() and self.runtime.orch is not None:
            tasks = getattr(self.runtime.orch.ws, "tasks", None)
            if tasks is not None:
                folder = Path(tasks) / path
        if folder.exists():
            os.startfile(str(folder if folder.is_dir() else folder.parent))  # noqa: S606

    def _cancel(self) -> None:
        item = self.scheduled.currentItem()
        if item is None or self.runtime.orch is None:
            return
        job_id = item.data(256)
        try:
            self.runtime.orch.scheduler.cancel(job_id)
        except Exception:
            log.exception("cancel failed")
        self.refresh()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.refresh()
