# SkipTrace V1 — operator and agent manual

Everything an engineer or agent needs to take this bot over without reading the
whole history. Read this first, then `README.md` for the user-facing description
and `formatter.py` for the dossier layout.

## What this is

A headless Python Telegram bot (`@AkipTrace_bot`) that builds a public-source
dossier for a Nigerian loan collector. The collector sends a debtor's full name,
phone number, gender, optional company and optional city/state; the bot searches the open web (Exa,
or Tavily when its key is set), scores every page deterministically, extracts
structured contact and company fields, and answers with one HTML dossier. It
polls with long polling, has no web server, and never contacts the debtor.

Not in scope, deliberately: Maigret (removed, too heavy for 512 MB), Groq/LLM
calls in the bot's own code, calling or messaging the debtor, leaked-database
lookups, BVN/NIN data.

## Files

| File | Role |
|---|---|
| `bot.py` | Telebot handlers, the 5-step conversation, the pipeline call, the hidden `/broadcast`, plain-text fallback when Telegram rejects HTML |
| `config.py` | Every runtime setting and tunable, from env or an optional `.env` |
| `access.py` | Per-account search allowance, the waitlist and manual-payment offer, `/stats` counters, and the `/broadcast` owner |
| `db.py` | SQLite: `searches`/`matches` audit log, `users` accounts, `meta` key/value |
| `normalizer.py` | Nigerian phone parsing → E.164, digit variants, carrier/region metadata |
| `names.py` | Rule-based + soundex/metaphone/nysiis handle candidates |
| `websearch.py` | The search engine: Exa/Tavily queries, grounded answer, company-site fetch |
| `searcher.py` | Curated profile checks, only used when no search key is set |
| `scorer.py` | Deterministic 0–100 score per page, plus the report threshold |
| `extract.py` | Person/company fields pulled from the answer, hits and company site |
| `formatter.py` | The dossier itself: HTML, the profiles view, clipping, length trimming, and the "did this trace find anything" rule |
| `banks.py` | Published account details for the "Associated banks" view |
| `MANUAL.md` | This document |

`skiptrace.db`, `.env`, `bot.log`, `.bot.pid`, `custom_venv/` and `__pycache__/`
are runtime state and are git-ignored. `custom_venv/` is where the dependencies
live on this host; it is not part of the project.

## Running it

```bash
cd <project directory>
set -a && . "$HOME/.cache/freebuff-project/secrets.env" && set +a
setsid nohup custom_venv/bin/python -u bot.py >> bot.log 2>&1 < /dev/null & echo $! > .bot.pid
```

- Logs: `bot.log` (line 1 of a healthy start is `Authenticated as @AkipTrace_bot`).
- Stop only through the recorded PID: `kill "$(cat .bot.pid)"`. Never
  `pkill -f bot.py`; the command line can match the agent's own shell.
- One poller per token. Telegram rejects a second `getUpdates` on the same bot,
  so stop the VM instance before starting the Wispbyte one.
- Secrets come from the environment or `.env`; `config.py` loads `.env` at
  import and never overrides an exported variable. Never print a token.

## Data model

`searches` — one row per dossier: collector's Telegram ID, timestamp, candidate
count, match count and `found` (1 when the dossier carried a record, 0 when it
came back empty, NULL on rows written before the flag existed). **No debtor
detail is stored:** the name, phone, gender, company and the reported profile
links are gone, and a build that still has them blanks them once on start
(`_purge_debtor_data`, guarded by `meta.pii_purged`). A write failure is logged
and never breaks the dossier.

`feature_votes` — `telegram_id`, `feature`, `vote`, `created_at`; one row per
account per feature question, replaced when the account answers again. Today it
holds the associated-bank poll (`associated_bank` → `yes` / `no`).

`users` — one row per Telegram account, created on first contact:

| Column | Meaning |
|---|---|
| `telegram_id` | Primary key |
| `telegram_username` | Last seen username, refreshed on contact |
| `first_seen`, `last_seen` | Contact timestamps |
| `free_searches_used` | Incremented per delivered dossier |
| `paid_searches_left` | Dossiers left in the current plan (or granted credits); spent before free searches |
| `plan`, `plan_expires_at` | `premium` and the end of the 30-day period; a lapsed plan is cleared before the quota is read |
| `purchases` | Lifetime count of plans started; never reset, so conversion still counts a lapsed payer | `waitlist` — one row per account that reached the paywall or sent `/premium`:
`telegram_id`, `telegram_username`, `created_at`, `claimed_paid_at`, 
`sender_name` (as typed by the buyer, or empty for a screenshot), `resolved_at`
(set when the operator activates or rejects the claim). This is the demand list:
it shows how many collectors got as far as being asked to pay, and
`db.pending_claims()` lists the ones still waiting for a decision.

