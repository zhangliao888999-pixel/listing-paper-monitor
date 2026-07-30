# -*- coding: utf-8 -*-
# 2026-07-30新增: 用户要求在VPS上开一个能实时看的窗口,自己就能第一时间发现
# "卡住了"，不用每次都来问我。这里盯的都是之前踩过的坑对应的信号:
#   - 3个计划任务是不是都在Running(不是的话supervisor没在跑)
#   - python进程数量(0个说明脚本层面挂了)
#   - git本地有多少个提交还没推上去(这个数字持续变大就是我们之前修的那个
#     "推送卡住"的bug,是最值得盯的信号)
#   - 最近一次成功push是多久之前
#   - journal.jsonl最新一笔交易是什么时候(纯参考,市场安静时本来就会很久
#     没有新交易,不代表卡住)
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$RepoRoot = "C:\claude_watchbot\listing-paper-monitor"
$TaskNames = @("watchbot_pregrad_scanner_loop.py", "watchbot_mcap_scanner_loop.py", "watchbot_lifecycle_runner_loop.py", "watchbot_git_push_flusher.py")

function Get-MinutesAgo($epochSeconds) {
    $then = [DateTimeOffset]::FromUnixTimeSeconds([long]$epochSeconds).LocalDateTime
    return [math]::Round(((Get-Date) - $then).TotalMinutes, 1)
}

while ($true) {
    Clear-Host
    Write-Host ("=== watch_bot 实时状态监控  刷新时间: " + (Get-Date -Format "yyyy-MM-dd HH:mm:ss")) -ForegroundColor Cyan
    Write-Host ""

    # 1. 计划任务状态
    Write-Host "--- 计划任务 ---"
    $anyDown = $false
    foreach ($name in $TaskNames) {
        try {
            $task = Get-ScheduledTask -TaskName $name -ErrorAction Stop
            $color = if ($task.State -eq "Running") { "Green" } else { "Red"; $anyDown = $true }
            Write-Host ("  {0,-38} {1}" -f $name, $task.State) -ForegroundColor $color
        } catch {
            Write-Host ("  {0,-38} 查询失败" -f $name) -ForegroundColor Red
            $anyDown = $true
        }
    }

    # 2. python进程
    Write-Host ""
    Write-Host "--- python进程 ---"
    $procs = Get-Process python -ErrorAction SilentlyContinue
    $procCount = if ($procs) { $procs.Count } else { 0 }
    $procColor = if ($procCount -gt 0) { "Green" } else { "Red" }
    Write-Host ("  当前存活: {0} 个" -f $procCount) -ForegroundColor $procColor

    # 3. git推送积压
    Write-Host ""
    Write-Host "--- git推送状态 ---"
    Push-Location $RepoRoot
    try {
        git fetch origin master --quiet 2>$null
        $behind = (git rev-list --count "origin/master..HEAD" 2>$null)
        if (-not $behind) { $behind = 0 }
        $backlogColor = if ([int]$behind -ge 10) { "Red" } elseif ([int]$behind -ge 3) { "Yellow" } else { "Green" }
        Write-Host ("  本地未推送提交数: {0}" -f $behind) -ForegroundColor $backlogColor
        if ([int]$behind -ge 10) {
            Write-Host "  *** 积压超过10个提交,大概率是推送卡住了,需要人工看一下 ***" -ForegroundColor Red
        }
        $lastPushMsg = (git log origin/master -1 --format="%s (%cr)" 2>$null)
        Write-Host ("  origin/master最新一笔: {0}" -f $lastPushMsg)
    } finally {
        Pop-Location
    }

    # 4. 最新交易记录
    Write-Host ""
    Write-Host "--- 最近一笔纸盘交易 ---"
    $journalPath = Join-Path $RepoRoot "watch_bot\journal.jsonl"
    if (Test-Path $journalPath) {
        $lastLine = Get-Content $journalPath -Tail 1
        try {
            $rec = $lastLine | ConvertFrom-Json
            $agoMin = Get-MinutesAgo $rec.written_at
            $pnlStr = if ($null -ne $rec.pnl_pct) { "{0:+0.0;-0.0}%" -f $rec.pnl_pct } else { "未知" }
            Write-Host ("  {0}  退出原因={1}  pnl={2}  ({3}分钟前)" -f $rec.name, $rec.exit_reason, $pnlStr, $agoMin)
        } catch {
            Write-Host "  (最后一行解析失败,可能刚好在写入中)" -ForegroundColor Yellow
        }
    } else {
        Write-Host "  journal.jsonl不存在" -ForegroundColor Red
    }

    Write-Host ""
    if ($anyDown) {
        Write-Host "*** 警告: 有计划任务不是Running状态,请检查 ***" -ForegroundColor Red -BackgroundColor Black
    }
    Write-Host "(每10秒自动刷新一次, 关掉这个窗口不影响后台脚本继续跑)" -ForegroundColor DarkGray

    Start-Sleep -Seconds 10
}
