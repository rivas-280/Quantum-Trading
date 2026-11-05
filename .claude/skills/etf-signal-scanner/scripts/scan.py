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
ET = ZoneInfo("America/New_York")
SESSION_OPEN = dt.time(9, 30)
SESSION_CLOSE = dt.time(16, 0)

# timeframe -> (interval to download, minutes per bar, resample from download?)
TIMEFRAMES = {
    "1m": ("1m", 1, False), "2m": ("2m", 2, False), "5m": ("5m", 5, False),
    "15m": ("15m", 15, False), "30m": ("30m", 30, False),
    "1h": ("60m", 60, False), "60m": ("60m", 60, False),
    "2h": ("60m", 120, True), "3h": ("60m", 180, True), "4h": ("60m", 240, True),
    "1d": ("1d", 1440, False),
}
# history to download per interval (valid for both Yahoo and yfinance)
AUTO_RANGE = {"1m": "5d", "2m": "1mo", "5m": "1mo", "15m": "1mo", "30m": "1mo",
              "60m": "1y", "1d": "2y"}

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


def resample_session(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Group bars into `minutes`-long bars anchored at the 09:30 ET open each
    day (so 4h = 09:30-13:30 and 13:30-16:00, like TradingView), or into one
    bar per day when minutes >= 1440."""
    if df.empty:
        return df
    day = df.index.normalize()
    if minutes >= 1440:
        key = day
    else:
        since_open = (df.index.hour * 60 + df.index.minute) - (SESSION_OPEN.hour * 60 + SESSION_OPEN.minute)
        bucket = np.floor_divide(np.asarray(since_open), minutes)
        offset = SESSION_OPEN.hour * 60 + SESSION_OPEN.minute + bucket * minutes
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


def bar_end(start: pd.Timestamp, timeframe: str) -> dt.datetime:
    minutes = TIMEFRAMES[timeframe][1]
    close = dt.datetime.combine(start.date(), SESSION_CLOSE, tzinfo=ET)
    if minutes >= 1440:
        return close
    end = start.to_pydatetime() + dt.timedelta(minutes=minutes)
    # session-anchored bars stop at the close (e.g. the 15:30 1h bar, 13:30 4h bar)
    return min(end, close) if start.time() < SESSION_CLOSE else end


def drop_forming_bar(df: pd.DataFrame, timeframe: str, now: dt.datetime) -> pd.DataFrame:
    if df.empty:
        return df
    return df.iloc[:-1] if now < bar_end(df.index[-1], timeframe) else df


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

def market_is_open(now: dt.datetime) -> bool:
    now = now.astimezone(ET)
    if now.weekday() >= 5:
        return False
    return SESSION_OPEN <= now.time() <= dt.time(16, 5)


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
    if source == "csv":
        base = clean_bars(fetch_csv(ticker, (SKILL_DIR / data_cfg.get("csv_dir", "./data")).resolve()))
        return {tf: resample_session(base, TIMEFRAMES[tf][1]) for tf in timeframes}
    downloads: dict[str, pd.DataFrame] = {}
    out = {}
    for tf in timeframes:
        interval, minutes, derived = TIMEFRAMES[tf]
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
        out[tf] = resample_session(bars, minutes) if derived else bars
    return out


def run_scan(cfg: dict, only: set[str] | None, now: dt.datetime) -> dict:
    data_cfg = cfg["data"]
    timeframes = configured_timeframes(data_cfg)
    need = min_bars_needed(cfg["indicators"])
    closed_only = bool(data_cfg.get("closed_bars_only", False))
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
                    bars = drop_forming_bar(bars, tf, now)
                if bars is None or len(bars) < need:
                    raise RuntimeError(f"only {0 if bars is None else len(bars)} {tf} bars, need {need}")
                check_price_jumps(bars, float(data_cfg.get("max_bar_change", 0.4)))
                res = analyze(bars, cfg)
                res["live"] = bool(now < bar_end(bars.index[-1], tf))
                tf_results[tf] = res
                price = res["price"] if price is None or TIMEFRAMES[tf][1] < 1440 else price
            except Exception as exc:
                errors.append({"ticker": t, "timeframe": tf, "error": f"{type(exc).__name__}: {exc}"})
        if tf_results:
            results.append({**target, "price": price, "timeframes": tf_results})

    return {
        "scanned_at": now.astimezone(ET).isoformat(timespec="seconds"),
        "timeframes": timeframes,
        "source": data_cfg.get("source", "yfinance"),
        "tickers_scanned": len(results),
        "errors": errors,
        "results": results,
    }


# =========================================================================
# Alerts / state
# =========================================================================

LEVEL_RANK = {"SIGNAL": 2, "WATCH": 1, None: 0}


def _tf_summary(res: dict, direction: str, tf: str) -> dict:
    ev = res[direction]
    return {"timeframe": tf, "level": ev["level"], "met": ev["met"], "enabled": ev["enabled"],
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


def _status(t: dict) -> str:
    return f"{t['level'] or '-'} {t['met']}/{t['enabled']}"


def _bar_label(t: dict) -> str:
    start = dt.datetime.fromisoformat(t["last_bar"]).astimezone(ET)
    daily = TIMEFRAMES.get(t.get("timeframe", ""), (None, 0))[1] >= 1440
    label = start.strftime("%b %d") if daily else start.strftime("%H:%M")
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
    rows = ["| TF | Status | RSI " + "/".join(map(str, rsi_lens)) + " | StochRSI K/D | MACD | Bollinger | Bar |",
            "|---|---|---|---|---|---|---|"]
    for tf in timeframes:
        t = a["timeframes"].get(tf)
        if not t:
            rows.append(f"| {tf} | no data | | | | | |")
            continue
        i = t["indicators"]
        mark = " ★" if tf in a["triggers"] else ""
        rows.append(
            f"| {tf}{mark} | {_status(t)} | "
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
                    f"        {tf:<4}{'*' if tf in a['triggers'] else ' '} {_status(t):<10} "
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


def scan_once(cfg: dict, args) -> dict | None:
    now = dt.datetime.now(tz=ET)
    if cfg.get("market_hours_only", True) and not args.force and not market_is_open(now):
        return None
    only = {t.strip().upper() for t in args.tickers.split(",")} if args.tickers else None
    scan = run_scan(cfg, only, now)
    apply_state(scan, cfg, use_state=not args.no_state, now=now)
    scan["notify"] = build_notification(scan, cfg)
    return scan


def emit(scan: dict | None, cfg: dict, args) -> None:
    if scan is None:
        msg = {"status": "market_closed",
               "message": "US market is closed; skipped scan (use --force to override)."}
        print(json.dumps(msg) if args.json else msg["message"], flush=True)
        return
    if args.json:
        if not args.all:
            scan = {k: v for k, v in scan.items() if k != "results"}
        print(json.dumps(scan, indent=2, default=str), flush=True)
    else:
        print(format_text(scan, cfg, args.all), flush=True)


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
    args = p.parse_args(argv)

    cfg = load_config(args.config)
    if args.timeframes:
        cfg["data"]["timeframes"] = args.timeframes
    if args.source:
        cfg["data"]["source"] = args.source

    if not args.loop:
        emit(scan_once(cfg, args), cfg, args)
        return 0

    interval = int(args.interval or cfg.get("interval_minutes", 15))
    align = bool(cfg.get("align_to_bar_close", True))
    delay = int(cfg.get("post_close_delay_seconds", 20))
    print(f"Looping every {interval} min (Ctrl-C to stop)", file=sys.stderr, flush=True)
    try:
        while True:
            try:
                emit(scan_once(cfg, args), cfg, args)
            except Exception as exc:
                print(f"scan failed: {exc}", file=sys.stderr, flush=True)
            time.sleep(seconds_until_next_run(dt.datetime.now(tz=ET), interval, align, delay))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
