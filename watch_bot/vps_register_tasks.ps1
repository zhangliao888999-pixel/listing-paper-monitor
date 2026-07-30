# -*- coding: utf-8 -*-
# 2026-07-30新增: 把几个循环脚本注册成Windows计划任务——直接用SSH远程会话启动的
# 后台进程会被Windows的job object机制在会话结束时一起杀掉(Start-Process
# -WindowStyle Hidden也不例外),计划任务运行在完全独立的会话之外,不受这个限制,
# 顺便实现开机自启动。
# 2026-07-30再补: 加了git_push_flusher.py——VPS+本机screener_local+云端三方
# 并发push同一仓库,某一方6次重试全部撞车失败时会把提交晾在本地,这个脚本
# 每45秒独立检查一次积压、逮到窗口就补推,不用等下一笔交易凑巧触发重试。
#
# 2026-07-30再修真正的根因: 这几个计划任务全部以SYSTEM账户运行,而部署
# 部署时配置的deploy key + SSH config(accept-new host key)只写在了
# Administrator这个用户自己的~/.ssh/下——SYSTEM有自己完全独立的profile
# (C:\Windows\system32\config\systemprofile),既没有identity file也没有
# 已知的github.com host key,导致SYSTEM下的git push要么卡在host key
# 验证提示上(没有交互终端应答,一直挂着),要么直接"Permission denied
# (publickey)"。之前"积压时有时无、时轻时重"看着像纯粹的并发运气问题,
# 实际上SYSTEM账户的SSH认证本来就没有稳定可靠过。用core.sshCommand直接
# 指定用哪把私钥、遇到未知host key自动接受,不再依赖SYSTEM自己那份
# (根本不存在的)per-user SSH配置。这行配置存在.git/config里,不会被
# git pull带来的代码更新覆盖,但如果仓库整个重新clone过要记得重新跑一次。
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $dir
Push-Location $repoRoot
git config core.sshCommand "ssh -i C:/Users/Administrator/.ssh/github_deploy_key -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
Write-Host "已设置core.sshCommand,SYSTEM账户下的git push不再依赖它自己(不存在的)SSH配置"
Pop-Location

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
