# One-off: run the full parameter sweep on VPS (4 workers, leaves CPU headroom
# so the forward collector on the same box keeps running and the server does not
# overheat). Local machine hit 99C with 6 workers - never run this locally again.
Set-Location "C:\claude_watchbot\listing-paper-monitor\survivor_research"
$env:BT_WORKERS = "5"
$env:BT_GRID = "coarse"
$env:OHLCV_DIR = "ohlcv3"
python -u backtest.py 2>&1 | Out-File -Encoding utf8 backtest_v2.log
Write-Host "backtest finished"
