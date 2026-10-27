"""Gmail adapter (official Gmail API + Google OAuth for installed apps).

Scopes are least-privilege: read mail and send mail. JARVIS cannot delete,
label, archive, or change settings. Tokens are stored in GMAIL_TOKEN_FILE
(git-ignored, chmod 600); no password is ever used.
"""
from __future__ import annotations

import base64
import json
import os
from email.message import EmailMessage as MimeMessage
from email.utils import parseaddr
from pathlib import Path
from typing import Protocol

from ..logging_setup import redact
from .email import EmailError, EmailMessage, bulk_reason, html_to_text

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]


class GmailApi(Protocol):
    def profile_email(self) -> str: ...
    def list_ids(self, query: str, limit: int) -> list[dict]: ...   # [{"id", "threadId"}], newest first
    def get(self, message_id: str) -> dict: ...                     # users.messages.get format=full
    def send(self, raw: str, thread_id: str | None) -> dict: ...    # returns {"id", "threadId"}


# ---------------------------------------------------------------- parsing

def _b64(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def _walk(part: dict):
    yield part
    for sub in part.get("parts", []) or []:
        yield from _walk(sub)


def parse_gmail_message(msg: dict) -> EmailMessage:
    payload = msg.get("payload", {}) or {}
    headers: dict[str, str] = {}
    for h in payload.get("headers", []) or []:
        headers.setdefault(h.get("name", "").lower(), h.get("value", ""))
    name, addr = parseaddr(headers.get("from", ""))
    plain, html, attachments = "", "", []
    for part in _walk(payload):
        mime = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data")
        if part.get("filename"):
            attachments.append(part["filename"])
            continue
        if data and mime == "text/plain" and not plain:
            plain = _b64(data)
        elif data and mime == "text/html" and not html:
            html = _b64(data)
    body = plain or (html_to_text(html) if html else "")
    reason = bulk_reason(headers, addr)
    return EmailMessage(
        message_id=msg["id"], thread_id=msg.get("threadId", ""), sender_name=name, sender_email=addr.lower(),
        subject=headers.get("subject", ""), body=body, rfc_message_id=headers.get("message-id", ""),
        attachments=attachments, is_bulk=bool(reason), bulk_reason=reason,
        internal_ts=int(msg.get("internalDate", 0) or 0),
    )


def _header_safe(value: str) -> str:
    return " ".join((value or "").split())  # no CR/LF -> no header injection


def build_reply(*, to: str, subject: str, body: str, in_reply_to: str = "") -> str:
    """Base64url RFC 2822 message for users.messages.send."""
    m = MimeMessage()
    m["To"] = _header_safe(to)
    subj = _header_safe(subject)
    m["Subject"] = subj if subj.lower().startswith("re:") or not in_reply_to else f"Re: {subj}"
    if in_reply_to:
        m["In-Reply-To"] = _header_safe(in_reply_to)
        m["References"] = _header_safe(in_reply_to)
    m.set_content(body)
    return base64.urlsafe_b64encode(m.as_bytes()).decode()


# ---------------------------------------------------------------- real API

class GoogleGmailApi:
    """Thin wrapper over google-api-python-client. Built lazily on first use."""

    def __init__(self, token_file: str):
        self.token_file = token_file
        self._service = None
        self._email = ""

    def _svc(self):
        if self._service is None:
            from googleapiclient.discovery import build
            self._service = build("gmail", "v1", credentials=load_credentials(self.token_file), cache_discovery=False)
        return self._service

    def _exec(self, request):
        try:
            return request.execute(num_retries=2)
        except Exception as exc:
            raise EmailError(redact(f"Gmail API error: {exc}")) from None

    def profile_email(self) -> str:
        if not self._email:
            self._email = self._exec(self._svc().users().getProfile(userId="me"))["emailAddress"].lower()
        return self._email

    def list_ids(self, query: str, limit: int) -> list[dict]:
        out, token = [], None
        while len(out) < limit:
            resp = self._exec(self._svc().users().messages().list(
                userId="me", q=query, maxResults=min(100, limit - len(out)), pageToken=token))
            out += resp.get("messages", [])
            token = resp.get("nextPageToken")
            if not token:
                break
        return out[:limit]

    def get(self, message_id: str) -> dict:
        return self._exec(self._svc().users().messages().get(userId="me", id=message_id, format="full"))

    def send(self, raw: str, thread_id: str | None) -> dict:
        body = {"raw": raw}
        if thread_id:
            body["threadId"] = thread_id
        return self._exec(self._svc().users().messages().send(userId="me", body=body))


def make_gmail(settings) -> "GoogleGmailApi | None":
    """Gmail client if the owner has authorized it, else None."""
    return GoogleGmailApi(settings.gmail_token_file) if settings.gmail_authorized else None


# ---------------------------------------------------------------- OAuth

def _write_private(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)


def load_credentials(token_file: str):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    path = Path(token_file)
    if not path.is_file():
        raise EmailError("Gmail is not authorized yet - run `jarvis gmail-auth`.")
    creds = Credentials.from_authorized_user_file(str(path), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as exc:
                raise EmailError(redact(f"Gmail token refresh failed ({type(exc).__name__}) - run `jarvis gmail-auth` again.")) from None
            _write_private(path, creds.to_json())
        else:
            raise EmailError("Gmail token is invalid - run `jarvis gmail-auth` again.")
    return creds


def run_oauth_flow(client_secrets_file: str, token_file: str) -> list[str]:
    """Open the browser for Google consent and store the token. Returns granted scopes."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not Path(client_secrets_file).is_file():
        raise EmailError(f"OAuth client file not found: {client_secrets_file}")
    flow = InstalledAppFlow.from_client_secrets_file(client_secrets_file, SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True,
                                  authorization_prompt_message="Opening your browser for Google sign-in...\n"
                                                               "If it doesn't open, visit:\n{url}\n")
    granted = sorted(set(creds.scopes or []))
    missing = [s for s in SCOPES if s not in granted]
    if missing:
        raise EmailError("Not all permissions were granted: " + ", ".join(missing))
    _write_private(Path(token_file), creds.to_json())
    return granted


def token_scopes(token_file: str) -> list[str]:
    try:
        return list(json.loads(Path(token_file).read_text(encoding="utf-8")).get("scopes", []))
    except Exception:
        return []
