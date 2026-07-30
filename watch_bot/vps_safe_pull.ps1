# -*- coding: utf-8 -*-
# 2026-07-31新增: VPS上pull前清理未提交改动的标准做法。
#
# 起因: 部署修复时习惯性地`git checkout -- <一堆文件>`清理阻塞pull的运行时
# 噪音(日志/缓存/state文件),有一次连journal.jsonl一起checkout了,把刚写入
# 还没commit的第一笔真实成交记录直接丢掉(交易本身已上链,只是账本行没了,
# 后来靠链上数据重建)。
#
# 这个脚本的规矩: journal.jsonl是append-only的真钱成交账本,永远先commit,
# 绝不checkout丢弃;其余运行时噪音文件才能直接丢。
$repo = "C:\claude_watchbot\listing-paper-monitor"
Set-Location $repo

# 1. 账本有改动就先落袋为安,单独commit
$journalDirty = git status --porcelain watch_bot/journal.jsonl
if ($journalDirty) {
    Write-Host "journal.jsonl有未提交改动,先commit保护(绝不丢弃真实成交记录)"
    git add watch_bot/journal.jsonl
    git commit -m "trade data autosave before pull $(Get-Date -Format 'yyyy-MM-ddTHH:mmZ')" | Out-Null
}

# 2. 其余噪音文件才可以直接丢(日志/缓存/state,重跑就会重新生成)
git checkout -- __pycache__ 2>$null
git status --porcelain | Where-Object { $_ -match '^ M ' } | ForEach-Object {
    $f = $_.Substring(3).Trim()
    if ($f -notlike '*journal.jsonl*') { git checkout -- $f 2>$null }
}

# 3. 未跟踪的日志文件挡pull的话删掉(它们本来就是每轮重新生成的)
git clean -fd watch_bot --dry-run | Out-Null
git clean -fd -- 'watch_bot/*_pregrad_scalp.log' 'watch_bot/*_crash_watch.log' 'watch_bot/*_snipe_exit.log' 'watch_bot/*_insider_sell_watch.log' 2>$null | Out-Null

# 4. 用钉死合并策略的pull(不依赖机器本地git配置)
git -c pull.rebase=false pull --no-edit origin master 2>&1 | Select-Object -Last 3
git log -1 --oneline
