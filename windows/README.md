# Run the ETF scanner on a Windows PC

This sets the scanner up to run on its own, around the clock, without Claude:
it starts when the PC boots (even if nobody logs in), restarts itself if it
crashes, scans every 15 minutes from 04:00 to 20:00 ET on weekdays, and posts
to `#etf-scanner` in Slack.

**Needs:** Windows 10 or 11, an always-on internet connection, about 500 MB of
disk, and an administrator account. About 15 minutes the first time.

## 1. Create a Slack webhook (about 2 minutes)

The scanner posts through a Slack "incoming webhook", a private URL that can
post into one channel.

1. Go to <https://api.slack.com/apps> and click **Create New App → From scratch**.
2. Name it `ETF Scanner`, pick the **Ethereal Exchange** workspace, click **Create App**.
3. In the left menu click **Incoming Webhooks** and switch it **On**.
4. Click **Add New Webhook to Workspace**, choose **#etf-scanner**, click **Allow**.
5. Copy the webhook URL (it starts with `https://hooks.slack.com/services/`).
   Treat it like a password: anyone with it can post to the channel.

## 2. Get the code onto the PC

Either:

- **With Git:** open PowerShell and run
  ```powershell
  winget install -e --id Git.Git        # if you don't have Git yet
  cd $HOME
  git clone -b claude/stock-analysis-skill-mrenzx https://github.com/rivas-280/quantum-trading.git Quantum-Trading
  ```
- **Without Git:** on GitHub open the repository, switch to the branch
  `claude/stock-analysis-skill-mrenzx`, click **Code → Download ZIP**, and unzip
  it to a permanent place such as `C:\Users\<you>\Quantum-Trading`.

Keep the folder where it is afterwards: the scheduled task points at it.

## 3. Run the installer

1. Click Start, type **PowerShell**, right-click **Windows PowerShell → Run as administrator**.
2. Go to the folder and run the installer:
   ```powershell
   cd $HOME\Quantum-Trading
   powershell -ExecutionPolicy Bypass -File .\windows\install.ps1
   ```
3. Answer its prompts:
   - If Python isn't installed it offers to install it; afterwards it asks you to
     open a new Administrator PowerShell and run the installer again.
   - Paste the webhook URL from step 1. A test message appears in `#etf-scanner`.
   - Choose whether to stop the PC sleeping while plugged in (recommended: yes).

When it finishes you'll see **ETF scanner started** in `#etf-scanner`.

## Day to day

| To... | Run (in the Quantum-Trading folder) |
|---|---|
| Check it's running | `powershell -ExecutionPolicy Bypass -File .\windows\status.ps1` |
| Read the log | `Get-Content .claude\skills\etf-signal-scanner\logs\scanner.log -Tail 40` |
| Change settings | Edit `.claude\skills\etf-signal-scanner\config.yaml`, then restart: `Stop-ScheduledTask "ETF Signal Scanner"; Start-ScheduledTask "ETF Signal Scanner"` (as admin) |
| Update the code | `git pull` (or download the ZIP again), run `install.ps1` again |
| Remove it | `powershell -ExecutionPolicy Bypass -File .\windows\uninstall.ps1` (as admin) |

## What gets posted to #etf-scanner

- **ETF scanner started**: every time the program starts (boot, crash restart,
  reinstall). Several of these in a row means it's crashing; check the log.
- **New SIGNAL** alerts, with the 1h / 2h / 4h / daily table.
- **Stopped for the day**: after 20:00 ET, with the time scans resume.
- **Isn't getting market data / data is back**: if half or more of the ETFs fail to download.
- **ETF scanner stopped**: when it's closed normally or uninstalled.

It can't post if the PC loses power or internet, or is switched off. If
`#etf-scanner` goes quiet during scan hours with no "stopped" message, check the PC.

## Notes

- The task runs as your Windows user with "run whether user is logged on or not",
  without storing your password. If you change your Windows password, nothing
  needs to change.
- Every 15 minutes Task Scheduler also checks the scanner is running and starts it
  if not, so a crash is never down for more than 15 minutes.
- The log grows by roughly 10 MB a month; delete `scanner.log` whenever you like.
- Once this is running, the copy that runs inside Claude Code should be stopped,
  or every alert is posted twice.
