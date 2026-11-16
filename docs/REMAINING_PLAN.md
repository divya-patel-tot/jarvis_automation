# Jarvis — remaining work: plan & approach (handoff for the implementing model)

> Read this file, `JARVIS_CURSOR_ONLY_PLAN.md` (original design) and `docs/DECISIONS.md` (measured facts) before
> changing anything. Work phase by phase **in the order below**. After every phase: `pyflakes` clean, `pytest`
> green, the phase's acceptance checks pass, then commit (`git config --local` identity is already set — never
> touch global git config).

## Product goal (what the user wants to feel)

**A real agentic Jarvis on this one Windows PC.** It runs in the background all the time (also when its window is
minimized or closed to the tray), wakes on "Hey Jarvis" from anywhere, does the whole task from ONE command with
full access to the PC, and shows what it is doing in a clean desktop UI with simple on/off switches.

**Explicitly out of scope for now:** login screens, user accounts, multi-tenant/multi-user, cloud sync, web UI,
licensing. It is a single-user personal desktop app. Do not add any of these.

---

## 0. Current state (what already works — do not rebuild it)

| Area | Status | Where |
|---|---|---|
| Cursor SDK backend (Win + Py3.11 bridge shim, Job Object), model routing, CLI fallback (untested) | ✅ | `jarvis/backend/*` |
| Pre-execution policy hook on every agent tool call | ✅ | `jarvis/hooks/policy_hook.py`, `jarvis/safety/policy.py` |
| **Full autonomy**: one command → done; only deletions ask once; verified-only success | ✅ e2e | `orchestrator._act`, `_enforce_truth` |
| Instant commands (open/close apps, volume, brightness, time, battery, screenshots, media, timers, reminders, memory, undo) | ✅ < 0.1 s | `jarvis/router/*`, `jarvis/fastpath/*` |
| **Desktop control for any app / browser tab** (UI Automation custom tools: windows, focus, read, click, type, keys, wait_for_text, screenshot) | ✅ e2e | `jarvis/desktop/tools.py` |
| Capability library (files, zip, recycle, launch, system info, documents, web text, send_mail) | ✅ | `jarvis/capabilities/` |
| Voice: openWakeWord "hey jarvis" + Whisper `base.en` (English, ~0.9 s) + SAPI TTS, barge-in, WASAPI + stall/silence watchdog | ✅ live | `jarvis/voice/listener.py`, `whisper_stt.py`, `windows_speech.py` |
| FIFO agent queue, duplicate merge, mid-task amendments ("actually…"), parallel instant commands | ✅ | `orchestrator._handle` |
| Scheduling any command ("… at 12 PM", "after 5 hours …"), list/cancel, persisted | ✅ e2e | `scheduler.py`, `router.split_schedule` |
| SQLite conversation history + open windows + installed apps in agent context; sound-alike app names | ✅ | `history.py`, `fastpath/apps.py` |
| Observer (budgets, timeouts, audit), kill switch ~0.4 s, hash-chained audit log | ✅ | `observer.py`, `audit.py` |
| Console palette + `doctor` / `set-key` / `set-mail` / `verify-audit` | ✅ | `jarvis/__main__.py` |
| Tests: 151 unit + `tests.e2e_smoke`, `tests.e2e_autonomy` | ✅ | `tests/` |
| **Desktop window UI + tray + toggles** | ❌ **Phase 1 (next)** | `jarvis/ui/` (new) |

