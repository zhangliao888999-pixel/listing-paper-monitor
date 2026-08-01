# ASCII-only on purpose: this file may be scp'd, and scp drops the UTF-8 BOM.
# Supervisor for the launch-scout module. Restarts on crash.
$ErrorActionPreference = "Continue"
$dir = "C:\claude_watchbot\listing-paper-monitor\survivor_research"
Set-Location $dir
$log = Join-Path $dir "lab_scout.log"
$sup = Join-Path $dir "lab_scout_supervisor.log"
while ($true) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $sup -Value "$ts starting lab_scout.py" -Encoding utf8
    $p = Start-Process -FilePath "python" -ArgumentList "-X","utf8","lab_scout.py" `
        -WorkingDirectory $dir -NoNewWindow -PassThru `
        -RedirectStandardOutput $log -RedirectStandardError "$log.err"
    $p.WaitForExit()
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $sup -Value "$ts exited code=$($p.ExitCode), restart in 30s" -Encoding utf8
    Start-Sleep -Seconds 30
}
