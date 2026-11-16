"""API key storage. Never hardcode, never log, never write into the workspace.

Lookup order:
1. Windows Credential Manager (service ``jarvis-cursor``, user ``CURSOR_API_KEY``)
2. ``CURSOR_API_KEY`` environment variable (process, then persisted user scope)

Store with ``python -m jarvis set-key`` (prompts without echo).
"""

from __future__ import annotations

import os
import re

SERVICE = "jarvis-cursor"
USERNAME = "CURSOR_API_KEY"
ENV_VAR = "CURSOR_API_KEY"

_KEY_RE = re.compile(r"\b(crsr_|key_|sk-)[A-Za-z0-9_\-]{16,}\b")


def _user_env(name: str) -> str:
    if os.name != "nt":
        return ""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            value, _ = winreg.QueryValueEx(k, name)
            return str(value or "")
    except OSError:
        return ""


def get_api_key() -> str:
    try:
        import keyring

        value = keyring.get_password(SERVICE, USERNAME)
        if value:
            return value.strip()
    except Exception:
        pass
    return (os.environ.get(ENV_VAR) or _user_env(ENV_VAR)).strip()


def set_api_key(value: str, *, also_user_env: bool = True) -> None:
    value = value.strip()
    if not value:
        raise ValueError("empty API key")
    import keyring

    keyring.set_password(SERVICE, USERNAME, value)
    if also_user_env and os.name == "nt":
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, ENV_VAR, 0, winreg.REG_SZ, value)


def delete_api_key() -> None:
    try:
        import keyring

        keyring.delete_password(SERVICE, USERNAME)
    except Exception:
        pass


def redact(text: str) -> str:
    """Remove anything that looks like an API key (and the configured key)."""
    if not text:
        return text
    key = get_api_key_cached()
    if key and key in text:
        text = text.replace(key, "[REDACTED]")
    return _KEY_RE.sub("[REDACTED]", text)


_cached: str | None = None


def get_api_key_cached() -> str:
    global _cached
    if _cached is None:
        _cached = get_api_key()
    return _cached
