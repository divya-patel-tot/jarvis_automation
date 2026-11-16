"""Win32 helpers for the overlay. No Qt widgets."""

from __future__ import annotations

import ctypes
from ctypes import wintypes


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


def redraw(hwnd: int) -> None:
    """Erase and repaint the whole window. A frameless resize otherwise leaves the old frame behind."""
    RDW_INVALIDATE = 0x0001
    RDW_ERASE = 0x0004
    RDW_ALLCHILDREN = 0x0080
    RDW_UPDATENOW = 0x0100
    try:
        ctypes.windll.user32.RedrawWindow(
            int(hwnd), None, None,
            RDW_INVALIDATE | RDW_ERASE | RDW_ALLCHILDREN | RDW_UPDATENOW)
    except Exception:
        return


def set_round_corners(hwnd: int, radius_px: int) -> None:
    """Clip a frameless window to a small radius. The window stays opaque, so text stays sharp."""
    try:
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        rect = wintypes.RECT()
        if not user32.GetWindowRect(int(hwnd), ctypes.byref(rect)):
            return
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        if width < 4 or height < 4:
            return
        diameter = max(2, int(radius_px) * 2)
        region = gdi32.CreateRoundRectRgn(0, 0, width + 1, height + 1, diameter, diameter)
        if not region:
            return
        if not user32.SetWindowRgn(int(hwnd), region, True):
            gdi32.DeleteObject(region)
    except Exception:
        return


def enable_glass(hwnd: int) -> bool:
    """Frost the desktop behind the window. Does not change the window's own opacity."""
    class _AccentPolicy(ctypes.Structure):
        _fields_ = [
            ("AccentState", ctypes.c_int),
            ("AccentFlags", ctypes.c_int),
            ("GradientColor", ctypes.c_uint),
            ("AnimationId", ctypes.c_int),
        ]

    class _CompositionData(ctypes.Structure):
        _fields_ = [
            ("Attribute", ctypes.c_int),
            ("Data", ctypes.POINTER(_AccentPolicy)),
            ("SizeOfData", ctypes.c_size_t),
        ]

    # 4 is acrylic blur, 3 is the older frosted blur. Tint is ABGR of #16161E.
    WCA_ACCENT_POLICY = 19
    try:
        user32 = ctypes.windll.user32
        user32.SetWindowRgn(int(hwnd), None, True)
        set_attr = user32.SetWindowCompositionAttribute
        for state in (4, 3):
            policy = _AccentPolicy(state, 2, 0x991E1616, 0)
            data = _CompositionData(WCA_ACCENT_POLICY, ctypes.pointer(policy), ctypes.sizeof(policy))
            if set_attr(int(hwnd), ctypes.byref(data)):
                return True
        return False
    except Exception:
        return False


def set_exclude_from_capture(hwnd: int, exclude: bool) -> None:
    WDA_NONE = 0x0
    WDA_EXCLUDEFROMCAPTURE = 0x11
    ctypes.windll.user32.SetWindowDisplayAffinity(
        int(hwnd), WDA_EXCLUDEFROMCAPTURE if exclude else WDA_NONE)
