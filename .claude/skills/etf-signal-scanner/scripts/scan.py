#!/usr/bin/env python3
"""Multi-timeframe ETF confluence scanner: RSI + StochRSI + MACD + Bollinger.

Bullish setup (look at the leveraged bull ETFs):
  * RSI(7) and RSI(14) below 30
  * StochRSI(3,3,14,14) %K/%D below 30
  * MACD(8,21,5) line about to cross ABOVE its signal line (or just did)
  * A candle pierces BELOW the lower Bollinger Band (20, 2)

Bearish setup (look at the inverse leveraged ETFs): the mirror image -
  RSI / StochRSI above 70, MACD about to cross BELOW signal, candle pierces
  ABOVE the upper band.

Every ticker is evaluated on each timeframe in data.timeframes (default
1h, 2h, 4h, 1d). When any timeframe triggers, the alert shows all of them.
2h/4h bars are built from 1h bars anchored to the 09:30 ET open.

All thresholds, lengths, timeframes and the universe live in ../config.yaml.

Usage:
  python3 scan.py                 # one scan, human-readable output
  python3 scan.py --json          # one scan, JSON (what the Claude skill reads)
  python3 scan.py --loop          # scan forever every interval_minutes
  python3 scan.py --force         # ignore market-hours gate
  python3 scan.py --tickers SPY,QQQ --timeframes 1h,1d --all
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import socket
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yaml

SKILL_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = SKILL_DIR / "config.yaml"
STATE_FILE = SKILL_DIR / "state" / "last_scan.json"
STATUS_FILE = SKILL_DIR / "state" / "status.json"
CONTROL_FILE = SKILL_DIR / "state" / "control.json"      # written by the dashboard: pause / scan now
DASHBOARD_FILE = SKILL_DIR / "state" / "dashboard.json"  # written by --loop: heartbeat + last check
WEBHOOK_FILE = SKILL_DIR / "slack_webhook_url.txt"   # git-ignored; or set SLACK_WEBHOOK_URL
ET = ZoneInfo("America/New_York")
SESSION_OPEN = dt.time(9, 30)        # regular trading hours
SESSION_CLOSE = dt.time(16, 0)
EXT_OPEN = dt.time(4, 0)             # pre-market start / post-market end
EXT_CLOSE = dt.time(20, 0)

# timeframe -> (interval to download, minutes per bar, resample from download?)
TIMEFRAMES = {
    "1m": ("1m", 1, False), "2m": ("2m", 2, False), "5m": ("5m", 5, False),
    "15m": ("15m", 15, False), "30m": ("30m", 30, False),
    "1h": ("60m", 60, False), "60m": ("60m", 60, False),
    "2h": ("60m", 120, True), "3h": ("60m", 180, True), "4h": ("60m", 240, True),
    "1d": ("1d", 1440, False),
}
# history to download per interval (valid for both Yahoo and yfinance)
AUTO_RANGE = {"1m": "5d", "2m": "1mo", "5m": "1mo", "15m": "60d", "30m": "60d",
              "60m": "1y", "1d": "2y"}


def session_hours(data_cfg: dict) -> tuple[dt.time, dt.time]:
    """Intraday session the candles cover: 04:00-20:00 with extended hours, else 09:30-16:00."""
    return (EXT_OPEN, EXT_CLOSE) if data_cfg.get("include_prepost") else (SESSION_OPEN, SESSION_CLOSE)


def timeframe_plan(tf: str, data_cfg: dict) -> tuple[str, int, bool]:
    """(interval to download, minutes per bar, resample?) for a timeframe.

    With extended hours, Yahoo's 60m bars mix clock hours (04:00...09:00) with
    09:30-anchored ones, so 1h/2h/3h/4h are built from 15m bars on the clock,
    anchored to 04:00 like TradingView's extended-hours charts."""
    interval, minutes, derived = TIMEFRAMES[tf]
    if data_cfg.get("include_prepost") and 60 <= minutes < 1440:
        return "15m", minutes, True
    return interval, minutes, derived

# =========================================================================
# Indicators (TradingView-compatible definitions, pure pandas)
# =========================================================================

def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def rma(series: pd.Series, length: int) -> pd.Series:
    """Wilder's moving average, seeded with an SMA like TradingView's ta.rma."""
    values = series.to_numpy(dtype=float)
    out = np.full(len(values), np.nan)
    valid = np.flatnonzero(~np.isnan(values))
    if len(valid) < length:
        return pd.Series(out, index=series.index)
    start = valid[0]
    seed_end = start + length
    if np.isnan(values[start:seed_end]).any():
        return pd.Series(out, index=series.index)
    out[seed_end - 1] = values[start:seed_end].mean()
    alpha = 1.0 / length
    for i in range(seed_end, len(values)):
        v = values[i]
        out[i] = out[i - 1] if np.isnan(v) else alpha * v + (1 - alpha) * out[i - 1]
    return pd.Series(out, index=series.index)


def rsi(close: pd.Series, length: int) -> pd.Series:
    delta = close.diff()
    avg_gain = rma(delta.clip(lower=0), length)
    avg_loss = rma(-delta.clip(upper=0), length)
    with np.errstate(divide="ignore", invalid="ignore"):
        value = 100 - 100 / (1 + avg_gain / avg_loss)
    value = value.mask((avg_loss == 0) & (avg_gain > 0), 100.0)
    value = value.mask((avg_loss == 0) & (avg_gain == 0), 50.0)
    return value


def stoch_rsi(close: pd.Series, smooth_k: int = 3, smooth_d: int = 3,
              rsi_length: int = 14, stoch_length: int = 14) -> tuple[pd.Series, pd.Series]:
    r = rsi(close, rsi_length)
    lowest = r.rolling(stoch_length).min()
    highest = r.rolling(stoch_length).max()
    span = (highest - lowest).replace(0, np.nan)
    stoch = 100 * (r - lowest) / span
    k = stoch.rolling(smooth_k).mean()
    d = k.rolling(smooth_d).mean()
    return k, d


def macd(close: pd.Series, fast: int = 8, slow: int = 21,
         signal: int = 5) -> tuple[pd.Series, pd.Series, pd.Series]:
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line, signal)
    return line, sig, line - sig


def bollinger(close: pd.Series, length: int = 20,
              stddev: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series]:
    mid = close.rolling(length).mean()
    sd = close.rolling(length).std(ddof=0)  # population stdev, as TradingView
    return mid - stddev * sd, mid, mid + stddev * sd


def compute_indicators(df: pd.DataFrame, ind_cfg: dict) -> pd.DataFrame:
    out = df.copy()
    close = out["close"]
    for length in ind_cfg["rsi"]["lengths"]:
        out[f"rsi{length}"] = rsi(close, int(length))
    s = ind_cfg["stochrsi"]
    out["stoch_k"], out["stoch_d"] = stoch_rsi(
        close, int(s["smooth_k"]), int(s["smooth_d"]),
        int(s["rsi_length"]), int(s["stoch_length"]))
    m = ind_cfg["macd"]
    out["macd"], out["macd_signal"], out["macd_hist"] = macd(
        close, int(m["fast"]), int(m["slow"]), int(m["signal"]))
    b = ind_cfg["bollinger"]
    out["bb_lower"], out["bb_mid"], out["bb_upper"] = bollinger(
        close, int(b["length"]), float(b["stddev"]))
    return out


