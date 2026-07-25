# 本地 screener 高频扫描：Windows 计划任务每几分钟调用一次。
# 只提交/推送本实例专属的 _local 文件，与云端实例(screener_state.json等,无后缀)
# 完全隔离，避免同时写同一个JSON文件产生git冲突。
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

git pull --rebase 2>&1 | Out-Null

$env:SCREENER_LOCAL = "1"
$env:PYTHONIOENCODING = "utf-8"
python screener.py

git add screener_state_local.json screener_candidates_local.json screener_local.log 2>&1 | Out-Null
$ts = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mmZ')
git commit -m "screener local cycle $ts" 2>&1 | Out-Null

$pushed = $false
for ($i = 0; $i -lt 3; $i++) {
    git push 2>&1 | Out-Null
    if ($?) { $pushed = $true; break }
    git pull --rebase 2>&1 | Out-Null
    Start-Sleep -Seconds 3
}
