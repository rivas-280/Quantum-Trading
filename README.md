# Quantum-Trading

## ETF Signal Scanner (Claude Code skill)

A Claude Code skill that scans ETFs with leveraged bull/bear versions for
reversal setups, on a repeating schedule (every 15 min by default, configurable).

| Side | RSI 7 & 14 | StochRSI (3,3,14,14) | MACD (8,21,5) | Bollinger (20,2) | Look at |
|------|-----------|----------------------|---------------|------------------|---------|
| Bullish | < 30 | %K & %D < 30 | MACD about to cross **above** signal | candle pierces lower band | bull ETF (SOXL, TQQQ, …) |
| Bearish | > 70 | %K & %D > 70 | MACD about to cross **below** signal | candle pierces upper band | bear ETF (SOXS, SQQQ, …) |

All 4 = **SIGNAL**, 3 of 4 = **WATCH**. The default universe has 30 underlyings
(SPY, QQQ, IWM, DIA, SOXX, XLK, XLF, XLE, XBI, TLT, GLD, GDX, USO, UNG, FXI, …),
each mapped to its leveraged ETFs.

### Setup

```bash
pip install -r .claude/skills/etf-signal-scanner/requirements.txt
```

Market data comes from Yahoo Finance's public chart endpoint (no API key). You
can switch to `yfinance` or local CSV files in the config.

### Use it in Claude Code

From this repo:

```
/etf-signal-scanner                # one scan now
/etf-signal-scanner loop           # repeat every interval_minutes (15 by default)
/etf-signal-scanner loop 5         # repeat every 5 minutes
/etf-signal-scanner SOXX,QQQ all   # just these tickers, with the full indicator table
/etf-signal-scanner stop           # stop the loop
```

Or just ask: *"scan the leveraged ETFs every 15 minutes"*.
Scans outside US market hours are skipped (add `force` to override), and each
alert is tagged **NEW** or ongoing so repeat runs only call out what changed.

### Use it without Claude

```bash
python3 .claude/skills/etf-signal-scanner/scripts/scan.py           # one scan
python3 .claude/skills/etf-signal-scanner/scripts/scan.py --loop    # every interval_minutes, aligned to bar closes
python3 .claude/skills/etf-signal-scanner/scripts/scan.py --json --all --force --tickers SOXX,SPY --timeframe 5m
```

### Configuration

Everything is in [`.claude/skills/etf-signal-scanner/config.yaml`](.claude/skills/etf-signal-scanner/config.yaml):
loop interval, bar timeframe, indicator lengths, thresholds, how "about to cross"
is defined, Bollinger touch/close/cross mode, how many recent bars a condition
may have fired in, and the ticker universe.

How "MACD about to cross" works: the histogram (MACD − signal) is still on the
wrong side of zero but has moved toward zero for `converging_bars` bars in a row
and, at its current slope, reaches zero within `lookahead_bars`. A cross that
already happened within `max_bars_since_cross` bars also counts.

Indicator math is plain pandas, uses TradingView's definitions, and is checked
against the `ta` library.

### Tests

```bash
pip install pytest && python3 -m pytest tests
```

> This is a screening tool, not financial advice. Leveraged ETFs reset daily
> and lose value over time in choppy markets; check every setup yourself.
