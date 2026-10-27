"""Gmail workflows: inbox sync, sent-mail tracking, sending approved drafts, auto-reply.

Guarantees:
- Each Gmail message is processed once (unique source+message id).
- A draft is sent at most once: APPROVED -> SENDING is a conditional UPDATE,
  so two callers can never both send it.
- A draft is reported SENT only when Gmail returns a message id.
- Replies only go to the address the customer wrote from (or the email you
  entered on the lead) - never to an address found inside a message body.
- Auto-reply is off unless JARVIS_AUTO_REPLY_ENABLED=true, and even then only
  for first-contact, high-confidence leads whose draft has no policy notes,
  capped per day.
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from dataclasses import dataclass, field

from sqlalchemy import func, select, update

from . import audit
from .classify import Category
from .config import BusinessProfile, Settings
from .crm import service as crm
from .crm.service import EMAIL_RE
from .db import Database, utcnow
from .drafting import SERVICE_LABEL, policy_check
from .integrations.email import EmailError, strip_quoted
from .integrations.gmail import GmailApi, build_reply, parse_gmail_message
from .logging_setup import redact
from .models import AuditLog, Draft, DraftStatus, InboundMessage, Interaction, KeyValue, Lead, ReviewStatus

log = logging.getLogger(__name__)

SOURCE = "gmail"
MARKER_KEY = "gmail.sync_after"
STATUS_KEY = "gmail.last_sync"
AUTO_REPLY_MIN_CONFIDENCE = 0.9


@dataclass
class SyncReport:
    fetched: int = 0
    new_leads: list[int] = field(default_factory=list)          # lead ids
    drafts: list[int] = field(default_factory=list)             # draft ids
    review: list[int] = field(default_factory=list)             # inbound ids needing review
    sensitive: list[int] = field(default_factory=list)          # inbound ids (subset of review)
    filtered: int = 0
    replies_detected: int = 0
    auto_sent: list[int] = field(default_factory=list)          # draft ids
    errors: list[str] = field(default_factory=list)

    @property
    def has_news(self) -> bool:
        return bool(self.new_leads or self.review or self.auto_sent or self.errors)


def _kv_get(db: Database, key: str) -> str:
    with db.session() as s:
        row = s.get(KeyValue, key)
        return row.value if row else ""


def _kv_set(db: Database, key: str, value: str) -> None:
    with db.session() as s:
        row = s.get(KeyValue, key)
        if row:
            row.value = value
        else:
            s.add(KeyValue(key=key, value=value))


def last_sync_status(db: Database) -> str:
    return _kv_get(db, STATUS_KEY)


def _already_seen(db: Database, message_id: str) -> bool:
    with db.session() as s:
        return s.scalar(select(func.count()).select_from(InboundMessage).where(
            InboundMessage.source == SOURCE, InboundMessage.external_id == message_id)) > 0


def sync_gmail(db: Database, api: GmailApi, settings: Settings, profile: BusinessProfile,
               now_epoch: int | None = None) -> SyncReport:
    report = SyncReport()
    now_epoch = now_epoch or int(time.time())
    marker = _kv_get(db, MARKER_KEY)
    after = int(marker) if marker.isdigit() else now_epoch - settings.gmail_lookback_days * 86400
    try:
        own = api.profile_email()
        inbox = api.list_ids(f"in:inbox -category:promotions -category:social after:{after}", settings.gmail_max_per_sync)
    except EmailError as exc:
        report.errors.append(str(exc))
        _record_status(db, report)
        return report

    for ref in reversed(inbox):  # oldest first
        mid = ref["id"]
        if _already_seen(db, mid):
            continue
        try:
            em = parse_gmail_message(api.get(mid))
        except EmailError as exc:
            report.errors.append(str(exc))
            continue
        report.fetched += 1
        if em.sender_email == own:
            continue
        with db.session() as s:
            if em.is_bulk or not EMAIL_RE.match(em.sender_email or ""):
                s.add(InboundMessage(source=SOURCE, external_id=mid, sender_name=em.sender_name[:200],
                                     sender_contact=em.sender_email[:320], subject=em.subject[:500],
                                     thread_id=em.thread_id, body=em.body[:2000], category=Category.UNRELATED,
                                     confidence=1.0, reasons=[f"automated email: {em.bulk_reason or 'no valid sender'}"]))
                report.filtered += 1
                continue
            attachments = [f"gmail:{mid}/{name}" for name in em.attachments]
            r = crm.ingest_message(s, source=SOURCE, external_id=mid, body=strip_quoted(em.body) or em.subject,
                                   sender_name=em.sender_name, sender_contact=em.sender_email,
                                   attachments=attachments, subject=em.subject, thread_id=em.thread_id,
                                   rfc_message_id=em.rfc_message_id, profile=profile,
                                   followup_hours=settings.followup_hours, actor="gmail-sync")
            if r.lead and not r.merged_into_existing:
                report.new_leads.append(r.lead.id)
            if r.draft:
                report.drafts.append(r.draft.id)
            if r.inbound.review_status == ReviewStatus.PENDING:
                report.review.append(r.inbound.id)
                if r.classification.category == Category.SENSITIVE:
                    report.sensitive.append(r.inbound.id)
            if r.classification.category in (Category.SPAM, Category.UNRELATED):
                report.filtered += 1
            auto_candidate = (r.draft.id if r.draft and not r.merged_into_existing
                              and r.classification.confidence >= AUTO_REPLY_MIN_CONFIDENCE else None)
        if auto_candidate and settings.auto_reply_enabled:
            outcome = try_auto_reply(db, api, settings, profile, auto_candidate, own)
            if outcome == "sent":
                report.auto_sent.append(auto_candidate)

    try:
        report.replies_detected = _detect_owner_replies(db, api, after, settings.gmail_max_per_sync)
    except EmailError as exc:
        report.errors.append(str(exc))

    if not report.errors:
        # 1h overlap; duplicates are skipped by message id.
        _kv_set(db, MARKER_KEY, str(now_epoch - 3600))
    _record_status(db, report)
    return report


def _record_status(db: Database, report: SyncReport) -> None:
    stamp = utcnow().strftime("%Y-%m-%d %H:%M UTC")
    status = f"error at {stamp}: {report.errors[0][:200]}" if report.errors else f"ok at {stamp}"
    _kv_set(db, STATUS_KEY, status)
    with db.session() as s:
        audit.record(s, "gmail-sync", "gmail.sync", result="failed" if report.errors else "ok",
                     fetched=report.fetched, leads=len(report.new_leads), review=len(report.review),
                     errors=report.errors[:3])


def _detect_owner_replies(db: Database, api: GmailApi, after: int, limit: int) -> int:
    """Record replies you sent from Gmail yourself, so leads don't look unanswered."""
    found = 0
    for ref in api.list_ids(f"in:sent after:{after}", limit):
        with db.session() as s:
            if s.scalar(select(func.count()).select_from(Interaction).where(
                    Interaction.channel == SOURCE, Interaction.provider_message_id == ref["id"])):
                continue
            lead = crm.find_thread_lead(s, SOURCE, ref.get("threadId", ""))
        if not lead:
            continue
        em = parse_gmail_message(api.get(ref["id"]))
        sent_at = dt.datetime.fromtimestamp(em.internal_ts / 1000, dt.timezone.utc).replace(tzinfo=None) if em.internal_ts else utcnow()
        with db.session() as s:
            lead = s.get(Lead, lead.id)
            s.add(Interaction(lead_id=lead.id, direction="out", channel=SOURCE, body=strip_quoted(em.body),
                              provider_message_id=ref["id"], created_at=sent_at))
            if not lead.last_outbound_at or lead.last_outbound_at < sent_at:
                lead.last_outbound_at = sent_at
            audit.record(s, "gmail-sync", "gmail.owner_reply_detected", f"lead:{lead.id}")
        found += 1
    return found


