"""ORM models. Portable types only (SQLite now, PostgreSQL later)."""
from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, utcnow


class LeadStage:
    NEW = "NEW"
    QUALIFYING = "QUALIFYING"
    NEEDS_INFORMATION = "NEEDS_INFORMATION"
    READY_FOR_QUOTE = "READY_FOR_QUOTE"
    QUOTE_PENDING_APPROVAL = "QUOTE_PENDING_APPROVAL"
    QUOTE_SENT = "QUOTE_SENT"
    FOLLOW_UP = "FOLLOW_UP"
    BOOKED = "BOOKED"
    COMPLETED = "COMPLETED"
    CLOSED = "CLOSED"

    ALL = [NEW, QUALIFYING, NEEDS_INFORMATION, READY_FOR_QUOTE, QUOTE_PENDING_APPROVAL,
           QUOTE_SENT, FOLLOW_UP, BOOKED, COMPLETED, CLOSED]
    OPEN = [NEW, QUALIFYING, NEEDS_INFORMATION, READY_FOR_QUOTE, QUOTE_PENDING_APPROVAL,
            QUOTE_SENT, FOLLOW_UP, BOOKED]


class ServiceType:
    JUNK_REMOVAL = "junk_removal"
    HOME_CLEANING = "home_cleaning"
    UNKNOWN = "unknown"


class DraftStatus:
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    SENT = "SENT"
    FAILED = "FAILED"


class ReviewStatus:
    NONE = "NONE"          # no human review needed (lead drafted, or spam/unrelated filtered)
    PENDING = "PENDING"    # waiting for the owner
    RESOLVED = "RESOLVED"


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    contact_method: Mapped[str] = mapped_column(String(30), default="")
    email: Mapped[str] = mapped_column(String(320), default="", index=True)
    phone: Mapped[str] = mapped_column(String(20), default="", index=True)
    service_type: Mapped[str] = mapped_column(String(30), default=ServiceType.UNKNOWN, index=True)
    location: Mapped[str] = mapped_column(String(300), default="")
    zip_code: Mapped[str] = mapped_column(String(10), default="")
    source: Mapped[str] = mapped_column(String(50), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    attachments: Mapped[list] = mapped_column(JSON, default=list)
    preferred_time: Mapped[str] = mapped_column(String(200), default="")
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    stage: Mapped[str] = mapped_column(String(30), default=LeadStage.NEW, index=True)
    quote_status: Mapped[str] = mapped_column(String(30), default="NONE")
    booking_status: Mapped[str] = mapped_column(String(30), default="NONE")
    job_outcome: Mapped[str] = mapped_column(String(200), default="")
    revenue_cents: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    follow_up_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True, index=True)
    last_inbound_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    last_outbound_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    interactions: Mapped[list["Interaction"]] = relationship(back_populates="lead", order_by="Interaction.created_at")
    drafts: Mapped[list["Draft"]] = relationship(back_populates="lead", order_by="Draft.created_at")

    @property
    def awaiting_reply(self) -> bool:
        """Customer wrote last and we have not answered since."""
        if self.last_inbound_at is None:
            return False
        return self.last_outbound_at is None or self.last_outbound_at < self.last_inbound_at


class InboundMessage(Base):
    """Every message JARVIS sees, whether or not it became a lead."""
    __tablename__ = "inbound_messages"
    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_inbound_source_extid"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(50))
    external_id: Mapped[str] = mapped_column(String(200))
    sender_name: Mapped[str] = mapped_column(String(200), default="")
    sender_contact: Mapped[str] = mapped_column(String(320), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(50), index=True)
    confidence: Mapped[float] = mapped_column(default=0.0)
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    review_status: Mapped[str] = mapped_column(String(20), default=ReviewStatus.NONE, index=True)
    lead_id: Mapped[Optional[int]] = mapped_column(ForeignKey("leads.id"), nullable=True)
    received_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class Interaction(Base):
    __tablename__ = "interactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"), index=True)
    direction: Mapped[str] = mapped_column(String(10))  # in | out | note
    channel: Mapped[str] = mapped_column(String(50), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    provider_message_id: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    lead: Mapped[Lead] = relationship(back_populates="interactions")


class Draft(Base):
    __tablename__ = "drafts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"), index=True)
    inbound_id: Mapped[Optional[int]] = mapped_column(ForeignKey("inbound_messages.id"), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    body: Mapped[str] = mapped_column(Text)
    missing_fields: Mapped[list] = mapped_column(JSON, default=list)
    policy_issues: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(30), default=DraftStatus.PENDING_APPROVAL, index=True)
    provider_message_id: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    decided_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)

    lead: Mapped[Lead] = relationship(back_populates="drafts")


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(100))       # owner | system | telegram:<id> | scheduler
    action: Mapped[str] = mapped_column(String(100), index=True)
    target: Mapped[str] = mapped_column(String(200), default="")
    result: Mapped[str] = mapped_column(String(20), default="ok")  # ok | failed | denied
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class ScheduledRun(Base):
    """One row per (job, run_key) so a scheduled job never fires twice."""
    __tablename__ = "scheduled_runs"
    __table_args__ = (UniqueConstraint("job_name", "run_key", name="uq_job_run"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_name: Mapped[str] = mapped_column(String(100))
    run_key: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), default="started")
    error: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)


class KeyValue(Base):
    """Small persistent state (e.g. Telegram update offset)."""
    __tablename__ = "kv"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
