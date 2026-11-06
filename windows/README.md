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

When it finishes you'll see **ETF scanner started** in `#etf-scanner`, and an
**ETF Scanner** icon on the Desktop and in the Start menu.

## The dashboard

Double-click **ETF Scanner** on the Desktop to open the dashboard:

- **Status**: Running / Scanning now / Paused / Not running, with the scan hours.
- **Last check**: when the scanner last checked and what happened (scanned,
  outside scan hours, paused, failed), plus when the next check is due.
- **Last market scan**: the results of the most recent real scan. A *Signals and
  watches* table shows which leveraged ETF to look at, and an *All ETFs* table
  shows every ETF on 1h / 2h / 4h / daily (▲ / ▼ = bullish / bearish setup with
  conditions met, ↑ / ↓ = which way the current candle leans). Signal rows are
  green (bullish) or red (bearish), watches yellow.
- **Charts**: double-click any ETF (in either table) to open its candlestick
  chart: candles with Bollinger Bands (20, 2) and volume, plus RSI (7, 14),
  StochRSI (3, 3, 14, 14) and MACD (8, 21, 5) panels with the 30/70 levels
  marked. Pick **15m / 1h / 2h / 4h / 1D** at the top right (or keys 1-5);
  mouse wheel zooms, dragging pans, **Home** jumps back to the latest candle, and
  hovering shows the values under the cursor. The line under the price shows
  the scanner's verdict on the current candle (which of the 4 conditions are
  met). A chart opened from a signal starts on the timeframe that triggered it.
- **Start scanner**: resumes a paused scanner, or starts the scheduled task if it
  isn't running. **Pause**: stops scanning and alerts until you press Start (it
  stays paused across reboots). **Scan now**: runs a full scan straight away, even
  outside scan hours. **Open log**: opens `scanner.log`.

Slack keeps working exactly as before: the background scanner does all the
posting, whether or not the dashboard is open, and it also posts when it's paused
or resumed. Closing the dashboard doesn't stop the scanner.

## Day to day

| To... | Run (in the Quantum-Trading folder) |
|---|---|
| See the status, last scan, start / pause | Double-click **ETF Scanner** on the Desktop |
| Check it's running (text) | `powershell -ExecutionPolicy Bypass -File .\windows\status.ps1` |
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
- **ETF scanner paused / resumed**: when you press Pause or Start in the dashboard.
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
