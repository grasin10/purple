"""Daily job scheduling with database-backed idempotency.

A job fires at most once per local calendar day: the (job_name, date) pair is
inserted into `scheduled_runs` under a unique constraint *before* the job runs,
so restarts, multiple processes, or retries cannot send a briefing twice.
Jobs only exist when the owner has configured a timezone and a time.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Callable
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError

from .config import HHMM_RE, Settings
from .db import Database, utcnow
from .models import ScheduledRun

log = logging.getLogger(__name__)


@dataclass
class DailyJob:
    name: str
    at: str                       # HH:MM local time
    run: Callable[[], None]


def configured_jobs(settings: Settings, briefing: Callable[[], None], evening: Callable[[], None]) -> list[DailyJob]:
    if not settings.timezone:
        return []
    jobs = []
    if HHMM_RE.match(settings.briefing_time or ""):
        jobs.append(DailyJob("morning_briefing", settings.briefing_time, briefing))
    if HHMM_RE.match(settings.evening_review_time or ""):
        jobs.append(DailyJob("evening_review", settings.evening_review_time, evening))
    return jobs


def _claim(db: Database, job: str, run_key: str) -> int | None:
    try:
        with db.session() as s:
            row = ScheduledRun(job_name=job, run_key=run_key)
            s.add(row)
            s.flush()
            return row.id
    except IntegrityError:
        return None


def run_due_jobs(db: Database, jobs: list[DailyJob], tz_name: str, now_utc: dt.datetime | None = None,
                 max_late_minutes: int = 120) -> list[str]:
    """Run each job whose time passed today (within the lateness window) and hasn't run today."""
    tz = ZoneInfo(tz_name)
    now_utc = now_utc or utcnow()
    local = now_utc.replace(tzinfo=dt.timezone.utc).astimezone(tz)
    ran = []
    for job in jobs:
        hh, mm = map(int, job.at.split(":"))
        late = (local.hour * 60 + local.minute) - (hh * 60 + mm)
        if late < 0 or late > max_late_minutes:
            continue
        run_id = _claim(db, job.name, local.date().isoformat())
        if run_id is None:
            continue
        status, error = "ok", ""
        try:
            job.run()
        except Exception as exc:
            status, error = "failed", type(exc).__name__
            log.exception("scheduled job %s failed", job.name)
        with db.session() as s:
            row = s.get(ScheduledRun, run_id)
            row.status, row.error, row.finished_at = status, error, utcnow()
        ran.append(job.name)
    return ran
