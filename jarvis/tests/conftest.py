import pytest

from jarvis.config import BusinessProfile, ServiceArea, Settings
from jarvis.db import Database

TOKEN = "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
OWNER = 1111


@pytest.fixture
def settings(tmp_path):
    return Settings(
        database_url="sqlite:///:memory:",
        secret_key="s" * 40,
        dashboard_password="correct-horse-battery",
        telegram_bot_token=TOKEN,
        telegram_allowed_user_ids=[OWNER],
        backup_dir=tmp_path / "backups",
        business_profile_path=tmp_path / "missing.toml",
    )


@pytest.fixture
def profile():
    return BusinessProfile(name="Test Haul & Clean", services=["junk_removal", "home_cleaning"],
                           service_area=ServiceArea(zips=["30301", "30302"], cities=["atlanta"]),
                           hours="Mon-Sat", loaded_from="test")


@pytest.fixture
def db():
    d = Database("sqlite:///:memory:")
    d.migrate()
    return d
