import base64
import email
import email.policy

import pytest
from sqlalchemy import text

from jarvis.db import Database
from jarvis.email_workflow import MARKER_KEY, send_draft, sync_gmail, try_auto_reply
from jarvis.integrations.email import EmailError, strip_quoted
from jarvis.integrations.gmail import build_reply, parse_gmail_message
from jarvis.models import Draft, DraftStatus, InboundMessage, KeyValue, Lead, ReviewStatus
from jarvis.router import Context, handle

OWN = "owner@business.com"


def b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def gmsg(mid, body, *, sender="Dana Smith <dana@example.com>", subject="Quote", thread=None,
         html=False, headers=None, attachments=(), ts=1_800_000_000_000):
    hdrs = [{"name": "From", "value": sender}, {"name": "Subject", "value": subject},
            {"name": "Message-ID", "value": f"<{mid}@mail.example.com>"}]
    hdrs += [{"name": k, "value": v} for k, v in (headers or {}).items()]
    parts = [{"mimeType": "text/html" if html else "text/plain", "body": {"data": b64(body)}}]
    parts += [{"mimeType": "image/jpeg", "filename": a, "body": {"attachmentId": "x"}} for a in attachments]
    return {"id": mid, "threadId": thread or f"t-{mid}", "internalDate": str(ts),
            "payload": {"mimeType": "multipart/mixed", "headers": hdrs, "parts": parts}}


class FakeGmail:
    def __init__(self):
        self.inbox, self.sent_box, self.store, self.sent = [], [], {}, []
        self.fail_send = False
        self.fail_list = False

    def add(self, m, box="inbox"):
        self.store[m["id"]] = m
        (self.inbox if box == "inbox" else self.sent_box).insert(0, {"id": m["id"], "threadId": m["threadId"]})

    def profile_email(self):
        return OWN

    def list_ids(self, query, limit):
        if self.fail_list:
            raise EmailError("Gmail API error: 503")
        return list(self.sent_box if "in:sent" in query else self.inbox)[:limit]

    def get(self, mid):
        return self.store[mid]

    def send(self, raw, thread_id):
        if self.fail_send:
            raise EmailError("Gmail API error: timeout")
        mid = f"sent-{len(self.sent) + 1}"
        self.sent.append((email.message_from_bytes(base64.urlsafe_b64decode(raw), policy=email.policy.default), thread_id))
        return {"id": mid, "threadId": thread_id}


@pytest.fixture
def gmail():
    return FakeGmail()


def lead_email(gmail, mid="m1", body="Hi, I need an old couch and a fridge hauled away from 30301 next Friday, photos attached", **kw):
    gmail.add(gmsg(mid, body, **kw))


# ---------------------------------------------------------------- parsing

def test_parse_plain_with_attachment():
    em = parse_gmail_message(gmsg("a", "hello", attachments=["garage.jpg"]))
    assert (em.sender_name, em.sender_email, em.subject) == ("Dana Smith", "dana@example.com", "Quote")
    assert em.body == "hello" and em.attachments == ["garage.jpg"] and em.rfc_message_id == "<a@mail.example.com>"
    assert not em.is_bulk


def test_parse_html_only():
    em = parse_gmail_message(gmsg("a", "<p>Need a <b>deep clean</b></p><style>x{}</style>", html=True))
    assert em.body == "Need a deep clean"


@pytest.mark.parametrize("sender,headers", [
    ("News <news@shop.com>", {"List-Unsubscribe": "<mailto:u@shop.com>"}),
    ("Bot <no-reply@service.com>", {}),
    ("X <x@y.com>", {"Precedence": "bulk"}),
    ("X <x@y.com>", {"Auto-Submitted": "auto-replied"}),
])
def test_bulk_detection(sender, headers):
    assert parse_gmail_message(gmsg("a", "hi", sender=sender, headers=headers)).is_bulk


