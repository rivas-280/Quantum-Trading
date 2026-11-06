<#
  ETF Signal Scanner - Windows installer.

  Run from an *Administrator* PowerShell window, in the repository folder:

      powershell -ExecutionPolicy Bypass -File .\windows\install.ps1

  What it does (safe to run again; it updates the existing install):
    1. Finds Python 3.11+ (offers to install it with winget if missing)
    2. Creates a private Python environment and installs the scanner's packages
    3. Saves your Slack webhook URL and posts a test message
    4. Saves your Webull OpenAPI App Key/Secret and connects to Webull (approve it in the Webull app)
    5. Optionally stops this PC from sleeping while plugged in
    6. Registers the scheduled task "ETF Signal Scanner", which starts the scanner
       at boot (whether or not you're logged in), restarts it if it crashes, and
       re-checks every 15 minutes that it's still running
    7. Starts it now
    8. Adds an "ETF Scanner" dashboard shortcut to the Desktop and Start menu
#>
#Requires -RunAsAdministrator
$ErrorActionPreference = "Stop"

$Repo     = Split-Path -Parent $PSScriptRoot
$Skill    = Join-Path $Repo ".claude\skills\etf-signal-scanner"
$Script   = Join-Path $Skill "scripts\scan.py"
$Reqs     = Join-Path $Skill "requirements.txt"
$Venv     = Join-Path $Skill ".venv"
$Py       = Join-Path $Venv "Scripts\python.exe"
$Webhook  = Join-Path $Skill "slack_webhook_url.txt"
$LogDir   = Join-Path $Skill "logs"
$Log      = Join-Path $LogDir "scanner.log"
$TaskName = "ETF Signal Scanner"

function Step($n, $text) { Write-Host "`n[$n/8] $text" -ForegroundColor Cyan }
function Fail($text) { Write-Host "ERROR: $text" -ForegroundColor Red; exit 1 }

if (-not (Test-Path $Script)) { Fail "Can't find $Script. Run this from inside the Quantum-Trading folder." }

# Clicking in a console window with QuickEdit on starts a text selection ("Select" in
# the title), which freezes this script at its next output until Esc is pressed.
# Turn QuickEdit off while installing. (Pasting still works with Ctrl+V.)
try {
    Add-Type -Namespace Install -Name Console -MemberDefinition @'
[DllImport("kernel32.dll")] public static extern IntPtr GetStdHandle(int h);
[DllImport("kernel32.dll")] public static extern bool GetConsoleMode(IntPtr h, out uint m);
[DllImport("kernel32.dll")] public static extern bool SetConsoleMode(IntPtr h, uint m);
'@
    $hIn = [Install.Console]::GetStdHandle(-10); $mode = 0
    if ([Install.Console]::GetConsoleMode($hIn, [ref]$mode)) {
        [void][Install.Console]::SetConsoleMode($hIn, ($mode -band (-bnot 0x40)) -bor 0x80)
    }
} catch { }

# ------------------------------------------------------------------ 1. Python
Step 1 "Checking for Python 3.11 or newer"
function Find-Python {
    $candidates = @()
    if (Get-Command py -ErrorAction SilentlyContinue)     { $candidates += ,@("py", "-3") }
    if (Get-Command python -ErrorAction SilentlyContinue) { $candidates += ,@("python") }
    foreach ($c in $candidates) {
        $exe = $c[0]; $extra = @($c | Select-Object -Skip 1)
        try {
            $ok = & $exe @extra -c "import sys; print(int(sys.version_info >= (3, 11)))" 2>$null
            if ("$ok".Trim() -eq "1") { return ,$c }
        } catch { }
    }
    return $null
}
$base = Find-Python
if (-not $base) {
    Write-Host "Python 3.11+ was not found."
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        $ans = Read-Host "Install Python 3.12 now with winget? (Y/n)"
        if ($ans -ne "n") {
            winget install -e --id Python.Python.3.12 --scope machine --accept-package-agreements --accept-source-agreements
            Write-Host "`nPython installed. Close this window, open a new Administrator PowerShell, and run the installer again." -ForegroundColor Yellow
            exit 0
        }
    }
    Fail "Install Python 3.12 from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then run this again."
}
$baseExe = $base[0]; $baseArgs = @($base | Select-Object -Skip 1)
Write-Host ("Using " + (& $baseExe @baseArgs --version))