def min_bars_needed(ind_cfg: dict) -> int:
    s, m, b = ind_cfg["stochrsi"], ind_cfg["macd"], ind_cfg["bollinger"]
    return max(
        int(s["rsi_length"]) + int(s["stoch_length"]) + int(s["smooth_k"]) + int(s["smooth_d"]),
        3 * int(m["slow"]) + int(m["signal"]),   # EMA warm-up
        int(b["length"]),
        3 * max(int(x) for x in ind_cfg["rsi"]["lengths"]),
    ) + 5


# =========================================================================
# Signal evaluation
# =========================================================================

def macd_cross_state(hist: pd.Series, direction: str, rules: dict) -> dict:
    """Is MACD about to cross (or has it just crossed) its signal line?

    Works on the histogram h = MACD - signal. For a bullish cross h goes from
    negative to positive; bearish is the mirror, so we flip the sign and use
    the same logic.
    """
    conv = int(rules.get("converging_bars", 2))
    lookahead = float(rules.get("lookahead_bars", 2))
    max_since = int(rules.get("max_bars_since_cross", 1))

    h = hist.dropna().to_numpy(dtype=float)
    if len(h) < max(conv, max_since) + 2:
        return {"met": False, "state": "insufficient_data"}
    x = h if direction == "bull" else -h

    # 1) Crossed within the last `max_since` bars and still on the new side?
    for since in range(0, max_since + 1):
        i = len(x) - 1 - since
        if i < 1:
            break
        if x[i] > 0 and x[i - 1] <= 0:
            if (x[i:] > 0).all():
                return {"met": True, "state": "crossed", "bars_since_cross": since}
            break

    # 2) Still on the wrong side but converging fast enough?
    if x[-1] < 0:
        steps = np.diff(x[-(conv + 1):])
        if len(steps) == conv and (steps > 0).all():
            bars_to_cross = -x[-1] / steps[-1]
            return {
                "met": bool(bars_to_cross <= lookahead),
                "state": "approaching",
                "bars_to_cross": round(float(bars_to_cross), 2),
            }
        return {"met": False, "state": "diverging"}
    return {"met": False, "state": "already_crossed"}


def _recent(mask: pd.Series, window: int) -> bool:
    return bool(mask.iloc[-window:].fillna(False).astype(bool).any())


def evaluate_direction(df: pd.DataFrame, direction: str, cfg: dict) -> dict:
    rules = cfg["rules"]
    window = max(1, int(rules.get("window_bars", 3)))
    rsi_lengths = [int(x) for x in cfg["indicators"]["rsi"]["lengths"]]
    bull = direction == "bull"
    conditions: dict[str, bool] = {}
    details: dict[str, object] = {}

    r = rules["rsi"]
    if r.get("enabled", True):
        level = float(r["oversold"] if bull else r["overbought"])
        masks = [(df[f"rsi{n}"] < level) if bull else (df[f"rsi{n}"] > level) for n in rsi_lengths]
        combined = masks[0]
        for mk in masks[1:]:
            combined = (combined & mk) if r.get("require", "all") == "all" else (combined | mk)
        conditions["rsi"] = _recent(combined, window)

    s = rules["stochrsi"]
    if s.get("enabled", True):
        level = float(s["oversold"] if bull else s["overbought"])
        cmp = (lambda col: df[col] < level) if bull else (lambda col: df[col] > level)
        line = s.get("line", "both")
        if line == "k":
            mask = cmp("stoch_k")
        elif line == "d":
            mask = cmp("stoch_d")
        else:
            mask = cmp("stoch_k") & cmp("stoch_d")
        conditions["stochrsi"] = _recent(mask, window)

    m = rules["macd"]
    if m.get("enabled", True):
        state = macd_cross_state(df["macd_hist"], direction, m)
        conditions["macd"] = state["met"]
        details["macd"] = state

    b = rules["bollinger"]
    if b.get("enabled", True):
        mode = b.get("mode", "touch")
        if bull:
            if mode == "close":
                mask = df["close"] < df["bb_lower"]
            elif mode == "cross":
                mask = (df["close"] < df["bb_lower"]) & (df["close"].shift() >= df["bb_lower"].shift())
            else:
                mask = df["low"] < df["bb_lower"]
        else:
            if mode == "close":
                mask = df["close"] > df["bb_upper"]
            elif mode == "cross":
                mask = (df["close"] > df["bb_upper"]) & (df["close"].shift() <= df["bb_upper"].shift())
            else:
                mask = df["high"] > df["bb_upper"]
        conditions["bollinger"] = _recent(mask, window)

    met = sum(conditions.values())
    enabled = len(conditions)
    sig_min = min(int(rules.get("signal_min_conditions", 4)), enabled)
    watch_min = int(rules.get("watch_min_conditions", 3))
    level = None
    if enabled and met >= sig_min:
        level = "SIGNAL"
    elif enabled and met >= watch_min:
        level = "WATCH"
    return {"level": level, "met": met, "enabled": enabled,
            "conditions": conditions, "details": details}


def _num(v) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else round(f, 4)


def analyze(df: pd.DataFrame, cfg: dict) -> dict:
    ind = compute_indicators(df, cfg["indicators"])
    last = ind.iloc[-1]
    snapshot = {col: _num(last[col]) for col in ind.columns
                if col not in ("open", "volume")}
    return {
        "last_bar": ind.index[-1].isoformat(),
        "bars": len(ind),
        "price": _num(last["close"]),
        "indicators": snapshot,
        "bull": evaluate_direction(ind, "bull", cfg),
        "bear": evaluate_direction(ind, "bear", cfg),
    }


# =========================================================================
# Data
# =========================================================================

def fetch_yahoo(ticker: str, interval: str, history_range: str,
                prepost: bool, session) -> pd.DataFrame:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    params = {"interval": interval, "range": history_range,
              "includePrePost": "true" if prepost else "false"}
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) etf-signal-scanner/1.0"}
    resp = session.get(url, params=params, headers=headers, timeout=20)
    resp.raise_for_status()
    chart = resp.json().get("chart", {})
    if chart.get("error"):
        raise RuntimeError(chart["error"].get("description", str(chart["error"])))
    result = (chart.get("result") or [None])[0]
    if not result or not result.get("timestamp"):
        raise RuntimeError("no data returned")
    quote = result["indicators"]["quote"][0]
    idx = pd.to_datetime(result["timestamp"], unit="s", utc=True).tz_convert(ET)
    return pd.DataFrame({k: quote.get(k) for k in ("open", "high", "low", "close", "volume")},
                        index=idx)


def fetch_yfinance(ticker: str, interval: str, history_range: str, prepost: bool) -> pd.DataFrame:
    import yfinance as yf
    df = yf.Ticker(ticker).history(period=history_range, interval=interval,
                                   prepost=prepost, auto_adjust=False)
    if df.empty:
        raise RuntimeError(f"no {interval} data returned")
    df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
    df.index = df.index.tz_convert(ET) if df.index.tz else df.index.tz_localize(ET)
    return df


