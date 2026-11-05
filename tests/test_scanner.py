import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

SKILL = Path(__file__).resolve().parents[1] / ".claude" / "skills" / "etf-signal-scanner"
sys.path.insert(0, str(SKILL / "scripts"))
import scan  # noqa: E402

ET = scan.ET


@pytest.fixture
def cfg():
    return scan.load_config(SKILL / "config.yaml")


# ---------------------------------------------------------------- indicators

def test_rsi_extremes():
    up = pd.Series(np.arange(1, 60, dtype=float))
    assert scan.rsi(up, 14).iloc[-1] == 100.0
    down = pd.Series(np.arange(60, 1, -1, dtype=float))
    assert scan.rsi(down, 14).iloc[-1] == 0.0
    flat = pd.Series(np.full(40, 5.0))
    assert scan.rsi(flat, 14).iloc[-1] == 50.0
    assert scan.rsi(up, 14).iloc[:14].isna().all()  # warm-up


def test_bollinger_constant_series_has_zero_width():
    lo, mid, up = scan.bollinger(pd.Series(np.full(30, 10.0)), 20, 2)
    assert lo.iloc[-1] == mid.iloc[-1] == up.iloc[-1] == 10.0


# ---------------------------------------------------------------- MACD cross logic

RULES = {"converging_bars": 2, "lookahead_bars": 2, "max_bars_since_cross": 1}


def hist(*vals):
    return pd.Series(vals, dtype=float)


def test_macd_bull_approaching():
    st = scan.macd_cross_state(hist(-0.9, -0.6, -0.3, -0.1), "bull", RULES)
    assert st["met"] and st["state"] == "approaching" and st["bars_to_cross"] == 0.5


def test_macd_bull_approaching_too_slowly():
    st = scan.macd_cross_state(hist(-1.0, -0.95, -0.9, -0.85), "bull", RULES)
    assert st["state"] == "approaching" and not st["met"]


def test_macd_bull_just_crossed_and_too_old():
    assert scan.macd_cross_state(hist(-0.5, -0.4, -0.2, 0.1), "bull", RULES)["state"] == "crossed"
    assert scan.macd_cross_state(hist(-0.5, -0.4, -0.2, 0.1, 0.2), "bull", RULES)["met"]
    old = scan.macd_cross_state(hist(-0.5, -0.4, -0.2, 0.1, 0.2, 0.3), "bull", RULES)
    assert not old["met"] and old["state"] == "already_crossed"


def test_macd_bull_diverging():
    st = scan.macd_cross_state(hist(0.0, -0.1, -0.2, -0.3), "bull", RULES)
    assert not st["met"] and st["state"] == "diverging"


def test_macd_bear_is_mirror():
    st = scan.macd_cross_state(hist(0.9, 0.6, 0.3, 0.1), "bear", RULES)
    assert st["met"] and st["state"] == "approaching"
    assert not scan.macd_cross_state(hist(0.9, 0.6, 0.3, 0.1), "bull", RULES)["met"]


# ---------------------------------------------------------------- rule evaluation

def frame(n=10, **cols):
    base = {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
            "rsi7": 50.0, "rsi14": 50.0, "stoch_k": 50.0, "stoch_d": 50.0,
            "macd_hist": 0.5, "bb_lower": 95.0, "bb_upper": 105.0}
    df = pd.DataFrame({k: np.full(n, v, dtype=float) for k, v in base.items()})
    for k, v in cols.items():
        df.loc[df.index[-len(v):], k] = v
    return df


def test_full_bull_signal(cfg):
    df = frame(rsi7=[25, 28], rsi14=[29, 29.5], stoch_k=[5, 12], stoch_d=[10, 15],
               macd_hist=[-0.9, -0.6, -0.3, -0.1], low=[94.0, 99.0])
    ev = scan.evaluate_direction(df, "bull", cfg)
    assert ev["level"] == "SIGNAL" and ev["met"] == 4
    assert scan.evaluate_direction(df, "bear", cfg)["level"] is None


