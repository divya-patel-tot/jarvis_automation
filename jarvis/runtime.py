"""Process wiring shared by the console and the desktop overlay.

Moves the startup sequence out of ``__main__.run``. It does not decide what a
command means or change how a task runs: it starts the pieces that already
exist and forwards text, answers, and approval clicks to them.
"""

from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Callable

from jarvis import config
from jarvis.events import bus
from jarvis.models import Source, TaskResult, UserRequest
from jarvis.config import Settings

log = logging.getLogger("jarvis")


def _print(msg: str) -> None:
    print(msg, flush=True)


def start_hotkeys(on_kill, on_ptt, on_show=None) -> None:
    """Global hotkeys: Ctrl+Alt+K stop, Ctrl+Alt+Space talk, Ctrl+Alt+J show the overlay."""
    def loop() -> None:
        user32 = ctypes.windll.user32
        MOD_ALT, MOD_CONTROL, WM_HOTKEY = 0x1, 0x2, 0x0312
        ok1 = user32.RegisterHotKey(None, 1, MOD_CONTROL | MOD_ALT, 0x4B)    # K
        ok2 = user32.RegisterHotKey(None, 2, MOD_CONTROL | MOD_ALT, 0x20)    # Space
        ok3 = user32.RegisterHotKey(None, 3, MOD_CONTROL | MOD_ALT, 0x4A)    # J
        if not ok1:
            log.warning("could not register Ctrl+Alt+K kill hotkey (in use?)")
        if not ok2:
            log.warning("could not register Ctrl+Alt+Space push-to-talk (in use?)")
        if not ok3:
            log.warning("could not register Ctrl+Alt+J overlay hotkey (in use?)")
        else:
            log.info("hotkeys: Ctrl+Alt+K = stop, Ctrl+Alt+Space = push-to-talk, Ctrl+Alt+J = overlay")
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message != WM_HOTKEY:
                continue
            if msg.wParam == 1:
                on_kill()
            elif msg.wParam == 2:
                on_ptt()
            elif on_show is not None:
                on_show()
    threading.Thread(target=loop, name="hotkeys", daemon=True).start()


