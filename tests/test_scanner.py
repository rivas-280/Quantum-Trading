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


# ---------------------------------------------------------------- running status / stop messages

def test_next_session_start():
    thu_night = dt.datetime(2026, 11, 5, 20, 10, tzinfo=ET)
    assert scan.next_session_start(thu_night, True) == dt.datetime(2026, 11, 6, 4, 0, tzinfo=ET)
    fri_night = dt.datetime(2026, 11, 6, 20, 10, tzinfo=ET)
    assert scan.next_session_start(fri_night, True) == dt.datetime(2026, 11, 9, 4, 0, tzinfo=ET)
    early = dt.datetime(2026, 11, 5, 3, 0, tzinfo=ET)
    assert scan.next_session_start(early, False) == dt.datetime(2026, 11, 5, 9, 30, tzinfo=ET)


def fake_scan(scanned, failed):
    return {"scanned_at": "2026-11-05T19:46:00-05:00", "tickers_scanned": scanned,
            "errors": [{"ticker": f"T{i}", "error": "HTTPError: 429"} for i in range(failed)]
                      + [{"ticker": "SOXX", "timeframe": "1d", "error": "split"}]}


def test_status_stop_outage_and_recovery(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(scan, "STATUS_FILE", tmp_path / "status.json")
    t = lambda h, m: dt.datetime(2026, 11, 6, h, m, tzinfo=ET)   # Friday
    assert scan.update_status(t(19, 46), cfg, fake_scan(30, 0)) == []          # running, healthy
    out = scan.update_status(t(19, 46), cfg, fake_scan(5, 25))
    assert len(out) == 1 and "isn't getting market data" in out[0] and "25 of 30" in out[0]
    assert scan.update_status(t(19, 46), cfg, fake_scan(5, 25)) == []          # outage posted once
    back = scan.update_status(t(19, 46), cfg, fake_scan(30, 0))
    assert len(back) == 1 and "data is back" in back[0]
    stop = scan.update_status(t(20, 16), cfg, None)
    assert len(stop) == 1 and "stopped for the day" in stop[0]
    assert "Last scan 19:46 ET" in stop[0] and "Mon Nov 09 04:00" in stop[0]
    assert scan.update_status(t(20, 31), cfg, None) == []                      # only the first skip
    cfg["slack"]["status_messages"] = False
    scan.update_status(t(20, 46), cfg, fake_scan(30, 0))
    assert scan.update_status(t(21, 1), cfg, None) == []                       # disabled
    assert json.loads((tmp_path / "status.json").read_text())["running"] is False


def test_closed_scan_returns_stop_notice(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(scan, "STATUS_FILE", tmp_path / "status.json")
    (tmp_path / "status.json").write_text(json.dumps({"running": True, "last_scan": "2026-11-05T19:46:00-05:00"}))
    monkeypatch.setattr(scan, "market_is_open", lambda now, extended=False: False)
    args = type("A", (), {"force": False, "no_state": False, "tickers": None})()
    out = scan.scan_once(cfg, args)
    assert out["status"] == "market_closed"
    assert out["notify"]["channel_id"] == "C0CF5LJJTTP" and out["notify"]["status_count"] == 1
    assert scan.scan_once(cfg, args)["notify"] is None                          # already announced
    args.force = True                                                           # forced runs don't track
    (tmp_path / "status.json").write_text(json.dumps({"running": True}))
    monkeypatch.setattr(scan, "run_scan", lambda c, o, n: {"scanned_at": "x", "tickers_scanned": 0,
                                                           "errors": [], "results": [], "timeframes": []})
    monkeypatch.setattr(scan, "STATE_FILE", tmp_path / "state.json")
    scan.scan_once(cfg, args)
    assert json.loads((tmp_path / "status.json").read_text()) == {"running": True}


# ---------------------------------------------------------------- direct Slack posting (standalone / Windows)

def test_to_slack_mrkdwn_converts_bold_and_tables():
    md = "**New BULLISH SIGNAL: XLK** · $1\nLeveraged: **TECL (3x leveraged)**\n\n| TF | Status |\n|---|---|\n| 1h ★ | SIGNAL 4/4 |\n| 1d | - 2/4 |\n\n_note_"
    out = scan.to_slack_mrkdwn(md)
    assert out.startswith("*New BULLISH SIGNAL: XLK* · $1\nLeveraged: *TECL (3x leveraged)*")
    assert "```\nTF    Status\n1h ★  SIGNAL 4/4\n1d    - 2/4\n```" in out
    assert "**" not in out and "|" not in out and out.endswith("_note_")


class FakeResp:
    def __init__(self, code=200, text="ok"):
        self.status_code, self.text = code, text


def test_post_to_slack_and_deliver(monkeypatch, tmp_path, capsys):
    import requests
    sent = []
    monkeypatch.setattr(requests, "post", lambda url, json, timeout: sent.append((url, json["text"])) or FakeResp())
    assert scan.post_to_slack(["**hi**", "two"], "https://hooks.slack.com/x") == []
    assert sent == [("https://hooks.slack.com/x", "*hi*"), ("https://hooks.slack.com/x", "two")]
    monkeypatch.setattr(requests, "post", lambda url, json, timeout: FakeResp(404, "no_service"))
    assert scan.post_to_slack(["x"], "u") == ["HTTP 404: no_service"]

    # deliver: needs --post-slack and a URL; reads the git-ignored file or env var
    monkeypatch.setattr(scan, "WEBHOOK_FILE", tmp_path / "hook.txt")
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    s = {"notify": {"messages": ["m1"]}}
    args = type("A", (), {"post_slack": True})()
    scan.deliver(s, args)
    assert "no webhook URL" in capsys.readouterr().err and "posted" not in s
    (tmp_path / "hook.txt").write_text("https://hooks.slack.com/file\n")
    sent.clear()
    monkeypatch.setattr(requests, "post", lambda url, json, timeout: sent.append(url) or FakeResp())
    scan.deliver(s, args)
    assert sent == ["https://hooks.slack.com/file"] and s["posted"] == 1
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/env")
    assert scan.webhook_url() == "https://hooks.slack.com/env"
    args.post_slack = False
    sent.clear(); scan.deliver(s, args); assert sent == []


def test_lifecycle_and_announce(cfg, monkeypatch, tmp_path, capsys):
    import requests
    start = scan.lifecycle_message(cfg, "start", 15)
    assert "ETF scanner started" in start and "every 15 min" in start and "04:00-20:00 ET" in start
    assert "ETF scanner stopped" in scan.lifecycle_message(cfg, "stop", 15)
    cfg["slack"]["status_messages"] = False
    assert scan.lifecycle_message(cfg, "start", 15) is None
    sent = []
    monkeypatch.setattr(requests, "post", lambda url, json, timeout: sent.append(json["text"]) or FakeResp())
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/env")
    assert scan.main(["--announce", "stop"]) == 0 and "*ETF scanner stopped*" in sent[0]
    assert scan.main(["--test-slack"]) == 0 and "ETF scanner test" in sent[1]
    monkeypatch.delenv("SLACK_WEBHOOK_URL")
    monkeypatch.setattr(scan, "WEBHOOK_FILE", tmp_path / "none.txt")
    assert scan.main(["--test-slack"]) == 2


# ---------------------------------------------------------------- dashboard link (--loop <-> dashboard.py)

def summary_scan(cfg):
    s = scan_with({"1h": ("SIGNAL", 4), "2h": (None, 0), "4h": ("WATCH", 3), "1d": (None, 0)})
    s.update(scanned_at="2026-11-05T10:00:20-05:00", tickers_scanned=1,
             errors=[{"ticker": "XLE", "error": "HTTPError: 429"}])
    scan.apply_state(s, cfg, False, dt.datetime(2026, 11, 5, 10, 0, tzinfo=ET))
    return s


def test_scan_summary_for_dashboard(cfg):
    s = scan.scan_summary(summary_scan(cfg), forced=True)
    assert s["forced"] and s["tickers_scanned"] == 1 and s["signal_count"] == 1 and s["error_count"] == 1
    row = s["rows"][0]
    assert row["ticker"] == "SOXX" and row["trade_bull"] == ["SOXL (3x leveraged)"]
    assert row["timeframes"]["1h"]["bull"] == "SIGNAL" and row["timeframes"]["4h"]["bull_met"] == 3
    assert row["timeframes"]["2h"]["lean"] == "neutral"
    a = row["alerts"][0]
    assert a["direction"] == "bull" and a["level"] == "SIGNAL" and a["funds"] == ["SOXL (3x leveraged)"]
    json.dumps(s)                                                    # serialisable as-is


def test_control_and_atomic_write(tmp_path, monkeypatch):
    monkeypatch.setattr(scan, "CONTROL_FILE", tmp_path / "state" / "control.json")
    assert scan.read_control() == {"paused": False, "scan_now": None}   # no file = running
    scan.write_json_atomic(scan.CONTROL_FILE, {"paused": True, "scan_now": "t1"})
    assert scan.read_control() == {"paused": True, "scan_now": "t1"}
    assert [p.name for p in scan.CONTROL_FILE.parent.iterdir()] == ["control.json"]   # no temp left


def test_pause_resume_messages(cfg):
    assert "paused" in scan.lifecycle_message(cfg, "pause", 15)
    assert "resumed" in scan.lifecycle_message(cfg, "resume", 15) and "every 15 min" in scan.lifecycle_message(cfg, "resume", 15)
    assert "started, but paused" in scan.lifecycle_message(cfg, "start", 15, paused=True)


def loop_harness(cfg, tmp_path, monkeypatch, steps, paused_at_start=False):
    """Run run_loop with fake scans; each time.sleep runs the next step, then Ctrl-C."""
    for name in ("CONTROL_FILE", "DASHBOARD_FILE", "STATUS_FILE", "STATE_FILE"):
        monkeypatch.setattr(scan, name, tmp_path / f"{name.lower()}.json")
    if paused_at_start:
        scan.write_json_atomic(scan.CONTROL_FILE, {"paused": True})
    calls, posted = [], []
    def fake_scan_once(c, a):
        calls.append(a.force)
        return summary_scan(cfg)
    monkeypatch.setattr(scan, "scan_once", fake_scan_once)
    monkeypatch.setattr(scan, "emit", lambda s, c, a: None)
    monkeypatch.setattr(scan, "webhook_url", lambda: "https://hooks.slack.com/x")
    monkeypatch.setattr(scan, "post_to_slack", lambda msgs, url: posted.extend(msgs) or [])
    monkeypatch.setattr(scan, "seconds_until_next_run", lambda *a: 900)
    pending = list(steps)
    def fake_sleep(_):
        if not pending:
            raise KeyboardInterrupt
        pending.pop(0)()
    monkeypatch.setattr(scan.time, "sleep", fake_sleep)
    args = type("A", (), {"interval": None, "post_slack": True, "force": False})()
    assert scan.run_loop(cfg, args) == 0
    return calls, posted, json.loads(scan.DASHBOARD_FILE.read_text())


def test_loop_pause_scan_now_resume(cfg, tmp_path, monkeypatch):
    ctl = lambda **kw: (lambda: scan.write_json_atomic(scan.CONTROL_FILE, {**scan.read_control(), **kw}))
    calls, posted, d = loop_harness(cfg, tmp_path, monkeypatch, [
        ctl(paused=True), ctl(scan_now="t1"), ctl(paused=False)])
    assert calls == [False, True, False]        # scheduled scan, Scan now (forced), scan on resume
    kinds = [m.split("**")[1] for m in posted]
    assert kinds == ["ETF scanner started", "ETF scanner paused", "ETF scanner resumed", "ETF scanner stopped"]
    assert d["state"] == "stopped" and d["paused"] is False and d["scan_now_handled"] == "t1"
    assert d["last_result"] == "scanned" and d["last_scan"]["signal_count"] == 1
    assert d["heartbeat"] and d["next_check"] and d["slack_webhook"] is True


def test_loop_starts_paused_and_skips_scans(cfg, tmp_path, monkeypatch):
    calls, posted, d = loop_harness(cfg, tmp_path, monkeypatch, [], paused_at_start=True)
    assert calls == [] and "started, but paused" in posted[0]
    assert d["paused"] is True and d["last_result"] == "paused"


def test_dashboard_helpers(tmp_path, monkeypatch):
    pytest.importorskip("tkinter")
    import dashboard
    monkeypatch.setattr(dashboard, "CONTROL_FILE", tmp_path / "control.json")
    dashboard.write_control(paused=True)
    dashboard.write_control(scan_now="t1")
    assert dashboard.read_json(dashboard.CONTROL_FILE) == {"paused": True, "scan_now": "t1"}
    now = dashboard.now_et()
    assert dashboard.scanner_alive({"heartbeat": now.isoformat(), "state": "idle"})
    assert not dashboard.scanner_alive({"heartbeat": (now - dt.timedelta(minutes=5)).isoformat(), "state": "idle"})
    assert dashboard.scanner_alive({"heartbeat": (now - dt.timedelta(minutes=5)).isoformat(), "state": "scanning"})
    assert not dashboard.scanner_alive({"heartbeat": now.isoformat(), "state": "stopped"})
    assert not dashboard.scanner_alive({})
    t = {"bull": "SIGNAL", "bull_met": 4, "bear": None, "bear_met": 0, "enabled": 4, "lean": "upside"}
    assert dashboard.tf_cell(t) == "▲ SIGNAL 4/4"
    assert dashboard.tf_cell({**t, "bull": None, "bull_met": 2}) == "↑ 2/4"
    assert dashboard.tf_cell(None) == "no data"
    row = {"alerts": [{"direction": "bear", "level": "SIGNAL", "funds": ["SOXS (3x inverse leveraged)"]}]}
    assert dashboard.row_tag(row) == "bear_signal" and dashboard.look_at(row) == "Bear: SOXS (3x inverse leveraged)"


# ---------------------------------------------------------------- chart window

def test_chart_axis_and_view_math():
    pytest.importorskip("tkinter")
    import chart
    assert chart.nice_ticks(0, 100, 5) == [0, 20, 40, 60, 80, 100]
    assert chart.nice_ticks(131.2, 136.9, 4) == [132, 134, 136]
    assert chart.nice_ticks(5, 5) == []
    assert chart.visible_window(500, 100, 0) == (400, 500, 100, 0)          # latest bars
    assert chart.visible_window(500, 100, 450) == (0, 100, 100, 400)        # can't pan past the start
    assert chart.visible_window(30, 100, 5) == (0, 30, 30, 0)               # fewer bars than the view
    count, offset = chart.zoom_window(500, 100, 0, 0.5, 1.0)                # zoom in at the right edge
    assert (count, offset) == (50, 0)
    count, offset = chart.zoom_window(500, 100, 0, 0.5, 0.0)                # zoom in at the left edge
    assert count == 50 and chart.visible_window(500, count, offset)[0] == 400
    assert list(chart.segments([(0, 1), (1, 2), (2, None), (3, 4), (4, 5), (5, 6)])) == [[0, 1, 1, 2], [3, 4, 4, 5, 5, 6]]
    assert chart.fmt_volume(6_760_000) == "6.76M" and chart.fmt_price(163.836) == "163.84"


def test_chart_window_draws_from_bars(cfg, monkeypatch):
    tk = pytest.importorskip("tkinter")
    import chart
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    root.withdraw()
    idx = pd.date_range("2026-10-01 09:30", periods=300, freq="h", tz=ET)
    close = pd.Series(100 + np.cumsum(np.sin(np.arange(300) / 7)), index=idx)
    bars = pd.DataFrame({"open": close.shift().fillna(100), "high": close + 1, "low": close - 1,
                         "close": close, "volume": 1000.0}, index=idx)
    data = {"ticker": "TEST", "tf": "1h", "df": scan.compute_indicators(bars, cfg["indicators"]),
            "verdict": scan.analyze(bars, cfg), "cfg": cfg, "rsi_lengths": [7, 14], "loaded": 0}
    monkeypatch.setattr(chart, "load_chart", lambda t, tf: data)
    win = chart.ChartWindow(root, "TEST", "Test ETF", tf="1h")
    win.geometry("1200x800")
    for _ in range(50):                                   # wait for the loader thread
        root.update()
        if win.data:
            break
        root.after(50)
    assert win.data is data and "Bullish" in win.verdict_lbl.cget("text")
    win.redraw()
    items = len(win.canvas.find_all())
    assert items > 500                                    # candles, bands, 3 indicator panels, axes
    win.hover = (600, 300)
    win.draw_crosshair()
    assert win.canvas.find_withtag("cross") and win.canvas.find_withtag("legend")
    win._zoom(0.5, 600)
    assert win.count == 60
    before = win.offset                                   # zooming mid-chart keeps that bar in place
    win._pan(20)
    assert win.offset == before + 20
    root.destroy()


# ---------------------------------------------------------------- Webull data source

def test_webull_response_layouts_and_times():
    import webull_source as wb
    bar = lambda t, c: {"time": t, "open": str(c), "high": str(c + 1), "low": str(c - 1), "close": str(c), "volume": "1000"}
    layouts = [
        [{"symbol": "SPY", "result": [bar("2026-11-05T15:00:00.000+0000", 10), bar("2026-11-05T14:00:00.000+0000", 9)]},
         {"symbol": "QQQ", "result": [bar(1793890800000, 20)]}],                       # epoch ms
        {"data": [{"symbol": "SPY", "bars": [bar("2026-11-05T14:00:00Z", 9), bar("2026-11-05T15:00:00Z", 10)]},
                  {"symbol": "QQQ", "bars": [bar("1793890800", 20)]}]},                 # epoch s as string
        {"SPY": [bar("2026-11-05T14:00:00Z", 9), bar("2026-11-05T15:00:00Z", 10)], "QQQ": [bar(1793890800000, 20)]},
    ]
    for body in layouts:
        got = wb.parse_bars_response(body, ["SPY", "QQQ"], daily=False)
        assert set(got) == {"SPY", "QQQ"}
        spy = got["SPY"]
        assert list(spy["close"]) == [9.0, 10.0]                            # sorted, numeric
        assert spy.index[0] == pd.Timestamp("2026-11-05 09:00", tz=ET)       # 14:00 UTC = 09:00 ET
        assert got["QQQ"].index[0] == pd.Timestamp("2026-11-05 10:00", tz=ET)
    single = wb.parse_bars_response([bar("2026-11-05T14:00:00Z", 9)], ["SPY"], daily=False)   # bare list
    assert list(single) == ["SPY"]
    daily = wb.parse_bars_response([bar("2026-11-05T00:00:00Z", 9), bar("2026-11-06T05:00:00Z", 10)], ["SPY"], daily=True)
    assert [d.date() for d in daily["SPY"].index] == [dt.date(2026, 11, 5), dt.date(2026, 11, 6)]   # calendar dates kept


def test_webull_errors_are_short_and_secret_free():
    import webull_source as wb
    class E(Exception):
        error_code, error_msg = "MARKET_DATA_NOT_SUBSCRIBED", "Insufficient permission, please subscribe to STOCK QUOTES."
    assert "subscription" in wb.describe_error(E()) and "STOCK QUOTES" in wb.describe_error(E())
    leaky = Exception("boom x-access-token: 0123456789abcdef0123456789abcdef Request:{headers...}")
    text = wb.describe_error(leaky)
    assert "0123456789abcdef" not in text and "headers" not in text
    assert "Connect Webull" in wb.describe_error(Exception("init_token status not verified error. status:PENDING"))


def test_webull_credentials(tmp_path, monkeypatch):
    import webull_source as wb
    monkeypatch.setattr(wb, "CREDENTIALS_FILE", tmp_path / "creds.yaml")
    assert not wb.configured()
    (tmp_path / "creds.yaml").write_text('app_key: ""\napp_secret: ""\nregion: us\n')
    with pytest.raises(wb.WebullError, match="missing"):
        wb.load_credentials()
    (tmp_path / "creds.yaml").write_text('app_key: "k"\napp_secret: "s"\n')
    assert wb.load_credentials() == {"app_key": "k", "app_secret": "s", "region": "us", "endpoint": "api.webull.com"}


def webull_cfg(cfg):
    cfg["data"].update(source="webull", fallback_source="csv", timeframes=["1h", "1d"])
    return cfg


def test_scan_uses_webull_candles(cfg, monkeypatch):
    import webull_source as wb
    idx = pd.date_range("2026-10-01 09:30", periods=300, freq="h", tz=ET)
    close = pd.Series(100 + np.sin(np.arange(300) / 5), index=idx)
    bars = pd.DataFrame({"open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 1.0}, index=idx)
    asked = []
    def fake_fetch(symbols, tf, count=600, prepost=False, token_wait=20):
        asked.append((tuple(symbols), tf, count, prepost))
        return {s: bars for s in symbols}
    monkeypatch.setattr(wb, "fetch_bars", fake_fetch)
    out = scan.run_scan(webull_cfg(cfg), {"SPY", "QQQ"}, dt.datetime(2026, 11, 5, 12, tzinfo=ET))
    assert out["source"] == "webull" and out["source_error"] is None and out["tickers_scanned"] == 2
    assert sorted(a[1] for a in asked) == ["1d", "1h"] and all(len(a[0]) == 2 for a in asked)   # one batch per timeframe
    assert all(a[3] is True for a in asked)                                                   # extended hours requested


def test_scan_falls_back_when_webull_fails(cfg, monkeypatch, tmp_path, capsys):
    import webull_source as wb
    def boom(*a, **k):
        raise wb.WebullError("MARKET_DATA_NOT_SUBSCRIBED: please subscribe")
    monkeypatch.setattr(wb, "fetch_bars", boom)
    seen = []
    monkeypatch.setattr(scan, "load_bars", lambda t, tfs, dc, s, pre=None: seen.append(dc["source"]) or (_ for _ in ()).throw(RuntimeError("csv missing")))
    out = scan.run_scan(webull_cfg(cfg), {"SPY"}, dt.datetime(2026, 11, 5, 12, tzinfo=ET))
    assert out["source"] == "csv" and out["requested_source"] == "webull" and "SUBSCRIBED" in out["source_error"]
    assert seen == ["csv"] and "Webull data unavailable" in capsys.readouterr().err
    cfg["data"].pop("fallback_source")
    with pytest.raises(RuntimeError, match="Webull data unavailable"):
        scan.run_scan(cfg, {"SPY"}, dt.datetime(2026, 11, 5, 12, tzinfo=ET))


def test_webull_outage_and_recovery_posted_once(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(scan, "STATUS_FILE", tmp_path / "status.json")
    t = dt.datetime(2026, 11, 6, 10, 0, tzinfo=ET)
    down = {**fake_scan(30, 0), "requested_source": "webull", "source": "yfinance", "source_error": "NOT_SUBSCRIBED"}
    up = {**fake_scan(30, 0), "requested_source": "webull", "source": "webull", "source_error": None}
    first = scan.update_status(t, cfg, down)
    assert len(first) == 1 and "Webull data unavailable" in first[0] and "yfinance" in first[0]
    assert scan.update_status(t, cfg, down) == []                     # only once
    back = scan.update_status(t, cfg, up)
    assert len(back) == 1 and "Webull data is back" in back[0]
    assert scan.update_status(t, cfg, up) == []
