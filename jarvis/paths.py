"""Filesystem locations. App data (logs, audit, state) is kept OUTSIDE the agent
workspace so the agent can neither read nor tamper with it (plan 7.1, 10.2.11)."""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_ROOT = Path(__file__).resolve().parent


def _env_path(var: str, default: Path) -> Path:
    value = os.environ.get(var)
    return Path(value).expanduser() if value else default


def home() -> Path:
    return Path.home()


def app_data_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or (home() / "AppData" / "Local"))
    return _env_path("JARVIS_APP_DATA", base / "Jarvis")


def default_workspace_dir() -> Path:
    return _env_path("JARVIS_WORKSPACE", home() / "JarvisWorkspace")


def logs_dir() -> Path:
    return app_data_dir() / "logs"


def audit_path() -> Path:
    return app_data_dir() / "audit" / "audit.jsonl"


def state_dir() -> Path:
    return app_data_dir() / "state"


def bridge_state_dir() -> Path:
    return app_data_dir() / "bridge-state"


def settings_path() -> Path:
    return app_data_dir() / "settings.json"


def policy_state_path() -> Path:
    """Per-run policy context read by the Cursor hook (outside the workspace)."""
    return state_dir() / "policy_state.json"


def hook_decisions_path() -> Path:
    return state_dir() / "hook_decisions.jsonl"


def usage_ledger_path() -> Path:
    return state_dir() / "usage.json"


def known_folders() -> dict[str, Path]:
    """Well-known user folders (resolves OneDrive redirection when possible)."""
    folders = {
        "desktop": home() / "Desktop",
        "downloads": home() / "Downloads",
        "documents": home() / "Documents",
        "pictures": home() / "Pictures",
        "music": home() / "Music",
        "videos": home() / "Videos",
    }
    if os.name == "nt":
        try:
            import winreg

            key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
            names = {"desktop": "Desktop", "documents": "Personal", "pictures": "My Pictures",
                     "music": "My Music", "videos": "My Video",
                     "downloads": "{374DE290-123F-4565-9164-39C4925E467B}"}
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
                for alias, value_name in names.items():
                    try:
                        raw, _ = winreg.QueryValueEx(k, value_name)
                        folders[alias] = Path(os.path.expandvars(raw))
                    except OSError:
                        pass
        except OSError:
            pass
    return folders


def ensure_app_dirs() -> None:
    for d in (app_data_dir(), logs_dir(), audit_path().parent, state_dir(), bridge_state_dir()):
        d.mkdir(parents=True, exist_ok=True)
