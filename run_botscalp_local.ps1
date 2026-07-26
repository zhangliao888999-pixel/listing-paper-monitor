# Local runner for Strategy D (bot-scalp), invoked by Windows Task Scheduler every few
# minutes. Cloud workflow (monitor_botscalp.yml) was disabled - GH Actions free-tier
# scheduling delay meant it ran zero times in its first ~20 minutes live, useless for a
# strategy whose whole premise is fast in/out (30min max hold, 5%/3% TP/SL). Only a
# reliable, frequent local runner can actually exercise this strategy as designed.
$ErrorActionPreference = "Stop"
$ScriptDir = "C:\Users\zhang\OneDrive\Desktop\claude_code_ohanism\listing_research\paper"
Set-Location $ScriptDir

$lockFile = Join-Path $ScriptDir "botscalp_local.lock"
if (Test-Path $lockFile) {
    $age = (Get-Date) - (Get-Item $lockFile).LastWriteTime
    if ($age.TotalMinutes -lt 10) {
        exit 0
    }
}
Set-Content -Path $lockFile -Value (Get-Date).ToString() -Encoding utf8

$prevEap = $ErrorActionPreference
try {
    # See run_screener_local.ps1 for why git calls here never use any `2>` redirect.
    $ErrorActionPreference = "Continue"

    git pull --rebase | Out-Null

    $env:PYTHONIOENCODING = "utf-8"
    python bot_scalp_monitor.py

    git add state_botscalp.json nav_botscalp.csv botscalp.log DASHBOARD_BOTSCALP.md | Out-Null
    $ts = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mmZ')
    git commit -m "botscalp local cycle $ts" | Out-Null

    for ($i = 0; $i -lt 3; $i++) {
        git push | Out-Null
        if ($LASTEXITCODE -eq 0) { break }
        git pull --rebase | Out-Null
        Start-Sleep -Seconds 3
    }
} finally {
    $ErrorActionPreference = $prevEap
    Remove-Item -Path $lockFile -Force -ErrorAction SilentlyContinue
}
