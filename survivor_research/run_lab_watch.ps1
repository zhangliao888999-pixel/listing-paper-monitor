# ASCII-only on purpose: this file is scp'd, and scp drops the UTF-8 BOM that
# Windows PowerShell needs, which corrupts non-ASCII text. Files needing
# Chinese go through git instead.
#
# Supervisor for the operator lab watcher. Restarts the watcher if it dies so
# an overnight run survives transient RPC/network failures.
$ErrorActionPreference = "Continue"
$dir = "C:\claude_watchbot\listing-paper-monitor\survivor_research"
Set-Location $dir
$log = Join-Path $dir "lab_watch.log"
$sup = Join-Path $dir "lab_watch_supervisor.log"

while ($true) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $sup -Value "$ts starting lab_watch.py" -Encoding utf8
    # Start-Process redirection writes the child's raw bytes. PowerShell's own
    # `*>>` re-encodes through the console codepage instead, which mangled the
    # watcher's UTF-8 Chinese output into unreadable logs.
    $p = Start-Process -FilePath "python" -ArgumentList "lab_watch.py" `
        -WorkingDirectory $dir -NoNewWindow -PassThru `
        -RedirectStandardOutput $log -RedirectStandardError "$log.err"
    $p.WaitForExit()
    $code = $p.ExitCode
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $sup -Value "$ts lab_watch.py exited code=$code, restart in 30s" -Encoding utf8
    Start-Sleep -Seconds 30
}
