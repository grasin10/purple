# JARVIS — personal business operating system

JARVIS is a private assistant for a **junk removal + home cleaning** business that you control from **Telegram** on your phone and from a **mobile-friendly web dashboard**. Version 0.1 handles leads. It sorts incoming messages, keeps a CRM, drafts replies for your approval, tracks follow-ups and sends you daily briefings. It also keeps an audit log of everything it does.

> It lives in `jarvis/`, separate from the Krypt Trader app in the rest of this repository. The only link is a read-only view of Krypt Trader's database (see [Krypt Trader link](#krypt-trader-link)).

## What actually works today (v0.1)

| Area | Status | Notes |
|---|---|---|
| Lead CRM (SQLite, Postgres-ready) | ✅ working, tested | stages, duplicate detection by email/phone, history, search/filter, follow-up dates |
| Message classifier | ✅ working, tested | rule-based; 7 categories; sensitive/injection/uncertain messages always go to **your review** |
| Reply drafting | ✅ working, tested | asks only for missing details; a policy guard blocks prices, guarantees, confirmations, insurance claims, discounts and coverage claims |
| Draft approval | ✅ working | approving a draft **does not send it**, because no outbound channel exists yet. You send it yourself, then mark it sent |
| Telegram bot | ✅ code complete, tested with a fake client | needs your bot token + user ID. Not live-tested: Telegram is unreachable from the build sandbox |
| Telegram security | ✅ tested | user-ID allowlist, private chats only, rate limit, unauthorized attempts audited |
| Briefing / evening review | ✅ working | on demand; scheduled once you set a timezone and times |
| Scheduler | ✅ tested | runs each job once per day (enforced by the database), skips briefings that are more than 2h late |
| Dashboard | ✅ working, tested | password login, signed cookie, CSRF, security headers, command box using the same router as Telegram |
| Audit log | ✅ working | phone numbers and emails are redacted; searchable in the dashboard |
| Config validation / health / backup | ✅ working | never prints secrets |
| Krypt Trader link | ✅ working, tested | **read-only**: `/krypt` status and P&L, `/trades`, `/backtest`, win/loss/error alerts, trading section in briefings. Cannot place trades or change settings |
| Email (Gmail) | ⏳ interface only | Phase 3, needs Google OAuth credentials |
| Automatic replies | 🚫 not implemented | the flag exists but stays off; nothing can be sent to customers automatically |
| Marketing, YouTube, sports, forex | ⏳ not started | `/marketing` says so plainly and does not pretend |

**Nothing is mocked behind your back.** When an integration doesn't exist, JARVIS tells you, and it never says a message was sent unless a provider confirmed it.

## Setup (about 10 minutes)

You need Python 3.10+ and git.

### 1. Install

macOS / Linux:
```bash
cd jarvis
```
```bash
python3 -m venv .venv
```
```bash
.venv/bin/pip install -e ".[dev]"
```

Windows (PowerShell):
```powershell
cd jarvis
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
```

The commands below use `.venv/bin/jarvis`. On Windows use `.venv\Scripts\jarvis`.

### 2. Create a Telegram bot (free)

1. In Telegram, open **@BotFather**, send `/newbot` and follow the prompts. Copy the token it gives you.
2. Open **@userinfobot** and send it any message. It replies with your numeric **user ID**.
3. Optional but recommended: in BotFather, use `/setjoingroups` → **Disable** so nobody can add the bot to a group.

### 3. Configure

```bash
cp .env.example .env
```
```bash
cp config/business.example.toml config/business.toml
```
```bash
.venv/bin/jarvis gen-secret
```

Edit `.env`:
- `TELEGRAM_BOT_TOKEN`: the token from BotFather
- `TELEGRAM_ALLOWED_USER_IDS`: your numeric ID
- `JARVIS_SECRET_KEY`: the value printed by `gen-secret`
- `JARVIS_DASHBOARD_PASSWORD`: a strong password (12+ characters)
- Optional: `JARVIS_TIMEZONE=America/New_York`, `JARVIS_BRIEFING_TIME=07:30`, `JARVIS_EVENING_REVIEW_TIME=19:00`

Edit `config/business.toml` with your **real** business name, hours, service ZIPs/cities and brand voice. Fields you leave blank stay unknown, and drafts never mention them.

Both files are git-ignored. Never commit them.

### 4. Initialize and verify

```bash
.venv/bin/jarvis init-db
```
```bash
.venv/bin/jarvis check-config
```
```bash
.venv/bin/jarvis health --online
```

`health --online` calls Telegram's `getMe`. It should print `telegram: connected as @YourBot`.

### 5. Run

Telegram bot (keep this terminal open):
```bash
.venv/bin/jarvis bot
```

