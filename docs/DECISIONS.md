# Phase 0 decisions log (measured on Win10 19045, Python 3.11.9, cursor-sdk 1.3.1, 2026-11-16)

- **D-001 Backend**: SDK is default (bridge start 1.3 s, agent create 0.9 s, warm run ~4 s, cold run with 1 shell call ~20 s). CLI not installed → fallback only.
- **D-002 Windows/3.11 bug**: `cursor_sdk.Bridge.launch` uses `os.get_blocking` + `selectors` on a pipe → crashes on Windows < 3.12. Fixed with our launcher `jarvis/backend/sdk_bridge_win.py` (reader thread, Job Object, no console window).
- **D-003 Plan mode**: `mode="plan"` makes no changes; plan text arrives in a `createPlan` tool call (`args.plan`). **`local.force=False` does NOT prevent writes** in the SDK → not a safety lever.
- **D-004 Cancel**: `run.cancel()` needs a client with env-key fallback; ends stream in ~10 ms. Hard kill via Job Object 0.1 s (5 processes). E2E kill switch 0.39 s.
- **D-005 Rules**: `AGENTS.md` in workspace is honoured with `setting_sources=["project"]`.
- **D-006 Enforcement**: `.cursor/cli.json` deny rules are **ignored** by the SDK. `.cursor/hooks.json` hooks work: `preToolUse` (all tools, `tool_name`+`tool_input`), `beforeShellExecution`, `beforeReadFile`; `{"permission":"deny"}` blocks before execution. One unknown event name disables **all** hooks.
- **D-007 No FS sandbox**: agent file tools can write outside the workspace → enforced by the hook (path policy).
- **D-008 Voice**: Windows dictation langs en-IN/en-US (no hi-IN); voices David/Zira; online-speech privacy toggle OFF and no microphone on this machine at test time.
- **D-009 Isolation**: separate low-privilege Windows user not yet set up → reduced isolation; compensated by hook + plan gate + observer.
- **D-010 Autonomy (user requirement, 2026-11-16)**: default `autonomy=full` - one command runs to completion (incl. installs, settings, real email via `send_mail.py`); only deletions are held by the hook (`NEEDS DELETE CONFIRMATION`) and confirmed once (click or fresh spoken 'confirm'). Always blocked: credential theft, disabling Defender/UAC, boot config, backup/log tampering. After untrusted content: network sends + tier-3 blocked. Routines always run in safe mode. E2E: create = 0 approvals, delete = 1 approval -> Recycle Bin.
