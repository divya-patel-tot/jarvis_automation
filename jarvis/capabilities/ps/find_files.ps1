# tier 0 - Find files (read-only). Output: {"ok":true,"count":N,"truncated":bool,"files":[{path,size,modified}]}
param([Parameter(Mandatory=$true)][string]$Path, [string]$Pattern = '*', [string[]]$Extensions = @(),
      [int]$NewerThanDays = 0, [int]$OlderThanDays = 0, [double]$MinSizeMB = 0, [int]$Limit = 200, [switch]$NoRecurse)
. "$PSScriptRoot\_common.ps1"
try {
    $root = Assert-JarvisPath $Path
    $exts = @($Extensions | ForEach-Object { $_ -split ',' } | Where-Object { $_ } |
        ForEach-Object { '.' + $_.Trim().TrimStart('.').ToLowerInvariant() })
    $now = Get-Date
    $all = @(Get-ChildItem -LiteralPath $root -File -Filter $Pattern -Recurse:(-not $NoRecurse) -ErrorAction SilentlyContinue |
        Where-Object {
            ($exts.Count -eq 0 -or $exts -contains $_.Extension.ToLowerInvariant()) -and
            ($NewerThanDays -le 0 -or $_.LastWriteTime -ge $now.AddDays(-$NewerThanDays)) -and
            ($OlderThanDays -le 0 -or $_.LastWriteTime -le $now.AddDays(-$OlderThanDays)) -and
            ($_.Length -ge $MinSizeMB * 1MB) -and
            ($_.FullName.ToLowerInvariant() -notmatch $script:SensitiveRegex)
        } | Sort-Object LastWriteTime -Descending)
    $files = @($all | Select-Object -First $Limit | ForEach-Object {
        @{ path = $_.FullName; size = $_.Length; modified = $_.LastWriteTime.ToString('yyyy-MM-dd HH:mm') } })
    Out-JarvisJson @{ ok = $true; count = $all.Count; truncated = ($all.Count -gt $Limit); files = $files }
} catch { Out-JarvisJson @{ ok = $false; error = $_.Exception.Message }; exit 1 }
