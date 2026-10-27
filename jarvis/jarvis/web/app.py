"""Mobile-first JARVIS dashboard.

Security: single-owner password login (JARVIS_DASHBOARD_PASSWORD), signed
session cookie (JARVIS_SECRET_KEY, HttpOnly, SameSite=Strict), CSRF token on
every POST, binds to 127.0.0.1 by default. `/healthz` is the only public route
and reveals nothing beyond liveness.
"""
from __future__ import annotations

import hashlib
import hmac
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select

from .. import audit
from ..briefing import build_briefing
from ..config import BusinessProfile, Settings
from ..crm import service as crm
from ..db import Database
from ..integrations.base import integration_statuses
from ..models import AuditLog, Draft, InboundMessage, Lead, LeadStage
from ..router import Context, handle
from ..security import RateLimiter

COOKIE = "jarvis_session"
SESSION_MAX_AGE = 12 * 3600
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


class NotAuthenticated(Exception):
    pass


def create_app(settings: Settings, profile: BusinessProfile, db: Database) -> FastAPI:
    if len(settings.dashboard_password) < 12 or len(settings.secret_key) < 32:
        raise RuntimeError("Dashboard requires JARVIS_DASHBOARD_PASSWORD (>=12 chars) and JARVIS_SECRET_KEY (>=32 chars).")

    app = FastAPI(title="JARVIS", docs_url=None, redoc_url=None, openapi_url=None)
    signer = URLSafeTimedSerializer(settings.secret_key, salt="jarvis-session")
    login_limiter = RateLimiter(5, window_seconds=300)

    def csrf_for(token: str) -> str:
        return hmac.new(settings.secret_key.encode(), f"csrf:{token}".encode(), hashlib.sha256).hexdigest()

    def current_session(request: Request) -> str:
        raw = request.cookies.get(COOKIE, "")
        try:
            data = signer.loads(raw, max_age=SESSION_MAX_AGE)
        except (BadSignature, SignatureExpired):
            raise NotAuthenticated()
        if data.get("u") != "owner":
            raise NotAuthenticated()
        return raw

    def require_csrf(request: Request, token: str, csrf: str) -> None:
        if not hmac.compare_digest(csrf_for(token), csrf or ""):
            raise HTTPException(status_code=403, detail="CSRF check failed")

    @app.exception_handler(NotAuthenticated)
    async def _redirect_login(request: Request, exc: NotAuthenticated):
        return RedirectResponse("/login", status_code=303)

    def render(request: Request, name: str, token: str, **ctx):
        return TEMPLATES.TemplateResponse(request, name, {"csrf": csrf_for(token), "business": profile.name or "JARVIS", **ctx})

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/healthz")
    def healthz():
        return JSONResponse({"ok": True})

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        return TEMPLATES.TemplateResponse(request, "login.html", {"error": ""})

    @app.post("/login")
    def login(request: Request, password: str = Form(...)):
        client = request.client.host if request.client else "?"
        if not login_limiter.allow(client):
            return TEMPLATES.TemplateResponse(request, "login.html", {"error": "Too many attempts. Wait 5 minutes."}, status_code=429)
        if not hmac.compare_digest(password.encode(), settings.dashboard_password.encode()):
            with db.session() as s:
                audit.record(s, "dashboard", "dashboard.login", result="denied")
            return TEMPLATES.TemplateResponse(request, "login.html", {"error": "Wrong password."}, status_code=401)
        with db.session() as s:
            audit.record(s, "dashboard", "dashboard.login")
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(COOKIE, signer.dumps({"u": "owner"}), max_age=SESSION_MAX_AGE, httponly=True,
                        samesite="strict", secure=settings.env == "production")
        return resp

    @app.post("/logout")
    def logout(request: Request, csrf: str = Form(""), token: str = Depends(current_session)):
        require_csrf(request, token, csrf)
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE)
        return resp

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, token: str = Depends(current_session)):
        with db.session() as s:
            return render(request, "home.html", token,
                          briefing=build_briefing(s, settings),
                          integrations=integration_statuses(settings),
                          drafts=crm.pending_drafts(s)[:10],
                          review=crm.review_queue(s)[:10],
                          due=crm.followups_due(s)[:10],
                          recent=list(s.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(10))))

    @app.get("/leads", response_class=HTMLResponse)
    def leads(request: Request, q: str = "", service: str = "", stage: str = "", token: str = Depends(current_session)):
        with db.session() as s:
            rows = crm.search_leads(s, q=q, service_type=service, stage=stage, limit=200)
            return render(request, "leads.html", token, leads=rows, q=q, service=service, stage=stage, stages=LeadStage.ALL)

    @app.get("/leads/{lead_id}", response_class=HTMLResponse)
    def lead_detail(request: Request, lead_id: int, token: str = Depends(current_session)):
        with db.session() as s:
            lead = s.get(Lead, lead_id)
            if not lead:
                raise HTTPException(404)
            return render(request, "lead.html", token, lead=lead, stages=LeadStage.ALL)

    @app.post("/leads/{lead_id}/stage")
    def lead_stage(request: Request, lead_id: int, stage: str = Form(...), csrf: str = Form(""), token: str = Depends(current_session)):
        require_csrf(request, token, csrf)
        with db.session() as s:
            lead = s.get(Lead, lead_id)
            if not lead or stage not in LeadStage.ALL:
                raise HTTPException(400)
            crm.set_stage(s, lead, stage, "dashboard")
        return RedirectResponse(f"/leads/{lead_id}", status_code=303)

    @app.post("/drafts/{draft_id}/{action}")
    def draft_action(request: Request, draft_id: int, action: str, csrf: str = Form(""), token: str = Depends(current_session)):
        require_csrf(request, token, csrf)
        with db.session() as s:
            draft = s.get(Draft, draft_id)
            if not draft:
                raise HTTPException(404)
            if action == "approve":
                crm.decide_draft(s, draft, True, "dashboard")
            elif action == "reject":
                crm.decide_draft(s, draft, False, "dashboard")
            elif action == "sent":
                crm.mark_draft_sent_manually(s, draft, "dashboard")
            else:
                raise HTTPException(400)
            lead_id = draft.lead_id
        return RedirectResponse(f"/leads/{lead_id}", status_code=303)

    @app.get("/review", response_class=HTMLResponse)
    def review(request: Request, token: str = Depends(current_session)):
        with db.session() as s:
            return render(request, "review.html", token, items=crm.review_queue(s))

    @app.post("/review/{msg_id}/resolve")
    def review_resolve(request: Request, msg_id: int, csrf: str = Form(""), token: str = Depends(current_session)):
        require_csrf(request, token, csrf)
        with db.session() as s:
            msg = s.get(InboundMessage, msg_id)
            if not msg:
                raise HTTPException(404)
            crm.resolve_review(s, msg, "dashboard")
        return RedirectResponse("/review", status_code=303)

    @app.get("/audit", response_class=HTMLResponse)
    def audit_view(request: Request, q: str = "", token: str = Depends(current_session)):
        with db.session() as s:
            stmt = select(AuditLog).order_by(AuditLog.id.desc()).limit(300)
            if q:
                stmt = stmt.where((AuditLog.action.ilike(f"%{q}%")) | (AuditLog.target.ilike(f"%{q}%")) | (AuditLog.actor.ilike(f"%{q}%")))
            return render(request, "audit.html", token, rows=list(s.scalars(stmt)), q=q)

    @app.post("/command", response_class=HTMLResponse)
    def command(request: Request, text: str = Form(...), csrf: str = Form(""), token: str = Depends(current_session)):
        """Same router the Telegram bot uses - one command center."""
        require_csrf(request, token, csrf)
        ctx = Context(db=db, settings=settings, profile=profile, actor="dashboard")
        reply = handle(ctx, text[:4000])
        return render(request, "command.html", token, text=text, reply=reply)

    return app
