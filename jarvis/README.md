# JARVIS — personal business operating system

JARVIS is a private assistant for a **junk removal + home cleaning** business that you control from **Telegram** on your phone and from a **mobile-friendly web dashboard**. Version 0.2 handles leads and Gmail. It reads your business inbox, sorts incoming messages, keeps a CRM, drafts replies, emails them once you approve, tracks follow-ups and sends you daily briefings. It also keeps an audit log of everything it does.

> It lives in `jarvis/`, separate from the Krypt Trader app in the rest of this repository, and shares no code or data with it.

## What actually works today (v0.2)

| Area | Status | Notes |
|---|---|---|
| Lead CRM (SQLite, Postgres-ready) | ✅ working, tested | stages, duplicate detection by email/phone, history, search/filter, follow-up dates |
| Message classifier | ✅ working, tested | rule-based; 7 categories; sensitive/injection/uncertain messages always go to **your review** |
| Reply drafting | ✅ working, tested | asks only for missing details; a policy guard blocks prices, guarantees, confirmations, insurance claims, discounts and coverage claims |
| Draft approval + sending | ✅ working, tested | `/approve` then `/send` replies in the customer's Gmail thread. Sending is two explicit steps, never done twice, and only marked sent when Gmail returns a message ID. You can also `/edit` a draft first |
| Telegram bot | ✅ code complete, tested with a fake client | needs your bot token + user ID. Not live-tested: Telegram is unreachable from the build sandbox |
| Telegram security | ✅ tested | user-ID allowlist, private chats only, rate limit, unauthorized attempts audited |
| Briefing / evening review | ✅ working | on demand; scheduled once you set a timezone and times |
| Scheduler | ✅ tested | runs each job once per day (enforced by the database), skips briefings that are more than 2h late |
| Dashboard | ✅ working, tested | password login, signed cookie, CSRF, security headers, command box using the same router as Telegram |
| Audit log | ✅ working | phone numbers and emails are redacted; searchable in the dashboard |
| Config validation / health / backup | ✅ working | never prints secrets |
| Gmail inbox sync | ✅ code complete, tested against the real Google client library (offline) | checks every 5 min; skips newsletters, no-reply senders, promotions and social; attaches replies in the same thread to the lead. **Not yet run against your real inbox:** needs your one-time Google sign-in |
| Your own Gmail replies | ✅ tested | replies you send from the Gmail app are detected, so leads don't show as unanswered |
| Automatic replies | ✅ built, **off by default** | when enabled: first contact only, confidence ≥ 0.9, no policy notes, business name + service area configured, capped per 24h |
| SMS / Facebook / Nextdoor messages | ⏳ not connected | paste them with `/inquiry` and reply yourself |
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
- `/send 1` if Gmail is connected and the lead has an email address
- otherwise send it yourself, then `/manualsent 1`

Or from the terminal:
```bash
.venv/bin/jarvis ingest --name "Dana" --contact dana@example.com --text "Need a couch hauled away"
```
```bash
.venv/bin/jarvis ask /briefing
```

### 7. Connect Gmail (one time, about 10 minutes, free)

JARVIS asks Google for exactly two permissions: **read** mail and **send** mail. It can't delete, archive, label or change settings, and it never sees your password.

1. Go to https://console.cloud.google.com and create a project called `JARVIS`.
2. Open **APIs & Services → Library**, search **Gmail API** and click **Enable**.
3. Open **Google Auth Platform** (older consoles call it **OAuth consent screen**) and click **Get started**:
   - App name `JARVIS`, with your email as the support email.
   - **Audience:** *Internal* if you use Google Workspace, *External* for a regular @gmail.com account.
4. External accounts only: under **Audience → Test users**, add your own Gmail address.
5. Under **Clients → Create client**, pick **Desktop app** and click Create, then **Download JSON**. Save the file as `jarvis/secrets/google_client_secret.json`. The `secrets/` folder is git-ignored.
6. On the computer that runs JARVIS:
   ```bash
   .venv/bin/jarvis gmail-auth
   ```
   A browser opens. Sign in with your **business** Gmail. Google will warn that the app isn't verified; that's expected for your own private app, so click *Continue* and allow both permissions. The token is saved to `secrets/gmail_token.json` with private file permissions.
7. Verify (read-only, changes nothing):
   ```bash
   .venv/bin/jarvis gmail-check
   ```
8. Process your inbox once, or just restart `jarvis bot`, which checks every 5 minutes:
   ```bash
   .venv/bin/jarvis gmail-sync
   ```
   The first sync looks back 3 days (`GMAIL_LOOKBACK_DAYS`). New leads, sensitive emails and sync errors arrive on Telegram; quiet periods stay quiet.

> **Important, External accounts:** while your Google app is in **Testing** status, Google expires the sign-in after **7 days**. JARVIS then messages you on Telegram that Gmail sync failed, and you rerun `jarvis gmail-auth`. To stop the weekly re-login, go to **Google Auth Platform → Audience** and click **Publish app**. For a private app used only by you, Google shows the "unverified" warning at sign-in, but no review is needed for your own account. Check Google's current policy on the console page. Workspace accounts using *Internal* aren't affected.

To turn on automatic first replies after you've reviewed drafts for a while, set `JARVIS_AUTO_REPLY_ENABLED=true` in `.env` and restart. Every automatic send is logged and reported to you on Telegram.

### Run the tests

```bash
.venv/bin/python -m pytest -q
```

## Commands

