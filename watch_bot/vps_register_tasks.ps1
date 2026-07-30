# -*- coding: utf-8 -*-
# 2026-07-30新增: 把几个循环脚本注册成Windows计划任务——直接用SSH远程会话启动的
# 后台进程会被Windows的job object机制在会话结束时一起杀掉(Start-Process
# -WindowStyle Hidden也不例外),计划任务运行在完全独立的会话之外,不受这个限制,
# 顺便实现开机自启动。
# 2026-07-30再补: 加了git_push_flusher.py——VPS+本机screener_local+云端三方
# 并发push同一仓库,某一方6次重试全部撞车失败时会把提交晾在本地,这个脚本
# 每45秒独立检查一次积压、逮到窗口就补推,不用等下一笔交易凑巧触发重试。
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path

foreach ($name in @("pregrad_scanner_loop.py", "mcap_scanner_loop.py", "lifecycle_runner_loop.py", "git_push_flusher.py")) {
    $taskName = "watchbot_$name"
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

    $argStr = "-NoProfile -ExecutionPolicy Bypass -File `"$dir\vps_run_forever.ps1`" -ScriptName $name"
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argStr -WorkingDirectory $dir
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Days 0) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)

    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskName $taskName
    Write-Host "已注册并启动: $taskName"
}
