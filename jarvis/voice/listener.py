"""Continuous voice listener: openWakeWord "hey jarvis" + local Whisper command STT.

One sounddevice input stream (16 kHz mono, 80 ms frames) feeds everything, so
"Hey Jarvis open notepad" in ONE breath works: on wake we keep the last ~1.5 s
of audio (wake phrase + the start of the command) and continue recording until
the user stops talking; Whisper transcribes it and the wake phrase is stripped.

Rules learned from live logs (2026-11-16):
* never listen while Jarvis is speaking (it heard its own "On it" -> "say again" loops)
* the follow-up window opens only AFTER the answer has been spoken, and an empty
  follow-up silently returns to wake mode (no "please say it again" chatter)
* WinRT's wake grammar rejects "hey jarvis <command>" in one breath -> openWakeWord
"""

from __future__ import annotations

import collections
import logging
import queue
import threading
import time
from typing import Callable

import numpy as np

from jarvis.events import bus
from jarvis.voice.whisper_stt import RATE, WhisperStt, strip_wake

log = logging.getLogger(__name__)

FRAME = 1280                  # 80 ms @ 16 kHz (openWakeWord frame size)
FRAME_S = FRAME / RATE


def _speaking() -> bool:
    try:
        from jarvis.voice.windows_speech import SPEAKING

        return SPEAKING.is_set()
    except Exception:
        return False


