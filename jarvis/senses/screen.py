"""Screen senses: foreground window, elevation & sensitivity checks, UIA text,
window screenshots (plan 8, 9.3). Text is preferred over pixels."""

from __future__ import annotations

import ctypes
import logging
import os
import time
from ctypes import wintypes
from pathlib import Path

log = logging.getLogger(__name__)

_user32 = ctypes.WinDLL("user32", use_last_error=True) if os.name == "nt" else None
_k32 = ctypes.WinDLL("kernel32", use_last_error=True) if os.name == "nt" else None
_adv = ctypes.WinDLL("advapi32", use_last_error=True) if os.name == "nt" else None

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TokenElevation = 20

_SENSITIVE_DEFAULT = ["keepass", "1password", "bitwarden", "lastpass", "dashlane", "credential manager",
                      "inprivate", "incognito", "private browsing", "netbanking", "bank", "paypal"]
_sensitive_list = list(_SENSITIVE_DEFAULT)


def configure_sensitive(words: list[str]) -> None:
    global _sensitive_list
    _sensitive_list = [w.lower() for w in words] or list(_SENSITIVE_DEFAULT)


def _process_name(pid: int) -> str:
    try:
        import psutil

        return psutil.Process(pid).name()
    except Exception:
        return ""


def is_process_elevated(pid: int) -> bool:
    """True if elevated OR we cannot even query it (treat unknown as untouchable)."""
    if os.name != "nt":
        return False
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return True
    try:
        tok = wintypes.HANDLE()
        if not _adv.OpenProcessToken(h, TOKEN_QUERY, ctypes.byref(tok)):
            return True
        try:
            elev = wintypes.DWORD()
            size = wintypes.DWORD()
            if not _adv.GetTokenInformation(tok, TokenElevation, ctypes.byref(elev), ctypes.sizeof(elev),
                                            ctypes.byref(size)):
                return True
            return bool(elev.value)
        finally:
            _k32.CloseHandle(tok)
    finally:
        _k32.CloseHandle(h)


def self_is_elevated() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def window_info(hwnd: int) -> dict:
    if not hwnd or os.name != "nt":
        return {"hwnd": 0, "title": "", "pid": 0, "process": "", "elevated": False}
    length = _user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buf, length + 1)
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    rect = wintypes.RECT()
    _user32.GetWindowRect(hwnd, ctypes.byref(rect))
    elevated = is_process_elevated(pid.value) and not self_is_elevated()
    return {"hwnd": int(hwnd), "title": buf.value, "pid": pid.value, "process": _process_name(pid.value),
            "elevated": elevated, "rect": [rect.left, rect.top, rect.right, rect.bottom]}


def foreground_window() -> dict:
    if os.name != "nt":
        return window_info(0)
    return window_info(_user32.GetForegroundWindow())


def list_windows(limit: int = 20) -> list[dict]:
    """Visible top-level windows with a title (cheap; for 'what is open' context)."""
    if os.name != "nt":
        return []
    out: list[dict] = []
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _lp):
        if len(out) >= limit or not _user32.IsWindowVisible(hwnd):
            return True
        n = _user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        _user32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value.strip()
        if not title or title in ("Program Manager", "Settings", "Windows Input Experience"):
            return True
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        out.append({"title": title[:120], "process": _process_name(pid.value)})
        return True

    _user32.EnumWindows(proto(cb), 0)
    return out


def is_sensitive(info: dict) -> bool:
    hay = f"{info.get('title', '')} {info.get('process', '')}".lower()
    return any(w in hay for w in _sensitive_list)


def foreground_is_sensitive() -> bool:
    return is_sensitive(foreground_window())


def uia_text(hwnd: int, max_chars: int = 60_000, max_depth: int = 25, time_budget_s: float = 4.0) -> str:
    """Collect visible text from a window's UI Automation tree (depth/time limited)."""
    try:
        import uiautomation as auto
    except ImportError:
        return ""
    out: list[str] = []
    total = 0
    deadline = time.monotonic() + time_budget_s
    with auto.UIAutomationInitializerInThread():
        try:
            root = auto.ControlFromHandle(hwnd)
        except Exception:
            return ""
        if root is None:
            return ""
        seen: set[str] = set()
        for ctrl, depth in auto.WalkControl(root, includeTop=True, maxDepth=max_depth):
            if time.monotonic() > deadline or total > max_chars:
                break
            try:
                parts = [ctrl.Name or ""]
                vp = ctrl.GetPattern(auto.PatternId.ValuePattern) if ctrl.IsValuePatternAvailable() else None
                if vp is not None:
                    parts.append(vp.Value or "")
                tp = ctrl.GetPattern(auto.PatternId.TextPattern) if ctrl.IsTextPatternAvailable() else None
                if tp is not None:
                    parts.append(tp.DocumentRange.GetText(max_chars - total) or "")
            except Exception:
                continue
            for p in parts:
                p = p.strip()
                if p and p not in seen and len(p) > 1:
                    seen.add(p)
                    out.append(p)
                    total += len(p)
    return "\n".join(out)[:max_chars]


def capture_window(path: Path, info: dict | None = None, max_width: int = 1600) -> Path:
    """Screenshot of the given (or foreground) window region, downscaled."""
    import mss
    from PIL import Image

    info = info or foreground_window()
    rect = info.get("rect") or [0, 0, 0, 0]
    with mss.mss() as sct:
        if rect[2] - rect[0] > 50 and rect[3] - rect[1] > 50:
            region = {"left": max(rect[0], 0), "top": max(rect[1], 0),
                      "width": rect[2] - max(rect[0], 0), "height": rect[3] - max(rect[1], 0)}
        else:
            region = sct.monitors[1]
        shot = sct.grab(region)
    img = Image.frombytes("RGB", shot.size, shot.rgb)
    if img.width > max_width:
        img = img.resize((max_width, int(img.height * max_width / img.width)))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "PNG", optimize=True)
    return path