def test_watch_when_band_not_pierced(cfg):
    df = frame(rsi7=[25], rsi14=[29], stoch_k=[5], stoch_d=[10],
               macd_hist=[-0.9, -0.6, -0.3, -0.1])
    ev = scan.evaluate_direction(df, "bull", cfg)
    assert ev["level"] == "WATCH" and ev["conditions"]["bollinger"] is False


def test_rsi_require_all_vs_any(cfg):
    df = frame(rsi7=[20], rsi14=[40])
    assert scan.evaluate_direction(df, "bull", cfg)["conditions"]["rsi"] is False
    cfg["rules"]["rsi"]["require"] = "any"
    assert scan.evaluate_direction(df, "bull", cfg)["conditions"]["rsi"] is True


def test_window_bars_limits_lookback(cfg):
    df = frame(rsi7=[20, 50, 50, 50], rsi14=[20, 50, 50, 50])  # extreme 4 bars ago
    assert scan.evaluate_direction(df, "bull", cfg)["conditions"]["rsi"] is False
    cfg["rules"]["window_bars"] = 4
    assert scan.evaluate_direction(df, "bull", cfg)["conditions"]["rsi"] is True


def test_full_bear_signal_and_band_modes(cfg):
    df = frame(rsi7=[80], rsi14=[75], stoch_k=[95], stoch_d=[90],
               macd_hist=[0.9, 0.6, 0.3, 0.1], high=[106.0])
    assert scan.evaluate_direction(df, "bear", cfg)["level"] == "SIGNAL"
    cfg["rules"]["bollinger"]["mode"] = "close"   # high pierced but close inside
    assert scan.evaluate_direction(df, "bear", cfg)["conditions"]["bollinger"] is False


def test_disabled_condition_lowers_requirement(cfg):
    cfg["rules"]["bollinger"]["enabled"] = False
    df = frame(rsi7=[25], rsi14=[29], stoch_k=[5], stoch_d=[10],
               macd_hist=[-0.9, -0.6, -0.3, -0.1])
    ev = scan.evaluate_direction(df, "bull", cfg)
    assert ev["enabled"] == 3 and ev["level"] == "SIGNAL"


# ---------------------------------------------------------------- data / universe / timing

def test_drop_forming_bar():
    idx = pd.DatetimeIndex([dt.datetime(2026, 11, 5, 10, 0, tzinfo=ET),
                            dt.datetime(2026, 11, 5, 10, 15, tzinfo=ET)])
    df = pd.DataFrame({"close": [1.0, 2.0]}, index=idx)
    assert len(scan.drop_forming_bar(df, "15m", dt.datetime(2026, 11, 5, 10, 20, tzinfo=ET))) == 1
    assert len(scan.drop_forming_bar(df, "15m", dt.datetime(2026, 11, 5, 10, 30, tzinfo=ET))) == 2


def test_build_targets_maps_leveraged_etfs(cfg):
    t = {x["ticker"]: x for x in scan.build_targets(cfg, None)}
    assert t["SOXX"]["trade_bull"] == ["SOXL"] and t["SOXX"]["trade_bear"] == ["SOXS"]
    assert "SOXL" not in t
    cfg["scan_targets"] = "both"
    t = {x["ticker"]: x for x in scan.build_targets(cfg, None)}
    # bearish read on a bull ETF -> buy the inverse, bullish read on a bear ETF -> buy itself
    assert t["SOXL"]["trade_bear"] == ["SOXS"] and t["SOXS"]["trade_bull"] == ["SOXS"]
    only = scan.build_targets(cfg, {"QQQ", "ZZZZ"})
    assert {x["ticker"] for x in only} == {"QQQ", "ZZZZ"}


