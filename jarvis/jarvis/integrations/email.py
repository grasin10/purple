"""Provider-independent email types and parsing helpers."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser


@dataclass
class EmailMessage:
    message_id: str            # provider id (Gmail message id)
    thread_id: str
    sender_name: str
    sender_email: str
    subject: str
    body: str
    rfc_message_id: str = ""   # Message-ID header
    attachments: list[str] = field(default_factory=list)
    is_bulk: bool = False      # newsletter / automated / no-reply
    bulk_reason: str = ""
    internal_ts: int = 0       # ms since epoch, provider receive time


class EmailError(RuntimeError):
    pass


class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "table"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _TextExtractor()
    p.feed(html)
    text = "".join(p.parts)
    return re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()


_QUOTE_HEADER = re.compile(
    r"^(?:On .{4,200}wrote:\s*$|-{2,}\s*Original Message\s*-{2,}|_{5,}\s*$|From: .+\n(?:Sent|Date): )",
    re.MULTILINE | re.IGNORECASE,
)


def strip_quoted(body: str) -> str:
    """Keep only the new text of a reply (drop quoted history and signatures)."""
    m = _QUOTE_HEADER.search(body)
    if m:
        body = body[: m.start()]
    lines = []
    for line in body.splitlines():
        if line.rstrip() in ("--", "-- "):
            break  # signature delimiter
        if line.lstrip().startswith(">"):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


NOREPLY_RE = re.compile(r"^(?:no[-_.]?reply|do[-_.]?not[-_.]?reply|mailer-daemon|postmaster|notifications?|alerts?|bounce[s]?)\b", re.I)


def bulk_reason(headers: dict[str, str], sender_email: str) -> str:
    """Why a message looks automated/bulk ('' if it looks like a person)."""
    if "list-unsubscribe" in headers or "list-id" in headers:
        return "mailing list / newsletter"
    if headers.get("precedence", "").lower() in ("bulk", "list", "junk"):
        return "bulk precedence"
    auto = headers.get("auto-submitted", "").lower()
    if auto and auto != "no":
        return "auto-submitted"
    if NOREPLY_RE.match(sender_email.split("@", 1)[0]):
        return "no-reply sender"
    return ""
