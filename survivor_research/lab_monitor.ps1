# 狗庄实验室监控窗口。在VPS上双击或右键"用PowerShell运行"即可。
# 走 git 部署(不用 scp): scp 会丢掉 UTF-8 BOM,PowerShell 就按 ANSI 解析,
# 中文全变乱码而且语法直接崩。
$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"
$dir = "C:\claude_watchbot\listing-paper-monitor\survivor_research"
Set-Location $dir
$host.UI.RawUI.WindowTitle = "狗庄实验室 - 实时监控"

while ($true) {
    Clear-Host
    try { python -X utf8 lab_monitor.py } catch { Write-Output "刷新出错: $_" }
    Start-Sleep -Seconds 15
}
