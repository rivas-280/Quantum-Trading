<#
  ETF Signal Scanner - remove the Windows scheduled task.

      powershell -ExecutionPolicy Bypass -File .\windows\uninstall.ps1      (Administrator)

  Stops the scanner, posts "ETF scanner stopped" to Slack and removes the task.
  The code, the Python environment, the logs and the saved webhook URL stay in
  the folder (delete the folder to remove everything).
#>
#Requires -RunAsAdministrator
$ErrorActionPreference = "Stop"
$Repo     = Split-Path -Parent $PSScriptRoot
$Skill    = Join-Path $Repo ".claude\skills\etf-signal-scanner"
$Py       = Join-Path $Skill ".venv\Scripts\python.exe"
$Script   = Join-Path $Skill "scripts\scan.py"
$TaskName = "ETF Signal Scanner"

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed the scheduled task '$TaskName'."
} else {
    Write-Host "No scheduled task named '$TaskName' was found."
}
# Stop-ScheduledTask only kills the cmd.exe wrapper; python.exe keeps scanning
# (and posting) on its own, so kill every scanner process too.
$stray = @(Get-CimInstance Win32_Process | Where-Object {
    $_.ProcessId -ne $PID -and $_.CommandLine -and $_.CommandLine -like "*$Script*" })
if ($stray.Count) {
    $stray | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Write-Host "Stopped $($stray.Count) scanner process(es)."
}
foreach ($dir in @([Environment]::GetFolderPath("Desktop"), [Environment]::GetFolderPath("Programs"))) {
    $lnk = Join-Path $dir "ETF Scanner.lnk"
    if (Test-Path $lnk) { Remove-Item $lnk -Force; Write-Host "Removed shortcut $lnk" }
}
# The scanner was killed rather than closed, so post its stop message for it.
if (Test-Path $Py) { & $Py $Script --announce stop }
