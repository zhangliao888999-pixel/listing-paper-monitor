Write-Host (Get-Date -Format o)
Write-Host "---cmdlines---"
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Select-Object ProcessId, CommandLine |
    Sort-Object ProcessId |
    Format-Table -AutoSize -Wrap