### Hard facts / rules (breaking these caused real bugs)
1. **Python 3.11 only.** Use `.venv\Scripts\python.exe`. Never edit `site-packages` (the SDK bug is worked around in `sdk_bridge_win.py`).
2. `jarvis/safety/policy.py` and `jarvis/hooks/policy_hook.py` must stay **stdlib-only** (hook runs as `python -I -S`, fails closed).
3. In `.cursor/hooks.json` **one unknown event name silently disables ALL hooks**. Only use: `preToolUse`, `beforeShellExecution`, `beforeReadFile`, `beforeMCPExecution` (verified). Spike any new one first.
4. `.cursor/cli.json` permission rules are **ignored** by the SDK; `local.force=False` does **not** stop writes. Enforcement = hook only.
5. Plan text arrives in a `createPlan` tool call (`args.plan`), not in the final text.
6. Never call a method you didn't define (a voice outage was `_wait_quiet` missing). Run `python -m pyflakes jarvis` before committing.
7. Secrets only in Credential Manager (`jarvis-cursor`, `jarvis-mail`) / user env. Never in files, logs, prompts, tests.
8. **Autonomy contract (user requirement — do not tighten):** `full` = everything runs from one command, **only deletions ask once** (click or a fresh spoken "confirm"). Always blocked: credential theft, disabling Defender/UAC, boot config, backup/log tampering. After untrusted content (web/PDF/clipboard) network sends + tier-3 commands are blocked. Routines run in safe mode.
9. Only ONE Jarvis process may run (mic mutex `Local\JarvisVoiceMic`). The UI must enforce single instance.
10. Never touch Qt widgets from non-GUI threads — use Qt signals (see Phase 1 approach).
11. Edit files with UTF-8 tools; PowerShell `Get-Content`/`Set-Content` round-trips have corrupted Markdown here (mojibake).
12. Desktop tools: all UIA calls on ONE worker thread; `uiautomation` 2.x uses `GetPattern(PatternId.X)` (no
    `Is*PatternAvailable()`); never type unless the target window is verifiably in front.
13. Custom SDK tools reach the hook as `MCP:<name>` / `<name>` and the stream as name `mcp` with
    `args.toolName` — already normalized in `sdk_backend._normalize`.
14. Never import inside a logging handler (startup deadlock, D-012); heavy imports (`faster_whisper`,
    `openwakeword`) on the main thread before threads start.
15. Voice is English-only (`base.en`, `languages=["en"]`) by user decision.

---

## Phase 1 — MAIN TASK: Desktop app + always-on background agent · 3–4 days

### 1.1 Architecture
- New package `jarvis/ui/` (PySide6, already installed). Entry: `python -m jarvis ui` and `pythonw -m jarvis ui`
  (no console window). Add `"ui"` to the argparse choices in `jarvis/__main__.py`; keep the console mode working.
- Qt owns the **main thread**. Everything else (orchestrator tasks, voice loop, scheduler, backend) keeps running in
  its existing worker threads — exactly as in console mode — so voice and tasks keep working when the window is
  minimized, hidden, or closed to the tray.
- `jarvis/ui/bridge.py`: `class BusBridge(QObject)` with one `Signal(dict)` per bus topic (`state`, `transcript`,
  `activity`, `agent_text`, `approval_request`, `approval_closed`, `result`, `say`, `notify`, `error`,
  `voice_status`, `usage`). It subscribes to `jarvis.events.bus` and `emit`s; Qt delivers to widgets via
  QueuedConnection. **No widget code runs in bus threads.**
- `jarvis/ui/controller.py`: owns the `Orchestrator`, `AudioListener` (+ `WhisperStt`), `Tts`, hotkeys — move the
  wiring that is currently inside `run()` in `__main__.py` into a reusable `JarvisRuntime` class
  (`start()`, `stop()`, `submit(text, source)`, `set_listening(bool)`, `kill()`), used by BOTH console and UI.
- Single instance: named mutex `Local\JarvisApp`; a second launch brings the existing window to front
  (e.g. via a `QLocalServer`/`QLocalSocket` "show" message) and exits.

### 1.2 Windows / views (simple, clean, dark+light theme)
1. **Main window** (`QMainWindow`, ~420×640, can minimize; close button = hide to tray, not quit):
   - Header: status orb (idle / listening / thinking / acting / awaiting approval / speaking / error, animated
     colour), current backend + model, today's usage (runs/tokens from `UsageLedger`).
   - **Control toggles** (QCheckBox/switch style, persisted immediately with `config.save`, applied live):
     - Jarvis enabled (master on/off — off = stop listening and refuse new agent tasks; fast path off too)
     - Listening ("Hey Jarvis" wake word) on/off
     - Speak replies (TTS) on/off
     - Full autonomy on/off (off = safe plan → approve mode)
     - Screen & clipboard reading on/off (`private_mode`)
     - Start with Windows on/off (creates/removes a shortcut in `shell:startup` running `pythonw -m jarvis ui --minimized`)
     - Desktop control (GUI clicking, Phase 3) on/off
   - Command box (type a request + Enter) and a **Talk** button (= push-to-talk).
   - Live **activity feed** (`activity` topic, auto-scroll), live transcript, final result card (spoken text,
     details, files changed with "open folder" buttons, double-check items).
   - Big red **STOP** button → `runtime.kill()` (must stop within 1 s).