`/briefing` `/evening` `/leads [search]` `/junk` `/cleaning` `/followups` `/unanswered` `/lead <id>` `/stage <id> <STAGE>` `/inquiry <message>` `/drafts` `/draft <id>` `/edit <id> <text>` `/approve <id>` `/reject <id>` `/send <id>` `/manualsent <id>` `/email` `/review` `/resolve <id>` `/status` `/settings` `/marketing` `/help`

- `/send` emails an **approved** draft through Gmail. It replies only to the address the customer wrote from, never to an address mentioned inside a message.
- If a send fails, the draft is marked FAILED with the error. Check Gmail's Sent folder, then `/approve` + `/send` to retry.
- Generated drafts are blocked from sending if they contain prices, guarantees, confirmations or similar. Text you write yourself with `/edit` only triggers a warning; it's your call.

Natural language works too, for example "JARVIS, find cleaning leads that haven't received a reply" or "show me which integrations aren't connected".

Lead stages: `NEW → QUALIFYING → NEEDS_INFORMATION → READY_FOR_QUOTE → QUOTE_PENDING_APPROVAL → QUOTE_SENT → FOLLOW_UP → BOOKED → COMPLETED → CLOSED`

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

- **Level 1 (automatic):** reading Gmail, classifying, drafting, reports, reminders, and changes to JARVIS's own records.
- **Customer emails:** you approve, then you send (`/approve` + `/send`).
- **Level 2 (pre-authorized):** routine first replies, only if you set `JARVIS_AUTO_REPLY_ENABLED=true`. It's capped per day and limited to clean, high-confidence first contacts. It's off by default.
- **Level 3 (explicit approval):** refunds, money, live trades, bets, public posts. None of these can be reached from v0.2. Complaints and refund requests are never drafted, let alone sent.

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
  email_workflow.py  Gmail sync, owner-reply detection, safe sending, gated auto-reply
  integrations/      telegram.py (Bot API via httpx), gmail.py (official Gmail API + OAuth),
                     email.py (parsing, quote stripping, bulk detection), base.py (status registry)
  web/               FastAPI dashboard + templates
  security.py        allowlist + rate limiter
  logging_setup.py   redaction of tokens, emails and phone numbers
```

One process runs the bot, the scheduler and Gmail polling, and a second process runs the dashboard. Both share the database. There are no paid services.

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
- `jarvis gmail-check` confirms Gmail access. Rerun `jarvis gmail-auth` if Google sign-in expired.
- **When moving to a server**, copy `secrets/gmail_token.json` over with `scp`. Don't run the browser sign-in on a headless server.

## Credentials you'll need, and when

| Phase | Credential | Where | Cost |
|---|---|---|---|
| 1 (now) | Telegram bot token + your user ID | @BotFather, @userinfobot | free |
| 3 (now) | Google OAuth Desktop client for the Gmail API (`gmail.readonly` + `gmail.send`) | see "Connect Gmail" above | free |
| 3 (optional) | LLM API key for nicer drafts (`ANTHROPIC_API_KEY`) | console.anthropic.com | usage-based, roughly a few $/mo at this volume |
| 4 | Meta app + Facebook **Page** access token (`pages_manage_posts`) | developers.facebook.com, needs app review for publishing | free |
| 4 | Canva Connect API (OAuth) | canva.com/developers | free tier |
| 4 | Nextdoor | the public API is limited; expect manual "ready-to-post" packages | — |
| 6 | Sports odds provider (e.g. The Odds API) | the-odds-api.com | free tier, then paid |
| 7 | Forex data (broker API in read-only/paper mode, or a data vendor) | depends on your broker | varies |

**Why Telegram rather than SMS:** it's free, has an official bot API, delivers reliably, supports long messages and needs no public server. SMS through Twilio costs about $1/mo per number plus about $0.008 per message, and new US numbers need A2P 10DLC registration. SMS can be added later as a second channel through the same router.

## Implementation plan

1. ✅ **Foundation:** config, database, migrations, audit log, router, Telegram auth, health/config checks, dashboard
2. ✅ **Lead system:** CRM, classifier, drafts, follow-ups, review queue, briefings
3. ✅ **Email:** Gmail OAuth, inbox sync, thread linking, owner-reply detection, approve-then-send, gated auto-reply *(this release)*. Still open: follow-up email drafts for leads that went quiet, and receipt/booking email sorting
4. **Marketing:** content calendar, platform-specific post drafts built from `business.toml`, Canva assets, approval/publishing queue, manual-post packages for Nextdoor and Groups
5. **YouTube studio:** project folders, IDEA→PUBLISHED pipeline, scripts/storyboards/captions
6. **Sports research:** odds provider adapter, implied vs. model probability, closing-line tracking, paper mode only
7. **Forex research:** data adapter, backtester with out-of-sample and walk-forward testing, journal, paper trading; live trading stays disabled
8. **Hardening:** integration tests, restore drills, deployment checklist

## Information I still need from you

1. **Business facts** for `config/business.toml`: name, hours, service ZIPs/cities, and any pricing rules you approve, such as minimum charges or how you price by volume.
2. **Gmail type:** a regular @gmail.com account or Google Workspace. This decides *External* vs *Internal* in the Gmail setup.
3. **Where else leads come from** (email, Facebook DMs, Nextdoor, phone/text, website form), so ingestion targets the right sources.
4. **Later, for Phase 7:** currency pairs, timeframes, broker/data provider, and maximum risk per trade and per day.
