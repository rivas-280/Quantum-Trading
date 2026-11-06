#!/usr/bin/env python3
"""Candlestick chart window for one ETF, opened from the dashboard.

Price candles with Bollinger Bands (20, 2) and volume, plus RSI (7, 14),
StochRSI (3, 3, 14, 14) and MACD (8, 21, 5) panels, using the scanner's own
data loading and indicator code (and config.yaml settings), so the chart shows
exactly what the scanner judges.

    Timeframes: 15m 1h 2h 4h 1D (buttons, or keys 1-5)
    Mouse wheel / + -: zoom      Drag / arrow keys: pan      Home: back to latest
    Hover: crosshair with the values under the cursor

Run on its own:  python chart.py SOXX [1h]
"""

from __future__ import annotations

import datetime as dt
import math
import queue
import sys
import threading
import time
import tkinter as tk

TIMEFRAMES = [("15m", "15m"), ("1h", "1h"), ("2h", "2h"), ("4h", "4h"), ("1D", "1d")]
DEFAULT_BARS = {"15m": 160, "1h": 120, "2h": 110, "4h": 100, "1d": 120}
CACHE_SECONDS = 60

BG, PANEL, GRID, SEP = "#131722", "#1e222d", "#1f2430", "#2a2e39"
TEXT, MUTED, CROSS = "#d1d4dc", "#787b86", "#9598a1"
UP, DOWN, UP_VOL, DOWN_VOL = "#26a69a", "#ef5350", "#1d4b48", "#5a2a2d"
BB_BAND, BB_MID = "#5b9cf6", "#f6c85f"
RSI_COLORS = ["#f6c85f", "#c77dff", "#5b9cf6"]
K_COLOR, D_COLOR = "#5b9cf6", "#ff9800"
MACD_COLOR, SIGNAL_COLOR = "#5b9cf6", "#ff9800"
HIST_UP, HIST_UP_FADE, HIST_DOWN, HIST_DOWN_FADE = "#26a69a", "#b2dfdb", "#ef5350", "#ffcdd2"

RIGHT_AXIS, BOTTOM_AXIS, LEFT_PAD = 76, 26, 8
PANES = [("price", 0.52), ("rsi", 0.16), ("stoch", 0.16), ("macd", 0.16)]
LEGEND_H = 20


# ---------------------------------------------------------------- pure helpers (tested)
def nice_ticks(lo: float, hi: float, target: int = 5) -> list[float]:
    """Round-number axis ticks between lo and hi."""
    if not (math.isfinite(lo) and math.isfinite(hi)) or hi <= lo:
        return []
    raw = (hi - lo) / max(1, target)
    step = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if raw <= step * m:
            step *= m
            break
    first = math.ceil(lo / step) * step
    ticks, v = [], first
    while v <= hi + step * 1e-9:
        ticks.append(round(v, 10))
        v += step
    return ticks


def visible_window(total: int, count: int, offset: int) -> tuple[int, int, int, int]:
    """Clamp (count, offset) and return (start, end, count, offset); offset =
    bars hidden to the right of the view (0 = showing the latest bar)."""
    count = max(10, min(int(count), max(10, total)))
    offset = max(0, min(int(offset), max(0, total - count)))
    end = total - offset
    start = max(0, end - count)
    return start, end, count, offset


def zoom_window(total: int, count: int, offset: int, factor: float, frac: float) -> tuple[int, int]:
    """Zoom by `factor` (<1 = in) keeping the bar at `frac` (0-1 across the plot) in place."""
    start, end, count, offset = visible_window(total, count, offset)
    anchor = start + frac * count
    new_count = max(10, min(total, round(count * factor)))
    new_start = round(anchor - frac * new_count)
    new_offset = total - (new_start + new_count)
    _, _, new_count, new_offset = visible_window(total, new_count, new_offset)
    return new_count, new_offset


def segments(points):
    """Split [(x, y or None)] into canvas line coordinate lists, breaking at gaps."""
    seg = []
    for x, y in points:
        if y is None:
            if len(seg) >= 4:
                yield seg
            seg = []
            continue
        seg += [x, y]
    if len(seg) >= 4:
        yield seg