`meta` — `owner_id` is stored here after the first `/broadcast`, plus
`pii_purged` once the debtor columns have been cleaned.

## Search allowance, waitlist and manual payment

Current policy: **10 free searches per account, then SkipTrace Premium — ₦200
for 30 days, up to 20 dossiers.** All four numbers are environment variables
(`FREE_SEARCHES`, `PLAN_PRICE_NGN`, `PLAN_SEARCHES`, `PLAN_DAYS`), so the offer
can change without a release. A plan is a 30-day pass rather than an
auto-renewing subscription until billing exists.

Flow in `bot.py`:

1. `/trace` calls `access.can_search(uid)` **before** asking for the five
   inputs. No allowance → the account joins the waitlist and gets the paywall,
   and no conversation starts.
2. `_run_and_reply` calls `access.charge(uid)` **after** the dossier is sent,
   and only when the trace actually found something. A failed search costs
   nothing, and `formatter.findings()` decides whether an empty result is
   charged: no profile above the reporting floor, no contact detail and no
   company address/phone/website/social means the message carries
   `🟢 No public record found` and the balance is untouched. A company that
   exists is a finding, so "person not found, business found" still counts.
3. A one-line receipt follows each dossier (`🎟 9 of 10 free searches left.`).
4. `/plan` repeats that accounting on demand: free searches used and left on
   the free plan, or plan searches left plus the period end on Premium (and any
   free searches still in hand). Owner accounts are told searches are not
   counted.

Each dossier is one message with two stacked buttons — **🏅 Top matching
profiles** and **🏦 Associated bank** — and each opens by editing that same
message, with **⬅️ Back to dossier** to return. Views are held in memory under
a short token for six hours (`VIEWS` in `bot.py`), only open for the account
that ran the trace, and old buttons say the dossier expired after a restart.

The bank tab is a **feature question, not a lookup**. It says in plain words
what an associated-bank lookup would do — take the phone number from step 2 and
show the bank the account is held with — states that it is not available because
a phone number does not carry the bank, and offers **✅ Yes, I would use it** and
**🙅 No, I would not**. Answers go to `feature_votes` and are counted in
`/stats`; a second answer replaces the first. The copy stays neutral: it asks
whether the feature is wanted, it does not argue for it.

A trace that finds nothing ends with **📖 How to get better results**, which
opens `/guide`: what details help (full name, company, city/state, a known
variant of the name), what the bot cannot do (look a phone number up, see
private or logged-in pages) and how to read a dossier. It deliberately does not
tell the collector to run the same trace again with something added — that is
what makes a thin result feel like a dead end.

**The paid plan is a waitlist with manual payment; no provider is connected.**
`/premium` (and the paywall) prints the offer plus the bank, account name and
account number from `PAYMENT_BANK`, `PAYMENT_ACCOUNT_NAME` and
`PAYMENT_ACCOUNT_NUMBER` — never hardcoded, so the account can change with the
server's secrets. The collector transfers ₦200 and sends `/paid`, which asks for
the **sender name** on the transfer; a receipt screenshot is accepted in the same
state (forwarded to the owner by file id, never downloaded or stored).

That claim arrives as one card for the operator:

```
💳 Payment claim
@collector · 123456789
Sender: ADEBAYO TUNDE
Plan: ₦200 · 30 days · 20 searches
[✅ Activate] [❌ Not found]
```

**✅ Activate** calls `access.grant_plan(uid)`, marks the claim resolved, and
messages the buyer `⭐ SkipTrace Premium is active`. **❌ Not found** closes the
claim and asks the buyer to resend the sender name. Buttons only respond to the
owner; anyone else gets "This is the operator's decision." A handled claim cannot
be activated twice.

The same activation by hand, if a card is lost:

```bash
python3 -c "import db, access; db.init_db(); access.grant_plan(123456789)"
python3 -c "import db; [print(u) for u in db.all_users()]"   # find the ID
python3 -c "import db; [print(c) for c in db.pending_claims()]"   # waiting claims
```

`access.grant_plan` also increments `users.purchases`, so a lapsed plan still
counts as a payer. `access.grant_searches(uid, n)` adds plain credits with no
period, for a goodwill top-up, and does not count as a purchase.

Demand gate: the offer stays at ₦200 until **30% of accounts have paid**, then
the price moves to ₦500 together with a new feature. `/stats` is the readout.

The owner (see below) is exempt from the quota so the bot can always be tested.

