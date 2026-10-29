"""Integration status registry.

Every module reports its real state. Nothing is reported as connected unless it
has been configured AND a live check succeeded.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..config import TELEGRAM_TOKEN_RE, Settings
from . import krypt


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


def integration_statuses(settings: Settings, live: dict[str, IntegrationStatus] | None = None) -> list[IntegrationStatus]:
    live = live or {}
    tg_state = IntegrationState.CONFIGURED if TELEGRAM_TOKEN_RE.match(settings.telegram_bot_token or "") else IntegrationState.NOT_CONFIGURED
    krypt_db = krypt.resolve_db_path(settings.krypt_db_path)
    statuses = [
        live.get("telegram") or IntegrationStatus(
            "Telegram bot", tg_state,
            "token present (run `jarvis health --online` to verify)" if tg_state == IntegrationState.CONFIGURED else "set TELEGRAM_BOT_TOKEN",
            "1"),
        IntegrationStatus(
            "Krypt Trader (read-only)",
            IntegrationState.CONFIGURED if krypt_db.is_file() else IntegrationState.NOT_CONFIGURED,
            f"reading {krypt_db}" if krypt_db.is_file() else "database not found - open Krypt Trader once or set KRYPT_TRADER_DB",
            "trading"),
        IntegrationStatus("Business email (Gmail)", IntegrationState.NOT_IMPLEMENTED, "adapter interface only; Gmail OAuth is Phase 3", "3"),
        IntegrationStatus("Outbound customer messaging", IntegrationState.NOT_IMPLEMENTED, "draft-only: approved replies are copied/sent by you", "3"),
        IntegrationStatus("Calendar", IntegrationState.NOT_IMPLEMENTED, "not started", "later"),
        IntegrationStatus("Facebook Page / Groups", IntegrationState.NOT_IMPLEMENTED, "Phase 4 - needs Meta app + Page token", "4"),
        IntegrationStatus("Nextdoor", IntegrationState.NOT_IMPLEMENTED, "Phase 4 - manual posting package expected", "4"),
        IntegrationStatus("Canva", IntegrationState.NOT_IMPLEMENTED, "Phase 4 - Canva Connect API (OAuth)", "4"),
        IntegrationStatus("YouTube", IntegrationState.NOT_IMPLEMENTED, "Phase 5", "5"),
        IntegrationStatus("Sports odds data", IntegrationState.NOT_IMPLEMENTED, "Phase 6 - needs a data provider key", "6"),
        IntegrationStatus("Forex market data", IntegrationState.NOT_IMPLEMENTED, "Phase 7 - needs pairs + data provider", "7"),
    ]
    return statuses