def fmt_price(v) -> str:
    if v is None or not math.isfinite(v):
        return "-"
    return f"{v:,.2f}" if abs(v) >= 1 else f"{v:.4f}"


def fmt_volume(v) -> str:
    if v is None or not math.isfinite(v):
        return "-"
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            return f"{v / div:.2f}{suffix}"
    return f"{v:.0f}"


def num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


# ---------------------------------------------------------------- data
_cache: dict[tuple[str, str], tuple[float, object]] = {}


def load_chart(ticker: str, tf: str) -> dict:
    """Bars + indicators + the scanner's verdict on the current candle."""
    hit = _cache.get((ticker, tf))
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    import scan                                    # pandas / yfinance: imported on first chart only
    cfg = scan.load_config(scan.DEFAULT_CONFIG)
    data_cfg = dict(cfg["data"])
    session = None
    if data_cfg.get("source", "yfinance") == "yahoo":
        import requests
        session = requests.Session()
    bars = scan.load_bars(ticker, [tf], data_cfg, session)[tf]
    if bars is None or bars.empty:
        raise RuntimeError(f"no {tf} data for {ticker}")
    df = scan.compute_indicators(bars, cfg["indicators"])
    verdict = None
    if len(bars) >= scan.min_bars_needed(cfg["indicators"]):
        verdict = scan.analyze(bars, cfg)
    out = {"ticker": ticker, "tf": tf, "df": df, "verdict": verdict, "cfg": cfg,
           "rsi_lengths": [int(x) for x in cfg["indicators"]["rsi"]["lengths"]],
           "loaded": time.time()}
    _cache[(ticker, tf)] = (time.time(), out)
    return out


