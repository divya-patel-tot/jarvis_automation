"""Command and path policy engine.

Pure, stdlib-only and deterministic so it can be imported by the Cursor
``preToolUse`` hook process (pre-execution enforcement, Phase 0 D-006) *and*
by the in-app observer (defence in depth), and be unit tested.

Decision values: ``allow``, ``deny`` (forbidden/tier 3 or out of policy),
``ask`` (allowed only inside an approved execute run).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

# --------------------------------------------------------------------------- shell
# Tier 3: never executed (security tooling, credentials, registry, boot, formatting, UAC...).
FORBIDDEN_COMMAND_PATTERNS: list[tuple[str, str]] = [
    (r"\bformat(\.com)?\s+[a-z]:", "disk formatting"),
    (r"\b(format-volume|clear-disk|initialize-disk|remove-partition|diskpart)\b", "disk/partition changes"),
    (r"\bcipher(\.exe)?\s+/w", "secure wipe"),
    (r"\b(reg(\.exe)?\s+(add|delete|import|load|unload|restore|copy|save|export))\b", "registry modification"),
    (r"\b(set-itemproperty|new-itemproperty|remove-itemproperty|remove-item|new-item|set-item)\b[^|;]*\b(hklm|hkcu|hkcr|hku|registry::)", "registry modification"),
    (r"\b(bcdedit|bootrec|bcdboot)\b", "boot configuration"),
    (r"\b(set-mppreference|add-mppreference|remove-mppreference)\b", "Defender settings"),
    (r"\b(netsh\s+(advfirewall|firewall|interface|wlan\s+(delete|set|add)))", "network/firewall config"),
    (r"\b(set-netfirewall\w*|new-netfirewall\w*|remove-netfirewall\w*|disable-netadapter|set-netipaddress|new-netipaddress|set-dnsclientserveraddress)\b", "network/firewall config"),
    (r"\b(net\s+(user|localgroup|accounts|share))\b", "user/permission management"),
    (r"\b(new-localuser|remove-localuser|set-localuser|add-localgroupmember|remove-localgroupmember)\b", "user management"),
    (r"\b(icacls|takeown|cacls|set-acl)\b", "permission changes"),
    (r"\b(sc(\.exe)?\s+(create|delete|config|stop|failure))\b", "service changes"),
    (r"\b(set-service|new-service|remove-service|stop-service)\b", "service changes"),
    (r"\b(schtasks(\.exe)?\s+/(create|delete|change))\b", "scheduled task changes"),
    (r"\b(register-scheduledtask|unregister-scheduledtask|set-scheduledtask)\b", "scheduled task changes"),
    (r"\b(vssadmin|wbadmin|wmic\s+shadowcopy)\b", "shadow copy/backup tampering"),
    (r"\b(mimikatz|procdump|lsass|sekurlsa|cmdkey|vaultcmd)\b", "credential access"),
    (r"\b(get-credential|convertfrom-securestring|export-pfxcertificate)\b", "credential access"),
    (r"\b(set-executionpolicy)\b", "execution policy change"),
    (r"\b(uac|consentpromptbehavior|enablelua)\b", "UAC tampering"),
    (r"\bstart-process\b[^|;]*-verb\s+runas", "elevation attempt"),
    (r"\b(runas(\.exe)?)\b", "elevation attempt"),
    (r"\b(shutdown(\.exe)?\s+/[srfp]|stop-computer|restart-computer)\b", "shutdown/restart"),
    (r"\b(rd|rmdir)\s+/s\b.*\b[a-z]:\\?\s*$", "recursive delete of a drive"),
    (r"remove-item\b[^|;]*\s[a-z]:\\?\*?\s*(-|$)", "delete at drive root"),
    (r"\b(del|erase)\b\s+/[sq].*[a-z]:\\\*", "mass deletion"),
    (r"\b(invoke-webrequest|iwr|curl|wget|invoke-restmethod|irm)\b[^|;]*\|\s*(iex|invoke-expression)", "download-and-execute"),
    (r"\b(iex|invoke-expression)\b", "dynamic code execution"),
    (r"-enc(odedcommand)?\s+[a-z0-9+/=]{16,}", "encoded command"),
    (r"\b(anydesk|teamviewer|ngrok|psexec|winrm|enable-psremoting|enter-pssession|ssh-keygen)\b", "remote access tooling"),
    (r"\b(add-type)\b[^|;]*dllimport", "native code injection"),
    (r"\bwevtutil\s+(cl|clear-log)|clear-eventlog\b", "log tampering"),
]

# Tier 2: allowed only inside an approved execute run.
CONSEQUENTIAL_COMMAND_PATTERNS: list[tuple[str, str]] = [
    (r"\b(remove-item|rm|del|erase|rd|rmdir|ri)\b", "deletes files"),
    (r"\b(move-item|mv|move|ren|rename-item)\b", "moves/renames files"),
    (r"\b(set-content|out-file|add-content|clear-content)\b", "writes files"),
    (r"\b(winget|choco|scoop|msiexec|pip3?|npm|yarn|pnpm)\s+(install|uninstall|upgrade|add|i)\b", "installs software"),
    (r"\b(stop-process|taskkill|kill)\b", "terminates processes"),
    (r"\b(send-mailmessage|invoke-webrequest|invoke-restmethod|iwr|irm|curl|wget|bitsadmin|start-bitstransfer)\b", "network transfer"),
    (r"\b(compress-archive|expand-archive)\b", "creates/extracts archives"),
    (r"\b(copy-item|cp|copy|xcopy|robocopy)\b", "copies files"),
    (r"\bset-clipboard\b", "changes clipboard"),
    (r"\.(ps1|bat|cmd|vbs|js|py)\b", "runs a script"),
]

READ_ONLY_COMMANDS = re.compile(
    r"^\s*(get-childitem|gci|ls|dir|get-content|gc|cat|type|get-item|gi|test-path|resolve-path|"
    r"get-process|gps|get-date|get-volume|get-psdrive|get-computerinfo|get-ciminstance|"
    r"select-string|sls|measure-object|where-object|select-object|sort-object|format-\w+|"
    r"echo|write-output|write-host|pwd|get-location|whoami|hostname|systeminfo|tasklist|ipconfig|"
    r"get-filehash|get-clipboard|findstr|where(\.exe)?|get-command)\b", re.I)

# --------------------------------------------------------------------------- paths
SENSITIVE_PATH_PATTERNS: list[tuple[str, str]] = [
    (r"\\\.ssh(\\|$)", "SSH keys"),
    (r"\\\.gnupg(\\|$)", "GPG keys"),
    (r"\\\.aws(\\|$)|\\\.azure(\\|$)|\\\.kube(\\|$)|\\\.docker\\config", "cloud credentials"),
    (r"\\appdata\\(local|roaming)\\(google\\chrome|microsoft\\edge|mozilla\\firefox|bravesoftware|opera software)", "browser profile"),
    (r"\\appdata\\(local|roaming)\\microsoft\\(credentials|protect|vault)", "credential store"),
    (r"\\appdata\\local\\jarvis(\\|$)", "Jarvis audit/state"),
    (r"\\appdata\\(local|roaming)\\(keepass|1password|bitwarden)", "password manager"),
    (r"\\windows\\system32\\config(\\|$)", "registry hives"),
    (r"(\\|^)(ntuser\.dat|sam|security|system)(\.log\d?)?$", "registry hives"),
    (r"\.(pem|pfx|p12|key|kdbx|keychain|ppk)$", "secret-like file"),
    (r"(\\|^)(\.env|\.netrc|\.pgpass|id_rsa|id_ed25519|credentials(\.json)?|secrets?\.(json|ya?ml|toml))$", "secret-like file"),
    (r"\\\.cursor\\(cli-config|mcp)\.json$", "Cursor user config"),
]

SYSTEM_ROOTS = [r"c:\windows", r"c:\program files", r"c:\program files (x86)", r"c:\programdata",
                r"c:\$recycle.bin", r"c:\system volume information", r"c:\recovery", r"c:\boot"]

PATH_TOKEN = re.compile(r"""(?:"([a-zA-Z]:\\[^"]+|\\\\[^"]+)"|'([a-zA-Z]:\\[^']+)'|((?:[a-zA-Z]:|%\w+%|\$env:\w+|~)\\?[^\s"'|;,<>]*))""")


def norm(path: str) -> str:
    p = os.path.expandvars(str(path).strip().strip('"').strip("'"))
    if p.startswith("~"):
        p = os.path.expanduser(p)
    home = os.environ.get("USERPROFILE", "")
    p = re.sub(r"(?i)^\$env:userprofile", lambda _m: home, p)
    p = p.replace("/", "\\")
    try:
        p = os.path.normpath(p)
    except Exception:
        pass
    # Resolve 8.3 short names (C:\Users\DIVYA~1.PAT -> long form) when possible.
    if "~" in p and os.name == "nt" and os.path.exists(p):
        try:
            import ctypes

            buf = ctypes.create_unicode_buffer(1024)
            if ctypes.windll.kernel32.GetLongPathNameW(p, buf, 1024):
                p = buf.value
        except Exception:
            pass
    return p.lower().rstrip("\\")


def is_within(path: str, root: str) -> bool:
    p, r = norm(path), norm(root)
    return p == r or p.startswith(r + "\\")


@dataclass
class PolicyContext:
    """Per-run context written by the app before each run (outside the workspace)."""
    mode: str = "plan"                                 # plan | execute | direct
    workspace: str = ""
    task_folder: str = ""
    read_roots: list[str] = field(default_factory=list)
    write_roots: list[str] = field(default_factory=list)   # effective for this run
    protected: list[str] = field(default_factory=list)     # workspace paths the agent may not write (rules, lib, .cursor, memory)
    tainted: bool = False
    allow_network: bool = False
    web_allowlist: list[str] = field(default_factory=list)
    allow_install: bool = False
    approved_plan_targets: list[str] = field(default_factory=list)
    task_id: str = ""
    library_dir: str = ""                               # workspace\lib (vetted scripts)
    library_tiers: dict[str, int] = field(default_factory=dict)  # script stem -> declared tier
    allowed_mcp_tools: list[str] = field(default_factory=list)
    autonomy: str = "safe"                              # safe | full (see config.Settings.autonomy)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PolicyContext":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


@dataclass
class Decision:
    action: str          # allow | deny
    reason: str = ""
    tier: int = 0

    @property
    def allowed(self) -> bool:
        return self.action == "allow"


def match_patterns(text: str, patterns: Iterable[tuple[str, str]]) -> str | None:
    for pat, reason in patterns:
        if re.search(pat, text, re.I):
            return reason
    return None


def classify_command(command: str) -> tuple[int, str]:
    """Return (tier, reason) for a shell command."""
    cmd = command.strip()
    reason = match_patterns(cmd, FORBIDDEN_COMMAND_PATTERNS)
    if reason:
        return 3, reason
    reason = match_patterns(cmd, CONSEQUENTIAL_COMMAND_PATTERNS)
    if reason:
        return 2, reason
    segments = [s for s in re.split(r"[|;&]+", cmd) if s.strip()]
    if segments and all(READ_ONLY_COMMANDS.match(s) for s in segments):
        return 0, "read-only"
    return 1, "unclassified command"


def extract_paths(text: str) -> list[str]:
    out = []
    for m in PATH_TOKEN.finditer(text or ""):
        token = next((g for g in m.groups() if g), "")
        if len(token) > 2:
            out.append(token)
    return out


def sensitive_reason(path: str) -> str | None:
    p = norm(path)
    reason = match_patterns(p, SENSITIVE_PATH_PATTERNS)
    if reason:
        return reason
    return None


def _in_any(path: str, roots: Iterable[str]) -> bool:
    return any(root and is_within(path, root) for root in roots)


def check_read(path: str, ctx: PolicyContext) -> Decision:
    reason = sensitive_reason(path)
    if reason:
        return Decision("deny", f"reading {reason} is not allowed", 3)
    if ctx.autonomy == "full":
        return Decision("allow", tier=0)
    roots = [ctx.workspace, *ctx.read_roots, *ctx.write_roots]
    if not _in_any(path, roots):
        return Decision("deny", f"read outside allowed folders: {path}", 2)
    return Decision("allow", tier=0)


def check_write(path: str, ctx: PolicyContext) -> Decision:
    reason = sensitive_reason(path)
    if reason:
        return Decision("deny", f"writing {reason} is not allowed", 3)
    p = norm(path)
    if any(p == norm(sr) or p.startswith(norm(sr) + "\\") for sr in SYSTEM_ROOTS):
        return Decision("deny", "writing to system folders is not allowed", 3)
    if _in_any(path, ctx.protected):
        return Decision("deny", "rule files, capability library, memory and config are read-only for the agent", 3)
    if ctx.autonomy == "full" and ctx.mode != "plan":
        return Decision("allow", tier=1)
    if ctx.mode == "plan":
        # Plan phase: nothing may change, except notes inside the task folder.
        if ctx.task_folder and is_within(path, ctx.task_folder):
            return Decision("allow", tier=1)
        return Decision("deny", "plan mode: no changes allowed", 2)
    if ctx.task_folder and is_within(path, ctx.task_folder):
        return Decision("allow", tier=1)
    if _in_any(path, [ctx.workspace]):
        return Decision("allow", tier=1)
    if ctx.mode == "execute" and _in_any(path, ctx.write_roots):
        return Decision("allow", tier=2)
    return Decision("deny", f"write outside allowed folders: {path}", 2)


LIB_CALL = re.compile(r"""(?:^|[\s&'"])(?:\.\\|[^\s'"]*\\)?lib\\(?:ps|py)\\([\w\-]+)\.(ps1|py)\b""", re.I)


def library_call_tier(command: str, ctx: PolicyContext) -> int | None:
    """If the command only invokes vetted library scripts, return their max declared tier."""
    calls = LIB_CALL.findall(command or "")
    if not calls or not ctx.library_tiers:
        return None
    tiers = []
    for stem, _ext in calls:
        if stem.lower() not in {k.lower() for k in ctx.library_tiers}:
            return None
        tiers.append(next(v for k, v in ctx.library_tiers.items() if k.lower() == stem.lower()))
    # Anything chained besides the script call is classified normally.
    rest = LIB_CALL.sub(" ", command)
    rest = re.sub(r"(?i)\b(powershell(\.exe)?|pwsh|python(\.exe)?|py|-noprofile|-executionpolicy\s+bypass|-file|&)\b", " ", rest)
    rest = re.sub(r"\s-\w+\s+(\"[^\"]*\"|'[^']*'|\S+)", " ", rest)  # named args
    rest_tier = classify_command(rest)[0] if rest.strip(" '\"") else 0
    if rest_tier >= 2:
        return None
    return max(tiers)


# ----------------------------------------------------------------- full autonomy
# Never allowed, even in full autonomy: stealing credentials or switching off the
# PC's own protections. Everything else the user asks for is allowed.
ALWAYS_FORBIDDEN: list[tuple[str, str]] = [
    (r"\b(set-mppreference|add-mppreference|remove-mppreference)\b", "Defender settings"),
    (r"\b(mimikatz|procdump|lsass|sekurlsa|vaultcmd)\b", "credential theft"),
    (r"\b(convertfrom-securestring|export-pfxcertificate)\b", "credential export"),
    (r"\b(consentpromptbehavior|enablelua)\b", "UAC tampering"),
    (r"\b(bcdedit|bootrec|bcdboot)\b", "boot configuration"),
    (r"\b(vssadmin|wbadmin|wmic\s+shadowcopy)\b", "backup/shadow copy tampering"),
    (r"\bwevtutil\s+(cl|clear-log)|clear-eventlog\b", "log tampering"),
    (r"\b(add-type)\b[^|;]*dllimport", "native code injection"),
]
# Deleting/destroying data: allowed in full autonomy only after ONE confirmation.
DELETE_PATTERNS: list[tuple[str, str]] = [
    (r"\b(remove-item|ri|rm|rmdir|rd|del|erase)\b", "deletes files"),
    (r"\brecycle\.ps1\b", "recycles files"),
    (r"\b(clear-recyclebin|clear-content|clear-item)\b", "clears data"),
    (r"\[(system\.)?io\.(file|directory)\]::delete\b|\.delete\(\)", "deletes files"),
    (r"\bformat(\.com)?\s+[a-z]:|\b(format-volume|clear-disk|initialize-disk|remove-partition|diskpart)\b"
     r"|\bcipher(\.exe)?\s+/w", "erases a disk"),
    (r"\b(winget|choco|scoop)\s+(uninstall|remove)\b|\bmsiexec\b[^|;]*\s/x\b|\buninstall-package\b|\bremove-appxpackage\b",
     "uninstalls software"),
    (r"\b(remove-localuser|net\s+user\s+\S+\s+/delete)\b", "deletes a user account"),
    (r"\breg(\.exe)?\s+delete\b|\bremove-itemproperty\b", "deletes registry data"),
    (r"\bgit\s+(clean\s+-[a-z]*f|reset\s+--hard|push\s+.*--force)", "discards git data"),
]
NETWORK_SEND = re.compile(r"\b(invoke-webrequest|iwr|irm|invoke-restmethod|curl|wget|start-bitstransfer|"
                          r"send-mailmessage|send_mail\.py|ftp|scp|sftp)\b", re.I)
DELETE_MARK = "NEEDS DELETE CONFIRMATION"


def is_delete_command(command: str) -> str | None:
    return match_patterns(command or "", DELETE_PATTERNS)


def _check_shell_full(command: str, ctx: PolicyContext) -> Decision:
    reason = match_patterns(command, ALWAYS_FORBIDDEN)
    if reason:
        return Decision("deny", f"forbidden: {reason}", 3)
    is_lib = library_call_tier(command, ctx) is not None
    for raw in extract_paths(command):
        r = sensitive_reason(raw)
        if r:
            return Decision("deny", f"command touches {r}", 3)
        # Protected Jarvis files (rules, hook, library, memory, app data): read/run only.
        if _in_any(raw, ctx.protected) and not is_lib and classify_command(command)[0] >= 1:
            return Decision("deny", "Jarvis's own rules/library/config can't be modified by the agent", 3)
    if ctx.tainted:
        # Untrusted content was read in this task: keep the injection defences.
        r = match_patterns(command, FORBIDDEN_COMMAND_PATTERNS)
        if r:
            return Decision("deny", f"blocked after reading untrusted content: {r}", 3)
        if NETWORK_SEND.search(command):
            return Decision("deny", "network send blocked: this task read untrusted web/document content", 2)
    deletes = is_delete_command(command)
    if deletes and ctx.mode != "execute":
        return Decision("deny", f"{DELETE_MARK}: {deletes}. Stop and list exactly what you wanted to delete; "
                                "Jarvis will ask the user once and then let you do it", 2)
    return Decision("allow", tier=2 if deletes else 1)


def check_shell(command: str, ctx: PolicyContext) -> Decision:
    if ctx.autonomy == "full" and ctx.mode != "plan":
        return _check_shell_full(command, ctx)
    tier, reason = classify_command(command)
    if tier >= 3:
        return Decision("deny", f"forbidden: {reason}", 3)
    lib_tier = library_call_tier(command, ctx)
    if lib_tier is not None:
        if lib_tier >= 3:
            return Decision("deny", "library script is disabled", 3)
        if lib_tier >= 2 and ctx.mode != "execute":
            return Decision("deny", "this library action needs plan approval first", 2)
        if lib_tier >= 1 and ctx.mode == "plan":
            if "-dryrun" in command.lower():
                return Decision("allow", tier=0)
            return Decision("deny", "plan mode: changes wait for approval (preview with -DryRun)", 2)
        return Decision("allow", tier=lib_tier)
    if re.search(r"\b(winget|choco|scoop|msiexec)\b", command, re.I) and not ctx.allow_install:
        return Decision("deny", "installing software is disabled in settings", 3)
    for raw in extract_paths(command):
        r = sensitive_reason(raw)
        if r:
            return Decision("deny", f"command touches {r}", 3)
    if ctx.tainted and re.search(r"\b(invoke-webrequest|iwr|irm|invoke-restmethod|curl|wget|start-bitstransfer|send-mailmessage)\b", command, re.I):
        return Decision("deny", "network transfer blocked: task has read untrusted content", 3)
    if tier == 2:
        if ctx.mode != "execute":
            return Decision("deny", f"{reason}: only allowed after plan approval", 2)
        # In execute mode, any explicit path the command writes to must be in an allowed root.
        for raw in extract_paths(command):
            p = norm(raw)
            if any(p == norm(sr) or p.startswith(norm(sr) + "\\") for sr in SYSTEM_ROOTS):
                return Decision("deny", "command targets a system folder", 3)
            if _in_any(raw, ctx.protected):
                return Decision("deny", "command targets protected Jarvis files", 3)
            if not _in_any(raw, [ctx.workspace, *ctx.write_roots, *ctx.read_roots]):
                return Decision("deny", f"command targets a path outside allowed folders: {raw}", 2)
        return Decision("allow", tier=2)
    if tier == 1 and ctx.mode == "plan":
        # Unknown commands in plan mode: allow only if they look like inspection.
        if re.search(r"\b(start-process|start|explorer|invoke-item|ii)\b", command, re.I):
            return Decision("deny", "plan mode: launching programs waits for approval", 2)
    return Decision("allow", tier=tier)


def check_url(url: str, ctx: PolicyContext) -> Decision:
    host = re.sub(r"^[a-z]+://", "", url.lower()).split("/")[0].split(":")[0]
    if any(host == d or host.endswith("." + d) for d in ctx.web_allowlist):
        return Decision("allow", tier=0)
    if ctx.autonomy == "full" and not ctx.tainted:
        return Decision("allow", tier=1)
    if ctx.allow_network and not ctx.tainted:
        return Decision("allow", tier=1)
    return Decision("deny", f"web fetch outside allowlist: {host}", 2)


# Tool names observed in Phase 0 (SDK): shell, read/Read, edit/Write, createPlan, ...
_SHELL = {"shell", "bash", "terminal", "run_terminal_cmd", "runcommand", "powershell", "shelltoolcall"}
_READ = {"read", "read_file", "readfile", "glob", "grep", "ls", "list_dir", "listdir", "search", "codebase_search",
         "file_search", "semsearch", "readtoolcall", "globtoolcall", "greptoolcall", "lstoolcall"}
_WRITE = {"write", "edit", "edit_file", "write_file", "delete", "delete_file", "multiedit", "multi_edit",
          "search_replace", "strreplace", "apply_patch", "notebookedit", "edittoolcall", "writetoolcall", "deletetoolcall"}
_WEB = {"webfetch", "web_fetch", "fetch", "websearch", "web_search"}
_PLAN = {"createplan", "create_plan", "todowrite", "todo_write", "updatetodos"}


def tool_category(name: str) -> str:
    n = (name or "").lower().replace("-", "_")
    flat = n.replace("_", "")
    if n in _SHELL or flat in _SHELL:
        return "shell"
    if n in _WRITE or flat in _WRITE:
        return "file_write"
    if n in _READ or flat in _READ:
        return "file_read"
    if n in _WEB or flat in _WEB:
        return "web_fetch"
    if n in _PLAN or flat in _PLAN:
        return "plan"
    if n.startswith("mcp") or ":" in n or "__" in n:
        return "mcp"
    return "other"


def _first(d: dict[str, Any], *keys: str) -> str:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v:
            return v
    return ""


def check_tool(name: str, args: dict[str, Any] | None, ctx: PolicyContext,
               allowed_mcp_tools: Iterable[str] = ()) -> Decision:
    args = args or {}
    cat = tool_category(name)
    if cat == "shell":
        return check_shell(_first(args, "command", "cmd", "commandLine", "script"), ctx)
    if cat == "file_read":
        path = _first(args, "file_path", "path", "target_file", "filePath", "target_directory", "directory", "cwd")
        return check_read(path, ctx) if path else Decision("allow")
    if cat == "file_write":
        path = _first(args, "file_path", "path", "target_file", "filePath")
        is_delete_tool = "delete" in (name or "").lower()
        if is_delete_tool and ctx.autonomy == "full" and ctx.mode != "execute":
            return Decision("deny", f"{DELETE_MARK}: deletes {path}. Stop and list exactly what you wanted to "
                                    "delete; Jarvis will ask the user once", 2)
        return check_write(path, ctx) if path else Decision("deny", "write without a path", 2)
    if cat == "web_fetch":
        url = _first(args, "url", "uri")
        return check_url(url, ctx) if url else (
            Decision("allow") if not ctx.tainted else Decision("deny", "web search blocked for tainted task", 2))
    if cat == "plan":
        return Decision("allow")
    if cat == "mcp":
        short = (name or "").split("__")[-1].split(":")[-1].lower()
        allowed = {t.lower() for t in allowed_mcp_tools}
        if short in allowed or (name or "").lower() in allowed:
            return Decision("allow", tier=1)
        return Decision("deny", f"MCP tool not allowed: {name}", 2)
    # Unknown built-in tools (subagents, todo, etc.) are allowed; their effects
    # still go through the shell/file tools which are checked individually.
    return Decision("allow")
