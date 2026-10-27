"""Central JARVIS command router.

Takes already-authorized text from any interface (Telegram, CLI, dashboard)
and returns a text reply. Every handler is Level 1 (read-only or changes to
JARVIS's own records). No handler sends anything to customers or external
platforms - those integrations do not exist yet, and the replies say so.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Callable

from sqlalchemy.orm import Session

from . import audit
from .briefing import LABEL, build_briefing, build_evening_review, lead_line
from .config import BusinessProfile, Settings, validate
from .crm import service as crm
from .db import Database
from .integrations.base import integration_statuses
from .models import Draft, InboundMessage, Lead, LeadStage


@dataclass
class Context:
    db: Database
    settings: Settings
    profile: BusinessProfile
    actor: str               # e.g. "telegram:12345"
    message_ref: str = ""    # unique id of the incoming command (for idempotent ingestion)


HELP = """JARVIS commands

Business
/briefing - daily business briefing
/evening - evening review
/leads - open leads
/junk - junk removal leads
/cleaning - home cleaning leads
/followups - follow-ups due
/unanswered - leads waiting on your reply
/lead <id> - lead details and history
/stage <id> <STAGE> - move a lead (e.g. BOOKED)
/inquiry <customer message> - classify a pasted customer message and draft a reply
   (optional first lines: "name: ...", "contact: <email or phone>")
/drafts - replies waiting for approval
/draft <id> - show a full draft
/approve <id> | /reject <id> - decide on a draft
/sent <id> - record that you sent an approved draft yourself
/review - messages that need your judgment
/resolve <id> - mark a review item handled

System
/status - integration health
/settings - safe configuration summary
/marketing - marketing module status

