"""Telegram bot process: long polling + scheduled briefings."""
from __future__ import annotations

import logging
import time

from . import audit
from .briefing import build_briefing, build_evening_review
from .config import BusinessProfile, Settings
from .db import Database
from .integrations.telegram import TelegramClient, TelegramError
from .models import KeyValue
from .router import Context, handle
from .scheduler import configured_jobs, run_due_jobs
from .security import RateLimiter, is_authorized_telegram_user

log = logging.getLogger(__name__)

OFFSET_KEY = "telegram.update_offset"
UNAUTHORIZED_REPLY = "This is a private assistant. You are not authorized to use it."


class Bot:
    def __init__(self, settings: Settings, profile: BusinessProfile, db: Database, client: TelegramClient):
        self.settings = settings
        self.profile = profile
        self.db = db
        self.client = client
        self.limiter = RateLimiter(settings.rate_limit_per_minute)
        self.unauth_limiter = RateLimiter(1, window_seconds=3600)  # at most one refusal per stranger per hour

    # --- persistence of the polling offset (prevents reprocessing after restart)
    def _load_offset(self) -> int | None:
        with self.db.session() as s:
            row = s.get(KeyValue, OFFSET_KEY)
            return int(row.value) if row and row.value else None

    def _save_offset(self, offset: int) -> None:
        with self.db.session() as s:
            row = s.get(KeyValue, OFFSET_KEY)
            if row:
                row.value = str(offset)
            else:
                s.add(KeyValue(key=OFFSET_KEY, value=str(offset)))

    def process_update(self, update: dict) -> str | None:
        """Handle one Telegram update. Returns the reply text sent (or None)."""
        msg = update.get("message") or {}
        text = msg.get("text")
        user = msg.get("from") or {}
        chat = msg.get("chat") or {}
        user_id, chat_id = user.get("id"), chat.get("id")
        if not text or chat_id is None:
            return None

        if not is_authorized_telegram_user(user_id, chat.get("type"), self.settings.telegram_allowed_user_ids):
            with self.db.session() as s:
                audit.record(s, f"telegram:{user_id}", "telegram.unauthorized", result="denied", chat_type=chat.get("type"))
            if chat.get("type") == "private" and self.unauth_limiter.allow(user_id):
                self._send(chat_id, UNAUTHORIZED_REPLY)
                return UNAUTHORIZED_REPLY
            return None

        if not self.limiter.allow(user_id):
            reply = "Rate limit reached - please wait a minute."
            self._send(chat_id, reply)
            return reply

        ctx = Context(db=self.db, settings=self.settings, profile=self.profile, actor=f"telegram:{user_id}",
                      message_ref=f"tg:{chat_id}:{msg.get('message_id')}")
        try:
            reply = handle(ctx, text)
        except Exception:
            log.exception("command failed")
            reply = "Something went wrong handling that request. It has been logged."
        self._send(chat_id, reply)
        return reply

    def _send(self, chat_id: int, text: str) -> bool:
        try:
            self.client.send_message(chat_id, text)
            return True
        except TelegramError as exc:
            log.error("sendMessage failed: %s", exc)
            with self.db.session() as s:
                audit.record(s, "system", "telegram.send", result="failed", error=str(exc))
            return False

    def notify_owner(self, text: str) -> None:
        """Send to every allowlisted user (private chat id == user id)."""
        for uid in self.settings.telegram_allowed_user_ids:
            if self._send(uid, text):
                with self.db.session() as s:
                    audit.record(s, "scheduler", "telegram.notify", f"telegram:{uid}")

    def _scheduled_briefing(self) -> None:
        with self.db.session() as s:
            text = build_briefing(s, self.settings)
        self.notify_owner(text)

    def _scheduled_evening(self) -> None:
        with self.db.session() as s:
            text = build_evening_review(s, self.settings)
        self.notify_owner(text)

    def run_forever(self, poll_timeout: int = 30) -> None:
        jobs = configured_jobs(self.settings, self._scheduled_briefing, self._scheduled_evening)
        log.info("JARVIS bot polling; %d scheduled job(s)", len(jobs))
        offset = self._load_offset()
        backoff = 1.0
        while True:
            if jobs:
                run_due_jobs(self.db, jobs, self.settings.timezone)
            try:
                updates = self.client.get_updates(offset, poll_timeout=poll_timeout)
                backoff = 1.0
            except TelegramError as exc:
                log.warning("getUpdates failed (%s); retrying in %.0fs", exc, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            for update in updates:
                # Advance and persist the offset first so a crash mid-command never replays it.
                offset = int(update["update_id"]) + 1
                self._save_offset(offset)
                self.process_update(update)
