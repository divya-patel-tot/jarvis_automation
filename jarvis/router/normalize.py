"""Text normalization for routing: wake-word stripping, Hinglish synonyms,
number words (English + Hindi romanized + Devanagari digits), durations, times."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

WAKE = re.compile(r"^\s*((hey|hi|ok|okay|hello|ae|arre)\s+)?jarvis[\s,.!:]*", re.I)

# Devanagari phrases -> romanized equivalents the patterns understand.
DEVANAGARI_MAP = {
    "खोलो": "kholo", "बंद करो": "band karo", "आवाज़": "awaaz", "आवाज": "awaaz", "बढ़ाओ": "badhao", "कम करो": "kam karo",
    "समय": "time", "बजे": "baje", "कितने": "kitne", "बैटरी": "battery", "स्क्रीनशॉट": "screenshot", "लॉक": "lock",
    "याद दिलाना": "remind", "याद रखो": "remember", "मिनट": "minute", "घंटे": "hour", "सेकंड": "second",
}
DEV_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

EN_NUM = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
          "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
          "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
          "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100, "half": 50,
          "a": 1, "an": 1}
HI_NUM = {"ek": 1, "do": 2, "teen": 3, "char": 4, "chaar": 4, "paanch": 5, "panch": 5, "chhe": 6, "che": 6,
          "saat": 7, "aath": 8, "nau": 9, "das": 10, "dus": 10, "gyarah": 11, "barah": 12, "pandrah": 15,
          "bees": 20, "pachchis": 25, "pachis": 25, "tees": 30, "chalis": 40, "chaalis": 40, "pachas": 50,
          "pachaas": 50, "saath": 60, "sattar": 70, "assi": 80, "nabbe": 90, "sau": 100, "aadha": 50}


def normalize(text: str) -> str:
    t = text.strip()
    for dev, rom in DEVANAGARI_MAP.items():
        t = t.replace(dev, rom)
    t = t.translate(DEV_DIGITS)
    t = WAKE.sub("", t)
    t = t.lower()
    t = re.sub(r"[’']", "", t)                 # what's -> whats
    t = re.sub(r"[^\w\s:%./\\\-+]", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" .")
    t = re.sub(r"^(please|plz|pls|can you|could you|would you|kindly|zara|jara)\s+", "", t)
    t = re.sub(r"\s+(please|plz|pls|na|naa)$", "", t)
    return t


def words_to_number(text: str) -> int | None:
    """'thirty five' -> 35, 'tees' -> 30, '45' -> 45, 'forty percent' -> 40."""
    t = text.lower().strip()
    m = re.search(r"\d+", t)
    if m:
        return int(m.group())
    total, found = 0, False
    for w in re.findall(r"[a-z]+", t):
        if w in EN_NUM and w not in ("a", "an"):
            v = EN_NUM[w]
            total = total * v if v == 100 and total else total + v
            found = True
        elif w in HI_NUM:
            total += HI_NUM[w]
            found = True
    return total if found else None


UNIT_SECONDS = {"s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1, "second s": 1, "m": 60, "min": 60,
                "mins": 60, "minute": 60, "minutes": 60, "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600,
                "hours": 3600, "ghanta": 3600, "ghante": 3600, "minat": 60}
NUM_WORD = r"(\d+|" + "|".join(sorted(set(EN_NUM) | set(HI_NUM), key=len, reverse=True)) + r")"


def parse_duration(text: str) -> int | None:
    """Seconds from '5 minutes', 'one hour 30 minutes', 'paanch minute', 'half an hour'."""
    t = text.lower()
    if re.search(r"\bhalf an? hour\b|\baadha ghanta\b", t):
        return 1800
    total, found = 0, False
    for num, unit in re.findall(r"\b" + NUM_WORD + r"\s*(seconds?|secs?|minutes?|mins?|minat|hours?|hrs?|ghante|ghanta|s|m|h)\b", t):
        n = words_to_number(num)
        if n is None:
            continue
        total += n * UNIT_SECONDS.get(unit, 60)
        found = True
    return total if found and total > 0 else None


class AmbiguousTime(Exception):
    def __init__(self, hour: int, minute: int) -> None:
        super().__init__("ambiguous time")
        self.hour, self.minute = hour, minute


def parse_clock_time(text: str, now: datetime | None = None) -> datetime | None:
    """'at 5 pm', 'at 17:30', 'shaam 5 baje', 'kal subah 7 baje'. Raises AmbiguousTime for bare 'at 5'."""
    now = now or datetime.now()
    t = text.lower()
    m = re.search(r"(?:\bat\b|\bko\b|^|\s)(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm|a\.m\.|p\.m\.|baje)?", t)
    if not m or not (re.search(r"\bat\b|\bbaje\b|am\b|pm\b|:\d{2}", t)):
        return None
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    suffix = (m.group(3) or "").replace(".", "")
    if hour > 23 or minute > 59:
        return None
    if suffix == "pm" or re.search(r"\b(shaam|sham|raat|evening|night|dopahar|afternoon)\b", t):
        if hour < 12:
            hour += 12
    elif suffix == "am" or re.search(r"\b(subah|morning)\b", t):
        if hour == 12:
            hour = 0
    elif 1 <= hour <= 12:
        raise AmbiguousTime(hour, minute)   # "at 5" / "5:30": AM or PM? -> ask, never guess
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if re.search(r"\b(tomorrow|kal)\b", t) or target <= now:
        target += timedelta(days=1)
    return target