You can also write naturally, e.g. "JARVIS, show me new junk removal inquiries"."""


def _cmd_start(ctx: Context, s: Session, args: str) -> str:
    return "JARVIS is online. I track your junk removal and cleaning leads and draft replies for your approval.\n\n" + HELP


def _cmd_help(ctx, s, args):
    return HELP


def _cmd_briefing(ctx, s, args):
    return build_briefing(s, ctx.settings)


def _cmd_evening(ctx, s, args):
    return build_evening_review(s, ctx.settings)


def _list(title: str, leads: list[Lead], limit: int = 25) -> str:
    if not leads:
        return f"{title}: none."
    lines = [f"{title} ({len(leads)}):"] + [lead_line(x) for x in leads[:limit]]
    if len(leads) > limit:
        lines.append(f"...and {len(leads) - limit} more (see dashboard)")
    return "\n".join(lines)


def _cmd_leads(ctx, s, args):
    return _list("Open leads", crm.search_leads(s, q=args.strip(), open_only=True))


def _cmd_junk(ctx, s, args):
    return _list("Open junk removal leads", crm.search_leads(s, service_type="junk_removal", open_only=True))


def _cmd_cleaning(ctx, s, args):
    return _list("Open home cleaning leads", crm.search_leads(s, service_type="home_cleaning", open_only=True))


def _cmd_followups(ctx, s, args):
    return _list("Follow-ups due", crm.followups_due(s))


def _cmd_unanswered(ctx, s, args):
    leads = crm.unanswered_leads(s)
    if args.strip() in LABEL:
        leads = [x for x in leads if x.service_type == args.strip()]
    return _list("Leads waiting on your reply", leads)


def _id_arg(args: str) -> int | None:
    m = re.match(r"\s*#?(\d+)", args)
    return int(m.group(1)) if m else None


def _cmd_lead(ctx, s, args):
    lead_id = _id_arg(args)
    lead = s.get(Lead, lead_id) if lead_id else None
    if not lead:
        return "Usage: /lead <id>. Lead not found."
    d = lead.details or {}
    lines = [lead_line(lead),
             f"Contact: {lead.email or lead.phone or lead.contact_method or 'unknown'}",
             f"Source: {lead.source}  Created: {lead.created_at:%Y-%m-%d %H:%M} UTC",
             f"Follow-up: {lead.follow_up_at:%Y-%m-%d %H:%M} UTC" if lead.follow_up_at else "Follow-up: none",
             "Known details: " + (", ".join(f"{k}={v}" for k, v in d.items()) or "none"),
             "", "History:"]
    for i in lead.interactions[-10:]:
        body = i.body.replace("\n", " ")
        lines.append(f"  {i.created_at:%m-%d %H:%M} {i.direction}: {body[:200]}")
    for dr in lead.drafts[-3:]:
        lines.append(f"Draft #{dr.id}: {dr.status}")
    return "\n".join(lines)


def _cmd_stage(ctx, s, args):
    parts = args.split()
    if len(parts) != 2 or not parts[0].lstrip("#").isdigit():
        return "Usage: /stage <id> <STAGE>\nStages: " + ", ".join(LeadStage.ALL)
    lead = s.get(Lead, int(parts[0].lstrip("#")))
    stage = parts[1].upper()
    if not lead:
        return "Lead not found."
    if stage not in LeadStage.ALL:
        return "Unknown stage. Stages: " + ", ".join(LeadStage.ALL)
    crm.set_stage(s, lead, stage, ctx.actor)
    return f"Lead #{lead.id} moved to {stage}."


INQUIRY_HEADER = re.compile(r"^\s*(name|contact|email|phone)\s*:\s*(.+)$", re.I)


def _cmd_inquiry(ctx, s, args):
    if not args.strip():
        return ("Paste the customer's message after /inquiry. Optional first lines:\n"
                "name: Jane Doe\ncontact: jane@example.com")
    name, contact, body_lines = "", "", []
    for line in args.strip().splitlines():
        m = INQUIRY_HEADER.match(line)
        if m and not body_lines:
            key, val = m.group(1).lower(), m.group(2).strip()
            if key == "name":
                name = val
            else:
                contact = val
        else:
            body_lines.append(line)
    body = "\n".join(body_lines).strip()
    if not body:
        return "No message text found after the header lines."
    r = crm.ingest_message(s, source="manual_telegram", external_id=ctx.message_ref or "manual:" + hashlib.sha256(args.encode()).hexdigest()[:32],
                           body=body, sender_name=name, sender_contact=contact, profile=ctx.profile,
                           followup_hours=ctx.settings.followup_hours, actor=ctx.actor)
    c = r.classification
    lines = [f"Classified: {c.category} (confidence {c.confidence:.2f})", "Why: " + "; ".join(c.reasons)]
    if r.duplicate_message:
        lines.insert(0, "Already processed this message.")
    if r.lead:
        lines.append(("Added to existing lead " if r.merged_into_existing else "Created lead ") + lead_line(r.lead))
    if r.draft:
        lines += ["", f"Draft #{r.draft.id} (not sent - approve with /approve {r.draft.id}):", "", r.draft.body]
        if r.draft.policy_issues:
            lines += ["", "Notes: " + "; ".join(r.draft.policy_issues)]
    elif c.needs_review:
        lines.append("No reply drafted - queued for your review (/review).")
    else:
        lines.append("Not a junk removal or cleaning inquiry - no reply drafted.")
    return "\n".join(lines)


def _cmd_drafts(ctx, s, args):
    drafts = crm.pending_drafts(s)
    if not drafts:
        return "No drafts waiting for approval."
    lines = [f"Drafts awaiting approval ({len(drafts)}):"]
    for d in drafts[:20]:
        preview = d.body.replace("\n", " ")[:120]
        lines.append(f"#{d.id} for {lead_line(d.lead)}\n  {preview}...")
    lines.append("\n/draft <id> to read in full, /approve <id> or /reject <id>.")
    return "\n".join(lines)


def _get_draft(s: Session, args: str) -> Draft | None:
    draft_id = _id_arg(args)
    return s.get(Draft, draft_id) if draft_id else None


def _cmd_draft(ctx, s, args):
    d = _get_draft(s, args)
    if not d:
        return "Usage: /draft <id>. Draft not found."
    lines = [f"Draft #{d.id} - {d.status} - {lead_line(d.lead)}", "", d.body]
    if d.policy_issues:
        lines += ["", "Notes: " + "; ".join(d.policy_issues)]
    return "\n".join(lines)


def _cmd_approve(ctx, s, args):
    d = _get_draft(s, args)
    return crm.decide_draft(s, d, True, ctx.actor) if d else "Usage: /approve <id>. Draft not found."


def _cmd_reject(ctx, s, args):
    d = _get_draft(s, args)
    return crm.decide_draft(s, d, False, ctx.actor) if d else "Usage: /reject <id>. Draft not found."


def _cmd_sent(ctx, s, args):
    d = _get_draft(s, args)
    return crm.mark_draft_sent_manually(s, d, ctx.actor) if d else "Usage: /sent <id>. Draft not found."


def _cmd_review(ctx, s, args):
    items = crm.review_queue(s)
    if not items:
        return "Review queue is empty."
    lines = [f"Needs your review ({len(items)}):"]
    for m in items[:20]:
        body = m.body.replace("\n", " ")[:160]
        lead = f" (lead #{m.lead_id})" if m.lead_id else ""
        lines.append(f"#{m.id} {m.category}{lead} via {m.source}: {body}\n  why: {'; '.join(m.reasons)}")
    lines.append("\n/resolve <id> when handled.")
    return "\n".join(lines)


def _cmd_resolve(ctx, s, args):
    msg_id = _id_arg(args)
    msg = s.get(InboundMessage, msg_id) if msg_id else None
    if not msg:
        return "Usage: /resolve <id>. Item not found."
    crm.resolve_review(s, msg, ctx.actor)
    return f"Review item #{msg.id} marked resolved."


def _cmd_status(ctx, s, args):
    lines = ["Integration status:"]
    for st in integration_statuses(ctx.settings):
        lines.append(f"- {st.name}: {st.state} - {st.detail}")
    lines.append(f"Database schema v{ctx.db.schema_version()}")
    return "\n".join(lines)


def _cmd_settings(ctx, s, args):
    lines = ["Configuration (secrets are never shown):"]
    for c in validate(ctx.settings, ctx.profile):
        mark = "OK " if c.ok else ("!! " if c.required else "-- ")
        lines.append(f"{mark}{c.name}: {c.detail}")
    return "\n".join(lines)


def _cmd_marketing(ctx, s, args):
    return ("Marketing content generation is Phase 4 and is not built yet. Nothing has been drafted or posted.\n"
            "Next: fill in config/business.toml (name, service area, approved offers, brand voice) so posts "
            "can use your real business details.")


COMMANDS: dict[str, Callable[[Context, Session, str], str]] = {
    "start": _cmd_start, "help": _cmd_help, "briefing": _cmd_briefing, "evening": _cmd_evening,
    "leads": _cmd_leads, "junk": _cmd_junk, "cleaning": _cmd_cleaning, "followups": _cmd_followups,
    "unanswered": _cmd_unanswered, "lead": _cmd_lead, "stage": _cmd_stage, "inquiry": _cmd_inquiry,
    "drafts": _cmd_drafts, "draft": _cmd_draft, "approve": _cmd_approve, "reject": _cmd_reject,
    "sent": _cmd_sent, "review": _cmd_review, "resolve": _cmd_resolve, "status": _cmd_status,
    "settings": _cmd_settings, "marketing": _cmd_marketing,
}

# Natural-language intents, checked in order. Each maps to (command, args).
NL_INTENTS: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"\b(integrations?|connected|health|status)\b", re.I), "status", ""),
    (re.compile(r"\bsettings?|config", re.I), "settings", ""),
    (re.compile(r"\b(evening|end of day|wrap[\s-]?up|summari[sz]e .*today)\b", re.I), "evening", ""),
    (re.compile(r"\b(briefing|morning|today|tasks|priorit|focus)\b", re.I), "briefing", ""),
    (re.compile(r"\bclean\w*\b.*\b(no|not|haven'?t|without|unanswered|waiting)\b.*\b(repl|respon|answer)", re.I), "unanswered", "home_cleaning"),
    (re.compile(r"\bjunk\b.*\b(no|not|haven'?t|without|unanswered|waiting)\b.*\b(repl|respon|answer)", re.I), "unanswered", "junk_removal"),
    (re.compile(r"\b(unanswered|haven'?t (?:received|gotten|had) a (?:reply|response)|no (?:reply|response)|waiting on)\b", re.I), "unanswered", ""),
    (re.compile(r"\bfollow[\s-]?ups?\b", re.I), "followups", ""),
    (re.compile(r"\b(review queue|needs? (?:my )?review|ambiguous|sensitive)\b", re.I), "review", ""),
    (re.compile(r"\bdrafts?\b|\bprepared (?:replies|responses)\b", re.I), "drafts", ""),
    (re.compile(r"\bjunk\b", re.I), "junk", ""),
    (re.compile(r"\bclean", re.I), "cleaning", ""),
    (re.compile(r"\b(leads?|inquir\w+|customers?)\b", re.I), "leads", ""),
    (re.compile(r"\b(marketing|posts?|facebook|nextdoor|canva)\b", re.I), "marketing", ""),
    (re.compile(r"\b(help|commands)\b", re.I), "help", ""),
]

WAKE_RE = re.compile(r"^\s*(?:hey\s+|ok\s+)?jarvis[\s,:!.-]*", re.I)


def parse(text: str) -> tuple[str | None, str]:
    """Return (command, args). command is None if nothing matched."""
    text = (text or "").strip()
    if text.startswith("/"):
        head, _, rest = text[1:].partition(" ")
        if "\n" in head:
            head, _, more = head.partition("\n")
            rest = more + ("\n" + rest if rest else "")
        cmd = head.split("@", 1)[0].lower()  # /leads@MyBot in group syntax
        return (cmd if cmd in COMMANDS else None), rest
    stripped = WAKE_RE.sub("", text)
    for rx, cmd, args in NL_INTENTS:
        if rx.search(stripped):
            return cmd, args
    return None, stripped


def handle(ctx: Context, text: str) -> str:
    cmd, args = parse(text)
    with ctx.db.session() as s:
        if cmd is None:
            audit.record(s, ctx.actor, "command.unknown", result="ok")
            return "I didn't recognize that request. Send /help to see what I can do right now."
        try:
            reply = COMMANDS[cmd](ctx, s, args)
            audit.record(s, ctx.actor, f"command.{cmd}")
            return reply
        except Exception as exc:
            s.rollback()
            with ctx.db.session() as s2:
                audit.record(s2, ctx.actor, f"command.{cmd}", result="failed", error=type(exc).__name__)
            raise
