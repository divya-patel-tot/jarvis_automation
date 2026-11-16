# tier 2 - Send files/folders to the Recycle Bin (never permanent delete) with an undo manifest.
param([Parameter(Mandatory=$true)][string[]]$Paths, [switch]$DryRun)
. "$PSScriptRoot\_common.ps1"
Add-Type -AssemblyName Microsoft.VisualBasic
$done = @()
try {
    $src = @($Paths | ForEach-Object { $_ -split ',' } | Where-Object { $_.Trim() } | ForEach-Object { Assert-JarvisPath $_.Trim() -Write })
    if ($src.Count -eq 0) { throw 'Nothing to recycle' }
    if ($src.Count -gt 1000) { throw 'Too many items (cap 1000)' }
    $pol = Get-JarvisPolicy
    $roots = @($pol.workspace) + @($pol.read_roots) + @($pol.write_roots) + @($pol.task_folder)
    foreach ($s in $src) {
        if (-not (Test-Path -LiteralPath $s)) { throw "Missing: $s" }
        foreach ($r in $roots) {
            if ($r -and ($s.ToLowerInvariant() -eq (Resolve-JarvisPath $r).ToLowerInvariant())) { throw "Refusing to recycle a whole allowed folder: $s" }
        }
    }
    if ($DryRun) { Out-JarvisJson @{ ok = $true; dry_run = $true; would_recycle = $src }; exit 0 }
    foreach ($s in $src) {
        $isDir = (Get-Item -LiteralPath $s).PSIsContainer
        if ($isDir) { [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory($s, 'OnlyErrorDialogs', 'SendToRecycleBin') }
        else { [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile($s, 'OnlyErrorDialogs', 'SendToRecycleBin') }
        $done += @{ path = $s; dir = $isDir }
    }
    $m = Write-UndoManifest 'recycle' $done
    Out-JarvisJson @{ ok = $true; recycled = $done; undo_manifest = $m }
} catch {
    $m = $null
    if ($done.Count -gt 0) { $m = Write-UndoManifest 'recycle' $done }
    Out-JarvisJson @{ ok = $false; error = $_.Exception.Message; recycled = $done; undo_manifest = $m }; exit 1
}
