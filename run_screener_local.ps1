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

$prevEap = $ErrorActionPreference
try {
    # NOTE: do not use ANY stderr redirect (2>&1, 2>$null, etc) on git here. Under
    # $ErrorActionPreference="Stop", PowerShell 5.1 intercepts a native exe's stderr
    # through its error-record machinery as soon as ANY redirect operator touches it,
    # regardless of the target - and git prints normal progress/status to stderr on
    # every push/pull, success or not. That was turning successful operations into
    # script-aborting "failures". Switch to Continue for this block so git's exit
    # code (checked via $LASTEXITCODE) drives control flow instead.
    $ErrorActionPreference = "Continue"

    git pull --rebase | Out-Null

    $env:SCREENER_LOCAL = "1"
    $env:PYTHONIOENCODING = "utf-8"
    python screener.py

    git add screener_state_local.json screener_candidates_local.json screener_local.log screener_enrich_cache_local.json | Out-Null
    $ts = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mmZ')
    git commit -m "screener local cycle $ts" | Out-Null

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
