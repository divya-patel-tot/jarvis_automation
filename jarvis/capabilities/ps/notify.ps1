# tier 1 - Windows notification balloon (returns immediately; a detached helper shows it).
param([string]$Title = 'Jarvis', [Parameter(Mandatory=$true)][string]$Message)
. "$PSScriptRoot\_common.ps1"
try {
    $t = $Title.Substring(0, [math]::Min(60, $Title.Length)).Replace("'", "''")
    $m = $Message.Substring(0, [math]::Min(240, $Message.Length)).Replace("'", "''")
    $code = "Add-Type -AssemblyName System.Windows.Forms,System.Drawing; `$n=New-Object System.Windows.Forms.NotifyIcon; " +
            "`$n.Icon=[System.Drawing.SystemIcons]::Information; `$n.Visible=`$true; " +
            "`$n.ShowBalloonTip(6000,'$t','$m','Info'); Start-Sleep -Seconds 7; `$n.Dispose()"
    Start-Process powershell -WindowStyle Hidden -ArgumentList '-NoProfile', '-Command', $code
    Out-JarvisJson @{ ok = $true }
} catch { Out-JarvisJson @{ ok = $false; error = $_.Exception.Message }; exit 1 }
