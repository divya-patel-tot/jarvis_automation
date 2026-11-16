# tier 0 - Folder summary (read-only)
param([Parameter(Mandatory=$true)][string]$Path, [int]$Top = 15)
. "$PSScriptRoot\_common.ps1"
try {
    $root = Assert-JarvisPath $Path
    $files = @(Get-ChildItem -LiteralPath $root -File -Recurse -ErrorAction SilentlyContinue)
    $dirs = @(Get-ChildItem -LiteralPath $root -Directory -ErrorAction SilentlyContinue)
    $byExt = @($files | Group-Object { $_.Extension.ToLowerInvariant() } | Sort-Object Count -Descending |
        Select-Object -First 12 | ForEach-Object {
            @{ ext = $_.Name; count = $_.Count; mb = [math]::Round((($_.Group | Measure-Object Length -Sum).Sum) / 1MB, 2) } })
    $fmt = { param($f) @{ path = $f.FullName; mb = [math]::Round($f.Length / 1MB, 2); modified = $f.LastWriteTime.ToString('yyyy-MM-dd HH:mm') } }
    $total = ($files | Measure-Object Length -Sum).Sum
    if ($null -eq $total) { $total = 0 }
    Out-JarvisJson @{
        ok = $true; path = $root; files = $files.Count; subfolders = $dirs.Count
        total_mb = [math]::Round($total / 1MB, 2); by_extension = $byExt
        largest = @($files | Sort-Object Length -Descending | Select-Object -First $Top | ForEach-Object { & $fmt $_ })
        newest = @($files | Sort-Object LastWriteTime -Descending | Select-Object -First $Top | ForEach-Object { & $fmt $_ })
    }
} catch { Out-JarvisJson @{ ok = $false; error = $_.Exception.Message }; exit 1 }
