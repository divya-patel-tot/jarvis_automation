# tier 0 - System information (read-only)
. "$PSScriptRoot\_common.ps1"
try {
    $os = Get-CimInstance Win32_OperatingSystem
    $cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
    $load = (Get-CimInstance Win32_Processor | Measure-Object LoadPercentage -Average).Average
    $bat = Get-CimInstance Win32_Battery -ErrorAction SilentlyContinue | Select-Object -First 1
    $disks = @(Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | ForEach-Object {
        @{ drive = $_.DeviceID; free_gb = [math]::Round($_.FreeSpace / 1GB, 1); size_gb = [math]::Round($_.Size / 1GB, 1) } })
    $batPct = $null; $onAc = $true
    if ($bat) { $batPct = $bat.EstimatedChargeRemaining; $onAc = ($bat.BatteryStatus -eq 2) }
    Out-JarvisJson @{
        ok = $true; os = $os.Caption; version = $os.Version; computer = $env:COMPUTERNAME
        cpu = $cpu.Name; cpu_load_pct = $load
        ram_total_gb = [math]::Round($os.TotalVisibleMemorySize / 1MB, 1)
        ram_free_gb = [math]::Round($os.FreePhysicalMemory / 1MB, 1)
        uptime_hours = [math]::Round(((Get-Date) - $os.LastBootUpTime).TotalHours, 1)
        disks = $disks; battery_pct = $batPct; on_ac = $onAc
    }
} catch { Out-JarvisJson @{ ok = $false; error = $_.Exception.Message }; exit 1 }
