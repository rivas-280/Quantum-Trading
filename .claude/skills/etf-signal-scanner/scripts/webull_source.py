"""Webull OpenAPI candles for the scanner, via Webull's official Python SDK
(pip package webull-openapi-python-sdk).

Needs, all from your Webull account:
  * approved OpenAPI access and an App Key + App Secret, saved in
    ../webull_credentials.yaml (git-ignored), and
  * an active OpenAPI market-data subscription for US stock/ETF quotes
    (separate from any Webull app / desktop subscription).

Webull also issues a login token that you approve in the Webull app; it's
stored in ../state/webull_token/ and has to be approved again when it
expires (about every two weeks). `scan.py --webull-login` (or the dashboard's
"Connect Webull" button) requests one and waits while you approve it.

The SDK logs full request headers, including the session token, to the
console and to a webull_data_sdk.log file by default; both are switched off
here.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import re
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yaml

SKILL_DIR = Path(__file__).resolve().parent.parent
CREDENTIALS_FILE = SKILL_DIR / "webull_credentials.yaml"
TOKEN_DIR = SKILL_DIR / "state" / "webull_token"
ET = ZoneInfo("America/New_York")

# scanner timeframe -> Webull Timespan (Webull's own candles, as in the Webull app)
TIMESPANS = {"1m": "M1", "5m": "M5", "15m": "M15", "30m": "M30",
             "1h": "M60", "60m": "M60", "2h": "M120", "4h": "M240", "1d": "D"}
MAX_COUNT = 1200          # Webull's per-request limit
BATCH_SIZE = 20           # symbols per request
SCAN_TOKEN_WAIT = 20      # seconds a scan waits for a login approval before falling back
LOGIN_TOKEN_WAIT = 300    # seconds --webull-login waits for you to approve in the app

APPROVAL_HINT = ("approve the Webull OpenAPI login: press 'Connect Webull' in the ETF Scanner dashboard, "
                 "then accept the request in the Webull app")


class WebullError(RuntimeError):
    """Anything that stops Webull data: no keys, login not approved, not subscribed, HTTP errors."""


_client = None


def reset_client() -> None:
    """Forget the cached client, so the next request logs in again."""
    global _client
    _client = None


def load_credentials() -> dict:
    if not CREDENTIALS_FILE.exists():
        raise WebullError(f"no {CREDENTIALS_FILE.name}; add your Webull App Key and App Secret to it")
    try:
        creds = yaml.safe_load(CREDENTIALS_FILE.read_text()) or {}
    except yaml.YAMLError as exc:
        raise WebullError(f"{CREDENTIALS_FILE.name} isn't valid YAML: {exc}") from None
    key, secret = str(creds.get("app_key") or "").strip(), str(creds.get("app_secret") or "").strip()
    if not key or not secret:
        raise WebullError(f"App Key / App Secret missing in {CREDENTIALS_FILE.name}")
    return {"app_key": key, "app_secret": secret, "region": str(creds.get("region") or "us").strip(),
            "endpoint": str(creds.get("endpoint") or "api.webull.com").strip()}


def configured() -> bool:
    try:
        load_credentials()
        return True
    except WebullError:
        return False


def _quiet_sdk_logging() -> None:
    for name in ("webull", "webull.core", "webull.data"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.addHandler(logging.NullHandler())
        lg.setLevel(logging.CRITICAL + 1)
        lg.propagate = False


def describe_error(exc: BaseException) -> str:
    """A short, secret-free description of an SDK / HTTP error."""
    code = getattr(exc, "error_code", None) or getattr(exc, "get_error_code", lambda: None)()
    msg = getattr(exc, "error_msg", None) or getattr(exc, "get_error_msg", lambda: None)()
    text = f"{code}: {msg}" if code or msg else f"{type(exc).__name__}: {exc}"
    text = re.sub(r"(?i)(x-access-token|x-signature|token)[\"':=\s]+[\w+/=.-]{8,}", r"\1=<hidden>", text)
    text = text.split(" Request:")[0]
    if re.search(r"(?i)not verified|PENDING|token.*(invalid|expired)|INVALID|EXPIRED|unauthori[sz]ed|401", text):
        text += f" - {APPROVAL_HINT}"
    elif "NOT_SUBSCRIBED" in text.upper():
        text += (" - turn on the OpenAPI market-data subscription for US stock quotes in your Webull "
                 "account (it's separate from the app/desktop subscriptions)")
    return text[:400]


def client(token_wait: int = SCAN_TOKEN_WAIT):
    """A logged-in Webull DataClient (cached for the life of the process)."""
    global _client
    if _client is not None:
        return _client
    creds = load_credentials()
    _quiet_sdk_logging()
    try:
        from webull.core.client import ApiClient
        from webull.data.data_client import DataClient
    except ImportError:
        raise WebullError("the Webull SDK isn't installed: run windows\\install.ps1 again "
                          "(or pip install webull-openapi-python-sdk)") from None
    api = ApiClient(creds["app_key"], creds["app_secret"], creds["region"],
                    token_check_duration_seconds=max(5, int(token_wait)), token_check_interval_seconds=5)
    api.add_endpoint(creds["region"], creds["endpoint"])
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    api.set_token_dir(str(TOKEN_DIR))
    api._stream_logger_set = True        # stop DataClient adding its own console + file loggers
    api._file_logger_set = True
    try:
        _client = DataClient(api)        # logs in (uses / renews the approved token)
    except Exception as exc:
        raise WebullError(f"login failed: {describe_error(exc)}") from None
    return _client


# ---------------------------------------------------------------- response parsing
TIME_KEYS = ("time", "timestamp", "t", "date", "datetime", "trade_time", "start_time", "begin_time")


def _parse_time(v) -> pd.Timestamp | None:
    if v is None or v == "":
        return None
    try:
        if isinstance(v, (int, float)) or (isinstance(v, str) and re.fullmatch(r"-?\d+(\.\d+)?", v)):
            n = float(v)
            unit = "ms" if abs(n) > 1e11 else "s"
            return pd.Timestamp(n, unit=unit, tz="UTC")
        ts = pd.Timestamp(v)
        return ts.tz_localize("UTC") if ts.tzinfo is None else ts
    except (ValueError, TypeError, OverflowError):
        return None


def _num(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return math.nan
    return f


def bars_frame(rows: list[dict], daily: bool) -> pd.DataFrame:
    """Webull bar dicts -> DataFrame(open, high, low, close, volume) indexed by ET time."""
    recs = []
    for r in rows:
        tkey = next((k for k in TIME_KEYS if k in r), None)
        ts = _parse_time(r.get(tkey)) if tkey else None
        if ts is None:
            continue
        recs.append({"time": ts, "open": _num(r.get("open")), "high": _num(r.get("high")),
                     "low": _num(r.get("low")), "close": _num(r.get("close")),
                     "volume": _num(r.get("volume", r.get("vol", 0)))})
    if not recs:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame(recs)
    idx = pd.DatetimeIndex(df.pop("time")).tz_convert(ET)
    if daily:
        # a daily bar stamped 00:00 UTC lands on the previous evening in ET; keep its calendar date
        idx = pd.DatetimeIndex([(t + pd.Timedelta(days=1)) if t.hour >= 12 else t for t in idx]).normalize()
    df.index = idx
    df["volume"] = df["volume"].fillna(0.0)
    df = df.dropna(subset=["open", "high", "low", "close"])
    return df[~df.index.duplicated(keep="last")].sort_index()


def parse_bars_response(body, symbols: list[str], daily: bool) -> dict[str, pd.DataFrame]:
    """Find the candles per symbol in a Webull bars response. Tolerant of the
    common layouts: [{symbol, result: [bars]}], {"data": [...]}, {SYMBOL: [bars]},
    or a bare list of bars for a single symbol."""
    wanted = {s.upper() for s in symbols}
    found: dict[str | None, list[dict]] = {}

    def walk(node, symbol=None):
        if isinstance(node, dict):
            sym = node.get("symbol") or node.get("ticker") or symbol
            sym = sym.upper() if isinstance(sym, str) else sym
            if "open" in node and "close" in node:
                found.setdefault(sym, []).append(node)
                return
            for k, v in node.items():
                walk(v, k.upper() if isinstance(k, str) and k.upper() in wanted else sym)
        elif isinstance(node, list):
            for v in node:
                walk(v, symbol)

    walk(body)
    if None in found and len(wanted) == 1:
        found.setdefault(next(iter(wanted)), []).extend(found.pop(None))
    return {s: bars_frame(rows, daily) for s, rows in found.items() if s in wanted}


# ---------------------------------------------------------------- fetching
def fetch_bars(symbols: list[str], timeframe: str, count: int = 600, prepost: bool = False,
               token_wait: int = SCAN_TOKEN_WAIT) -> dict[str, pd.DataFrame]:
    """Webull's own candles for each symbol (missing symbols are left out)."""
    span = TIMESPANS.get(timeframe)
    if not span:
        raise WebullError(f"Webull has no {timeframe} candles (use {', '.join(TIMESPANS)})")
    daily = span == "D"
    sessions = ["PRE", "RTH", "ATH"] if prepost and not daily else ["RTH"]
    count = max(1, min(int(count), MAX_COUNT))
    data = client(token_wait)
    out: dict[str, pd.DataFrame] = {}
    todo = [s.upper() for s in dict.fromkeys(symbols)]
    for category in ("US_ETF", "US_STOCK"):          # ETFs first; anything not found is retried as a stock
        if not todo:
            break
        for i in range(0, len(todo), BATCH_SIZE):
            chunk = todo[i:i + BATCH_SIZE]
            try:
                res = data.market_data.get_batch_history_bar(
                    chunk, category, span, count=str(count), trading_sessions=sessions)
            except Exception as exc:
                reset_client()
                raise WebullError(describe_error(exc)) from None
            if res.status_code != 200:
                reset_client()
                raise WebullError(f"HTTP {res.status_code}: {str(res.text)[:200]}")
            for sym, df in parse_bars_response(res.json(), chunk, daily).items():
                if not df.empty:
                    out[sym] = df
        todo = [s for s in todo if s not in out]
    return out


def login(token_wait: int = LOGIN_TOKEN_WAIT) -> dict:
    """Log in (waiting while you approve in the Webull app) and check data access.
    Returns {"ok": bool, "message": str}."""
    reset_client()
    try:
        client(token_wait)
    except WebullError as exc:
        return {"ok": False, "message": str(exc)}
    try:
        got = fetch_bars(["SPY"], "1d", count=2, token_wait=token_wait)
    except WebullError as exc:
        return {"ok": False, "message": f"logged in, but data request failed: {exc}"}
    if "SPY" not in got:
        return {"ok": False, "message": "logged in, but Webull returned no SPY candles"}
    last = got["SPY"].index[-1]
    return {"ok": True, "message": f"connected; SPY daily candles through {last:%a %b %d}"}


def token_expiry() -> dt.datetime | None:
    """When the saved login token expires (from the SDK's token file), if known."""
    path = TOKEN_DIR / "token.txt"
    try:
        lines = path.read_text().splitlines()
        return dt.datetime.fromtimestamp(int(lines[1]) / 1000, tz=ET)
    except (OSError, IndexError, ValueError):
        return None
