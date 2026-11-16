"""Generic desktop control for the agent - works on ANY app window and browser tab via Windows
UI Automation (the accessibility layer Win32, WPF, UWP, Chrome/Edge/WebView2, Office and Electron expose).

Registered as Cursor SDK custom tools (in-process; the policy hook sees them as "MCP:desktop_*").
Workflow the agent follows: desktop_windows -> desktop_read (numbered elements) -> desktop_click /
desktop_type / desktop_keys -> desktop_wait_for_text (VERIFY) -> report.

Every UIA call runs on ONE worker thread (UIA COM objects belong to the thread that created them, so element
ids from desktop_read stay valid for the next click). Elevated windows, password managers and password fields
are refused.
"""

from __future__ import annotations

import ctypes
import logging
import queue
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any, Callable

from jarvis.senses import screen

log = logging.getLogger(__name__)

MAX_ELEMENTS = 300
MAX_TEXT = 14_000
TOOL_NAMES = ("desktop_windows", "desktop_focus", "desktop_read", "desktop_click", "desktop_type",
              "desktop_keys", "desktop_wait_for_text", "desktop_screenshot")

VK = {"ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B, "enter": 0x0D, "return": 0x0D,
      "esc": 0x1B, "escape": 0x1B, "tab": 0x09, "space": 0x20, "backspace": 0x08, "delete": 0x2E, "del": 0x2E,
      "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "home": 0x24, "end": 0x23, "pageup": 0x21,
      "pagedown": 0x22, "insert": 0x2D, **{f"f{i}": 0x6F + i for i in range(1, 13)}}
KEYEVENTF_KEYUP = 0x0002


class DesktopError(Exception):
    pass


def pattern(ctrl, name: str):
    """uiautomation 2.x: patterns via GetPattern(PatternId.X) -> pattern or None (no Is*Available helpers)."""
    import uiautomation as auto

    try:
        return ctrl.GetPattern(getattr(auto.PatternId, name))
    except Exception:
        return None


def force_foreground(hwnd: int) -> bool:
    """Reliably bring a window to the front despite Windows' foreground lock."""
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)                          # SW_RESTORE
    if user32.GetForegroundWindow() == hwnd:
        return True
    user32.mouse_event(0x0001, 0, 0, 0, 0)                  # zero-move input event: we become "last input" owner
    me = kernel32.GetCurrentThreadId()
    fg_tid = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
    tgt_tid = user32.GetWindowThreadProcessId(hwnd, None)
    for tid in {fg_tid, tgt_tid} - {me}:
        user32.AttachThreadInput(tid, me, True)
    try:
        user32.BringWindowToTop(hwnd)
        user32.ShowWindow(hwnd, 5)                          # SW_SHOW
        user32.SetForegroundWindow(hwnd)
        user32.SetActiveWindow(hwnd)
    finally:
        for tid in {fg_tid, tgt_tid} - {me}:
            user32.AttachThreadInput(tid, me, False)
    for _ in range(10):
        if user32.GetForegroundWindow() == hwnd:
            return True
        time.sleep(0.05)
    return False


# --------------------------------------------------------------------------- worker thread
class _Worker:
    def __init__(self) -> None:
        self._q: "queue.Queue[tuple[Callable, tuple, queue.Queue]]" = queue.Queue()
        threading.Thread(target=self._run, name="desktop-uia", daemon=True).start()

    def _run(self) -> None:
        import uiautomation as auto

        with auto.UIAutomationInitializerInThread():
            while True:
                fn, args, out = self._q.get()
                try:
                    out.put((True, fn(*args)))
                except Exception as exc:          # returned to the agent as an error result
                    out.put((False, exc))

    def call(self, fn: Callable, *args: Any, timeout: float = 30.0) -> Any:
        out: "queue.Queue" = queue.Queue()
        self._q.put((fn, args, out))
        try:
            ok, value = out.get(timeout=timeout)
        except queue.Empty:
            raise DesktopError(f"desktop action timed out after {timeout:.0f}s (app not responding?)") from None
        if not ok:
            raise value
        return value


