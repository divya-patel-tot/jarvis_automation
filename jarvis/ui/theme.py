"""Colours and stylesheet for the overlay and dialogs. Frosted glass panel, solid controls."""

from __future__ import annotations

ACCENT = "#8EB6FF"
TEXT = "#FFFFFF"
MUTED = "#D5D8E4"
GLASS = (22, 22, 30, 176)
CORNER = 12

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
QWidget#overlay, QWidget#body { background: transparent; color: #FFFFFF; border: none; }
QLabel { color: #FFFFFF; background: transparent; }
QLabel#hint { color: #FFFFFF; font-size: 13px; font-weight: 600; }
QLabel#muted, QLabel#detail { color: #D5D8E4; }
QLabel#you { color: #FFFFFF; font-weight: 600; }
QLabel#notice {
    background: #2C3348;
    color: #FFFFFF;
    border: 1px solid #8EB6FF;
    border-radius: 8px;
    padding: 6px 8px;
}
QLabel#badgeOk { color: #8DFFC0; font-weight: 700; }
QLabel#badgeWarn { color: #FFD56A; font-weight: 700; }
QWidget#card {
    background: #23232E;
    border: 1px solid #4A5168;
    border-radius: 10px;
}
QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: none; }
QLineEdit {
    background: #2A2A36;
    border: 1px solid #6A7390;
    border-radius: 10px;
    padding: 6px 10px;
    color: #FFFFFF;
    selection-background-color: #3D5A99;
}
QLineEdit:focus { border: 1px solid #8EB6FF; }
QPushButton {
    background: #2E3448;
    color: #FFFFFF;
    border: 1px solid #5C657C;
    border-radius: 10px;
    padding: 6px 12px;
}
QPushButton:hover { background: #3A4670; }
QPushButton#stop {
    background: #E85D4C;
    border: none;
    color: white;
    font-weight: 700;
}
QPushButton#stop:hover { background: #FF6E5C; }
QPushButton#approve { background: #2F7D5A; border: none; color: white; font-weight: 700; }
QPushButton#deny { background: #C44B3E; border: none; color: white; font-weight: 700; }
QPushButton#icon {
    background: #23232E;
    border: 1px solid #4A5168;
    padding: 2px;
    border-radius: 8px;
    color: #FFFFFF;
}
QPushButton#icon:hover { background: #3A4670; }
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
