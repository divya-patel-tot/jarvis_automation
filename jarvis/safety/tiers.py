"""Pre-flight risk classification of requests and of agent plans (plan 10.2.4).

Request classification is deliberately conservative: when unsure, go higher.
It decides whether a plan phase is needed; the *plan* is then re-classified
from its concrete steps, and the stricter of the two wins.
"""

from __future__ import annotations

import re

from jarvis.models import Plan, PlanStep, RiskTier
from jarvis.safety import policy

# English + Hindi/Hinglish cues (romanized and Devanagari).
FORBIDDEN_CUES = [
    r"\bformat\b.*\b(drive|disk|c:|d:)", r"\bregistry\b|\bregedit\b", r"\bbios\b|\bboot\b|\bbcdedit\b",
    r"\b(disable|turn off|band karo|hata do)\b.*\b(defender|antivirus|firewall|uac|security)\b",
    r"\b(password|passwords|credential|credentials|cookies|saved logins?)\b.*\b(show|read|dump|export|copy|send|nikalo|batao)\b",
    r"\b(show|read|dump|export|copy|send|nikalo|batao)\b.*\b(password|passwords|credential|credentials|cookies)\b",
    r"\b(create|add|delete|remove)\b.*\b(user account|admin account|administrator)\b",
    r"\b(wipe|erase)\b.*\b(disk|drive|everything|computer|pc)\b",
    r"\bdelete (everything|all files|system32|windows)\b", r"\bsab kuch delete\b|\bsab delete\b",
]
CONSEQUENTIAL_CUES = [
    r"\b(delete|remove|erase|trash|clean ?up|clear out|purge|uninstall)\b", r"\b(hatao|hata do|mita do|delete karo|saaf karo)\b",
    r"\b(move|rename|overwrite|replace|organi[sz]e|sort)\b.*\b(file|files|folder|folders|photos?|documents?)\b",
    r"\b(install|download and run|setup)\b", r"\b(send|email|mail|post|upload|share|submit|tweet|message)\b",
    r"\b(bhejo|bhej do|upload karo)\b", r"\b(kill|terminate|end task|force close)\b",
    r"\b(run|execute|chalao)\b.*\b(script|\.ps1|\.bat|\.py|program)\b", r"\b(startup|autostart|schedule)\b",
    r"\b(change|edit|modify)\b.*\b(settings?|config|file|document)\b", r"\bpay\b|\bbuy\b|\border\b|\bpurchase\b",
]
REVERSIBLE_CUES = [
    r"\b(open|launch|start|kholo|chalu karo)\b", r"\b(create|make|write|banao|likho)\b", r"\b(copy|zip|compress|backup)\b",
    r"\b(play|pause|volume|mute|brightness)\b", r"\b(type|click|press)\b", r"\b(download)\b",
]
READ_ONLY_CUES = [
    r"\b(what|which|how|when|where|who|why|kya|kaun|kitna|kitne|kab|kahan|kaise)\b",
    r"\b(list|show|find|search|look up|summari[sz]e|read|tell me|check|count|status|explain|dikhao|batao|dhundo|padho)\b",
]


def _any(patterns: list[str], text: str) -> bool:
    return any(re.search(p, text, re.I) for p in patterns)


CATASTROPHIC_CUES = [
    r"\b(disable|turn off|band karo|hata do|bypass)\b.*\b(defender|antivirus|uac|security)\b",
    r"\b(password|passwords|credential|credentials|cookies|saved logins?)\b.*\b(show|read|dump|export|copy|send|nikalo|batao|steal)\b",
    r"\b(show|read|dump|export|copy|send|nikalo|batao|steal)\b.*\b(password|passwords|credential|credentials|cookies|saved logins?)\b",
    r"\bbios\b|\bbcdedit\b|\bboot (config|menu|loader)\b",
]
DELETE_CUES = [
    r"\b(delete|remove|erase|trash|recycle|wipe|purge|uninstall|clean ?up|clear out|empty (the )?(recycle|bin|trash)|format)\b",
    r"\b(hatao|hata do|mita do|delete karo|saaf karo|uda do)\b",
]


def is_catastrophic_request(text: str) -> bool:
    """Blocked even in full autonomy: credential theft, disabling protections, boot config."""
    return _any(CATASTROPHIC_CUES, text.lower())


def is_delete_request(text: str) -> bool:
    return _any(DELETE_CUES, text.lower())


