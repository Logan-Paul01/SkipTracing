# SkipTrace V1 — operator and agent manual

Everything an engineer or agent needs to take this bot over without reading the
whole history. Read this first, then `README.md` for the user-facing description
and `formatter.py` for the dossier layout.

## What this is

A headless Python Telegram bot (`@AkipTrace_bot`) that builds a public-source
dossier for a Nigerian loan collector. The collector sends a debtor's full name,
phone number, gender and optional company; the bot searches the open web (Exa,
or Tavily when its key is set), scores every page deterministically, extracts
structured contact and company fields, and answers with one HTML dossier. It
polls with long polling, has no web server, and never contacts the debtor.

Not in scope, deliberately: Maigret (removed, too heavy for 512 MB), Groq/LLM
calls in the bot's own code, calling or messaging the debtor, leaked-database
lookups, BVN/NIN data.

## Files

| File | Role |
|---|---|
| `bot.py` | Telebot handlers, the 4-step conversation, the pipeline call, the hidden `/broadcast`, plain-text fallback when Telegram rejects HTML |
| `config.py` | Every runtime setting and tunable, from env or an optional `.env` |
| `access.py` | Per-account search allowance, the paywall text, the payment seam, and the `/broadcast` owner |
| `db.py` | SQLite: `searches`/`matches` audit log, `users` accounts, `meta` key/value |
| `normalizer.py` | Nigerian phone parsing → E.164, digit variants, carrier/region metadata |
| `names.py` | Rule-based + soundex/metaphone/nysiis handle candidates |
| `websearch.py` | The search engine: Exa/Tavily queries, grounded answer, company-site fetch |
| `searcher.py` | Curated profile checks, only used when no search key is set |
| `scorer.py` | Deterministic 0–100 score per page, plus the report threshold |
| `extract.py` | Person/company fields pulled from the answer, hits and company site |
| `formatter.py` | The dossier itself: HTML, clipping, length trimming |
| `MANUAL.md` | This document |

`skiptrace.db`, `.env`, `bot.log`, `.bot.pid`, `custom_venv/` and `__pycache__/`
are runtime state and are git-ignored. `custom_venv/` is where the dependencies
live on this host; it is not part of the project.

## Running it

```bash
cd /workspace/chats/c1834984ec767b84dbb7acc0
set -a && . "$HOME/.cache/freebuff-project/secrets.env" && set +a
setsid nohup env PYTHONPATH=custom_venv python3 -u bot.py >> bot.log 2>&1 & echo $! > .bot.pid
```

- Logs: `bot.log` (line 1 of a healthy start is `Authenticated as @AkipTrace_bot`).
- Stop only through the recorded PID: `kill "$(cat .bot.pid)"`. Never
  `pkill -f bot.py`; the command line can match the agent's own shell.
- One poller per token. Telegram rejects a second `getUpdates` on the same bot,
  so stop the VM instance before starting the Wispbyte one.
- Secrets come from the environment or `.env`; `config.py` loads `.env` at
  import and never overrides an exported variable. Never print a token.

## Data model

`searches` / `matches` — one row per dossier and one per reported page, with the
collector's Telegram ID, the subject, score and reasons. Written after every
trace; a write failure is logged and never breaks the dossier.

`users` — one row per Telegram account, created on first contact:

| Column | Meaning |
|---|---|
| `telegram_id` | Primary key |
| `telegram_username` | Last seen username, refreshed on contact |
| `first_seen`, `last_seen` | Contact timestamps |
| `free_searches_used` | Incremented per delivered dossier |
| `paid_searches_left` | Dossiers left in the current plan (or granted credits); spent before free searches |
| `plan`, `plan_expires_at` | `premium` and the end of the 30-day period; a lapsed plan is cleared before the quota is read |

`meta` — `owner_id` is stored here after the first `/broadcast`.

## Search allowance and payment

Current policy: **5 free searches per account, then SkipTrace Premium — ₦200
for 30 days, up to 20 dossiers.** All four numbers are environment variables
(`FREE_SEARCHES`, `PLAN_PRICE_NGN`, `PLAN_SEARCHES`, `PLAN_DAYS`), so the offer
can change without a release. A plan is a 30-day pass rather than an
auto-renewing subscription until billing exists.

Flow in `bot.py`:

1. `/trace` calls `access.can_search(uid)` **before** asking for the four
   inputs. No allowance → paywall text, no conversation started.
2. `_run_and_reply` calls `access.charge(uid)` **after** the dossier is sent.
   A failed search costs nothing, because the charge only happens on success.
3. A one-line receipt follows each dossier (`🎟 4 of 5 free searches left.`).

**No payment provider is connected.** `access.grant_plan(uid)` is the only seam
a payment integration needs: confirm the ₦200 payment, call it, and the existing
quota logic spends the allowance. Activating by hand:

```bash
python3 -c "import db, access; db.init_db(); access.grant_plan(123456789)"
python3 -c "import db; [print(u) for u in db.all_users()]"   # find the ID
```

`access.grant_searches(uid, n)` adds plain credits with no period, for a
goodwill top-up.

The owner (see below) is exempt from the quota so the bot can always be tested.

The commercial shape is recorded under "Open decisions" at the end.

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
hits, answer = asyncio.run(websearch.search_subject("Ayodeji Ibrahim Balogun", "Musician", ["9029327067"]))
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
2. Startup command: `pip install -r requirements.txt && python bot.py`.
3. Server Configuration → secrets: `BOT_TOKEN`, `EXA_API_KEY` (or
   `TAVILY_API_KEY`). If custom variables are refused, upload a `.env` with
   those lines; `config.py` reads it. Free servers have no SFTP after start.
4. Watch the console for `Authenticated as @...`.
5. Make sure the filesystem holding `DB_PATH` persists, otherwise the account
   counters reset on restart and every user gets five fresh free searches.

## Gotchas

- Telegram rejects messages over 4,096 characters; `formatter` caps at 3,900
  and drops whole match blocks rather than slicing HTML.
- `_send_html` retries as plain text on `ApiTelegramException`, so a formatting
  bug degrades instead of losing the dossier.
- `config.BOT_TOKEN` falls back to the secret name `Skiptracingbotkey`.
- Exa is the default provider; only `EXA_API_KEY` is live in this workspace, so every trace here runs on Exa.
- `generalgasng.com` was unreachable from this VM once, so that test subject's
  company address/email came back partial. The host fetch is capped at 3 s per
  page and skipped for 10 minutes after a failure.
- The audit log must never raise into the pipeline: writes are wrapped.
- Namesake noise: profiles of a different surname can still score ~40 because
  of shared first names and company mentions. Known, not fixed.

## Open decisions

1. **Launch pricing** — ₦200 / 30 days / 20 searches is the demand test. It is
   deliberately inside the free Exa credit; at paid API rates (≈₦70 per company
   trace) the worst case costs more than the price. When the free credit runs
   out, either raise the price (₦500 for 50, ₦1,000 for 120) or cut the
   per-trace query count. 50 searches at ₦200 would burn the whole monthly
   credit with about four subscribers.
2. **Payment provider** — none chosen; `access.grant_plan` is the seam.
3. **Ranking quality** — namesake profiles with a different surname still leak
   into the top matches, and `extract.collect` can attribute a namesake's email.
