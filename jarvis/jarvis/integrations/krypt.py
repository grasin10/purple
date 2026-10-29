"""Read-only link to Krypt Trader (the Kalshi auto-trader in this repository).

JARVIS only ever *reads* Krypt Trader's local SQLite database and the
non-secret trading switches in its settings.json. The database is opened with
SQLite's `mode=ro`, so no code path here can place, cancel or change a trade,
or flip demo/live. Trading control stays in the Krypt Trader app.

Every query tolerates missing tables/columns, because Krypt Trader may be an
older or newer version than this code was written against.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

APP_DIR_NAME = "Krypt Trader"
DB_RELATIVE = Path("data") / "krypt-trader.db"
OPEN_STATUSES = ("submitted", "partial", "filled")
C15_OPEN_STATUSES = ("submitted", "filled", "exiting")
FRESH_MINUTES = 10  # a P&L snapshot newer than this means the engine is running


class KryptUnavailable(Exception):
    """Krypt Trader's database could not be found or opened."""


def default_user_data_dir() -> Path:
    """Where Electron keeps Krypt Trader's data on this OS."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / APP_DIR_NAME


def resolve_db_path(configured: str = "") -> Path:
    if configured:
        p = Path(configured).expanduser()
        return p / DB_RELATIVE if p.is_dir() else p
    env = os.environ.get("KRYPT_TRADER_USERDATA")
    if env:
        return Path(env) / DB_RELATIVE
    return default_user_data_dir() / DB_RELATIVE


@contextmanager
def connect_ro(db_path: Path) -> Iterator[sqlite3.Connection]:
    if not db_path.is_file():
        raise KryptUnavailable(f"Krypt Trader database not found at {db_path}")
    try:
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as exc:
        raise KryptUnavailable(f"could not open Krypt Trader database: {exc}") from exc
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except sqlite3.OperationalError as exc:
        if "no such" in str(exc):  # table/column missing in this Krypt version
            return []
        raise KryptUnavailable(f"could not read Krypt Trader database: {exc}") from exc


def _one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> dict:
    rows = _rows(conn, sql, params)
    return rows[0] if rows else {}


def read_switches(db_path: Path) -> dict:
    """Non-secret trading switches from settings.json (next to the data folder)."""
    settings_file = db_path.parent.parent / "settings.json"
    try:
        cfg = json.loads(settings_file.read_text(encoding="utf-8")).get("config", {})
    except (OSError, ValueError, AttributeError):
        return {}
    keys = ("kalshiEnv", "enableTrading", "crypto15mEnabled", "crypto15mLive")
    return {k: cfg[k] for k in keys if k in cfg}


def _parse_ts(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        t = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


@dataclass
class KryptSnapshot:
    switches: dict = field(default_factory=dict)
    env: str = "demo"
    engine_running: bool = False
    last_snapshot_at: dt.datetime | None = None
    total_usd: float | None = None
    cash_usd: float | None = None
    open_positions: int = 0
    open_cost_usd: float = 0.0
    today_wins: int = 0
    today_losses: int = 0
    today_pnl_usd: float = 0.0
    week_wins: int = 0
    week_losses: int = 0
    week_pnl_usd: float = 0.0
    c15_open: int = 0
    c15_today_pnl_usd: float = 0.0
    errors_24h: int = 0


def snapshot(db_path: Path, now: dt.datetime | None = None) -> KryptSnapshot:
    now = now or dt.datetime.now(dt.timezone.utc)
    today = now.strftime("%Y-%m-%d")
    week_ago = (now - dt.timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    day_ago = (now - dt.timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    s = KryptSnapshot(switches=read_switches(db_path))
    s.env = str(s.switches.get("kalshiEnv") or "demo")
    with connect_ro(db_path) as c:
        latest = _one(c, "SELECT at, cash_usd, total_usd, kalshi_env FROM pnl_snapshots "
                         "WHERE kalshi_env=? ORDER BY at DESC LIMIT 1", (s.env,))
        if latest:
            s.last_snapshot_at = _parse_ts(latest.get("at"))
            s.cash_usd, s.total_usd = latest.get("cash_usd"), latest.get("total_usd")
        run = _one(c, "SELECT ended_at FROM bot_runs WHERE kalshi_env=? ORDER BY started_at DESC LIMIT 1", (s.env,))
        fresh = bool(s.last_snapshot_at and now - s.last_snapshot_at < dt.timedelta(minutes=FRESH_MINUTES))
        s.engine_running = fresh and bool(run) and not run.get("ended_at")

        ph = ",".join("?" * len(OPEN_STATUSES))
        op = _one(c, f"SELECT COUNT(*) AS n, COALESCE(SUM(cost_usd),0) AS cost FROM bot_positions "
                     f"WHERE resolved=0 AND status IN ({ph}) AND kalshi_env=?", (*OPEN_STATUSES, s.env))
        s.open_positions, s.open_cost_usd = int(op.get("n") or 0), float(op.get("cost") or 0)

        def results(since_sql: str, param: str) -> dict:
            return _one(c, "SELECT SUM(CASE WHEN outcome_correct=1 THEN 1 ELSE 0 END) AS w, "
                           "SUM(CASE WHEN outcome_correct=0 THEN 1 ELSE 0 END) AS l, "
                           "COALESCE(SUM(pnl_usd),0) AS pnl FROM bot_positions "
                           f"WHERE resolved=1 AND filled_contracts>0 AND kalshi_env=? AND {since_sql}",
                        (s.env, param))
        t = results("substr(resolved_at,1,10)=?", today)
        s.today_wins, s.today_losses, s.today_pnl_usd = int(t.get("w") or 0), int(t.get("l") or 0), float(t.get("pnl") or 0)
        w = results("resolved_at>=?", week_ago)
        s.week_wins, s.week_losses, s.week_pnl_usd = int(w.get("w") or 0), int(w.get("l") or 0), float(w.get("pnl") or 0)

        ph15 = ",".join("?" * len(C15_OPEN_STATUSES))
        s.c15_open = int(_one(c, f"SELECT COUNT(*) AS n FROM crypto15m_positions WHERE resolved=0 "
                                 f"AND status IN ({ph15}) AND kalshi_env=?", (*C15_OPEN_STATUSES, s.env)).get("n") or 0)
        s.c15_today_pnl_usd = float(_one(c, "SELECT COALESCE(SUM(pnl_usd),0) AS pnl FROM crypto15m_positions "
                                            "WHERE resolved=1 AND kalshi_env=? AND substr(resolved_at,1,10)=?",
                                         (s.env, today)).get("pnl") or 0)
        s.errors_24h = int(_one(c, "SELECT COUNT(*) AS n FROM bot_positions WHERE status='error' AND created_at>=?",
                                (day_ago,)).get("n") or 0)
    return s


def _money(x: float | None) -> str:
    if x is None:
        return "unknown"
    return f"{'-' if x < 0 else ''}${abs(x):,.2f}"


def _signed(x: float) -> str:
    return f"{'+' if x >= 0 else '-'}${abs(x):,.2f}"


def mode_line(s: KryptSnapshot) -> str:
    env = "LIVE (real money)" if s.env == "prod" else "DEMO (practice money)"
    trading = "ON" if s.switches.get("enableTrading") else "OFF (dry-run, no orders)"
    line = f"Mode: {env} - auto-trading {trading}"
    if s.switches.get("crypto15mEnabled"):
        line += f"; 15-min crypto {'LIVE' if s.switches.get('crypto15mLive') else 'paper'}"
    return line


def format_status(s: KryptSnapshot) -> str:
    if s.engine_running:
        engine = "running"
    elif s.last_snapshot_at:
        engine = f"not running (last update {s.last_snapshot_at:%Y-%m-%d %H:%M} UTC)"
    else:
        engine = "no activity recorded yet"
    rate = lambda w, l: f" ({w / (w + l) * 100:.0f}% win)" if w + l else ""
    lines = [
        "Krypt Trader (read-only view)",
        mode_line(s),
        f"Engine: {engine}",
        f"Balance: {_money(s.total_usd)} total, {_money(s.cash_usd)} cash" if s.total_usd is not None else "Balance: not recorded yet",
        f"Open positions: {s.open_positions} ({_money(s.open_cost_usd)} at risk)",
        f"Today: {s.today_wins}W / {s.today_losses}L{rate(s.today_wins, s.today_losses)}, P&L {_signed(s.today_pnl_usd)}",
        f"Last 7 days: {s.week_wins}W / {s.week_losses}L{rate(s.week_wins, s.week_losses)}, P&L {_signed(s.week_pnl_usd)}",
    ]
    if s.c15_open or s.c15_today_pnl_usd:
        lines.append(f"15-min crypto: {s.c15_open} open, today {_signed(s.c15_today_pnl_usd)}")
    if s.errors_24h:
        lines.append(f"Order errors (24h): {s.errors_24h} - check the Krypt Trader log")
    lines.append("To change settings or stop trading, use the Krypt Trader app. JARVIS cannot trade.")
    return "\n".join(lines)


def briefing_lines(s: KryptSnapshot) -> list[str]:
    return [
        "Trading (Krypt Trader):",
        "  " + mode_line(s),
        f"  Engine {'running' if s.engine_running else 'not running'}; {s.open_positions} open position(s)",
        f"  Today {_signed(s.today_pnl_usd)} ({s.today_wins}W/{s.today_losses}L); 7 days {_signed(s.week_pnl_usd)}",
    ] + ([f"  {s.errors_24h} order error(s) in the last 24h"] if s.errors_24h else [])


def recent_trades(db_path: Path, limit: int = 10) -> str:
    with connect_ro(db_path) as c:
        rows = _rows(c, "SELECT title, ticker, direction, filled_contracts, cost_usd, status, resolved, "
                        "outcome_correct, pnl_usd, kalshi_env, created_at FROM bot_positions "
                        "WHERE status != 'dry_run' ORDER BY created_at DESC LIMIT ?", (limit,))
    if not rows:
        return "No Krypt Trader positions recorded yet."
    out = [f"Last {len(rows)} Krypt Trader positions:"]
    for r in rows:
        if r.get("resolved") and r.get("outcome_correct") is not None:
            result = f"{'WON' if r['outcome_correct'] else 'LOST'} {_signed(float(r.get('pnl_usd') or 0))}"
        else:
            result = str(r.get("status") or "?").upper()
        name = (r.get("title") or r.get("ticker") or "?")[:48]
        env = "" if r.get("kalshi_env") == "prod" else " [demo]"
        out.append(f"- {str(r.get('created_at') or '')[:16]} {str(r.get('direction') or '').upper()} "
                   f"{name} x{r.get('filled_contracts') or 0} ({_money(r.get('cost_usd'))}) -> {result}{env}")
    return "\n".join(out)


# ---- alerts --------------------------------------------------------------

@dataclass
class Watermark:
    resolved_at: str = ""
    pos_id: int = 0
    c15_resolved_at: str = ""
    c15_id: int = 0
    error_id: int = 0

    def dumps(self) -> str:
        return json.dumps(self.__dict__)

    @classmethod
    def loads(cls, raw: str | None) -> "Watermark | None":
        if not raw:
            return None
        try:
            return cls(**json.loads(raw))
        except (ValueError, TypeError):
            return None


def current_watermark(db_path: Path) -> Watermark:
    with connect_ro(db_path) as c:
        p = _one(c, "SELECT resolved_at, id FROM bot_positions WHERE resolved=1 AND resolved_at IS NOT NULL "
                    "ORDER BY resolved_at DESC, id DESC LIMIT 1")
        q = _one(c, "SELECT resolved_at, id FROM crypto15m_positions WHERE resolved=1 AND resolved_at IS NOT NULL "
                    "ORDER BY resolved_at DESC, id DESC LIMIT 1")
        e = _one(c, "SELECT MAX(id) AS id FROM bot_positions WHERE status='error'")
    return Watermark(p.get("resolved_at") or "", int(p.get("id") or 0),
                     q.get("resolved_at") or "", int(q.get("id") or 0), int(e.get("id") or 0))


def new_alerts(db_path: Path, wm: Watermark, limit: int = 20) -> tuple[list[str], Watermark]:
    """Messages for positions settled / errored since the watermark, plus the new watermark."""
    msgs: list[str] = []
    nw = Watermark(**wm.__dict__)
    with connect_ro(db_path) as c:
        for r in _rows(c, "SELECT id, title, ticker, direction, filled_contracts, outcome_correct, pnl_usd, "
                          "kalshi_env, resolved_at FROM bot_positions WHERE resolved=1 AND resolved_at IS NOT NULL "
                          "AND (resolved_at>? OR (resolved_at=? AND id>?)) ORDER BY resolved_at, id LIMIT ?",
                       (wm.resolved_at, wm.resolved_at, wm.pos_id, limit)):
            nw.resolved_at, nw.pos_id = r["resolved_at"], int(r["id"])
            if not r.get("filled_contracts") or r.get("outcome_correct") is None:
                continue
            tag = "" if r.get("kalshi_env") == "prod" else " [demo]"
            msgs.append(f"Krypt {'WIN' if r['outcome_correct'] else 'LOSS'}{tag}: "
                        f"{(r.get('title') or r.get('ticker') or '?')[:60]} "
                        f"({str(r.get('direction') or '').upper()} x{r['filled_contracts']}) "
                        f"{_signed(float(r.get('pnl_usd') or 0))}")
        for r in _rows(c, "SELECT id, asset, side, outcome_correct, pnl_usd, exit_reason, kalshi_env, dry_run, "
                          "resolved_at FROM crypto15m_positions WHERE resolved=1 AND resolved_at IS NOT NULL "
                          "AND (resolved_at>? OR (resolved_at=? AND id>?)) ORDER BY resolved_at, id LIMIT ?",
                       (wm.c15_resolved_at, wm.c15_resolved_at, wm.c15_id, limit)):
            nw.c15_resolved_at, nw.c15_id = r["resolved_at"], int(r["id"])
            if r.get("pnl_usd") is None:
                continue
            tag = " [paper]" if r.get("dry_run") else ("" if r.get("kalshi_env") == "prod" else " [demo]")
            reason = " (stop-loss)" if r.get("exit_reason") == "stop_loss" else ""
            msgs.append(f"Krypt 15-min {r.get('asset') or '?'} {str(r.get('side') or '').upper()}{tag}: "
                        f"{_signed(float(r['pnl_usd']))}{reason}")
        for r in _rows(c, "SELECT id, ticker, error FROM bot_positions WHERE status='error' AND id>? "
                          "ORDER BY id LIMIT ?", (wm.error_id, limit)):
            nw.error_id = int(r["id"])
            msgs.append(f"Krypt order error on {r.get('ticker') or '?'}: {str(r.get('error') or 'unknown')[:160]}")
    return msgs, nw


# ---- backtest (uses Krypt Trader's own fee-aware backtester) --------------

def _load_backtest_module(src_dir: Path):
    path = src_dir / "backtest.py"
    if not path.is_file():
        raise KryptUnavailable(f"Krypt Trader backtester not found at {path}")
    spec = importlib.util.spec_from_file_location("krypt_backtest", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


BACKTEST_CAVEAT = ("These are past results on a small sample (in-sample), not proof of future profit. "
                   "Treat a positive result as 'worth paper-testing', never as a reason to go live. Not financial advice.")


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def _cents(x: float) -> str:
    return f"{'+' if x >= 0 else '-'}{abs(x) * 100:.1f}c"


def run_backtest(db_path: Path, src_dir: Path, crypto15m: bool = False) -> str:
    bt = _load_backtest_module(src_dir)
    with connect_ro(db_path) as c:
        signals = bt.load_crypto15m_signals(c) if crypto15m else bt.load_resolved_signals(c)
    if crypto15m:
        rep = bt.crypto15m_report(signals)
        lines = ["Backtest: 15-minute crypto strategy", f"Settled markets analysed: {len(signals)}"]
        fav = rep.get("favorite") or {}
        if fav.get("n"):
            lines.append(f"Buying the favourite: {_pct(fav['win_rate'])} win rate, "
                         f"{_cents(fav['net_ev'])} per contract after fees (n={fav['n']})")
        lines.append("Verdict: " + str(rep.get("verdict", "")))
    else:
        rep = bt.build_report(signals, bt.DEFAULT_FEE_COEFF)
        o = rep["overall"]
        lines = ["Backtest: whale + momentum signals", f"Resolved signals: {o['n']}"]
        if o["n"]:
            lines.append(f"Overall: {_pct(o['win_rate'])} win rate, {_cents(o['net_ev'])} per contract after fees "
                         f"(fees cost {_cents(-o['fee_ev'])} each)")
            for src in ("whale", "momentum"):
                b = rep["by_source"][src]
                if b["n"]:
                    lines.append(f"  {src.title()}: {_pct(b['win_rate'])} win, {_cents(b['net_ev'])}/contract (n={b['n']})")
            ok = [r for r in rep["sweep"] if r["n"] >= 30]
            if ok:
                best = max(ok, key=lambda r: r["net_ev"])
                lines.append(f"Best confidence filter with 30+ trades: >= {best['threshold']:.0f} "
                             f"-> {_cents(best['net_ev'])}/contract (n={best['n']})")
        lines.append("Verdict: " + rep["verdict"])
    lines += ["", BACKTEST_CAVEAT]
    return "\n".join(lines)
