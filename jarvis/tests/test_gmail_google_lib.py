"""Exercise the real Google client libraries offline (no network, no browser)."""
import datetime as dt
import json
import os

from googleapiclient.discovery import build
from googleapiclient.http import HttpMockSequence

from jarvis.integrations.gmail import SCOPES, GoogleGmailApi, build_reply, load_credentials, token_scopes


def api_with(responses):
    api = GoogleGmailApi("unused")
    api._service = build("gmail", "v1", http=HttpMockSequence(responses), static_discovery=True)
    return api


def test_real_client_list_get_send_requests():
    seq = [
        ({"status": "200"}, json.dumps({"emailAddress": "Owner@Business.com"})),
        ({"status": "200"}, json.dumps({"messages": [{"id": "a", "threadId": "t"}], "nextPageToken": "p2"})),
        ({"status": "200"}, json.dumps({"messages": [{"id": "b", "threadId": "t"}]})),
        ({"status": "200"}, json.dumps({"id": "a", "threadId": "t", "payload": {"headers": []}})),
        ({"status": "200"}, json.dumps({"id": "sent1", "threadId": "t"})),
    ]
    api = api_with(seq)
    assert api.profile_email() == "owner@business.com"
    assert [m["id"] for m in api.list_ids("in:inbox after:1", 50)] == ["a", "b"]  # follows pagination
    assert api.get("a")["id"] == "a"
    assert api.send(build_reply(to="d@example.com", subject="Hi", body="x"), "t")["id"] == "sent1"


def test_real_client_errors_are_wrapped_and_redacted():
    api = api_with([({"status": "403"}, json.dumps({"error": {"message": "Insufficient Permission for dana@example.com"}}))] * 3)
    try:
        api.profile_email()
        raise AssertionError("expected EmailError")
    except Exception as exc:
        assert type(exc).__name__ == "EmailError"
        assert "dana@example.com" not in str(exc)


def test_oauth_flow_requests_only_least_privilege_scopes(tmp_path):
    from google_auth_oauthlib.flow import InstalledAppFlow
    client = tmp_path / "client.json"
    client.write_text(json.dumps({"installed": {
        "client_id": "123.apps.googleusercontent.com", "client_secret": "x", "redirect_uris": ["http://localhost"],
        "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token"}}))
    flow = InstalledAppFlow.from_client_secrets_file(str(client), SCOPES)
    flow.redirect_uri = "http://localhost:1"
    url, _ = flow.authorization_url()
    assert "gmail.readonly" in url and "gmail.send" in url
    assert "gmail.modify" not in url and "mail.google.com" not in url


def test_load_valid_token(tmp_path):
    token = tmp_path / "token.json"
    expiry = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).replace(tzinfo=None).isoformat() + "Z"
    token.write_text(json.dumps({"token": "abc", "refresh_token": "r", "client_id": "c", "client_secret": "s",
                                 "token_uri": "https://oauth2.googleapis.com/token", "scopes": SCOPES, "expiry": expiry}))
    assert load_credentials(str(token)).valid
    assert token_scopes(str(token)) == SCOPES


def test_missing_token_gives_instructions(tmp_path):
    try:
        load_credentials(str(tmp_path / "nope.json"))
    except Exception as exc:
        assert "jarvis gmail-auth" in str(exc)


def test_private_token_file_permissions(tmp_path):
    from jarvis.integrations.gmail import _write_private
    p = tmp_path / "secrets" / "t.json"
    _write_private(p, "{}")
    if os.name == "posix":
        assert oct(p.stat().st_mode & 0o777) == "0o600"
