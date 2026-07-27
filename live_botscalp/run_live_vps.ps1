# Windows VPS runner for live_runner.py, invoked by Windows Task Scheduler every
# few minutes. Mirrors the proven pattern from run_screener_local.ps1 /
# run_botscalp_local.ps1 on the home PC (lock file, no `2>` redirect on git under
# Stop preference - see those files for why).
#
# Each scheduled-task run is a fresh process, so it does NOT inherit env vars set
# in some other PowerShell session - this script must dot-source set_env.ps1 itself
# every single run, or WALLET_PRIVATE_KEY / LIVE_TRADING / CONFIRM_LIVE_BOTSCALP
# will not be set and live_runner.py will refuse to trade (safe failure mode, but
# also means "it's just not doing anything" if you forget this step).
$ErrorActionPreference = "Stop"
$ScriptDir = $PSScriptRoot
$RepoDir = Split-Path -Parent $ScriptDir   # live_botscalp's parent = the git repo root (paper/)
Set-Location $ScriptDir

$lockFile = Join-Path $ScriptDir "live_vps.lock"
if (Test-Path $lockFile) {
    $age = (Get-Date) - (Get-Item $lockFile).LastWriteTime
    if ($age.TotalMinutes -lt 10) {
        exit 0
    }
}
Set-Content -Path $lockFile -Value (Get-Date).ToString() -Encoding utf8

$prevEap = $ErrorActionPreference
try {
    $ErrorActionPreference = "Continue"

    # Pull the latest screener candidate data (both the cloud workflow and the home
    # PC's local task push to the same repo, so a pull here picks up both sources).
    Set-Location $RepoDir
    git pull --rebase | Out-Null
    Set-Location $ScriptDir

    # Load wallet/trading-mode env vars for THIS run. set_env.ps1 is gitignored -
    # copy set_env.example.ps1 to set_env.ps1 and fill in your own key first.
    if (Test-Path (Join-Path $ScriptDir "set_env.ps1")) {
        . (Join-Path $ScriptDir "set_env.ps1")
    }

    $env:PYTHONIOENCODING = "utf-8"
    python live_runner.py

    # Deliberately NOT pushing live_state.json/live_orders.jsonl/live_runner.log back
    # to git by default - this repo is public, and committing real tx signatures makes
    # it trivially easy to look up your wallet on Solscan and aggregate its whole
    # history in one place (the underlying data is already public on-chain either way,
    # this is about not making it extra convenient to correlate). Uncomment below if
    # you want this strategy's live status to show up on the same dashboard as the
    # paper strategies, or push these files to a private repo instead.
    # Set-Location $RepoDir
    # git add live_botscalp/live_state.json live_botscalp/live_orders.jsonl live_botscalp/live_runner.log | Out-Null
    # $ts = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mmZ')
    # git commit -m "live botscalp cycle $ts" | Out-Null
    # for ($i = 0; $i -lt 3; $i++) {
    #     git push | Out-Null
    #     if ($LASTEXITCODE -eq 0) { break }
    #     git pull --rebase | Out-Null
    #     Start-Sleep -Seconds 3
    # }
} finally {
    $ErrorActionPreference = $prevEap
    Remove-Item -Path $lockFile -Force -ErrorAction SilentlyContinue
}
