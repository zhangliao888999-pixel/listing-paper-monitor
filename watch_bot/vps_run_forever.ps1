# -*- coding: utf-8 -*-
# 2026-07-30新增: VPS部署专用的外层看守脚本——GitHub Actions那边云端job最长
# 只能跑6小时,得靠cron每5小时接力重启;VPS没有这个限制,可以真正一直跑,但
# 万一某个循环脚本自己崩了(未捕获异常/服务器重启),需要有人负责把它重新拉起来,
# 不然就会像GH Actions那样"进程死了但没人知道"。这个脚本包一层while循环,
# 子进程退出了就等5秒重新拉起,并把重启这件事写进日志方便事后排查。
param(
    [Parameter(Mandatory=$true)][string]$ScriptName,
    [string]$LoopRounds = "99999"
)

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here
$logFile = Join-Path $here "vps_supervisor_$ScriptName.log"

function Log($msg) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "[$ts] $msg" | Out-File -Append -Encoding utf8 $logFile
}

Log "=== 看守启动: $ScriptName ==="
$env:LOOP_ROUNDS = $LoopRounds

while ($true) {
    Log "启动 $ScriptName ..."
    $proc = Start-Process -FilePath "python" -ArgumentList $ScriptName -NoNewWindow -PassThru -Wait `
        -RedirectStandardOutput "$here\vps_stdout_$ScriptName.log" `
        -RedirectStandardError "$here\vps_stderr_$ScriptName.log"
    Log "$ScriptName 退出,退出码=$($proc.ExitCode),5秒后重新拉起"
    Start-Sleep -Seconds 5
}
