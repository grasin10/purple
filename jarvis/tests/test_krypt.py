"""Krypt Trader read-only link, tested against Krypt's real schema (python/db.py)."""
import datetime as dt
import importlib.util
import json
import sqlite3

import pytest
from conftest import OWNER

from jarvis.bot import Bot
from jarvis.config import PROJECT_ROOT
from jarvis.integrations import krypt
from jarvis.router import Context, _cmd_krypt, handle, parse

KRYPT_SRC = PROJECT_ROOT.parent / "python"
NOW = dt.datetime(2026, 10, 29, 15, 0, tzinfo=dt.timezone.utc)


def _krypt_schema() -> str:
    spec = importlib.util.spec_from_file_location("krypt_db_for_tests", KRYPT_SRC / "db.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.SCHEMA


def _pos(conn, pid, *, status="filled", resolved=0, correct=None, pnl=None, resolved_at=None, env="demo",
         created="2026-10-29 14:00:00", error=None, filled=3):
    conn.execute(
        "INSERT INTO bot_positions (id, signal_source, signal_id, ticker, title, direction, target_contracts, "
        "limit_price_cents, filled_contracts, cost_usd, client_order_id, status, resolved, outcome_correct, "
        "pnl_usd, kalshi_env, created_at, resolved_at, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid, "whale", pid, f"KX-{pid}", f"Market {pid}", "yes", 3, 40, filled, 1.2, f"c{pid}", status,
         resolved, correct, pnl, env, created, resolved_at, error))


@pytest.fixture
def kdb(tmp_path):
    user_data = tmp_path / "Krypt Trader"
    path = user_data / "data" / "krypt-trader.db"
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(path)
    conn.executescript(_krypt_schema())
    _pos(conn, 1)                                                                         # open
    _pos(conn, 2, resolved=1, correct=1, pnl=1.80, resolved_at="2026-10-29 12:00:00")     # win today
    _pos(conn, 3, resolved=1, correct=0, pnl=-1.20, resolved_at="2026-10-29 13:00:00")    # loss today
    _pos(conn, 4, resolved=1, correct=1, pnl=0.50, resolved_at="2026-10-25 09:00:00")     # win this week
    _pos(conn, 5, resolved=1, correct=1, pnl=9.99, resolved_at="2026-10-29 12:30:00", env="prod")  # other env
    conn.execute("INSERT INTO pnl_snapshots (at, kalshi_env, cash_usd, portfolio_usd, total_usd) "
                 "VALUES ('2026-10-29 14:58:00','demo',90,10,100)")
    conn.execute("INSERT INTO bot_runs (kalshi_env, started_at) VALUES ('demo','2026-10-29T08:00:00Z')")
    conn.commit()
    conn.close()
    (user_data / "settings.json").write_text(json.dumps({"config": {
        "kalshiEnv": "demo", "enableTrading": False, "apiKeyId": "never-read"}}))
    return path


@pytest.fixture
def ksettings(settings, kdb):
    settings.krypt_db_path = str(kdb)
    settings.krypt_src_dir = KRYPT_SRC
    return settings


def test_snapshot_reads_demo_numbers(kdb):
    s = krypt.snapshot(kdb, now=NOW)
    assert s.env == "demo" and s.engine_running
    assert (s.open_positions, s.today_wins, s.today_losses) == (1, 1, 1)
    assert s.today_pnl_usd == pytest.approx(0.60)
    assert (s.week_wins, s.week_pnl_usd) == (2, pytest.approx(1.10))
    assert s.total_usd == 100


def test_switches_only_expose_trading_flags(kdb):
    assert krypt.read_switches(kdb) == {"kalshiEnv": "demo", "enableTrading": False}


def test_engine_not_running_when_snapshot_stale(kdb):
    assert not krypt.snapshot(kdb, now=NOW + dt.timedelta(hours=2)).engine_running


def test_connection_is_read_only(kdb):
    with krypt.connect_ro(kdb) as c, pytest.raises(sqlite3.OperationalError):
        c.execute("UPDATE bot_positions SET status='canceled'")


def test_missing_db_is_reported(tmp_path, settings):
    settings.krypt_db_path = str(tmp_path / "nope.db")
    ctx = Context(db=None, settings=settings, profile=None, actor="t")
    assert "isn't reachable" in _cmd_krypt(ctx, None, "")


def test_krypt_command(db, ksettings, profile):
    reply = handle(Context(db=db, settings=ksettings, profile=profile, actor="t"), "/krypt")
    assert "DEMO" in reply and "auto-trading OFF" in reply and "JARVIS cannot trade" in reply


def test_trades_command(db, ksettings, profile):
    reply = handle(Context(db=db, settings=ksettings, profile=profile, actor="t"), "/trades")
    assert "WON +$1.80" in reply and "LOST -$1.20" in reply


def test_backtest_uses_krypt_backtester_and_warns(db, ksettings, profile):
    reply = handle(Context(db=db, settings=ksettings, profile=profile, actor="t"), "/backtest")
    assert "Resolved signals: 0" in reply and "Not financial advice" in reply
    assert "15-minute" in handle(Context(db=db, settings=ksettings, profile=profile, actor="t"), "/backtest c15")


@pytest.mark.parametrize("text,cmd", [
    ("JARVIS, how is my trading doing?", "krypt"),
    ("what's my P&L", "krypt"),
    ("show my last trades", "trades"),
    ("run a backtest", "backtest"),
    ("backtest the 15 minute crypto", "backtest"),
    ("is my strategy profitable", "backtest"),
])
def test_trading_intents(text, cmd):
    assert parse(text)[0] == cmd


def test_briefing_includes_trading(db, ksettings, profile):
    assert "Trading (Krypt Trader)" in handle(Context(db=db, settings=ksettings, profile=profile, actor="t"), "/briefing")


class FakeTelegram:
    def __init__(self):
        self.sent = []

    def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


def test_alerts_skip_history_then_report_new(db, ksettings, profile, kdb):
    tg = FakeTelegram()
    bot = Bot(ksettings, profile, db, tg)
    assert bot.check_krypt_alerts() == []          # first run: baseline only, no flood
    assert tg.sent == []
    conn = sqlite3.connect(kdb)
    _pos(conn, 6, resolved=1, correct=0, pnl=-2.00, resolved_at="2026-10-29 15:30:00")
    _pos(conn, 7, status="error", error="insufficient balance")
    conn.commit()
    conn.close()
    msgs = bot.check_krypt_alerts()
    assert any("LOSS [demo]" in m and "-$2.00" in m for m in msgs)
    assert any("order error" in m and "insufficient balance" in m for m in msgs)
    assert tg.sent and tg.sent[0][0] == OWNER
    assert bot.check_krypt_alerts() == []          # nothing repeats


def test_alerts_can_be_disabled(db, ksettings, profile):
    ksettings.krypt_alerts = False
    assert Bot(ksettings, profile, db, FakeTelegram()).check_krypt_alerts() == []