# --------------------------------------------------------------------------- desktop model
class Desktop:
    def __init__(self, screenshot_dir: Callable[[], Path] | None = None) -> None:
        self._worker = _Worker()
        self._elements: dict[str, Any] = {}
        self._element_window: dict[str, int] = {}
        self._screenshot_dir = screenshot_dir or (lambda: Path.home() / "JarvisWorkspace" / "outputs")

    # ---- window lookup -------------------------------------------------
    @staticmethod
    def _windows() -> list[dict]:
        out = []
        user32 = ctypes.windll.user32
        proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def cb(hwnd, _lp):
            if not user32.IsWindowVisible(hwnd) or user32.GetWindowTextLengthW(hwnd) <= 0:
                return True
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            if cls.value in ("Shell_TrayWnd", "Progman", "WorkerW", "Windows.UI.Core.CoreWindow"):
                return True
            info = screen.window_info(hwnd)
            if info["title"] and info["title"] not in ("Program Manager",):
                info["minimized"] = bool(user32.IsIconic(hwnd))
                out.append(info)
            return True

        user32.EnumWindows(proto(cb), 0)
        return out

    def _find_window(self, window: str | int | None) -> dict:
        wins = self._windows()
        if window in (None, "", "foreground", "active"):
            fg = screen.foreground_window()
            if fg.get("hwnd"):
                return fg
            raise DesktopError("no foreground window")
        if isinstance(window, int) or (isinstance(window, str) and window.isdigit()):
            for w in wins:
                if w["hwnd"] == int(window):
                    return w
        q = str(window).lower()
        for key in ("title", "process"):          # z-order: the first match is the top-most window
            for w in wins:
                if q in (w.get(key) or "").lower():
                    return w
        raise DesktopError(f"no open window matches '{window}'. Open windows: "
                           + "; ".join(f"{w['title'][:50]} ({w['process']})" for w in wins[:15]))

    @staticmethod
    def _guard(win: dict) -> None:
        if win.get("elevated"):
            raise DesktopError(f"'{win['title']}' runs as administrator; Windows does not allow controlling it")
        if screen.is_sensitive(win):
            raise DesktopError(f"'{win['title']}' is a sensitive window (passwords/banking); refusing")

    # ---- tools (run on the worker thread) --------------------------------
    def windows(self) -> dict:
        wins = self._windows()
        return {"windows": [{"hwnd": w["hwnd"], "title": w["title"], "process": w["process"],
                             "minimized": w.get("minimized", False), "admin": w["elevated"]} for w in wins[:40]]}

    def focus(self, window: str | int) -> dict:
        return self._worker.call(self._focus, window)

    def _focus(self, window):
        import uiautomation as auto

        win = self._find_window(window)
        self._guard(win)
        hwnd = win["hwnd"]
        ok = force_foreground(hwnd)
        if not ok:
            try:
                auto.ControlFromHandle(hwnd).SetActive()
            except Exception:
                pass
            ok = force_foreground(hwnd)
        if not ok:
            raise DesktopError(f"could not bring '{win['title']}' to the front")
        time.sleep(0.15)
        return {"focused": True, "title": win["title"], "process": win["process"]}

    def read(self, window: str | int | None = None, max_depth: int = 30, filter_text: str = "") -> dict:
        return self._worker.call(self._read, window, max_depth, filter_text, timeout=45)

    def _read(self, window, max_depth, filter_text):
        import uiautomation as auto

        win = self._find_window(window)
        self._guard(win)
        root = auto.ControlFromHandle(win["hwnd"])
        self._elements.clear()
        lines, n, total = [], 0, 0
        flt = (filter_text or "").lower()
        deadline = time.monotonic() + 12
        for ctrl, depth in auto.WalkControl(root, includeTop=False, maxDepth=max(1, min(int(max_depth), 60))):
            if time.monotonic() > deadline or n >= MAX_ELEMENTS or total > MAX_TEXT:
                lines.append("... (truncated - use filter_text or a smaller window)")
                break
            try:
                ctype = ctrl.ControlTypeName.replace("Control", "")
                name = (ctrl.Name or "").strip()
                is_pw = bool(getattr(ctrl, "IsPassword", False))
                value = ""
                vp = None if is_pw else pattern(ctrl, "ValuePattern")
                if vp is not None:
                    value = (vp.Value or "").strip()
                elif not is_pw and ctype in ("Document", "Edit"):
                    tp = pattern(ctrl, "TextPattern")
                    if tp is not None:
                        value = (tp.DocumentRange.GetText(400) or "").strip()
                interactive = ctype in ("Button", "Edit", "CheckBox", "RadioButton", "ComboBox", "ListItem",
                                        "MenuItem", "TabItem", "Hyperlink", "TreeItem", "Document", "SplitButton")
                if not (name or value or interactive):
                    continue
                text = f"{name} {value}".lower()
                if flt and flt not in text and not interactive:
                    continue
                n += 1
                eid = f"e{n}"
                self._elements[eid] = ctrl
                self._element_window[eid] = win["hwnd"]
                bits = [f"[{eid}]", ctype]
                if name:
                    bits.append(f'"{name[:120]}"')
                if is_pw:
                    bits.append("(password field - not readable)")
                elif value and value != name:
                    bits.append(f'value="{value[:200]}"')
                if not ctrl.IsEnabled:
                    bits.append("(disabled)")
                line = "  " * min(depth, 8) + " ".join(bits)
                lines.append(line)
                total += len(line)
            except Exception:
                continue
        return {"window": win["title"], "process": win["process"], "elements": n,
                "tree": "\n".join(lines) if lines else "(no accessible elements - try desktop_screenshot)"}

    def _element(self, element_id: str):
        ctrl = self._elements.get(str(element_id).strip())
        if ctrl is None:
            raise DesktopError(f"unknown element '{element_id}'. Call desktop_read again (ids change after each read).")
        win = screen.window_info(self._element_window.get(element_id, 0))
        self._guard(win)
        return ctrl

    def click(self, element_id: str, double: bool = False) -> dict:
        return self._worker.call(self._click, element_id, double)

    def _click(self, element_id, double):
        ctrl = self._element(element_id)
        how = ""
        if not double:
            for pname, method, label in (("InvokePattern", "Invoke", "invoke"),
                                         ("SelectionItemPattern", "Select", "select"),
                                         ("TogglePattern", "Toggle", "toggle"),
                                         ("ExpandCollapsePattern", "Expand", "expand")):
                p = pattern(ctrl, pname)
                if p is None:
                    continue
                try:
                    getattr(p, method)()
                    how = label
                    break
                except Exception:
                    continue
        if not how:
            self._focus(self._element_window.get(element_id))
            (ctrl.DoubleClick if double else ctrl.Click)(simulateMove=False)
            how = "mouse double-click" if double else "mouse click"
        time.sleep(0.3)
        return {"clicked": element_id, "name": ctrl.Name, "method": how}

    def type_text(self, text: str, element_id: str = "", replace: bool = False, press_enter: bool = False) -> dict:
        return self._worker.call(self._type, text, element_id, replace, press_enter)

    def _type(self, text, element_id, replace, press_enter):
        import pyperclip

        method = "paste"
        if element_id:
            ctrl = self._element(element_id)
            if getattr(ctrl, "IsPassword", False):
                raise DesktopError("refusing to type into a password field")
            target_hwnd = self._element_window.get(element_id)
            self._focus(target_hwnd)                      # raises if the window can't be brought to front
            vp = pattern(ctrl, "ValuePattern") if replace else None
            if vp is not None:
                try:
                    vp.SetValue(text)
                    method = "set-value"
                except Exception:
                    pass
            if method == "paste":
                try:
                    ctrl.SetFocus()
                except Exception:
                    ctrl.Click(simulateMove=False)
                if replace:
                    self._press(["ctrl", "a"])
        else:
            fg = screen.foreground_window()
            self._guard(fg)
        if method == "paste":                          # clipboard paste: exact unicode, fast; clipboard restored
            try:
                previous = pyperclip.paste()
            except Exception:
                previous = None
            pyperclip.copy(text)
            time.sleep(0.05)
            self._press(["ctrl", "v"])
            time.sleep(0.25)
            if previous is not None:
                threading.Timer(1.0, lambda: pyperclip.copy(previous)).start()
        if press_enter:
            self._press(["enter"])
        return {"typed_chars": len(text), "method": method, "pressed_enter": press_enter}

    @staticmethod
    def _press(keys: list[str]) -> None:
        user32 = ctypes.windll.user32
        codes = []
        for k in keys:
            k = k.lower().strip()
            if k in VK:
                codes.append(VK[k])
            elif len(k) == 1:
                vk = user32.VkKeyScanW(ord(k)) & 0xFF
                if vk == 0xFF:
                    raise DesktopError(f"unknown key '{k}'")
                codes.append(vk)
            else:
                raise DesktopError(f"unknown key '{k}'")
        for c in codes:
            user32.keybd_event(c, 0, 0, 0)
        for c in reversed(codes):
            user32.keybd_event(c, 0, KEYEVENTF_KEYUP, 0)

    def keys(self, keys: str, window: str | int | None = None) -> dict:
        return self._worker.call(self._keys, keys, window)

    def _keys(self, keys, window):
        if window not in (None, ""):
            self._focus(window)
        fg = screen.foreground_window()
        self._guard(fg)
        combos = [c for c in str(keys).replace(" ", "").split(",") if c]
        for combo in combos:                           # "ctrl+enter", "tab,tab,enter"
            self._press(combo.split("+"))
            time.sleep(0.12)
        return {"pressed": combos, "window": fg.get("title")}

    def wait_for_text(self, text: str, window: str | int | None = None, timeout_s: float = 8.0,
                      gone: bool = False) -> dict:
        return self._worker.call(self._wait, text, window, timeout_s, gone, timeout=float(timeout_s) + 20)

    def _wait(self, text, window, timeout_s, gone):
        win = self._find_window(window)
        self._guard(win)
        needle = str(text).lower()
        deadline = time.monotonic() + max(0.5, min(float(timeout_s), 60.0))
        while True:
            body = screen.uia_text(win["hwnd"], max_chars=80_000, time_budget_s=4.0)
            found = needle in f"{win['title']}\n{body}".lower()
            if found != bool(gone) or time.monotonic() > deadline:
                idx = body.lower().find(needle)
                snippet = body[max(0, idx - 80): idx + len(needle) + 80] if idx >= 0 else ""
                return {"found": found, "verified": found != bool(gone), "window": win["title"],
                        "snippet": snippet.replace("\n", " | ")}
            time.sleep(0.7)

    def screenshot(self, window: str | int | None = None) -> dict:
        win = self._find_window(window)
        self._guard(win)
        out = self._screenshot_dir() / f"screen-{time.strftime('%H%M%S')}.png"
        screen.capture_window(out, win)
        return {"path": str(out), "window": win["title"],
                "hint": "Read this image file to see the window (vision fallback)."}


