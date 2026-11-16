# tier 1 - Launch an app, file, folder or URL DETACHED (never waits for it to exit).
# Resolves: URL/URI, existing path, PATH executables, App Paths registry, Start Menu shortcuts, Store apps.
param([Parameter(Mandatory=$true)][string]$Target, [string]$Arguments = '', [switch]$DryRun)
. "$PSScriptRoot\_common.ps1"

function Find-App([string]$Name) {
    if ($Name -match '^[a-z][a-z0-9+.\-]*:' -and $Name -notmatch '^[a-z]:\\') { return @{ kind = 'uri'; path = $Name } }
    if (Test-Path -LiteralPath $Name) { return @{ kind = 'path'; path = (Resolve-JarvisPath $Name) } }
    $cmd = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cmd) { return @{ kind = 'exe'; path = $cmd.Source } }
    foreach ($hive in 'HKCU:', 'HKLM:') {
        $k = "$hive\Software\Microsoft\Windows\CurrentVersion\App Paths\$Name.exe"
        if (Test-Path $k) {
            $v = (Get-ItemProperty $k).'(default)'
            if ($v) { return @{ kind = 'exe'; path = $v.Trim('"') } }
        }
    }
    $dirs = @("$env:APPDATA\Microsoft\Windows\Start Menu\Programs", "$env:ProgramData\Microsoft\Windows\Start Menu\Programs")
    $lnks = @(Get-ChildItem -LiteralPath $dirs -Filter *.lnk -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.BaseName -notmatch 'uninstall' })
    $hit = $lnks | Where-Object { $_.BaseName -ieq $Name } | Select-Object -First 1
    if (-not $hit) { $hit = $lnks | Where-Object { $_.BaseName -ilike "*$Name*" } | Sort-Object { $_.BaseName.Length } | Select-Object -First 1 }
    if ($hit) { return @{ kind = 'shortcut'; path = $hit.FullName; name = $hit.BaseName } }
    $pkg = Get-StartApps -ErrorAction SilentlyContinue | Where-Object { $_.Name -ilike "*$Name*" } |
        Sort-Object { $_.Name.Length } | Select-Object -First 1
    if ($pkg) { return @{ kind = 'appx'; path = "shell:AppsFolder\$($pkg.AppID)"; name = $pkg.Name } }
    return $null
}

try {
    $app = Find-App $Target
    if (-not $app) { throw "No app, file or URL found for '$Target'" }
    if ($app.kind -eq 'uri' -and $app.path -notmatch '^(https?|mailto|ms-settings|spotify|steam|zoommtg|msteams|calculator|ms-clock|ms-photos):') {
        throw "URI scheme not allowed: $($app.path)"
    }
    if ($app.kind -eq 'path' -and $app.path -match '\.(ps1|bat|cmd|vbs|js|msi|reg|scr|hta)$') { throw "Refusing to launch a script/installer: $($app.path)" }
    if ($DryRun) { Out-JarvisJson @{ ok = $true; dry_run = $true; would_launch = $app }; exit 0 }
    if ($app.kind -eq 'appx') { Start-Process explorer.exe -ArgumentList $app.path }
    elseif ($Arguments) { Start-Process -FilePath $app.path -ArgumentList $Arguments }
    else { Start-Process -FilePath $app.path }
    Out-JarvisJson @{ ok = $true; launched = $app }
} catch { Out-JarvisJson @{ ok = $false; error = $_.Exception.Message }; exit 1 }
