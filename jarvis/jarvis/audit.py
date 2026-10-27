"""Audit log helpers. Details are redacted before storage."""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from .logging_setup import redact
from .models import AuditLog


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)[:500]
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def record(session: Session, actor: str, action: str, target: str = "", result: str = "ok", **detail: Any) -> AuditLog:
    entry = AuditLog(actor=actor, action=action, target=target, result=result, detail=_clean(detail))
    session.add(entry)
    session.flush()
    return entry
