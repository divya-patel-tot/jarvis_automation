# Jarvis — voice-controlled agent for your Windows PC (Cursor-only AI, Python 3.11)

Say **"Hey Jarvis, …"** (or type) and Jarvis does the whole task on your PC from one command: opens and closes
apps, writes in any app, sends email and chat messages, works in browser tabs, manages files, schedules tasks.
The only AI is the **Cursor agent** (official `cursor-sdk`); everything else runs locally.

Design: `JARVIS_CURSOR_ONLY_PLAN.md` · measured facts & decisions: `docs/DECISIONS.md` ·
remaining work (desktop UI): `docs/REMAINING_PLAN.md` · prompt for Cursor: `docs/CURSOR_PROMPT.md`

---

## Quick start
```powershell
cd D:\jarvis_automation
py -3.11 -m venv .venv                                   # first time only
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m jarvis set-key               # Cursor API key -> Windows Credential Manager
.venv\Scripts\python.exe -m jarvis set-mail              # optional: Gmail App Password so email really sends
.venv\Scripts\python.exe -m jarvis doctor                # checks key, Cursor, voice, mic
.venv\Scripts\python.exe -m jarvis                       # run (voice + typed console)
```
Run only **one** Jarvis window at a time (it owns the microphone).

| Key / word | Action |
|---|---|
| "Hey Jarvis, <request>" | one breath is fine: "Hey Jarvis, open notepad" |
| Ctrl+Alt+Space | talk without the wake word |
| Ctrl+Alt+K or "stop" | kill switch - stops everything in < 1 s |
| talk while Jarvis speaks | interrupts it (barge-in) |
| "actually … / instead … / also …" during a task | changes the running task |

## What to say (examples)
| Instant (no AI, < 0.1 s) | Agent (Cursor, ~10-30 s) |
|---|---|
| open / close notepad, teams, chrome… | open notepad and write today's plan in it |
| set volume to 30, mute, brightness up | message Nilesh Valand on Teams: are you free? |
| what time is it, battery, cpu, ram, disk | send a mail to Mitesh about the report |
| take a screenshot, lock the screen | open Chrome, open Gmail and read my latest mail |
| timer 5 minutes, remind me to call mom at 6 pm | find last week's PDFs in Downloads and zip them to Desktop |
| what's scheduled, cancel the task for Ravi | summarize my screen / this page / what I copied |
| remember my project folder is D:\work | delete old zip files in Downloads (asks once) |

**Scheduling:** add a time to any command - "… at 12 PM", "after 5 hours …", "tomorrow at 9 …",
"in 30 minutes …". Saved across restarts; runs through the same queue.

## How it behaves
- **Autonomy = full (default):** one command and it's done - no plan/approve step. **Only deletions ask once**
  (Approve on screen, type `a`, or say "confirm"). Deleted files go to the Recycle Bin; "undo that" restores.
- **Truthful:** a task counts as done only if the agent verified the real effect (message visible in the chat,
  file exists…). Otherwise Jarvis says it couldn't confirm.
- **Action order** (same for every app): shell/PowerShell → Python scripts → UI Automation desktop tools
  (read/click/type in any app or browser tab) → screenshot.
- **Queue:** agent tasks run one at a time (one mouse/keyboard); instant commands run immediately in parallel;
  repeated commands are merged ("Already on it").
- **Memory:** every turn is saved (SQLite), so "write it in that", "send that to Ravi" work - also after restart.

## Services used (what leaves the PC)
| Function | Service | Where |
|---|---|---|
| Reasoning + actions | Cursor agent (`cursor-sdk` local runtime) | Cursor cloud models |
| Wake word "hey jarvis" | openWakeWord | offline |
| Speech-to-text | Whisper `base.en` (faster-whisper), English | offline |
| Text-to-speech | Windows SAPI voices | offline |
| App control | Windows UI Automation | offline |
| Email (optional) | your SMTP account (Gmail App Password) | your mail provider |

Only the text of your requests (and anything the agent reads to do them) goes to Cursor.

## Safety (enforced in code, not only in prompts)
1. **Pre-execution hook** - `<workspace>\.cursor\hooks.json` → `jarvis/hooks/policy_hook.py` runs before *every*
   agent tool call and blocks credential theft, disabling Defender/UAC, boot/backup/log tampering, writes to
   Jarvis's own rules/library, network sends after reading untrusted web/PDF content; deletions wait for one OK.
2. Desktop tools refuse admin windows, password managers and password fields; never type into the wrong window.
3. Kill switch via Windows Job Object (~0.4 s); hash-chained redacted audit log in `%LOCALAPPDATA%\Jarvis\audit`.
4. Secrets only in Windows Credential Manager (`jarvis-cursor`, `jarvis-mail`); never in files or logs.

## Settings
`%LOCALAPPDATA%\Jarvis\settings.json` (created on first run). Useful keys:
`autonomy` (full|safe), `voice.wake_threshold` (0.5; lower = more sensitive), `voice.barge_in`
(voice|wake|off), `voice.whisper_model` (base.en), `desktop_control_enabled`, `budgets.daily_tokens`.

## Files & logs
| Path | What |
|---|---|
| `%LOCALAPPDATA%\Jarvis\logs\jarvis.jsonl` | log (send this when reporting a bug) |
| `%LOCALAPPDATA%\Jarvis\state\history.sqlite3` | conversation history |
| `%LOCALAPPDATA%\Jarvis\archive\<time>\` | archived logs/history/tasks from earlier test rounds |
| `%USERPROFILE%\JarvisWorkspace` | agent workspace: tasks, outputs, memory, rules (`AGENTS.md`), library (`lib`) |

## Code layout
`jarvis/backend` Cursor SDK (+ Windows/3.11 bridge shim) and CLI fallback · `jarvis/safety` policy, risk,
budgets, confirmations · `jarvis/router` + `jarvis/fastpath` instant commands · `jarvis/voice` wake word,
Whisper, TTS · `jarvis/desktop` UI Automation tools · `jarvis/senses` screen, documents, web ·
`jarvis/capabilities` vetted scripts · `jarvis/orchestrator.py` the pipeline · `tests/`.

## Tests
```powershell
.venv\Scripts\python.exe -m pytest tests -q            # 151 unit tests
.venv\Scripts\python.exe -m tests.e2e_autonomy         # real agent (uses Cursor usage)
```

## Not done yet
Desktop window UI with on/off toggles + tray (being built - see `docs/REMAINING_PLAN.md` Phase 1),
vision click fallback, golden/red-team test suites, installer / start-with-Windows.
