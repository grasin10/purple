"""CRM operations: ingest messages, dedupe leads, manage stages and drafts."""
from __future__ import annotations

import datetime as dt
import hashlib
import re
from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .. import audit
from ..classify import Category, Classification, classify
from ..config import BusinessProfile
from ..db import utcnow
from ..drafting import draft_reply, extract_details, merge_details, policy_check
from ..models import Draft, DraftStatus, InboundMessage, Interaction, Lead, LeadStage, ReviewStatus

EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+\.[\w.-]+$")


def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else ""


def normalize_contact(raw: str) -> tuple[str, str]:
    """Return (email, phone) from a free-form contact string."""
    raw = (raw or "").strip()
    if EMAIL_RE.match(raw):
        return raw.lower(), ""
    return "", normalize_phone(raw)


def find_open_lead(session: Session, email: str, phone: str) -> Lead | None:
    conds = []
    if email:
        conds.append(Lead.email == email)
    if phone:
        conds.append(Lead.phone == phone)
    if not conds:
        return None
    stmt = (select(Lead).where(or_(*conds), Lead.stage.in_(LeadStage.OPEN))
            .order_by(Lead.created_at.desc()))
    return session.scalars(stmt).first()


def find_thread_lead(session: Session, source: str, thread_id: str) -> Lead | None:
    """Open lead previously linked to this email thread."""
    stmt = (select(Lead).join(InboundMessage, InboundMessage.lead_id == Lead.id)
            .where(InboundMessage.source == source, InboundMessage.thread_id == thread_id,
                   Lead.stage.in_(LeadStage.OPEN))
            .order_by(InboundMessage.received_at.desc()))
    return session.scalars(stmt).first()


@dataclass
class IngestResult:
    inbound: InboundMessage
    classification: Classification
    lead: Lead | None = None
    draft: Draft | None = None
    duplicate_message: bool = False
    merged_into_existing: bool = False
    notes: list[str] | None = None


def ingest_message(session: Session, *, source: str, external_id: str, body: str,
                   sender_name: str = "", sender_contact: str = "", attachments: list | None = None,
                   subject: str = "", thread_id: str = "", rfc_message_id: str = "",
                   profile: BusinessProfile, followup_hours: int = 24, actor: str = "system") -> IngestResult:
    """Classify an inbound message and route it. Idempotent on (source, external_id)."""
    existing_msg = session.scalars(select(InboundMessage).where(
        InboundMessage.source == source, InboundMessage.external_id == external_id)).first()
    if existing_msg:
        lead = session.get(Lead, existing_msg.lead_id) if existing_msg.lead_id else None
        return IngestResult(existing_msg, Classification(existing_msg.category, existing_msg.confidence,
                                                         list(existing_msg.reasons)), lead, duplicate_message=True)

    email, phone = normalize_contact(sender_contact)
    open_lead = find_open_lead(session, email, phone) or (find_thread_lead(session, source, thread_id) if thread_id else None)
    # The subject often carries the request ("Junk removal quote") while the body is short.
    text_for_classifier = f"{subject}\n{body}" if subject else body
    c = classify(text_for_classifier, has_existing_lead=open_lead is not None)

    inbound = InboundMessage(source=source, external_id=external_id, sender_name=sender_name[:200],
                             sender_contact=sender_contact[:320], subject=subject[:500], thread_id=thread_id,
                             rfc_message_id=rfc_message_id[:500], body=body, category=c.category,
                             confidence=c.confidence, reasons=c.reasons,
                             review_status=ReviewStatus.PENDING if c.needs_review else ReviewStatus.NONE)
    session.add(inbound)
    session.flush()
    result = IngestResult(inbound, c, notes=[])
    now = utcnow()

    if c.category in (Category.SPAM, Category.UNRELATED):
        audit.record(session, actor, "inbound.filtered", f"inbound:{inbound.id}", category=c.category)
        return result

    # Attach anything service-related or needing review to the sender's open lead.
    lead = open_lead
    if c.is_lead and lead is None:
        lead = Lead(name=sender_name, email=email, phone=phone,
                    contact_method="email" if email else ("phone" if phone else source),
                    service_type=c.service_type, source=source, description=body[:4000],
                    attachments=list(attachments or []), stage=LeadStage.NEW)
        session.add(lead)
        session.flush()
        audit.record(session, actor, "lead.created", f"lead:{lead.id}", service=c.service_type, source=source)
    elif lead is not None:
        result.merged_into_existing = True

    if lead is None:
        audit.record(session, actor, "inbound.review_queued", f"inbound:{inbound.id}", category=c.category)
        return result

    inbound.lead_id = lead.id
    session.add(Interaction(lead_id=lead.id, direction="in", channel=source, body=body, provider_message_id=external_id))
    lead.last_inbound_at = now
    if attachments and result.merged_into_existing:
        lead.attachments = list(lead.attachments or []) + [a for a in attachments if a not in (lead.attachments or [])]
    details = merge_details(lead.details, extract_details(text_for_classifier, profile))
    lead.details = details
    lead.zip_code = lead.zip_code or details.get("zip_code", "")
    lead.location = lead.location or details.get("city", "")
    lead.preferred_time = lead.preferred_time or details.get("timing", "")
    result.lead = lead

    # Only clear, non-sensitive service inquiries get a drafted reply.
    if not c.is_lead:
        audit.record(session, actor, "inbound.review_queued", f"inbound:{inbound.id}",
                     category=c.category, lead=lead.id)
        return result

    d = draft_reply(name=lead.name, service_type=lead.service_type, details=details,
                    attachments=lead.attachments, profile=profile)
    key = "draft:" + hashlib.sha256(f"{source}:{external_id}".encode()).hexdigest()[:32]
    draft = Draft(lead_id=lead.id, inbound_id=inbound.id, idempotency_key=key, body=d.body,
                  missing_fields=d.missing, policy_issues=d.policy_issues + d.notes)
    session.add(draft)
    lead.stage = LeadStage.NEEDS_INFORMATION if d.missing else LeadStage.READY_FOR_QUOTE
    lead.follow_up_at = now + dt.timedelta(hours=followup_hours)
    session.flush()
    result.draft = draft
    result.notes = d.notes
    audit.record(session, actor, "draft.created", f"draft:{draft.id}", lead=lead.id, missing=d.missing)
    return result


