# -*- coding: utf-8 -*-
# 2026-07-30新增: 用户要求在VPS上开一个能实时看的窗口,自己就能第一时间发现
# "卡住了"，不用每次都来问我。这里盯的都是之前踩过的坑对应的信号:
#   - 计划任务是不是都在Running(不是的话supervisor没在跑)
#   - python进程数量(0个说明脚本层面挂了)
#   - git本地有多少个提交还没推上去(这个数字持续变大就是我们之前修的那个
#     "推送卡住"的bug,是最值得盯的信号)
#   - 最近一次成功push是多久之前
#   - journal.jsonl最新一笔交易是什么时候(纯参考,市场安静时本来就会很久
#     没有新交易,不代表卡住)
#
# 2026-07-30再补: 纸盘三个发现循环(pregrad/mcap/lifecycle-纸盘)已经主动停掉
# 转去跑实盘,故意是Ready状态,不该再当成"任务挂了"报红——只监控当前实际
# 该跑的任务。新增实盘专属状态: 私钥文件在不在、当前是否占着那唯一一个
# 真实仓位名额、最近一笔真实成交是什么结果。
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$RepoRoot = "C:\claude_watchbot\listing-paper-monitor"
$TaskNames = @("watchbot_live_runner_loop", "watchbot_live_mcap_loop", "watchbot_live_pregrad_loop", "watchbot_git_push_flusher.py")

