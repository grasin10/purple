import datetime as dt

from conftest import OWNER, TOKEN

from jarvis.bot import UNAUTHORIZED_REPLY, Bot
from jarvis.config import Settings, validate
from jarvis.integrations.telegram import TelegramError, chunk_text
from jarvis.logging_setup import redact
from jarvis.models import AuditLog, Lead, ScheduledRun
from jarvis.scheduler import DailyJob, run_due_jobs
from jarvis.security import RateLimiter, is_authorized_telegram_user


class FakeTelegram:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    def send_message(self, chat_id, text):
        if self.fail:
            raise TelegramError("network down")
        self.sent.append((chat_id, text))
        return [len(self.sent)]


def update(text, user_id=OWNER, chat_type="private", uid=1, mid=10):
    return {"update_id": uid, "message": {"message_id": mid, "text": text, "from": {"id": user_id},
                                          "chat": {"id": user_id, "type": chat_type}}}


def make_bot(settings, profile, db, client=None):
    return Bot(settings, profile, db, client or FakeTelegram())


def test_allowlist():
    assert is_authorized_telegram_user(1, "private", [1])
    assert not is_authorized_telegram_user(2, "private", [1])
    assert not is_authorized_telegram_user(1, "group", [1])
    assert not is_authorized_telegram_user(1, "private", [])
    assert not is_authorized_telegram_user(None, "private", [1])


def test_unauthorized_user_cannot_run_commands(settings, profile, db):
    bot = make_bot(settings, profile, db)
    reply = bot.process_update(update("/inquiry Need junk removal for a couch", user_id=999))
    assert reply == UNAUTHORIZED_REPLY
    # second attempt within the hour gets no reply at all
    assert bot.process_update(update("/leads", user_id=999, uid=2)) is None
    with db.session() as s:
        assert s.query(Lead).count() == 0
        assert s.query(AuditLog).filter_by(action="telegram.unauthorized", result="denied").count() == 2


def test_owner_in_group_chat_is_refused(settings, profile, db):
    bot = make_bot(settings, profile, db)
    assert bot.process_update(update("/leads", chat_type="group")) is None
    assert bot.client.sent == []


def test_authorized_owner_flow(settings, profile, db):
    bot = make_bot(settings, profile, db)
    reply = bot.process_update(update("/inquiry name: Dana\ncontact: dana@example.com\nNeed an old couch hauled away from 30301"))
    assert "JUNK_REMOVAL_LEAD" in reply and "Draft #1" in reply and "not sent" in reply
    # same Telegram message delivered twice -> no duplicate lead
    bot.process_update(update("/inquiry name: Dana\ncontact: dana@example.com\nNeed an old couch hauled away from 30301"))
    with db.session() as s:
        assert s.query(Lead).count() == 1
    assert "Dana" in bot.process_update(update("JARVIS, show me every new junk removal inquiry", mid=11))
    assert "approved" in bot.process_update(update("/approve 1", mid=12))


def test_replies_never_contain_secrets(settings, profile, db):
    bot = make_bot(settings, profile, db)
    for cmd in ["/settings", "/status", "/help", "/briefing", "what integrations aren't connected?"]:
        reply = bot.process_update(update(cmd))
        for secret in settings.secret_values():
            assert secret not in reply


def test_rate_limit(settings, profile, db):
    settings.rate_limit_per_minute = 2
    bot = make_bot(settings, profile, db)
    bot.process_update(update("/help"))
    bot.process_update(update("/help"))
    assert "Rate limit" in bot.process_update(update("/help"))


def test_send_failure_is_logged_not_hidden(settings, profile, db):
    bot = make_bot(settings, profile, db, FakeTelegram(fail=True))
    bot.process_update(update("/help"))
    with db.session() as s:
        assert s.query(AuditLog).filter_by(action="telegram.send", result="failed").count() == 1


def test_rate_limiter_window():
    t = [0.0]
    rl = RateLimiter(2, 60, clock=lambda: t[0])
    assert rl.allow("u") and rl.allow("u") and not rl.allow("u")
    t[0] = 61
    assert rl.allow("u")


def test_redaction():
    out = redact(f"token {TOKEN} email a@b.com phone (555) 201-3344 pw hunter2hunter2", ["hunter2hunter2"])
    assert TOKEN not in out and "a@b.com" not in out and "201-3344" not in out and "hunter2" not in out


def test_validate_never_leaks_secrets(settings, profile):
    for c in validate(settings, profile):
        for secret in settings.secret_values():
            assert secret not in c.detail


def test_validate_flags_missing():
    checks = {c.name: c for c in validate(Settings(), __import__("jarvis.config").config.BusinessProfile())}
    assert not checks["TELEGRAM_BOT_TOKEN"].ok
    assert not checks["TELEGRAM_ALLOWED_USER_IDS"].ok


def test_scheduler_runs_once_per_day(db):
    calls = []
    jobs = [DailyJob("morning_briefing", "07:30", lambda: calls.append(1))]
    tz = "America/New_York"
    before = dt.datetime(2026, 10, 27, 11, 0)   # 07:00 EDT
    at = dt.datetime(2026, 10, 27, 11, 45)      # 07:45 EDT
    late = dt.datetime(2026, 10, 27, 20, 0)     # 16:00 EDT - beyond lateness window
    assert run_due_jobs(db, jobs, tz, before) == []
    assert run_due_jobs(db, jobs, tz, at) == ["morning_briefing"]
    assert run_due_jobs(db, jobs, tz, at) == []          # idempotent
    assert run_due_jobs(db, jobs, tz, dt.datetime(2026, 10, 28, 20, 0)) == []  # missed next day: skipped, not sent late
    assert run_due_jobs(db, jobs, tz, late) == []
    assert calls == [1]
    with db.session() as s:
        assert s.query(ScheduledRun).one().status == "ok"


def test_chunk_text():
    parts = chunk_text("line\n" * 2000, limit=100)
    assert all(len(p) <= 100 for p in parts) and "".join(parts) == "line\n" * 2000
