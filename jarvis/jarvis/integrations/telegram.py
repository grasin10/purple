"""Minimal Telegram Bot API client (long polling) over httpx.

Uses only documented Bot API methods: getMe, getUpdates, sendMessage.
The bot token is part of the request URL, so httpx request logging is turned
down in logging_setup and errors are redacted before they are logged.
"""
from __future__ import annotations

import logging

import httpx

from ..logging_setup import redact

log = logging.getLogger(__name__)

MAX_MESSAGE_LEN = 4000  # Telegram limit is 4096; leave headroom.


def chunk_text(text: str, limit: int = MAX_MESSAGE_LEN) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)
    return chunks


class TelegramError(RuntimeError):
    pass


class TelegramClient:
    def __init__(self, token: str, timeout: float = 40.0, client: httpx.Client | None = None):
        self._token = token
        self._base = f"https://api.telegram.org/bot{token}"
        self._http = client or httpx.Client(timeout=timeout)

    def _call(self, method: str, payload: dict | None = None, timeout: float | None = None) -> dict | list:
        try:
            resp = self._http.post(f"{self._base}/{method}", json=payload or {}, timeout=timeout)
            data = resp.json()
        except Exception as exc:  # network / decode errors
            raise TelegramError(redact(f"{method} failed: {exc}", [self._token])) from None
        if not data.get("ok"):
            raise TelegramError(redact(f"{method} error: {data.get('description', 'unknown')}", [self._token]))
        return data["result"]

    def get_me(self) -> dict:
        return self._call("getMe")  # type: ignore[return-value]

    def get_updates(self, offset: int | None, poll_timeout: int = 30) -> list[dict]:
        payload = {"timeout": poll_timeout, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        return self._call("getUpdates", payload, timeout=poll_timeout + 10)  # type: ignore[return-value]

    def send_message(self, chat_id: int, text: str) -> list[int]:
        """Send text (chunked). Returns Telegram message ids confirmed by the API."""
        ids = []
        for part in chunk_text(text):
            result = self._call("sendMessage", {"chat_id": chat_id, "text": part, "disable_web_page_preview": True})
            ids.append(int(result["message_id"]))  # type: ignore[index]
        return ids
