"""Colours and stylesheet for the overlay and dialogs. Dark glass, one accent."""

from __future__ import annotations

ACCENT = "#8EB6FF"
TEXT = "#F4F5F8"
MUTED = "#9AA0AE"
GLASS = (20, 20, 24, 230)  # #141418 at ~90%

STATE_COLOR = {
    "idle": "#8EB6FF",
    "listening": "#5EE0B5",
    "thinking": "#C9A6FF",
    "planning": "#C9A6FF",
    "acting": "#8EB6FF",
    "awaiting_approval": "#F5C16C",
    "speaking": "#7EE0FF",
    "error": "#FF6B6B",
}

OVERLAY_QSS = """
QLabel { color: #F4F5F8; background: transparent; }
QLabel#muted, QLabel#detail { color: #9AA0AE; }
QLabel#you { color: #C9D4EE; }
QLabel#badgeOk { color: #7DDEAA; font-weight: 600; }
QLabel#badgeWarn { color: #E7C37A; font-weight: 600; }
QWidget#card {
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 12px;
}
QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: none; }
QLineEdit {
    background: rgba(255, 255, 255, 0.06);
    border: 1px solid rgba(255, 255, 255, 0.10);
    border-radius: 10px;
    padding: 6px 10px;
    color: #F4F5F8;
    selection-background-color: #3D5A99;
}
QLineEdit:focus { border: 1px solid rgba(142, 182, 255, 0.85); }
QPushButton {
    background: rgba(255, 255, 255, 0.06);
    color: #F4F5F8;
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 10px;
    padding: 6px 12px;
}
QPushButton:hover { background: rgba(142, 182, 255, 0.18); }
QPushButton#stop {
    background: #E85D4C;
    border: none;
    color: white;
    font-weight: 600;
}
QPushButton#stop:hover { background: #FF6E5C; }
QPushButton#approve { background: #2F7D5A; border: none; color: white; }
QPushButton#deny { background: rgba(232, 93, 76, 0.85); border: none; color: white; }
QPushButton#icon {
    background: transparent;
    border: none;
    padding: 4px;
    border-radius: 12px;
    color: #F4F5F8;
}
QPushButton#icon:hover { background: rgba(255, 255, 255, 0.08); }
QToolButton { color: #9AA0AE; background: transparent; border: none; }
"""

DIALOG_QSS = """
QDialog, QWidget { background: #16161C; color: #F4F5F8; }
QLabel { color: #F4F5F8; }
QGroupBox {
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 10px;
    margin-top: 12px;
    padding: 10px;
    font-weight: 600;
}
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #8EB6FF; }
QCheckBox, QRadioButton { color: #F4F5F8; spacing: 8px; }
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QListWidget {
    background: #1E1E26;
    color: #F4F5F8;
    border: 1px solid rgba(255, 255, 255, 0.10);
    border-radius: 8px;
    padding: 4px 8px;
}
QPushButton {
    background: #2A3148;
    color: #F4F5F8;
    border: none;
    border-radius: 8px;
    padding: 6px 14px;
}
QPushButton:hover { background: #3A4670; }
QScrollArea { border: none; }
"""