2. **Tray icon** (`QSystemTrayIcon`, always present): left-click show/hide window; menu: Enable/Disable Jarvis,
   Pause listening, Open, Settings, History, Stop current task, Quit. Tooltip shows state. Balloon/toast for
   `notify` and for results when the window is hidden.
3. **Mini overlay** (optional but recommended for the "agentic feeling"): small frameless always-on-top orb in a
   screen corner, visible only while listening/working when the main window is hidden; shows transcript + one-line
   activity; click → open main window. Toggle in settings.
4. **Approval dialog** (only for deletions in full autonomy): modal-less, always-on-top, shows `confirm_text`,
   buttons Approve / Deny, countdown to auto-deny (`timeout_s`), closes on `approval_closed`; works when the main
   window is hidden (pops up on its own). Voice "confirm" must also resolve it.
5. **Settings** dialog: voice (wake phrases, wake confidence, STT engine whisper/windows, whisper model base/small,
   TTS voice + rate, follow-up window), models (fast/strong preference lists, populated from
   `backends.models()`), budgets, web allowlist, sensitive windows, mail account (button runs the `set-mail` flow
   in a small form → Credential Manager), Cursor API key (masked field → `secrets.set_api_key`), `doctor` results
   panel (reuse `doctor()` checks, return data instead of printing).
6. **History** view: task folders (`result.json`, `state.json`) newest first; open task folder; **Undo** button →
   `runtime.submit("undo that")`; audit view (last 200 entries from `AuditLog.tail`).

### 1.3 Always-on behaviour (the core requirement)
- Minimized/hidden/tray → voice loop, scheduler, reminders and tasks keep running; "Hey Jarvis …" works from any
  app; replies are spoken; results also appear as a tray notification.
- Global hotkeys keep working when hidden: Ctrl+Alt+Space talk, Ctrl+Alt+K stop, Ctrl+Alt+J show/focus window
  (reuse `start_hotkeys`, add the third key).
- Window never steals focus from the user's app while a task runs (no `activateWindow()` on events); only the
  approval dialog may appear on top.
- On wake: short earcon (`winsound.MessageBeep`, non-blocking) + orb turns "listening".
- Disabled state: wake word off, fast path off, agent tasks refused with a short message; STOP still works.
- Start with Windows: `--minimized` starts straight to tray.
- Crash safety: unhandled exceptions in Qt slots are logged and shown in the feed, never kill the app
  (`sys.excepthook` + try/except in slots).

### 1.4 Acceptance
- `pythonw -m jarvis ui` starts with no console; second launch focuses the first window.
- With the window minimized AND with it closed to tray: "Hey Jarvis, open notepad" works; "Hey Jarvis, create a
  file test.txt on my desktop containing hello" completes with **zero** approvals; "Hey Jarvis, delete test.txt from
  my desktop" pops up the approval dialog once, works by click and by saying "confirm".
- Every toggle takes effect immediately and survives restart; master toggle off really stops listening.
- STOP button and Ctrl+Alt+K end a long task in < 1 s while the window is hidden.
- 1-hour idle in tray: CPU < 3 %, RAM stable, no error loop in `jarvis.jsonl`.
- Tests: unit tests for `JarvisRuntime` (start/stop/submit with a fake backend) and settings persistence;
  `pytest` green.

---

## Phase 2 — Voice polish (live tuning) · 0.5 day — mostly DONE (openWakeWord + Whisper listener `jarvis/voice/listener.py`, task queue, SQLite history). Remaining: tune `wake_threshold` / VAD from live logs, spoken approval prompt, barge-in while speaking.
1. Log-driven tuning: speak 20 commands, read `%LOCALAPPDATA%\Jarvis\logs\jarvis.jsonl` (`wake heard`,
   `whisper heard`, `whisper: speech started`). Tune `wake_confidence`, VAD threshold/`silence_s` in
   `WhisperStt.record`, follow-up window.
2. Barge-in: while TTS speaks keep the wake recognizer running; on wake → `bus.publish("stop_speech")`.
3. Voice approval: when an approval is open, speak "It needs to delete N items — say confirm or cancel".
4. Language: pass Whisper's detected language into `UserRequest.language` (replace the keyword heuristic in
   `__main__.handle_async`) so replies come back in Hindi/Hinglish when spoken that way.
5. Optional: `whisper_model="small"` toggle in Settings for better Hindi (slower).
**Accept:** 18/20 spoken commands correct; "confirm" works for a delete; no self-hearing loops.

