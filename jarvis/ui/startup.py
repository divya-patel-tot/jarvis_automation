"""Start-with-Windows shortcut in the current user's Startup folder."""

from __future__ import annotations

import sys
from pathlib import Path

from jarvis import paths


def startup_folder() -> Path:
    appdata = Path.home() / "AppData" / "Roaming"
    import os
    raw = os.environ.get("APPDATA")
    if raw:
        appdata = Path(raw)
    return appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def shortcut_path(folder: Path | None = None) -> Path:
    return (folder or startup_folder()) / "Jarvis.lnk"


def pythonw_executable() -> str:
    exe = Path(sys.executable)
    if exe.name.lower() == "python.exe":
        sibling = exe.with_name("pythonw.exe")
        if sibling.exists():
            return str(sibling)
    return str(exe)


def set_startup_shortcut(enabled: bool, folder: Path | None = None) -> Path:
    link = shortcut_path(folder)
    if not enabled:
        link.unlink(missing_ok=True)
        return link
    link.parent.mkdir(parents=True, exist_ok=True)
    _write_lnk(link, pythonw_executable(), "-m jarvis ui --minimized", str(paths.PROJECT_ROOT))
    if not link.exists():
        raise OSError(f"shortcut was not created: {link}")
    return link


def _write_lnk(path: Path, target: str, arguments: str, workdir: str) -> None:
    """WScript via PowerShell. comtypes returns a raw IDispatch here, with no Save()."""
    import subprocess

    def q(value: str) -> str:
        return str(value).replace("'", "''")

    script = (
        f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{q(path)}'); "
        f"$s.TargetPath = '{q(target)}'; "
        f"$s.Arguments = '{q(arguments)}'; "
        f"$s.WorkingDirectory = '{q(workdir)}'; "
        f"$s.WindowStyle = 7; "
        f"$s.Description = 'Jarvis'; "
        f"$s.Save()"
    )
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        check=True, timeout=30, creationflags=flags,
    )
