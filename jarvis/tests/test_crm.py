import datetime as dt

from jarvis.classify import Category
from jarvis.crm import service as crm
from jarvis.db import utcnow
from jarvis.models import AuditLog, Draft, DraftStatus, InboundMessage, Lead, LeadStage, ReviewStatus


def ingest(s, profile, text, ext="m1", contact="dana@example.com", name="Dana", source="email"):
    return crm.ingest_message(s, source=source, external_id=ext, body=text, sender_name=name,
                              sender_contact=contact, profile=profile)


def test_qualified_lead_creates_lead_and_draft(db, profile):
    with db.session() as s:
        r = ingest(s, profile, "Need an old couch hauled away from 30301")
        assert r.lead and r.draft
        assert r.lead.service_type == "junk_removal"
        assert r.lead.stage == LeadStage.NEEDS_INFORMATION
        assert r.lead.follow_up_at is not None
        assert r.draft.status == DraftStatus.PENDING_APPROVAL
        assert r.lead.awaiting_reply


def test_ingest_is_idempotent(db, profile):
    with db.session() as s:
        ingest(s, profile, "Need an old couch hauled away")
        r2 = ingest(s, profile, "Need an old couch hauled away")
        assert r2.duplicate_message
        assert s.query(Lead).count() == 1
        assert s.query(Draft).count() == 1
        assert s.query(InboundMessage).count() == 1


def test_duplicate_contact_merges_into_open_lead(db, profile):
    with db.session() as s:
        ingest(s, profile, "Need junk removal for a couch", ext="a", contact="Dana@Example.com")
        r = ingest(s, profile, "Also a fridge, my zip is 30301", ext="b", contact="dana@example.com")
        assert s.query(Lead).count() == 1
        assert r.merged_into_existing
        assert r.classification.category == Category.EXISTING_CUSTOMER_REQUEST
        assert r.inbound.review_status == ReviewStatus.PENDING
        lead = s.query(Lead).one()
        assert lead.zip_code == "30301"
        assert "fridge" in lead.details["items"]
        assert len(lead.interactions) == 2


def test_phone_normalization_dedupes():
    assert crm.normalize_phone("+1 (555) 201-3344") == crm.normalize_phone("555.201.3344") == "5552013344"


def test_spam_and_unrelated_create_nothing(db, profile):
    with db.session() as s:
        ingest(s, profile, "rank on google with our SEO, click here", ext="x")
        ingest(s, profile, "are we still on for dinner?", ext="y")
        assert s.query(Lead).count() == 0
        assert s.query(Draft).count() == 0
        assert crm.review_queue(s) == []


def test_sensitive_goes_to_review_without_draft(db, profile):
    with db.session() as s:
        r = ingest(s, profile, "Your crew damaged my wall, I want a refund")
        assert r.draft is None and r.lead is None
        assert [m.id for m in crm.review_queue(s)] == [r.inbound.id]


def test_draft_approval_never_claims_sent(db, profile):
    with db.session() as s:
        r = ingest(s, profile, "Need a house cleaning for my 2 bedroom apartment")
        msg = crm.decide_draft(s, r.draft, True, "test")
        assert "NOT sent" in msg
        assert r.draft.status == DraftStatus.APPROVED
        assert r.lead.last_outbound_at is None
        assert "already" in crm.decide_draft(s, r.draft, False, "test")
        crm.mark_draft_sent_manually(s, r.draft, "test")
        assert r.draft.status == DraftStatus.SENT and not r.lead.awaiting_reply


def test_followups_due_and_stage_change(db, profile):
    with db.session() as s:
        r = ingest(s, profile, "Need a mattress removal")
        assert crm.followups_due(s) == []
        later = utcnow() + dt.timedelta(hours=25)
        assert [x.id for x in crm.followups_due(s, now=later)] == [r.lead.id]
        crm.set_stage(s, r.lead, LeadStage.CLOSED, "test")
        assert crm.followups_due(s, now=later) == []
        assert s.query(AuditLog).filter_by(action="lead.stage_changed").count() == 1


def test_search(db, profile):
    with db.session() as s:
        ingest(s, profile, "Need an old couch hauled away from 30301", ext="1", contact="a@x.com", name="Alice")
        ingest(s, profile, "Need a deep cleaning, 3 bed", ext="2", contact="b@x.com", name="Bob")
        assert [l.name for l in crm.search_leads(s, q="alice")] == ["Alice"]
        assert [l.name for l in crm.search_leads(s, service_type="home_cleaning")] == ["Bob"]
        assert [l.name for l in crm.search_leads(s, q="30301")] == ["Alice"]


def test_audit_redacts_contact_details(db, profile):
    from jarvis import audit
    with db.session() as s:
        audit.record(s, "test", "x", note="call 555-201-3344 or dana@example.com")
        row = s.query(AuditLog).filter_by(action="x").one()
        assert "555" not in row.detail["note"] and "dana@" not in row.detail["note"]