# ------------------------------------------------------------------ 2. packages
Step 2 "Installing the scanner's Python packages (first run takes a few minutes)"
if (-not (Test-Path $Py)) {
    & $baseExe @baseArgs -m venv $Venv
    if ($LASTEXITCODE -ne 0) { Fail "Couldn't create the Python environment in $Venv" }
}
& $Py -m pip install --upgrade pip --quiet
& $Py -m pip install -r $Reqs --quiet
if ($LASTEXITCODE -ne 0) { Fail "Package install failed (see the messages above)." }
& $Py $Script --help > $null
if ($LASTEXITCODE -ne 0) { Fail "The scanner didn't start; see the error above." }
Write-Host "Packages installed."

# ------------------------------------------------------------------ 3. Slack
Step 3 "Slack webhook"
$current = if (Test-Path $Webhook) { (Get-Content $Webhook -Raw).Trim() } else { "" }
if ($current) {
    $ans = Read-Host "A webhook URL is already saved. Replace it? (y/N)"
    if ($ans -eq "y") { $current = "" }
}
while (-not $current) {
    $url = (Read-Host "Paste (Ctrl+V) the Slack incoming-webhook URL for #etf-scanner (starts with https://hooks.slack.com/)").Trim()
    if ($url -match '^https://hooks\.slack\.com/') { $current = $url } else { Write-Host "That doesn't look like a Slack webhook URL; try again." }
}
Set-Content -Path $Webhook -Value $current -NoNewline -Encoding ascii
& $Py $Script --test-slack
if ($LASTEXITCODE -ne 0) { Fail "Slack test post failed. Check the webhook URL and run the installer again." }

# ------------------------------------------------------------------ 4. Webull
Step 4 "Webull market data"
$WebullCreds = Join-Path $Skill "webull_credentials.yaml"
function Test-WebullKeys {
    if (-not (Test-Path $WebullCreds)) { return $false }
    $t = Get-Content $WebullCreds -Raw
    return ($t -match "(?m)^\s*app_key:\s*['""]?[^'""\s]+") -and ($t -match "(?m)^\s*app_secret:\s*['""]?[^'""\s]+")
}
$haveKeys = Test-WebullKeys
if ($haveKeys) {
    $ans = Read-Host "Webull App Key/Secret are already saved. Replace them? (y/N)"
    if ($ans -eq "y") { $haveKeys = $false }
}
if (-not $haveKeys) {
    Write-Host "Scans use Webull's own candles via the Webull OpenAPI (needs approved API access and an"
    Write-Host "OpenAPI market-data subscription in your Webull account). Without it they use Yahoo Finance."
    $key = (Read-Host "Webull OpenAPI App Key (press Enter to skip)").Trim()
    if ($key) {
        $sec = Read-Host "Webull OpenAPI App Secret (hidden)" -AsSecureString
        $secret = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)).Trim()
        $q = { param($v) "'" + ($v -replace "'", "''") + "'" }
        @(
            "# Webull OpenAPI credentials for the ETF scanner. Private: git-ignored, never leaves this PC.",
            "app_key: $(& $q $key)",
            "app_secret: $(& $q $secret)",
            "region: us"
        ) | Set-Content -Path $WebullCreds -Encoding ascii
        $secret = $null
        $haveKeys = $true
    }
}
if ($haveKeys) {
    Write-Host "Connecting to Webull. If the Webull app shows an OpenAPI login request, approve it (waits up to 5 minutes)..."
    & $Py $Script --webull-login
    if ($LASTEXITCODE -ne 0) {
        Write-Host ("Webull isn't ready yet (see above). Scans use Yahoo Finance until it is; once fixed, press " +
                    "'Connect Webull' in the ETF Scanner dashboard.") -ForegroundColor Yellow
    }
} else {
    Write-Host "Skipped: scans use Yahoo Finance data. Re-run the installer to add Webull later."
}