# 2026-07-30再补: "正在交易"面板要记住"上一次展示的已卖出记录是哪一笔、什么
# 时候开始展示的"——这两个变量在while循环外面声明,循环体每轮刷新都能读到
# 上一轮的值,不会被每轮清零,这样才能实现"卖出后保留30秒自动消失"。
$lastClosedKey = $null
$lastClosedShownAt = $null

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

    # 5. 实盘状态
    Write-Host ""
    Write-Host "--- 实盘状态 ---"
    $keyFile = Join-Path $RepoRoot "watch_bot\.live_wallet_key"
    if (Test-Path $keyFile) {
        Write-Host "  私钥文件: 已就位" -ForegroundColor Green
    } else {
        Write-Host "  私钥文件: 不存在(实盘只监控不下单)" -ForegroundColor Yellow
    }
    # 2026-07-30修复: 之前用pump_lifecycle.json的live_deployed+status字段判断,
    # 发现这两个字段跟"仓位到底平没平"没关系(live_deployed一旦True永远不清零,
    # status是池子自己死没死,不是我们交易平没平),会一直显示"占用中"就算早
    # 就平仓了。改成看snipe_exit.py自己维护的标记文件(建仓时创建,平仓时删除)。
    $posMarker = Join-Path $RepoRoot "watch_bot\.live_position_open"
    if (Test-Path $posMarker) {
        $markerAgeMin = ((Get-Date) - (Get-Item $posMarker).LastWriteTime).TotalMinutes
        if ($markerAgeMin -gt 50) {
            Write-Host ("  当前占用的真实仓位名额: 0 (标记文件存在但已{0:N0}分钟,大概率是上个进程崩溃没清理)" -f $markerAgeMin) -ForegroundColor Yellow
        } else {
            Write-Host ("  当前占用的真实仓位名额: 1 (已建仓{0:N0}分钟)" -f $markerAgeMin) -ForegroundColor Cyan
        }
    } else {
        Write-Host "  当前占用的真实仓位名额: 0"
    }
    if (Test-Path $journalPath) {
        $liveLines = Get-Content $journalPath | Select-Object -Last 200 | ForEach-Object {
            try { $r = $_ | ConvertFrom-Json; if ($r.dry_run -eq $false) { $r } } catch {}
        }
        if ($liveLines) {
            $lastLive = $liveLines | Select-Object -Last 1
            $agoMin = Get-MinutesAgo $lastLive.written_at
            $pnlActualStr = if ($null -ne $lastLive.pnl_pct_actual) { "{0:+0.0;-0.0}%" -f $lastLive.pnl_pct_actual } else { "还没有真实盈亏数据" }
            Write-Host ("  最近一笔真实成交: {0}  退出原因={1}  真实pnl={2}  ({3}分钟前)" -f $lastLive.name, $lastLive.exit_reason, $pnlActualStr, $agoMin) -ForegroundColor Cyan
        } else {
            Write-Host "  还没有任何真实成交记录" -ForegroundColor DarkGray
        }
    }

    # 6. 正在交易的币: 买入时间/买入价格/现价/涨跌幅,实时拉GT现价;卖出之后
    # 这条记录保留30秒(用户明确要求),超过30秒就不再显示,回到"无持仓"。
    Write-Host ""
    Write-Host "--- 正在交易 ---"
    $showedSomething = $false
    if ((Test-Path $posMarker) -and $markerAgeMin -le 50) {
        try {
            $posInfo = Get-Content $posMarker -Raw | ConvertFrom-Json
            $openedAt = [DateTimeOffset]::FromUnixTimeSeconds([long]$posInfo.opened_at).LocalDateTime
            $entryPrice = [double]$posInfo.entry_price
            $curPrice = $null
            try {
                $resp = Invoke-RestMethod -Uri ("https://api.geckoterminal.com/api/v2/networks/solana/pools/" + $posInfo.addr) `
                    -Headers @{ "Accept" = "application/json;version=20230302" } -TimeoutSec 8
                $curPrice = [double]$resp.data.attributes.base_token_price_usd
            } catch {}
            Write-Host ("  {0}" -f $posInfo.name) -ForegroundColor Cyan
            Write-Host ("  买入时间: {0}   买入价格: `${1:N10}" -f $openedAt.ToString("HH:mm:ss"), $entryPrice)
            if ($curPrice -and $entryPrice) {
                $pctChange = ($curPrice / $entryPrice - 1) * 100
                $chgColor = if ($pctChange -ge 0) { "Green" } else { "Red" }
                Write-Host ("  现价: `${0:N10}   涨跌幅: {1:+0.00;-0.00}%" -f $curPrice, $pctChange) -ForegroundColor $chgColor
            } else {
                Write-Host "  现价: 拉取失败,下一轮重试" -ForegroundColor Yellow
            }
            $showedSomething = $true
            $lastClosedKey = $null
        } catch {
            Write-Host "  (标记文件解析失败,可能刚好在写入中)" -ForegroundColor Yellow
            $showedSomething = $true
        }
    }
    if (-not $showedSomething -and (Test-Path $journalPath)) {
        try {
            $rec = (Get-Content $journalPath -Tail 1) | ConvertFrom-Json
            if ($rec.dry_run -eq $false) {
                $recKey = "$($rec.mint)_$($rec.written_at)"
                if ($recKey -ne $lastClosedKey) {
                    $lastClosedKey = $recKey
                    $lastClosedShownAt = Get-Date
                }
                $elapsed = ((Get-Date) - $lastClosedShownAt).TotalSeconds
                if ($elapsed -le 30) {
                    $profitUsd = $null
                    if ($null -ne $rec.entry_usd_actual -and $null -ne $rec.pnl_pct_actual) {
                        $profitUsd = [double]$rec.entry_usd_actual * [double]$rec.pnl_pct_actual / 100
                    }
                    Write-Host ("  {0}  已卖出({1:N0}秒前)" -f $rec.name, $elapsed) -ForegroundColor Cyan
                    Write-Host ("  卖出价格: `${0:N10}" -f $rec.exit_price)
                    if ($null -ne $profitUsd) {
                        $sign = if ($profitUsd -ge 0) { "+" } else { "-" }
                        $profitColor = if ($profitUsd -ge 0) { "Green" } else { "Red" }
                        Write-Host ("  利润金额: {0}`${1:N2}" -f $sign, [Math]::Abs($profitUsd)) -ForegroundColor $profitColor
                    } else {
                        Write-Host "  利润金额: 还没有真实盈亏数据" -ForegroundColor Yellow
                    }
                    $showedSomething = $true
                }
            }
        } catch {}
    }
    if (-not $showedSomething) {
        Write-Host "  (当前无持仓)" -ForegroundColor DarkGray
    }

    Write-Host ""
    if ($anyDown) {
        Write-Host "*** 警告: 有计划任务不是Running状态,请检查 ***" -ForegroundColor Red -BackgroundColor Black
    }
    Write-Host "(每10秒自动刷新一次, 关掉这个窗口不影响后台脚本继续跑)" -ForegroundColor DarkGray

    Start-Sleep -Seconds 10
}
