"""Backend selection, fallback and health (plan 6.2, 13.4)."""

from __future__ import annotations

import logging
import sys
import threading
import time

from jarvis import paths
from jarvis.backend.base import AgentBackend, BackendUnavailable, EventSink, RunSpec, pick_model
from jarvis.config import Settings
from jarvis.models import RunOutcome
from jarvis.secrets import get_api_key

log = logging.getLogger(__name__)


class BackendManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.workspace = settings.workspace_path()
        env = {
            "JARVIS_POLICY_STATE": str(paths.policy_state_path()),
            "JARVIS_PY": sys.executable,
            "JARVIS_HOME": str(paths.PROJECT_ROOT),
            "PYTHONIOENCODING": "utf-8",
        }
        self._backends: dict[str, AgentBackend] = {}
        try:
            from jarvis.backend.sdk_backend import SdkBackend

            self._backends["sdk"] = SdkBackend(self.workspace, get_api_key, paths.bridge_state_dir(), env)
        except Exception as exc:  # cursor-sdk missing
            log.warning("SDK backend unavailable: %s", exc)
        from jarvis.backend.cli_backend import CliBackend

        self._backends["cli"] = CliBackend(self.workspace, get_api_key, env)
        self.active: AgentBackend | None = None
        self.status: dict[str, str] = {}
        self.degraded_reason = ""
        self._models: list[str] = []
        self._lock = threading.Lock()

    def order(self) -> list[str]:
        pref = self.settings.backend
        if pref in ("sdk", "cli"):
            return [pref] + [n for n in ("sdk", "cli") if n != pref]
        return ["sdk", "cli"]

    def select(self) -> AgentBackend | None:
        """Probe in preference order; first usable backend becomes active."""
        with self._lock:
            for name in self.order():
                b = self._backends.get(name)
                if b is None:
                    continue
                t0 = time.perf_counter()
                ok, reason = b.probe()
                self.status[name] = ("ok: " if ok else "unavailable: ") + reason
                log.info("backend %s probe %.1fs -> %s", name, time.perf_counter() - t0, self.status[name])
                if ok:
                    self.active = b
                    self.degraded_reason = ""
                    self._models = b.list_models()
                    return b
            self.active = None
            self.degraded_reason = "; ".join(f"{k}: {v}" for k, v in self.status.items())
            return None

    @property
    def available(self) -> bool:
        return self.active is not None

    def models(self) -> list[str]:
        return list(self._models)

    def model_for(self, strong: bool) -> str:
        prefs = self.settings.models.strong if strong else self.settings.models.fast
        return pick_model(prefs, self._models)

    def run(self, spec: RunSpec, on_event: EventSink) -> RunOutcome:
        if self.active is None and self.select() is None:
            raise BackendUnavailable(self.degraded_reason or "no agent backend available", "unavailable")
        try:
            return self.active.run(spec, on_event)   # type: ignore[union-attr]
        except BackendUnavailable as exc:
            log.error("backend %s failed (%s); trying fallback", self.active.name, exc)  # type: ignore[union-attr]
            failed = self.active
            self.active = None
            for name in self.order():
                b = self._backends.get(name)
                if b is None or b is failed:
                    continue
                ok, reason = b.probe()
                self.status[name] = ("ok: " if ok else "unavailable: ") + reason
                if ok:
                    self.active = b
                    return b.run(spec, on_event)
            self.degraded_reason = str(exc)
            raise

    def cancel(self) -> None:
        if self.active:
            self.active.cancel()

    def kill(self) -> None:
        for b in self._backends.values():
            try:
                b.kill()
            except Exception:
                log.exception("kill failed for %s", b.name)

    def drop_session(self, key: str) -> None:
        if self.active:
            self.active.drop_session(key)

    def rotate_idle(self) -> None:
        b = self.active
        if b is not None and hasattr(b, "rotate_idle"):
            b.rotate_idle(self.settings.session_idle_rotate_s)   # type: ignore[attr-defined]

    def health(self) -> dict:
        b = self.active
        if b is None:
            return {"ok": False, "reason": self.degraded_reason}
        ok, reason = b.probe()
        if not ok:
            self.active = None
            self.degraded_reason = reason
        return {"ok": ok, "backend": b.name, "reason": reason}

    def close(self) -> None:
        for b in self._backends.values():
            try:
                b.close()
            except Exception:
                pass
