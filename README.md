# Jarvis (Cursor-only) — Windows voice assistant, Python 3.11

The Cursor agent (official `cursor-sdk`, CLI fallback) is the only AI. This app is the voice, senses,
fast reflexes and **safety officer** around it. Design: `JARVIS_CURSOR_ONLY_PLAN.md`; measured facts: `docs/DECISIONS.md`.

## Quick start
```powershell
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m jarvis set-key      # stores key in Windows Credential Manager (never in the repo)
.venv\Scripts\python.exe -m jarvis doctor       # checks every component
.venv\Scripts\python.exe -m jarvis              # run: type requests or say "Hey Jarvis"; Ctrl+Alt+K = kill switch
```
Demo requests: `what time is it` · `set volume to 30` · `open notepad` · `battery kitni hai` ·
`list the files in my Jarvis workspace` · `find pdf files in my downloads from last week and zip them to my desktop`
(shows a plan → type `a` to approve) · `undo that`.

## Services used
| Function | Service | Where |
|---|---|---|
| Reasoning + actions | Cursor agent (`cursor-sdk` local runtime; CLI fallback) | Cursor cloud models |
| Wake word | Windows SpeechRecognizer, phrase list | offline |
| Speech-to-text | Windows dictation (en-IN/en-US) — needs *Online speech recognition* ON | Microsoft online speech |
| Text-to-speech | Windows SAPI voices | offline |
Everything else (UI Automation, screenshots, volume, documents, recycle bin) is local.

## Safety model (enforced, not just prompted)
1. **Pre-execution hook**: `<workspace>\.cursor\hooks.json` → `jarvis/hooks/policy_hook.py` runs before *every*
   agent tool call (`preToolUse`) and denies forbidden commands, secret paths, writes outside allowed folders,
   changes in plan mode, network after untrusted content. Fails closed.
2. **Plan → approve → execute** for anything that changes state; confirmation text is generated from the plan by our code.
3. Risk tiers 0-3; tier 3 (registry, security, credentials, boot, format, elevation) never runs.
4. Observer: budgets, timeouts, defence-in-depth re-check, kill on violation. **Job Object** kill switch (~0.4 s).
5. Hash-chained, redacted audit log in `%LOCALAPPDATA%\Jarvis\audit` (agent cannot read it).
6. Deletions → Recycle Bin with undo manifests; protected rule/library files are integrity-checked and restored.

## Layout
`jarvis/backend` (SDK + Windows/3.11 bridge shim, CLI, manager) · `jarvis/safety` (policy, tiers, budgets, confirm) ·
`jarvis/router` + `jarvis/fastpath` (no-AI commands) · `jarvis/workspace` (rules, hooks, task folders) ·
`jarvis/capabilities` (vetted PowerShell/Python scripts) · `jarvis/senses` (screen/UIA, documents, web, ingestion) ·
`jarvis/voice` · `jarvis/orchestrator.py` · `tests/` (unit + `tests.e2e_smoke` against the real agent).

## Not done yet
Qt tray/overlay UI (console palette works now), own MCP server for UI-Automation clicks (phase 6),
Whisper STT option for Hindi, separate low-privilege Windows user for OS-level isolation, red-team suite.