def set_stage(session: Session, lead: Lead, stage: str, actor: str) -> None:
    if stage not in LeadStage.ALL:
        raise ValueError(f"unknown stage {stage}")
    old = lead.stage
    lead.stage = stage
    if stage in (LeadStage.COMPLETED, LeadStage.CLOSED):
        lead.follow_up_at = None
    session.add(Interaction(lead_id=lead.id, direction="note", channel="system", body=f"Stage {old} -> {stage}"))
    audit.record(session, actor, "lead.stage_changed", f"lead:{lead.id}", old=old, new=stage)


def decide_draft(session: Session, draft: Draft, approve: bool, actor: str) -> str:
    """Approve or reject a draft. Approval never sends; sending is a separate explicit step."""
    # A FAILED send can be re-approved (after checking Gmail's Sent folder) to retry.
    if draft.status not in (DraftStatus.PENDING_APPROVAL, DraftStatus.FAILED):
        return f"Draft #{draft.id} is already {draft.status}."
    draft.status = DraftStatus.APPROVED if approve else DraftStatus.REJECTED
    draft.decided_at = utcnow()
    audit.record(session, actor, "draft.approved" if approve else "draft.rejected", f"draft:{draft.id}", lead=draft.lead_id)
    if approve:
        return f"Draft #{draft.id} approved. NOT sent yet."
    return f"Draft #{draft.id} rejected."


def edit_draft(session: Session, draft: Draft, body: str, actor: str) -> str:
    """Replace a draft's text with the owner's wording; it goes back to pending approval."""
    if draft.status not in (DraftStatus.PENDING_APPROVAL, DraftStatus.APPROVED, DraftStatus.FAILED):
        return f"Draft #{draft.id} is {draft.status} and can't be edited."
    body = body.strip()
    if not body:
        return "The new draft text is empty."
    draft.body = body[:5000]
    draft.edited_by_owner = True
    draft.status = DraftStatus.PENDING_APPROVAL
    audit.record(session, actor, "draft.edited", f"draft:{draft.id}", lead=draft.lead_id)
    warnings = policy_check(draft.body)
    note = ("\nHeads up - your text " + "; ".join(warnings) + ". That's your call; it won't be blocked.") if warnings else ""
    return f"Draft #{draft.id} updated:\n\n{draft.body}\n{note}\nApprove with /approve {draft.id}."


def mark_draft_sent_manually(session: Session, draft: Draft, actor: str) -> str:
    if draft.status != DraftStatus.APPROVED:
        return f"Draft #{draft.id} must be APPROVED first (currently {draft.status})."
    draft.status = DraftStatus.SENT
    draft.provider_message_id = "manual"
    lead = draft.lead
    lead.last_outbound_at = utcnow()
    session.add(Interaction(lead_id=lead.id, direction="out", channel="manual", body=draft.body, provider_message_id="manual"))
    audit.record(session, actor, "draft.marked_sent_manually", f"draft:{draft.id}", lead=lead.id)
    return f"Recorded draft #{draft.id} as sent manually by you."


def search_leads(session: Session, *, q: str = "", service_type: str = "", stage: str = "",
                 open_only: bool = False, limit: int = 100) -> list[Lead]:
    stmt = select(Lead)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Lead.name.ilike(like), Lead.email.ilike(like), Lead.phone.ilike(like),
                              Lead.description.ilike(like), Lead.zip_code.ilike(like), Lead.location.ilike(like)))
    if service_type:
        stmt = stmt.where(Lead.service_type == service_type)
    if stage:
        stmt = stmt.where(Lead.stage == stage)
    if open_only:
        stmt = stmt.where(Lead.stage.in_(LeadStage.OPEN))
    return list(session.scalars(stmt.order_by(Lead.created_at.desc()).limit(limit)))


def followups_due(session: Session, now: dt.datetime | None = None) -> list[Lead]:
    now = now or utcnow()
    stmt = (select(Lead).where(Lead.stage.in_(LeadStage.OPEN), Lead.follow_up_at.is_not(None), Lead.follow_up_at <= now)
            .order_by(Lead.follow_up_at))
    return list(session.scalars(stmt))


def unanswered_leads(session: Session) -> list[Lead]:
    return [lead for lead in search_leads(session, open_only=True, limit=500) if lead.awaiting_reply]


def pending_drafts(session: Session) -> list[Draft]:
    return list(session.scalars(select(Draft).where(Draft.status == DraftStatus.PENDING_APPROVAL).order_by(Draft.created_at)))


def review_queue(session: Session) -> list[InboundMessage]:
    return list(session.scalars(select(InboundMessage).where(InboundMessage.review_status == ReviewStatus.PENDING)
                                .order_by(InboundMessage.received_at)))


def resolve_review(session: Session, msg: InboundMessage, actor: str) -> None:
    msg.review_status = ReviewStatus.RESOLVED
    audit.record(session, actor, "inbound.review_resolved", f"inbound:{msg.id}")
