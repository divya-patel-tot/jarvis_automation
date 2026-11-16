"""Win32 helpers for the overlay: backdrop, capture exclusion. No Qt widgets."""

from __future__ import annotations

import ctypes


def enable_backdrop(hwnd: int) -> bool:
    """Windows 11 acrylic/mica. Returns False on Windows 10, where the glass paint is the fallback."""
    try:
        DWMWA_SYSTEMBACKDROP_TYPE = 38
        DWMSBT_TRANSIENTWINDOW = 3
        value = ctypes.c_int(DWMSBT_TRANSIENTWINDOW)
        hr = ctypes.windll.dwmapi.DwmSetWindowAttribute(
            int(hwnd), DWMWA_SYSTEMBACKDROP_TYPE, ctypes.byref(value), ctypes.sizeof(value))
        return hr == 0
    except Exception:
        return False


def set_exclude_from_capture(hwnd: int, exclude: bool) -> None:
    WDA_NONE = 0x0
    WDA_EXCLUDEFROMCAPTURE = 0x11
    ctypes.windll.user32.SetWindowDisplayAffinity(
        int(hwnd), WDA_EXCLUDEFROMCAPTURE if exclude else WDA_NONE)
