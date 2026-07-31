# Progress + ETA for the v2 (corrected data) backtest run.
$log = "C:\claude_watchbot\listing-paper-monitor\survivor_research\backtest_v2.log"
$total = 240
$done = 0
foreach ($l in (Get-Content $log -ErrorAction SilentlyContinue)) {
    if ($l -match '(\d+)/(\d+)\s*$') { $done = [int]$Matches[1] }
}
$proc = Get-Process python -ErrorAction SilentlyContinue |
        Sort-Object StartTime -Descending | Select-Object -First 1
"log_last_write=" + (Get-Item $log -ErrorAction SilentlyContinue).LastWriteTime
"combos_done=$done / $total"
if ($proc) {
    $elapsed = ((Get-Date) - $proc.StartTime).TotalMinutes
    "elapsed_min=" + [math]::Round($elapsed,1)
    # total CPU across workers tells us real work done even before a checkpoint prints
    $cpu = (Get-Process python -ErrorAction SilentlyContinue |
            Measure-Object -Property @{Expression={$_.TotalProcessorTime.TotalMinutes}} -Sum).Sum
    "cpu_minutes_total=" + [math]::Round($cpu,1)
    if ($done -gt 0) {
        $rate = $done / $elapsed
        "rate_per_min=" + [math]::Round($rate,2)
        "eta_min=" + [math]::Round(($total - $done)/$rate, 0)
    } else {
        "note=no checkpoint yet (prints every 200 combos, total is $total)"
    }
} else { "no python running" }