class JarvisRuntime:
    """One running Jarvis: orchestrator, voice, speech, and hotkeys."""

    def __init__(self, settings: Settings, *, voice: bool = True, console: bool = False,
                 orchestrator=None) -> None:
        self.settings = settings
        self.voice_wanted = voice
        self.console = console
        self.orch = orchestrator
        self.listener = None
        self.tts = None
        self._tts_say: Callable[[str], None] | None = None
        self._answers: list[str] = []
        self._answer_lock = threading.Lock()
        self._answer_ready = threading.Event()
        self._waiting_question = threading.Event()
        self._started = False
        self._hotkeys_on = False
        self._console_state = {"v": ""}
        self._on_settings_changed: list[Callable[[], None]] = []

    # ------------------------------------------------------------------ questions
    def ask_user(self, question: str) -> str:
        """Blocking clarifying question. Console prints it; the overlay shows chips."""
        if self.console:
            _print(f"\nJarvis asks: {question}")
        with self._answer_lock:
            self._answers.clear()
        self._answer_ready.clear()
        self._waiting_question.set()
        bus.publish("question", text=question)
        bus.publish("say", text=question)
        self._answer_ready.wait(120)
        self._waiting_question.clear()
        with self._answer_lock:
            return self._answers.pop() if self._answers else ""

    def answer(self, text: str) -> None:
        with self._answer_lock:
            self._answers.append(text)
        self._answer_ready.set()

    def waiting_for_answer(self) -> bool:
        return self._waiting_question.is_set()

    # ------------------------------------------------------------------ commands
    def submit(self, text: str, source: Source = Source.TYPED, confidence: float = 1.0, *,
               block: bool = False) -> None:
        """Hand a typed or spoken line to Jarvis. ``block`` waits (used by tests)."""
        if self.waiting_for_answer():
            self.answer(text)
            return
        if not self.settings.ui.enabled:
            self._refuse_disabled()
            return
        heard = getattr(self.listener, "last_language", "") if source == Source.VOICE else ""
        lang = heard or ("hi" if any(w in f" {text.lower()} " for w in (" karo", " kholo", " batao", " kya ", " hai "))
                         else "en")
        req = UserRequest(text=text, source=source, confidence=confidence, language=lang)
        if source == Source.VOICE and confidence < 0.6 and not self.orch.confirm.pending():
            if self.console:
                _print(f"Jarvis: Did you mean \"{text}\"? (low confidence - please type or repeat)")
            bus.publish("say", text="Sorry, could you repeat that?")
            return
        if block:
            self.orch.handle(req)
            return
        threading.Thread(target=self.orch.handle, args=(req,), daemon=True).start()

    def _refuse_disabled(self) -> None:
        spoken = "Jarvis is turned off. Enable it to take commands."
        bus.publish("say", text=spoken)
        bus.publish("result", result=TaskResult("failed", spoken))
        bus.publish("state", state="idle")

    def approve(self, nonce: str) -> bool:
        return bool(self.orch and self.orch.confirm.click(nonce, "approve"))

    def deny(self, nonce: str) -> bool:
        return bool(self.orch and self.orch.confirm.click(nonce, "deny"))

    def kill(self) -> None:
        if self.orch is not None:
            self.orch.kill_switch()

    def push_to_talk(self) -> None:
        if not self.settings.ui.enabled or not self.settings.voice.enabled:
            return
        if self.listener is not None and hasattr(self.listener, "push_to_talk"):
            self.listener.push_to_talk()

    def show_overlay(self) -> None:
        bus.publish("show_overlay")

    # ------------------------------------------------------------------ lifecycle
    def start(self, *, hotkeys: bool = True) -> None:
        if self._started:
            return
        # Heavy imports on this thread before worker threads exist (D-012).
        if self.voice_wanted and self.settings.voice.enabled:
            self._preload_voice_modules()
        if self.orch is None:
            from jarvis.orchestrator import Orchestrator

            self.orch = Orchestrator(self.settings, ask_user=self.ask_user)
        if self.console:
            self._bind_console()
        self._ensure_tts()
        self.orch.start()
        self._start_listener()
        self._apply_listen_gate()
        if hotkeys and not self._hotkeys_on:
            self._hotkeys_on = True
            start_hotkeys(self.kill, self.push_to_talk, self.show_overlay)
        self._started = True

    def stop(self) -> None:
        if self.listener is not None:
            try:
                self.listener.stop()
            except Exception:
                log.exception("voice stop failed")
        if self.orch is not None:
            self.orch.shutdown()

    def _preload_voice_modules(self) -> None:
        try:
            import importlib
            importlib.import_module("faster_whisper")
            if self.settings.voice.wake_engine == "openwakeword":
                importlib.import_module("openwakeword")
        except Exception:
            log.exception("voice libraries failed to import")

    def _start_listener(self) -> None:
        if self.listener is not None or not self.voice_wanted or not self.settings.voice.enabled:
            return
        s = self.settings
        listener = None
        if s.voice.wake_engine == "openwakeword":
            try:
                import importlib
                importlib.import_module("faster_whisper")
                importlib.import_module("openwakeword")

                from jarvis.voice.listener import AudioListener
                from jarvis.voice.whisper_stt import WhisperStt

                whisper = WhisperStt(s.voice.whisper_model, languages=tuple(s.voice.languages or ["en"]))
                whisper.preload_async()
                listener = AudioListener(lambda t, c: self.submit(t, Source.VOICE, c), whisper,
                                         wake_threshold=s.voice.wake_threshold,
                                         follow_up_s=s.voice.follow_up_window_s,
                                         max_utterance_s=s.voice.max_utterance_s)
                listener.barge_in = s.voice.barge_in
                if not s.ui.enabled:
                    listener.paused.set()
                listener.start()
                self._print("Voice: say 'Hey Jarvis' + your request in one go (or press Ctrl+Alt+Space).")
            except Exception as exc:
                self._print(f"(openWakeWord voice unavailable: {exc}; using Windows speech)")
                log.exception("openwakeword listener failed")
                listener = None
        if listener is None and self.voice_wanted and s.voice.enabled:
            try:
                from jarvis.voice.windows_speech import WindowsListener

                listener = WindowsListener(lambda t, c: self.submit(t, Source.VOICE, c), s.voice.wake_phrases,
                                           s.voice.stt_language, s.voice.follow_up_window_s, s.voice.max_utterance_s,
                                           s.voice.wake_confidence)
                listener.stt_engine = s.voice.stt_engine
                try:
                    import importlib
                    importlib.import_module("faster_whisper")

                    from jarvis.voice.whisper_stt import WhisperStt

                    hints = ", ".join(sorted({e.name for e in self.orch.apps.entries})[:60])
                    listener.whisper = WhisperStt(s.voice.whisper_model, hints)
                    listener.whisper.preload_async()
                except ImportError:
                    log.info("faster-whisper not installed; Windows dictation only")
                if not s.ui.enabled:
                    listener.paused.set()
                listener.start()
                self._print("Voice is starting in the background. Typed commands work now; watch [voice] lines.")
            except Exception as exc:
                self._print(f"(voice unavailable: {exc})")
                log.exception("voice failed to start")
                listener = None
        self.listener = listener

    def _ensure_tts(self) -> None:
        if not self.settings.voice.tts_enabled:
            return
        if self.tts is not None:
            return
        try:
            from jarvis.voice.windows_speech import Tts

            self.tts = Tts(self.settings.voice.tts_voice, self.settings.voice.tts_rate)
            self._tts_say = self.tts.say

            def gated(text: str, rt: "JarvisRuntime" = self) -> None:
                if rt.settings.voice.tts_enabled and rt.settings.ui.enabled and rt._tts_say is not None:
                    rt._tts_say(text)

            self.tts.say = gated  # type: ignore[method-assign]
        except Exception as exc:
            self._print(f"(TTS unavailable: {exc})")

    def _apply_listen_gate(self) -> None:
        want = self.voice_wanted and self.settings.voice.enabled and self.settings.ui.enabled
        if want and self.listener is None and self._started:
            self._start_listener()
        listener = self.listener
        if listener is None or not hasattr(listener, "paused"):
            return
        if want:
            listener.paused.clear()
        else:
            listener.paused.set()

    def _print(self, msg: str) -> None:
        if self.console:
            _print(msg)

    def _bind_console(self) -> None:
        bus.subscribe("activity", lambda e: _print(f"   · {e['text']}") if e.get("text") else None)
        bus.subscribe("error", lambda e: _print(f"!! {e['text']}"))
        bus.subscribe("notify", lambda e: _print(f"[{e.get('title')}] {e.get('text')}"))
        bus.subscribe("transcript", lambda e: _print(
            f"\nYou (voice): {e['text']}  (conf {e.get('confidence', 0):.0%})"))
        bus.subscribe("voice_status", lambda e: _print(f"  [voice] {e['text']}") if e.get("text") else None)

        def _show_state(e, box=self._console_state):
            st = e.get("state") or ""
            if st and st != box["v"] and st in ("listening", "speaking", "idle"):
                box["v"] = st
                if st == "listening":
                    _print("  [voice] Listening...")
                elif st == "idle" and box.get("shown"):
                    pass
        bus.subscribe("state", _show_state)

        def show_result(e):
            r = e["result"]
            if r.spoken:
                _print(f"Jarvis: {r.spoken}")
            if r.details and r.details.strip() != (r.spoken or "").strip():
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

    # ------------------------------------------------------------------ settings (live)
    def watch_settings(self, fn: Callable[[], None]) -> None:
        self._on_settings_changed.append(fn)

    def _notify(self) -> None:
        for fn in list(self._on_settings_changed):
            try:
                fn()
            except Exception:
                log.exception("settings listener failed")

    def save_settings(self) -> None:
        config.save(self.settings)

    def set_enabled(self, on: bool) -> None:
        self.settings.ui.enabled = bool(on)
        if not on and self.tts is not None:
            self.tts.stop()
        self._apply_listen_gate()
        self.save_settings()
        self._notify()

    def set_listening(self, on: bool) -> None:
        self.settings.voice.enabled = bool(on)
        self._apply_listen_gate()
        self.save_settings()
        self._notify()

    def set_speak(self, on: bool) -> None:
        self.settings.voice.tts_enabled = bool(on)
        if on:
            self._ensure_tts()
        elif self.tts is not None:
            self.tts.stop()
        self.save_settings()
        self._notify()

    def set_autonomy(self, full: bool) -> None:
        self.settings.autonomy = "full" if full else "safe"
        self.save_settings()
        self._notify()

    def set_private_mode(self, on: bool) -> None:
        """On means screen and clipboard reading are off (``private_mode``)."""
        self.settings.private_mode = bool(on)
        self.save_settings()
        self._notify()

    def set_desktop_control(self, on: bool) -> None:
        self.settings.desktop_control_enabled = bool(on)
        self.save_settings()
        self._notify()

    def set_hide_from_capture(self, on: bool) -> None:
        self.settings.ui.hide_from_capture = bool(on)
        self.save_settings()
        self._notify()

    def set_start_with_windows(self, on: bool, folder: Path | None = None) -> None:
        from jarvis.ui.startup import set_startup_shortcut

        set_startup_shortcut(bool(on), folder)
        self.settings.ui.start_with_windows = bool(on)
        self.save_settings()
        self._notify()

    def apply_voice_tuning(self) -> None:
        """Push wake threshold, barge-in, follow-up, and TTS rate into the live objects."""
        listener = self.listener
        voice = self.settings.voice
        if listener is not None:
            if hasattr(listener, "barge_in"):
                listener.barge_in = voice.barge_in
            if hasattr(listener, "wake_threshold"):
                listener.wake_threshold = voice.wake_threshold
            if hasattr(listener, "follow_up_s"):
                listener.follow_up_s = voice.follow_up_window_s
        if self.tts is not None:
            self.tts.rate = voice.tts_rate
            sapi = getattr(self.tts, "_voice", None)
            if sapi is not None:
                try:
                    sapi.Rate = int(voice.tts_rate)
                except Exception:
                    log.debug("could not apply TTS rate", exc_info=True)
        self.save_settings()

    def reload_whisper(self) -> None:
        """Swap the speech model. Takes effect on the next utterance."""
        listener = self.listener
        if listener is None or not hasattr(listener, "whisper"):
            self.save_settings()
            return
        try:
            from jarvis.voice.whisper_stt import WhisperStt

            voice = self.settings.voice
            if self.settings.voice.wake_engine == "openwakeword":
                whisper = WhisperStt(voice.whisper_model, languages=tuple(voice.languages or ["en"]))
            else:
                names = getattr(getattr(self.orch, "apps", None), "entries", ()) or ()
                hints = ", ".join(sorted({e.name for e in names})[:60])
                whisper = WhisperStt(voice.whisper_model, hints)
            whisper.preload_async()
            listener.whisper = whisper
        except Exception:
            log.exception("whisper reload failed")
        self.save_settings()

    def set_tts_voice(self, hint: str, rate: int) -> None:
        self.settings.voice.tts_voice = hint
        self.settings.voice.tts_rate = rate
        if self.tts is not None:
            self.tts.say = lambda _text: None  # type: ignore[method-assign]
            self.tts.stop()
            self.tts = None
            self._tts_say = None
        self.save_settings()
        self._ensure_tts()

    def set_model_prefs(self, fast: list[str], strong: list[str]) -> None:
        self.settings.models.fast = [m for m in fast if m] or self.settings.models.fast
        self.settings.models.strong = [m for m in strong if m] or self.settings.models.strong
        self.save_settings()

    def set_budgets(self, **values: int) -> None:
        for key, value in values.items():
            if hasattr(self.settings.budgets, key):
                setattr(self.settings.budgets, key, int(value))
        self.save_settings()