# ---------------------------------------------------------------- sending

def _reply_target(s, draft: Draft) -> InboundMessage | None:
    if draft.inbound_id:
        msg = s.get(InboundMessage, draft.inbound_id)
        if msg and msg.source == SOURCE and msg.thread_id:
            return msg
    return s.scalars(select(InboundMessage).where(InboundMessage.lead_id == draft.lead_id,
                                                  InboundMessage.source == SOURCE, InboundMessage.thread_id != "")
                     .order_by(InboundMessage.received_at.desc())).first()


def send_draft(db: Database, api: GmailApi, draft_id: int, actor: str, own_email: str = "") -> str:
    """Send an APPROVED draft via Gmail. Returns a human-readable result."""
    with db.session() as s:
        draft = s.get(Draft, draft_id)
        if not draft:
            return "Draft not found."
        if draft.status != DraftStatus.APPROVED:
            return f"Draft #{draft_id} is {draft.status}; only APPROVED drafts can be sent (use /approve {draft_id})."
        lead = draft.lead
        to = (lead.email or "").strip().lower()
        if not EMAIL_RE.match(to):
            return f"Lead #{lead.id} has no email address - send this one yourself, then /manualsent {draft_id}."
        if own_email and to == own_email.lower():
            return "Refusing to send: the recipient is your own address."
        violations = [] if draft.edited_by_owner else policy_check(draft.body)
        if violations:
            return f"Refusing to send draft #{draft_id}: it " + "; ".join(violations) + ". Edit it with /edit."
        target = _reply_target(s, draft)
        subject = target.subject if target and target.subject else f"Your {SERVICE_LABEL.get(lead.service_type, 'service')} request"
        raw = build_reply(to=to, subject=subject, body=draft.body, in_reply_to=target.rfc_message_id if target else "")
        thread_id = target.thread_id if target else None

    # Claim the draft atomically so concurrent senders can't double-send.
    with db.session() as s:
        claimed = s.execute(update(Draft).where(Draft.id == draft_id, Draft.status == DraftStatus.APPROVED)
                            .values(status=DraftStatus.SENDING)).rowcount
    if claimed != 1:
        return f"Draft #{draft_id} is already being sent."

    try:
        resp = api.send(raw, thread_id)
        gmail_id = str(resp.get("id") or "")
        if not gmail_id:
            raise EmailError("Gmail did not return a message id")
    except Exception as exc:
        err = redact(str(exc))[:300]
        with db.session() as s:
            d = s.get(Draft, draft_id)
            d.status, d.send_error = DraftStatus.FAILED, err
            audit.record(s, actor, "draft.send", f"draft:{draft_id}", result="failed", error=err)
        return (f"Send FAILED for draft #{draft_id}: {err}\n"
                f"Check Gmail's Sent folder before retrying. To retry: /approve {draft_id} then /send {draft_id}.")

    with db.session() as s:
        d = s.get(Draft, draft_id)
        d.status, d.provider_message_id, d.send_error = DraftStatus.SENT, gmail_id, ""
        lead = d.lead
        lead.last_outbound_at = utcnow()
        s.add(Interaction(lead_id=lead.id, direction="out", channel=SOURCE, body=d.body, provider_message_id=gmail_id))
        audit.record(s, actor, "draft.sent", f"draft:{draft_id}", lead=lead.id, gmail_id=gmail_id)
    return f"Sent draft #{draft_id} to the customer via Gmail (Gmail message id {gmail_id})."


