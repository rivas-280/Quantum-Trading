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
$Dash = Join-Path $Skill "state\dashboard.json"
if (Test-Path $Dash) {
    $d = Get-Content $Dash -Raw | ConvertFrom-Json
    Write-Host "Heartbeat:    $($d.heartbeat)   paused: $($d.paused)   last check: $($d.last_check) ($($d.last_result))"
    if ($d.paused) { Write-Host "Paused from the dashboard: press Start in the 'ETF Scanner' window to resume." -ForegroundColor Yellow }
}
if ($task.State -ne "Running" -and (Test-Path $Log)) {
    # If something still holds the log open while the task isn't running, an
    # orphaned scanner from an earlier install is running outside the task and
    # blocks it from starting (it exits with result 1).
    try { [IO.File]::Open($Log, "Append", "Write", "Read").Close() }
    catch {
        Write-Host ("`nA scanner is running outside the scheduled task and is blocking it. " +
                    "Run install.ps1 again as Administrator to clean it up.") -ForegroundColor Red
    }
}
if (Test-Path $Log) { Write-Host "`nLast 15 log lines:"; Get-Content $Log -Tail 15 }
