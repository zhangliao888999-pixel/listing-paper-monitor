$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
Unregister-ScheduledTask -TaskName "diag_system_ssh" -Confirm:$false -ErrorAction SilentlyContinue

$outFile = Join-Path $dir "_diag_ssh_out.txt"
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c whoami > `"$outFile`" 2>&1 & echo --- >> `"$outFile`" & set >> `"$outFile`" & echo --- >> `"$outFile`" & ssh -T git@github.com >> `"$outFile`" 2>&1 & echo --- >> `"$outFile`" & git push >> `"$outFile`" 2>&1" -WorkingDirectory (Split-Path -Parent $dir)
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName "diag_system_ssh" -Action $action -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName "diag_system_ssh"
Write-Host "started, waiting..."
Start-Sleep -Seconds 10
Write-Host "--- output ---"
Get-Content $outFile -ErrorAction SilentlyContinue
