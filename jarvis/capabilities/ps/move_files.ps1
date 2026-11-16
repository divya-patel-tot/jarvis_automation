# tier 2 - Move files into a folder with an undo manifest (never overwrites).
param([Parameter(Mandatory=$true)][string[]]$Paths, [Parameter(Mandatory=$true)][string]$Destination, [switch]$DryRun)
. "$PSScriptRoot\_common.ps1"
$done = @()
try {
    $dest = Assert-JarvisPath $Destination -Write
    $src = @($Paths | ForEach-Object { $_ -split ',' } | Where-Object { $_.Trim() } | ForEach-Object { Assert-JarvisPath $_.Trim() -Write })
    if ($src.Count -gt 2000) { throw 'Too many files (cap 2000)' }
    $ops = @($src | ForEach-Object {
        $t = Join-Path $dest (Split-Path $_ -Leaf)
        if (Test-Path -LiteralPath $t) { throw "Target exists, refusing to overwrite: $t" }
        @{ from = $_; to = $t } })
    if ($DryRun) { Out-JarvisJson @{ ok = $true; dry_run = $true; operations = $ops }; exit 0 }
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    foreach ($op in $ops) { Move-Item -LiteralPath $op.from -Destination $op.to; $done += $op }
    $m = Write-UndoManifest 'move' $done
    Out-JarvisJson @{ ok = $true; moved = $done; undo_manifest = $m }
} catch {
    $m = $null
    if ($done.Count -gt 0) { $m = Write-UndoManifest 'move' $done }
    Out-JarvisJson @{ ok = $false; error = $_.Exception.Message; moved = $done; undo_manifest = $m }; exit 1
}
