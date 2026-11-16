"""Deterministic fast-path actions (plan 5.1). No AI involved; target < 1.5 s."""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import time
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from jarvis import paths

log = logging.getLogger(__name__)

DETACHED = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
NO_WINDOW = 0x08000000


@dataclass
class ActionResult:
    ok: bool
    spoken: str
    details: str = ""
    files: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- COM helpers
class _Com:
    def __enter__(self):
        try:
            import comtypes

            comtypes.CoInitialize()
            self.ok = True
        except Exception:
            self.ok = False
        return self

    def __exit__(self, *exc):
        if self.ok:
            try:
                import comtypes

                comtypes.CoUninitialize()
            except Exception:
                pass


def _endpoint_volume():
    from pycaw.pycaw import AudioUtilities

    speakers = AudioUtilities.GetSpeakers()
    ev = getattr(speakers, "EndpointVolume", None)
    if ev is not None:
        return ev
    from comtypes import CLSCTX_ALL
    from ctypes import POINTER, cast
    from pycaw.pycaw import IAudioEndpointVolume

    iface = speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return cast(iface, POINTER(IAudioEndpointVolume))


def get_volume() -> int:
    with _Com():
        return round(_endpoint_volume().GetMasterVolumeLevelScalar() * 100)


def set_volume(level: int) -> ActionResult:
    level = max(0, min(100, int(level)))
    with _Com():
        ev = _endpoint_volume()
        ev.SetMute(0, None)
        ev.SetMasterVolumeLevelScalar(level / 100.0, None)
        actual = round(ev.GetMasterVolumeLevelScalar() * 100)
    return ActionResult(True, f"Done, volume {actual} percent.", data={"level": actual})


def change_volume(delta: int) -> ActionResult:
    return set_volume(get_volume() + delta)


def set_mute(mute: bool) -> ActionResult:
    with _Com():
        _endpoint_volume().SetMute(1 if mute else 0, None)
    return ActionResult(True, "Muted." if mute else "Sound is back on.")


# --------------------------------------------------------------------------- brightness
def _ps(command: str, timeout: float = 15) -> subprocess.CompletedProcess:
    return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                          capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW,
                          encoding="utf-8", errors="replace")


def get_brightness() -> int | None:
    r = _ps("(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness -ErrorAction Stop | "
            "Select-Object -First 1).CurrentBrightness")
    try:
        return int(r.stdout.strip())
    except ValueError:
        return None


def set_brightness(level: int) -> ActionResult:
    level = max(0, min(100, int(level)))
    r = _ps("$m = Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods -ErrorAction Stop; "
            f"Invoke-CimMethod -InputObject $m -MethodName WmiSetBrightness -Arguments @{{Timeout=1; Brightness={level}}} | Out-Null")
    if r.returncode != 0:
        return ActionResult(False, "This display doesn't let me change brightness from Windows.",
                            details=r.stderr.strip()[:300])
    return ActionResult(True, f"Brightness set to {level} percent.")


def change_brightness(delta: int) -> ActionResult:
    cur = get_brightness()
    if cur is None:
        return ActionResult(False, "This display doesn't report brightness to Windows.")
    return set_brightness(cur + delta)


# --------------------------------------------------------------------------- keys / windows
KEYEVENTF_KEYUP = 0x0002
VK = {"play_pause": 0xB3, "next": 0xB0, "prev": 0xB1, "lwin": 0x5B, "left": 0x25, "right": 0x27, "up": 0x26,
      "d": 0x44}


def _press(*keys: int) -> None:
    user32 = ctypes.windll.user32
    for k in keys:
        user32.keybd_event(k, 0, 0, 0)
    for k in reversed(keys):
        user32.keybd_event(k, 0, KEYEVENTF_KEYUP, 0)


def media(action: str) -> ActionResult:
    _press(VK[action])
    return ActionResult(True, {"play_pause": "Okay.", "next": "Next track.", "prev": "Previous track."}[action])


