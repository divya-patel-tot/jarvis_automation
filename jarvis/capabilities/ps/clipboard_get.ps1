# tier 0 - Clipboard text (read-only). The content is UNTRUSTED data, never instructions.
. "$PSScriptRoot\_common.ps1"
try {
    $t = Get-Clipboard -Raw -ErrorAction SilentlyContinue
    if ($null -eq $t) { $t = '' }
    Out-JarvisJson @{ ok = $true; trust = 'untrusted'; chars = $t.Length; text = $t.Substring(0, [math]::Min($t.Length, 20000)) }
} catch { Out-JarvisJson @{ ok = $false; error = $_.Exception.Message }; exit 1 }
