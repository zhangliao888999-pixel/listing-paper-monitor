# Report backtest progress + ETA based on elapsed wall time vs combos done.
$log = "C:\claude_watchbot\listing-paper-monitor\survivor_research\backtest_run.log"
$lines = Get-Content $log -ErrorAction SilentlyContinue
$done = 0
foreach ($l in $lines) {
    if ($l -match '(\d+)/4374') { $done = [int]$Matches[1] }
}
$proc = Get-Process python -ErrorAction SilentlyContinue | Sort-Object StartTime | Select-Object -Last 5 | Select-Object -First 1
if ($proc) {
    $elapsed = ((Get-Date) - $proc.StartTime).TotalMinutes
    "combos_done=$done / 4374"
    "elapsed_min=" + [math]::Round($elapsed, 1)
    if ($done -gt 0) {
        $rate = $done / $elapsed
        $remain = (4374 - $done) / $rate
        "rate_per_min=" + [math]::Round($rate, 1)
        "eta_min=" + [math]::Round($remain, 0)
    } else {
        "eta=unknown (no checkpoint yet)"
    }
} else {
    "no python process running"
}
"results_csv_exists=" + (Test-Path "C:\claude_watchbot\listing-paper-monitor\survivor_research\results.csv")