def fetch_csv(ticker: str, csv_dir: Path) -> pd.DataFrame:
    """<TICKER>.csv with intraday bars; every timeframe is resampled from it."""
    path = csv_dir / f"{ticker}.csv"
    if not path.exists():
        raise RuntimeError(f"missing {path}")
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    time_col = next((c for c in ("datetime", "date", "time", "timestamp") if c in df.columns),
                    df.columns[0])
    idx = pd.to_datetime(df[time_col], utc=True).dt.tz_convert(ET)
    return df.set_index(pd.DatetimeIndex(idx))[["open", "high", "low", "close", "volume"]]


def clean_bars(df: pd.DataFrame) -> pd.DataFrame:
    df = df.dropna(subset=["open", "high", "low", "close"])
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df.astype(float)


_AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}


def resample_session(df: pd.DataFrame, minutes: int, anchor: dt.time = SESSION_OPEN) -> pd.DataFrame:
    """Group bars into `minutes`-long bars anchored at `anchor` each day
    (regular hours: 4h = 09:30-13:30 and 13:30-16:00, like TradingView;
    extended hours anchor at 04:00), or one bar per day when minutes >= 1440."""
    if df.empty:
        return df
    day = df.index.normalize()
    if minutes >= 1440:
        key = day
    else:
        start = anchor.hour * 60 + anchor.minute
        since_open = (df.index.hour * 60 + df.index.minute) - start
        bucket = np.floor_divide(np.asarray(since_open), minutes)
        offset = start + bucket * minutes
        key = day + pd.to_timedelta(offset, unit="m")
    out = df.groupby(key).agg(_AGG)
    out.index = pd.DatetimeIndex(out.index).tz_convert(ET) if out.index.tz else out.index
    return out.dropna(subset=["close"])


def check_price_jumps(bars: pd.DataFrame, max_change: float, lookback: int = 250) -> None:
    """Refuse data with an implausible close-to-close jump (typically a split
    the feed has only partly adjusted), instead of computing bogus signals."""
    if not max_change or len(bars) < 2:
        return
    change = bars["close"].iloc[-lookback:].pct_change().abs()
    worst = change.idxmax() if change.notna().any() else None
    if worst is not None and change[worst] > max_change:
        raise RuntimeError(f"close jumps {change[worst]:.0%} at {worst:%Y-%m-%d %H:%M}; "
                           "likely an unadjusted split in the data feed, skipped")


def bar_end(start: pd.Timestamp, timeframe: str, session_close: dt.time = SESSION_CLOSE) -> dt.datetime:
    minutes = TIMEFRAMES[timeframe][1]
    if minutes >= 1440:   # daily candles are regular-session only
        return dt.datetime.combine(start.date(), SESSION_CLOSE, tzinfo=ET)
    close = dt.datetime.combine(start.date(), session_close, tzinfo=ET)
    end = start.to_pydatetime() + dt.timedelta(minutes=minutes)
    # session-anchored bars stop at the close (e.g. the 15:30 1h bar, 13:30 4h bar)
    return min(end, close) if start.time() < session_close else end


def drop_forming_bar(df: pd.DataFrame, timeframe: str, now: dt.datetime,
                     session_close: dt.time = SESSION_CLOSE) -> pd.DataFrame:
    if df.empty:
        return df
    return df.iloc[:-1] if now < bar_end(df.index[-1], timeframe, session_close) else df


# =========================================================================
# Universe
# =========================================================================

def _etfs(spec, inverse: bool) -> list[dict]:
    """Accept {TICKER: leverage} or [TICKER, ...] and label each fund."""
    items = spec.items() if isinstance(spec, dict) else ((t, None) for t in (spec or []))
    out = []
    for ticker, lev in items:
        lev = None if lev is None else -abs(float(lev)) if inverse else abs(float(lev))
        out.append({"ticker": ticker, "leverage": lev,
                    "type": "inverse leveraged" if inverse else "leveraged"})
    return out


def describe_etf(e: dict) -> str:
    if e.get("leverage") is None:
        return f"{e['ticker']} ({e['type']})"
    return f"{e['ticker']} ({abs(e['leverage']):g}x {e['type']})"


def build_targets(cfg: dict, only: set[str] | None) -> list[dict]:
    mode = cfg.get("scan_targets", "underlying")
    targets: list[dict] = []
    seen: set[str] = set()

    def add(ticker, **meta):
        if ticker in seen or (only and ticker not in only):
            return
        seen.add(ticker)
        targets.append({"ticker": ticker, **meta})

    for entry in cfg["universe"]:
        u = entry["underlying"]
        bulls, bears = _etfs(entry.get("bull"), False), _etfs(entry.get("bear"), True)
        name = entry.get("name", u)
        if mode in ("underlying", "both"):
            add(u, name=name, role="underlying", underlying=u,
                trade_bull=bulls, trade_bear=bears)
        if mode in ("leveraged", "both"):
            for e in bulls:   # bull ETF rising -> it; falling -> the inverse funds
                add(e["ticker"], name=f"{name} ({describe_etf(e)})", role="leveraged",
                    underlying=u, trade_bull=[e], trade_bear=bears)
            for e in bears:   # inverse ETF rising == underlying falling
                add(e["ticker"], name=f"{name} ({describe_etf(e)})", role="inverse",
                    underlying=u, trade_bull=[e], trade_bear=bulls)
    if only:
        for t in sorted(only - seen):  # ad-hoc tickers not in the universe
            add(t, name=t, role="adhoc", underlying=t, trade_bull=[], trade_bear=[])
    return targets


# =========================================================================
# Scan
# =========================================================================

def market_is_open(now: dt.datetime, extended: bool = False) -> bool:
    """Weekday 09:30-16:05 ET, or 04:00-20:05 with extended hours."""
    now = now.astimezone(ET)
    if now.weekday() >= 5:
        return False
    open_, close = (EXT_OPEN, EXT_CLOSE) if extended else (SESSION_OPEN, SESSION_CLOSE)
    last = (dt.datetime.combine(now.date(), close) + dt.timedelta(minutes=5)).time()
    return open_ <= now.time() <= last


def configured_timeframes(data_cfg: dict) -> list[str]:
    tfs = data_cfg.get("timeframes") or [data_cfg.get("timeframe", "1h")]
    if isinstance(tfs, str):
        tfs = [t.strip() for t in tfs.split(",") if t.strip()]
    bad = [t for t in tfs if t not in TIMEFRAMES]
    if bad:
        raise SystemExit(f"Unsupported timeframe(s) {bad}; use any of {list(TIMEFRAMES)}")
    return list(dict.fromkeys(tfs))