# --------------------------------------------------------------------------- SDK registration
def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


WIN = {"type": "string", "description": "Window title part, process name, hwnd, or 'foreground'"}


def build_custom_tools(desktop: Desktop) -> dict:
    """Cursor SDK CustomTool map. Errors are returned as {'ok': False, 'error': ...} so the agent can adapt."""
    from cursor_sdk import CustomTool

    def wrap(name: str, fn: Callable[[dict], dict]):
        def execute(args, _ctx):
            t0 = time.perf_counter()
            try:
                result = {"ok": True, **fn(dict(args or {}))}
            except DesktopError as exc:
                result = {"ok": False, "error": str(exc)}
            except Exception as exc:
                log.exception("desktop tool %s failed", name)
                result = {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}
            log.info("desktop tool %s(%s) -> ok=%s in %.2fs", name, str(args)[:160], result.get("ok"),
                     time.perf_counter() - t0)
            return result
        return execute

    d = desktop
    specs = {
        "desktop_windows": ("List open windows (title, process, hwnd). Start here.",
                            _obj({}), lambda a: d.windows()),
        "desktop_focus": ("Bring a window to the front (restores if minimized).",
                          _obj({"window": WIN}, ["window"]), lambda a: d.focus(a["window"])),
        "desktop_read": ("Read a window's UI as numbered elements [eN] (buttons, edits, list items, links, texts, "
                         "browser page content, tabs). Ids are valid until the next desktop_read.",
                         _obj({"window": WIN, "filter_text": {"type": "string", "description": "only lines containing this"},
                               "max_depth": {"type": "integer"}}),
                         lambda a: d.read(a.get("window"), int(a.get("max_depth") or 30), a.get("filter_text", ""))),
        "desktop_click": ("Click/activate element [eN] from desktop_read (uses accessibility Invoke first).",
                          _obj({"element": {"type": "string"}, "double": {"type": "boolean"}}, ["element"]),
                          lambda a: d.click(a["element"], bool(a.get("double")))),
        "desktop_type": ("Type text (any language) into element [eN], or into the focused control if no element. "
                         "replace=true clears it first; press_enter=true presses Enter after.",
                         _obj({"text": {"type": "string"}, "element": {"type": "string"},
                               "replace": {"type": "boolean"}, "press_enter": {"type": "boolean"}}, ["text"]),
                         lambda a: d.type_text(a["text"], a.get("element", ""), bool(a.get("replace")),
                                               bool(a.get("press_enter")))),
        "desktop_keys": ("Press keys in the focused (or given) window, e.g. 'ctrl+enter', 'tab,tab,enter', 'ctrl+t'.",
                         _obj({"keys": {"type": "string"}, "window": WIN}, ["keys"]),
                         lambda a: d.keys(a["keys"], a.get("window"))),
        "desktop_wait_for_text": ("VERIFY: wait until text appears (or with gone=true disappears) in a window. "
                                  "Use after every send/save/change; report verified only if this succeeds.",
                                  _obj({"text": {"type": "string"}, "window": WIN, "timeout_s": {"type": "number"},
                                        "gone": {"type": "boolean"}}, ["text"]),
                                  lambda a: d.wait_for_text(a["text"], a.get("window"), float(a.get("timeout_s") or 8),
                                                            bool(a.get("gone")))),
        "desktop_screenshot": ("Screenshot a window to a PNG you can read (fallback when desktop_read shows little).",
                               _obj({"window": WIN}), lambda a: d.screenshot(a.get("window"))),
    }
    return {name: CustomTool(execute=wrap(name, fn), description=desc, input_schema=schema)
            for name, (desc, schema, fn) in specs.items()}
