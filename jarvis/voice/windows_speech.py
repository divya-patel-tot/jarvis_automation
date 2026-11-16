"""Windows-native voice: offline wake word + dictation STT (WinRT SpeechRecognizer)
and interruptible TTS (SAPI). English-only path; no extra models needed.

* Wake word: list-constraint recognizer ("hey jarvis") -> works offline.
* Commands: SpeechRecognitionTopicConstraint(WEB_SEARCH) — short phrases in a
  desktop app. DICTATION is a fallback (it often returns Unknown unpackaged).
* TTS: SAPI SpVoice async with purge -> instant barge-in.

A single SpeechRecognizer is reused (two instances fight over the microphone).
COM is initialized on the voice thread; every listen result is logged.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import queue
import re
import threading
from datetime import timedelta
from typing import Callable

from jarvis.events import bus

log = logging.getLogger(__name__)

# Set while SAPI is playing so STT does not transcribe Jarvis's own voice.
SPEAKING = threading.Event()

# HRESULT -> what the user should actually do.
_HRESULT_HINTS = {
    -2147199735: "Turn on Settings > Privacy > Speech > Online speech recognition.",
    -2147221008: "COM was not initialized (internal). Restart Jarvis.",
    -2146233079: "Speech engine rejected the listen call (no dictation grammar, or another app has the mic).",
    -2147023728: "The microphone is in use by another program. Close it and retry.",
    -2147024891: "Microphone access denied. Settings > Privacy > Microphone > allow desktop apps.",
}

_CONF_SCORE = {0: 0.95, 1: 0.8, 2: 0.5, 3: 0.0}  # HIGH, MEDIUM, LOW, REJECTED
_CONF_NAME = {0: "high", 1: "medium", 2: "low", 3: "rejected"}
_MIN_CONF = {"high": 0.8, "medium": 0.5, "low": 0.2}
_STATUS_NAME = {
    0: "success", 1: "language-not-supported", 2: "grammar-mismatch", 3: "grammar-compile-failed",
    4: "audio-quality", 5: "cancelled", 6: "unknown", 7: "timeout", 8: "pause-limit",
    9: "network-failure", 10: "microphone-unavailable",
}
_STATE_NAME = {0: "idle", 1: "capturing", 2: "processing", 3: "sound", 4: "sound-ended",
               5: "speech-detected", 6: "paused"}
_WAKE_STRIP = re.compile(r"[^a-z0-9 ]+")


def explain_winerror(exc: BaseException) -> str:
    """Turn a WinRT/COM error into a one-line diagnosis."""
    win = getattr(exc, "winerror", None)
    if isinstance(win, int) and win in _HRESULT_HINTS:
        return f"{exc} -- {_HRESULT_HINTS[win]}"
    text = str(exc)
    low = text.lower()
    if "privacy" in low or "0x80045509" in low:
        return f"{exc} -- Turn on Settings > Privacy > Speech > Online speech recognition."
    if "microphone" in low or "0x80070490" in low:
        return f"{exc} -- Check Settings > System > Sound > Input is your headset, and Privacy > Microphone allows desktop apps."
    return text


def pick_speech_language(preferred: str, supported: list[str]) -> str:
    """Prefer the configured tag, then other English tags the OS actually has."""
    tags = [t for t in (supported or []) if t]
    pref = (preferred or "").strip()
    if pref and pref in tags:
        return pref
    for cand in ("en-US", "en-GB", "en-IN", "en-AU"):
        if cand in tags:
            return cand
    english = next((t for t in tags if t.lower().startswith("en")), "")
    return english or pref or "en-US"


def min_confidence(level: str) -> float:
    return _MIN_CONF.get((level or "medium").lower(), 0.5)


def remainder_after_wake(text: str, phrases: list[str]) -> str:
    """If the user said 'hey jarvis open notepad', return 'open notepad'."""
    n = _WAKE_STRIP.sub(" ", (text or "").lower()).strip()
    for p in sorted(phrases, key=lambda s: len(s), reverse=True):
        pn = _WAKE_STRIP.sub(" ", p.lower()).strip()
        if not pn:
            continue
        if n == pn:
            return ""
        if n.startswith(pn + " "):
            return n[len(pn) + 1:].strip()
    return ""


def is_wake_text(text: str, phrases: list[str]) -> bool:
    n = _WAKE_STRIP.sub(" ", (text or "").lower()).strip()
    if not n:
        return False
    tokens = n.split()
    for p in phrases:
        pn = _WAKE_STRIP.sub(" ", p.lower()).strip()
        if not pn:
            continue
        if n == pn or n.startswith(pn + " ") or pn in tokens:
            return True
    return False


def _coinitialize() -> str:
    """Initialize COM on this thread. WinRT speech wants MTA; ignore if already set."""
    ole32 = ctypes.windll.ole32
    hr = ole32.CoInitializeEx(None, 0x0)  # COINIT_MULTITHREADED
    code = hr & 0xFFFFFFFF if hr < 0 else hr
    # S_OK=0, S_FALSE=1, RPC_E_CHANGED_MODE=0x80010106, RPC_E_WRONG_THREAD=0x8001010A
    if code in (0, 1):
        return "mta"
    if code in (0x80010106, 0x8001010A):
        return "already"
    return f"hr=0x{code:08X}"


def list_capture_devices() -> list[dict]:
    """Active recording endpoints. Runs on a dedicated STA thread so it never
    fights WinRT COM (MTA) on the caller."""
    out: list[dict] = []
    err: list[str] = []

    def _run() -> None:
        try:
            ctypes.windll.ole32.CoInitializeEx(None, 0x2)  # STA for pycaw/comtypes
            from pycaw.pycaw import AudioUtilities

            enumerator = AudioUtilities.GetDeviceEnumerator()
            default_ids = {}
            for role, label in ((0, "console"), (2, "communications")):
                try:
                    default_ids[label] = enumerator.GetDefaultAudioEndpoint(1, role).GetId()
                except Exception:
                    default_ids[label] = ""
            collection = enumerator.EnumAudioEndpoints(1, 1)  # eCapture, DEVICE_STATE_ACTIVE
            for i in range(collection.GetCount()):
                dev = collection.Item(i)
                did = dev.GetId()
                name = AudioUtilities.CreateDevice(dev).FriendlyName
                out.append({
                    "name": name, "id": did,
                    "default_console": did == default_ids.get("console"),
                    "default_comms": did == default_ids.get("communications"),
                })
        except Exception as exc:
            err.append(explain_winerror(exc))
        finally:
            try:
                ctypes.windll.ole32.CoUninitialize()
            except Exception:
                pass

    t = threading.Thread(target=_run, name="list-mics", daemon=True)
    t.start()
    t.join(timeout=3)
    if t.is_alive():
        log.warning("listing capture devices timed out")
    if err:
        log.warning("could not list capture devices: %s", err[0])
    return out


def default_capture_name() -> str:
    devices = list_capture_devices()
    for d in devices:
        if d.get("default_comms") or d.get("default_console"):
            return d["name"]
    return devices[0]["name"] if devices else "(none)"


def microphone_privacy_ok() -> tuple[bool, str]:
    import winreg

    def _value(path: str) -> str:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
                return str(winreg.QueryValueEx(k, "Value")[0])
        except OSError:
            return ""

    store = _value(r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone")
    apps = _value(r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone\NonPackaged")
    if store.lower() == "deny":
        return False, "Windows microphone privacy is Deny for all apps"
    if apps.lower() == "deny":
        return False, "Windows microphone privacy is Deny for desktop apps"
    return True, "desktop apps allowed" if apps.lower() == "allow" or not apps else f"store={store or 'unset'} apps={apps or 'unset'}"


def supported_speech_languages() -> list[str]:
    try:
        from winrt.windows.media.speechrecognition import SpeechRecognizer

        return [l.language_tag for l in SpeechRecognizer.supported_topic_languages]
    except Exception as exc:
        log.warning("could not list speech languages: %s", exc)
        return []


def voice_health() -> list[tuple[str, bool, str]]:
    """Checks for `python -m jarvis doctor`."""
    checks: list[tuple[str, bool, str]] = []
    langs = supported_speech_languages()
    checks.append(("Windows speech languages", bool(langs), ", ".join(langs) or "none"))
    try:
        import winreg

        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Speech_OneCore\Settings\OnlineSpeechPrivacy")
        accepted = bool(winreg.QueryValueEx(k, "HasAccepted")[0])
    except OSError:
        accepted = False
    checks.append(("Online speech recognition", accepted,
                   "on" if accepted else "OFF - Settings > Privacy > Speech > Online speech recognition"))
    ok, detail = microphone_privacy_ok()
    checks.append(("Microphone privacy", ok, detail))
    devices = list_capture_devices()
    if devices:
        bits = []
        for d in devices:
            tag = d["name"]
            if d["default_comms"]:
                tag += " [default communications]"
            elif d["default_console"]:
                tag += " [default]"
            bits.append(tag)
        checks.append(("Recording devices", True, "; ".join(bits)))
    else:
        checks.append(("Recording devices", False, "none active - plug in the headset and set it as the Windows input device"))
    return checks


# --------------------------------------------------------------------------- TTS
class Tts:
    SVSFlagsAsync, SVSFPurgeBeforeSpeak = 1, 2

    def __init__(self, voice_hint: str = "", rate: int = 1) -> None:
        self._q: "queue.Queue[str | None]" = queue.Queue()
        self.voice_hint, self.rate = voice_hint.lower(), rate
        self._voice = None
        self.speaking = threading.Event()
        self.voice_name = ""
        threading.Thread(target=self._loop, name="tts", daemon=True).start()
        bus.subscribe("say", lambda e: self.say(e.get("text", "")))
        bus.subscribe("stop_speech", lambda e: self.stop())

    def _loop(self) -> None:
        import comtypes
        import comtypes.client

        comtypes.CoInitialize()
        self._voice = comtypes.client.CreateObject("SAPI.SpVoice")
        self._voice.Rate = self.rate
        voices = self._voice.GetVoices()
        chosen = voices.Item(0).GetDescription() if voices.Count else "(default)"
        if self.voice_hint:
            for i in range(voices.Count):
                desc = voices.Item(i).GetDescription()
                if self.voice_hint in desc.lower():
                    self._voice.Voice = voices.Item(i)
                    chosen = desc
                    break
        self.voice_name = chosen
        log.info("TTS ready: %s (rate %+d)", chosen, self.rate)
        bus.publish("voice_status", text=f"TTS: {chosen}")
        while True:
            text = self._q.get()
            if text is None:
                continue
            self.speaking.set()
            SPEAKING.set()
            bus.publish("state", state="speaking")
            try:
                log.info("TTS speak: %s", text[:200])
                self._voice.Speak(text, self.SVSFlagsAsync)
                while not self._voice.WaitUntilDone(100):
                    if not self._q.empty() and self._q.queue[0] is None:
                        break
            except Exception as exc:
                log.warning("TTS failed: %s", explain_winerror(exc))
            self.speaking.clear()
            SPEAKING.clear()

    def say(self, text: str) -> None:
        if text:
            self._q.put(text)

    def stop(self) -> None:
        """Barge-in: drop queued speech and purge the current utterance."""
        with self._q.mutex:
            self._q.queue.clear()
        self._q.put(None)
        try:
            if self._voice is not None:
                self._voice.Speak("", self.SVSFPurgeBeforeSpeak | self.SVSFlagsAsync)
        except Exception:
            pass
        SPEAKING.clear()


class WindowsListener:
    """Wake word loop -> dictation -> callback(text, confidence)."""

    def __init__(self, on_utterance: Callable[[str, float], None], wake_phrases: list[str],
                 language: str = "en-US", follow_up_s: float = 6.0, max_utterance_s: float = 15.0,
                 wake_confidence: str = "medium") -> None:
        self.on_utterance = on_utterance
        self.wake_phrases = [p for p in (wake_phrases or ["hey jarvis"]) if p]
        self.language_pref = language
        self.language = language
        self.follow_up_s = follow_up_s
        self.max_utterance_s = max_utterance_s
        self.min_conf = min_confidence(wake_confidence)
        self.paused = threading.Event()
        self._ptt = threading.Event()
        self._stop = threading.Event()
        self.ready = threading.Event()
        self.error = ""
        self.status_line = ""
        self._mic = ""
        self._mutex = None
        self._grammar = ("", "")

    def push_to_talk(self) -> None:
        log.info("push-to-talk pressed")
        bus.publish("voice_status", text="Push-to-talk: speak now")
        self._ptt.set()

    def start(self) -> None:
        def _run() -> None:
            try:
                asyncio.run(self._main())
            except Exception as exc:
                self.error = f"Voice thread crashed: {explain_winerror(exc)}"
                log.exception("voice thread crashed")
                bus.publish("error", text=self.error)
                self.ready.set()

        threading.Thread(target=_run, name="voice", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    async def _new_recognizer(self, language: str):
        from winrt.windows.globalization import Language
        from winrt.windows.media.speechrecognition import SpeechRecognizer

        try:
            rec = SpeechRecognizer(Language(language))
        except Exception as exc:
            log.warning("SpeechRecognizer(%s) failed (%s); using OS default", language, exc)
            rec = SpeechRecognizer()
        rec.ui_options.is_read_back_enabled = False
        rec.ui_options.show_confirmation = False
        rec.add_state_changed(lambda _s, e: self._on_state(e))
        rec.add_recognition_quality_degrading(lambda _s, e: self._on_quality(e))
        return rec

    def _on_state(self, ev) -> None:
        try:
            name = _STATE_NAME.get(int(ev.state), str(ev.state))
        except Exception:
            return
        if name in ("capturing", "speech-detected", "processing"):
            log.info("mic: %s", name)
            if name == "speech-detected":
                bus.publish("voice_status", text="Heard speech...")
            elif name == "capturing":
                bus.publish("state", state="listening")

    def _on_quality(self, ev) -> None:
        try:
            problem = getattr(ev, "problem", ev)
            log.warning("audio quality: %s (headset muted, wrong input device, or too far from the mic?)", problem)
            bus.publish("voice_status", text=f"Audio problem: {problem}")
        except Exception:
            pass

    async def _compile(self, rec, kind: str, scenario: str = "web") -> None:
        from winrt.windows.media.speechrecognition import (
            SpeechRecognitionListConstraint, SpeechRecognitionScenario, SpeechRecognitionTopicConstraint,
        )

        rec.constraints.clear()
        if kind == "wake":
            rec.constraints.append(SpeechRecognitionListConstraint(self.wake_phrases, "wake"))
            label = "wake"
        else:
            # WEB_SEARCH is the reliable short-command grammar for unpackaged Win32
            # apps. DICTATION often returns status=Unknown with empty text.
            if scenario == "dictation":
                scen, hint, label = SpeechRecognitionScenario.DICTATION, "dictation", "dictation"
            else:
                scen, hint, label = SpeechRecognitionScenario.WEB_SEARCH, "webSearch", "web-search"
            rec.constraints.append(SpeechRecognitionTopicConstraint(scen, hint))
        res = await rec.compile_constraints_async()
        if int(res.status) != 0:
            raise RuntimeError(f"compile {label} grammar failed: status {int(res.status)}")
        log.info("compiled %s grammar (%s)", label, self.language)
        self._grammar = (kind, scenario if kind != "wake" else "list")

    async def _listen_once(self, rec, timeout_s: float, kind: str) -> tuple[str, float, str]:
        rec.timeouts.initial_silence_timeout = timedelta(seconds=max(1.0, timeout_s))
        rec.timeouts.end_silence_timeout = timedelta(seconds=0.8)
        rec.timeouts.babble_timeout = timedelta(seconds=self.max_utterance_s)
        op = rec.recognize_async()
        try:
            result = await asyncio.wait_for(op, timeout=timeout_s + 8)
        except asyncio.TimeoutError:
            log.warning("%s listen hung after %.0fs - cancelling (mic stuck or another app holding it)", kind, timeout_s + 8)
            try:
                op.cancel()
            except Exception:
                pass
            try:
                await rec.stop_recognition_async()
            except Exception:
                pass
            return "", 0.0, "hung"
        status = _STATUS_NAME.get(int(result.status), str(int(result.status)))
        conf = _CONF_SCORE.get(int(result.confidence), 0.0)
        text = (result.text or "").strip()
        conf_name = _CONF_NAME.get(int(result.confidence), "?")
        if status == "success" and text:
            log.info("%s heard %r (confidence %s %.2f, status %s)", kind, text, conf_name, conf, status)
        elif status == "timeout":
            log.info("%s: silence timeout (no %s in %.0fs) mic=%s", kind,
                     "wake phrase" if kind == "wake" else "speech", timeout_s, self._mic or "?")
        else:
            log.info("%s result: status=%s confidence=%s text=%r", kind, status, conf_name, text)
        return text, conf, status

    def _claim_mic_mutex(self) -> bool:
        """False if another Jarvis process already owns the microphone."""
        kernel32 = ctypes.windll.kernel32
        kernel32.SetLastError(0)
        handle = kernel32.CreateMutexW(None, True, "Local\\JarvisVoiceMic")
        already = kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS
        self._mutex = handle
        if already:
            log.error("another Jarvis process already owns the microphone - close it and restart")
            return False
        return True

    async def _startup(self) -> None:
        """Cheap, logged steps. Must not hang the console waiting for WinRT/mic."""
        log.info("voice thread starting")
        bus.publish("voice_status", text="Voice starting...")
        apt = _coinitialize()
        log.info("voice COM=%s", apt)
        if not self._claim_mic_mutex():
            raise RuntimeError("Another Jarvis is already running and has the microphone. "
                               "Close that window, then start Jarvis again.")
        self.language = self.language_pref or "en-US"
        try:
            langs = supported_speech_languages()
            self.language = pick_speech_language(self.language_pref, langs)
            log.info("speech languages: %s -> using %s", ", ".join(langs) or "?", self.language)
        except Exception as exc:
            log.warning("could not list speech languages (%s); using %s", exc, self.language)
        try:
            devices = list_capture_devices()
            self._mic = next((d["name"] for d in devices if d.get("default_comms") or d.get("default_console")),
                             devices[0]["name"] if devices else "")
            log.info("mic: %s", self._mic or "(none listed)")
        except Exception as exc:
            log.warning("mic list failed: %s", explain_winerror(exc))
        privacy_ok, privacy = microphone_privacy_ok()
        log.info("mic privacy: %s", privacy)
        if not privacy_ok:
            raise RuntimeError(f"Microphone blocked: {privacy}")

    async def _main(self) -> None:
        rec = None
        try:
            await self._startup()
            rec = await self._new_recognizer(self.language)
            await self._compile(rec, "wake")
        except Exception as exc:
            self.error = f"Voice unavailable: {explain_winerror(exc)}"
            log.error(self.error)
            bus.publish("error", text=self.error)
            self.ready.set()
            return

        self.status_line = (f"Voice ready [{self.language}] mic: {self._mic or 'default'}. "
                            f"Say {self.wake_phrases[0]!r} or press Ctrl+Alt+Space.")
        log.info(self.status_line)
        bus.publish("voice_status", text=self.status_line)
        self.ready.set()

        follow_up_until = 0.0
        loop = asyncio.get_running_loop()
        compiled = "wake"
        command_scenario = "web"
        command_misses = 0
        consecutive_errors = 0
        while not self._stop.is_set():
            if self.paused.is_set() or SPEAKING.is_set():
                await asyncio.sleep(0.2)
                continue
            try:
                await self._wait_quiet()
                triggered = self._ptt.is_set() or loop.time() < follow_up_until
                if not triggered:
                    if compiled != "wake":
                        rec = await self._rebuild(rec, "wake")
                        compiled = "wake"
                    bus.publish("state", state="idle")
                    text, conf, status = await self._listen_once(rec, 25, "wake")
                    consecutive_errors = 0
                    if status == "hung":
                        rec = await self._rebuild(rec, "wake")
                        continue
                    if status == "microphone-unavailable":
                        bus.publish("error", text="Microphone unavailable - is another Jarvis (or Teams) using it?")
                        await asyncio.sleep(3)
                        rec = await self._rebuild(rec, "wake")
                        continue
                    if not text or conf < self.min_conf or not is_wake_text(text, self.wake_phrases):
                        if text:
                            log.info("ignored non-wake %r (conf %.2f, need %.2f)", text, conf, self.min_conf)
                        continue
                    bus.publish("stop_speech")
                    bus.publish("earcon")
                    bus.publish("voice_status", text=f"Wake: {text}")
                    extra = remainder_after_wake(text, self.wake_phrases)
                    if extra:
                        log.info("command included in wake utterance: %r", extra)
                        bus.publish("transcript", text=extra, final=True, confidence=conf)
                        await loop.run_in_executor(None, self.on_utterance, extra, conf)
                        follow_up_until = loop.time() + self.follow_up_s
                        command_misses = 0
                        continue
                self._ptt.clear()
                rec, text, conf, status, compiled, command_scenario = await self._capture_command(
                    rec, compiled, command_scenario, timeout_s=10.0 if not triggered else max(self.follow_up_s, 8.0))
                consecutive_errors = 0
                if status == "hung":
                    rec = await self._rebuild(rec, "command", command_scenario)
                    compiled = "command"
                    follow_up_until = loop.time() + 8.0
                    continue
                if text and conf >= 0.2:
                    command_misses = 0
                    command_scenario = "web"
                    bus.publish("transcript", text=text, final=True, confidence=conf)
                    await loop.run_in_executor(None, self.on_utterance, text, conf)
                    follow_up_until = loop.time() + self.follow_up_s
                    continue
                # Stay in command mode so repeating the request is not treated
                # as a wake phrase (that was the empty/rejected loop in the logs).
                command_misses += 1
                if command_misses >= 2:
                    log.info("no command after %s tries - back to wake word", command_misses)
                    command_misses = 0
                    command_scenario = "web"
                    follow_up_until = 0.0
                    bus.publish("say", text="Sorry, I didn't catch that.")
                else:
                    command_scenario = "dictation" if command_scenario == "web" else "web"
                    follow_up_until = loop.time() + 8.0
                    log.info("no command text (status=%s) - retry as %s, say the request again",
                             status, command_scenario)
                    bus.publish("voice_status", text="Didn't catch that. Say the request again.")
                    bus.publish("say", text="Please say it again.")
            except Exception as exc:
                consecutive_errors += 1
                msg = explain_winerror(exc)
                log.warning("voice loop error (%s): %s", consecutive_errors, msg)
                bus.publish("voice_status", text=f"Voice error: {msg}")
                if "privacy" in str(exc).lower() or "0x80045509" in str(exc).lower():
                    bus.publish("error", text="Turn on Settings > Privacy > Speech > Online speech recognition.")
                    await asyncio.sleep(15)
                else:
                    await asyncio.sleep(min(4, consecutive_errors))
                try:
                    rec = await self._rebuild(rec, compiled if compiled == "wake" else "command",
                                              command_scenario)
                except Exception as rebuild_exc:
                    log.error("could not rebuild recognizer: %s", explain_winerror(rebuild_exc))
                    await asyncio.sleep(5)
        if rec is not None:
            try:
                rec.close()
            except Exception:
                pass

    async def _capture_command(self, rec, compiled: str, scenario: str, timeout_s: float):
        """One command listen on a fresh topic-constraint recognizer (not the wake list)."""
        label = "web-search" if scenario == "web" else "dictation"
        if compiled != "command" or self._grammar != ("command", scenario):
            rec = await self._rebuild(rec, "command", scenario)
            compiled = "command"
        bus.publish("state", state="listening")
        bus.publish("voice_status", text="Listening... speak your request")
        await self._wait_quiet()
        text, conf, status = await self._listen_once(rec, timeout_s, f"command/{label}")
        return rec, text, conf, status, compiled, scenario

    async def _wait_quiet(self, max_wait_s: float = 30.0, tail_s: float = 0.35) -> None:
        """Don't listen while our own TTS is talking (the mic would hear Jarvis).

        Waits for SPEAKING to clear (capped, so a stuck TTS can't freeze voice),
        then a short tail so the end of the speech/echo isn't captured.
        """
        if not SPEAKING.is_set():
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max_wait_s
        while SPEAKING.is_set() and loop.time() < deadline and not self._stop.is_set():
            await asyncio.sleep(0.1)
        if SPEAKING.is_set():
            log.warning("TTS still marked speaking after %.0fs - listening anyway", max_wait_s)
        await asyncio.sleep(tail_s)

    async def _rebuild(self, rec, kind: str, scenario: str = "web"):
        log.info("recreating speech recognizer (%s/%s)", kind, scenario if kind != "wake" else "list")
        try:
            if rec is not None:
                rec.close()
        except Exception:
            pass
        rec = await self._new_recognizer(self.language)
        await self._compile(rec, kind, scenario)
        return rec