def load_bars(ticker: str, timeframes: list[str], data_cfg: dict, session) -> dict[str, pd.DataFrame]:
    """Download each needed interval once and build every timeframe from it."""
    source = data_cfg.get("source", "yfinance")
    prepost = bool(data_cfg.get("include_prepost", False))
    ranges = data_cfg.get("history_range", "auto")
    anchor = session_hours(data_cfg)[0]
    if source == "csv":
        base = clean_bars(fetch_csv(ticker, (SKILL_DIR / data_cfg.get("csv_dir", "./data")).resolve()))
        if not prepost:   # keep only regular-hours bars
            t = base.index.time
            base = base[(t >= SESSION_OPEN) & (t < SESSION_CLOSE)]
        return {tf: resample_session(base, TIMEFRAMES[tf][1], anchor) for tf in timeframes}
    downloads: dict[str, pd.DataFrame] = {}
    out = {}
    for tf in timeframes:
        interval, minutes, derived = timeframe_plan(tf, data_cfg)
        if interval not in downloads:
            rng = AUTO_RANGE.get(interval, "1y") if ranges == "auto" else (
                ranges.get(interval, AUTO_RANGE.get(interval, "1y")) if isinstance(ranges, dict) else ranges)
            if source == "yahoo":
                raw = fetch_yahoo(ticker, interval, rng, prepost, session)
                time.sleep(float(data_cfg.get("request_pause_seconds", 0.25)))
            elif source == "yfinance":
                raw = fetch_yfinance(ticker, interval, rng, prepost)
            else:
                raise SystemExit(f"Unknown data source {source!r}")
            downloads[interval] = clean_bars(raw)
        bars = downloads[interval]
        out[tf] = resample_session(bars, minutes, anchor) if derived else bars
    return out


def run_scan(cfg: dict, only: set[str] | None, now: dt.datetime) -> dict:
    data_cfg = cfg["data"]
    timeframes = configured_timeframes(data_cfg)
    need = min_bars_needed(cfg["indicators"])
    closed_only = bool(data_cfg.get("closed_bars_only", False))
    session_close = session_hours(data_cfg)[1]
    session = None
    if data_cfg.get("source", "yfinance") == "yahoo":
        import requests
        session = requests.Session()

    results, errors = [], []
    for target in build_targets(cfg, only):
        t = target["ticker"]
        try:
            per_tf = load_bars(t, timeframes, data_cfg, session)
        except SystemExit:
            raise
        except Exception as exc:
            errors.append({"ticker": t, "error": f"{type(exc).__name__}: {exc}"})
            continue
        tf_results, price = {}, None
        for tf in timeframes:
            bars = per_tf.get(tf)
            try:
                if closed_only:
                    bars = drop_forming_bar(bars, tf, now, session_close)
                if bars is None or len(bars) < need:
                    raise RuntimeError(f"only {0 if bars is None else len(bars)} {tf} bars, need {need}")
                check_price_jumps(bars, float(data_cfg.get("max_bar_change", 0.4)))
                res = analyze(bars, cfg)
                res["live"] = bool(now < bar_end(bars.index[-1], tf, session_close))
                res["extended_hours"] = bool(data_cfg.get("include_prepost")) and TIMEFRAMES[tf][1] < 1440
                tf_results[tf] = res
                price = res["price"] if price is None or TIMEFRAMES[tf][1] < 1440 else price
            except Exception as exc:
                errors.append({"ticker": t, "timeframe": tf, "error": f"{type(exc).__name__}: {exc}"})
        if tf_results:
            results.append({**target, "price": price, "timeframes": tf_results})

    return {
        "scanned_at": now.astimezone(ET).isoformat(timespec="seconds"),
        "timeframes": timeframes,
        "session": "extended (04:00-20:00 ET)" if data_cfg.get("include_prepost") else "regular (09:30-16:00 ET)",
        "source": data_cfg.get("source", "yfinance"),
        "tickers_scanned": len(results),
        "errors": errors,
        "results": results,
    }


# =========================================================================
# Alerts / state
# =========================================================================

LEVEL_RANK = {"SIGNAL": 2, "WATCH": 1, None: 0}


def lean(res: dict) -> dict:
    """Which way the current candle points: more bullish or bearish conditions met."""
    up, down = res["bull"]["met"], res["bear"]["met"]
    side = "upside" if up > down else "downside" if down > up else "neutral"
    return {"side": side, "bull_met": up, "bear_met": down, "enabled": res["bull"]["enabled"]}


def _tf_summary(res: dict, direction: str, tf: str) -> dict:
    ev = res[direction]
    return {"timeframe": tf, "level": ev["level"], "met": ev["met"], "enabled": ev["enabled"],
            "lean": lean(res),
            "conditions": ev["conditions"], "macd": ev["details"].get("macd"),
            "last_bar": res["last_bar"], "live": res.get("live", False),
            "indicators": res["indicators"]}


def apply_state(scan: dict, cfg: dict, use_state: bool, now: dt.datetime) -> None:
    """Group triggers per ticker+direction and tag which are new.

    State is kept per ticker:direction:timeframe. A trigger that disappears
    is remembered for `rules.clear_after_minutes`, so a live bar flickering
    in and out of a setup is not reported as new each time.
    """
    clear_after = float(cfg["rules"].get("clear_after_minutes", 60))
    previous = {}
    if use_state and STATE_FILE.exists():
        try:
            previous = json.loads(STATE_FILE.read_text()).get("active", {})
        except (OSError, json.JSONDecodeError):
            previous = {}
    stamp = now.astimezone(ET).isoformat(timespec="seconds")
    active: dict[str, dict] = {}
    alerts = []
    for res in scan["results"]:
        for direction in ("bull", "bear"):
            tfs = res["timeframes"]
            triggers = {tf: r[direction]["level"] for tf, r in tfs.items() if r[direction]["level"]}
            if not triggers:
                continue
            new_tfs = []
            for tf, level in triggers.items():
                key = f"{res['ticker']}:{direction}:{tf}"
                prior = previous.get(key)
                is_new = prior is None or LEVEL_RANK[level] > LEVEL_RANK[prior.get("level")]
                if is_new:
                    new_tfs.append(tf)
                active[key] = {"level": level, "last_seen": stamp,
                               "first_seen": stamp if is_new else prior.get("first_seen", stamp)}
            best = max(triggers.values(), key=LEVEL_RANK.get)
            alerts.append({
                "ticker": res["ticker"], "name": res["name"], "direction": direction,
                "level": best, "new": bool(new_tfs), "triggers": triggers, "new_triggers": new_tfs,
                "trade": res["trade_bull" if direction == "bull" else "trade_bear"],
                "price": res["price"],
                "timeframes": {tf: _tf_summary(r, direction, tf) for tf, r in tfs.items()},
            })
    cleared = []
    for key, prior in previous.items():
        if key in active:
            continue
        try:
            age = (now - dt.datetime.fromisoformat(prior["last_seen"])).total_seconds() / 60
        except (KeyError, ValueError, TypeError):
            age = math.inf
        if age < clear_after:
            active[key] = prior          # cooling down; keep so a re-trigger isn't "new"
        else:
            cleared.append(key)
    alerts.sort(key=lambda a: (-LEVEL_RANK[a["level"]], not a["new"], -len(a["triggers"]), a["ticker"]))
    scan["alerts"] = alerts
    scan["cleared"] = sorted(cleared)
    if use_state:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps({"updated": stamp, "active": active}, indent=2))


# =========================================================================
# Output
# =========================================================================

def _fmt(v, nd=1):
    return "n/a" if v is None else f"{v:.{nd}f}"


