# tier 1 - Copy files into a folder (never overwrites: adds " (n)" on conflict).
param([Parameter(Mandatory=$true)][string[]]$Paths, [Parameter(Mandatory=$true)][string]$Destination, [switch]$DryRun)
. "$PSScriptRoot\_common.ps1"

function Get-FreeName([string]$Dir, [string]$Name) {
    $target = Join-Path $Dir $Name; $i = 1
    $base = [IO.Path]::GetFileNameWithoutExtension($Name); $ext = [IO.Path]::GetExtension($Name)
    while (Test-Path -LiteralPath $target) { $target = Join-Path $Dir ("{0} ({1}){2}" -f $base, $i, $ext); $i++ }
    return $target
}

$done = @()
try {
    $dest = Assert-JarvisPath $Destination -Write
    $src = @($Paths | ForEach-Object { $_ -split ',' } | Where-Object { $_.Trim() } | ForEach-Object { Assert-JarvisPath $_.Trim() })
    if ($src.Count -gt 2000) { throw 'Too many files (cap 2000)' }
    $ops = @($src | ForEach-Object { @{ from = $_; to = (Get-FreeName $dest (Split-Path $_ -Leaf)) } })
    if ($DryRun) { Out-JarvisJson @{ ok = $true; dry_run = $true; operations = $ops }; exit 0 }
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    foreach ($op in $ops) { Copy-Item -LiteralPath $op.from -Destination $op.to -Recurse; $done += $op }
    Out-JarvisJson @{ ok = $true; copied = $done }
} catch { Out-JarvisJson @{ ok = $false; error = $_.Exception.Message; copied = $done }; exit 1 }
