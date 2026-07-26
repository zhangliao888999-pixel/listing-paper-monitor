# Local screener high-frequency scan, invoked by Windows Task Scheduler every few minutes.
# Only commits/pushes this instance's own _local files, kept separate from the cloud
# instance's files (screener_state.json etc, no suffix) to avoid git conflicts.
$ErrorActionPreference = "Stop"
$ScriptDir = "C:\Users\zhang\OneDrive\Desktop\claude_code_ohanism\listing_research\paper"
Set-Location $ScriptDir

# Lock file: as the tracked-pool count grows, each cycle takes longer, and a previous
# cycle can still be running when the next scheduled trigger fires - observed in
# practice, causing two processes to race on the same local files and git commits.
# A lock older than 15 minutes is treated as an abandoned/crashed run and overridden.
$lockFile = Join-Path $ScriptDir "screener_local.lock"
if (Test-Path $lockFile) {
    $age = (Get-Date) - (Get-Item $lockFile).LastWriteTime
    if ($age.TotalMinutes -lt 15) {
        exit 0
    }
}
Set-Content -Path $lockFile -Value (Get-Date).ToString() -Encoding utf8

try {
    git pull --rebase 2>&1 | Out-Null

    $env:SCREENER_LOCAL = "1"
    $env:PYTHONIOENCODING = "utf-8"
    python screener.py

    git add screener_state_local.json screener_candidates_local.json screener_local.log screener_enrich_cache_local.json 2>&1 | Out-Null
    $ts = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mmZ')
    git commit -m "screener local cycle $ts" 2>&1 | Out-Null

    for ($i = 0; $i -lt 3; $i++) {
        git push 2>&1 | Out-Null
        if ($?) { break }
        git pull --rebase 2>&1 | Out-Null
        Start-Sleep -Seconds 3
    }
} finally {
    Remove-Item -Path $lockFile -Force -ErrorAction SilentlyContinue
}