def _macd_text(mac: dict | None, direction: str, short: bool = False) -> str:
    mac = mac or {}
    arrow = ("↑" if direction == "bull" else "↓") if short else ("up" if direction == "bull" else "down")
    if mac.get("state") == "crossed":
        return f"{arrow} crossed {mac['bars_since_cross']} bar(s) ago" if not short else f"{arrow} crossed {mac['bars_since_cross']}b ago"
    if mac.get("state") == "approaching":
        return f"crossing {arrow} in ~{mac['bars_to_cross']} bars" if not short else f"{arrow} in ~{mac['bars_to_cross']}b"
    return {"diverging": "moving away", "already_crossed": "crossed earlier",
            "insufficient_data": "n/a"}.get(mac.get("state"), str(mac.get("state", "n/a")))


def _band_text(t: dict, bull: bool) -> str:
    i = t["indicators"]
    if not t["conditions"].get("bollinger"):
        return f"inside (lower {_fmt(i.get('bb_lower'), 2)})" if bull else f"inside (upper {_fmt(i.get('bb_upper'), 2)})"
    return f"below lower {_fmt(i.get('bb_lower'), 2)}" if bull else f"above upper {_fmt(i.get('bb_upper'), 2)}"


def _lean_text(t: dict) -> str:
    ln = t.get("lean") or {}
    if ln.get("side") == "upside":
        return f"▲ upside {ln['bull_met']}/{ln['enabled']}"
    if ln.get("side") == "downside":
        return f"▼ downside {ln['bear_met']}/{ln['enabled']}"
    return "neutral" if ln else ""


def _status(t: dict) -> str:
    return f"{t['level'] or '-'} {t['met']}/{t['enabled']}"


def _bar_label(t: dict) -> str:
    start = dt.datetime.fromisoformat(t["last_bar"]).astimezone(ET)
    minutes = TIMEFRAMES.get(t.get("timeframe", ""), (None, 0))[1]
    if minutes >= 1440:
        label = start.strftime("%b %d")
    else:
        label = start.strftime("%H:%M")
        end = (start + dt.timedelta(minutes=minutes)).time()
        if end <= SESSION_OPEN and start.time() < SESSION_OPEN:
            label += " pre-mkt"
        elif start.time() >= SESSION_CLOSE:
            label += " after-hrs"
    return label + (" live" if t.get("live") else "")


def alert_message(a: dict, cfg: dict, timeframes: list[str]) -> str:
    bull = a["direction"] == "bull"
    rsi_lens = [int(x) for x in cfg["indicators"]["rsi"]["lengths"]]
    trig = ", ".join(f"{tf}{' (new)' if tf in a['new_triggers'] else ''}" for tf in timeframes if tf in a["triggers"])
    head = (f"{':chart_with_upwards_trend:' if bull else ':chart_with_downwards_trend:'} "
            f"**New {'BULLISH' if bull else 'BEARISH'} {a['level']}: {a['ticker']}** "
            f"({a['name']}) · ${_fmt(a['price'], 2)} · triggered on {trig}")
    kind = "Leveraged" if bull else "Inverse leveraged"
    funds = ", ".join(describe_etf(e) for e in a["trade"]) or "none mapped"
    rows = ["| TF | Status | Lean | RSI " + "/".join(map(str, rsi_lens)) + " | StochRSI K/D | MACD | Bollinger | Bar |",
            "|---|---|---|---|---|---|---|---|"]
    for tf in timeframes:
        t = a["timeframes"].get(tf)
        if not t:
            rows.append(f"| {tf} | no data | | | | | | |")
            continue
        i = t["indicators"]
        mark = " ★" if tf in a["triggers"] else ""
        rows.append(
            f"| {tf}{mark} | {_status(t)} | {_lean_text(t)} | "
            + " / ".join(_fmt(i.get(f"rsi{n}")) for n in rsi_lens)
            + f" | {_fmt(i.get('stoch_k'))} / {_fmt(i.get('stoch_d'))} | {_macd_text(t['macd'], a['direction'], True)} "
            f"| {_band_text(t, bull)} | {_bar_label(t)} |")
    return "\n".join([head, f"{kind}: **{funds}**", "", *rows, "",
                      "_Screening alert from the ETF signal scanner, not financial advice._"])


def build_notification(scan: dict, cfg: dict) -> dict | None:
    """Slack messages (one per ticker) for alerts with a new trigger at slack.levels."""
    slack = cfg.get("slack") or {}
    if not slack.get("enabled") or not slack.get("channel_id"):
        return None
    levels = set(slack.get("levels") or ["SIGNAL"])
    fresh = [a for a in scan.get("alerts", [])
             if any(a["triggers"][tf] in levels for tf in a["new_triggers"])]
    if not fresh:
        return None
    return {"channel_id": slack["channel_id"], "channel_name": slack.get("channel_name"),
            "count": len(fresh),
            "messages": [alert_message(a, cfg, scan["timeframes"]) for a in fresh]}


