"""Daily briefing and evening review built only from data in the database."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .classify import Category
from .config import Settings
from .crm import service as crm
from .db import utcnow
from .integrations.base import IntegrationState, integration_statuses
from .models import AuditLog, Draft, DraftStatus, InboundMessage, Lead, LeadStage

LABEL = {"junk_removal": "Junk", "home_cleaning": "Cleaning", "unknown": "?"}


def lead_line(lead: Lead) -> str:
    who = lead.name or lead.email or (f"...{lead.phone[-4:]}" if lead.phone else "unknown")
    where = lead.zip_code or lead.location or "no location"
    flag = " - awaiting reply" if lead.awaiting_reply else ""
    return f"#{lead.id} [{LABEL.get(lead.service_type, '?')}] {who} - {where} - {lead.stage}{flag}"


def _since(hours: int) -> dt.datetime:
    return utcnow() - dt.timedelta(hours=hours)


def build_briefing(session: Session, settings: Settings) -> str:
    day_ago = _since(24)
    new_leads = list(session.scalars(select(Lead).where(Lead.created_at >= day_ago).order_by(Lead.created_at)))
    new_junk = [x for x in new_leads if x.service_type == "junk_removal"]
    new_clean = [x for x in new_leads if x.service_type == "home_cleaning"]
    needs_info = crm.search_leads(session, stage=LeadStage.NEEDS_INFORMATION)
    quotes = crm.search_leads(session, stage=LeadStage.QUOTE_PENDING_APPROVAL) + crm.search_leads(session, stage=LeadStage.READY_FOR_QUOTE)
    due = crm.followups_due(session)
    booked = crm.search_leads(session, stage=LeadStage.BOOKED)
    drafts = crm.pending_drafts(session)
    review = crm.review_queue(session)
    sensitive = [m for m in review if m.category == Category.SENSITIVE]
    unanswered = crm.unanswered_leads(session)
    failures = session.scalar(select(func.count()).select_from(AuditLog)
                              .where(AuditLog.ts >= day_ago, AuditLog.result == "failed")) or 0

    out = ["JARVIS business briefing", ""]

    def section(title: str, leads: list[Lead], limit: int = 8) -> None:
        out.append(f"{title}: {len(leads)}")
        out.extend("  " + lead_line(x) for x in leads[:limit])
        if len(leads) > limit:
            out.append(f"  ...and {len(leads) - limit} more")

    section("New junk removal leads (24h)", new_junk)
    section("New home cleaning leads (24h)", new_clean)
    section("Needs more information", needs_info)
    section("Ready to quote / quote awaiting approval", quotes)
    section("Follow-ups due", due)
    section("Booked jobs", booked)
    out.append(f"Drafts awaiting your approval: {len(drafts)}")
    out.append(f"Messages needing your review: {len(review)} ({len(sensitive)} sensitive)")
    out.append(f"Failed operations (24h): {failures}")
    disconnected = [s.name for s in integration_statuses(settings)
                    if s.state not in (IntegrationState.CONNECTED, IntegrationState.CONFIGURED) and s.phase in ("1", "3")]
    if disconnected:
        out.append("Not connected: " + ", ".join(disconnected))

    out += ["", "Recommended priorities:"]
    prios = []
    if sensitive:
        prios.append(f"Handle {len(sensitive)} sensitive message(s) personally (/review)")
    if unanswered:
        prios.append(f"Reply to {len(unanswered)} lead(s) waiting on you (/drafts)")
    if quotes:
        prios.append(f"Price and send {len(quotes)} quote(s) - details are complete")
    if due:
        prios.append(f"Follow up with {len(due)} lead(s) (/followups)")
    if len(review) > len(sensitive):
        prios.append(f"Triage {len(review) - len(sensitive)} uncertain message(s) (/review)")
    if not prios:
        prios.append("No open lead work recorded. Lead volume is the bottleneck - marketing (Phase 4) is next.")
    out.extend(f"{i}. {p}" for i, p in enumerate(prios[:5], 1))
    return "\n".join(out)


def build_evening_review(session: Session, settings: Settings) -> str:
    day_ago = _since(24)
    created = session.scalar(select(func.count()).select_from(Lead).where(Lead.created_at >= day_ago)) or 0
    inbound = session.scalar(select(func.count()).select_from(InboundMessage).where(InboundMessage.received_at >= day_ago)) or 0
    approved = session.scalar(select(func.count()).select_from(Draft).where(
        Draft.decided_at >= day_ago, Draft.status.in_([DraftStatus.APPROVED, DraftStatus.SENT]))) or 0
    completed = session.scalar(select(func.count()).select_from(AuditLog).where(
        AuditLog.ts >= day_ago, AuditLog.action == "lead.stage_changed",
        AuditLog.detail["new"].as_string() == LeadStage.COMPLETED)) or 0
    lines = [
        "JARVIS evening review", "",
        f"Messages received today: {inbound}",
        f"New leads: {created}",
        f"Drafts approved/sent: {approved}",
        f"Jobs marked completed: {completed}",
        f"Still waiting on a reply: {len(crm.unanswered_leads(session))}",
        f"Drafts still pending: {len(crm.pending_drafts(session))}",
        f"Unreviewed messages: {len(crm.review_queue(session))}",
        "", "Tomorrow:",
    ]
    tomorrow = utcnow() + dt.timedelta(hours=24)
    due = crm.followups_due(session, now=tomorrow)
    lines.append(f"Follow-ups due by tomorrow: {len(due)}")
    lines.extend("  " + lead_line(x) for x in due[:8])
    return "\n".join(lines)
