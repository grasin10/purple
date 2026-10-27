import re

import pytest
from fastapi.testclient import TestClient

from jarvis.config import Settings
from jarvis.web.app import create_app


@pytest.fixture
def client(settings, profile, db):
    return TestClient(create_app(settings, profile, db))


def login(client, pw="correct-horse-battery"):
    return client.post("/login", data={"password": pw}, follow_redirects=False)


def csrf(client, path="/"):
    return re.search(r'name="csrf" value="([0-9a-f]+)"', client.get(path).text).group(1)


def test_refuses_to_start_without_secrets(profile, db):
    with pytest.raises(RuntimeError):
        create_app(Settings(), profile, db)


def test_requires_login(client):
    assert client.get("/healthz").json() == {"ok": True}
    for path in ["/", "/leads", "/review", "/audit"]:
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"


def test_wrong_password_and_lockout(client):
    for _ in range(5):
        assert login(client, "nope").status_code == 401
    assert login(client).status_code == 429


def test_forged_cookie_rejected(client):
    client.cookies.set("jarvis_session", "eyJ1Ijoib3duZXIifQ.forged.sig")
    assert client.get("/", follow_redirects=False).status_code == 303


def test_dashboard_flow_with_csrf(client):
    assert login(client).status_code == 303
    assert "JARVIS business briefing" in client.get("/").text
    token = csrf(client)
    r = client.post("/command", data={"text": "/inquiry Need a deep cleaning for my 3 bed house", "csrf": token})
    assert "HOME_CLEANING_LEAD" in r.text
    assert client.post("/drafts/1/approve", data={"csrf": "bad"}).status_code == 403
    r = client.post("/drafts/1/approve", data={"csrf": token}, follow_redirects=False)
    assert r.status_code == 303
    page = client.get("/leads/1").text
    assert "APPROVED" in page and "I sent it myself" in page
    assert "lead.created" in client.get("/audit").text
    assert client.get("/").headers["X-Frame-Options"] == "DENY"