def format_text(scan: dict, cfg: dict, show_all: bool) -> str:
    ind, rules = cfg["indicators"], cfg["rules"]
    rsi_lens = [int(x) for x in ind["rsi"]["lengths"]]
    s, m, b = ind["stochrsi"], ind["macd"], ind["bollinger"]
    tfs = scan["timeframes"]
    lines = [
        f"ETF Signal Scan  {scan['scanned_at']}  |  timeframes {', '.join(tfs)}  |  "
        f"{scan['tickers_scanned']} scanned, {len(scan['errors'])} errors",
        f"Rules: RSI({','.join(map(str, rsi_lens))}) <{rules['rsi']['oversold']}/>{rules['rsi']['overbought']} "
        f"[{rules['rsi'].get('require', 'all')}]  StochRSI({s['smooth_k']},{s['smooth_d']},{s['rsi_length']},{s['stoch_length']}) "
        f"<{rules['stochrsi']['oversold']}/>{rules['stochrsi']['overbought']} [{rules['stochrsi'].get('line', 'both')}]  "
        f"MACD({m['fast']},{m['slow']},{m['signal']})  BB({b['length']},{b['stddev']}) [{rules['bollinger'].get('mode', 'touch')}]  "
        f"window={rules.get('window_bars', 3)} bars",
        "",
    ]
    for direction, title in (("bull", "BULLISH  (oversold, MACD turning up, lower-band pierce)"),
                             ("bear", "BEARISH  (overbought, MACD turning down, upper-band pierce)")):
        lines.append(f"== {title}")
        rows = [a for a in scan["alerts"] if a["direction"] == direction]
        if not rows:
            lines.append("   (none)")
        for a in rows:
            trig = ",".join(f"{tf}{'*' if tf in a['new_triggers'] else ''}" for tf in tfs if tf in a["triggers"])
            lines.append(f" {'NEW' if a['new'] else '   '} {a['level']:<6} {a['ticker']:<5} {_fmt(a['price'], 2):>8}"
                         f"  on {trig}   {a['name']}")
            funds = ", ".join(describe_etf(e) for e in a["trade"]) or "(none mapped)"
            lines.append(f"        {'Leveraged' if direction == 'bull' else 'Inverse leveraged'}: {funds}")
            for tf in tfs:
                t = a["timeframes"].get(tf)
                if not t:
                    continue
                i = t["indicators"]
                lines.append(
                    f"        {tf:<4}{'*' if tf in a['triggers'] else ' '} {_status(t):<10} {_lean_text(t):<14}"
                    + " ".join(f"RSI{n} {_fmt(i.get(f'rsi{n}')):>5}" for n in rsi_lens)
                    + f"  K/D {_fmt(i.get('stoch_k')):>5}/{_fmt(i.get('stoch_d')):<5}"
                    f"  MACD {_macd_text(t['macd'], direction, True):<14} BB {_band_text(t, direction == 'bull'):<22} {_bar_label(t)}")
        lines.append("")
    if scan.get("cleared"):
        lines.append("Cleared since last scan: " + ", ".join(scan["cleared"]))
    if scan["errors"]:
        lines.append("Errors: " + "; ".join(
            f"{e['ticker']}{'/' + e['timeframe'] if e.get('timeframe') else ''}: {e['error']}" for e in scan["errors"]))
    if show_all and scan["results"]:
        lines += ["", "All tickers:",
                  f"  {'TICKER':<6}{'TF':<4}{'PRICE':>9} " + " ".join(f"{'RSI' + str(n):>6}" for n in rsi_lens)
                  + f"{'K':>7}{'D':>7}{'HIST':>9}{'%B':>7}  BULL  BEAR"]
        for r in scan["results"]:
            for tf, res in r["timeframes"].items():
                i = res["indicators"]
                pct_b = None
                if None not in (i.get("bb_upper"), i.get("bb_lower"), i.get("close")) and i["bb_upper"] != i["bb_lower"]:
                    pct_b = (i["close"] - i["bb_lower"]) / (i["bb_upper"] - i["bb_lower"])
                lines.append(
                    f"  {r['ticker']:<6}{tf:<4}{_fmt(res['price'], 2):>9} "
                    + " ".join(f"{_fmt(i.get(f'rsi{n}')):>6}" for n in rsi_lens)
                    + f"{_fmt(i.get('stoch_k')):>7}{_fmt(i.get('stoch_d')):>7}"
                    f"{_fmt(i.get('macd_hist'), 3):>9}{_fmt(pct_b, 2):>7}"
                    f"  {res['bull']['met']}/{res['bull']['enabled']}   {res['bear']['met']}/{res['bear']['enabled']}")
    return "\n".join(lines).rstrip() + "\n"


# =========================================================================
# CLI
# =========================================================================

def load_config(path: Path) -> dict:
    with open(path) as fh:
        return yaml.safe_load(fh)


def seconds_until_next_run(now: dt.datetime, interval_min: int, align: bool, delay: int) -> float:
    if not align:
        return interval_min * 60
    now_et = now.astimezone(ET)
    midnight = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = (now_et - midnight).total_seconds()
    step = interval_min * 60
    nxt = (math.floor((elapsed - delay) / step) + 1) * step + delay
    return max(1.0, nxt - elapsed)


def next_session_start(now: dt.datetime, extended: bool) -> dt.datetime:
    """Next weekday session open after `now` (exchange holidays not detected)."""
    open_ = EXT_OPEN if extended else SESSION_OPEN
    now = now.astimezone(ET)
    day = now.date()
    while True:
        start = dt.datetime.combine(day, open_, tzinfo=ET)
        if start > now and start.weekday() < 5:
            return start
        day += dt.timedelta(days=1)


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def update_status(now: dt.datetime, cfg: dict, scan: dict | None) -> list[str]:
    """Track whether the scanner is running and return Slack status messages:
    one when it stops for the day (first skipped scan after a session), one
    when most tickers can't be fetched, and one when data comes back.
    `scan` is None when the scan was skipped because the market is closed."""
    extended = bool(cfg["data"].get("include_prepost"))
    st = _read_json(STATUS_FILE) or {"running": False, "outage": False}
    msgs: list[str] = []
    now_et = now.astimezone(ET)
    if scan is None:
        if st.get("running"):
            nxt = next_session_start(now_et, extended)
            hours = "04:00-20:00 ET" if extended else "09:30-16:00 ET"
            last = st.get("last_scan")
            last_txt = dt.datetime.fromisoformat(last).astimezone(ET).strftime("%H:%M") if last else "n/a"
            msgs.append(
                f":octagonal_sign: **ETF scanner stopped for the day** · {now_et:%a %b %d %H:%M} ET, "
                f"outside scan hours ({hours}). Last scan {last_txt} ET. "
                f"Next scans start {nxt:%a %b %d %H:%M} ET (exchange holidays aren't detected).")
            st.update(running=False, stopped_at=now_et.isoformat(timespec="seconds"))
    else:
        failures = sum(1 for e in scan["errors"] if not e.get("timeframe"))
        total = scan["tickers_scanned"] + failures
        failing = total > 0 and failures / total >= 0.5
        if failing and not st.get("outage"):
            first = next(e for e in scan["errors"] if not e.get("timeframe"))
            msgs.append(
                f":warning: **ETF scanner isn't getting market data** · {now_et:%H:%M} ET: "
                f"{failures} of {total} ETFs failed to download (e.g. {first['ticker']}: {first['error'][:150]}). "
                "Signals can't fire until this clears; it keeps retrying every scan and will post here when data is back.")
            st["outage"] = True
        elif not failing and st.get("outage"):
            msgs.append(f":white_check_mark: **ETF scanner market data is back** · {now_et:%H:%M} ET, "
                        f"{scan['tickers_scanned']} of {total} ETFs scanned.")
            st["outage"] = False
        st.update(running=True, last_scan=scan["scanned_at"])
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.write_text(json.dumps(st, indent=2))
    slack = cfg.get("slack") or {}
    return msgs if slack.get("enabled") and slack.get("status_messages", True) else []


def combine_notify(cfg: dict, status_msgs: list[str], signals: dict | None) -> dict | None:
    msgs = status_msgs + ((signals or {}).get("messages") or [])
    slack = cfg.get("slack") or {}
    if not msgs or not slack.get("channel_id"):
        return None
    return {"channel_id": slack["channel_id"], "channel_name": slack.get("channel_name"),
            "count": len(msgs), "status_count": len(status_msgs), "messages": msgs}


def scan_once(cfg: dict, args) -> dict:
    now = dt.datetime.now(tz=ET)
    extended = bool(cfg["data"].get("include_prepost"))
    gated = cfg.get("market_hours_only", True) and not args.force
    track = gated and not args.no_state   # forced / stateless runs don't change running status
    if gated and not market_is_open(now, extended):
        msgs = update_status(now, cfg, None) if track else []
        return {"status": "market_closed",
                "message": "Outside scan hours; skipped scan (use --force to override).",
                "notify": combine_notify(cfg, msgs, None)}
    only = {t.strip().upper() for t in args.tickers.split(",")} if args.tickers else None
    scan = run_scan(cfg, only, now)
    apply_state(scan, cfg, use_state=not args.no_state, now=now)
    msgs = update_status(now, cfg, scan) if track else []
    scan["notify"] = combine_notify(cfg, msgs, build_notification(scan, cfg))
    return scan


