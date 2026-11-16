"""Windows-native voice: offline wake word + dictation STT (WinRT SpeechRecognizer)
and interruptible TTS (SAPI). No extra models needed.

* Wake word: list-constraint recognizer ("hey jarvis") -> runs OFFLINE.
* Dictation: free-form topic recognizer -> uses Microsoft online speech
  (Settings > Privacy > Speech > Online speech recognition must be ON).
* TTS: SAPI SpVoice async with purge -> instant barge-in.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
from typing import Callable

from jarvis.events import bus

log = logging.getLogger(__name__)


class Tts:
    SVSFlagsAsync, SVSFPurgeBeforeSpeak = 1, 2

    def __init__(self, voice_hint: str = "", rate: int = 1) -> None:
        self._q: "queue.Queue[str | None]" = queue.Queue()
        self.voice_hint, self.rate = voice_hint.lower(), rate
        self._voice = None
        self.speaking = threading.Event()
        threading.Thread(target=self._loop, name="tts", daemon=True).start()
        bus.subscribe("say", lambda e: self.say(e.get("text", "")))
        bus.subscribe("stop_speech", lambda e: self.stop())

    def _loop(self) -> None:
        import comtypes
        import comtypes.client

        comtypes.CoInitialize()
        self._voice = comtypes.client.CreateObject("SAPI.SpVoice")
        self._voice.Rate = self.rate
        if self.voice_hint:
            voices = self._voice.GetVoices()
            for i in range(voices.Count):
                if self.voice_hint in voices.Item(i).GetDescription().lower():
                    self._voice.Voice = voices.Item(i)
        while True:
            text = self._q.get()
            if text is None:
                continue
            self.speaking.set()
            bus.publish("state", state="speaking")
            try:
                self._voice.Speak(text, self.SVSFlagsAsync)
                while not self._voice.WaitUntilDone(100):
                    if not self._q.empty() and self._q.queue[0] is None:
                        break
            except Exception as exc:
                log.warning("TTS failed: %s", exc)
            self.speaking.clear()

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


class WindowsListener:
    """Wake word loop -> dictation -> callback(text, confidence)."""

    def __init__(self, on_utterance: Callable[[str, float], None], wake_phrases: list[str],
                 language: str = "en-IN", follow_up_s: float = 6.0, max_utterance_s: float = 15.0) -> None:
        self.on_utterance = on_utterance
        self.wake_phrases = wake_phrases
        self.language = language
        self.follow_up_s = follow_up_s
        self.max_utterance_s = max_utterance_s
        self.paused = threading.Event()
        self._ptt = threading.Event()
        self._stop = threading.Event()
        self.error = ""

    def push_to_talk(self) -> None:
        self._ptt.set()

    def start(self) -> None:
        threading.Thread(target=lambda: asyncio.run(self._main()), name="voice", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    async def _recognizer(self, phrases: list[str] | None):
        from winrt.windows.globalization import Language
        from winrt.windows.media.speechrecognition import (SpeechRecognitionCompilationResult,  # noqa: F401
                                                           SpeechRecognitionListConstraint, SpeechRecognizer)

        try:
            r = SpeechRecognizer(Language(self.language))
        except Exception:
            r = SpeechRecognizer()
        if phrases:
            r.constraints.append(SpeechRecognitionListConstraint(phrases, "wake"))
        res = await r.compile_constraints_async()
        if int(res.status) != 0:
            raise RuntimeError(f"speech compile failed: status {int(res.status)}")
        return r

    async def _listen_once(self, recognizer, timeout_s: float) -> tuple[str, float]:
        from datetime import timedelta

        recognizer.timeouts.initial_silence_timeout = timedelta(seconds=timeout_s)
        recognizer.timeouts.end_silence_timeout = timedelta(seconds=0.8)
        recognizer.timeouts.babble_timeout = timedelta(seconds=self.max_utterance_s)
        result = await recognizer.recognize_async()
        conf = {0: 0.95, 1: 0.8, 2: 0.5, 3: 0.0}.get(int(result.confidence), 0.0)
        return (result.text or "").strip(), conf

    async def _main(self) -> None:
        try:
            wake = await self._recognizer(self.wake_phrases)
            dictation = await self._recognizer(None)
        except Exception as exc:
            self.error = (f"Voice unavailable: {exc}. Check a microphone is connected and "
                          "Settings > Privacy > Speech > Online speech recognition is ON.")
            log.error(self.error)
            bus.publish("error", text=self.error)
            return
        follow_up_until = 0.0
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            if self.paused.is_set():
                await asyncio.sleep(0.3)
                continue
            try:
                triggered = self._ptt.is_set() or loop.time() < follow_up_until
                if not triggered:
                    bus.publish("state", state="idle")
                    text, conf = await self._listen_once(wake, 30)
                    if not text or conf < 0.5:
                        continue
                    bus.publish("stop_speech")       # barge-in
                    bus.publish("earcon")
                self._ptt.clear()
                bus.publish("state", state="listening")
                text, conf = await self._listen_once(dictation, 5 if not triggered else self.follow_up_s)
                if not text:
                    follow_up_until = 0.0
                    if not triggered:
                        bus.publish("say", text="Sorry, I didn't catch that.")
                    continue
                bus.publish("transcript", text=text, final=True, confidence=conf)
                await loop.run_in_executor(None, self.on_utterance, text, conf)
                follow_up_until = loop.time() + self.follow_up_s
            except Exception as exc:
                msg = str(exc)
                log.warning("voice loop error: %s", msg)
                if "privacy" in msg.lower() or "0x80045509" in msg:
                    bus.publish("error", text="Turn on Settings > Privacy > Speech > Online speech recognition "
                                              "for dictation, or set stt_engine to whisper.")
                    await asyncio.sleep(30)
                else:
                    await asyncio.sleep(1)