def auto_replies_last_24h(db: Database) -> int:
    with db.session() as s:
        return s.scalar(select(func.count()).select_from(AuditLog).where(
            AuditLog.action == "draft.auto_approved", AuditLog.ts >= utcnow() - dt.timedelta(hours=24))) or 0


def try_auto_reply(db: Database, api: GmailApi, settings: Settings, profile: BusinessProfile,
                   draft_id: int, own_email: str) -> str:
    """Auto-send a routine first reply if every gate passes. Returns 'sent' or the reason it didn't."""
    if not settings.auto_reply_enabled:
        return "disabled"
    if not profile.name or not profile.service_area.configured:
        return "business name and service area must be configured"
    if auto_replies_last_24h(db) >= settings.auto_reply_daily_limit:
        return "daily auto-reply limit reached"
    with db.session() as s:
        draft = s.get(Draft, draft_id)
        if not draft or draft.status != DraftStatus.PENDING_APPROVAL:
            return "draft not pending"
        if draft.policy_issues:
            return "draft has notes that need your review"
        if len(draft.lead.interactions) != 1 or draft.lead.last_outbound_at:
            return "not a first contact"
        draft.status, draft.decided_at = DraftStatus.APPROVED, utcnow()
        audit.record(s, "auto-reply", "draft.auto_approved", f"draft:{draft_id}", lead=draft.lead_id)
    result = send_draft(db, api, draft_id, "auto-reply", own_email)
    return "sent" if result.startswith("Sent") else result


def format_report(report: SyncReport) -> str:
    lines = ["New email activity:"]
    if report.sensitive:
        lines.append(f"!! {len(report.sensitive)} sensitive message(s) need you personally - /review")
    if report.new_leads:
        lines.append(f"{len(report.new_leads)} new lead(s), {len(report.drafts)} draft(s) ready - /drafts")
    other_review = len(report.review) - len(report.sensitive)
    if other_review > 0:
        lines.append(f"{other_review} message(s) need your judgment - /review")
    if report.auto_sent:
        lines.append("Auto-replied (routine first response): " + ", ".join(f"#{d}" for d in report.auto_sent))
    if report.errors:
        lines.append("Gmail sync problem: " + report.errors[0][:200])
    return "\n".join(lines)
