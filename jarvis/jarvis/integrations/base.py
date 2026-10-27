"""Integration status registry.

Every module reports its real state. Nothing is reported as connected unless it
has been configured AND a live check succeeded.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..config import TELEGRAM_TOKEN_RE, Settings


class IntegrationState:
    CONNECTED = "connected"          # configured and a live check passed
    CONFIGURED = "configured"        # credentials present, not verified in this process
    NOT_CONFIGURED = "not_configured"
    NOT_IMPLEMENTED = "not_implemented"
    ERROR = "error"


@dataclass
class IntegrationStatus:
    name: str
    state: str
    detail: str
    phase: str = ""


def gmail_status(settings: Settings, last_sync: str = "") -> IntegrationStatus:
    if not settings.gmail_authorized:
        return IntegrationStatus("Business email (Gmail)", IntegrationState.NOT_CONFIGURED, "run `jarvis gmail-auth` (see README)", "3")
    if last_sync.startswith("ok"):
        return IntegrationStatus("Business email (Gmail)", IntegrationState.CONNECTED, f"last sync {last_sync}", "3")
    if last_sync.startswith("error"):
        return IntegrationStatus("Business email (Gmail)", IntegrationState.ERROR, f"last sync {last_sync}", "3")
    return IntegrationStatus("Business email (Gmail)", IntegrationState.CONFIGURED, "authorized, not synced yet", "3")


def integration_statuses(settings: Settings, live: dict[str, IntegrationStatus] | None = None,
                         gmail_last_sync: str = "") -> list[IntegrationStatus]:
    live = live or {}
    gmail = gmail_status(settings, gmail_last_sync)
    tg_state = IntegrationState.CONFIGURED if TELEGRAM_TOKEN_RE.match(settings.telegram_bot_token or "") else IntegrationState.NOT_CONFIGURED
    statuses = [
        live.get("telegram") or IntegrationStatus(
            "Telegram bot", tg_state,
            "token present (run `jarvis health --online` to verify)" if tg_state == IntegrationState.CONFIGURED else "set TELEGRAM_BOT_TOKEN",
            "1"),
        gmail,
        IntegrationStatus("Customer email replies", gmail.state,
                          ("auto-reply ON for routine first replies" if settings.auto_reply_enabled else "approval required: /approve then /send")
                          if settings.gmail_authorized else "needs Gmail - until then you send replies yourself", "3"),
        IntegrationStatus("SMS / Facebook message replies", IntegrationState.NOT_IMPLEMENTED, "paste these with /inquiry and reply yourself", "later"),
        IntegrationStatus("Calendar", IntegrationState.NOT_IMPLEMENTED, "not started", "later"),
        IntegrationStatus("Facebook Page / Groups", IntegrationState.NOT_IMPLEMENTED, "Phase 4 - needs Meta app + Page token", "4"),
        IntegrationStatus("Nextdoor", IntegrationState.NOT_IMPLEMENTED, "Phase 4 - manual posting package expected", "4"),
        IntegrationStatus("Canva", IntegrationState.NOT_IMPLEMENTED, "Phase 4 - Canva Connect API (OAuth)", "4"),
        IntegrationStatus("YouTube", IntegrationState.NOT_IMPLEMENTED, "Phase 5", "5"),
        IntegrationStatus("Sports odds data", IntegrationState.NOT_IMPLEMENTED, "Phase 6 - needs a data provider key", "6"),
        IntegrationStatus("Forex market data", IntegrationState.NOT_IMPLEMENTED, "Phase 7 - needs pairs + data provider", "7"),
    ]
    return statuses
