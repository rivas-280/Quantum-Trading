---
name: etf-signal-scanner
description: Scan ETFs that have leveraged bull/bear versions for RSI(7,14) + StochRSI(3,3,14,14) + MACD(8,21,5) + Bollinger(20,2) confluence (oversold with MACD about to cross up and a lower-band pierce, or the overbought mirror) and say which leveraged ETF to look at. Use when the user asks to scan or check ETFs/leveraged ETFs for setups or signals, says "run the scanner", or wants to start or stop a recurring scan (e.g. "scan every 15 minutes").
argument-hint: "[loop [minutes] | stop | all | force | TICKER,TICKER]"
allowed-tools: Bash(python3 .claude/skills/etf-signal-scanner/scripts/scan.py:*), Read, mcp__Slack__slack_send_message
---

# ETF Signal Scanner

Finds reversal setups on ETFs that have leveraged versions, and points to the
leveraged ETF to trade:

| Side | RSI 7 & 14 | StochRSI %K & %D | MACD (8,21,5) | Bollinger (20,2) | Leveraged ETF |
|------|-----------|------------------|---------------|------------------|-----------------|
| Bullish | < 30 | < 30 | about to cross **above** signal (or just did) | candle pierces **below** lower band | bull (e.g. SOXL) |
| Bearish | > 70 | > 70 | about to cross **below** signal (or just did) | candle pierces **above** upper band | bear (e.g. SOXS) |

4/4 conditions = **SIGNAL**, 3/4 = **WATCH**. All thresholds, lengths, the bar
timeframe, the loop interval and the ticker universe are in
`.claude/skills/etf-signal-scanner/config.yaml`.

## Arguments (`$ARGUMENTS`)

- *(empty)*: run one scan and report.
- `loop` or `loop N`: start a recurring scan (see Looping below).
- `stop`: stop a running loop.
- `all`: also show the indicator table for every ticker.
- `force`: scan even outside market hours.
- A comma-separated ticker list (e.g. `SOXX,QQQ`): scan only those, including tickers not in the universe.

## Running a scan

1. From the repository root run:
   ```bash
   python3 .claude/skills/etf-signal-scanner/scripts/scan.py --json [--force] [--all] [--tickers T1,T2]
   ```
   Add `--force` if the user passed `force`, `--all` for `all`, and `--tickers` for a ticker list.
   If it fails with `ModuleNotFoundError`, tell the user to run
   `pip install -r .claude/skills/etf-signal-scanner/requirements.txt`.

2. If the JSON has `"status": "market_closed"`, reply with a single line saying
   the market is closed and the scan was skipped. Do nothing else (this matters
   when looping, so overnight runs stay quiet).

3. Otherwise read `alerts` (already sorted SIGNAL before WATCH, new first) and report:
   - **New SIGNALs first.** For each: ticker, direction, price, RSI 7/14,
     StochRSI K/D, MACD state (`bars_to_cross` or `bars_since_cross`), which band
     was pierced, and the leveraged ETFs in `trade`.
   - Then new WATCH items in one line each, naming the missing condition
     (the `conditions` entry that is `false`).
   - Ongoing alerts (`"new": false`) as one compact line, and `cleared` keys if any.
   - If there are no alerts: one line, e.g. "No setups on 30 ETFs (15m, bar 10:30 ET)."
   - Mention `errors` briefly (ticker + reason) if present; do not retry them.
   Keep looping reports short: a few lines unless something new fired.

4. **Slack alert.** If the JSON's `notify` is not null, send `notify.message`
   unchanged with the Slack `slack_send_message` tool to `notify.channel_id`
   (load it with ToolSearch if needed), then add the message link to your report.
   Send exactly one message per scan and only when `notify` is present: the
   scanner already limits it to alerts that are new this scan at the levels in
   `config.yaml` → `slack.levels`, so ongoing alerts are never re-posted. If the
   send fails, say so in one line and carry on; do not retry or post elsewhere.
   Never post when the market is closed or when `notify` is null.

5. Do not place trades or tell the user to buy anything. Present setups as
   candidates to check; leveraged ETFs reset daily and decay, so mention it
   once if the user seems to be holding positions overnight.

## Looping

The scanner itself does a single pass; recurring runs come from Claude Code's
`/loop` skill.

- On `loop` / "every N minutes" / "keep scanning": take N from the argument or
  the user's words, otherwise read `interval_minutes` from `config.yaml`
  (default 15). Then invoke the `loop` skill with `"<N>m /etf-signal-scanner"`
  (add ` force` or a ticker list after it if the user asked for those). Run the
  first scan right away, then confirm the interval and how to stop.
- On `stop`: end the loop (stop the scheduled job the loop created) and confirm.

The scanner remembers the previous pass in `state/last_scan.json`, so each
run tags alerts as new or ongoing and you only shout about new ones.

For running without Claude (e.g. in a terminal or on a server), point the
user to `python3 .claude/skills/etf-signal-scanner/scripts/scan.py --loop`,
which waits for each bar to close and rescans every `interval_minutes`.

## Changing settings

When the user asks to change thresholds, timeframe, interval, tickers or rules,
edit `config.yaml` (it is commented) rather than the script. Useful knobs:

- `data.timeframe`: bar size (15m default; 5m, 1h, 1d, ...).
- `rules.window_bars`: how many recent bars RSI/StochRSI/band conditions may
  have fired in (default 3; 1 = all on the latest bar).
- `rules.rsi.require`: `all` (RSI 7 **and** 14) or `any`.
- `rules.macd.lookahead_bars` / `max_bars_since_cross`: how close or how recent the cross must be.
- `rules.bollinger.mode`: `touch` (wick), `close`, or `cross`.
- `scan_targets`: `underlying` (default), `leveraged`, or `both`.
- `universe`: add or remove `{underlying, bull, bear}` entries.
- `slack.channel_id` / `slack.levels` / `slack.enabled`: where Slack alerts go, which
  levels trigger them (`[SIGNAL]` by default, add `WATCH` for near-misses), or turn them off.