# ------------------------------------------------------------------ 5. sleep
Step 5 "Power settings"
$ans = Read-Host "Stop this PC from sleeping or hibernating while plugged in, so scans don't pause? (Y/n)"
if ($ans -ne "n") {
    powercfg /change standby-timeout-ac 0
    powercfg /change hibernate-timeout-ac 0
    Write-Host "Sleep disabled on AC power. (On a laptop, also set 'When I close the lid' to 'Do nothing'.)"
} else {
    Write-Host "Skipped. Scans pause whenever the PC sleeps."
}

# ------------------------------------------------------------------ 6. scheduled task
Step 6 "Registering the scheduled task '$TaskName'"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$cmdLine  = "`"$Py`" -u `"$Script`" --loop --post-slack >> `"$Log`" 2>&1"
$action   = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$cmdLine`"" -WorkingDirectory $Repo
$triggers = @(
    (New-ScheduledTaskTrigger -AtStartup),
    # Re-check every 15 minutes: if the scanner isn't running, this starts it
    # (if it is running, MultipleInstances=IgnoreNew makes this a no-op).
    (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 15))
)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
# S4U = runs whether or not you're logged in, without storing your password.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Limited
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}
# Stop-ScheduledTask only kills the cmd.exe wrapper; python.exe keeps running
# (and keeps scanner.log locked, so the new task can't start). Kill any scanner
# left over from an earlier install, uninstall or manual run.
$stray = @(Get-CimInstance Win32_Process | Where-Object {
    $_.ProcessId -ne $PID -and $_.CommandLine -and $_.CommandLine -like "*$Script*" })
if ($stray.Count) {
    $stray | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
    Write-Host "Stopped $($stray.Count) leftover scanner process(es)."
}
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers -Settings $settings `
    -Principal $principal -Force `
    -Description "Scans leveraged-ETF underlyings every 15 min and posts signals to Slack #etf-scanner." | Out-Null
Write-Host "Task registered."

# ------------------------------------------------------------------ 7. start
Step 7 "Starting the scanner"
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 20
$state = (Get-ScheduledTask -TaskName $TaskName).State
Write-Host "Task state: $state"
if (Test-Path $Log) { Write-Host "`nLast log lines:"; Get-Content $Log -Tail 5 }

# ------------------------------------------------------------------ 8. dashboard shortcut
Step 8 "Adding the 'ETF Scanner' dashboard shortcut"
$Pyw  = Join-Path $Venv "Scripts\pythonw.exe"
$Dash = Join-Path $Skill "scripts\dashboard.py"
$Icon = Join-Path $PSScriptRoot "etf-scanner.ico"
$shell = New-Object -ComObject WScript.Shell
foreach ($dir in @([Environment]::GetFolderPath("Desktop"), [Environment]::GetFolderPath("Programs"))) {
    $lnk = $shell.CreateShortcut((Join-Path $dir "ETF Scanner.lnk"))
    $lnk.TargetPath       = $Pyw
    $lnk.Arguments        = "`"$Dash`""
    $lnk.WorkingDirectory = $Repo
    $lnk.IconLocation     = "$Icon,0"
    $lnk.Description      = "ETF Signal Scanner dashboard: status, last scan, start / pause"
    $lnk.Save()
}
Write-Host "Shortcut added to the Desktop and the Start menu."
Write-Host @"

Done. The scanner now runs on its own:
  - Open the dashboard with the 'ETF Scanner' icon on the Desktop (or Start menu) to see
    its status and the last scan, and to start, pause or scan now.
  - It posts 'ETF scanner started' to #etf-scanner each time it starts, alerts for new
    SIGNALs, a message when it stops for the day, and data-outage notices.
  - Log file:   $Log
  - Status:     powershell -ExecutionPolicy Bypass -File .\windows\status.ps1
  - Uninstall:  powershell -ExecutionPolicy Bypass -File .\windows\uninstall.ps1  (as Administrator)
"@ -ForegroundColor Green