# ---------------------------------------------------------------- direct Slack posting
# Used when the scanner runs on its own (e.g. a Windows scheduled task) instead
# of through the Claude skill, which posts via the Slack connector.

def webhook_url() -> str | None:
    url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not url and WEBHOOK_FILE.exists():
        url = WEBHOOK_FILE.read_text().strip()
    return url or None


def to_slack_mrkdwn(md: str) -> str:
    """Convert the alert markdown to Slack's mrkdwn: **bold** -> *bold*, and
    markdown tables -> an aligned monospace block (webhooks don't render tables)."""
    out: list[str] = []
    table: list[str] = []

    def flush():
        if not table:
            return
        rows = [[c.strip() for c in r.strip().strip("|").split("|")] for r in table
                if not set(r.replace("|", "").strip()) <= set("-: ")]
        ncol = max(len(r) for r in rows)
        widths = [max(len(r[i]) if i < len(r) else 0 for r in rows) for i in range(ncol)]
        lines = ["  ".join(c.ljust(widths[i]) for i, c in enumerate(r)).rstrip() for r in rows]
        out.append("```\n" + "\n".join(lines) + "\n```")
        table.clear()

    for line in md.splitlines():
        if line.lstrip().startswith("|"):
            table.append(line)
            continue
        flush()
        out.append(re.sub(r"\*\*(.+?)\*\*", r"*\1*", line))
    flush()
    return "\n".join(out)


def post_to_slack(messages: list[str], url: str) -> list[str]:
    """Post each message to a Slack incoming webhook; return error strings."""
    import requests
    errors = []
    for m in messages:
        try:
            r = requests.post(url, json={"text": to_slack_mrkdwn(m)}, timeout=20)
            if r.status_code != 200:
                errors.append(f"HTTP {r.status_code}: {r.text[:120]}")
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
    return errors


def deliver(scan: dict, args) -> None:
    if not getattr(args, "post_slack", False):
        return
    msgs = (scan.get("notify") or {}).get("messages") or []
    if not msgs:
        return
    url = webhook_url()
    if not url:
        print("--post-slack: no webhook URL (set SLACK_WEBHOOK_URL or create "
              f"{WEBHOOK_FILE.name}); {len(msgs)} message(s) not sent", file=sys.stderr, flush=True)
        return
    errors = post_to_slack(msgs, url)
    scan["posted"] = len(msgs) - len(errors)
    for e in errors:
        print(f"Slack post failed: {e}", file=sys.stderr, flush=True)


def lifecycle_message(cfg: dict, event: str, interval: int, paused: bool = False) -> str | None:
    slack = cfg.get("slack") or {}
    if not (slack.get("enabled") and slack.get("status_messages", True)):
        return None
    now = dt.datetime.now(tz=ET)
    host = socket.gethostname()
    hours = "04:00-20:00 ET" if cfg["data"].get("include_prepost") else "09:30-16:00 ET"
    if event == "start" and paused:
        return (f":double_vertical_bar: **ETF scanner started, but paused** on {host} · {now:%a %b %d %H:%M} ET · "
                "it was paused from the dashboard; press Start there to resume scanning.")
    if event == "start":
        return (f":arrow_forward: **ETF scanner started** on {host} · {now:%a %b %d %H:%M} ET · "
                f"scanning every {interval} min during {hours}.")
    if event == "pause":
        return (f":double_vertical_bar: **ETF scanner paused** on {host} · {now:%a %b %d %H:%M} ET, "
                "from the dashboard. No scans or alerts until it's started again.")
    if event == "resume":
        return (f":arrow_forward: **ETF scanner resumed** on {host} · {now:%a %b %d %H:%M} ET, "
                f"from the dashboard · scanning every {interval} min during {hours}.")
    return (f":octagonal_sign: **ETF scanner stopped** on {host} · {now:%a %b %d %H:%M} ET "
            "(the program was closed). No scans or alerts until it starts again.")


def emit(scan: dict, cfg: dict, args) -> None:
    deliver(scan, args)
    if scan.get("status") == "market_closed":
        if args.json:
            print(json.dumps(scan), flush=True)
        else:
            print(scan["message"], flush=True)
            for m in (scan.get("notify") or {}).get("messages", []):
                print(m, flush=True)
        return
    if args.json:
        if not args.all:
            scan = {k: v for k, v in scan.items() if k != "results"}
        print(json.dumps(scan, indent=2, default=str), flush=True)
    else:
        print(format_text(scan, cfg, args.all), flush=True)


# ---------------------------------------------------------------- dashboard link
# The --loop scanner and the desktop dashboard (dashboard.py) talk through two
# small JSON files in state/: the dashboard writes control.json (pause, scan
# now) and the scanner writes dashboard.json (heartbeat, last check, results).

POLL_SECONDS = 5          # how often --loop checks control.json between scans
HEARTBEAT_SECONDS = 30    # how often --loop refreshes dashboard.json while idle


