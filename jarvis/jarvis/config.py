"""Configuration: secrets from environment variables, business facts from a TOML profile.

Secrets (tokens, passwords) are only ever read from the environment (or a local
`.env` file that is git-ignored). Business facts (services, service areas,
hours, pricing rules) live in `config/business.toml` so they can be reviewed and
corrected by the owner. Nothing here invents a business fact: missing values
stay empty and are reported by `jarvis check-config`.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

PROJECT_ROOT = Path(__file__).resolve().parent.parent

TELEGRAM_TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")
HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def load_dotenv(path: Path) -> None:
    """Minimal .env loader. Existing environment variables win."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_list(value: str | None) -> list[int]:
    out: list[int] = []
    for part in (value or "").replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.append(int(part))
    return out


@dataclass
class Settings:
    env: str = "development"
    database_url: str = ""
    secret_key: str = ""
    dashboard_password: str = ""
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8000
    telegram_bot_token: str = ""
    telegram_allowed_user_ids: list[int] = field(default_factory=list)
    timezone: str = ""
    briefing_time: str = ""
    evening_review_time: str = ""
    business_profile_path: Path = PROJECT_ROOT / "config" / "business.toml"
    auto_reply_enabled: bool = False
    followup_hours: int = 24
    rate_limit_per_minute: int = 20
    log_level: str = "INFO"
    backup_dir: Path = PROJECT_ROOT / "backups"
    # Email (Gmail OAuth) - file paths only, never passwords.
    gmail_client_secrets_file: str = ""
    gmail_token_file: str = ""
    # Krypt Trader (read-only). Empty path = auto-detect the app's data folder.
    krypt_db_path: str = ""
    krypt_src_dir: Path = PROJECT_ROOT.parent / "python"
    krypt_alerts: bool = True

    @classmethod
    def from_env(cls, load_env_file: bool = True) -> "Settings":
        if load_env_file:
            load_dotenv(PROJECT_ROOT / ".env")
        e = os.environ.get
        data_dir = PROJECT_ROOT / "data"
        return cls(
            env=e("JARVIS_ENV", "development"),
            database_url=e("JARVIS_DATABASE_URL") or f"sqlite:///{data_dir / 'jarvis.db'}",
            secret_key=e("JARVIS_SECRET_KEY", ""),
            dashboard_password=e("JARVIS_DASHBOARD_PASSWORD", ""),
            dashboard_host=e("JARVIS_DASHBOARD_HOST", "127.0.0.1"),
            dashboard_port=int(e("JARVIS_DASHBOARD_PORT", "8000") or 8000),
            telegram_bot_token=e("TELEGRAM_BOT_TOKEN", ""),
            telegram_allowed_user_ids=_int_list(e("TELEGRAM_ALLOWED_USER_IDS")),
            timezone=e("JARVIS_TIMEZONE", ""),
            briefing_time=e("JARVIS_BRIEFING_TIME", ""),
            evening_review_time=e("JARVIS_EVENING_REVIEW_TIME", ""),
            business_profile_path=Path(e("JARVIS_BUSINESS_PROFILE") or PROJECT_ROOT / "config" / "business.toml"),
            auto_reply_enabled=_bool(e("JARVIS_AUTO_REPLY_ENABLED"), False),
            followup_hours=int(e("JARVIS_FOLLOWUP_HOURS", "24") or 24),
            rate_limit_per_minute=int(e("JARVIS_RATE_LIMIT_PER_MINUTE", "20") or 20),
            log_level=e("JARVIS_LOG_LEVEL", "INFO"),
            backup_dir=Path(e("JARVIS_BACKUP_DIR") or PROJECT_ROOT / "backups"),
            gmail_client_secrets_file=e("GMAIL_CLIENT_SECRETS_FILE", ""),
            gmail_token_file=e("GMAIL_TOKEN_FILE", ""),
            krypt_db_path=e("KRYPT_TRADER_DB", ""),
            krypt_src_dir=Path(e("KRYPT_TRADER_SRC") or PROJECT_ROOT.parent / "python"),
            krypt_alerts=_bool(e("JARVIS_KRYPT_ALERTS"), True),
        )

    def secret_values(self) -> list[str]:
        """Values that must never appear in logs or messages."""
        return [v for v in (self.telegram_bot_token, self.secret_key, self.dashboard_password) if v]


@dataclass
class ServiceArea:
    zips: list[str] = field(default_factory=list)
    cities: list[str] = field(default_factory=list)

    @property
    def configured(self) -> bool:
        return bool(self.zips or self.cities)


