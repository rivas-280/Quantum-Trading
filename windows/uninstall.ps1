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
# The scanner was killed rather than closed, so post its stop message for it.
if (Test-Path $Py) { & $Py $Script --announce stop }