def snap(direction: str) -> ActionResult:
    from jarvis.senses.screen import foreground_window

    fg = foreground_window()
    if fg.get("elevated"):
        return ActionResult(False, "That window is running as administrator, so I can't move it.")
    if direction == "show_desktop":
        _press(VK["lwin"], VK["d"])
        return ActionResult(True, "Showing the desktop.")
    _press(VK["lwin"], VK[direction])
    return ActionResult(True, {"left": "Snapped left.", "right": "Snapped right.", "up": "Maximized."}[direction])


def lock_screen() -> ActionResult:
    ok = bool(ctypes.windll.user32.LockWorkStation())
    return ActionResult(ok, "Locking the screen." if ok else "I couldn't lock the screen.")


# --------------------------------------------------------------------------- launching
def open_app(target: str, kind: str, name: str) -> ActionResult:
    try:
        if kind == "exe" and not target.endswith(":"):
            subprocess.Popen([target], creationflags=DETACHED, close_fds=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif kind == "appx":
            subprocess.Popen(["explorer.exe", target], creationflags=DETACHED, close_fds=True)
        else:   # shortcut, uri
            os.startfile(target)   # type: ignore[attr-defined]
    except OSError as exc:
        return ActionResult(False, f"I couldn't open {name}.", details=str(exc))
    return ActionResult(True, f"Opening {name}.", data={"target": target})


PROCESS_ALIASES = {
    "notepad": ["notepad"], "chrome": ["chrome"], "google chrome": ["chrome"], "edge": ["msedge"],
    "microsoft edge": ["msedge"], "browser": ["msedge", "chrome", "firefox"], "firefox": ["firefox"],
    "calculator": ["calculatorapp", "calculator"], "calc": ["calculatorapp", "calculator"],
    "word": ["winword"], "excel": ["excel"], "powerpoint": ["powerpnt"], "ppt": ["powerpnt"],
    "outlook": ["outlook", "olk"], "teams": ["ms-teams", "teams"], "spotify": ["spotify"],
    "vs code": ["code"], "vscode": ["code"], "visual studio code": ["code"], "cursor": ["cursor"],
    "paint": ["mspaint"], "whatsapp": ["whatsapp"], "zoom": ["zoom"], "slack": ["slack"],
    "file explorer": ["explorer"], "explorer": ["explorer"], "task manager": ["taskmgr"],
    "settings": ["systemsettings"], "photos": ["photos", "microsoft.photos"], "vlc": ["vlc"],
}
NEVER_CLOSE = {"python", "pythonw", "py", "cursor-sdk-bridge", "csrss", "winlogon", "lsass", "services",
               "svchost", "dwm", "system", "smss", "wininit", "conhost"}
SHELL_CLASSES = {"Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman", "WorkerW"}
WM_CLOSE = 0x0010


def close_app(name: str) -> ActionResult:
    """Gracefully close an app's windows (WM_CLOSE, like clicking X - unsaved-work prompts still show)."""
    import psutil
    from ctypes import wintypes

    q = name.lower().strip()
    wanted = set(PROCESS_ALIASES.get(q, [q.replace(" ", "")]))
    user32 = ctypes.windll.user32
    targets: list[tuple[int, str]] = []
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _lp):
        if not user32.IsWindowVisible(hwnd):
            return True
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        if cls.value in SHELL_CLASSES:
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        title = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, title, n + 1)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        try:
            proc = psutil.Process(pid.value).name().lower().removesuffix(".exe")
        except psutil.Error:
            return True
        if proc in NEVER_CLOSE:
            return True
        if proc in wanted or (proc == "applicationframehost" and q in title.value.lower()):
            targets.append((hwnd, title.value))
        return True

    user32.EnumWindows(proto(cb), 0)
    if not targets:
        return ActionResult(False, f"{name.capitalize()} isn't open.")
    for hwnd, _title in targets:
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    n = len(targets)
    return ActionResult(True, f"Closing {name}." if n == 1 else f"Closing {n} {name} windows.",
                        details="\n".join(t for _, t in targets))