## Hidden operator command: /broadcast

`/broadcast` messages every account that has ever used the bot.

- It is **not** mentioned in `/start`, not in the help text, and not registered
  with `setMyCommands`. Do not add it to the BotFather menu.
- `/broadcast <text>` sends immediately. `/broadcast` alone asks for the body in
  the next message, which allows multi-line text. `/cancel` aborts.
- Each send is prefixed with `📢 SkipTrace update`; HTML is allowed and falls
  back to plain text if Telegram rejects the markup.
- Rate limiting: 0.05 s between sends (~20/s, under Telegram's ~30/s), a 3 s
  pause on HTTP 429, and a progress edit every 25 recipients.
- A blocked user or a dead chat counts as `Failed` and does not stop the run.

### Operator stats: /stats

`/stats` is owner-only and hidden like `/broadcast`: anyone else gets the
ordinary unknown-command text. It reports accounts, collectors who ran a trace,
dossiers delivered, waitlist size, payment claims, paid accounts (lifetime), paid
accounts with an allowance now, and the conversion percentage against the 30%
go/no-go line. Below the counters it lists every claim still waiting, with the
sender name and ID, so a card that was scrolled past can still be acted on.

`/backup` sends the database and the revenue CSV to the operator's chat, so the
ledger is recoverable without SSH or FTP; that is the only backup that survives
the panel.

A payment that arrives before the operator has claimed ownership is not lost:
the claim is stored, and claiming `/broadcast` immediately re-sends every
waiting claim as a card. Until then the bot logs a warning on startup that no
operator is set.

```
📊 SkipTrace usage
Accounts: 12
Collectors who ran a trace: 9
Dossiers delivered: 40
With a record: 31 · nothing found: 9
On the waitlist: 5
Said they paid: 2 (0 waiting)
Paid accounts (ever): 2
Paid accounts with an allowance now: 2
Conversion: 16.7% of accounts (go/no-go: 30%)
Associated-bank poll: 4 yes · 1 no
```

Ownership, without configuring a Telegram ID:

1. If `OWNER_ID` is set, only that account can use the command.
2. Otherwise the **first** account to send `/broadcast` claims it, and the ID is
   stored in `meta.owner_id` permanently.
3. Anyone else receives the ordinary unknown-command text, so the command name
   reveals nothing. To move ownership, delete the `meta` row:

```bash
python3 -c "import db; db.init_db(); db.meta_set('owner_id', '')"
```

Claim it once, right after starting the bot, if the command must not be
snatched by someone who guesses the name.

## Search pipeline (unchanged by billing work)

```
/trace → normalizer → names → websearch (Exa/Tavily)
       → scorer.rank_hits → extract.collect → formatter.format_dossier
       → db.log_search/log_matches → access.charge → receipt
```

Only `formatter.py` renders; all cleaning of scraped titles/snippets lives
there (`_clean_text`, `_clean_title`, `_pretty_handle`, `_short_url`,
`_evidence_line`) so provider markup never reaches the reader. Each ranked
match is a fixed card: `medal + site — score/100`, bold name, quoted excerpt,
short blue link, italic evidence line (max 3 signals).

## Checks worth running before a restart

```bash
python3 -m py_compile bot.py config.py access.py db.py extract.py formatter.py \
    scorer.py websearch.py normalizer.py names.py searcher.py
```

Offline pipeline smoke test (no Telegram, no keys needed for the fallback path):

```bash
python3 - <<'PY'
import asyncio, websearch, scorer, formatter
hits, answer = asyncio.run(websearch.search_subject("Ayodeji Ibrahim Balogun", "Musician"))
ctx = {"name": "Ayodeji Ibrahim Balogun", "gender": "Male", "company": "Musician",
       "phone_e164": "+2349029327067", "phone_variants": ["9029327067"], "meta": {}}
profiles = {h["url"]: {"display_name": h.get("title", ""), "bio": h.get("snippet", "")} for h in hits}
matches = scorer.rank_hits(hits, profiles, ctx)
print(formatter.format_dossier(ctx, matches, 12, "@Nifemi8000", answer=answer,
                               sources_count=len(hits), hits=hits))
PY
```

Quota/broadcast smoke test (uses a throwaway database, never the live one):

```bash
DB_PATH=/tmp/quota.db python3 - <<'PY'
import db, access
db.init_db()
db.touch_user(111, "tester")
print(access.status(111))
for _ in range(6):
    print(access.can_search(111))
    if access.can_search(111):
        access.charge(111)
print(access.status(111), access.paywall(111))
access.grant_plan(111)
print(access.can_search(111), access.receipt(111))
print(access.claim_owner(111), access.is_owner(111), access.is_owner(222))
PY
```

## Deployment (Wispbyte free, 512 MB)

Worker process only: no port, no webhook, no inbound traffic.

1. Technology **Python**, GitHub integration → `Logan-Paul01/SkipTracing`,
   branch `main`. Wispbyte has no push webhook: **Pull**, then restart.
2. Startup command: `bash start.sh` (installs `requirements.txt` unless the
   file is unchanged since the last install, then runs `python -u bot.py`).
   `pip install -r requirements.txt && python bot.py` also works if the panel
   runs it as a shell line.
3. Server Configuration → secrets: `BOT_TOKEN`, `EXA_API_KEY` (or
   `TAVILY_API_KEY`), `OWNER_ID`, `PAYMENT_BANK`, `PAYMENT_ACCOUNT_NAME`,
   `PAYMENT_ACCOUNT_NUMBER`. If custom variables are refused, upload a `.env`
   with those lines; `config.py` reads it. Free servers have no SFTP after
   start.
4. Watch the console for `Authenticated as @...`.
5. Make sure the filesystem holding `DB_PATH` persists, otherwise the account
   counters reset on restart and every user gets ten fresh free searches.

### Updating the live server

A restart does **not** fetch code: it re-runs `start.sh` against the files
already on disk. The new commit only goes live after a successful pull.

1. **Stop** the server.
2. **GitHub Integration → Pull**; watch the GitHub tab until it shows the new
   commit rather than an error.
3. **Start** and read the console. `Build <short-sha>` is logged at startup; if
   it matches the commit just pushed, the release is live. If it is the old
   hash, the pull did not apply — the GitHub tab has the reason.

No server configuration changes for a release: the command stays
`bash start.sh`, and `.deps-stamp` makes an unchanged `requirements.txt` skip
pip so a restart takes seconds instead of a download.

### Backing up: /backup

Owner-only and hidden like `/stats`. It writes a consistent copy of the live
database and a revenue CSV, sends both to the operator in Telegram, then deletes
its temporary files — the panel's 1 GB disk does not keep a second copy. The CSV
has one row per account: id, username, first/last seen, plan, expiry, purchases,
paid searches left, free searches used, dossiers run, payment state
(`waitlist` / `claimed` / `confirmed`) and the associated-bank answer
(`bank_vote`: `yes`, `no` or empty). Neither file contains anything about a
traced person.

## Gotchas

- Telegram rejects messages over 4,096 characters; `formatter` caps at 3,900
  and drops whole match blocks rather than slicing HTML.
- `_send_html` retries as plain text on `ApiTelegramException`, so a formatting
  bug degrades instead of losing the dossier.
- `config.BOT_TOKEN` falls back to the secret name `Skiptracingbotkey`.
- Exa is the default provider; only `EXA_API_KEY` is live in this workspace, so every trace here runs on Exa.
- The debtor's number is never used as a search query: it only builds handle
  candidates and scores pages that mention it, so a trace spends no credit on
  it.
- `generalgasng.com` was unreachable from this VM once, so that test subject's
  company address/email came back partial. The host fetch is capped at 3 s per
  page and skipped for 10 minutes after a failure.
- The audit log must never raise into the pipeline: writes are wrapped.
- Namesake noise: profiles of a different surname can still score ~40 because
  of shared first names and company mentions. Known, not fixed.
- Company fields need the whole name: every significant word of the entered
  company must appear in the source, so "Jacobs Memoria Farms" (Owerri) no
  longer takes the street address, phone lines and LinkedIn page of "Grace
  Jacobs Farms Limited" (Benin City). A business whose sources only ever print a
  shortened name now shows the empty-block line instead of another firm's
  details.

## Open decisions

1. **Launch pricing** — ₦200 / 30 days / 20 searches is the demand test, sold
   through the waitlist with a manual transfer. It is deliberately inside the
   free Exa credit; at paid API rates (≈₦70 per company trace) the worst case
   costs more than the price. Go/no-go: 30% of accounts paying, read from
   `/stats`. If it passes, move to ₦500 with a new feature. If the free credit
   runs out first, cut the per-trace query count as well as raising the price.
2. **Payment provider** — none; manual transfer plus `/paid` and
   `access.grant_plan`. A provider can replace the transfer later without
   touching the quota logic.
3. **Ranking quality** — namesake profiles with a different surname still leak
   into the top matches. Emails are safer: a freemail address needs its
   sentence or page to carry the subject's whole name, and a surname-only
   social handle is dropped once a full-name link exists.
