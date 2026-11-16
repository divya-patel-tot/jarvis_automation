"""Jarvis entry point.

    python -m jarvis               run (console palette + voice + global kill hotkey)
    python -m jarvis --no-voice    typed only
    python -m jarvis ui            desktop overlay (no console with pythonw)
    python -m jarvis doctor        health check of every component
    python -m jarvis set-key       store the Cursor API key (Credential Manager)
    python -m jarvis verify-audit  check the audit hash chain
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys

from jarvis import config, logging_setup, paths


def _print(msg: str) -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------- doctor
def doctor_checks() -> list[tuple[str, bool, str]]:
    from jarvis.audit import AuditLog
    from jarvis.backend.manager import BackendManager
    from jarvis.secrets import get_api_key
    from jarvis.workspace.manager import Workspace

    s = config.load()
    checks: list[tuple[str, bool, str]] = []
    checks.append(("Python 3.11", sys.version_info[:2] == (3, 11), sys.version.split()[0]))
    key = get_api_key()
    checks.append(("Cursor API key", bool(key), "found (Credential Manager/env)" if key else "missing: python -m jarvis set-key"))
    ws = Workspace(s)
    ws.ensure()
    checks.append(("Workspace", ws.root.exists(), str(ws.root)))
    checks.append(("Policy hook registered", (ws.cursor_dir / "hooks.json").exists(), ws.hook_command()))
    bm = BackendManager(s)
    b = bm.select()
    for name, st in bm.status.items():
        checks.append((f"Backend {name}", st.startswith("ok"), st))
    if b:
        checks.append(("Fast/strong model", True, f"{bm.model_for(False)} / {bm.model_for(True)}"))
    bm.close()
    ok, n = AuditLog().verify()
    checks.append(("Audit chain", ok, f"{n} lines"))
    from jarvis.voice.windows_speech import voice_health

    checks.extend(voice_health())
    return checks


def doctor() -> int:
    checks = doctor_checks()
    width = max(len(c[0]) for c in checks)
    for name, good, detail in checks:
        _print(f"  [{'OK ' if good else '!! '}] {name.ljust(width)}  {detail}")
    return 0 if all(c[1] for c in checks[:5]) else 1


# --------------------------------------------------------------------------- console app
def run(voice: bool) -> int:
    from jarvis.models import Source
    from jarvis.runtime import JarvisRuntime

    rt = JarvisRuntime(config.load(), voice=voice, console=True)
    rt.start()

    _print(f"Logs: {paths.logs_dir() / 'jarvis.jsonl'}")
    _print("Jarvis is running. Type a request, or say 'Hey Jarvis'. Ctrl+Alt+Space = talk now. Ctrl+Alt+K = kill switch. 'quit' to exit.")
    try:
        while True:
            try:
                line = input("\n> ").strip()
            except EOFError:
                break
            if not line:
                continue
            if rt.waiting_for_answer():
                rt.answer(line)
                continue
            pending = rt.orch.confirm.pending()
            if pending and line.lower()[:1] in ("a", "d", "e") and (len(line) == 1 or line[1] == " "):
                p = pending[-1]
                if line.lower().startswith("a"):
                    rt.approve(p.nonce)
                elif line.lower().startswith("d"):
                    rt.deny(p.nonce)
                else:
                    rt.orch.confirm.click(p.nonce, "edit", line[1:].strip())
                continue
            if line.lower() in ("quit", "exit"):
                break
            if line.lower() in ("kill", "stop"):
                rt.kill()
                continue
            rt.submit(line, Source.TYPED)
    except KeyboardInterrupt:
        pass
    finally:
        rt.kill()
        rt.stop()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="jarvis")
    ap.add_argument("command", nargs="?", default="run",
                    choices=["run", "ui", "doctor", "set-key", "set-mail", "verify-audit"])
    ap.add_argument("--no-voice", action="store_true")
    ap.add_argument("--minimized", action="store_true", help="ui: start in the tray")
    ap.add_argument("--log-level", default="INFO")
    a = ap.parse_args()
    logging_setup.setup(a.log_level, console=a.command != "ui")
    paths.ensure_app_dirs()
    if a.command == "ui":
        from jarvis.ui.app import run_ui

        return run_ui(minimized=a.minimized, voice=not a.no_voice)
    if a.command == "doctor":
        return doctor()
    if a.command == "set-mail":
        import keyring

        _print("Email account Jarvis sends from (Gmail: create an App Password at "
               "https://myaccount.google.com/apppasswords).")
        address = input("Email address: ").strip()
        name = input("Display name (optional): ").strip()
        host = input("SMTP host [smtp.gmail.com]: ").strip() or "smtp.gmail.com"
        port = input("SMTP port [587]: ").strip() or "587"
        pw = getpass.getpass("App password (input hidden): ").replace(" ", "")
        keyring.set_password("jarvis-mail", "account", json.dumps(
            {"address": address, "from_name": name, "smtp_host": host, "smtp_port": int(port), "app_password": pw}))
        _print("Saved to Windows Credential Manager (service 'jarvis-mail').")
        return 0
    if a.command == "set-key":
        from jarvis.secrets import set_api_key

        set_api_key(getpass.getpass("Cursor API key (input hidden): "))
        _print("Saved to Windows Credential Manager and user environment.")
        return 0
    if a.command == "verify-audit":
        from jarvis.audit import AuditLog

        ok, n = AuditLog().verify()
        _print(json.dumps({"ok": ok, "line": n}))
        return 0 if ok else 1
    return run(voice=not a.no_voice)


if __name__ == "__main__":
    if os.name != "nt":
        sys.exit("Jarvis supports Windows only.")
    sys.exit(main())
