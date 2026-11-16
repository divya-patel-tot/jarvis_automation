"""The request pipeline (plan 4.2 / 4A): route -> fast path, or
context -> safety gate -> plan -> approval -> execute -> result."""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from typing import Callable

from jarvis.audit import AuditLog
from jarvis.backend.base import BackendUnavailable, RunSpec
from jarvis.backend.manager import BackendManager
from jarvis.config import Settings
from jarvis.events import bus
from jarvis.fastpath import actions
from jarvis.fastpath.apps import AppIndex
from jarvis.memory import Memory
from jarvis.models import Mode, RiskTier, TaskContext, TaskResult, UserRequest
from jarvis.observer import Observer
from jarvis import prompts, undo
from jarvis.result_parser import parse_result
from jarvis.router.router import Router
from jarvis.safety.budgets import UsageLedger
from jarvis.safety.confirm import ConfirmationBroker, confirmation_text, spoken_summary
from jarvis.safety.policy import DELETE_MARK
from jarvis.safety.tiers import build_plan, classify_request, is_catastrophic_request, is_delete_request
from jarvis.scheduler import Scheduler
from jarvis.senses import ingest, screen
from jarvis.workspace.manager import Workspace

log = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, settings: Settings, ask_user: Callable[[str], str] | None = None) -> None:
        self.s = settings
        self.audit = AuditLog()
        self.ws = Workspace(settings)
        self.ws.ensure()
        self.memory = Memory(self.ws.memory)
        self.apps = AppIndex()
        if not self.apps.entries:
            self.apps.refresh_async()
        self.router = Router(self.apps, self.memory, settings.tier_overrides)
        self.backends = BackendManager(settings)
        self.ledger = UsageLedger(settings.budgets)
        # Full autonomy: a fresh spoken "confirm" may approve a deletion (never the utterance that asked for it).
        self.confirm = ConfirmationBroker(settings.voice.voice_confirm_tier2 or settings.autonomy == "full",
                                          settings.voice.confirm_phrase)
        self._last_blocked: list[dict] = []
        self.scheduler = Scheduler(settings.routines, self.run_routine)
        self.ask_user = ask_user          # synchronous clarifying-question callback (console/voice)
        self.busy = threading.Lock()
        self.last_task: TaskContext | None = None
        self.current: TaskContext | None = None
        self._stop = threading.Event()
        screen.configure_sensitive(settings.sensitive_windows)

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self.scheduler.start()
        self._recover_crashed_tasks()
        threading.Thread(target=self._init_backend, name="backend-probe", daemon=True).start()
        threading.Thread(target=self._housekeeping, name="housekeeping", daemon=True).start()

    def _init_backend(self) -> None:
        b = self.backends.select()
        if b is None:
            bus.publish("error", text=f"Cursor agent unavailable; quick commands only. ({self.backends.degraded_reason[:200]})")
        else:
            bus.publish("notify", title="Jarvis ready", text=f"Cursor backend: {b.name}")

    def _housekeeping(self) -> None:
        while not self._stop.wait(300):
            try:
                self.backends.rotate_idle()
                self.ws.purge_old_tasks()
                self.backends.health()
            except Exception:
                log.exception("housekeeping failed")

    def _recover_crashed_tasks(self) -> None:
        for d in sorted(self.ws.tasks.glob("*"))[-20:]:
            state = d / "state.json"
            if state.exists():
                try:
                    st = json.loads(state.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if st.get("status") == "running":
                    st["status"] = "aborted"
                    state.write_text(json.dumps(st), encoding="utf-8")
                    self.audit.write("task_aborted_on_restart", d.name, phase=st.get("phase"))
                    bus.publish("notify", title="Jarvis recovered",
                                text=f"Task {d.name} was interrupted. Say 'undo' to revert its changes if needed.")

    def shutdown(self) -> None:
        self._stop.set()
        self.scheduler.stop()
        self.backends.close()

    # ------------------------------------------------------------------ kill switch
    def kill_switch(self) -> None:
        """Cancel everything within ~1 s: approvals, agent run, process tree, speech."""
        t0 = time.perf_counter()
        self.confirm.cancel_all()
        self.backends.cancel()
        self.backends.kill()
        bus.publish("stop_speech")
        bus.publish("state", state="idle")
        self.audit.write("kill_switch", self.current.task_id if self.current else "",
                         seconds=round(time.perf_counter() - t0, 3))
        bus.publish("activity", task_id="", text="Stopped.")

    # ------------------------------------------------------------------ entry
    def handle(self, req: UserRequest, fg_window: dict | None = None) -> TaskResult:
        route = self.router.route(req.text)
        self.audit.write("request", req.id, text=req.text, source=req.source.value, language=req.language,
                         route=route.path, intent=route.intent, confidence=route.confidence, reason=route.reason)
        if route.path == "control":
            if route.intent == "stop":
                self.kill_switch()
                return TaskResult("cancelled", "Stopped.")
            if self.confirm.voice(req.text, req.id):
                return TaskResult("success", "")
            return TaskResult("failed", "There's nothing waiting for confirmation.")
        if self.confirm.pending() and self.confirm.voice(req.text, req.id):
            return TaskResult("success", "")
        if route.path == "clarify":
            if route.intent == "reminder" and self.ask_user:
                return self._clarify_reminder(route)
            return self._say(TaskResult("needs_input", route.question or "Sorry, I didn't catch that."))
        if route.path == "fast":
            return self._say(self._fast(route, req))
        if not self.busy.acquire(blocking=False):
            return self._say(TaskResult("failed", "I'm still working on the previous task. Say stop to cancel it."))
        try:
            return self._say(self._agent_task(req, fg_window))
        finally:
            self.busy.release()

    def _say(self, r: TaskResult) -> TaskResult:
        if r.spoken:
            bus.publish("say", text=r.spoken)
        bus.publish("result", result=r)
        bus.publish("state", state="idle")
        return r

    # ------------------------------------------------------------------ fast path
    def _fast(self, route, req: UserRequest) -> TaskResult:
        p, i = route.params, route.intent
        lang = "hi" if req.language.startswith("hi") else "en"
        t0 = time.perf_counter()
        try:
            a = {
                "time": lambda: actions.tell_time(lang), "date": lambda: actions.tell_date(lang),
                "volume_set": lambda: actions.set_volume(p["level"]),
                "volume_up": lambda: actions.change_volume(10), "volume_down": lambda: actions.change_volume(-10),
                "mute": lambda: actions.set_mute(True), "unmute": lambda: actions.set_mute(False),
                "brightness_set": lambda: actions.set_brightness(p["level"]),
                "brightness_up": lambda: actions.change_brightness(15),
                "brightness_down": lambda: actions.change_brightness(-15),
                "lock": actions.lock_screen, "screenshot": lambda: actions.screenshot(self.ws.root),
                "battery": actions.battery, "cpu": actions.cpu, "ram": actions.ram, "disk": actions.disk,
                "media_playpause": lambda: actions.media("play_pause"), "media_next": lambda: actions.media("next"),
                "media_prev": lambda: actions.media("prev"),
                "snap_left": lambda: actions.snap("left"), "snap_right": lambda: actions.snap("right"),
                "maximize": lambda: actions.snap("up"), "show_desktop": lambda: actions.snap("show_desktop"),
                "read_clipboard": lambda: actions.read_clipboard(self.s.private_mode),
                "open_app": lambda: actions.open_app(p["target"], p["kind"], p["app"]),
                "open_folder": lambda: actions.open_folder(p["folder"], p.get("path"), self.ws.root),
                "open_website": lambda: actions.open_website(p["url"]),
            }.get(i)
            if a is not None:
                res = a()
            elif i == "timer":
                self.scheduler.add("timer", "timer", time.time() + p["seconds"])
                m, s = divmod(p["seconds"], 60)
                res = actions.ActionResult(True, f"Timer set for {m} minutes{f' {s} seconds' if s else ''}." if m
                                           else f"Timer set for {s} seconds.")
            elif i == "reminder":
                due = time.time() + p["seconds"] if "seconds" in p else datetime.fromisoformat(p["at"]).timestamp()
                self.scheduler.add("reminder", p["what"], due)
                res = actions.ActionResult(True, f"Okay, I'll remind you to {p['what']} at "
                                                 f"{datetime.fromtimestamp(due).strftime('%I:%M %p').lstrip('0')}.")
            elif i == "remember":
                self.memory.remember(p["fact"])
                res = actions.ActionResult(True, "Got it, I'll remember that.")
            elif i == "forget_fact":
                n = self.memory.forget(p["key"])
                res = actions.ActionResult(bool(n), "Forgotten." if n else "I didn't have anything like that saved.")
            elif i == "list_memory":
                facts = [f["text"] for f in self.memory.facts()]
                res = actions.ActionResult(True, f"I remember {len(facts)} things about you; they're on screen."
                                           if facts else "I don't have anything saved yet.", details="\n".join(facts))
            elif i == "new_topic":
                self.backends.drop_session("conversation")
                self.last_task = None
                res = actions.ActionResult(True, "Okay, fresh start.")
            elif i == "undo":
                return self._undo(req)
            elif i == "help":
                res = actions.ActionResult(True, "I can open apps and sites, control volume, take screenshots, set "
                                           "timers and reminders, and do multi-step tasks with files after you approve a plan.")
            else:
                res = actions.ActionResult(False, "I can't do that one yet.")
        except Exception as exc:
            log.exception("fast path %s failed", i)
            res = actions.ActionResult(False, "That didn't work.", details=str(exc))
        ms = int((time.perf_counter() - t0) * 1000)
        self.audit.write("fast_path", req.id, intent=i, params=p, ok=res.ok, ms=ms, details=res.details)
        return TaskResult("success" if res.ok else "failed", res.spoken, res.details, res.files)

    def _clarify_reminder(self, route) -> TaskResult:
        ans = (self.ask_user(route.question) or "").lower()
        hour = route.params["pending_hour"]
        if "pm" in ans or "p.m" in ans or "evening" in ans or "shaam" in ans or "night" in ans:
            hour = hour % 12 + 12
        elif "am" in ans or "morning" in ans or "subah" in ans:
            hour = hour % 12
        else:
            return self._say(TaskResult("needs_input", "I still wasn't sure, so I didn't set it."))
        now = datetime.now()
        at = now.replace(hour=hour, minute=route.params["pending_minute"], second=0, microsecond=0)
        if at <= now:
            at = datetime.fromtimestamp(at.timestamp() + 86400)
        route.path, route.params["at"] = "fast", at.isoformat()
        return self._say(self._fast(route, UserRequest(text=route.normalized)))

    def _undo(self, req: UserRequest) -> TaskResult:
        plan = undo.latest(self.ws.root)
        if plan is None:
            return TaskResult("failed", "There's nothing I can undo.")
        p = self.confirm.request(req.id, plan.describe(), RiskTier.CONSEQUENTIAL, req.id)
        bus.publish("say", text="Please approve the undo on screen.")
        if self.confirm.wait(p) != "approve":
            return TaskResult("cancelled", "Okay, I left everything as it is.")
        done, failed = undo.execute(plan)
        self.audit.write("undo", plan.task_id, done=done, failed=failed)
        if failed:
            return TaskResult("partial", f"Restored {len(done)} items; {len(failed)} could not be restored.",
                              "\n".join(failed))
        return TaskResult("success", f"Undone. Restored {len(done)} items.", "\n".join(done))

    # ------------------------------------------------------------------ agent path
    def _agent_task(self, req: UserRequest, fg_window: dict | None) -> TaskResult:
        status, msg = self.ledger.status()
        if status == "exhausted":
            return TaskResult("failed", msg)
        if status == "warn":
            bus.publish("notify", title="Jarvis usage", text=msg)
        if not self.backends.available and self.backends.select() is None:
            return TaskResult("failed", "The Cursor agent isn't available right now, so I can only do quick "
                                        "commands.", self.backends.degraded_reason)
        tier, why = classify_request(req.text, self.s.tier_overrides)
        full = self.s.autonomy == "full"
        if full and tier == RiskTier.FORBIDDEN and not is_catastrophic_request(req.text):
            tier, why = RiskTier.CONSEQUENTIAL, why + " (full autonomy: confirm once instead of blocking)"
        if tier == RiskTier.FORBIDDEN:
            self.audit.write("forbidden_request", req.id, text=req.text, reason=why)
            return TaskResult("blocked", "I can't do that; it touches protected parts of the system.", why)
        if req.follow_up_of is None and self.last_task and self._is_follow_up(req.text):
            req.follow_up_of = self.last_task.task_id
        ctx = self.ws.new_task(req)
        self.current = ctx
        ctx.tier = tier
        bus.publish("state", state="thinking")
        bus.publish("say", text="On it." if req.language.startswith("en") else "Theek hai, kar raha hoon.")
        rep = ingest.gather(ctx, self.s, fg_window)
        if rep.refused:
            return TaskResult("blocked", rep.refused)
        extra = {"Relevant memory": "\n".join(f"- {m}" for m in self.memory.relevant(req.text)),
                 "Foreground window when asked": json.dumps(rep.window) if rep.window else "",
                 "Notes": "\n".join(rep.notes)}
        self.ws.write_context(ctx, extra)
        self.audit.write("task_started", ctx.task_id, tier=int(tier), why=why, tainted=ctx.tainted,
                         ingested=ctx.ingested)
        session = "conversation" if not ctx.tainted else ""
        try:
            if full:
                # One command -> done. Only explicit delete requests go through plan + one confirmation.
                result = (self._plan_then_execute(ctx, session) if is_delete_request(req.text)
                          else self._act(ctx, session))
            elif tier == RiskTier.READ_ONLY and tier < self.s.plan_first_tier + 1 and not self._mentions_change(req.text):
                result = self._run_phase(ctx, Mode.DIRECT, prompts.direct_prompt(ctx), session, strong=False)
            else:
                result = self._plan_then_execute(ctx, session)
        except BackendUnavailable as exc:
            result = TaskResult("failed", self._backend_msg(exc.code), str(exc))
        self._finish(ctx, result)
        return result

    @staticmethod
    def _is_follow_up(text: str) -> bool:
        import re

        return bool(re.search(r"\b(again|same|that|it|those|them|the (first|second|third|last) one|dobara|wahi|usko)\b",
                              text, re.I))

    @staticmethod
    def _mentions_change(text: str) -> bool:
        import re

        return bool(re.search(r"\b(open|create|make|write|save|zip|move|copy|delete|rename|send|install|type|click)\b",
                              text, re.I))

    def _backend_msg(self, code: str) -> str:
        return {"auth": "My Cursor API key isn't working. Please check it with 'jarvis set-key'.",
                "rate_limit": "Cursor's usage limit was hit. I'll stick to quick commands for now.",
                "offline": "I'm offline, so I can only do quick commands.",
                "not_enabled": "The Cursor SDK isn't enabled for this account; switching to the CLI may help."
                }.get(code, "The Cursor agent failed to start.")

    def _act(self, ctx: TaskContext, session: str) -> TaskResult:
        """Full autonomy: run the whole request directly; confirm only held deletions."""
        bus.publish("state", state="acting")
        r = self._run_phase(ctx, Mode.DIRECT, prompts.act_prompt(ctx), session, strong=True)
        held = [b for b in self._last_blocked if DELETE_MARK in str(b.get("reason", ""))]
        if not held:
            return r
        items = "\n".join(f"- {str(b.get('input') or '')[:300]}" for b in held[:30])
        extra = "\n".join(f"- {d}" for d in r.double_check[:30])
        text = (f"Request: {ctx.request.text[:200]}\n\nEverything else is done. It also needs to DELETE:\n{items}"
                + (f"\n\nAgent's list:\n{extra}" if extra else "")
                + "\n\nDeleted files go to the Recycle Bin (say 'undo that' to restore).")
        p = self.confirm.request(ctx.task_id, text, RiskTier.CONSEQUENTIAL, ctx.request.id)
        bus.publish("say", text="That includes deleting something. Say confirm, or approve on screen.")
        bus.publish("state", state="awaiting_approval")
        decision = self.confirm.wait(p)
        self.audit.write("delete_approval", ctx.task_id, decision=decision, channel=p.channel, items=items)
        if decision != "approve":
            r.status = "partial"
            r.spoken = "Done, except the deletion, which I skipped."
            return r
        ctx.approved = True
        bus.publish("state", state="acting")
        return self._run_phase(ctx, Mode.EXECUTE, prompts.delete_prompt(ctx, items + ("\n" + extra if extra else "")),
                               session, strong=False)

    def _plan_then_execute(self, ctx: TaskContext, session: str) -> TaskResult:
        revision = ""
        for _attempt in range(3):
            bus.publish("state", state="planning")
            outcome_box: dict = {}
            r = self._run_phase(ctx, Mode.PLAN, prompts.plan_prompt(ctx, revision), session, strong=True,
                                outcome_box=outcome_box)
            plan_text = outcome_box.get("plan_text") or ""
            if r.status in ("failed", "cancelled", "blocked") and not plan_text:
                return r
            if not plan_text:
                if r.status == "needs_input" or "?" in (r.spoken or ""):
                    ans = self.ask_user(r.question or r.spoken) if self.ask_user else ""
                    if not ans:
                        return TaskResult("needs_input", r.question or r.spoken)
                    revision = f"Answer to your question: {ans}"
                    continue
                plan_text = outcome_box.get("text") or r.details
            plan = build_plan(plan_text, ctx.tier)
            ctx.plan = plan
            (ctx.folder / "plan.md").write_text(plan.raw, encoding="utf-8")
            if plan.forbidden_reasons:
                self.audit.write("plan_forbidden", ctx.task_id, reasons=plan.forbidden_reasons)
                return TaskResult("blocked", "The plan needs a forbidden action, so I stopped.",
                                  "\n".join(plan.forbidden_reasons))
            if plan.tier == RiskTier.READ_ONLY and all(s.tier == RiskTier.READ_ONLY for s in plan.steps):
                decision = "approve"     # tier 0: auto-allowed
            else:
                text = confirmation_text(plan, ctx.request.text)
                bus.publish("plan", task_id=ctx.task_id, plan=plan, confirm_text=text)
                bus.publish("say", text=spoken_summary(plan))
                bus.publish("state", state="awaiting_approval")
                p = self.confirm.request(ctx.task_id, text, plan.tier, ctx.request.id)
                decision = self.confirm.wait(p)
                self.audit.write("approval", ctx.task_id, decision=decision, channel=p.channel, tier=int(plan.tier),
                                 edit=p.edit_text)
                if decision == "edit" and p.edit_text:
                    revision = p.edit_text
                    continue
            if decision != "approve":
                return TaskResult("cancelled", "Okay, I won't do it." if decision == "deny"
                                  else "No approval received, so I cancelled it.")
            ctx.approved = True
            bus.publish("state", state="acting")
            return self._run_phase(ctx, Mode.EXECUTE, prompts.execute_prompt(ctx), session, strong=False)
        return TaskResult("needs_input", "I couldn't settle on a plan. Please rephrase the request.")

    def _run_phase(self, ctx: TaskContext, mode: Mode, prompt: str, session: str, strong: bool,
                   outcome_box: dict | None = None, autonomy: str | None = None) -> TaskResult:
        tampered = self.ws.verify_integrity()
        if tampered:
            self.audit.write("integrity_restored", ctx.task_id, files=tampered)
        pctx = self.ws.policy_context(ctx, mode, autonomy=autonomy)
        self._last_blocked = []
        self.ws.write_policy_state(pctx)
        self._write_state(ctx, "running", mode.value)
        phase = mode.value
        spec = RunSpec(prompt=prompt, cwd=self.ws.root, mode="plan" if mode == Mode.PLAN else "agent",
                       model=self.backends.model_for(strong), session_key=session)
        obs = Observer(ctx.task_id, self.audit, self.s.budgets, pctx, self.backends.cancel, self.backends.kill, phase)

        def on_event(ev):
            obs.on_event(ev)
            if not ctx.tainted and ev.category.value == "web_fetch":
                ctx.tainted = True
                pctx.tainted = True
                self.ws.write_policy_state(pctx)    # hook re-reads per call: network now blocked

        with obs:
            outcome = self.backends.run(spec, on_event)
        self._last_blocked = list(obs.blocked)
        self.ledger.record(runs=1, tokens=int(outcome.usage.get("total_tokens", 0) or
                                              outcome.usage.get("input_tokens", 0) + outcome.usage.get("output_tokens", 0)),
                           tool_calls=outcome.tool_calls, seconds=outcome.duration_s)
        bus.publish("usage", **self.ledger.snapshot())
        self.audit.write("run_finished", ctx.task_id, phase=phase, status=outcome.status, seconds=outcome.duration_s,
                         tools=outcome.tool_calls, usage=outcome.usage, error=outcome.error,
                         blocked=[b.get("reason") for b in obs.blocked], stop=obs.stop_reason)
        self.ws.verify_integrity()
        if outcome_box is not None:
            outcome_box.update(plan_text=outcome.plan_text, text=outcome.text)
        if obs.violation:
            return TaskResult("blocked", "I stopped the task because it tried something unsafe.", obs.violation)
        if obs.stop_reason:
            return TaskResult("failed", f"I stopped the task: {obs.stop_reason.split(':', 1)[-1].strip()}.",
                              outcome.text[-3000:])
        if outcome.status == "cancelled":
            return TaskResult("cancelled", "Cancelled.")
        if outcome.status != "finished":
            if outcome.error_code == "rate_limit":
                return TaskResult("failed", self._backend_msg("rate_limit"), outcome.error)
            return TaskResult("failed", "The agent ran into an error.", outcome.error or outcome.text[-2000:])
        result = parse_result(outcome.text)
        if obs.blocked and result.status == "success":
            result.double_check.append("Some actions were blocked by policy: "
                                       + "; ".join(sorted({str(b.get('reason')) for b in obs.blocked}))[:500])
        if result.injection_warning:
            self.audit.write("injection_detected", ctx.task_id, warning=result.injection_warning)
            bus.publish("notify", title="Possible prompt injection", text=result.injection_warning[:200])
        if result.status == "needs_input" and result.question and self.ask_user and mode != Mode.PLAN:
            ans = self.ask_user(result.question)
            if ans:
                return self._run_phase(ctx, mode, prompts.clarification_prompt(result.question, ans),
                                       session or "", strong)
        return result

    def _write_state(self, ctx: TaskContext, status: str, phase: str) -> None:
        (ctx.folder / "state.json").write_text(json.dumps({"status": status, "phase": phase}), encoding="utf-8")

    def _finish(self, ctx: TaskContext, result: TaskResult) -> None:
        self._write_state(ctx, result.status, "done")
        (ctx.folder / "result.json").write_text(json.dumps(result.__dict__, indent=1), encoding="utf-8")
        self.audit.write("task_finished", ctx.task_id, status=result.status, spoken=result.spoken,
                         files=result.files_changed, double_check=result.double_check)
        if ctx.tainted:
            self.backends.drop_session("conversation")   # never reuse a tainted session
        self.last_task = ctx
        self.current = None

    # ------------------------------------------------------------------ routines
    def run_routine(self, routine: dict) -> None:
        from jarvis.models import Source

        req = UserRequest(text=routine.get("prompt", ""), source=Source.ROUTINE)
        if not self.busy.acquire(timeout=600):
            return
        try:
            ctx = self.ws.new_task(req)
            self.ws.write_context(ctx, {"Routine": routine.get("name", "")})
            # Unattended -> always SAFE autonomy (tier 0/1 only), whatever the global setting is.
            r = self._run_phase(ctx, Mode.DIRECT, prompts.direct_prompt(ctx), "", strong=False, autonomy="safe")
            self._finish(ctx, r)
            bus.publish("notify", title=f"Routine: {routine.get('name', '')}", text=r.spoken)
            bus.publish("say", text=r.spoken)
        finally:
            self.busy.release()
