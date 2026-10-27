"""`jarvis` command-line entry point."""
from __future__ import annotations

import argparse
import datetime as dt
import secrets
import sqlite3
import sys
import uuid

from .config import BusinessProfile, Settings, validate
from .db import SCHEMA_VERSION, Database
from .logging_setup import setup_logging


def _load() -> tuple[Settings, BusinessProfile, Database]:
    settings = Settings.from_env()
    setup_logging(settings.log_level, settings.secret_values())
    profile = BusinessProfile.load(settings.business_profile_path)
    return settings, profile, Database(settings.database_url)


def cmd_init_db(args) -> int:
    _, _, db = _load()
    version = db.migrate()
    print(f"Database ready at schema v{version}.")
    return 0


def cmd_check_config(args) -> int:
    settings, profile, _ = _load()
    failed = 0
    for c in validate(settings, profile):
        mark = "OK  " if c.ok else ("FAIL" if c.required else "WARN")
        failed += (not c.ok and c.required)
        print(f"[{mark}] {c.name}: {c.detail}")
    print(f"\n{failed} required check(s) failing." if failed else "\nAll required checks pass.")
    return 1 if failed else 0


def cmd_health(args) -> int:
    settings, profile, db = _load()
    ok = True
    try:
        version = db.schema_version()
        state = "ok" if version == SCHEMA_VERSION else f"schema v{version}, expected v{SCHEMA_VERSION} - run `jarvis init-db`"
        ok &= version == SCHEMA_VERSION
    except Exception as exc:
        state, ok = f"error: {type(exc).__name__}", False
    print(f"database: {state}")
    if args.online:
        if not settings.telegram_bot_token:
            print("telegram: not configured")
            ok = False
        else:
            from .integrations.telegram import TelegramClient, TelegramError
            try:
                me = TelegramClient(settings.telegram_bot_token).get_me()
                print(f"telegram: connected as @{me.get('username')}")
            except TelegramError as exc:
                print(f"telegram: FAILED - {exc}")
                ok = False
    else:
        print("telegram: not checked (use --online to call getMe)")
    return 0 if ok else 1


def cmd_bot(args) -> int:
    settings, profile, db = _load()
    db.migrate()
    problems = [c for c in validate(settings, profile) if c.name.startswith("TELEGRAM") and not c.ok]
    if problems:
        for c in problems:
            print(f"Cannot start bot - {c.name}: {c.detail}", file=sys.stderr)
        return 2
    from .bot import Bot
    from .integrations.telegram import TelegramClient
    from .integrations.gmail import make_gmail
    Bot(settings, profile, db, TelegramClient(settings.telegram_bot_token), email=make_gmail(settings)).run_forever()
    return 0


def cmd_web(args) -> int:
    settings, profile, db = _load()
    db.migrate()
    import uvicorn
    from .web.app import create_app
    try:
        from .integrations.gmail import make_gmail
        app = create_app(settings, profile, db, email=make_gmail(settings))
    except RuntimeError as exc:
        print(f"Cannot start dashboard - {exc}", file=sys.stderr)
        return 2
    uvicorn.run(app, host=args.host or settings.dashboard_host, port=args.port or settings.dashboard_port,
                log_level="warning", proxy_headers=False)
    return 0


def cmd_ask(args) -> int:
    """Run a JARVIS command locally (same router as Telegram)."""
    settings, profile, db = _load()
    db.migrate()
    from .integrations.gmail import make_gmail
    from .router import Context, handle
    print(handle(Context(db=db, settings=settings, profile=profile, actor="cli", email=make_gmail(settings)),
                 " ".join(args.text)))
    return 0


def cmd_ingest(args) -> int:
    """Feed one inbound customer message through classification + drafting."""
    settings, profile, db = _load()
    db.migrate()
    from .crm.service import ingest_message
    body = args.text if args.text != "-" else sys.stdin.read()
    with db.session() as s:
        r = ingest_message(s, source=args.source, external_id=args.id or f"cli:{uuid.uuid4()}", body=body,
                           sender_name=args.name, sender_contact=args.contact, profile=profile,
                           followup_hours=settings.followup_hours, actor="cli")
        print(f"{r.classification.category} ({r.classification.confidence:.2f}): {'; '.join(r.classification.reasons)}")
        if r.duplicate_message:
            print("(already ingested - no changes)")
        if r.lead:
            print(f"lead #{r.lead.id} stage={r.lead.stage}")
        if r.draft:
            print(f"\ndraft #{r.draft.id}:\n{r.draft.body}")
    return 0


