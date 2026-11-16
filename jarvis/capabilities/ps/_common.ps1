# Shared helpers for Jarvis capability scripts (Windows PowerShell 5.1 compatible).
# Every script: strict mode, JSON output, path allowlist enforced from the per-run policy state.
Set-StrictMode -Version 2
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

function Get-JarvisPolicy {
    $p = $env:JARVIS_POLICY_STATE
    if (-not $p) { $p = Join-Path $env:LOCALAPPDATA 'Jarvis\state\policy_state.json' }
    if (-not (Test-Path -LiteralPath $p)) { return $null }
    try { return Get-Content -LiteralPath $p -Raw -Encoding UTF8 | ConvertFrom-Json } catch { return $null }
}

function Resolve-JarvisPath([string]$Path) {
    $expanded = [Environment]::ExpandEnvironmentVariables($Path)
    if ($expanded.StartsWith('~')) { $expanded = $env:USERPROFILE + $expanded.Substring(1) }
    if (-not [System.IO.Path]::IsPathRooted($expanded)) { $expanded = Join-Path (Get-Location).Path $expanded }
    return [System.IO.Path]::GetFullPath($expanded).TrimEnd('\')
}

function Test-Within([string]$Path, [string]$Root) {
    if (-not $Root) { return $false }
    $p = (Resolve-JarvisPath $Path).ToLowerInvariant()
    $r = (Resolve-JarvisPath $Root).ToLowerInvariant()
    return ($p -eq $r) -or $p.StartsWith($r + '\')
}

$script:SensitiveRegex = '\\\.ssh(\\|$)|\\\.gnupg|\\\.aws(\\|$)|\\appdata\\(local|roaming)\\(google\\chrome|microsoft\\edge|mozilla|microsoft\\(credentials|protect|vault)|jarvis)|\\windows(\\|$)|\\program files|\\programdata(\\|$)|\.(pem|pfx|p12|key|kdbx|ppk)$|(\\|^)(\.env|id_rsa|id_ed25519)$'

function Assert-JarvisPath([string]$Path, [switch]$Write) {
    $full = Resolve-JarvisPath $Path
    if ($full.ToLowerInvariant() -match $script:SensitiveRegex) { throw "Blocked: protected location $full" }
    $pol = Get-JarvisPolicy
    if ($null -eq $pol) { throw "Blocked: no Jarvis policy context" }
    foreach ($prot in @($pol.protected)) { if ($prot -and (Test-Within $full $prot)) { throw "Blocked: protected Jarvis path $full" } }
    $roots = @($pol.workspace) + @($pol.task_folder)
    if ($Write) {
        if ($pol.mode -eq 'execute') { $roots += @($pol.write_roots) }
    } else {
        $roots += @($pol.read_roots) + @($pol.write_roots)
    }
    foreach ($r in $roots) { if ($r -and (Test-Within $full $r)) { return $full } }
    throw "Blocked: $full is outside the allowed folders"
}

function Out-JarvisJson($Object) {
    $Object | ConvertTo-Json -Depth 6 -Compress
}

function Write-UndoManifest([string]$Action, $Entries) {
    $pol = Get-JarvisPolicy
    $ws = if ($pol) { $pol.workspace } else { (Get-Location).Path }
    $dir = Join-Path $ws 'undo'
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $tid = if ($pol -and $pol.task_id) { $pol.task_id } else { 'manual' }
    $file = Join-Path $dir ("{0}-{1}-{2}.json" -f (Get-Date -Format 'yyyyMMdd-HHmmss'), $tid, $Action)
    @{ action = $Action; task_id = $tid; created = (Get-Date).ToString('o'); entries = @($Entries) } |
        ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $file -Encoding UTF8
    return $file
}