def test_strip_quoted():
    body = "Yes, ZIP is 30301\n\nOn Mon, Oct 26, 2026 at 9:00 AM Owner <o@b.com> wrote:\n> Hi Dana\n> what ZIP?"
    assert strip_quoted(body) == "Yes, ZIP is 30301"
    assert strip_quoted("Thanks!\n-- \nDana\n555-1234") == "Thanks!"


def test_build_reply_threads_and_blocks_header_injection():
    raw = build_reply(to="dana@example.com", subject="Quote\r\nBcc: evil@x.com", body="Hi", in_reply_to="<m1@x>")
    m = email.message_from_bytes(base64.urlsafe_b64decode(raw))
    assert m["To"] == "dana@example.com" and m["Bcc"] is None
    assert m["Subject"].startswith("Re: Quote") and m["In-Reply-To"] == "<m1@x>" and m["References"] == "<m1@x>"


# ---------------------------------------------------------------- sync

def test_sync_creates_lead_and_is_idempotent(db, settings, profile, gmail):
    lead_email(gmail, attachments=["couch.jpg"])
    r = sync_gmail(db, gmail, settings, profile, now_epoch=2_000_000_000)
    assert len(r.new_leads) == 1 and len(r.drafts) == 1 and not r.errors
    with db.session() as s:
        lead = s.query(Lead).one()
        assert lead.email == "dana@example.com" and lead.source == "gmail" and lead.zip_code == "30301"
        assert lead.attachments == ["gmail:m1/couch.jpg"]
        assert s.get(KeyValue, MARKER_KEY).value == str(2_000_000_000 - 3600)
    r2 = sync_gmail(db, gmail, settings, profile)
    assert r2.fetched == 0 and not r2.has_news
    with db.session() as s:
        assert s.query(Lead).count() == 1 and s.query(Draft).count() == 1


def test_sync_filters_bulk_own_and_unrelated(db, settings, profile, gmail):
    gmail.add(gmsg("n1", "Big sale on couches!", sender="Shop <deals@shop.com>", headers={"List-Unsubscribe": "<x>"}))
    gmail.add(gmsg("o1", "note to self: haul couch", sender=f"Me <{OWN}>"))
    gmail.add(gmsg("p1", "Are we still on for dinner tonight?", sender="Pat <pat@friend.com>", subject="Tonight"))
    r = sync_gmail(db, gmail, settings, profile)
    assert r.new_leads == [] and r.drafts == [] and r.review == []
    with db.session() as s:
        assert s.query(Lead).count() == 0
        assert s.query(InboundMessage).count() == 2  # own message is not stored


def test_sensitive_email_flagged_not_drafted(db, settings, profile, gmail):
    gmail.add(gmsg("c1", "Your crew damaged my floor. I want a refund.", subject="Complaint"))
    r = sync_gmail(db, gmail, settings, profile)
    assert r.sensitive and not r.drafts
    with db.session() as s:
        assert s.query(InboundMessage).one().review_status == ReviewStatus.PENDING


def test_subject_helps_classification(db, settings, profile, gmail):
    gmail.add(gmsg("s1", "Can you come Saturday? 2 bed 1 bath", subject="House cleaning quote"))
    r = sync_gmail(db, gmail, settings, profile)
    assert len(r.new_leads) == 1
    with db.session() as s:
        assert s.query(Lead).one().service_type == "home_cleaning"


def test_thread_reply_attaches_to_existing_lead(db, settings, profile, gmail):
    lead_email(gmail, thread="T1")
    sync_gmail(db, gmail, settings, profile)
    # customer replies from a different address in the same thread
    gmail.add(gmsg("m2", "My zip is 30302\n\nOn Mon wrote:\n> old", sender="Dana <dana.work@example.org>", thread="T1"))
    r = sync_gmail(db, gmail, settings, profile)
    with db.session() as s:
        assert s.query(Lead).count() == 1
        msg = s.query(InboundMessage).filter_by(external_id="m2").one()
        assert msg.lead_id == 1 and msg.body == "My zip is 30302"
    assert r.review  # follow-ups go to you, not to an automatic redraft