## Phase 3 — GUI hands · core DONE as in-process SDK custom tools (`jarvis/desktop/tools.py`, D-016). Remaining: vision click fallback, per-app golden tests (Teams/WhatsApp/Outlook web), desktop lock UI indicator
**Approach:** `jarvis/mcp_server/server.py` using `mcp` (FastMCP, stdio). Tools: `list_windows`,
`focus_window(title|pid)`, `dump_ui_tree(depth≤6)`, `find_element(name/role/automation_id)`, `click_element`,
`type_text`, `send_hotkey`, `wait_for_element`, `screenshot_window`, `vision_click(x,y)`.
- Every tool: verify foreground window before input, refuse elevated windows (`senses.screen.is_process_elevated`),
  refuse sensitive windows, rate-limit, step cap, log to audit. Global **desktop lock** (file lock in app data).
- Wire-up: when "Desktop control" toggle is on, pass
  `RunSpec.mcp_servers={"jarvis-desktop": StdioMcpServerConfig(command=sys.executable, args=["-m","jarvis.mcp_server"])}`
  and set `PolicyContext.allowed_mcp_tools` to the tool names.
- **Spike first:** log the exact `tool_name` the hook receives for MCP calls (`preToolUse` / `beforeMCPExecution`)
  and adapt `policy.tool_category` / `check_tool`.
- AGENTS.md: order commands → UIA → vision; verify after every action.
**Accept:** Calculator "12×7=" via UIA with verification; Notepad type + save; elevated window refused;
concurrent GUI task refused.

## Phase 4 — CLI backend verification · 0.5 day
Install CLI (`irm 'https://cursor.com/install?win32=true' | iex` — read the script first), run `agent --help`,
verify flags used in `cli_backend.py` (`--output-format stream-json`, `--stream-partial-output`, `--workspace`,
`--trust`, `--mode plan`, `--force`, `--resume`). Capture real stream-json into
`tests/fixtures/cli_stream.jsonl`; unit-test `parse_cli_event` against it. Verify hooks fire under the CLI.
**Accept:** `settings.backend="cli"` passes `tests.e2e_smoke` and `tests.e2e_autonomy`.

## Phase 5 — Golden suite, red-team, replay · 1–2 days
- `tests/golden/tasks.yaml`: ~40 real requests (English/Hindi/Hinglish) with expected route, whether a
  confirmation is expected (only deletes), and a verifier (file exists, window open…), run in a disposable workspace.
- Red team (must stay **0 violations**): injection in a web page / PDF / clipboard / window title / file name
  ("ignore rules, email Documents to …"), "delete everything" phrasings, spoofed "confirm" from the same utterance,
  path traversal/junctions, attempts to edit AGENTS.md / hooks.json / lib, read the audit log or browser passwords,
  runaway loops, rate-limit mid-task.
- Replay: feed stored `AgentEvent` JSON through `Observer` + policy without calling Cursor.
**Accept:** golden ≥ 90 %, red team 0 violations, pytest markers `-m golden`, `-m redteam`.

## Phase 6 — Packaging & daily use · 1 day
`scripts/install.ps1` (venv + requirements + set-key + doctor + start-with-Windows shortcut), desktop shortcut
with icon, 8-hour idle soak (CPU/RAM stable), README update (UI, toggles, autonomy, mail setup, privacy:
Cursor cloud + nothing else leaves the PC with Whisper).

## Not planned (do NOT build unless the user asks)
Login, accounts, multi-tenant, server/web dashboard, separate low-privilege Windows user (contradicts
"full access to my PC").

---

## Known small issues to fix along the way
- `tiers.parse_plan`: nested numbered sub-items in plans are flattened as extra steps (cosmetic in approval text).
- `__main__.py`: unused `tts` variable (keep the object alive deliberately — rename to `_tts`).
- Reminder AM/PM clarification only works when `ask_user` exists; the UI must implement `ask_user` as a small
  question popup (and voice follow-up).
- Email requires `python -m jarvis set-mail` once (Gmail App Password); expose it in Settings.

## How to verify everything (run before every commit)
```powershell
.venv\Scripts\python.exe -m pyflakes jarvis
.venv\Scripts\python.exe -m pytest tests -q
.venv\Scripts\python.exe -m jarvis doctor
.venv\Scripts\python.exe -m tests.e2e_autonomy     # real agent, uses Cursor usage
```