Dashboard, in a second terminal (open http://127.0.0.1:8000 afterwards):
```bash
.venv/bin/jarvis web
```

Message your bot `/start`. To restart, press Ctrl+C and run the command again. The bot saves its Telegram position in the database, so it won't reprocess old messages.

### 6. Try it

From Telegram, paste a real customer message you've received:

```
/inquiry name: Dana Smith
contact: dana@example.com
Hi, I need an old couch and a fridge hauled away from my garage in 30301 next week. How much?
```

JARVIS classifies it, creates or updates the lead and shows a draft. Then:
- `/approve 1`
- copy the draft to the customer yourself
- `/sent 1`

Or from the terminal:
```bash
.venv/bin/jarvis ingest --name "Dana" --contact dana@example.com --text "Need a couch hauled away"
```
```bash
.venv/bin/jarvis ask /briefing
```

### Run the tests

```bash
.venv/bin/python -m pytest -q
```

## Commands

`/krypt` `/trades` `/backtest` `/backtest c15` `/briefing` `/evening` `/leads [search]` `/junk` `/cleaning` `/followups` `/unanswered` `/lead <id>` `/stage <id> <STAGE>` `/inquiry <message>` `/drafts` `/draft <id>` `/approve <id>` `/reject <id>` `/sent <id>` `/review` `/resolve <id>` `/status` `/settings` `/marketing` `/help`

Natural language works too, for example "JARVIS, find cleaning leads that haven't received a reply" or "show me which integrations aren't connected".

Lead stages: `NEW → QUALIFYING → NEEDS_INFORMATION → READY_FOR_QUOTE → QUOTE_PENDING_APPROVAL → QUOTE_SENT → FOLLOW_UP → BOOKED → COMPLETED → CLOSED`

## Krypt Trader link

If Krypt Trader is installed on the same computer, JARVIS finds its database automatically (`%APPDATA%\Krypt Trader\data\krypt-trader.db` on Windows). Set `KRYPT_TRADER_DB` in `.env` if it lives elsewhere. `jarvis ask /status` shows whether it was found.

| Command | What you get |
|---|---|
| `/krypt` | demo or live, auto-trading on/off, engine running, balance, open positions, today's and 7-day P&L |
| `/trades` | the last 10 positions and how they ended |
| `/backtest` | Krypt's own fee-aware backtest of whale and momentum signals, in plain English |
| `/backtest c15` | the same for the 15-minute crypto strategy |

Natural language works too: "how is my trading doing?", "show my last trades", "is my strategy profitable?".

The bot also messages you when a position **wins, loses or hits an order error** (checked every minute; set `JARVIS_KRYPT_ALERTS=false` to stop). The first check after start-up only records where Krypt is, so old trades never flood you. Briefings and evening reviews gain a short trading section.

**JARVIS can look, never touch.** The database is opened in SQLite read-only mode and only the non-secret switches (`kalshiEnv`, `enableTrading`, 15-minute crypto on/live) are read from `settings.json`. Starting, stopping, going live or changing strategy happens only in the Krypt Trader app on your computer. Backtest replies always carry the same warning as the day-trader skill: in-sample results on small samples are "worth paper-testing", not proof, and not financial advice.

## How messages are handled

| Category | What JARVIS does |
|---|---|
| `JUNK_REMOVAL_LEAD` / `HOME_CLEANING_LEAD` (confidence ≥ 0.8) | creates/updates the lead, drafts a reply asking only for missing details, sets a follow-up date |
| `EXISTING_CUSTOMER_REQUEST` | attaches to the open lead → **your review** (no draft) |
| `AMBIGUOUS` (including suspected prompt injection) | **your review** |
| `SENSITIVE_OR_ESCALATION_REQUIRED` (refund, damage, complaint, legal…) | **your review**, flagged first in the briefing |
| `UNRELATED` / `SPAM` | logged, ignored |

Instructions found inside customer text are treated as data. A message like "ignore your rules" is forced into review.

## Permission model

- **Level 1 (automatic):** classifying, drafting, reports, reminders, and changes to JARVIS's own records. This is everything v0.1 can do.
- **Level 2 (pre-authorized routine sends):** none enabled. This needs a sending integration plus a tested, explicit opt-in.
- **Level 3 (explicit approval):** refunds, money, live trades, bets, public posts. None of these can be reached from v0.1.

A Telegram message alone can never authorize a financial transaction.

## Architecture

```
jarvis/
  config.py          env settings + business profile (TOML) + validation
  db.py, models.py   SQLAlchemy models, versioned migrations (SQLite → Postgres via JARVIS_DATABASE_URL)
  classify.py        narrow-scope lead classifier
  drafting.py        detail extraction, missing-info questions, policy guard
  crm/service.py     ingestion pipeline (idempotent on source+message id), dedupe, stages, drafts
  router.py          central command router shared by Telegram, dashboard and CLI
  briefing.py        morning briefing / evening review
  scheduler.py       daily jobs, run once per day (enforced by the database)
  bot.py             Telegram long polling, auth, rate limits
  integrations/      telegram.py (Bot API via httpx), email.py (adapter interface), base.py (status registry)
  web/               FastAPI dashboard + templates
  security.py        allowlist + rate limiter
  logging_setup.py   redaction of tokens, emails and phone numbers
```

One process runs the bot and the scheduler, and a second process runs the dashboard. Both share the database. There are no paid services.

## Run as a persistent service

Your computer has to be on for the bot to answer. When you're ready for 24/7:

- **Linux VPS (≈$5/mo)**: copy the repo to `/opt/jarvis`, create a `jarvis` user and install `deploy/jarvis-bot.service` + `deploy/jarvis-web.service`:
  ```bash
  sudo systemctl enable --now jarvis-bot jarvis-web
  ```
  Add `deploy/backup.cron` for nightly backups. Keep the dashboard on 127.0.0.1 and reach it through an SSH tunnel or Tailscale (free). Never expose it to the public internet without HTTPS.
- **Windows**: run `jarvis bot` from Task Scheduler ("At log on", restart on failure), or use NSSM to run it as a service.
- **Webhook mode** (an optional optimization) is not implemented yet. Long polling needs no public URL and is the better choice for a single-user bot.

Maintenance:
- `jarvis backup` writes a SQLite backup and checks its integrity.
- `jarvis init-db` applies new migrations after an update.
- `jarvis check-config` validates the configuration after any `.env` change.

## Credentials you'll need, and when

| Phase | Credential | Where | Cost |
|---|---|---|---|
| 1 (now) | Telegram bot token + your user ID | @BotFather, @userinfobot | free |
| 3 | Google Cloud OAuth client (Gmail API, `gmail.modify` scope) | console.cloud.google.com → APIs → Gmail API → OAuth consent + Desktop client | free |
| 3 (optional) | LLM API key for nicer drafts (`ANTHROPIC_API_KEY`) | console.anthropic.com | usage-based, roughly a few $/mo at this volume |
| 4 | Meta app + Facebook **Page** access token (`pages_manage_posts`) | developers.facebook.com, needs app review for publishing | free |
| 4 | Canva Connect API (OAuth) | canva.com/developers | free tier |
| 4 | Nextdoor | the public API is limited; expect manual "ready-to-post" packages | — |
| 6 | Sports odds provider (e.g. The Odds API) | the-odds-api.com | free tier, then paid |
| 7 | Forex data (broker API in read-only/paper mode, or a data vendor) | depends on your broker | varies |

**Why Telegram rather than SMS:** it's free, has an official bot API, delivers reliably, supports long messages and needs no public server. SMS through Twilio costs about $1/mo per number plus about $0.008 per message, and new US numbers need A2P 10DLC registration. SMS can be added later as a second channel through the same router.

## Implementation plan

1. ✅ **Foundation:** config, database, migrations, audit log, router, Telegram auth, health/config checks, dashboard
2. ✅ **Lead system:** CRM, classifier, drafts, follow-ups, review queue, briefings *(this release)*
3. **Email:** Gmail OAuth adapter (`integrations/email.py` interface already defined), polling ingestion into `ingest_message`, threaded replies linked to leads, approved-draft sending with idempotency keys and provider-confirmed message IDs
4. **Marketing:** content calendar, platform-specific post drafts built from `business.toml`, Canva assets, approval/publishing queue, manual-post packages for Nextdoor and Groups
5. **YouTube studio:** project folders, IDEA→PUBLISHED pipeline, scripts/storyboards/captions
6. **Sports research:** odds provider adapter, implied vs. model probability, closing-line tracking, paper mode only
7. **Forex research:** data adapter, backtester with out-of-sample and walk-forward testing, journal, paper trading; live trading stays disabled
8. **Hardening:** integration tests, restore drills, deployment checklist

## Information I still need from you

1. **Business facts** for `config/business.toml`: name, hours, service ZIPs/cities, and any pricing rules you approve, such as minimum charges or how you price by volume.
2. **Email provider:** Gmail / Google Workspace, Outlook or something else.
3. **Where leads come from today** (email, Facebook DMs, Nextdoor, phone/text, website form), so ingestion targets the right sources.
4. **Later, for Phase 7:** currency pairs, timeframes, broker/data provider, and maximum risk per trade and per day.