def test_list_error_keeps_marker_and_reports(db, settings, profile, gmail):
    gmail.fail_list = True
    r = sync_gmail(db, gmail, settings, profile)
    assert r.errors and r.has_news
    with db.session() as s:
        assert s.get(KeyValue, MARKER_KEY) is None
        assert "error" in s.get(KeyValue, "gmail.last_sync").value


def test_owner_reply_from_gmail_marks_lead_answered(db, settings, profile, gmail):
    lead_email(gmail, thread="T1")
    sync_gmail(db, gmail, settings, profile)
    gmail.add(gmsg("r1", "Sure, $200", sender=f"Me <{OWN}>", thread="T1", ts=1_900_000_000_000), box="sent")
    r = sync_gmail(db, gmail, settings, profile)
    assert r.replies_detected == 1
    with db.session() as s:
        assert not s.query(Lead).one().awaiting_reply
    assert sync_gmail(db, gmail, settings, profile).replies_detected == 0  # idempotent


# ---------------------------------------------------------------- sending

def synced_draft(db, settings, profile, gmail):
    lead_email(gmail, thread="T1")
    sync_gmail(db, gmail, settings, profile)
    with db.session() as s:
        return s.query(Draft).one().id


def approve(db, draft_id):
    with db.session() as s:
        d = s.get(Draft, draft_id)
        d.status = DraftStatus.APPROVED


def test_send_requires_approval_then_threads_reply(db, settings, profile, gmail):
    did = synced_draft(db, settings, profile, gmail)
    assert "only APPROVED" in send_draft(db, gmail, did, "test")
    assert gmail.sent == []
    approve(db, did)
    result = send_draft(db, gmail, did, "test")
    assert result.startswith("Sent") and "sent-1" in result
    msg, thread = gmail.sent[0]
    assert thread == "T1" and msg["To"] == "dana@example.com" and msg["In-Reply-To"] == "<m1@mail.example.com>"
    with db.session() as s:
        d = s.get(Draft, did)
        assert d.status == DraftStatus.SENT and d.provider_message_id == "sent-1"
        assert not d.lead.awaiting_reply
    assert "only APPROVED" in send_draft(db, gmail, did, "test")  # no double send
    assert len(gmail.sent) == 1


def test_concurrent_claim_prevents_double_send(db, settings, profile, gmail):
    did = synced_draft(db, settings, profile, gmail)
    approve(db, did)
    with db.session() as s:  # another sender already claimed it
        s.get(Draft, did).status = DraftStatus.SENDING
    assert "only APPROVED" in send_draft(db, gmail, did, "test")
    assert gmail.sent == []


def test_send_failure_is_reported_and_retryable(db, settings, profile, gmail):
    did = synced_draft(db, settings, profile, gmail)
    approve(db, did)
    gmail.fail_send = True
    result = send_draft(db, gmail, did, "test")
    assert "FAILED" in result and "Sent folder" in result
    with db.session() as s:
        d = s.get(Draft, did)
        assert d.status == DraftStatus.FAILED and "timeout" in d.send_error
        assert d.lead.awaiting_reply
    ctx = Context(db=db, settings=settings, profile=profile, actor="test", email=gmail)
    assert "/send" in handle(ctx, f"/approve {did}")
    gmail.fail_send = False
    assert handle(ctx, f"/send {did}").startswith("Sent")


def test_generated_text_with_price_is_blocked_but_owner_edit_is_allowed(db, settings, profile, gmail):
    did = synced_draft(db, settings, profile, gmail)
    with db.session() as s:
        s.get(Draft, did).body = "That will be $150, guaranteed."
    approve(db, did)
    assert "Refusing" in send_draft(db, gmail, did, "test")
    ctx = Context(db=db, settings=settings, profile=profile, actor="test", email=gmail)
    reply = handle(ctx, f"/edit {did} Hi Dana, for a couch and fridge it's $150. Does Friday work?")
    assert "Heads up" in reply and "PENDING" not in reply
    handle(ctx, f"/approve {did}")
    assert handle(ctx, f"/send {did}").startswith("Sent")
    assert "$150" in gmail.sent[0][0].get_content()


