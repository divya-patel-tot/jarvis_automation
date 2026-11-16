# tier 0 - Visible top-level windows (read-only)
. "$PSScriptRoot\_common.ps1"
try {
    $w = @(Get-Process | Where-Object { $_.MainWindowHandle -ne 0 -and $_.MainWindowTitle } | ForEach-Object {
        @{ process = $_.ProcessName; pid = $_.Id; title = $_.MainWindowTitle } })
    Out-JarvisJson @{ ok = $true; trust = 'untrusted-titles'; windows = $w }
} catch { Out-JarvisJson @{ ok = $false; error = $_.Exception.Message }; exit 1 }