@dataclass
class BusinessProfile:
    name: str = ""
    phone: str = ""
    email: str = ""
    website: str = ""
    hours: str = ""
    brand_voice: str = ""
    services: list[str] = field(default_factory=list)
    service_area: ServiceArea = field(default_factory=ServiceArea)
    pricing: dict = field(default_factory=dict)
    loaded_from: str = ""

    @classmethod
    def load(cls, path: Path) -> "BusinessProfile":
        if not path.is_file():
            return cls()
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        biz = data.get("business", {})
        area = data.get("service_area", {})
        return cls(
            name=str(biz.get("name", "")).strip(),
            phone=str(biz.get("phone", "")).strip(),
            email=str(biz.get("email", "")).strip(),
            website=str(biz.get("website", "")).strip(),
            hours=str(biz.get("hours", "")).strip(),
            brand_voice=str(biz.get("brand_voice", "")).strip(),
            services=[str(s) for s in data.get("services", {}).get("enabled", [])],
            service_area=ServiceArea(
                zips=[str(z).strip() for z in area.get("zips", [])],
                cities=[str(c).strip().lower() for c in area.get("cities", [])],
            ),
            pricing=data.get("pricing", {}),
            loaded_from=str(path),
        )

    def missing_fields(self) -> list[str]:
        missing = []
        if not self.loaded_from:
            return ["business profile file (copy config/business.example.toml to config/business.toml)"]
        if not self.name:
            missing.append("business.name")
        if not self.hours:
            missing.append("business.hours")
        if not self.services:
            missing.append("services.enabled")
        if not self.service_area.configured:
            missing.append("service_area.zips or service_area.cities")
        for svc in ("junk_removal", "home_cleaning"):
            if not self.pricing.get(svc, {}).get("approved_rules"):
                missing.append(f"pricing.{svc}.approved_rules (quotes will require your approval)")
        return missing


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True


def validate(settings: Settings, profile: BusinessProfile) -> list[Check]:
    """Configuration checks. Details never include secret values."""
    checks: list[Check] = []
    tok = settings.telegram_bot_token
    checks.append(Check(
        "TELEGRAM_BOT_TOKEN",
        bool(tok) and bool(TELEGRAM_TOKEN_RE.match(tok)),
        "set, format looks valid" if tok and TELEGRAM_TOKEN_RE.match(tok)
        else ("set but format looks wrong" if tok else "not set - get one from @BotFather"),
    ))
    ids = settings.telegram_allowed_user_ids
    checks.append(Check(
        "TELEGRAM_ALLOWED_USER_IDS",
        bool(ids),
        f"{len(ids)} authorized user id(s)" if ids else "not set - the bot will reject everyone",
    ))
    checks.append(Check(
        "JARVIS_SECRET_KEY",
        len(settings.secret_key) >= 32,
        "set" if len(settings.secret_key) >= 32 else "missing or shorter than 32 chars",
    ))
    checks.append(Check(
        "JARVIS_DASHBOARD_PASSWORD",
        len(settings.dashboard_password) >= 12,
        "set" if len(settings.dashboard_password) >= 12 else "missing or shorter than 12 chars - dashboard won't start",
    ))
    tz_ok = True
    if settings.timezone:
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(settings.timezone)
        except Exception:
            tz_ok = False
    checks.append(Check(
        "JARVIS_TIMEZONE",
        bool(settings.timezone) and tz_ok,
        settings.timezone if settings.timezone and tz_ok else ("invalid timezone" if settings.timezone else "not set - scheduled briefings disabled"),
        required=False,
    ))
    for key, val in (("JARVIS_BRIEFING_TIME", settings.briefing_time), ("JARVIS_EVENING_REVIEW_TIME", settings.evening_review_time)):
        checks.append(Check(
            key,
            bool(val) and bool(HHMM_RE.match(val)),
            val if val and HHMM_RE.match(val) else ("must be HH:MM" if val else "not set - schedule disabled"),
            required=False,
        ))
    gmail = bool(settings.gmail_client_secrets_file)
    checks.append(Check("GMAIL", False, "adapter not implemented yet (Phase 3)" if gmail else "not configured (Phase 3)", required=False))
    checks.append(Check(
        "JARVIS_AUTO_REPLY_ENABLED",
        True,
        "ENABLED - but no sending integration exists, so nothing is sent" if settings.auto_reply_enabled else "disabled (draft-only mode)",
        required=False,
    ))
    missing = profile.missing_fields()
    checks.append(Check(
        "business profile",
        not missing,
        "complete" if not missing else "missing: " + "; ".join(missing),
        required=False,
    ))
    return checks