class AudioListener:
    STALL_S = 2.0               # no mic audio for this long -> reopen the stream
    SILENT_RMS = 0.0015         # below this a frame is "digital silence" (a live room is ~0.002-0.02)
    SILENT_REOPEN_S = 15.0      # this long without any sound -> reopen (dead-stream safety net)
    _last_frame = 0.0
    _last_loud = 0.0
    _barge_run = 0.0
    barge_in = "voice"          # voice (just talk over Jarvis - headset) | wake (say "Hey Jarvis") | off

    def __init__(self, on_utterance: Callable[[str, float], None], whisper: WhisperStt,
                 wake_threshold: float = 0.5, follow_up_s: float = 6.0, max_utterance_s: float = 15.0,
                 end_silence_s: float = 0.8, command_start_timeout_s: float = 5.0) -> None:
        self.on_utterance = on_utterance
        self.whisper = whisper
        self.wake_threshold = wake_threshold
        self.follow_up_s = follow_up_s
        self.max_utterance_s = max_utterance_s
        self.end_silence_s = end_silence_s
        self.command_start_timeout_s = command_start_timeout_s
        self.paused = threading.Event()
        self.ready = threading.Event()
        self.error = ""
        self.last_language = ""
        self._ptt = threading.Event()
        self._stop = threading.Event()
        self._frames: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=400)
        self._ring: collections.deque = collections.deque(maxlen=int(1.5 / FRAME_S))   # pre-roll 1.5 s
        self._noise = 0.004
        self._followup_pending = False
        self._followup_until = 0.0
        self._oww = None
        bus.subscribe("result", lambda e: self._on_result())

    # ------------------------------------------------------------------ public
    def start(self) -> None:
        threading.Thread(target=self._run, name="voice", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def push_to_talk(self) -> None:
        log.info("push-to-talk pressed")
        bus.publish("voice_status", text="Push-to-talk: speak now")
        self._ptt.set()

    def _on_result(self) -> None:
        self._followup_pending = True   # opened once TTS has finished speaking the answer

    # ------------------------------------------------------------------ setup
    def _load_wake_model(self):
        import openwakeword
        from openwakeword.model import Model

        try:
            return Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
        except Exception:
            openwakeword.utils.download_models(["hey_jarvis"])
            return Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")

    def _run(self) -> None:
        import sounddevice as sd

        try:
            bus.publish("voice_status", text="Voice starting...")
            self._oww = self._load_wake_model()
            self.whisper.ready.wait(60)
            dev = sd.query_devices(kind="input")
            mic = dev.get("name", "default")
        except Exception as exc:
            self.error = f"Voice unavailable: {exc}"
            log.exception("voice setup failed")
            bus.publish("error", text=self.error)
            self.ready.set()
            return

        def cb(indata, frames, t, status):  # noqa: ARG001 - sounddevice callback signature
            self._last_frame = time.monotonic()
            try:
                self._frames.put_nowait(indata[:, 0].copy())
            except queue.Full:
                pass

        first = True
        while not self._stop.is_set():
            try:
                self._last_frame = time.monotonic()
                self._last_loud = time.monotonic()
                device, extra = self._pick_device(sd)
                with sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=FRAME, callback=cb,
                                    device=device, extra_settings=extra):
                    if first:
                        msg = f"Voice ready (mic: {mic}). Say 'Hey Jarvis' + your request, or press Ctrl+Alt+Space."
                        log.info(msg)
                        bus.publish("voice_status", text=msg)
                        first = False
                    else:
                        log.info("mic stream reopened")
                    self.ready.set()
                    self._loop()      # returns when the stream stalls -> reopen
            except Exception as exc:
                log.warning("audio stream error (%s); reopening in 2 s", exc)
                bus.publish("voice_status", text=f"Mic error: {exc}. Retrying...")
                time.sleep(2)

    @staticmethod
    def _pick_device(sd):
        """Prefer WASAPI (reports device changes as errors). The MME default stream kept running but
        delivered digital silence after Teams reconfigured the Jabra headset (live 11:10, py-spy rms 0.0003
        while a fresh stream heard rms 0.02)."""
        try:
            for api in sd.query_hostapis():
                if "wasapi" in api["name"].lower() and api.get("default_input_device", -1) >= 0:
                    return api["default_input_device"], sd.WasapiSettings(auto_convert=True)
        except Exception as exc:
            log.debug("WASAPI not available (%s); using default device", exc)
        return None, None

    # ------------------------------------------------------------------ main loop
    def _next(self, timeout: float = 0.5) -> np.ndarray | None:
        try:
            return self._frames.get(timeout=timeout)
        except queue.Empty:
            return None

    def _drain(self) -> None:
        while not self._frames.empty():
            try:
                self._frames.get_nowait()
            except queue.Empty:
                break
        self._ring.clear()
        if self._oww is not None:
            self._oww.reset()

    def _loop(self) -> None:
        bus.publish("state", state="idle")
        while not self._stop.is_set():
            frame = self._next()
            if frame is None:
                # Watchdog: the Jabra/USB capture stream silently stopped delivering audio after TTS
                # played (live log 11:02:33, py-spy: voice thread waiting on an empty queue). Reopen it.
                if time.monotonic() - self._last_frame > self.STALL_S:
                    log.warning("mic stream stalled (no audio for %.0fs) - reopening", self.STALL_S)
                    return
                continue
            if self.paused.is_set():
                self._drain()
                continue
            now = time.monotonic()
            if _speaking():
                # Barge-in: keep listening while Jarvis talks; the user can cut in (like ElevenLabs agents).
                self._ring.append(frame)
                self._last_loud = now
                if self.barge_in == "off":
                    continue
                score = float(self._oww.predict(frame).get("hey_jarvis", 0.0))
                rms = float(np.sqrt(np.mean((frame.astype(np.float32) / 32768.0) ** 2)))
                loud = rms > max(self._threshold() * 2.5, 0.015)
                self._barge_run = self._barge_run + FRAME_S if loud else 0.0
                if score >= self.wake_threshold or (self.barge_in == "voice" and self._barge_run >= 0.35):
                    log.info("barge-in while speaking (wake %.2f, voice %.2fs)", score, self._barge_run)
                    self._barge_run = 0.0
                    self._capture(preroll=True, reason="barge-in", require_new_speech=False)
                continue
            if self._followup_pending:
                self._followup_pending = False
                self._followup_until = now + self.follow_up_s
            rms = float(np.sqrt(np.mean((frame.astype(np.float32) / 32768.0) ** 2)))
            if rms > self.SILENT_RMS:
                self._last_loud = now
            elif now - self._last_loud > self.SILENT_REOPEN_S:
                # Safety net for a stream that runs but is dead-silent; reopening during silence costs ~0.1 s.
                log.debug("mic near-silent for %.0fs - reopening stream (guards against dead streams)",
                          self.SILENT_REOPEN_S)
                return
            self._ring.append(frame)
            if self._ptt.is_set():
                self._ptt.clear()
                self._capture(preroll=False, reason="push-to-talk")
                continue
            if now < self._followup_until and rms > self._threshold():
                self._followup_until = 0.0
                self._capture(preroll=True, reason="follow-up", require_new_speech=False)
                continue
            score = float(self._oww.predict(frame).get("hey_jarvis", 0.0))
            if score >= self.wake_threshold:
                log.info("wake word 'hey jarvis' (score %.2f)", score)
                self._capture(preroll=True, reason=f"wake {score:.2f}")
                continue
            if rms < self._threshold():               # track the noise floor on quiet frames
                self._noise = 0.97 * self._noise + 0.03 * rms

    def _threshold(self) -> float:
        return max(self._noise * 3.0, 0.006)

    # ------------------------------------------------------------------ capture
    def _capture(self, preroll: bool, reason: str, require_new_speech: bool = True) -> None:
        bus.publish("stop_speech")
        bus.publish("earcon")
        bus.publish("state", state="listening")
        bus.publish("voice_status", text="Listening... speak your request")
        try:
            import winsound

            winsound.MessageBeep(0x40)       # non-blocking "I'm listening" cue
        except Exception:
            pass
        chunks = list(self._ring) if preroll else []
        thr = self._threshold()
        heard_new = not require_new_speech
        silent = 0.0
        t0 = time.monotonic()
        while not self._stop.is_set():
            frame = self._next(timeout=1.0)
            if frame is None:
                if time.monotonic() - self._last_frame > self.STALL_S:
                    log.warning("mic stream stalled during capture")
                break
            chunks.append(frame)
            rms = float(np.sqrt(np.mean((frame.astype(np.float32) / 32768.0) ** 2)))
            bus.publish("mic_level", level=round(min(1.0, rms * 25), 3))      # UI waveform (~12/s, capture only)
            elapsed = time.monotonic() - t0
            if rms > thr:
                heard_new, silent = True, 0.0
            else:
                silent += FRAME_S
            if heard_new and silent >= self.end_silence_s:
                break
            if not heard_new and elapsed > self.command_start_timeout_s:
                break
            if elapsed > self.max_utterance_s:
                break
        audio = (np.concatenate(chunks).astype(np.float32) / 32768.0) if chunks else np.zeros(0, np.float32)
        self._drain()
        bus.publish("state", state="thinking")
        text, conf, lang = self.whisper.transcribe(audio)
        command = strip_wake(text)
        log.info("voice %s: heard %r -> command %r", reason, text, command)
        if not command:
            bus.publish("voice_status", text="Yes? Say 'Hey Jarvis' and your request.")
            bus.publish("state", state="idle")
            return
        self.last_language = lang
        bus.publish("transcript", text=command, final=True, confidence=conf)
        threading.Thread(target=self.on_utterance, args=(command, conf), name="voice-cmd", daemon=True).start()
