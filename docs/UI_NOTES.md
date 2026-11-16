# UI notes (no core behaviour changes)

The overlay and `JarvisRuntime` call existing functions and subscribe to the event bus.
Nothing under the agent, voice, safety, router, or desktop-control packages was changed for this UI.

## Topics the UI adds

The core does not publish these. `JarvisRuntime` does, for the overlay only:

- `question` — `{text}` when `ask_user` is waiting (AM/PM and other clarifications).
- `show_overlay` — Ctrl+Alt+J. The hotkey thread only publishes; the overlay shows itself on the Qt thread.

## What the master switch cannot do without a core change

`ui.enabled` off pauses the microphone and `JarvisRuntime.submit` refuses typed and spoken commands
(fast path and agent). Jobs already stored in the scheduler call `Orchestrator.handle` directly when they
fire, so a reminder or scheduled command still runs after the switch is turned off. Stopping those too
would mean editing `scheduler.py` or `orchestrator.py`, which this UI does not do.

The capture stream stays open while listening is paused, so it can resume without rebuilding the
listener (`AudioListener.stop` cannot be undone). Wake-word handling does not run while paused.

## What the overlay deliberately does not show

`step.detail` for a shell call is often the raw command. The overlay shows `step.title` and shows
`detail` for failed, blocked, and held steps (that text is the error). It hides command-like details
on running and done steps. `activity` and `agent_text` stay in the log file.

## This PC

The overlay is frosted glass: the desktop behind the box is blurred, and the panel fill is a dark
tint with a 12px corner. Window opacity stays at 1, and the text, buttons, and text field stay solid.
`SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)` is applied when "Hide from screen share" is on.

A quick command that finishes while another task is still running is shown as a banner. It does not
clear that task's steps, timer, or the original request. "Got it, updating the current task." stays
in that banner until the task itself finishes.

The panel changes height in one step. Animating a frameless window left copies of the text field and
STOP button on screen. Spoken answers publish `speaking` and do not publish `idle` when playback ends,
so the overlay leaves the header on the task or the answer instead of leaving it on "Speaking…".
