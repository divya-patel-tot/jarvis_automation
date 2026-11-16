"""Dual-path request router (plan 5). Deterministic; never guesses.

Order of checks: control words -> specific intents -> generic open -> agent.
Multi-clause requests and low-confidence matches go to the agent path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from jarvis.models import RiskTier
from jarvis.router.normalize import (AmbiguousTime, normalize, parse_clock_time, parse_duration,
                                     words_to_number)

CONFIDENCE_THRESHOLD = 0.82
COMPOUND = re.compile(r"\b(and then|then|and also|and|phir|aur|uske baad|after that|also)\b")


@dataclass
class Route:
    path: str                       # fast | agent | control | clarify
    intent: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    tier: RiskTier = RiskTier.READ_ONLY
    reason: str = ""
    question: str = ""              # for clarify
    normalized: str = ""


Spec = tuple[str, RiskTier, list[str]]

# (intent, tier, patterns) - patterns are full-match regexes on normalized text.
INTENTS: list[Spec] = [
    ("time", RiskTier.READ_ONLY, [r"(what|whats) (is )?the time( now)?", r"(what )?time (is it|hai|kya hai|ho gaya)",
                                   r"kitne baje (hain|hai)?", r"time (batao|bata do|kya hua)", r"current time"]),
    ("date", RiskTier.READ_ONLY, [r"(what|whats) (is )?(the |todays )?(date|day)( today)?", r"aaj (ki date|kya din|kaun sa din) (hai|kya hai)?",
                                   r"(todays )?date (kya hai|batao)", r"what day is (it|today)"]),
    ("volume_set", RiskTier.REVERSIBLE, [r"(set |change |make )?(the )?(volume|sound|awaaz) (to |at |)(?P<n>[\w ]+?)( percent| %|%)?( karo| kar do)?",
                                          r"(?P<n>[\w ]+?) (percent|%) (volume|awaaz)"]),
    ("volume_up", RiskTier.REVERSIBLE, [r"(turn |)(the )?(volume|sound|awaaz) up", r"(increase|raise) (the )?(volume|sound)",
                                         r"(volume|awaaz|sound) (badhao|badha do|tez karo|zyada karo)", r"louder"]),
    ("volume_down", RiskTier.REVERSIBLE, [r"(turn |)(the )?(volume|sound|awaaz) down", r"(decrease|lower|reduce) (the )?(volume|sound)",
                                           r"(volume|awaaz|sound) (kam karo|kam kar do|dheere karo|ghatao)", r"quieter"]),
    ("mute", RiskTier.REVERSIBLE, [r"mute( (the )?(volume|sound|audio|pc|computer))?", r"(awaaz|sound|volume) band karo", r"chup( ho jao)?"]),
    ("unmute", RiskTier.REVERSIBLE, [r"unmute( (the )?(volume|sound|audio))?", r"(awaaz|sound) (chalu|wapas) karo"]),
    ("brightness_set", RiskTier.REVERSIBLE, [r"(set |change )?(the )?(screen )?brightness (to |at |)(?P<n>[\w ]+?)( percent| %|%)?( karo)?"]),
    ("brightness_up", RiskTier.REVERSIBLE, [r"(increase|raise|turn up) (the )?(screen )?brightness", r"brightness (up|badhao|zyada karo)", r"brighter"]),
    ("brightness_down", RiskTier.REVERSIBLE, [r"(decrease|lower|reduce|turn down) (the )?(screen )?brightness", r"brightness (down|kam karo)", r"dimmer"]),
    ("lock", RiskTier.REVERSIBLE, [r"lock (the |my )?(screen|pc|computer|laptop|system)", r"(screen|pc|computer|laptop) lock (karo|kar do)", r"lock it"]),
    ("screenshot", RiskTier.REVERSIBLE, [r"(take|capture|grab) (a )?(screenshot|screen shot|screen capture)", r"screenshot (lo|le lo|lena)", r"screenshot"]),
    ("battery", RiskTier.READ_ONLY, [r"(how much |whats the |what is the )?battery( (level|left|percentage|status|kitni hai|kitna hai))?",
                                      r"battery kitn(i|a)( hai)?"]),
    ("cpu", RiskTier.READ_ONLY, [r"(whats the |what is the |how much )?(cpu|processor)( usage| load)?( kitna hai)?"]),
    ("ram", RiskTier.READ_ONLY, [r"(whats the |what is the |how much )?(ram|memory)( usage| free| left| kitni hai)?( is (used|free))?"]),
    ("disk", RiskTier.READ_ONLY, [r"(how much )?(disk|storage|drive) (space|left|free|kitni hai)( is (left|free))?", r"free (disk|storage) space"]),
    ("timer", RiskTier.REVERSIBLE, [r"(set |start |put )?(a )?timer (for |of )?(?P<d>.+)", r"(?P<d>.+) (ka|ki) timer( lagao| set karo| laga do)?",
                                     r"(?P<d>.+) timer"]),
    ("reminder", RiskTier.REVERSIBLE, [r"remind me (to |about )?(?P<what>.+?) (?P<when>(in|at|after|tomorrow|kal) .+)",
                                        r"remind me (?P<when>(in|at|after|tomorrow) .+?) (to|about) (?P<what>.+)",
                                        r"(?P<when>.+? baje) (?P<what>.+?) (yaad dilana|remind karna)"]),
    ("media_playpause", RiskTier.REVERSIBLE, [r"(play|pause|resume)( (the )?(music|song|video|media))?", r"(gaana|music|song) (chalao|roko|band karo)"]),
    ("media_next", RiskTier.REVERSIBLE, [r"(next|skip)( (the )?(song|track|video))?", r"agla (gaana|song)"]),
    ("media_prev", RiskTier.REVERSIBLE, [r"(previous|prev|last|go back)( (the )?(song|track))", r"pichla (gaana|song)"]),
    ("snap_left", RiskTier.REVERSIBLE, [r"(snap|move) (the )?(window|this) (to the )?left"]),
    ("snap_right", RiskTier.REVERSIBLE, [r"(snap|move) (the )?(window|this) (to the )?right"]),
    ("maximize", RiskTier.REVERSIBLE, [r"maximi[sz]e( (the |this )?window)?"]),
    ("show_desktop", RiskTier.REVERSIBLE, [r"(minimi[sz]e (all|everything)( windows)?|show (the )?desktop|desktop dikhao)"]),
    ("read_clipboard", RiskTier.READ_ONLY, [r"(read|tell me|say) (out )?(my |the )?clipboard( aloud)?", r"clipboard (padho|batao|mein kya hai)",
                                             r"(what is|whats) (in|on) (my |the )?clipboard"]),
    ("remember", RiskTier.REVERSIBLE, [r"remember (that )?(?P<fact>.+)", r"yaad rakho (ki )?(?P<fact>.+)", r"(?P<fact>.+) yaad rakhna"]),
    ("forget_fact", RiskTier.REVERSIBLE, [r"forget (about |that )?(my |the )?(?P<key>.+)", r"(?P<key>.+) bhool jao"]),
    ("list_memory", RiskTier.READ_ONLY, [r"what do you (remember|know) about me", r"(show|list) (my )?memor(y|ies)", r"tumhe kya yaad hai"]),
    ("new_topic", RiskTier.READ_ONLY, [r"(new topic|start over|new conversation|reset (the )?conversation|forget (this|the) conversation)"]),
    ("undo", RiskTier.CONSEQUENTIAL, [r"undo( (that|it|the last (one|action|task)|last))?", r"(wapas|vapas) karo", r"revert( that)?"]),
    ("help", RiskTier.READ_ONLY, [r"(help|what can you do|tum kya kar sakte ho)"]),
    ("close_app", RiskTier.REVERSIBLE, [r"(close|quit|exit|shut) (down )?(the |my |all )?(?P<app>[\w .+\-]+?)( app| application| program| windows?)?( for me)?",
                                         r"(?P<app>[\w .+\-]+?) (band karo|band kar do|close karo|close kar do|bandh karo)"]),
    ("open_folder", RiskTier.REVERSIBLE, [r"(open|show|go to) (my |the )?(?P<folder>[\w ]+?) folder", r"(?P<folder>[\w ]+?) folder (kholo|dikhao|open karo)",
                                           r"(open|show) (my |the )?(?P<folder>downloads|documents|desktop|pictures|music|videos)",
                                           r"(?P<folder>downloads|documents|desktop|pictures|music|videos) (kholo|dikhao)"]),
    ("open_website", RiskTier.REVERSIBLE, [r"(open|go to|visit|launch) (?P<site>[\w\-]+(\.[\w\-]+)+(/\S*)?)",
                                            r"(open|go to|visit) (?P<site>youtube|gmail|google|github|wikipedia|linkedin|twitter|x|facebook|instagram|amazon|flipkart|netflix|chatgpt|maps|google maps|drive|google drive)( website)?",
                                            r"(?P<site>youtube|gmail|github|wikipedia|linkedin|facebook|instagram|amazon|flipkart|netflix) (kholo|open karo|chalao)"]),
    ("open_app", RiskTier.REVERSIBLE, [r"(open|launch|start|run) (up )?(the |my |a )?(?P<app>[\w .+\-]+?)( app| application| program)?( for me)?",
                                        r"(?P<app>[\w .+\-]+?) (kholo|khol do|open karo|chalu karo|chalao|start karo)"]),
]

STOP_WORDS = re.compile(r"^(stop|cancel|abort|halt|ruko|ruk jao|bas|band karo|stop it|cancel (that|it|task)|kill( it)?)$")
CONFIRM_WORDS = re.compile(r"^(jarvis )?(confirm|approve|yes do it|haan karo)$")

SITES = {"youtube": "https://www.youtube.com", "gmail": "https://mail.google.com", "google": "https://www.google.com",
         "github": "https://github.com", "wikipedia": "https://www.wikipedia.org", "linkedin": "https://www.linkedin.com",
         "twitter": "https://x.com", "x": "https://x.com", "facebook": "https://www.facebook.com",
         "instagram": "https://www.instagram.com", "amazon": "https://www.amazon.in", "flipkart": "https://www.flipkart.com",
         "netflix": "https://www.netflix.com", "chatgpt": "https://chatgpt.com", "maps": "https://maps.google.com",
         "google maps": "https://maps.google.com", "drive": "https://drive.google.com", "google drive": "https://drive.google.com"}
FOLDERS = {"downloads", "documents", "desktop", "pictures", "music", "videos", "download", "document", "photos",
           "workspace", "outputs", "jarvis"}


class Router:
    def __init__(self, app_index=None, memory=None, overrides: dict[str, int] | None = None) -> None:
        self.apps = app_index
        self.memory = memory
        self.overrides = overrides or {}
        self._compiled = [(name, tier, [re.compile(f"^(?:{p})$") for p in pats]) for name, tier, pats in INTENTS]

    def route(self, text: str) -> Route:
        n = normalize(text)
        if not n:
            return Route("clarify", question="Sorry, I didn't catch that.", normalized=n, reason="empty")
        if STOP_WORDS.match(n):
            return Route("control", intent="stop", confidence=1.0, normalized=n)
        if CONFIRM_WORDS.match(n):
            return Route("control", intent="confirm", confidence=1.0, normalized=n)
        for cand in self._candidates(n):
            for name, tier, patterns in self._compiled:
                for pat in patterns:
                    m = pat.match(cand)
                    if not m:
                        continue
                    route = self._build(name, tier, m.groupdict(), cand)
                    if route is None:
                        continue
                    if route.path == "fast" and name not in ("remember", "reminder", "timer") and COMPOUND.search(cand):
                        return Route("agent", reason="multi-clause request", normalized=n)
                    ov = self.overrides.get(name)
                    if ov is not None and ov > route.tier:
                        route.tier = RiskTier(ov)
                    return route
        return Route("agent", reason="no fast-path match", normalized=n)

    NEGATIONS = {"dont", "don't", "do", "not", "never", "mat", "nahi", "na", "no", "stop", "cancel"}

    def _candidates(self, n: str) -> list[str]:
        """The text, then the text without 1-2 leading noise words (speech recognition often adds
        a misheard word: 'service open the notepad'). Never drops a negation."""
        words = n.split()
        out = [n]
        for k in (1, 2):
            if len(words) - k >= 1 and not any(w in self.NEGATIONS for w in words[:k]):
                out.append(" ".join(words[k:]))
        # Polite prefixes may only appear after the noise ("edge jarvis can you open teams").
        polite = re.compile(r"^(jarvis\s+)?(please|plz|can you|could you|would you|will you|kindly|zara|jara)\s+")
        for c in list(out):
            stripped = polite.sub("", c)
            if stripped != c and stripped not in out:
                out.append(stripped)
        return out

    # ------------------------------------------------------------------ params
    def _build(self, name: str, tier: RiskTier, g: dict[str, str], n: str) -> Route | None:
        r = Route("fast", intent=name, tier=tier, confidence=1.0, normalized=n)
        if name in ("volume_set", "brightness_set"):
            v = words_to_number(g.get("n") or "")
            if v is None or not 0 <= v <= 100:
                return None
            r.params["level"] = v
        elif name == "timer":
            d = parse_duration(g.get("d") or "")
            if d is None or d > 24 * 3600:
                return None
            r.params["seconds"] = d
            r.params["label"] = "timer"
        elif name == "reminder":
            what, when = (g.get("what") or "").strip(), (g.get("when") or "").strip()
            if not what or not when:
                return None
            r.params["what"] = what
            secs = parse_duration(when) if re.search(r"\b(in|after|baad)\b", when) else None
            if secs:
                r.params["seconds"] = secs
            else:
                try:
                    at = parse_clock_time(when)
                except AmbiguousTime as amb:
                    r.path = "clarify"
                    r.question = f"{amb.hour}{':%02d' % amb.minute if amb.minute else ''} AM or PM?"
                    r.params["pending_hour"], r.params["pending_minute"] = amb.hour, amb.minute
                    r.params["when_text"] = when
                    return r
                if at is None:
                    return None
                r.params["at"] = at.isoformat()
        elif name == "open_folder":
            folder = (g.get("folder") or "").strip()
            folder = {"download": "downloads", "document": "documents", "photos": "pictures"}.get(folder, folder)
            if folder in FOLDERS:
                r.params["folder"] = folder
            elif self.memory is not None and self.memory.alias_path(folder):
                r.params["folder"] = folder
                r.params["path"] = self.memory.alias_path(folder)
            else:
                return None
        elif name == "open_website":
            site = (g.get("site") or "").strip()
            url = SITES.get(site)
            if not url:
                if not re.match(r"^[\w\-]+(\.[\w\-]+)+(/\S*)?$", site):
                    return None
                url = "https://" + site
            r.params["url"] = url
        elif name == "open_app":
            app = (g.get("app") or "").strip()
            if not app or len(app) > 40 or COMPOUND.search(app):
                return None
            if app in SITES:
                r.intent, r.params["url"] = "open_website", SITES[app]
                return r
            if app.replace(" folder", "") in FOLDERS:
                r.intent, r.params["folder"] = "open_folder", app.replace(" folder", "")
                return r
            if self.memory is not None and self.memory.alias_path(app):
                r.intent, r.params["folder"], r.params["path"] = "open_folder", app, self.memory.alias_path(app)
                return r
            if self.apps is None:
                return None
            entry, conf = self.apps.resolve(app)
            if entry is None or conf < CONFIDENCE_THRESHOLD:
                return Route("agent", reason=f"app '{app}' not confidently resolved ({conf:.2f})", normalized=n,
                             confidence=conf)
            r.params.update(app=entry.name, target=entry.target, kind=entry.kind)
            r.confidence = conf
        elif name == "close_app":
            app = (g.get("app") or "").strip()
            app = re.sub(r"^(the|my)\s+", "", app)
            from jarvis.fastpath.actions import PROCESS_ALIASES

            # Only well-known apps on the fast path; anything else ("close the deal email") -> agent.
            if not app or COMPOUND.search(app) or app not in PROCESS_ALIASES:
                return None
            r.params["app"] = app
        elif name == "remember":
            fact = (g.get("fact") or "").strip()
            if len(fact) < 3:
                return None
            r.params["fact"] = fact
        elif name == "forget_fact":
            key = (g.get("key") or "").strip()
            if key in ("that", "it", "this", "everything", "this conversation", "the conversation"):
                return Route("fast", intent="new_topic", tier=RiskTier.READ_ONLY, confidence=1.0, normalized=n)
            r.params["key"] = key
        return r
