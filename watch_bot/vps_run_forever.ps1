# -*- coding: utf-8 -*-
# 2026-07-30新增: VPS部署专用的外层看守脚本——GitHub Actions那边云端job最长
# 只能跑6小时,得靠cron每5小时接力重启;VPS没有这个限制,可以真正一直跑,但
# 万一某个循环脚本自己崩了(未捕获异常/服务器重启),需要有人负责把它重新拉起来,
# 不然就会像GH Actions那样"进程死了但没人知道"。这个脚本包一层while循环,
# 子进程退出了就等5秒重新拉起,并把重启这件事写进日志方便事后排查。
param(
    [Parameter(Mandatory=$true)][string]$ScriptName,
    [string]$LoopRounds = "99999",
    # 2026-07-30新增: 纸盘大框架跑通了,用户要求拿真钱小额测试——同一个
    # lifecycle_runner_loop.py,加-LiveMode就切换成真实下单模式,不用另外
    # 写一份脚本。日志文件名加live前缀,避免跟纸盘那份任务的日志互相覆盖。
    [switch]$LiveMode
)

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here
$logPrefix = if ($LiveMode) { "live_$ScriptName" } else { $ScriptName }
$logFile = Join-Path $here "vps_supervisor_$logPrefix.log"

function Log($msg) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "[$ts] $msg" | Out-File -Append -Encoding utf8 $logFile
}

Log "=== 看守启动: $ScriptName $(if ($LiveMode) {'[实盘模式]'} else {''}) ==="
$env:LOOP_ROUNDS = $LoopRounds
# 2026-07-30新增: 云端和VPS推的是同一份journal.jsonl,打个来源标记才能对比。
$env:DEPLOY_ENV = "vps"

if ($LiveMode) {
    $env:SNIPE_LIVE_MODE = "1"
    if (-not $env:MAX_CONCURRENT_LIVE) { $env:MAX_CONCURRENT_LIVE = "1" }
    if (-not $env:LIVE_POS_SIZE_USD) { $env:LIVE_POS_SIZE_USD = "5" }
    # 2026-07-30新增: 实盘首跑10分钟无开仓的教训——纸盘时代lifecycle这条腿10分钟
    # 一轮无所谓(发现主力是pregrad/mcap高频腿),实盘模式下它是主要发现入口之一,
    # 10分钟一轮会让大部分年龄窗口内的候选直接过期。实盘把它提到3分钟一轮
    # (纸盘的高频腿已停,GT/GMGN调用量整体反而比以前低,不会触发限流)。
    $env:LIFECYCLE_INTERVAL_SEC = "180"
    $keyFile = Join-Path $here ".live_wallet_key"
    if (Test-Path $keyFile) {
        $env:WALLET_PRIVATE_KEY = (Get-Content $keyFile -Raw).Trim()
        Log "实盘模式: 已从.live_wallet_key读取私钥, MAX_CONCURRENT_LIVE=$($env:MAX_CONCURRENT_LIVE) LIVE_POS_SIZE_USD=$($env:LIVE_POS_SIZE_USD)"
    } else {
        Log "*** 实盘模式但找不到 $keyFile ,不会真的下单(lifecycle_logger.py会自己拒绝、只跑监控不跑snipe_exit) ***"
    }
}

# 2026-07-30新增: 用户提出VPS没有GitHub Actions那种共享IP限流顾虑,想试试把
# 并发调高(加倍)看扫描效率有没有提升,跟云端(默认值)直接对比。只在VPS这边
# 通过环境变量覆盖,不改代码默认值,云端继续用原来保守的3/4。
if ($ScriptName -eq "pregrad_scanner_loop.py") {
    $env:PREGRAD_MAX_CONCURRENT = "6"
    Log "并发测试: PREGRAD_MAX_CONCURRENT=6(云端默认3)"
}
if ($ScriptName -eq "mcap_scanner_loop.py") {
    $env:MCAP_MAX_CONCURRENT_DEPLOYED = "8"
    Log "并发测试: MCAP_MAX_CONCURRENT_DEPLOYED=8(云端默认4)"
}

while ($true) {
    Log "启动 $ScriptName ..."
    # 2026-07-30新增: python的stdout重定向到文件时默认是整块缓冲,不是行缓冲,
    # print()内容可能长时间攒在内存里不落盘,导致排查git_push_flusher.py这类
    # 靠print()记日志的脚本时,日志文件看着一直是0字节,分不清"没在干活"还是
    # "干了但没写出来"。加-u强制无缓冲,让print()立刻落盘。
    $proc = Start-Process -FilePath "python" -ArgumentList "-u", $ScriptName -NoNewWindow -PassThru -Wait `
        -RedirectStandardOutput "$here\vps_stdout_$logPrefix.log" `
        -RedirectStandardError "$here\vps_stderr_$logPrefix.log"
    Log "$ScriptName 退出,退出码=$($proc.ExitCode),5秒后重新拉起"
    Start-Sleep -Seconds 5
}