def test_lead_without_email_cannot_be_sent(db, settings, profile, gmail):
    ctx = Context(db=db, settings=settings, profile=profile, actor="test", message_ref="x", email=gmail)
    handle(ctx, "/inquiry contact: 555-201-3344\nNeed an old couch hauled away")
    assert "manualsent" in handle(ctx, "/approve 1")
    assert "no email address" in handle(ctx, "/send 1")
    assert gmail.sent == []


def test_send_without_gmail_is_honest(db, settings, profile):
    ctx = Context(db=db, settings=settings, profile=profile, actor="test", message_ref="y")
    handle(ctx, "/inquiry contact: dana@example.com\nNeed an old couch hauled away")
    assert "Gmail isn't connected" in handle(ctx, "/approve 1")
    assert "nothing was sent" in handle(ctx, "/send 1")


# ---------------------------------------------------------------- auto-reply

def test_auto_reply_off_by_default(db, settings, profile, gmail):
    lead_email(gmail)
    r = sync_gmail(db, gmail, settings, profile)
    assert r.auto_sent == [] and gmail.sent == []


def test_auto_reply_sends_clean_first_contact_and_respects_cap(db, settings, profile, gmail):
    settings.auto_reply_enabled = True
    settings.auto_reply_daily_limit = 1
    lead_email(gmail, "m1")
    lead_email(gmail, "m2", sender="Lee <lee@example.com>", body="Need junk removal for 2 mattresses in 30302, photos attached, Monday")
    r = sync_gmail(db, gmail, settings, profile)
    assert len(r.auto_sent) == 1 and len(gmail.sent) == 1
    with db.session() as s:
        statuses = sorted(d.status for d in s.query(Draft))
    assert statuses == [DraftStatus.PENDING_APPROVAL, DraftStatus.SENT]


def test_auto_reply_skips_drafts_with_notes(db, settings, profile, gmail):
    settings.auto_reply_enabled = True
    lead_email(gmail, body="Need an old couch hauled away from 99999, photos attached, Friday")  # out of area
    r = sync_gmail(db, gmail, settings, profile)
    assert r.auto_sent == [] and gmail.sent == []


def test_auto_reply_requires_business_profile(db, settings, gmail):
    from jarvis.config import BusinessProfile
    settings.auto_reply_enabled = True
    assert "configured" in try_auto_reply(db, gmail, settings, BusinessProfile(), 1, OWN)


# ---------------------------------------------------------------- migration

def test_migration_v1_to_v2_preserves_data(tmp_path):
    url = f"sqlite:///{tmp_path / 'old.db'}"
    db = Database(url)
    db.migrate()
    with db.engine.begin() as c:  # turn it back into a v1 database
        c.execute(text("DROP INDEX ix_inbound_messages_thread_id"))
        for col in ("subject", "thread_id", "rfc_message_id"):
            c.execute(text(f"ALTER TABLE inbound_messages DROP COLUMN {col}"))
        c.execute(text("ALTER TABLE drafts DROP COLUMN send_error"))
        c.execute(text("ALTER TABLE drafts DROP COLUMN edited_by_owner"))
        c.execute(text("DELETE FROM schema_version WHERE version = 2"))
        c.execute(text("INSERT INTO leads (name, contact_method, email, phone, service_type, location, zip_code, source, "
                       "description, attachments, preferred_time, details, stage, quote_status, booking_status, job_outcome, "
                       "created_at, updated_at) VALUES ('Old', '', '', '', 'junk_removal', '', '', 'manual', '', '[]', '', '{}', "
                       "'NEW', 'NONE', 'NONE', '', '2026-01-01', '2026-01-01')"))
    db2 = Database(url)
    assert db2.schema_version() == 1
    assert db2.migrate() == 2
    with db2.session() as s:
        assert s.query(Lead).one().name == "Old"
        assert s.query(InboundMessage).filter_by(thread_id="").count() == 0
