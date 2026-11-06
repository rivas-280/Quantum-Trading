#!/usr/bin/env python3
"""ETF Signal Scanner dashboard: a small desktop window that shows whether the
scanner is running, what it found the last time it checked, and lets you
start / pause it or run a scan right away.

The scanning itself (and every Slack post) is done by the background scanner,
`scan.py --loop --post-slack`, which on Windows runs as the scheduled task
"ETF Signal Scanner". This window only talks to it through two files in
state/: it reads dashboard.json (written by the scanner) and writes
control.json (read by the scanner every few seconds). Closing the window does
not stop the scanner.

    pythonw dashboard.py        (the installer puts an "ETF Scanner" shortcut on the desktop)
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
import subprocess
import sys
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from zoneinfo import ZoneInfo

import chart

SKILL_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = SKILL_DIR.parent.parent.parent
CONTROL_FILE = SKILL_DIR / "state" / "control.json"
DASHBOARD_FILE = SKILL_DIR / "state" / "dashboard.json"
LOG_FILE = SKILL_DIR / "logs" / "scanner.log"
SCAN_SCRIPT = SKILL_DIR / "scripts" / "scan.py"
WEBULL_LOGIN_FILE = SKILL_DIR / "state" / "webull_login.json"
WEBULL_TOKEN_FILE = SKILL_DIR / "state" / "webull_token" / "token.txt"
SOURCE_NAMES = {"webull": "Webull", "yfinance": "Yahoo Finance", "yahoo": "Yahoo Finance", "csv": "CSV files"}
ICON_FILE = REPO_DIR / "windows" / "etf-scanner.ico"
TASK_NAME = "ETF Signal Scanner"
ET = ZoneInfo("America/New_York")
REFRESH_MS = 3000
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

GREEN, RED, AMBER, BLUE, GREY = "#1a7f37", "#cf222e", "#9a6700", "#0969da", "#57606a"


# ---------------------------------------------------------------- files
def read_json(path: Path) -> dict:
    for _ in range(3):
        try:
            return json.loads(path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError):
            time.sleep(0.05)       # being replaced this instant; try again
    return {}


def write_control(**changes) -> None:
    data = read_json(CONTROL_FILE)
    data.update(changes)
    CONTROL_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONTROL_FILE.with_name(f"control.json.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    for _ in range(20):
        try:
            os.replace(tmp, CONTROL_FILE)
            return
        except PermissionError:
            time.sleep(0.05)
    raise OSError(f"Couldn't write {CONTROL_FILE}")


def parse_time(s) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(s).astimezone(ET) if s else None
    except (TypeError, ValueError):
        return None


def now_et() -> dt.datetime:
    return dt.datetime.now(tz=ET)


def ago(t: dt.datetime | None) -> str:
    if not t:
        return ""
    secs = (now_et() - t).total_seconds()
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)} min ago"
    if secs < 86400:
        h, m = divmod(int(secs // 60), 60)
        return f"{h} h {m} min ago"
    return f"{int(secs // 86400)} day(s) ago"


def when(t: dt.datetime | None) -> str:
    if not t:
        return "never"
    fmt = "%H:%M ET" if t.date() == now_et().date() else "%a %b %d %H:%M ET"
    return t.strftime(fmt)


def scanner_alive(d: dict) -> bool:
    """The scanner refreshes its heartbeat every 30 s while idle; a scan can
    take a few minutes, so allow longer while one is in progress."""
    hb = parse_time(d.get("heartbeat"))
    if not hb or d.get("state") == "stopped":
        return False
    limit = 20 * 60 if d.get("state") == "scanning" else 120
    return (now_et() - hb).total_seconds() < limit


# ---------------------------------------------------------------- scheduled task
def task_state() -> str | None:
    """'Running' / 'Ready' / 'Disabled', or None if the task isn't installed."""
    if os.name != "nt":
        return None
    try:
        out = subprocess.run(["schtasks", "/query", "/tn", TASK_NAME, "/fo", "csv", "/nh"],
                             capture_output=True, text=True, timeout=15, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    rows = list(csv.reader(io.StringIO(out.stdout.strip())))
    return rows[0][-1] if rows and rows[0] else None


def run_task() -> tuple[bool, str]:
    if os.name != "nt":
        return False, "Starting the scanner from here only works on Windows (scheduled task)."
    try:
        out = subprocess.run(["schtasks", "/run", "/tn", TASK_NAME],
                             capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    return out.returncode == 0, (out.stdout + out.stderr).strip()


# ---------------------------------------------------------------- table cells
def tf_cell(t: dict | None) -> str:
    if not t:
        return "no data"
    n = t.get("enabled", 4)
    if t.get("bull"):
        return f"▲ {t['bull']} {t['bull_met']}/{n}"
    if t.get("bear"):
        return f"▼ {t['bear']} {t['bear_met']}/{n}"
    if t.get("lean") == "upside":
        return f"↑ {t['bull_met']}/{n}"
    if t.get("lean") == "downside":
        return f"↓ {t['bear_met']}/{n}"
    return f"· {t.get('bull_met', 0)}/{n}"


def row_tag(row: dict) -> str:
    levels = [(a["level"], a["direction"]) for a in row.get("alerts", [])]
    if ("SIGNAL", "bull") in levels:
        return "bull_signal"
    if ("SIGNAL", "bear") in levels:
        return "bear_signal"
    if levels:
        return "watch"
    return "plain"


def look_at(row: dict) -> str:
    out = []
    for a in row.get("alerts", []):
        funds = ", ".join(a.get("funds") or []) or "none mapped"
        out.append(("Bull: " if a["direction"] == "bull" else "Bear: ") + funds)
    return "  |  ".join(out)


# ---------------------------------------------------------------- window
class Dashboard(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ETF Signal Scanner")
        self.geometry("1180x720")
        self.minsize(900, 520)
        if ICON_FILE.exists():
            try:
                self.iconbitmap(default=str(ICON_FILE))
            except tk.TclError:
                pass
        self.data: dict = {}
        self.shown_scan = None
        self.rows_by_ticker: dict[str, dict] = {}
        self.charts: dict[str, tk.Toplevel] = {}
        self.task = None
        self.task_checked = 0.0
        self.notice = ("", GREY)
        self.notice_until = 0.0
        self._build()
        self.refresh()

    # ---- layout
    def _build(self):
        style = ttk.Style(self)
        base = ("Segoe UI", 10)
        style.configure(".", font=base)
        style.configure("Treeview", rowheight=24)
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))
        style.configure("Big.TButton", font=("Segoe UI", 11), padding=(14, 6))

        top = ttk.Frame(self, padding=(14, 12, 14, 4))
        top.pack(fill="x")
        self.status_dot = tk.Label(top, text="●", font=("Segoe UI", 22), fg=GREY)
        self.status_dot.grid(row=0, column=0, rowspan=2, sticky="w", padx=(0, 8))
        self.status_text = ttk.Label(top, text="Loading…", font=("Segoe UI", 15, "bold"))
        self.status_text.grid(row=0, column=1, sticky="w")
        self.status_sub = ttk.Label(top, text="", foreground=GREY)
        self.status_sub.grid(row=1, column=1, sticky="w")
        top.columnconfigure(1, weight=1)

        buttons = ttk.Frame(top)
        buttons.grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self.btn_start = ttk.Button(buttons, text="▶  Start scanner", style="Big.TButton", command=self.start)
        self.btn_pause = ttk.Button(buttons, text="❚❚  Pause", style="Big.TButton", command=self.pause)
        self.btn_scan = ttk.Button(buttons, text="⟳  Scan now", style="Big.TButton", command=self.scan_now)
        self.btn_log = ttk.Button(buttons, text="Open log", command=self.open_log)
        self.btn_webull = ttk.Button(buttons, text="Connect Webull", command=self.connect_webull)
        self.login_proc = None
        for i, b in enumerate((self.btn_start, self.btn_pause, self.btn_scan, self.btn_log, self.btn_webull)):
            b.grid(row=0, column=i, padx=(0, 8))

        info = ttk.Frame(self, padding=(14, 6, 14, 6))
        info.pack(fill="x")
        self.info: dict[str, ttk.Label] = {}
        for r, key in enumerate(("Last check", "Next check", "Last market scan", "Data", "Slack")):
            ttk.Label(info, text=key + ":", font=("Segoe UI", 10, "bold")).grid(row=r, column=0, sticky="w", padx=(0, 10))
            lbl = ttk.Label(info, text="")
            lbl.grid(row=r, column=1, sticky="w")
            self.info[key] = lbl
        self.notice_lbl = ttk.Label(info, text="", foreground=BLUE)
        self.notice_lbl.grid(row=5, column=0, columnspan=2, sticky="w", pady=(4, 0))

        body = ttk.PanedWindow(self, orient="vertical")
        body.pack(fill="both", expand=True, padx=14, pady=(4, 6))

        sig_box = ttk.Labelframe(body, text=" Signals and watches from the last market scan  (double-click for the chart) ", padding=6)
        self.sig = self._tree(sig_box, ("ticker", "setup", "level", "new", "on", "look"),
                              ("Ticker", "Setup", "Level", "New?", "Triggered on", "Look at"),
                              (80, 90, 80, 60, 300, 420), height=5)
        body.add(sig_box, weight=1)

        all_box = ttk.Labelframe(body, text=" All ETFs  (double-click one for its candle chart) ", padding=6)
        self.all_box = all_box
        self.all = None
        body.add(all_box, weight=3)

        bottom = ttk.Frame(self, padding=(14, 0, 14, 10))
        bottom.pack(fill="x")
        ttk.Label(bottom, foreground=GREY, text=(
            "▲ / ▼ = bullish / bearish setup with conditions met (RSI, StochRSI, MACD, Bollinger).   "
            "↑ / ↓ = which way the current candle leans.\n"
            "Closing this window doesn't stop the scanner: it keeps scanning and posting to Slack.")).pack(anchor="w")
        self.err_lbl = ttk.Label(bottom, text="", foreground=RED, wraplength=1100, justify="left")
        self.err_lbl.pack(anchor="w")

    def _tree(self, parent, cols, heads, widths, height=10):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(frame, columns=cols, show="headings", height=height)
        for c, h, w in zip(cols, heads, widths):
            tree.heading(c, text=h)
            tree.column(c, width=w, minwidth=40, stretch=c in ("look", "name"), anchor="w")
        sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        tree.bind("<Double-1>", lambda e, t=tree: self.open_chart_from(t, e))
        tree.bind("<Return>", lambda e, t=tree: self.open_chart_from(t))
        tree.tag_configure("bull_signal", background="#dafbe1")
        tree.tag_configure("bear_signal", background="#ffebe9")
        tree.tag_configure("watch", background="#fff8c5")
        return tree

    def _build_all_tree(self, timeframes):
        for child in self.all_box.winfo_children():
            child.destroy()
        cols = ("ticker", "name", "price", *[f"tf_{tf}" for tf in timeframes], "look")
        heads = ("Ticker", "Name", "Price", *timeframes, "Look at")
        widths = (70, 230, 80, *[110] * len(timeframes), 260)
        self.all = self._tree(self.all_box, cols, heads, widths)
        self.all_timeframes = list(timeframes)

    # ---- actions
    def say(self, text: str, color: str = BLUE, seconds: float = 15):
        self.notice = (text, color)
        self.notice_until = time.time() + seconds
        self.notice_lbl.configure(text=text, foreground=color)

    def start(self):
        try:
            write_control(paused=False)
        except OSError as exc:
            messagebox.showerror("ETF Scanner", str(exc))
            return
        if scanner_alive(self.data):
            self.say("Starting… the scanner resumes within a few seconds and posts to Slack.")
        else:
            ok, out = run_task()
            self.task_checked = 0
            if not ok:
                messagebox.showerror("ETF Scanner", "Couldn't start the scheduled task "
                                     f"'{TASK_NAME}'.\n\n{out}\n\nIf it isn't installed, run "
                                     "windows\\install.ps1 as Administrator.")
                return
            if "currently running" in out.lower():
                self.say("Windows says the scanner task is already running but it isn't responding. "
                         "Check the log, or run windows\\install.ps1 again as Administrator.", RED, 60)
            else:
                self.say("Starting the scanner… it takes about 10-20 seconds to come up.")
        self.after(500, self.refresh_once)

    def pause(self):
        try:
            write_control(paused=True)
        except OSError as exc:
            messagebox.showerror("ETF Scanner", str(exc))
            return
        self.say("Pausing… no scans or alerts until you press Start. (A Slack message confirms it.)", AMBER)
        self.after(500, self.refresh_once)

    def scan_now(self):
        if not scanner_alive(self.data):
            messagebox.showinfo("ETF Scanner", "The scanner isn't running. Press Start first.")
            return
        try:
            write_control(scan_now=now_et().isoformat(timespec="seconds"))
        except OSError as exc:
            messagebox.showerror("ETF Scanner", str(exc))
            return
        self.say("Scan requested. It starts within 5 seconds and takes a minute or two; "
                 "new signals are posted to Slack as usual.")
        self.after(500, self.refresh_once)

    def open_chart_from(self, tree, event=None):
        item = tree.identify_row(event.y) if event is not None else tree.focus()
        if not item:
            return
        ticker = (tree.item(item, "values") or [""])[0]
        if ticker:
            self.open_chart(ticker)

    def open_chart(self, ticker: str):
        win = self.charts.get(ticker)
        if win is not None and win.winfo_exists():
            win.deiconify()
            win.lift()
            win.focus_force()
            return
        row = self.rows_by_ticker.get(ticker, {})
        tf = "1h"
        for a in row.get("alerts", []):          # open on the timeframe that triggered
            tf = next(iter(a.get("triggers") or {}), tf)
            break
        self.charts[ticker] = chart.ChartWindow(self, ticker, row.get("name", ""), tf=tf)

    def connect_webull(self):
        """Run `scan.py --webull-login` in the background: it asks Webull for a login,
        waits while you approve it in the Webull app, then checks data access."""
        if self.login_proc and self.login_proc.poll() is None:
            return
        try:
            WEBULL_LOGIN_FILE.unlink(missing_ok=True)
            self.login_proc = subprocess.Popen([sys.executable, str(SCAN_SCRIPT), "--webull-login"],
                                               cwd=str(REPO_DIR), creationflags=NO_WINDOW,
                                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            messagebox.showerror("ETF Scanner", f"Couldn't start the Webull login: {exc}")
            return
        self.btn_webull.state(["disabled"])
        self.say("Connecting to Webull… if the Webull app shows an OpenAPI login request, approve it "
                 "(waiting up to 5 minutes).", BLUE, 330)
        self.after(1000, self._check_login)

    def _check_login(self):
        if self.login_proc and self.login_proc.poll() is None:
            self.after(1000, self._check_login)
            return
        self.btn_webull.state(["!disabled"])
        r = read_json(WEBULL_LOGIN_FILE)
        if r.get("ok"):
            exp = parse_time(r.get("token_expires"))
            self.say(f"Webull {r.get('message', 'connected')}" + (f"; login valid until {when(exp)}." if exp else "."),
                     GREEN, 60)
        else:
            self.say(f"Webull: {r.get('message') or 'the login did not finish'}", RED, 120)

    def _data_text(self, s: dict) -> tuple[str, str]:
        src, wanted, err = s.get("source"), s.get("requested_source"), s.get("source_error")
        if not src:
            if s.get("scanned_at"):
                return "Yahoo Finance (last scan was made before the Webull switch; restart the scanner to use Webull)", ""
            return "no scan yet", ""
        text = SOURCE_NAMES.get(src, src)
        if wanted == "webull" and err:
            return f"{text} (Webull unavailable: {err[:160]})", RED
        if src == "webull":
            try:
                exp = dt.datetime.fromtimestamp(int(WEBULL_TOKEN_FILE.read_text().splitlines()[1]) / 1000, tz=ET)
                days = (exp - now_et()).total_seconds() / 86400
                text += f"  ·  login valid until {when(exp)}"
                if days < 2:
                    return text + " - press Connect Webull soon to renew it", AMBER
            except (OSError, IndexError, ValueError):
                pass
        return text, ""

    def open_log(self):
        if not LOG_FILE.exists():
            messagebox.showinfo("ETF Scanner", f"No log yet at\n{LOG_FILE}")
            return
        if os.name == "nt":
            os.startfile(LOG_FILE)        # noqa: S606 - opens in the default text editor
        else:
            subprocess.Popen(["xdg-open", str(LOG_FILE)])

    # ---- refresh
    def refresh(self):
        try:
            self.refresh_once()
        finally:
            self.after(REFRESH_MS, self.refresh)

    def refresh_once(self):
        d = self.data = read_json(DASHBOARD_FILE)
        control = read_json(CONTROL_FILE)
        alive = scanner_alive(d)
        want_paused = bool(control.get("paused"))
        if not alive and time.time() - self.task_checked > 15:
            self.task = task_state()
            self.task_checked = time.time()

        hours = d.get("hours", "scan hours")
        interval = d.get("interval_minutes", 15)
        if not alive:
            if not d:
                head, sub, color = "Not running", "The scanner hasn't run on this PC yet. Press Start.", RED
            else:
                last = parse_time(d.get("heartbeat"))
                head, color = "Not running", RED
                sub = f"Last seen {when(last)} ({ago(last)}). Press Start to run it."
                if self.task is None and os.name == "nt":
                    sub += "  The scheduled task isn't installed: run windows\\install.ps1 as Administrator."
                elif self.task == "Running":
                    sub = (f"Windows says the task is running, but the scanner last responded {ago(last)}. "
                           "Check the log.")
        elif d.get("state") == "scanning":
            started = parse_time(d.get("scan_started"))
            head, sub, color = "Scanning now…", f"Started {when(started)}.", BLUE
        elif d.get("paused") or want_paused:
            if want_paused and not d.get("paused"):
                head, sub, color = "Pausing…", "", AMBER
            else:
                head, sub, color = "Paused", "No scans or Slack alerts until you press Start.", AMBER
        else:
            if not want_paused and d.get("paused"):
                head, sub, color = "Resuming…", "", BLUE
            else:
                head, color = "Running", GREEN
                sub = f"Scans every {interval} min during {hours}, Mon-Fri. Alerts go to Slack."
                if d.get("last_result") == "market_closed":
                    sub = f"Outside scan hours ({hours}), waiting. " + sub
        self.status_dot.configure(fg=color)
        self.status_text.configure(text=head, foreground=color)
        self.status_sub.configure(text=sub)

        running = alive and not d.get("paused") and not want_paused
        self.btn_start.state(["disabled"] if running else ["!disabled"])
        self.btn_pause.state(["!disabled"] if alive and not want_paused else ["disabled"])
        self.btn_scan.state(["!disabled"] if alive and d.get("state") != "scanning" else ["disabled"])

        last_check = parse_time(d.get("last_check"))
        result = {"scanned": "scanned", "market_closed": "outside scan hours, skipped",
                  "paused": "paused, skipped", "failed": "FAILED"}.get(d.get("last_result"), "")
        msg = d.get("message") or ""
        self.info["Last check"].configure(
            text=f"{when(last_check)}  ({ago(last_check)})  ·  {msg or result}" if last_check else "never",
            foreground=RED if d.get("last_result") == "failed" else "")
        nxt = parse_time(d.get("next_check"))
        self.info["Next check"].configure(text=when(nxt) if alive and nxt and not d.get("paused") else "—")
        s = d.get("last_scan") or {}
        scanned = parse_time(s.get("scanned_at"))
        if scanned:
            self.info["Last market scan"].configure(text=(
                f"{when(scanned)} ({ago(scanned)})  ·  {s.get('tickers_scanned', 0)} ETFs  ·  "
                f"{s.get('signal_count', 0)} signal(s), {s.get('alert_count', 0) - s.get('signal_count', 0)} watch(es)  ·  "
                f"{s.get('error_count', 0)} error(s)" + ("  ·  from Scan now" if s.get("forced") else "")))
        else:
            self.info["Last market scan"].configure(text="none yet (scans run during market hours or with Scan now)")
        data_text, data_color = self._data_text(s)
        self.info["Data"].configure(text=data_text, foreground=data_color)
        if d:
            self.info["Slack"].configure(
                text="posting alerts and status messages via the saved webhook" if d.get("slack_webhook")
                else "NO webhook saved, so nothing is posted. Run windows\\install.ps1 to add it.",
                foreground="" if d.get("slack_webhook") else RED)
        if self.notice_until and time.time() > self.notice_until:
            self.notice_lbl.configure(text="")
            self.notice_until = 0

        key = (s.get("scanned_at"), s.get("forced"))
        if key != self.shown_scan:
            self.shown_scan = key
            self.fill_tables(s)

    def fill_tables(self, s: dict):
        tfs = s.get("timeframes") or ["1h", "2h", "4h", "1d"]
        if self.all is None or tfs != self.all_timeframes:
            self._build_all_tree(tfs)
        self.sig.delete(*self.sig.get_children())
        self.all.delete(*self.all.get_children())
        rows = s.get("rows") or []
        self.rows_by_ticker = {r["ticker"]: r for r in rows}
        order = {"bull_signal": 0, "bear_signal": 0, "watch": 1, "plain": 2}
        for row in sorted(rows, key=lambda r: (order[row_tag(r)], r["ticker"])):
            tag = row_tag(row)
            for a in row.get("alerts", []):
                on = ", ".join(f"{tf} ({lvl})" for tf, lvl in a["triggers"].items())
                self.sig.insert("", "end", tags=("bull_signal" if a["direction"] == "bull" else "bear_signal")
                                if a["level"] == "SIGNAL" else ("watch",), values=(
                    row["ticker"], "Bullish ▲" if a["direction"] == "bull" else "Bearish ▼",
                    a["level"], "new" if a.get("new") else "", on, ", ".join(a.get("funds") or []) or "none mapped"))
            price = row.get("price")
            self.all.insert("", "end", tags=(tag,), values=(
                row["ticker"], row.get("name", ""), f"${price:,.2f}" if isinstance(price, (int, float)) else "",
                *[tf_cell(row["timeframes"].get(tf)) for tf in tfs], look_at(row)))
        if rows and not self.sig.get_children():
            self.sig.insert("", "end", values=("", "No signals or watches in the last scan.", "", "", "", ""))
        errs = s.get("errors") or []
        if errs:
            shown = "; ".join(f"{e['ticker']}{'/' + e['timeframe'] if e.get('timeframe') else ''}: "
                              f"{e['error'][:80]}" for e in errs[:4])
            more = f" (+{s.get('error_count', len(errs)) - 4} more, see the log)" if s.get("error_count", 0) > 4 else ""
            self.err_lbl.configure(text=f"Errors in the last scan: {shown}{more}")
        else:
            self.err_lbl.configure(text="")


def main() -> int:
    if os.name == "nt":
        try:                                    # crisp text on high-DPI screens
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    Dashboard().mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
