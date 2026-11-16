"""Undo from manifests written by the capability library (plan 10.2.12).

* move   -> move each file back (refuses if the original location is taken)
* recycle-> restore each item from the Recycle Bin (matched by original path)
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class UndoPlan:
    manifest: Path
    action: str
    task_id: str
    entries: list[dict]

    def describe(self) -> str:
        if self.action == "move":
            lines = [f"Move back {len(self.entries)} item(s):"]
            lines += [f"  {e['to']}  ->  {e['from']}" for e in self.entries[:30]]
        else:
            lines = [f"Restore {len(self.entries)} item(s) from the Recycle Bin:"]
            lines += [f"  {e['path']}" for e in self.entries[:30]]
        return "\n".join(lines)


def latest(workspace: Path, task_id: str | None = None) -> UndoPlan | None:
    d = workspace / "undo"
    if not d.exists():
        return None
    files = sorted((p for p in d.glob("*.json") if not p.name.endswith(".done.json")),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        if task_id and data.get("task_id") != task_id:
            continue
        entries = data.get("entries") or []
        if isinstance(entries, dict):
            entries = [entries]
        return UndoPlan(f, data.get("action", ""), data.get("task_id", ""), entries)
    return None


RESTORE_PS = r"""
param([string]$Json)
$want = @{}; foreach ($p in ($Json | ConvertFrom-Json)) { $want[$p.ToLowerInvariant()] = $true }
$shell = New-Object -ComObject Shell.Application
$bin = $shell.Namespace(10)
$restored = @()
foreach ($item in @($bin.Items())) {
    $from = $item.ExtendedProperty('System.Recycle.DeletedFrom')
    $orig = (Join-Path $from $item.Name).ToLowerInvariant()
    if ($want.ContainsKey($orig) -and -not (Test-Path -LiteralPath $orig)) {
        $item.InvokeVerb('undelete'); $restored += $orig
    }
}
$restored | ConvertTo-Json -Compress
"""


def execute(plan: UndoPlan) -> tuple[list[str], list[str]]:
    """Return (done, failed)."""
    done, failed = [], []
    if plan.action == "move":
        for e in reversed(plan.entries):
            src, dst = Path(e["to"]), Path(e["from"])
            try:
                if dst.exists():
                    raise FileExistsError(f"{dst} already exists")
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                done.append(str(dst))
            except Exception as exc:
                failed.append(f"{src}: {exc}")
    elif plan.action == "recycle":
        paths = [e["path"] for e in plan.entries]
        r = subprocess.run(["powershell", "-NoProfile", "-Command",
                            f"& {{ {RESTORE_PS} }} -Json '{json.dumps(paths).replace(chr(39), chr(39) * 2)}'"],
                           capture_output=True, text=True, timeout=120, creationflags=0x08000000,
                           encoding="utf-8", errors="replace")
        try:
            restored = json.loads(r.stdout.strip() or "[]")
            restored = [restored] if isinstance(restored, str) else restored
        except json.JSONDecodeError:
            restored = []
        got = {p.lower() for p in restored}
        for p in paths:
            (done if p.lower() in got or Path(p).exists() else failed).append(p)
    else:
        failed.append(f"unknown manifest action {plan.action}")
    if not failed:
        plan.manifest.rename(plan.manifest.with_name(plan.manifest.stem + ".done.json"))
    return done, failed
