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
               macd_hist=[-0.9, -0.6, -0.3, -0.1], low=[99.0, 94.0])
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


# ---------------------------------------------------------------- bars / timeframes

def ts(h, m, day=5):
    return pd.Timestamp(dt.datetime(2026, 11, day, h, m, tzinfo=ET))


def test_bar_end_and_forming_bar():
    assert scan.bar_end(ts(10, 30), "1h") == ts(11, 30)
    assert scan.bar_end(ts(15, 30), "1h") == ts(16, 0)       # last 1h bar is 30 min
    assert scan.bar_end(ts(13, 30), "4h") == ts(16, 0)
    assert scan.bar_end(ts(9, 30), "1d") == ts(16, 0)
    df = pd.DataFrame({"close": [1.0, 2.0]}, index=pd.DatetimeIndex([ts(10, 0), ts(10, 15)]))
    assert len(scan.drop_forming_bar(df, "15m", ts(10, 20).to_pydatetime())) == 1
    assert len(scan.drop_forming_bar(df, "15m", ts(10, 30).to_pydatetime())) == 2


def hourly_day(day=5):
    idx = pd.DatetimeIndex([ts(h, 30, day) for h in range(9, 16)])   # 09:30 ... 15:30
    vals = np.arange(1, 8, dtype=float)
    return pd.DataFrame({"open": vals, "high": vals + 0.5, "low": vals - 0.5,
                         "close": vals + 0.25, "volume": 10.0}, index=idx)


def test_resample_session_anchors_to_open():
    h = hourly_day()
    two = scan.resample_session(h, 120)
    assert [t.strftime("%H:%M") for t in two.index] == ["09:30", "11:30", "13:30", "15:30"]
    four = scan.resample_session(h, 240)
    assert [t.strftime("%H:%M") for t in four.index] == ["09:30", "13:30"]
    first = four.iloc[0]
    assert (first.open, first.high, first.low, first.close, first.volume) == (1.0, 4.5, 0.5, 4.25, 40.0)
    day = scan.resample_session(pd.concat([h, hourly_day(6)]), 1440)
    assert len(day) == 2 and day.iloc[0].close == 7.25


def test_configured_timeframes(cfg):
    assert scan.configured_timeframes(cfg["data"]) == ["1h", "2h", "4h", "1d"]
    assert scan.configured_timeframes({"timeframes": "1h, 1d"}) == ["1h", "1d"]
    with pytest.raises(SystemExit):
        scan.configured_timeframes({"timeframes": ["7h"]})


# ---------------------------------------------------------------- universe

def test_leveraged_and_inverse_labels(cfg):
    t = {x["ticker"]: x for x in scan.build_targets(cfg, None)}
    assert t["SOXX"]["trade_bull"] == [{"ticker": "SOXL", "leverage": 3.0, "type": "leveraged"}]
    assert t["SOXX"]["trade_bear"] == [{"ticker": "SOXS", "leverage": -3.0, "type": "inverse leveraged"}]
    assert [scan.describe_etf(e) for e in t["SPY"]["trade_bear"]] == [
        "SPXS (3x inverse leveraged)", "SPXU (3x inverse leveraged)", "SDS (2x inverse leveraged)"]
    assert scan.describe_etf(t["XLE"]["trade_bull"][0]) == "ERX (2x leveraged)"
    assert "SOXL" not in t
    # every fund in the universe has a leverage multiple
    assert all(e["leverage"] for x in t.values() for e in x["trade_bull"] + x["trade_bear"])


def test_leveraged_scan_mode_and_list_form(cfg):
    cfg["scan_targets"] = "both"
    t = {x["ticker"]: x for x in scan.build_targets(cfg, None)}
    # bearish read on a bull fund -> inverse funds; bullish read on an inverse fund -> itself
    assert [e["ticker"] for e in t["SOXL"]["trade_bear"]] == ["SOXS"]
    assert [e["ticker"] for e in t["SOXS"]["trade_bull"]] == ["SOXS"]
    assert scan._etfs(["ABC"], True) == [{"ticker": "ABC", "leverage": None, "type": "inverse leveraged"}]
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


# ---------------------------------------------------------------- alerts, state, Slack

def tf_result(level=None, met=0):
    ev = {"level": level, "met": met, "enabled": 4,
          "conditions": {"rsi": met > 0, "stochrsi": met > 1, "macd": met > 2, "bollinger": met > 3},
          "details": {"macd": {"met": met > 2, "state": "approaching", "bars_to_cross": 1.2}}}
    none = {"level": None, "met": 0, "enabled": 4, "details": {},
            "conditions": {"rsi": False, "stochrsi": False, "macd": False, "bollinger": False}}
    return {"last_bar": "2026-11-05T13:30:00-05:00", "live": True, "price": 212.34,
            "indicators": {"rsi7": 22.1, "rsi14": 27.5, "stoch_k": 8.2, "stoch_d": 11.0,
                           "bb_lower": 211.8, "bb_upper": 220.0},
            "bull": ev, "bear": none}


