"""Local speech-to-text with faster-whisper (offline; English + Hindi/Hinglish).

Used for the COMMAND after the wake word. Windows' WinRT free-form dictation
returns status=Unknown for unpackaged desktop apps on this machine even with
online speech enabled (D-011), so Whisper is the reliable path.

listen(): records from the default microphone with an energy VAD (noise floor
calibrated per call), stops after ~0.9 s of silence or max length, transcribes.
"""

from __future__ import annotations

import logging
import queue
import threading
import time

import numpy as np

log = logging.getLogger(__name__)

RATE = 16_000
BLOCK = 480          # 30 ms


class WhisperStt:
    def __init__(self, model: str = "base", hints: str = "") -> None:
        self.model_name = model
        self.hints = hints
        self._model = None
        self._lock = threading.Lock()
        self.ready = threading.Event()
        self.error = ""

    # ------------------------------------------------------------------ model
    def preload_async(self) -> None:
        threading.Thread(target=self._load, name="whisper-load", daemon=True).start()

    def _load(self):
        with self._lock:
            if self._model is None:
                try:
                    from faster_whisper import WhisperModel

                    t0 = time.perf_counter()
                    import os

                    self._model = WhisperModel(self.model_name, device="cpu", compute_type="int8",
                                               cpu_threads=max(2, (os.cpu_count() or 4) - 1))
                    log.info("whisper '%s' loaded in %.1fs", self.model_name, time.perf_counter() - t0)
                except Exception as exc:
                    self.error = f"Whisper unavailable: {exc}"
                    log.error(self.error)
                finally:
                    self.ready.set()
        return self._model

    @property
    def available(self) -> bool:
        return self._load() is not None

    # ------------------------------------------------------------------ audio
    def record(self, start_timeout_s: float = 8.0, max_s: float = 15.0, silence_s: float = 0.9) -> np.ndarray:
        import sounddevice as sd

        q: "queue.Queue[np.ndarray]" = queue.Queue()

        def cb(indata, frames, t, status):  # noqa: ARG001
            q.put(indata[:, 0].copy())

        frames: list[np.ndarray] = []
        noise: list[float] = []
        started = False
        silent_for = 0.0
        t0 = time.monotonic()
        with sd.InputStream(samplerate=RATE, channels=1, dtype="float32", blocksize=BLOCK, callback=cb):
            while True:
                try:
                    block = q.get(timeout=1.0)
                except queue.Empty:
                    break
                rms = float(np.sqrt(np.mean(block ** 2)) + 1e-9)
                elapsed = time.monotonic() - t0
                if elapsed < 0.25:                       # calibrate noise floor
                    noise.append(rms)
                    frames.append(block)
                    continue
                floor = (np.median(noise) if noise else 0.003)
                threshold = max(floor * 3.0, 0.006)
                if not started:
                    frames.append(block)
                    frames[:] = frames[-10:]               # keep ~300 ms pre-roll
                    if rms > threshold:
                        started = True
                        log.info("whisper: speech started (rms %.4f > %.4f)", rms, threshold)
                    elif elapsed > start_timeout_s:
                        log.info("whisper: no speech within %.0fs", start_timeout_s)
                        return np.zeros(0, dtype=np.float32)
                    continue
                frames.append(block)
                silent_for = silent_for + BLOCK / RATE if rms < threshold * 0.8 else 0.0
                if silent_for >= silence_s or elapsed > max_s:
                    break
        return np.concatenate(frames) if frames else np.zeros(0, dtype=np.float32)

    # ------------------------------------------------------------------ stt
    def transcribe(self, audio: np.ndarray) -> tuple[str, float, str]:
        model = self._load()
        if model is None or audio.size < RATE * 0.3:
            return "", 0.0, ""
        t0 = time.perf_counter()
        kwargs = dict(beam_size=1, language=None, condition_on_previous_text=False, without_timestamps=True,
                      temperature=0.0,                     # single pass: no slow temperature-fallback retries
                      vad_filter=True,                     # bundled Silero VAD drops non-speech -> fewer hallucinations
                      vad_parameters={"min_silence_duration_ms": 400})
        if self.hints:
            kwargs["hotwords"] = self.hints[:300]          # vocabulary bias without a leak-prone sentence prompt
        try:
            segments, info = model.transcribe(audio, **kwargs)
        except TypeError:                                  # older faster-whisper without hotwords
            kwargs.pop("hotwords", None)
            segments, info = model.transcribe(audio, **kwargs)
        text = " ".join(s.text.strip() for s in segments).strip()
        if self.hints and text and text.lower() in self.hints.lower():
            text = ""                                      # echo of the hint list, not speech
        conf = float(getattr(info, "language_probability", 0.0) or 0.0)
        log.info("whisper heard %r (lang %s %.2f) in %.1fs", text, info.language, conf, time.perf_counter() - t0)
        # Whisper hallucinates stock phrases on near-silence; drop them.
        if text.lower().strip(" .!") in ("", "thank you", "thanks for watching", "you", "bye"):
            return "", 0.0, info.language
        return text, max(conf, 0.7), info.language

    def listen(self, start_timeout_s: float = 8.0, max_s: float = 15.0) -> tuple[str, float, str]:
        audio = self.record(start_timeout_s=start_timeout_s, max_s=max_s)
        return self.transcribe(audio)
