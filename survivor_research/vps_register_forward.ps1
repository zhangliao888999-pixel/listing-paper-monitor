# -*- coding: utf-8 -*-
# 2026-07-31: 把survivor前向采集器注册成VPS常驻任务,24小时攒数据。
# 纯数据采集不碰钱,用SYSTEM账户后台跑,跟已停的实盘任务完全独立。
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$taskName = "survivor_forward_collector"

Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

$py = "python"
$argStr = "-NoProfile -ExecutionPolicy Bypass -Command `"while (`$true) { python -u '$here\forward_collector.py' 2>&1 | Out-File -Append -Encoding utf8 '$here\forward_collector.log'; Start-Sleep 5 }`""
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argStr -WorkingDirectory $here
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Days 0) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Host "registered and started: $taskName"