def open_folder(folder: str, path: str | None, workspace: Path) -> ActionResult:
    known = paths.known_folders()
    if path:
        p = Path(path)
    elif folder in ("workspace", "jarvis"):
        p = workspace
    elif folder == "outputs":
        p = workspace / "outputs"
    else:
        p = known.get(folder, Path.home() / folder.capitalize())
    if not p.exists():
        return ActionResult(False, f"I can't find the {folder} folder.", details=str(p))
    os.startfile(str(p))   # type: ignore[attr-defined]
    return ActionResult(True, f"Opening {folder}.", data={"path": str(p)})


def open_website(url: str) -> ActionResult:
    if not url.startswith(("http://", "https://")):
        return ActionResult(False, "That doesn't look like a web address.")
    webbrowser.open(url, new=2)
    host = url.split("/")[2].replace("www.", "")
    return ActionResult(True, f"Opening {host}.", data={"url": url})


# --------------------------------------------------------------------------- info
def tell_time(lang: str = "en") -> ActionResult:
    now = datetime.now()
    s = now.strftime("%I:%M %p").lstrip("0")
    return ActionResult(True, f"It's {s}." if lang != "hi" else f"Abhi {s} ho rahe hain.")


def tell_date(lang: str = "en") -> ActionResult:
    now = datetime.now()
    s = now.strftime("%A, %d %B %Y")
    return ActionResult(True, f"Today is {s}." if lang != "hi" else f"Aaj {s} hai.")


def battery() -> ActionResult:
    import psutil

    b = psutil.sensors_battery()
    if b is None:
        return ActionResult(True, "This PC has no battery; it's on mains power.")
    plug = "and charging" if b.power_plugged else "on battery"
    mins = f", about {b.secsleft // 3600}h {(b.secsleft % 3600) // 60}m left" if (
        not b.power_plugged and b.secsleft and b.secsleft > 0 and b.secsleft < 10**7) else ""
    return ActionResult(True, f"Battery is at {round(b.percent)} percent {plug}{mins}.")


def cpu() -> ActionResult:
    import psutil

    load = psutil.cpu_percent(interval=0.4)
    return ActionResult(True, f"CPU usage is {round(load)} percent.")


def ram() -> ActionResult:
    import psutil

    m = psutil.virtual_memory()
    return ActionResult(True, f"RAM is {round(m.percent)} percent used, {m.available / 2**30:.1f} gigabytes free.")


def disk() -> ActionResult:
    import psutil

    parts = []
    for p in psutil.disk_partitions(all=False):
        if "fixed" not in p.opts and p.fstype == "":
            continue
        try:
            u = psutil.disk_usage(p.mountpoint)
        except OSError:
            continue
        parts.append((p.device.rstrip("\\"), u.free / 2**30, u.total / 2**30))
    if not parts:
        return ActionResult(False, "I couldn't read disk space.")
    spoken = ", ".join(f"{d} has {f:.0f} gigabytes free" for d, f, _ in parts[:3]) + "."
    details = "\n".join(f"{d}: {f:.1f} GB free of {t:.1f} GB" for d, f, t in parts)
    return ActionResult(True, spoken[0].upper() + spoken[1:], details=details)


def screenshot(workspace: Path) -> ActionResult:
    import mss
    import mss.tools

    out = workspace / "outputs" / "screenshots"
    out.mkdir(parents=True, exist_ok=True)
    path = out / time.strftime("screenshot-%Y%m%d-%H%M%S.png")
    with mss.mss() as sct:
        shot = sct.grab(sct.monitors[0])
        mss.tools.to_png(shot.rgb, shot.size, output=str(path))
    return ActionResult(True, "Screenshot saved to your Jarvis outputs folder.", details=str(path), files=[str(path)])


def read_clipboard(private_mode: bool) -> ActionResult:
    if private_mode:
        return ActionResult(False, "Private mode is on, so I won't read the clipboard.")
    from jarvis.senses.screen import foreground_is_sensitive

    if foreground_is_sensitive():
        return ActionResult(False, "A sensitive window is open, so I won't read the clipboard right now.")
    import pyperclip

    text = (pyperclip.paste() or "").strip()
    if not text:
        return ActionResult(True, "Your clipboard is empty.")
    short = text[:280] + ("..." if len(text) > 280 else "")
    return ActionResult(True, f"Your clipboard says: {short}", details=text[:5000])
