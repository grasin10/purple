"""Structured logging with secret and PII redaction."""
from __future__ import annotations

import logging
import re

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")
_TG_TOKEN_RE = re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{30,}\b")


def redact(text: str, secrets: list[str] | None = None) -> str:
    out = str(text)
    for s in secrets or []:
        if s:
            out = out.replace(s, "[REDACTED]")
    out = _TG_TOKEN_RE.sub("[REDACTED_TOKEN]", out)
    out = _EMAIL_RE.sub("[email]", out)
    out = _PHONE_RE.sub("[phone]", out)
    return out


class RedactingFilter(logging.Filter):
    def __init__(self, secrets: list[str]):
        super().__init__()
        self.secrets = secrets

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage(), self.secrets)
        record.args = ()
        return True


def setup_logging(level: str, secrets: list[str]) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter(secrets))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # httpx logs full request URLs, which contain the Telegram bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