def scan_with(levels: dict, ticker="SOXX"):
    res = {"ticker": ticker, "name": "Semiconductors", "price": 212.34,
           "trade_bull": [{"ticker": "SOXL", "leverage": 3.0, "type": "leveraged"}],
           "trade_bear": [{"ticker": "SOXS", "leverage": -3.0, "type": "inverse leveraged"}],
           "timeframes": {tf: tf_result(*lv) for tf, lv in levels.items()}}
    return {"timeframes": list(levels), "results": [res]}


def run_state(cfg, levels, minute):
    s = scan_with(levels)
    scan.apply_state(s, cfg, True, dt.datetime(2026, 11, 5, 10, minute, tzinfo=ET))
    return s


def test_state_new_ongoing_upgrade_and_cooldown(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(scan, "STATE_FILE", tmp_path / "state.json")
    quiet, watch, sig = (None, 0), ("WATCH", 3), ("SIGNAL", 4)
    s = run_state(cfg, {"1h": sig, "2h": quiet, "4h": watch, "1d": quiet}, 0)
    a = s["alerts"][0]
    assert a["level"] == "SIGNAL" and a["new"] and sorted(a["new_triggers"]) == ["1h", "4h"]
    assert set(a["timeframes"]) == {"1h", "2h", "4h", "1d"}           # all timeframes shown
    s = run_state(cfg, {"1h": sig, "2h": quiet, "4h": watch, "1d": quiet}, 15)
    assert not s["alerts"][0]["new"]                                     # ongoing
    s = run_state(cfg, {"1h": sig, "2h": quiet, "4h": sig, "1d": quiet}, 30)
    assert s["alerts"][0]["new_triggers"] == ["4h"]                      # WATCH -> SIGNAL upgrade
    s = run_state(cfg, {"1h": quiet, "2h": quiet, "4h": sig, "1d": quiet}, 45)
    assert s["cleared"] == []                                            # 1h cooling down
    s = run_state(cfg, {"1h": sig, "2h": quiet, "4h": sig, "1d": quiet}, 50)
    assert not s["alerts"][0]["new"]                                     # flicker, not new
    run_state(cfg, {"1h": quiet, "2h": quiet, "4h": quiet, "1d": quiet}, 51)
    s = scan_with({"1h": quiet, "2h": quiet, "4h": quiet, "1d": quiet})
    scan.apply_state(s, cfg, True, dt.datetime(2026, 11, 5, 12, 0, tzinfo=ET))
    assert s["cleared"] == ["SOXX:bull:1h", "SOXX:bull:4h"]


def test_notification_shows_all_timeframes_and_labels(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(scan, "STATE_FILE", tmp_path / "state.json")
    s = run_state(cfg, {"1h": ("WATCH", 3), "2h": (None, 1), "4h": (None, 0), "1d": (None, 0)}, 0)
    assert scan.build_notification(s, cfg) is None                       # WATCH not in levels
    s = run_state(cfg, {"1h": ("WATCH", 3), "2h": ("SIGNAL", 4), "4h": (None, 0), "1d": (None, 0)}, 15)
    note = scan.build_notification(s, cfg)
    assert note["channel_id"] == "C0CF5LJJTTP" and note["count"] == 1
    msg = note["messages"][0]
    assert "BULLISH SIGNAL: SOXX" in msg and "triggered on 1h, 2h (new)" in msg
    assert "Leveraged: **SOXL (3x leveraged)**" in msg
    for tf in ("| 1h ★ |", "| 2h ★ |", "| 4h |", "| 1d |", "▲ upside"):
        assert tf in msg
    assert "13:30 live" in msg
    s = run_state(cfg, {"1h": ("WATCH", 3), "2h": ("SIGNAL", 4), "4h": (None, 0), "1d": (None, 0)}, 30)
    assert scan.build_notification(s, cfg) is None                       # already posted
    cfg["slack"]["enabled"] = False
    assert scan.build_notification(s, cfg) is None


# ---------------------------------------------------------------- end to end (offline)

def test_end_to_end_csv(tmp_path, cfg, monkeypatch, capsys):
    rng = np.random.default_rng(3)
    days = pd.bdate_range("2026-06-01", periods=110)
    idx = pd.DatetimeIndex([pd.Timestamp(f"{d.date()} 09:30", tz=ET) + pd.Timedelta(minutes=15 * k)
                            for d in days for k in range(26)])
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.003, len(idx))))
    df = pd.DataFrame({"datetime": idx, "open": close, "high": close * 1.002,
                       "low": close * 0.998, "close": close, "volume": 1000})
    df.to_csv(tmp_path / "SPY.csv", index=False)

    cfg["data"].update(source="csv", csv_dir=str(tmp_path))
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(json.dumps(cfg))  # JSON is valid YAML
    monkeypatch.setattr(scan, "STATE_FILE", tmp_path / "state.json")

    assert scan.main(["--config", str(cfg_path), "--tickers", "SPY", "--force", "--json", "--all"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["tickers_scanned"] == 1 and not out["errors"]
    res = out["results"][0]
    assert list(res["timeframes"]) == ["1h", "2h", "4h", "1d"]
    assert res["timeframes"]["4h"]["bars"] == 2 * 110 and res["timeframes"]["1d"]["bars"] == 110
    assert [e["ticker"] for e in res["trade_bull"]] == ["SPXL", "UPRO", "SSO"]
    assert all(0 <= r["indicators"]["rsi7"] <= 100 for r in res["timeframes"].values())

    assert scan.main(["--config", str(cfg_path), "--tickers", "SPY", "--force", "--all"]) == 0
    assert "ETF Signal Scan" in capsys.readouterr().out


def test_unadjusted_split_is_rejected():
    idx = pd.date_range("2026-10-01", periods=30, freq="D", tz=ET)
    close = np.r_[np.full(27, 150.0), [450.0, 455.0, 150.0]]
    bars = pd.DataFrame({"close": close}, index=idx)
    with pytest.raises(RuntimeError, match="unadjusted split"):
        scan.check_price_jumps(bars, 0.4)
    scan.check_price_jumps(bars.iloc[:27], 0.4)        # clean data passes
    scan.check_price_jumps(bars, 0)                    # disabled


def test_current_candle_only_by_default(cfg):
    assert cfg["rules"]["window_bars"] == 1
    # oversold and pierced the band one candle ago, but not on the current candle
    df = frame(rsi7=[25, 45], rsi14=[29, 50], stoch_k=[5, 40], stoch_d=[10, 40],
               macd_hist=[-0.9, -0.6, -0.3, -0.1], low=[94.0, 99.0])
    ev = scan.evaluate_direction(df, "bull", cfg)
    assert ev["conditions"] == {"rsi": False, "stochrsi": False, "macd": True, "bollinger": False}
    assert ev["level"] is None


def test_low_readings_lean_upside_high_readings_lean_downside(cfg):
    low = frame(rsi7=[20], rsi14=[25], stoch_k=[5], stoch_d=[8], low=[94.0])
    high = frame(rsi7=[80], rsi14=[75], stoch_k=[95], stoch_d=[92], high=[106.0])
    for df, side in ((low, "upside"), (high, "downside")):
        res = {"bull": scan.evaluate_direction(df, "bull", cfg),
               "bear": scan.evaluate_direction(df, "bear", cfg)}
        assert scan.lean(res)["side"] == side
    assert scan._lean_text({"lean": {"side": "upside", "bull_met": 3, "bear_met": 0, "enabled": 4}}) == "▲ upside 3/4"


# ---------------------------------------------------------------- extended hours

def test_extended_hours_session_and_gate(cfg):
    assert scan.market_is_open(dt.datetime(2026, 11, 5, 6, 0, tzinfo=ET), extended=True)
    assert scan.market_is_open(dt.datetime(2026, 11, 5, 19, 30, tzinfo=ET), extended=True)
    assert not scan.market_is_open(dt.datetime(2026, 11, 5, 19, 30, tzinfo=ET))
    assert not scan.market_is_open(dt.datetime(2026, 11, 5, 21, 0, tzinfo=ET), extended=True)
    cfg["data"]["include_prepost"] = True
    assert scan.session_hours(cfg["data"]) == (scan.EXT_OPEN, scan.EXT_CLOSE)
    assert scan.timeframe_plan("4h", cfg["data"]) == ("15m", 240, True)
    assert scan.timeframe_plan("1h", cfg["data"]) == ("15m", 60, True)
    assert scan.timeframe_plan("1d", cfg["data"]) == ("1d", 1440, False)
    cfg["data"]["include_prepost"] = False
    assert scan.timeframe_plan("1h", cfg["data"]) == ("60m", 60, False)


def test_extended_hours_candles_align_to_4am():
    idx = pd.date_range("2026-11-05 04:00", "2026-11-05 19:45", freq="15min", tz=ET)
    v = np.arange(len(idx), dtype=float)
    df = pd.DataFrame({"open": v, "high": v + 1, "low": v - 1, "close": v + 0.5, "volume": 1.0}, index=idx)
    four = scan.resample_session(df, 240, scan.EXT_OPEN)
    assert [t.strftime("%H:%M") for t in four.index] == ["04:00", "08:00", "12:00", "16:00"]
    one = scan.resample_session(df, 60, scan.EXT_OPEN)
    assert one.index[5].strftime("%H:%M") == "09:00" and one.iloc[5].volume == 4  # 09:00-10:00 incl. the open
    assert scan.bar_end(ts(16, 0), "4h", scan.EXT_CLOSE) == ts(20, 0)
    assert scan.bar_end(ts(9, 30), "1d", scan.EXT_CLOSE) == ts(16, 0)          # daily stays regular-session
    assert scan._bar_label({"timeframe": "1h", "last_bar": "2026-11-05T17:00:00-05:00"}) == "17:00 after-hrs"
    assert scan._bar_label({"timeframe": "1h", "last_bar": "2026-11-05T07:00:00-05:00", "live": True}) == "07:00 pre-mkt live"
    assert scan._bar_label({"timeframe": "4h", "last_bar": "2026-11-05T08:00:00-05:00"}) == "08:00"
