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

# Apps people actually say, first in the hotword list (alphabetical Start-menu names drowned "Notepad").
COMMON_HINTS = ("Jarvis, Notepad, Chrome, Google Chrome, Edge, Calculator, Excel, Word, PowerPoint, Outlook, "
                "WhatsApp, YouTube, Gmail, Downloads, Desktop, Documents, Spotify, Teams, VS Code, Cursor, "
                "File Explorer, Settings, Task Manager, screenshot, volume, brightness")

# Wake phrase (and how Whisper mishears it) at the start of a transcript.
import re  # noqa: E402

WAKE_PREFIX = re.compile(
    r"^\W*(?:(?:hey|hi|hay|he|a|ok|okay|hello|ae)\W+)?"
    r"(?:jarvis|jervis|javis|jarvas|jarves|jarvi|travis|service|jarvish|charvis|garvis)\b\W*", re.I)


WAKE_WORD = re.compile(r"\b(jarvis|jervis|javis|jarvas|jarves|jarvish|charvis|garvis)\b[\s,.!?:]*", re.I)


def strip_wake(text: str) -> str:
    """Drop the wake phrase. Whisper renders "Hey" as anything ("Edge, Jarvis", "A Jarvis"), so when a
    Jarvis-word appears within the first 3 words, everything up to and including it is removed."""
    text = (text or "").strip()
    m = WAKE_WORD.search(text)
    if m:
        before = [w.strip(" ,.!?").lower() for w in text[:m.start()].split()]
        # "open the jarvis folder" -> 'jarvis' is content, not the wake word.
        if len(before) <= 2 and not any(w in COMMAND_WORDS for w in before):
            return text[m.end():].strip(" ,.!?")
    return WAKE_PREFIX.sub("", text).strip(" ,.!?")


COMMAND_WORDS = {"open", "close", "start", "launch", "run", "create", "make", "delete", "remove", "show", "find",
                 "search", "send", "play", "write", "read", "copy", "move", "rename", "the", "my", "in", "to", "of",
                 "kholo", "band", "dikhao", "batao", "set", "turn"}


def clean_transcript(text: str, max_words: int = 40) -> str:
    """Reject hallucinated output: hint-list echoes, a phrase repeated 3+ times, runaway length."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return ""
    low = text.lower().strip(" .,!?")
    if low in COMMON_HINTS.lower() or low in ("thank you", "thanks for watching", "you", "bye"):
        return ""
    parts = [p.strip().lower() for p in re.split(r"[,.;!?]", text) if p.strip()]
    if parts:
        most = max(parts.count(p) for p in set(parts))
        if most >= 3:
            return ""
    words = text.split()
    if len(words) > max_words:
        return ""
    return text


class WhisperStt:
    def __init__(self, model: str = "base", hints: str = "", languages: tuple[str, ...] = ("en", "hi")) -> None:
        self.model_name = model
        # Curated hints ONLY. The full Start-menu list made Whisper hallucinate app names on noise
        # ("Client Key Manager" x40 in the live log). `hints` is accepted for API compatibility.
        self.hints = COMMON_HINTS
        self.languages = tuple(languages) or ("en",)
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

                    threads = max(2, (os.cpu_count() or 4) - 1)
                    try:   # cached model: no network check to HuggingFace (it stalled startup)
                        self._model = WhisperModel(self.model_name, device="cpu", compute_type="int8",
                                                   cpu_threads=threads, local_files_only=True)
                    except Exception:
                        log.info("whisper model '%s' not cached yet - downloading once", self.model_name)
                        self._model = WhisperModel(self.model_name, device="cpu", compute_type="int8",
                                                   cpu_threads=threads)
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
        language = self._pick_language(model, audio)
        kwargs = dict(beam_size=3, language=language, condition_on_previous_text=False, without_timestamps=True,
                      temperature=0.0,                     # single pass: no slow temperature-fallback retries
                      vad_filter=True,                     # bundled Silero VAD drops non-speech -> fewer hallucinations
                      vad_parameters={"min_silence_duration_ms": 400},
                      no_repeat_ngram_size=3, repetition_penalty=1.15,   # stop "X, X, X, X..." loops
                      max_new_tokens=96)                                 # a spoken command is short
        if self.hints:
            kwargs["hotwords"] = self.hints[:300]          # vocabulary bias without a leak-prone sentence prompt
        try:
            segments, info = model.transcribe(audio, **kwargs)
        except TypeError:                                  # older faster-whisper without hotwords
            kwargs.pop("hotwords", None)
            segments, info = model.transcribe(audio, **kwargs)
        kept = []
        for s in segments:
            # Low-confidence / probably-not-speech segments are where hallucinations come from.
            if getattr(s, "no_speech_prob", 0.0) > 0.6 or getattr(s, "avg_logprob", 0.0) < -1.0:
                log.info("whisper dropped low-confidence segment %r (no_speech %.2f, logprob %.2f)", s.text,
                         getattr(s, "no_speech_prob", 0.0), getattr(s, "avg_logprob", 0.0))
                continue
            if getattr(s, "compression_ratio", 0.0) > 2.4:
                log.info("whisper dropped repetitive segment %r", s.text[:80])
                continue
            kept.append(s.text.strip())
        text = clean_transcript(" ".join(kept))
        conf = float(getattr(info, "language_probability", 0.0) or 0.0)
        log.info("whisper heard %r (lang %s %.2f) in %.1fs", text, info.language, conf, time.perf_counter() - t0)
        # Whisper hallucinates stock phrases on near-silence; drop them.
        if text.lower().strip(" .!") in ("", "thank you", "thanks for watching", "you", "bye"):
            return "", 0.0, info.language
        return text, max(conf, 0.7), info.language

    def _pick_language(self, model, audio: np.ndarray) -> str:
        """Best of the allowed languages only (free detection said 'nn' = Norwegian for English speech)."""
        if len(self.languages) == 1:
            return self.languages[0]
        try:
            _lang, _prob, all_probs = model.detect_language(audio)
            allowed = [(p, lang) for lang, p in all_probs if lang in self.languages]
            if allowed:
                return max(allowed)[1]
        except Exception as exc:
            log.debug("language detection failed (%s); using %s", exc, self.languages[0])
        return self.languages[0]

    def listen(self, start_timeout_s: float = 8.0, max_s: float = 15.0) -> tuple[str, float, str]:
        audio = self.record(start_timeout_s=start_timeout_s, max_s=max_s)
        return self.transcribe(audio)