def test_market_hours():
    assert scan.market_is_open(dt.datetime(2026, 11, 5, 10, 0, tzinfo=ET))      # Thu
    assert not scan.market_is_open(dt.datetime(2026, 11, 5, 8, 0, tzinfo=ET))
    assert not scan.market_is_open(dt.datetime(2026, 11, 7, 11, 0, tzinfo=ET))  # Sat


def test_loop_aligns_to_interval():
    now = dt.datetime(2026, 11, 5, 10, 7, 0, tzinfo=ET)
    assert scan.seconds_until_next_run(now, 15, True, 20) == 8 * 60 + 20
    just_after = dt.datetime(2026, 11, 5, 10, 15, 5, tzinfo=ET)
    assert scan.seconds_until_next_run(just_after, 15, True, 20) == 15


# ---------------------------------------------------------------- end to end (offline)

def test_end_to_end_csv(tmp_path, cfg, monkeypatch, capsys):
    rng = np.random.default_rng(3)
    idx = pd.date_range("2026-10-01 09:30", periods=400, freq="15min", tz=ET)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.003, len(idx))))
    df = pd.DataFrame({"datetime": idx, "open": close, "high": close * 1.002,
                       "low": close * 0.998, "close": close, "volume": 1000})
    df.to_csv(tmp_path / "SPY.csv", index=False)

    cfg["data"].update(source="csv", csv_dir=str(tmp_path), closed_bars_only=False)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(json.dumps(cfg))  # JSON is valid YAML
    monkeypatch.setattr(scan, "STATE_FILE", tmp_path / "state.json")

    assert scan.main(["--config", str(cfg_path), "--tickers", "SPY", "--force", "--json", "--all"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["tickers_scanned"] == 1 and not out["errors"]
    res = out["results"][0]
    assert res["bull"]["trade"] == ["SPXL", "UPRO", "SSO"]
    assert res["bear"]["trade"] == ["SPXS", "SPXU", "SDS"]
    assert 0 <= res["indicators"]["rsi7"] <= 100

    assert scan.main(["--config", str(cfg_path), "--tickers", "SPY", "--force", "--all"]) == 0
    assert "ETF Signal Scan" in capsys.readouterr().out


# ---------------------------------------------------------------- Slack notification

def _alert(level, new, direction="bull", ticker="SOXX"):
    return {"ticker": ticker, "name": "Semiconductors", "direction": direction, "level": level,
            "new": new, "met": 4, "enabled": 4,
            "conditions": {"rsi": True, "stochrsi": True, "macd": True, "bollinger": True},
            "macd": {"met": True, "state": "approaching", "bars_to_cross": 1.2},
            "trade": ["SOXL"], "price": 212.34, "last_bar": "2026-11-05T10:30:00-05:00",
            "indicators": {"rsi7": 22.1, "rsi14": 27.5, "stoch_k": 8.2, "stoch_d": 11.0,
                           "bb_lower": 211.8, "bb_upper": 220.0}}


def test_notification_only_for_new_alerts_at_configured_levels(cfg):
    scan_out = {"timeframe": "15m", "alerts": [_alert("SIGNAL", False), _alert("WATCH", True, ticker="XLF")]}
    assert scan.build_notification(scan_out, cfg) is None   # ongoing SIGNAL, WATCH not configured
    scan_out["alerts"].append(_alert("SIGNAL", True, ticker="QQQ"))
    note = scan.build_notification(scan_out, cfg)
    assert note["count"] == 1 and note["channel_id"] == cfg["slack"]["channel_id"]
    assert "BULLISH SIGNAL: QQQ" in note["message"] and "SOXL" in note["message"]
    assert "XLF" not in note["message"] and "bar 10:30 ET" in note["message"]
    cfg["slack"]["levels"] = ["SIGNAL", "WATCH"]
    assert scan.build_notification(scan_out, cfg)["count"] == 2
    cfg["slack"]["enabled"] = False
    assert scan.build_notification(scan_out, cfg) is None
