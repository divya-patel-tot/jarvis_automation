"""Settings with safe defaults (plan 10.4). Stored as JSON in app data.

Risk-tier overrides may only make things stricter; ``load`` enforces that.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from jarvis import paths

log = logging.getLogger(__name__)


@dataclass
class BudgetSettings:
    task_wall_clock_s: int = 300          # total per task
    first_event_timeout_s: int = 60       # agent stuck starting
    idle_timeout_s: int = 90              # no events between
    max_tool_calls: int = 40
    max_events: int = 4000
    max_screenshots: int = 3
    max_files_created: int = 50
    daily_agent_runs: int = 200
    daily_tokens: int = 3_000_000
    warn_fraction: float = 0.8


@dataclass
class VoiceSettings:
    enabled: bool = True
    wake_phrases: list[str] = field(default_factory=lambda: ["hey jarvis", "ok jarvis", "jarvis"])
    wake_confidence: str = "medium"       # low | medium | high (WinRT confidence)
    stt_engine: str = "whisper"           # whisper (local, reliable, Hindi) | windows (auto-falls back to whisper)
    stt_language: str = "en-US"           # Windows speech language tag (English)
    whisper_model: str = "base"           # base ~1 s/command on CPU; "small" = better Hindi, slower
    tts_enabled: bool = True
    tts_voice: str = ""                   # substring of an installed SAPI voice name
    tts_rate: int = 1                     # SAPI -10..10
    follow_up_window_s: float = 6.0
    max_utterance_s: float = 15.0
    confirm_phrase: str = "confirm"
    voice_confirm_tier2: bool = False     # Tier 2 by on-screen click only (safe default)


@dataclass
class ModelSettings:
    # Preference lists; the first id the account actually exposes wins (discovery, never hardcoded).
    fast: list[str] = field(default_factory=lambda: ["composer-2.5", "composer-2", "claude-haiku-5-5", "gpt-5.4-mini", "default"])
    strong: list[str] = field(default_factory=lambda: ["default", "claude-sonnet-5-5", "composer-2.5"])


@dataclass
class Settings:
    backend: str = "auto"                 # auto | sdk | cli
    workspace: str = ""
    models: ModelSettings = field(default_factory=ModelSettings)
    budgets: BudgetSettings = field(default_factory=BudgetSettings)
    voice: VoiceSettings = field(default_factory=VoiceSettings)
    # Folders the agent may read (plus the workspace). Writes outside the
    # workspace need execute mode with an approved plan and must fall here.
    allowed_folders: list[str] = field(default_factory=lambda: ["desktop", "downloads", "documents", "pictures"])
    excluded_folders: list[str] = field(default_factory=list)
    # "full": one command -> done. Runs everything directly on the PC; ONLY deletions
    #         (and actions suggested by untrusted web/PDF/clipboard content) ask once.
    # "safe": original plan -> approve -> execute for anything that changes state.
    autonomy: str = "full"
    plan_first_tier: int = 1              # safe mode only: tiers >= this run plan-then-execute
    allow_install_software: bool = False  # unlocked in settings only
    desktop_control_enabled: bool = False # MCP UIA tools
    vision_enabled: bool = False
    web_allowlist: list[str] = field(default_factory=lambda: [
        "wikipedia.org", "learn.microsoft.com", "docs.python.org", "github.com", "stackoverflow.com"])
    private_mode: bool = False            # disables screen/clipboard ingestion
    sensitive_windows: list[str] = field(default_factory=lambda: [
        "keepass", "1password", "bitwarden", "lastpass", "dashlane", "credential manager",
        "inprivate", "incognito", "private browsing", "netbanking", "bank", "paypal", "upi"])
    session_idle_rotate_s: int = 900
    retention_days: int = 7
    tier_overrides: dict[str, int] = field(default_factory=dict)   # intent -> stricter tier
    routines: list[dict[str, Any]] = field(default_factory=list)
    hotkeys: dict[str, str] = field(default_factory=lambda: {
        "palette": "ctrl+alt+j", "push_to_talk": "ctrl+alt+space", "kill": "ctrl+alt+k"})
    language_reply: str = "auto"

    # ------------------------------------------------------------------ helpers
    def workspace_path(self) -> Path:
        return Path(self.workspace).expanduser() if self.workspace else paths.default_workspace_dir()

    def allowed_folder_paths(self) -> list[Path]:
        known = paths.known_folders()
        out: list[Path] = []
        for item in self.allowed_folders:
            p = known.get(item.lower()) if item.lower() in known else Path(item).expanduser()
            if p is not None:
                out.append(p)
        return out

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _merge(dc_type: type, data: dict[str, Any]) -> Any:
    kwargs: dict[str, Any] = {}
    for f in fields(dc_type):
        if f.name not in data:
            continue
        value = data[f.name]
        default = dc_type().__getattribute__(f.name) if callable(dc_type) else None
        if hasattr(default, "__dataclass_fields__") and isinstance(value, dict):
            kwargs[f.name] = _merge(type(default), value)
        else:
            kwargs[f.name] = value
    return dc_type(**kwargs)


def load(path: Path | None = None) -> Settings:
    path = path or paths.settings_path()
    if not path.exists():
        s = Settings()
        save(s, path)
        return s
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        s = _merge(Settings, data)
    except Exception as exc:
        log.error("settings unreadable (%s); using safe defaults", exc)
        return Settings()
    # Overrides may only tighten: clamp to [0, 3]; the classifier takes max(default, override).
    s.tier_overrides = {k: max(0, min(3, int(v))) for k, v in (s.tier_overrides or {}).items()}
    s.plan_first_tier = max(0, min(2, int(s.plan_first_tier)))
    return s


def save(settings: Settings, path: Path | None = None) -> None:
    path = path or paths.settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings.to_dict(), indent=2), encoding="utf-8")
    tmp.replace(path)
