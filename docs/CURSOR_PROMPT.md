# Prompt for Cursor — build the Jarvis UI (Cluely-style overlay + live steps)

> Paste everything below the line into Cursor (Grok 4.7 High). It is self-contained.

---

You are adding a **UI layer** to an existing, working, tested project: **Jarvis**, a voice-controlled agent for
this Windows PC (Python 3.11, Cursor SDK brain). Voice, instant commands, desktop control, scheduling, queue,
memory, safety and the agent are DONE (161 tests). **Your job is UI only. The UI is an add-on: it listens to events
and calls existing functions. It must not change how Jarvis thinks or acts.**

## 0. Golden rule — do not harm the core
- **Do NOT modify** anything in: `jarvis/orchestrator.py`, `jarvis/backend/`, `jarvis/safety/`, `jarvis/hooks/`,
  `jarvis/desktop/`, `jarvis/voice/`, `jarvis/router/`, `jarvis/fastpath/`, `jarvis/capabilities/`,
  `jarvis/observer.py`, `jarvis/steps.py`, `jarvis/scheduler.py`, `jarvis/history.py`, `jarvis/workspace/`.
- The only existing files you may touch: `jarvis/__main__.py` (add the `ui` command and use `JarvisRuntime`) and
  `jarvis/config.py` (only ADD new UI fields with defaults; never rename/remove fields).
- New code goes in **`jarvis/runtime.py`** and **`jarvis/ui/`** only. If you believe a core change is needed,
  STOP and write it into `docs/UI_NOTES.md` instead of changing it.
- All 161 existing tests must still pass unchanged.

## 1. Read first
`README.md`, `docs/REMAINING_PLAN.md` (Phase 1), `docs/DECISIONS.md`, `jarvis/__main__.py` (`run()` = current
wiring), `jarvis/events.py`, `jarvis/steps.py` (UI event contract), `jarvis/config.py`.

## 2. `jarvis/runtime.py` — `JarvisRuntime` (wiring only, no new behaviour)
Move the wiring from `run()` in `jarvis/__main__.py` into a class: `start()`, `stop()`, `submit(text, source)`,
`set_listening(bool)`, `kill()`, `answer(text)` (for `ask_user` questions), `approve(nonce)/deny(nonce)`.
Keep `run()`'s exact startup order: `faster_whisper` + `openwakeword` imported on the main thread BEFORE any thread
starts (prevents a proven deadlock); `AudioListener` + `WhisperStt(model, languages=...)`;
`listener.barge_in = settings.voice.barge_in`; `Tts`; hotkeys; `orch.start()`. Console mode
(`python -m jarvis`) must keep working through `JarvisRuntime` with identical output.

## 3. Event contract (subscribe to `jarvis.events.bus`; never poll)
| topic | payload | use |
|---|---|---|
| `state` | `state`: idle/listening/thinking/planning/acting/awaiting_approval/speaking/error | orb + bar state |
| `mic_level` | `level` 0..1 (~12/s while listening) | live waveform |
| `transcript` | `text`, `confidence` | what the user said |
| `task` | `task_id`, `status` started/queued/finished, `request`, `result_status`, `spoken`, `position` | task cards |
| `step` | `task_id`, `step_id`, `status` running/done/failed/blocked/held, `title`, `detail`, `icon`, `internal` | **live steps** |
| `result` | `result` (TaskResult: status, spoken, details, files_changed, double_check, verified) | result card |
| `approval_request` / `approval_closed` | `nonce`, `confirm_text`, `tier`, `timeout_s` / `decision` | delete confirm |
| `say`, `notify`, `error`, `voice_status`, `usage` | text | toasts / status line |
- **UI shows only friendly data**: `step.title`/`detail`, `task`, `result.spoken`. Never show raw tool calls,
  commands, JSON or log lines in the overlay (raw stays in `%LOCALAPPDATA%\Jarvis\logs\jarvis.jsonl`).
- Steps with `internal: true` are hidden by default (expandable "n hidden steps").
- `BusBridge(QObject)` re-emits each topic as a Qt `Signal(dict)`; never touch widgets from bus/worker threads.

## 4. The overlay — Cluely-style "agentic" bar (main UI)
- **Look**: frameless, rounded (16 px), dark translucent glass (Windows acrylic/blur via `DwmSetWindowAttribute`
  backdrop when available, else semi-transparent `#141418E6` + soft shadow), subtle 1 px border, Inter/Segoe UI
  Variable, one accent colour, smooth 150–250 ms animations (`QPropertyAnimation`), 60 fps, no jank.
- **Placement**: always-on-top, top-center, draggable, remembers position; `Qt.Tool` (no taskbar button).
- **Never steals focus** (Jarvis types into other apps!): `WA_ShowWithoutActivating`, `WindowDoesNotAcceptFocus`
  except when the user clicks the text box. Optional toggle "Hide from screen share"
  (`SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)`).