def write_json_atomic(path: Path, data: dict) -> None:
    """Write via a temp file + rename so a reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    for _ in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:     # Windows: the reader has it open this instant
            time.sleep(0.05)
    tmp.unlink(missing_ok=True)


def read_control() -> dict:
    c = _read_json(CONTROL_FILE)
    return {"paused": bool(c.get("paused", False)), "scan_now": c.get("scan_now")}


def scan_summary(scan: dict, forced: bool = False) -> dict:
    """The parts of a scan the dashboard shows: per-ticker, per-timeframe
    level/lean, the alerts and the errors."""
    alerts: dict[str, list] = {}
    for a in scan.get("alerts", []):
        alerts.setdefault(a["ticker"], []).append({
            "direction": a["direction"], "level": a["level"], "new": a["new"],
            "triggers": a["triggers"], "funds": [describe_etf(e) for e in a["trade"]]})
    rows = []
    for r in scan["results"]:
        tfs = {}
        for tf, res in r["timeframes"].items():
            tfs[tf] = {"bull": res["bull"]["level"], "bull_met": res["bull"]["met"],
                       "bear": res["bear"]["level"], "bear_met": res["bear"]["met"],
                       "enabled": res["bull"]["enabled"], "lean": lean(res)["side"],
                       "live": bool(res.get("live"))}
        rows.append({"ticker": r["ticker"], "name": r.get("name", r["ticker"]), "price": r.get("price"),
                     "trade_bull": [describe_etf(e) for e in r.get("trade_bull", [])],
                     "trade_bear": [describe_etf(e) for e in r.get("trade_bear", [])],
                     "timeframes": tfs, "alerts": alerts.get(r["ticker"], [])})
    return {"scanned_at": scan["scanned_at"], "forced": forced, "timeframes": scan["timeframes"],
            "session": scan.get("session"), "tickers_scanned": scan["tickers_scanned"],
            "alert_count": len(scan.get("alerts", [])),
            "signal_count": sum(1 for a in scan.get("alerts", []) if a["level"] == "SIGNAL"),
            "posted": scan.get("posted", 0),
            "errors": scan["errors"][:50], "error_count": len(scan["errors"]), "rows": rows}


class LoopStatus:
    """dashboard.json, kept up to date by the --loop scanner."""

    def __init__(self, cfg: dict, interval: int, paused: bool):
        previous = _read_json(DASHBOARD_FILE)
        now = dt.datetime.now(tz=ET).isoformat(timespec="seconds")
        self.data = {
            "pid": os.getpid(), "host": socket.gethostname(), "started_at": now,
            "interval_minutes": interval,
            "hours": "04:00-20:00 ET" if cfg["data"].get("include_prepost") else "09:30-16:00 ET",
            "slack_webhook": bool(webhook_url()), "paused": paused, "state": "idle",
            "last_check": None, "last_result": None, "message": None, "next_check": None,
            "last_scan": previous.get("last_scan"),       # keep showing results across restarts
            "scan_now_handled": None, "stopped_at": None,
        }
        self.saved = 0.0

    def save(self, **changes) -> None:
        self.data.update(changes)
        self.data["heartbeat"] = dt.datetime.now(tz=ET).isoformat(timespec="seconds")
        try:
            write_json_atomic(DASHBOARD_FILE, self.data)
        except OSError as exc:
            print(f"Couldn't update {DASHBOARD_FILE.name}: {exc}", file=sys.stderr, flush=True)
        self.saved = time.monotonic()


def run_loop(cfg: dict, args) -> int:
    interval = int(args.interval or cfg.get("interval_minutes", 15))
    align = bool(cfg.get("align_to_bar_close", True))
    delay = int(cfg.get("post_close_delay_seconds", 20))
    print(f"Looping every {interval} min (Ctrl-C to stop)", file=sys.stderr, flush=True)
    url = webhook_url() if args.post_slack else None

    def slack(event: str, paused: bool = False) -> None:
        msg = lifecycle_message(cfg, event, interval, paused)
        if url and msg:
            for err in post_to_slack([msg], url):
                print(f"Slack post failed: {err}", file=sys.stderr, flush=True)

    def stamp() -> str:
        return dt.datetime.now(tz=ET).isoformat(timespec="seconds")

    control = read_control()
    paused = control["paused"]
    handled = control["scan_now"]          # ignore requests made before this start
    status = LoopStatus(cfg, interval, paused)
    status.save(scan_now_handled=handled)
    slack("start", paused)
    if paused:
        print(f"{dt.datetime.now(tz=ET):%Y-%m-%d %H:%M} Paused from the dashboard; not scanning.",
              file=sys.stderr, flush=True)

    def check(force: bool = False) -> None:
        run_args = argparse.Namespace(**{**vars(args), "force": True}) if force else args
        status.save(state="scanning", scan_started=stamp())
        try:
            scan = scan_once(cfg, run_args)
            emit(scan, cfg, args)
            if scan.get("status") == "market_closed":
                status.save(state="idle", last_check=stamp(), last_result="market_closed",
                            message="Outside scan hours; skipped.")
            else:
                s = scan_summary(scan, forced=force)
                status.save(state="idle", last_check=stamp(), last_result="scanned", last_scan=s,
                            message=f"{s['tickers_scanned']} ETFs scanned, {s['signal_count']} signal(s), "
                                    f"{s['error_count']} error(s)" + (" (Scan now)" if force else ""))
        except Exception as exc:
            print(f"{dt.datetime.now(tz=ET):%Y-%m-%d %H:%M} scan failed: {exc}", file=sys.stderr, flush=True)
            status.save(state="idle", last_check=stamp(), last_result="failed", message=str(exc)[:300])

    next_run = time.time()                  # first check straight away
    try:
        while True:
            control = read_control()
            if control["paused"] != paused:
                paused = control["paused"]
                print(f"{dt.datetime.now(tz=ET):%Y-%m-%d %H:%M} "
                      f"{'Paused' if paused else 'Resumed'} from the dashboard.", file=sys.stderr, flush=True)
                slack("pause" if paused else "resume")
                status.save(paused=paused)
                if not paused:
                    next_run = time.time()   # resume -> check right away
            if control["scan_now"] and control["scan_now"] != handled:
                handled = control["scan_now"]
                print(f"{dt.datetime.now(tz=ET):%Y-%m-%d %H:%M} Scan requested from the dashboard.",
                      file=sys.stderr, flush=True)
                check(force=True)
                status.save(scan_now_handled=handled)
            if time.time() >= next_run:
                if paused:
                    status.save(last_check=stamp(), last_result="paused",
                                message="Paused from the dashboard; skipped.")
                else:
                    check()
                next_run = time.time() + seconds_until_next_run(dt.datetime.now(tz=ET), interval, align, delay)
                status.save(next_check=dt.datetime.fromtimestamp(next_run, tz=ET).isoformat(timespec="seconds"))
            elif time.monotonic() - status.saved >= HEARTBEAT_SECONDS:
                status.save()
            time.sleep(max(0.2, min(POLL_SECONDS, next_run - time.time())))
    except KeyboardInterrupt:
        status.save(state="stopped", stopped_at=stamp())
        slack("stop")
        return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--all", action="store_true", help="include every scanned ticker, not just alerts")
    p.add_argument("--force", action="store_true", help="scan even when the market is closed")
    p.add_argument("--tickers", help="comma-separated subset / ad-hoc tickers")
    p.add_argument("--timeframes", "--timeframe", dest="timeframes",
                   help="override data.timeframes, comma-separated (e.g. 1h,4h,1d)")
    p.add_argument("--source", help="override data.source (yfinance | yahoo | csv)")
    p.add_argument("--loop", action="store_true", help="keep scanning every interval")
    p.add_argument("--interval", type=int, help="override interval_minutes for --loop")
    p.add_argument("--no-state", action="store_true", help="don't read/write new-vs-ongoing state")
    p.add_argument("--post-slack", action="store_true",
                   help="post alerts/status straight to a Slack incoming webhook "
                        "(SLACK_WEBHOOK_URL or slack_webhook_url.txt) instead of via Claude")
    p.add_argument("--test-slack", action="store_true", help="post one test message via the webhook and exit")
    p.add_argument("--announce", choices=["start", "stop"],
                   help="post the started/stopped status message via the webhook and exit")
    args = p.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):   # Windows consoles default to cp1252
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    cfg = load_config(args.config)
    if args.timeframes:
        cfg["data"]["timeframes"] = args.timeframes
    if args.source:
        cfg["data"]["source"] = args.source

    if args.test_slack:
        url = webhook_url()
        if not url:
            print(f"No webhook URL: set SLACK_WEBHOOK_URL or create {WEBHOOK_FILE}", file=sys.stderr)
            return 2
        errs = post_to_slack([f":white_check_mark: **ETF scanner test** from {socket.gethostname()} · "
                              f"{dt.datetime.now(tz=ET):%H:%M} ET. Slack posting works."], url)
        print("\n".join(errs) or "Test message posted.", file=sys.stderr if errs else sys.stdout)
        return 1 if errs else 0

    if args.announce:
        url, msg = webhook_url(), lifecycle_message(cfg, args.announce, int(cfg.get("interval_minutes", 15)))
        if url and msg:
            errs = post_to_slack([msg], url)
            print("\n".join(errs) or "Posted.", file=sys.stderr if errs else sys.stdout)
            return 1 if errs else 0
        return 0

    if not args.loop:
        emit(scan_once(cfg, args), cfg, args)
        return 0

    return run_loop(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
