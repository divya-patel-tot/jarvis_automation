"""The agent's controlled world (plan section 7).

Layout:
    <workspace>/
        AGENTS.md               rules/persona (honoured by the SDK - Phase 0 D-005)
        .cursor/hooks.json      pre-execution policy hook registration
        lib/ps, lib/py          vetted capability scripts + manifest.json + README.md
        tasks/<task_id>/        request.md, context.md, ingested/, plan.md, result.json
        outputs/                user-facing artifacts
        memory/                 durable facts (written by the app only)
        staging/                agent-proposed scripts awaiting human review
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path

from jarvis import paths
from jarvis.config import Settings
from jarvis.models import Mode, TaskContext, UserRequest
from jarvis.safety.policy import PolicyContext

log = logging.getLogger(__name__)

TEMPLATES = Path(__file__).parent / "templates"
CAPABILITIES = paths.PACKAGE_ROOT / "capabilities"
HOOK_SCRIPT = paths.PACKAGE_ROOT / "hooks" / "policy_hook.py"
HOOK_EVENTS = ("preToolUse", "beforeShellExecution", "beforeReadFile", "beforeMCPExecution")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


class Workspace:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root = settings.workspace_path()
        self.tasks = self.root / "tasks"
        self.outputs = self.root / "outputs"
        self.memory = self.root / "memory"
        self.staging = self.root / "staging"
        self.lib = self.root / "lib"
        self.cursor_dir = self.root / ".cursor"
        self._integrity: dict[str, str] = {}

    # ------------------------------------------------------------------ setup
    def ensure(self) -> None:
        for d in (self.root, self.tasks, self.outputs, self.memory, self.staging, self.lib, self.cursor_dir,
                  self.root / "undo"):
            d.mkdir(parents=True, exist_ok=True)
        self._write_rules()
        self._install_library()
        self._write_hooks()
        self._write_cursorignore()
        self._snapshot_integrity()

    def _write_rules(self) -> None:
        text = (TEMPLATES / "AGENTS.md").read_text(encoding="utf-8")
        # Library table inline: the agent used to spend a tool call reading lib\README.md every task.
        manifest = json.loads((CAPABILITIES / "manifest.json").read_text(encoding="utf-8"))
        rows = ["", "## Capability library (already installed in lib\\ - do not read README, use directly)",
                "| script | tier | what | args |", "|---|---|---|---|"]
        rows += [f"| lib\\{i['file']} | {i['tier']} | {i['description']} | `{i['usage']}` |"
                 for _, i in sorted(manifest.items())]
        (self.root / "AGENTS.md").write_text(text.rstrip() + "\n" + "\n".join(rows) + "\n", encoding="utf-8")

    def _install_library(self) -> None:
        if self.lib.exists():
            shutil.rmtree(self.lib, ignore_errors=True)
        shutil.copytree(CAPABILITIES, self.lib, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        manifest = self.library_manifest()
        lines = ["# Capability library (vetted, prefer these)", "",
                 "PowerShell: `powershell -NoProfile -ExecutionPolicy Bypass -File lib\\ps\\<name>.ps1 <args>`",
                 "Python: `& $env:JARVIS_PY lib\\py\\<name>.py <args>`", "",
                 "Every script prints one JSON object. Tier: 0 read-only, 1 reversible, 2 needs approved plan.",
                 "Scripts with tier >= 1 accept `-DryRun` (PowerShell) / `--dry-run` (Python) to preview in plan mode.", "",
                 "| script | tier | what it does | usage |", "|---|---|---|---|"]
        for name, info in sorted(manifest.items()):
            lines.append(f"| {info['file']} | {info['tier']} | {info['description']} | `{info['usage']}` |")
        (self.lib / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def library_manifest(self) -> dict[str, dict]:
        path = self.lib / "manifest.json"
        if not path.exists():
            path = CAPABILITIES / "manifest.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def library_tiers(self) -> dict[str, int]:
        return {name: int(info["tier"]) for name, info in self.library_manifest().items()}

    def hook_command(self) -> str:
        py = Path(sys.executable)
        # Use the base interpreter's python.exe (pythonw has no stdout); -I -S: isolated + no site -> fast, stdlib only.
        if py.name.lower() == "pythonw.exe":
            py = py.with_name("python.exe")
        return f'"{py.as_posix()}" -I -S "{HOOK_SCRIPT.as_posix()}"'

    def _write_hooks(self) -> None:
        cmd = self.hook_command()
        hooks = {"version": 1, "hooks": {e: [{"command": cmd}] for e in HOOK_EVENTS}}
        (self.cursor_dir / "hooks.json").write_text(json.dumps(hooks, indent=2), encoding="utf-8")
        mcp = self.cursor_dir / "mcp.json"
        if mcp.exists():
            mcp.unlink()   # MCP servers are passed per run by the backend, never from project config

    def _write_cursorignore(self) -> None:
        (self.root / ".cursorignore").write_text("undo/\n", encoding="utf-8")

    # ------------------------------------------------------------------ integrity
    def protected_paths(self) -> list[Path]:
        return [self.root / "AGENTS.md", self.cursor_dir, self.lib, self.memory, self.root / "undo",
                self.root / ".cursorignore", paths.PROJECT_ROOT, paths.app_data_dir()]

    def _protected_files(self) -> list[Path]:
        files = [self.root / "AGENTS.md", self.cursor_dir / "hooks.json"]
        files += [p for p in self.lib.rglob("*") if p.is_file()]
        return files

    def _snapshot_integrity(self) -> None:
        self._integrity = {str(p): _sha(p) for p in self._protected_files()}

    def verify_integrity(self) -> list[str]:
        """Return tampered files and restore them. Called before and after each run."""
        tampered = []
        current = {str(p): _sha(p) for p in self._protected_files()}
        for path, digest in self._integrity.items():
            if current.get(path) != digest:
                tampered.append(path)
        extra = set(current) - set(self._integrity)
        tampered.extend(sorted(extra))
        if tampered:
            log.warning("protected workspace files changed, restoring: %s", tampered)
            self.ensure()
        return tampered

    # ------------------------------------------------------------------ tasks
    def new_task(self, request: UserRequest) -> TaskContext:
        task_id = time.strftime("%Y%m%d-%H%M%S-") + request.id[:6]
        folder = self.tasks / task_id
        (folder / "ingested").mkdir(parents=True, exist_ok=True)
        (folder / "request.md").write_text(
            f"# User request\n\nSource: {request.source.value}  Language: {request.language}\n\n{request.text}\n",
            encoding="utf-8")
        return TaskContext(request=request, task_id=task_id, folder=folder)

    def write_context(self, ctx: TaskContext, extra: dict[str, str]) -> Path:
        allowed = [str(p) for p in self.settings.allowed_folder_paths()]
        full = getattr(self.settings, "autonomy", "safe") == "full"
        lines = ["# Task context", "", f"Task folder: {ctx.folder}", f"Outputs folder: {self.outputs}",
                 f"Time: {time.strftime('%A %d %B %Y, %H:%M')}", "",
                 f"Autonomy: {'FULL - do the whole request now; only deletions need the user' if full else 'SAFE'}",
                 f"User profile: {Path.home()}", "",
                 "Main user folders:" if full else "User folders you may read (writes only inside an approved plan):"]
        lines += [f"- {p}" for p in allowed] or ["- (none)"]
        for key, value in extra.items():
            if value:
                lines += ["", f"## {key}", value]
        path = ctx.folder / "context.md"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def policy_context(self, ctx: TaskContext, mode: Mode, allowed_mcp_tools: list[str] | None = None,
                       autonomy: str | None = None) -> PolicyContext:
        s = self.settings
        autonomy = autonomy or getattr(s, "autonomy", "safe")
        roots = [str(p) for p in s.allowed_folder_paths() if str(p) not in s.excluded_folders]
        return PolicyContext(
            mode=mode.value,
            workspace=str(self.root),
            task_folder=str(ctx.folder),
            read_roots=roots,
            write_roots=roots if mode == Mode.EXECUTE else [],
            protected=[str(p) for p in self.protected_paths()] + [
                str(ctx.folder / n) for n in ("plan.md", "request.md", "context.md", "ingested")],
            tainted=ctx.tainted,
            allow_network=False,
            web_allowlist=list(s.web_allowlist),
            allow_install=s.allow_install_software,
            approved_plan_targets=list(ctx.plan.targets) if ctx.plan else [],
            task_id=ctx.task_id,
            library_dir=str(self.lib),
            library_tiers=self.library_tiers(),
            allowed_mcp_tools=list(allowed_mcp_tools or []),
            autonomy=autonomy,
        )

    def write_policy_state(self, pctx: PolicyContext) -> Path:
        path = paths.policy_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(pctx.__dict__, indent=1), encoding="utf-8")
        os.replace(tmp, path)
        return path

    def purge_old_tasks(self) -> int:
        cutoff = time.time() - self.settings.retention_days * 86400
        removed = 0
        for d in self.tasks.iterdir() if self.tasks.exists() else []:
            try:
                if d.is_dir() and d.stat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
                    removed += 1
            except OSError:
                pass
        return removed