- **Collapsed (idle)**: compact pill ~420×48: orb + "Say “Hey Jarvis”…" + mic button + ⚙. Fades to 60 % opacity
  when idle.
- **Expanded (auto on wake / task / hotkey Ctrl+Alt+J)** grows downward (~420×max 560):
  1. header: animated orb by `state` + live **waveform** from `mic_level` while listening;
  2. **what you said** (`transcript`);
  3. **live steps timeline** for the current task from `step`: spinner → ✓ done / ✗ failed (detail shown) /
     🛡 blocked / ⏸ held (waiting for your OK); per-step elapsed time and total task timer; newest at bottom;
     auto-scroll; collapsed "n hidden steps" for `internal`;
  4. **result card** from `result`: spoken text, verified badge (✓ verified / ⚠ unverified), expandable details,
     files with "Open folder", buttons **Undo** (`submit("undo that")`) and **Copy**;
  5. **queue chips** ("2 queued", "next scheduled 12:00 PM: send mail…") from `task` + scheduler;
  6. text box (Enter = `submit`), mic button (= push-to-talk), red **STOP** (= `kill()`, < 1 s).
  Auto-collapse 8 s after a finished task if the mouse is not over it.
- **Inline approval** (only deletes): card inside the overlay (and a toast if collapsed): `confirm_text`,
  Approve/Deny, countdown ring (`timeout_s`); spoken "confirm" also resolves it (core already handles it).
- **Inline question** for `ask_user` ("5 AM or PM?", "Which Nilesh?"): chips/buttons + text, voice works too.

## 5. Tray + windows
- Tray icon always: Show/Hide overlay, Enable/Disable Jarvis, Pause listening, Stop, Settings, History, Quit;
  tooltip = state; toasts for `notify` and for results while the overlay is hidden.
- **Settings window** (normal window, opened from tray/⚙), saved with `config.save`, applied live:
  toggles Jarvis enabled (master), Listening, Speak replies, Full autonomy, Screen & clipboard reading
  (`private_mode`), Desktop control, Start with Windows (shortcut in `shell:startup` →
  `pythonw -m jarvis ui --minimized`), Hide from screen share; voice (wake_threshold, barge_in, whisper_model,
  TTS voice/rate); models (from `backends.models()`); budgets; Cursor key (masked → `secrets.set_api_key`);
  mail account (same Credential Manager entry as `python -m jarvis set-mail`); doctor results.
- **History window**: SQLite history (`jarvis/history.py`) + task folders; Undo; Scheduled list with cancel
  (`scheduler.pending()` / `scheduler.cancel(id)`).
- Single instance: mutex `Local\JarvisApp`; a second launch shows the existing overlay and exits.
- Entry points: `python -m jarvis ui` and `pythonw -m jarvis ui --minimized` (add `ui` to argparse choices).
- Always-on: hidden/minimized/tray → voice, scheduler, reminders, hotkeys (Ctrl+Alt+Space talk, Ctrl+Alt+K stop,
  Ctrl+Alt+J overlay) keep working exactly as in console mode.

## 6. Hard rules (each caused a real bug before)
- `.venv\Scripts\python.exe` (3.11) only; never edit `site-packages`.
- Never call a method you didn't define (`python -m pyflakes jarvis` clean).
- Never import inside a logging handler or bus callback running on another thread.
- UTF-8 file edits only (no PowerShell `Set-Content` on source/markdown).
- One Jarvis process only (mic mutex `Local\JarvisVoiceMic`). Secrets only via `jarvis/secrets.py`.
- Out of scope: login, accounts, multi-user, web UI, changing agent/voice/safety behaviour.

## 7. Done = all true (check yourself)
- `pyflakes` clean; `pytest tests -q` → 161 existing + your new tests (runtime with a fake backend, settings
  persistence of every toggle, BusBridge thread-safety) all green; `python -m jarvis doctor` OK;
  `python -m jarvis` console unchanged.
- `pythonw -m jarvis ui` shows the overlay, no console; second launch reuses it.
- "Hey Jarvis, open notepad and write hello in it" with the overlay collapsed AND hidden to tray: overlay expands
  without stealing focus, steps stream live (Opening Notepad ✓ → Typing “hello” ✓ → Verifying ✓), result card
  shows ✓ verified; text really lands in Notepad.
- Deleting a test file shows the inline approval once (click and spoken "confirm" both work).
- STOP / Ctrl+Alt+K end a running task in < 1 s; toggles apply live and survive restart; master off stops listening.
- 30 min idle: CPU < 2 %, RAM stable, no error loop in the log.
- Commit with the repo-local git config (never global), no secrets.
