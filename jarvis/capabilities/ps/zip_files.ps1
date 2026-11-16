# tier 1 - Zip files into a NEW archive (never overwrites), then verify the entry count.
param([Parameter(Mandatory=$true)][string[]]$Paths, [Parameter(Mandatory=$true)][string]$Destination, [switch]$DryRun)
. "$PSScriptRoot\_common.ps1"
try {
    $src = @($Paths | ForEach-Object { $_ -split ',' } | Where-Object { $_.Trim() } | ForEach-Object { Assert-JarvisPath $_.Trim() })
    if ($src.Count -eq 0) { throw 'No input files' }
    if ($src.Count -gt 2000) { throw 'Too many files (cap 2000)' }
    foreach ($s in $src) { if (-not (Test-Path -LiteralPath $s)) { throw "Missing: $s" } }
    if (-not $Destination.ToLowerInvariant().EndsWith('.zip')) { $Destination += '.zip' }
    $dest = Assert-JarvisPath $Destination -Write
    if (Test-Path -LiteralPath $dest) { throw "Destination exists, refusing to overwrite: $dest" }
    if ($DryRun) { Out-JarvisJson @{ ok = $true; dry_run = $true; would_create = $dest; inputs = $src }; exit 0 }
    New-Item -ItemType Directory -Force -Path (Split-Path $dest) | Out-Null
    Compress-Archive -LiteralPath $src -DestinationPath $dest -CompressionLevel Optimal
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $z = [System.IO.Compression.ZipFile]::OpenRead($dest); $n = $z.Entries.Count; $z.Dispose()
    Out-JarvisJson @{ ok = $true; created = $dest; entries = $n; bytes = (Get-Item -LiteralPath $dest).Length }
} catch { Out-JarvisJson @{ ok = $false; error = $_.Exception.Message }; exit 1 }
