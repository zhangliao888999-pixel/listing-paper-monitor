# Windows VPS runner for live_runner.py, invoked by Windows Task Scheduler every
# few minutes. Mirrors the proven pattern from run_screener_local.ps1 /
# run_botscalp_local.ps1 on the home PC (lock file, no `2>` redirect on git under
# Stop preference - see those files for why), simplified since this package is
# meant to be dropped into any folder standalone (no git repo required) -
# live_runner.py fetches candidate data over HTTPS itself now, so there's nothing
# to `git pull` here.
#
# Each scheduled-task run is a fresh process, so it does NOT inherit env vars set
# in some other PowerShell session - this script must dot-source set_env.ps1 itself
# every single run, or WALLET_PRIVATE_KEY / LIVE_TRADING / CONFIRM_LIVE_BOTSCALP
# will not be set and live_runner.py will refuse to trade (safe failure mode, but
# also means "it's just not doing anything" if you forget this step).
$ErrorActionPreference = "Stop"
$ScriptDir = $PSScriptRoot
Set-Location $ScriptDir

$lockFile = Join-Path $ScriptDir "live_vps.lock"
if (Test-Path $lockFile) {
    $age = (Get-Date) - (Get-Item $lockFile).LastWriteTime
    if ($age.TotalMinutes -lt 10) {
        exit 0
    }
}
Set-Content -Path $lockFile -Value (Get-Date).ToString() -Encoding utf8

try {
    # Load wallet/trading-mode env vars for THIS run. set_env.ps1 is gitignored -
    # copy set_env.example.ps1 to set_env.ps1 and fill in your own key first.
    if (Test-Path (Join-Path $ScriptDir "set_env.ps1")) {
        . (Join-Path $ScriptDir "set_env.ps1")
    }

    $env:PYTHONIOENCODING = "utf-8"
    python live_runner.py
} finally {
    Remove-Item -Path $lockFile -Force -ErrorAction SilentlyContinue
}
