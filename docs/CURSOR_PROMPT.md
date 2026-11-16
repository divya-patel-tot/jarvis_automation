# Prompt for Cursor (paste as-is) — Phase 1: Desktop UI + always-on background agent

> Model: Grok 4.7 High (or any strong agent model). Paste everything below the line.

---

You are continuing an existing, **working** project: **Jarvis**, a voice-controlled agent for this Windows PC
(Python 3.11). Its AI brain is the Cursor SDK. Voice, instant commands, desktop control, scheduling, queue,
memory and safety are done and tested (151 tests). **Do not rebuild, redesign or "improve" any of that.** Your job
is the desktop UI only.

## 1. Read first (fully)
`docs/REMAINING_PLAN.md` (your spec is **Phase 1**), `docs/DECISIONS.md`, `README.md`, then
`jarvis/__main__.py` (`run()` is the current wiring you will reuse), `jarvis/orchestrator.py`, `jarvis/events.py`
(event bus topics), `jarvis/voice/listener.py`, `jarvis/voice/whisper_stt.py`, `jarvis/config.py`,
`jarvis/history.py`, `jarvis/scheduler.py`.

## 2. Build (Phase 1 in REMAINING_PLAN.md, sections 1.1-1.4)
1. **`JarvisRuntime`** (new, e.g. `jarvis/runtime.py`): move the wiring from `run()` in `jarvis/__main__.py` into a
   class with `start() / stop() / submit(text, source) / set_listening(bool) / kill()` and the `ask_user`
   question/answer mechanism. Keep the exact startup ORDER from `run()`:
   - `faster_whisper` and `openwakeword` are imported on the main thread BEFORE any thread starts
     (prevents a proven import/logging deadlock),
   - voice uses `AudioListener` + `WhisperStt(model, languages=...)`, `listener.barge_in = settings.voice.barge_in`,
   - `Tts` subscribes to `say` / `stop_speech`.
   Console mode (`python -m jarvis`) must keep working through `JarvisRuntime`.
2. **`jarvis/ui/`** (PySide6, already installed): entry `python -m jarvis ui` and `pythonw -m jarvis ui --minimized`
   (add `ui` to argparse choices in `__main__.py`).
   - `BusBridge(QObject)`: subscribes to `jarvis.events.bus`, re-emits each topic as a Qt `Signal(dict)`
     (topics: state, transcript, activity, agent_text, approval_request, approval_closed, result, say, notify,
     error, voice_status, usage). Never touch widgets from bus/worker threads.
   - **Main window** (close = hide to tray): status orb (idle/listening/thinking/acting/awaiting_approval/
     speaking/error), backend + model + today's usage, **toggles saved immediately with `config.save` and applied
     live**: Jarvis enabled (master), Listening (wake word), Speak replies, Full autonomy, Screen & clipboard
     reading (`private_mode`), Desktop control (`desktop_control_enabled`), Start with Windows (shortcut in
     `shell:startup` running `pythonw -m jarvis ui --minimized`). Command box + Talk button (push-to-talk),
     live activity feed, transcript, result card (spoken text, details, files with "open folder", double-check
     items), queue/scheduled list, big red **STOP** (`runtime.kill()`).
   - **Tray icon** always present: show/hide, Enable/Disable, Pause listening, Stop current task, Settings,
     History, Quit; tooltip = state; notifications for `notify` and for results while hidden.
   - **Approval dialog** (only deletions in full autonomy): always-on-top, shows `confirm_text`, Approve/Deny,
     countdown auto-deny (`timeout_s`), closes on `approval_closed`, pops up even when the window is hidden;
     spoken "confirm" must also resolve it (already handled by the orchestrator).
   - **Question popup** for `ask_user` (e.g. "5 AM or PM?", "Which Nilesh?"), answerable by click, typing or voice.
   - **Settings** dialog: voice (wake_threshold, barge_in, whisper_model base.en/small, TTS voice+rate), models
     (fast/strong lists from `backends.models()`), budgets, autonomy, Cursor key (masked → `secrets.set_api_key`),
     mail account (form → same Credential Manager entry as `python -m jarvis set-mail`), doctor results.
   - **History** view: from `jarvis/history.py` (SQLite) + task folders; **Undo** button →
     `runtime.submit("undo that")`; Scheduled view from `scheduler.pending()` with cancel.
   - Single instance: named mutex `Local\JarvisApp`; a second launch focuses the first window and exits.
   - Window must not steal focus while tasks run (desktop tools type into other apps!); only the approval/question
     popups may appear on top.
3. Always-on: when minimized or hidden to tray, voice, scheduler, reminders, hotkeys (Ctrl+Alt+Space talk,
   Ctrl+Alt+K stop, add Ctrl+Alt+J show window) and tasks keep working exactly as in console mode.

**Out of scope:** login, accounts, multi-user, web UI, cloud sync, changing voice/agent/safety behaviour.

## 3. Hard rules (each one caused a real bug before)
- Use `.venv\Scripts\python.exe` (3.11). Never edit `site-packages`.
- Do not modify `jarvis/safety/policy.py`, `jarvis/hooks/policy_hook.py`, `jarvis/desktop/tools.py`,
  `jarvis/voice/*`, the autonomy contract (only deletions ask once) or the AGENTS.md rules.
- Never call a method you didn't define (`python -m pyflakes jarvis` must be clean).
- Never import anything inside a logging handler or bus callback that runs on another thread.
- `uiautomation` 2.x: patterns via `GetPattern(PatternId.X)`; there are no `Is*PatternAvailable()` helpers.
- Edit files as UTF-8 (no PowerShell `Set-Content` round-trips on Markdown/Python).
- Only one Jarvis process (mic mutex `Local\JarvisVoiceMic`).
- Secrets only via `jarvis/secrets.py` / Credential Manager.

## 4. Done means (verify yourself before saying done)
- `python -m pyflakes jarvis` clean; `python -m pytest tests -q` all green (151 existing + new tests for
  `JarvisRuntime` with a fake backend and for settings persistence of every toggle).
- `python -m jarvis doctor` OK; `python -m jarvis` (console) still works.
- `pythonw -m jarvis ui` opens with no console; second launch focuses the first window.
- With the window minimized AND closed to tray: "Hey Jarvis, open notepad" works; "Hey Jarvis, open notepad and
  write hello in it" completes with zero approvals; "Hey Jarvis, delete <a test file> from my desktop" shows the
  approval popup once (click and spoken "confirm" both work).
- Every toggle takes effect immediately and survives restart; master toggle off really stops listening.
- STOP button and Ctrl+Alt+K stop a running task in < 1 s while hidden.
- 30 min idle in tray: CPU < 3 %, RAM stable, no error loop in `%LOCALAPPDATA%\Jarvis\logs\jarvis.jsonl`.
- Commit with a clear message using the existing repo-local git config (never change global git config,
  never commit secrets).