# ---------------------------------------------------------------- window
class ChartWindow(tk.Toplevel):
    def __init__(self, master, ticker: str, name: str = "", tf: str = "1h"):
        super().__init__(master)
        self.ticker, self.name = ticker, name
        self.title(f"{ticker} chart - ETF Signal Scanner")
        self.geometry("1280x860")
        self.minsize(820, 560)
        self.configure(bg=BG)
        self.tf = tf
        self.data = None
        self.count, self.offset = DEFAULT_BARS.get(tf, 120), 0
        self.hover = None              # (x, y) of the mouse over the plot
        self.drag = None
        self.results: queue.Queue = queue.Queue()
        self.loading = False
        self._build()
        self.load(tf)
        self.after(100, self._poll)

    # ---- layout
    def _build(self):
        bar = tk.Frame(self, bg=BG, padx=14, pady=8)
        bar.pack(fill="x")
        left = tk.Frame(bar, bg=BG)
        left.pack(side="left")
        tk.Label(left, text=self.ticker, bg=BG, fg=TEXT, font=("Segoe UI", 18, "bold")).grid(row=0, column=0, sticky="w")
        self.price_lbl = tk.Label(left, text="", bg=BG, fg=TEXT, font=("Segoe UI", 18))
        self.price_lbl.grid(row=0, column=1, sticky="w", padx=(14, 0))
        self.change_lbl = tk.Label(left, text="", bg=BG, fg=MUTED, font=("Segoe UI", 12))
        self.change_lbl.grid(row=0, column=2, sticky="w", padx=(10, 0))
        tk.Label(left, text=self.name, bg=BG, fg=MUTED, font=("Segoe UI", 10)).grid(
            row=1, column=0, columnspan=3, sticky="w")

        right = tk.Frame(bar, bg=BG)
        right.pack(side="right")
        self.tf_btns = {}
        for label, tf in TIMEFRAMES:
            b = tk.Label(right, text=label, bg=PANEL, fg=TEXT, font=("Segoe UI", 10, "bold"),
                         padx=12, pady=5, cursor="hand2")
            b.pack(side="left", padx=2)
            b.bind("<Button-1>", lambda e, t=tf: self.load(t))
            self.tf_btns[tf] = b
        reload_btn = tk.Label(right, text="⟳", bg=PANEL, fg=TEXT, font=("Segoe UI", 11),
                              padx=10, pady=3, cursor="hand2")
        reload_btn.pack(side="left", padx=(10, 0))
        reload_btn.bind("<Button-1>", lambda e: self.load(self.tf, force=True))

        self.verdict_lbl = tk.Label(self, text="", bg=BG, fg=MUTED, font=("Segoe UI", 10),
                                    anchor="w", padx=14)
        self.verdict_lbl.pack(fill="x")
        self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True, padx=(0, 0), pady=(4, 0))
        self.status_lbl = tk.Label(self, text="", bg=BG, fg=MUTED, font=("Segoe UI", 9), anchor="w", padx=14)
        self.status_lbl.pack(fill="x", pady=(0, 4))

        c = self.canvas
        c.bind("<Configure>", lambda e: self.redraw())
        c.bind("<Motion>", self._on_motion)
        c.bind("<Leave>", self._on_leave)
        c.bind("<MouseWheel>", self._on_wheel)
        c.bind("<Button-4>", lambda e: self._zoom(0.85, e.x))   # Linux wheel
        c.bind("<Button-5>", lambda e: self._zoom(1 / 0.85, e.x))
        c.bind("<ButtonPress-1>", self._on_press)
        c.bind("<B1-Motion>", self._on_drag)
        c.bind("<ButtonRelease-1>", lambda e: setattr(self, "drag", None))
        self.bind("<Left>", lambda e: self._pan(max(1, self.count // 10)))
        self.bind("<Right>", lambda e: self._pan(-max(1, self.count // 10)))
        self.bind("<plus>", lambda e: self._zoom(0.8))
        self.bind("<equal>", lambda e: self._zoom(0.8))
        self.bind("<minus>", lambda e: self._zoom(1.25))
        self.bind("<Home>", lambda e: self._reset_view())
        for i, (_, tf) in enumerate(TIMEFRAMES, 1):
            self.bind(str(i), lambda e, t=tf: self.load(t))
        self.bind("<Escape>", lambda e: self.destroy())

    # ---- loading
    def load(self, tf: str, force: bool = False):
        if self.loading:
            return
        if force:
            _cache.pop((self.ticker, tf), None)
        self.tf = tf
        for t, b in self.tf_btns.items():
            b.configure(bg="#2962ff" if t == tf else PANEL)
        self.loading = True
        self.status_lbl.configure(text=f"Loading {self.ticker} {tf} candles…")
        threading.Thread(target=self._fetch, args=(self.ticker, tf), daemon=True).start()

    def _fetch(self, ticker, tf):
        try:
            self.results.put(("ok", load_chart(ticker, tf)))
        except Exception as exc:                       # shown in the window
            self.results.put(("error", f"{type(exc).__name__}: {exc}"))

    def _poll(self):
        try:
            kind, payload = self.results.get_nowait()
        except queue.Empty:
            pass
        else:
            self.loading = False
            if kind == "error":
                self.status_lbl.configure(text=f"Couldn't load {self.ticker} {self.tf}: {payload}", fg=DOWN)
            else:
                if not self.data or self.data["tf"] != payload["tf"]:
                    self.count, self.offset = DEFAULT_BARS.get(payload["tf"], 120), 0
                self.data = payload
                self._update_header()
                self.redraw()
        if self.winfo_exists():
            self.after(100, self._poll)

    def _update_header(self):
        df = self.data["df"]
        last, prev = df.iloc[-1], df.iloc[-2] if len(df) > 1 else df.iloc[-1]
        chg = last["close"] - prev["close"]
        pct = chg / prev["close"] * 100 if prev["close"] else 0
        color = UP if chg >= 0 else DOWN
        self.price_lbl.configure(text=f"${fmt_price(last['close'])}", fg=color)
        self.change_lbl.configure(text=f"{chg:+.2f} ({pct:+.2f}%) vs previous {self.data['tf']} close", fg=color)
        v = self.data.get("verdict")
        if v:
            names = {"rsi": "RSI", "stochrsi": "StochRSI", "macd": "MACD", "bollinger": "Bollinger"}

            def side(d, label):
                conds = "  ".join(f"{names.get(k, k)} {'✓' if ok else '✗'}" for k, ok in d["conditions"].items())
                lvl = f"  →  {d['level']}" if d["level"] else ""
                return f"{label} {d['met']}/{d['enabled']}:  {conds}{lvl}"
            self.verdict_lbl.configure(
                text=f"Current {self.data['tf']} candle   ▲ {side(v['bull'], 'Bullish')}      ▼ {side(v['bear'], 'Bearish')}",
                fg=UP if v["bull"]["level"] else DOWN if v["bear"]["level"] else MUTED)
        else:
            self.verdict_lbl.configure(text="Not enough candles on this timeframe for the scanner's rules.", fg=MUTED)
        when = dt.datetime.fromtimestamp(self.data["loaded"]).strftime("%H:%M:%S")
        self.status_lbl.configure(
            text=f"{len(df)} candles · last {df.index[-1]:%a %b %d %H:%M} ET · loaded {when} · "
                 "wheel = zoom, drag = pan, Home = latest, 1-5 = timeframe", fg=MUTED)

    # ---- view changes
    def _plot_box(self):
        w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
        return LEFT_PAD, w - RIGHT_AXIS, h - BOTTOM_AXIS

    def _zoom(self, factor, x=None):
        if not self.data:
            return
        left, right, _ = self._plot_box()
        frac = 1.0 if x is None else min(1, max(0, (x - left) / max(1, right - left)))
        self.count, self.offset = zoom_window(len(self.data["df"]), self.count, self.offset, factor, frac)
        self.redraw()

    def _pan(self, bars):
        if not self.data:
            return
        _, _, self.count, self.offset = visible_window(len(self.data["df"]), self.count, self.offset + bars)
        self.redraw()

    def _reset_view(self):
        self.count, self.offset = DEFAULT_BARS.get(self.tf, 120), 0
        self.redraw()

    def _on_wheel(self, e):
        self._zoom(0.85 if e.delta > 0 else 1 / 0.85, e.x)

    def _on_press(self, e):
        self.drag = (e.x, self.offset)

    def _on_drag(self, e):
        if not (self.drag and self.data):
            return
        left, right, _ = self._plot_box()
        bar_w = (right - left) / max(1, self.count)
        x0, off0 = self.drag
        _, _, self.count, self.offset = visible_window(
            len(self.data["df"]), self.count, off0 + round((e.x - x0) / bar_w))
        self.redraw()

    def _on_motion(self, e):
        self.hover = (e.x, e.y)
        self.draw_crosshair()

    def _on_leave(self, _):
        self.hover = None
        self.draw_crosshair()

    # ---- drawing
    def _panes(self):
        _, _, bottom = self._plot_box()
        y, out = 0, {}
        for key, frac in PANES:
            hgt = bottom * frac
            out[key] = (y, y + hgt)
            y += hgt
        return out

    def redraw(self):
        c = self.canvas
        c.delete("all")
        if not self.data:
            return
        df = self.data["df"]
        start, end, self.count, self.offset = visible_window(len(df), self.count, self.offset)
        view = df.iloc[start:end]
        if view.empty:
            return
        left, right, bottom = self._plot_box()
        bar_w = (right - left) / self.count
        xs = [left + (i + 0.5) * bar_w for i in range(len(view))]
        self.view, self.xs, self.bar_w = view, xs, bar_w
        panes = self._panes()
        self.scales = {}

        # frames, separators, axis background
        c.create_rectangle(right, 0, right + RIGHT_AXIS, bottom + BOTTOM_AXIS, fill=BG, outline="")
        for key, (top, bot) in panes.items():
            if key != "price":
                c.create_line(0, top, right + RIGHT_AXIS, top, fill=SEP)
        c.create_line(right, 0, right, bottom, fill=SEP)
        c.create_line(0, bottom, right + RIGHT_AXIS, bottom, fill=SEP)

        # ---- price pane
        top, bot = panes["price"]
        lo_cols = [view["low"], view["bb_lower"]]
        hi_cols = [view["high"], view["bb_upper"]]
        lo = min(float(s.min()) for s in lo_cols if s.notna().any())
        hi = max(float(s.max()) for s in hi_cols if s.notna().any())
        pad = (hi - lo) * 0.06 or max(abs(hi) * 0.01, 0.01)
        lo, hi = lo - pad, hi + pad
        p_top, p_bot = top + LEGEND_H + 4, bot - 6
        self._axis(c, "price", lo, hi, p_top, p_bot, right, fmt=fmt_price)

        # volume (bottom 18% of the price pane)
        vmax = float(view["volume"].max() or 0)
        if vmax > 0:
            v_h = (p_bot - p_top) * 0.18
            for x, (_, row) in zip(xs, view.iterrows()):
                vh = v_h * float(row["volume"]) / vmax
                col = UP_VOL if row["close"] >= row["open"] else DOWN_VOL
                c.create_rectangle(x - bar_w * 0.35, p_bot - vh, x + bar_w * 0.35, p_bot, fill=col, outline="")

        to_y = self.scales["price"]
        for col, color, dash in (("bb_upper", BB_BAND, None), ("bb_lower", BB_BAND, None), ("bb_mid", BB_MID, (4, 3))):
            pts = [(x, to_y(v) if (v := num(val)) is not None else None) for x, val in zip(xs, view[col])]
            for seg in segments(pts):
                c.create_line(*seg, fill=color, width=1, dash=dash)

        body_w = max(1.0, bar_w * 0.7)
        for x, (_, row) in zip(xs, view.iterrows()):
            o, h, l, cl = (float(row[k]) for k in ("open", "high", "low", "close"))
            color = UP if cl >= o else DOWN
            c.create_line(x, to_y(h), x, to_y(l), fill=color)
            y1, y2 = sorted((to_y(o), to_y(cl)))
            c.create_rectangle(x - body_w / 2, y1, x + body_w / 2, max(y2, y1 + 1), fill=color, outline=color)

        last = df.iloc[-1]
        last_color = UP if last["close"] >= last["open"] else DOWN
        ly = to_y(float(last["close"]))
        if p_top <= ly <= p_bot:
            c.create_line(left, ly, right, ly, fill=last_color, dash=(2, 3))
            c.create_rectangle(right, ly - 9, right + RIGHT_AXIS, ly + 9, fill=last_color, outline="")
            c.create_text(right + 6, ly, text=fmt_price(float(last["close"])), anchor="w",
                          fill="white", font=("Segoe UI", 9, "bold"))

        # ---- RSI
        rules = self.data["cfg"]["rules"]
        top, bot = panes["rsi"]
        self._axis(c, "rsi", 0, 100, top + LEGEND_H, bot - 4, right, ticks=[30, 50, 70])
        self._levels(c, "rsi", [rules["rsi"]["oversold"], rules["rsi"]["overbought"]], left, right)
        for n, color in zip(self.data["rsi_lengths"], RSI_COLORS):
            self._series(c, "rsi", view[f"rsi{n}"], color)

        # ---- StochRSI
        top, bot = panes["stoch"]
        self._axis(c, "stoch", 0, 100, top + LEGEND_H, bot - 4, right, ticks=[20, 50, 80])
        self._levels(c, "stoch", [rules["stochrsi"]["oversold"], rules["stochrsi"]["overbought"]], left, right)
        self._series(c, "stoch", view["stoch_k"], K_COLOR)
        self._series(c, "stoch", view["stoch_d"], D_COLOR)

        # ---- MACD
        top, bot = panes["macd"]
        vals = [v for col in ("macd", "macd_signal", "macd_hist") for v in view[col] if num(v) is not None]
        m_lo, m_hi = min(vals + [0.0]), max(vals + [0.0])
        m_pad = (m_hi - m_lo) * 0.08 or 1.0
        self._axis(c, "macd", m_lo - m_pad, m_hi + m_pad, top + LEGEND_H, bot - 4, right, fmt=lambda v: f"{v:.2f}")
        to_y = self.scales["macd"]
        zero = to_y(0)
        c.create_line(left, zero, right, zero, fill=SEP)
        prev = None
        for x, h in zip(xs, view["macd_hist"]):
            h = num(h)
            if h is None:
                prev = None
                continue
            rising = prev is None or h >= prev
            color = (HIST_UP if rising else HIST_UP_FADE) if h >= 0 else (HIST_DOWN_FADE if rising else HIST_DOWN)
            c.create_rectangle(x - bar_w * 0.3, min(zero, to_y(h)), x + bar_w * 0.3, max(zero, to_y(h)),
                               fill=color, outline="")
            prev = h
        self._series(c, "macd", view["macd"], MACD_COLOR)
        self._series(c, "macd", view["macd_signal"], SIGNAL_COLOR)

        # ---- time axis
        self._time_axis(c, view, xs, bottom)
        c.tag_lower("grid")                     # grid lines behind candles and indicator lines
        self.panes = panes
        self._legends()
        self.draw_crosshair()

    def _axis(self, c, key, lo, hi, top, bot, right, ticks=None, fmt=lambda v: f"{v:g}"):
        def to_y(v, lo=lo, hi=hi, top=top, bot=bot):
            return bot - (v - lo) / (hi - lo) * (bot - top) if hi > lo else (top + bot) / 2

        def from_y(y, lo=lo, hi=hi, top=top, bot=bot):
            return lo + (bot - y) / (bot - top) * (hi - lo) if bot > top else lo
        self.scales[key] = to_y
        self.scales[key + "_inv"] = from_y
        self.scales[key + "_fmt"] = fmt
        for t in ticks if ticks is not None else nice_ticks(lo, hi, max(2, int((bot - top) / 55))):
            y = to_y(t)
            if top - 1 <= y <= bot + 1:
                c.create_line(LEFT_PAD, y, right, y, fill=GRID, tags="grid")
                c.create_text(right + 6, y, text=fmt(t), anchor="w", fill=MUTED, font=("Segoe UI", 9))

    def _levels(self, c, key, levels, left, right):
        for lv in levels:
            y = self.scales[key](float(lv))
            c.create_line(left, y, right, y, fill="#4a4f5c", dash=(4, 4))

    def _series(self, c, key, series, color):
        to_y = self.scales[key]
        pts = [(x, to_y(v) if (v := num(val)) is not None else None) for x, val in zip(self.xs, series)]
        for seg in segments(pts):
            c.create_line(*seg, fill=color, width=1.4)

    def _time_axis(self, c, view, xs, bottom):
        intraday = self.data["tf"] != "1d"
        step = max(1, math.ceil(len(view) / max(1, (self._plot_box()[1] - LEFT_PAD) / 95)))
        prev_day = None
        for i in range(0, len(view), step):
            ts = view.index[i]
            if intraday:
                label = ts.strftime("%b %d") if ts.date() != prev_day else ts.strftime("%H:%M")
                prev_day = ts.date()
            else:
                label = ts.strftime("%b %d '%y")
            c.create_line(xs[i], 0, xs[i], bottom, fill=GRID, tags="grid")
            if xs[i] < 34 or xs[i] > self._plot_box()[1] - 30:   # would be clipped at the edges
                continue
            c.create_text(xs[i], bottom + 13, text=label, fill=MUTED, font=("Segoe UI", 9))

    def _legends(self, idx=None):
        """Pane titles with the values of the hovered bar (or the latest)."""
        c, view = self.canvas, self.view
        c.delete("legend")
        i = len(view) - 1 if idx is None else idx
        row = view.iloc[i]
        cfg = self.data["cfg"]["indicators"]
        ts = view.index[i]
        when = ts.strftime("%a %b %d") if self.data["tf"] == "1d" else ts.strftime("%a %b %d %H:%M")
        up = row["close"] >= row["open"]
        price_parts = [(f"{self.ticker} · {self.data['tf']} · {when}   ", TEXT)]
        for k in ("open", "high", "low", "close"):
            price_parts.append((f"{k[0].upper()} ", MUTED))
            price_parts.append((f"{fmt_price(num(row[k]))}  ", UP if up else DOWN))
        price_parts += [("Vol ", MUTED), (f"{fmt_volume(num(row['volume']))}   ", TEXT),
                        (f"BB({cfg['bollinger']['length']},{cfg['bollinger']['stddev']:g}) ", MUTED),
                        (f"{fmt_price(num(row['bb_upper']))} ", BB_BAND), (f"{fmt_price(num(row['bb_mid']))} ", BB_MID),
                        (f"{fmt_price(num(row['bb_lower']))}", BB_BAND)]
        rsi_parts = [("RSI  ", MUTED)]
        for n, color in zip(self.data["rsi_lengths"], RSI_COLORS):
            v = num(row[f"rsi{n}"])
            rsi_parts.append((f"RSI({n}) {v:.2f}   " if v is not None else f"RSI({n}) -   ", color))
        s = cfg["stochrsi"]
        k, d = num(row["stoch_k"]), num(row["stoch_d"])
        stoch_parts = [(f"StochRSI({s['smooth_k']},{s['smooth_d']},{s['rsi_length']},{s['stoch_length']})  ", MUTED),
                       (f"K {k:.2f}   " if k is not None else "K -   ", K_COLOR),
                       (f"D {d:.2f}" if d is not None else "D -", D_COLOR)]
        m = cfg["macd"]
        mv, sv, hv = num(row["macd"]), num(row["macd_signal"]), num(row["macd_hist"])
        f3 = lambda v: f"{v:.3f}" if v is not None else "-"
        macd_parts = [(f"MACD({m['fast']},{m['slow']},{m['signal']})  ", MUTED), (f"MACD {f3(mv)}   ", MACD_COLOR),
                      (f"Signal {f3(sv)}   ", SIGNAL_COLOR),
                      (f"Hist {f3(hv)}", HIST_UP if (hv or 0) >= 0 else HIST_DOWN)]
        for key, parts in (("price", price_parts), ("rsi", rsi_parts), ("stoch", stoch_parts), ("macd", macd_parts)):
            x, y = LEFT_PAD + 4, self.panes[key][0] + LEGEND_H / 2 + 2
            for text, color in parts:
                item = c.create_text(x, y, text=text, anchor="w", fill=color, font=("Segoe UI", 9), tags="legend")
                x = c.bbox(item)[2]

    def draw_crosshair(self):
        c = self.canvas
        c.delete("cross")
        if not self.data or not getattr(self, "xs", None):
            return
        left, right, bottom = self._plot_box()
        if not self.hover:
            self._legends()
            return
        x, y = self.hover
        if not (left <= x <= right and 0 <= y <= bottom):
            self._legends()
            return
        i = min(len(self.xs) - 1, max(0, int((x - left) / self.bar_w)))
        cx = self.xs[i]
        c.create_line(cx, 0, cx, bottom, fill=CROSS, dash=(3, 3), tags="cross")
        c.create_line(left, y, right, y, fill=CROSS, dash=(3, 3), tags="cross")
        for key, (top, bot) in self.panes.items():
            if top <= y <= bot:
                val = self.scales[key + "_inv"](y)
                text = self.scales[key + "_fmt"](val) if key in ("price", "macd") else f"{val:.1f}"
                c.create_rectangle(right, y - 9, right + RIGHT_AXIS, y + 9, fill="#363a45", outline="", tags="cross")
                c.create_text(right + 6, y, text=text, anchor="w", fill=TEXT, font=("Segoe UI", 9), tags="cross")
        ts = self.view.index[i]
        label = ts.strftime("%a %b %d '%y") if self.data["tf"] == "1d" else ts.strftime("%a %b %d %H:%M")
        tw = 7 * len(label) + 12
        c.create_rectangle(cx - tw / 2, bottom + 2, cx + tw / 2, bottom + BOTTOM_AXIS - 2, fill="#363a45",
                           outline="", tags="cross")
        c.create_text(cx, bottom + 13, text=label, fill=TEXT, font=("Segoe UI", 9), tags="cross")
        self._legends(i)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    ticker = (argv[0] if argv else "SPY").upper()
    tf = argv[1] if len(argv) > 1 else "1h"
    root = tk.Tk()
    root.withdraw()
    win = ChartWindow(root, ticker, tf=tf)
    win.protocol("WM_DELETE_WINDOW", root.destroy)
    win.bind("<Escape>", lambda e: root.destroy())
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
