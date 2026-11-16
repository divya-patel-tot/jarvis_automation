"""Jarvis entry point.

    python -m jarvis               run (console palette + voice + global kill hotkey)
    python -m jarvis --no-voice    typed only
    python -m jarvis doctor        health check of every component
    python -m jarvis set-key       store the Cursor API key (Credential Manager)
    python -m jarvis verify-audit  check the audit hash chain
"""

from __future__ import annotations

import argparse
import ctypes
import getpass
import json
import logging
import os
import sys
import threading
from ctypes import wintypes

from jarvis import config, logging_setup, paths
from jarvis.events import bus
from jarvis.models import Source, UserRequest

log = logging.getLogger("jarvis")


def _print(msg: str) -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------- hotkeys
def start_hotkeys(on_kill, on_ptt) -> None:
    """Global hotkeys via RegisterHotKey: Ctrl+Alt+K = kill switch, Ctrl+Alt+Space = push-to-talk."""
    def loop() -> None:
        user32 = ctypes.windll.user32
        MOD_ALT, MOD_CONTROL, WM_HOTKEY = 0x1, 0x2, 0x0312
        ok1 = user32.RegisterHotKey(None, 1, MOD_CONTROL | MOD_ALT, 0x4B)    # K
        ok2 = user32.RegisterHotKey(None, 2, MOD_CONTROL | MOD_ALT, 0x20)    # Space
        if not ok1:
            log.warning("could not register Ctrl+Alt+K kill hotkey (in use?)")
        if not ok2:
            log.warning("could not register Ctrl+Alt+Space push-to-talk (in use?)")
        else:
            log.info("hotkeys: Ctrl+Alt+K = stop, Ctrl+Alt+Space = push-to-talk")
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == WM_HOTKEY:
                (on_kill if msg.wParam == 1 else on_ptt)()
    threading.Thread(target=loop, name="hotkeys", daemon=True).start()


# --------------------------------------------------------------------------- doctor
def doctor() -> int:
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
    width = max(len(c[0]) for c in checks)
    for name, good, detail in checks:
        _print(f"  [{'OK ' if good else '!! '}] {name.ljust(width)}  {detail}")
    return 0 if all(c[1] for c in checks[:5]) else 1


# --------------------------------------------------------------------------- console app
def run(voice: bool) -> int:
    from jarvis.orchestrator import Orchestrator

    s = config.load()
    answers: "list[str]" = []
    answer_ready = threading.Event()
    waiting_question = threading.Event()

    def ask_user(question: str) -> str:
        _print(f"\nJarvis asks: {question}")
        bus.publish("say", text=question)
        waiting_question.set()
        answer_ready.clear()
        answer_ready.wait(120)
        waiting_question.clear()
        return answers.pop() if answers else ""

    orch = Orchestrator(s, ask_user=ask_user)

    bus.subscribe("activity", lambda e: _print(f"   · {e['text']}") if e.get("text") else None)
    bus.subscribe("error", lambda e: _print(f"!! {e['text']}"))
    bus.subscribe("notify", lambda e: _print(f"[{e.get('title')}] {e.get('text')}"))
    bus.subscribe("transcript", lambda e: _print(f"\nYou (voice): {e['text']}  (conf {e.get('confidence', 0):.0%})"))
    bus.subscribe("voice_status", lambda e: _print(f"  [voice] {e['text']}") if e.get("text") else None)
    _last_state = {"v": ""}

    def _show_state(e):
        st = e.get("state") or ""
        if st and st != _last_state["v"] and st in ("listening", "speaking", "idle"):
            _last_state["v"] = st
            if st == "listening":
                _print("  [voice] Listening...")
            elif st == "idle" and _last_state.get("shown"):
                pass
    bus.subscribe("state", _show_state)

    def show_result(e):
        r = e["result"]
        if r.spoken:
            _print(f"Jarvis: {r.spoken}")
        if r.details and r.details.strip() != r.spoken.strip():
            _print("   " + r.details.strip().replace("\n", "\n   ")[:4000])
        for f in r.files_changed:
            _print(f"   changed: {f}")
        for d in r.double_check:
            _print(f"   check: {d}")
    bus.subscribe("result", show_result)

    def on_approval(e):
        _print("\n" + "=" * 70 + "\nAPPROVAL NEEDED\n" + e["confirm_text"] + "\n" + "=" * 70)
        _print("Type: a = approve, d = deny, e <changes> = edit the request")
    bus.subscribe("approval_request", on_approval)

    tts = None
    if s.voice.tts_enabled:
        try:
            from jarvis.voice.windows_speech import Tts

            tts = Tts(s.voice.tts_voice, s.voice.tts_rate)
        except Exception as exc:
            _print(f"(TTS unavailable: {exc})")

    orch.start()

    def handle_async(text: str, source: Source, conf: float = 1.0) -> None:
        lang = "hi" if any(w in text.lower() for w in (" karo", " kholo", " batao", "kya ", " hai")) else "en"
        req = UserRequest(text=text, source=source, confidence=conf, language=lang)
        if source == Source.VOICE and conf < 0.6 and not orch.confirm.pending():
            _print(f"Jarvis: Did you mean \"{text}\"? (low confidence - please type or repeat)")
            bus.publish("say", text="Sorry, could you repeat that?")
            return
        threading.Thread(target=orch.handle, args=(req,), daemon=True).start()

    listener = None
    if voice and s.voice.enabled:
        try:
            from jarvis.voice.windows_speech import WindowsListener

            listener = WindowsListener(lambda t, c: handle_async(t, Source.VOICE, c), s.voice.wake_phrases,
                                       s.voice.stt_language, s.voice.follow_up_window_s, s.voice.max_utterance_s,
                                       s.voice.wake_confidence)
            listener.start()
            _print("Voice is starting in the background. Typed commands work now; watch [voice] lines.")
        except Exception as exc:
            _print(f"(voice unavailable: {exc})")
            log.exception("voice failed to start")
    start_hotkeys(orch.kill_switch, (listener.push_to_talk if listener else lambda: None))

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
            if waiting_question.is_set():
                answers.append(line)
                answer_ready.set()
                continue
            pending = orch.confirm.pending()
            if pending and line.lower()[:1] in ("a", "d", "e") and (len(line) == 1 or line[1] == " "):
                p = pending[-1]
                if line.lower().startswith("a"):
                    orch.confirm.click(p.nonce, "approve")
                elif line.lower().startswith("d"):
                    orch.confirm.click(p.nonce, "deny")
                else:
                    orch.confirm.click(p.nonce, "edit", line[1:].strip())
                continue
            if line.lower() in ("quit", "exit"):
                break
            if line.lower() in ("kill", "stop"):
                orch.kill_switch()
                continue
            handle_async(line, Source.TYPED)
    except KeyboardInterrupt:
        pass
    finally:
        orch.kill_switch()
        orch.shutdown()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="jarvis")
    ap.add_argument("command", nargs="?", default="run",
                    choices=["run", "doctor", "set-key", "set-mail", "verify-audit"])
    ap.add_argument("--no-voice", action="store_true")
    ap.add_argument("--log-level", default="INFO")
    a = ap.parse_args()
    logging_setup.setup(a.log_level)
    paths.ensure_app_dirs()
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
