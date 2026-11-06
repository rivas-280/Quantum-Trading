<#
  ETF Signal Scanner - is it running?

      powershell -ExecutionPolicy Bypass -File .\windows\status.ps1
#>
$Repo     = Split-Path -Parent $PSScriptRoot
$Skill    = Join-Path $Repo ".claude\skills\etf-signal-scanner"
$Log      = Join-Path $Skill "logs\scanner.log"
$Status   = Join-Path $Skill "state\status.json"
$TaskName = "ETF Signal Scanner"

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) { Write-Host "Not installed (no scheduled task '$TaskName')." -ForegroundColor Yellow; exit 1 }
$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host "Task state:   $($task.State)   (Running = scanner is up)"
Write-Host "Last started: $($info.LastRunTime)   result: $($info.LastTaskResult)"
if (Test-Path $Status) {
    $s = Get-Content $Status -Raw | ConvertFrom-Json
    Write-Host "Last scan:    $($s.last_scan)   in session: $($s.running)   data outage: $($s.outage)"
}
if (Test-Path $Log) { Write-Host "`nLast 15 log lines:"; Get-Content $Log -Tail 15 }