def cmd_backup(args) -> int:
    settings, _, db = _load()
    if not settings.database_url.startswith("sqlite:///"):
        print("Backup command supports SQLite only; use pg_dump for PostgreSQL.", file=sys.stderr)
        return 2
    src_path = settings.database_url.removeprefix("sqlite:///")
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    dest = settings.backup_dir / f"jarvis-{dt.datetime.now():%Y%m%d-%H%M%S}.db"
    with sqlite3.connect(src_path) as src, sqlite3.connect(dest) as out:
        src.backup(out)
    with sqlite3.connect(dest) as check:
        result = check.execute("PRAGMA integrity_check").fetchone()[0]
    print(f"Backup written to {dest} (integrity: {result})")
    return 0 if result == "ok" else 1


def cmd_gmail_auth(args) -> int:
    """Interactive Google sign-in (run on your own computer - it opens a browser)."""
    settings, _, _ = _load()
    from .integrations.email import EmailError
    from .integrations.gmail import run_oauth_flow
    try:
        scopes = run_oauth_flow(settings.gmail_client_secrets_file, settings.gmail_token_file)
    except EmailError as exc:
        print(f"Gmail authorization failed: {exc}", file=sys.stderr)
        return 1
    print("Gmail authorized with: " + ", ".join(x.rsplit("/", 1)[-1] for x in scopes))
    print(f"Token saved to {settings.gmail_token_file} (keep this file private). Next: jarvis gmail-check")
    return 0


def cmd_gmail_check(args) -> int:
    settings, _, _ = _load()
    from .integrations.email import EmailError
    from .integrations.gmail import make_gmail
    api = make_gmail(settings)
    if not api:
        print("Gmail not authorized - run `jarvis gmail-auth`.", file=sys.stderr)
        return 1
    try:
        print(f"Gmail connected as {api.profile_email()}")
        n = len(api.list_ids("in:inbox newer_than:1d", 10))
        print(f"Read access OK ({n} inbox message(s) in the last day, up to 10 checked). Nothing was changed or sent.")
    except EmailError as exc:
        print(f"Gmail check FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_gmail_sync(args) -> int:
    settings, profile, db = _load()
    db.migrate()
    from .email_workflow import format_report, sync_gmail
    from .integrations.gmail import make_gmail
    api = make_gmail(settings)
    if not api:
        print("Gmail not authorized - run `jarvis gmail-auth`.", file=sys.stderr)
        return 1
    r = sync_gmail(db, api, settings, profile)
    print(format_report(r) if r.has_news else "No new customer email.")
    print(f"fetched={r.fetched} filtered={r.filtered} owner_replies_detected={r.replies_detected} errors={len(r.errors)}")
    return 1 if r.errors else 0


def cmd_gen_secret(args) -> int:
    print(secrets.token_urlsafe(48))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="jarvis", description="JARVIS business assistant")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db", help="create/upgrade the database").set_defaults(fn=cmd_init_db)
    sub.add_parser("check-config", help="validate configuration (never prints secrets)").set_defaults(fn=cmd_check_config)
    h = sub.add_parser("health", help="health check")
    h.add_argument("--online", action="store_true", help="also verify the Telegram token with getMe")
    h.set_defaults(fn=cmd_health)
    sub.add_parser("bot", help="run the Telegram bot (long polling + scheduled briefings)").set_defaults(fn=cmd_bot)
    w = sub.add_parser("web", help="run the dashboard")
    w.add_argument("--host")
    w.add_argument("--port", type=int)
    w.set_defaults(fn=cmd_web)
    a = sub.add_parser("ask", help='run a command locally, e.g. jarvis ask /briefing')
    a.add_argument("text", nargs="+")
    a.set_defaults(fn=cmd_ask)
    i = sub.add_parser("ingest", help="process an inbound customer message")
    i.add_argument("--text", required=True, help='message body, or "-" for stdin')
    i.add_argument("--name", default="")
    i.add_argument("--contact", default="", help="email or phone")
    i.add_argument("--source", default="manual")
    i.add_argument("--id", help="provider message id (for idempotency)")
    i.set_defaults(fn=cmd_ingest)
    sub.add_parser("backup", help="back up the SQLite database").set_defaults(fn=cmd_backup)
    sub.add_parser("gmail-auth", help="sign in to Gmail (opens a browser)").set_defaults(fn=cmd_gmail_auth)
    sub.add_parser("gmail-check", help="verify Gmail access (read-only)").set_defaults(fn=cmd_gmail_check)
    sub.add_parser("gmail-sync", help="process new Gmail messages now").set_defaults(fn=cmd_gmail_sync)
    sub.add_parser("gen-secret", help="print a random value for JARVIS_SECRET_KEY").set_defaults(fn=cmd_gen_secret)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