def classify_request(text: str, overrides: dict[str, int] | None = None) -> tuple[RiskTier, str]:
    t = text.lower()
    if _any(FORBIDDEN_CUES, t):
        return RiskTier.FORBIDDEN, "request touches security, credentials, registry, boot or mass deletion"
    if _any(CONSEQUENTIAL_CUES, t):
        tier, why = RiskTier.CONSEQUENTIAL, "request may delete, move, send, install or modify things"
    elif _any(REVERSIBLE_CUES, t):
        tier, why = RiskTier.REVERSIBLE, "request creates or opens things"
    elif _any(READ_ONLY_CUES, t):
        tier, why = RiskTier.READ_ONLY, "request only reads or answers"
    else:
        tier, why = RiskTier.REVERSIBLE, "unclear request; treated as changing state"
    for key, value in (overrides or {}).items():
        if key.lower() in t and value > tier:
            tier, why = RiskTier(value), f"stricter user override for '{key}'"
    return tier, why


STEP_RE = re.compile(r"^\s*(?:#+\s*)?(?:step\s*)?(\d{1,2})[\.\):]\s+(.+)$", re.I)
BULLET_RE = re.compile(r"^\s*[-*•]\s+(.+)$")


def parse_plan(text: str) -> list[PlanStep]:
    steps: list[PlanStep] = []
    in_steps_section = False
    for line in (text or "").splitlines():
        if re.match(r"^\s*#+\s*(steps|plan)\b", line, re.I):
            in_steps_section = True
            continue
        if re.match(r"^\s*#+\s*", line) and in_steps_section and not STEP_RE.match(line):
            in_steps_section = False
        m = STEP_RE.match(line)
        if m:
            steps.append(PlanStep(number=int(m.group(1)), text=m.group(2).strip()))
            continue
        b = BULLET_RE.match(line)
        if b and in_steps_section:
            steps.append(PlanStep(number=len(steps) + 1, text=b.group(1).strip()))
        elif steps and line.startswith(("   ", "\t")) and line.strip():
            steps[-1].text += " " + line.strip()
    if not steps and text.strip():
        steps = [PlanStep(1, text.strip()[:500])]
    return steps


STEP_FORBIDDEN = [r"\bregistry\b|\bhklm\b|\bhkcu\b", r"\bformat\b.*\bdrive\b", r"\bdisable\b.*\b(defender|firewall|uac)\b",
                  r"\bcredential|\bpassword", r"\bbcdedit\b|\bboot config"]
STEP_CONSEQUENTIAL = [r"\b(delete|remove|recycle|trash|erase)\b", r"\b(move|rename|overwrite|replace)\b",
                      r"\b(install|uninstall)\b", r"\b(send|upload|email|post|submit)\b", r"\b(kill|terminate|stop)\b.*\bprocess",
                      r"\b(modify|edit|change)\b.*\b(setting|config)", r"\bvision\b.*\bclick\b|\bclick at\b"]
STEP_REVERSIBLE = [r"\b(create|write|save|zip|compress|copy|open|launch|start|type|click|download)\b"]


def classify_step(step: PlanStep) -> RiskTier:
    t = step.text.lower()
    for raw in policy.extract_paths(step.text):
        if policy.sensitive_reason(raw):
            return RiskTier.FORBIDDEN
    for cmd in re.findall(r"`([^`]+)`", step.text):
        tier, _ = policy.classify_command(cmd)
        if tier == 3:
            return RiskTier.FORBIDDEN
    if _any(STEP_FORBIDDEN, t):
        return RiskTier.FORBIDDEN
    if _any(STEP_CONSEQUENTIAL, t):
        return RiskTier.CONSEQUENTIAL
    if _any(STEP_REVERSIBLE, t):
        return RiskTier.REVERSIBLE
    return RiskTier.READ_ONLY


def build_plan(text: str, request_tier: RiskTier) -> Plan:
    steps = parse_plan(text)
    targets: list[str] = []
    forbidden: list[str] = []
    for s in steps:
        s.tier = classify_step(s)
        s.targets = [p for p in policy.extract_paths(s.text)]
        targets.extend(s.targets)
        if s.tier == RiskTier.FORBIDDEN:
            forbidden.append(f"step {s.number}: {s.text[:120]}")
    tier = max([s.tier for s in steps] + [request_tier]) if steps else request_tier
    title = next((ln.lstrip("# ").strip() for ln in text.splitlines() if ln.strip().startswith("#")), "")
    return Plan(raw=text, steps=steps, tier=RiskTier(tier), targets=sorted(set(targets)),
                summary=title or (steps[0].text[:100] if steps else ""), forbidden_reasons=forbidden)
