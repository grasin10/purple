"""Authorization and rate limiting."""
from __future__ import annotations

import time
from collections import defaultdict, deque


def is_authorized_telegram_user(user_id: int | None, chat_type: str | None, allowed: list[int]) -> bool:
    """Only allowlisted user ids, only in private chats.

    Knowing the bot's username is never enough: the numeric Telegram user id
    must be in TELEGRAM_ALLOWED_USER_IDS. Group chats are refused so customer
    data is never posted where other people can read it.
    """
    if user_id is None or not allowed:
        return False
    if chat_type != "private":
        return False
    return user_id in allowed


class RateLimiter:
    """Sliding-window limiter keyed by user id."""

    def __init__(self, max_events: int, window_seconds: float = 60.0, clock=time.monotonic):
        self.max_events = max_events
        self.window = window_seconds
        self.clock = clock
        self._events: dict[object, deque] = defaultdict(deque)

    def allow(self, key: object) -> bool:
        now = self.clock()
        q = self._events[key]
        while q and now - q[0] >= self.window:
            q.popleft()
        if len(q) >= self.max_events:
            return False
        q.append(now)
        return True
