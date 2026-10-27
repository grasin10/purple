"""Email adapter interface (Phase 3 will add a Gmail OAuth implementation).

The interface is defined now so the CRM ingest pipeline and draft workflow are
provider-independent. `NotConfiguredEmail` makes the absence explicit: it never
pretends to fetch or send anything.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class EmailMessage:
    message_id: str
    thread_id: str
    sender_name: str
    sender_email: str
    subject: str
    body: str
    attachments: list[str] = field(default_factory=list)


@dataclass
class SendResult:
    ok: bool
    provider_message_id: str = ""
    error: str = ""


class EmailAdapter(Protocol):
    name: str

    def fetch_new(self, since_marker: str | None) -> tuple[list[EmailMessage], str | None]:
        """Return new messages and an updated marker for incremental sync."""

    def send_reply(self, *, thread_id: str, to: str, subject: str, body: str, idempotency_key: str) -> SendResult:
        """Send a reply. Must return ok=True only when the provider confirms."""


class EmailNotConfigured(RuntimeError):
    pass


class NotConfiguredEmail:
    name = "none"

    def fetch_new(self, since_marker: str | None) -> tuple[list[EmailMessage], str | None]:
        raise EmailNotConfigured("No email provider configured (Phase 3: Gmail OAuth).")

    def send_reply(self, **_kwargs) -> SendResult:
        return SendResult(ok=False, error="No email provider configured - nothing was sent.")
