# -*- coding: utf-8 -*-
# 2026-07-30新增: 用户要求能在VPS上直接看到一个实时刷新的状态窗口,自己就能
# 第一时间发现卡住,不用每次来问我。这个任务跟另外3个跑数据的计划任务不同:
# 那3个用SYSTEM/ServiceAccount,是为了在没人登录时也能后台一直跑;这个监控
# 窗口的目的正相反,是要在Administrator实际RDP登录进来的时候弹出一个看得见
# 的窗口,所以用Interactive登录类型 + AtLogOn触发器,绑定到Administrator这个
# 交互式会话上。
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$taskName = "watchbot_status_monitor"

Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

$argStr = "-NoExit -NoProfile -ExecutionPolicy Bypass -WindowStyle Normal -File `"$dir\vps_status_monitor.ps1`""
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argStr -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "Administrator"
$principal = New-ScheduledTaskPrincipal -UserId "Administrator" -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Days 0)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Write-Host "已注册: $taskName (下次Administrator登录会自动弹出监控窗口)"

# 现在Administrator可能已经登录着(RDP会话存在),尝试立刻启动一次,能弹出来就直接弹出来
Start-ScheduledTask -TaskName $taskName
Start-Sleep -Seconds 2
Write-Host "已尝试立即启动一次,如果当前有Administrator的交互式会话,窗口应该已经弹出"
