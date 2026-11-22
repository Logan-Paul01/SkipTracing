# SkipTrace V1

SkipTrace V1 is a headless Python Telegram bot that builds a public-source
dossier for a Nigerian loan collector. The collector sends a debtor's full name,
phone number, gender and (optionally) company; the bot normalizes the number,
searches the open web for that person and their employer, pulls structured
contact and company fields out of the evidence it finds, scores every page
deterministically, and replies with one HTML dossier. It runs on long polling,
writes an audit log to SQLite, and never calls or messages the debtor.

Search is a real web-search API — **Exa** or **Tavily** — which returns live
pages plus a grounded, cited answer in a couple of seconds. Maigret has been
removed entirely: its 3,500-site walk and dependency tree made both the runtime
and the install far too heavy. With no search key, the bot falls back to a
handful of curated public-profile checks.

## How a trace runs

```
/trace
  → normalizer.normalize_phone      E.164 + digit variants (+234 only)
  → names.generate_usernames        rule-based + phonetic handle candidates
  → websearch.search_subject        Tavily/Exa pages + grounded answer,
                                    plus the company's own site when known
  → scorer.rank_hits                deterministic 0-100 score per page
  → extract.collect                 phones, WhatsApp, emails, socials,
                                    website, address, company summary
  → formatter.format_dossier        Telegram-safe HTML
  → db.log_search / log_matches     SQLite audit trail
```

| Module | Responsibility |
|---|---|
| `bot.py` | Telebot handlers, 4-step conversation, pipeline orchestration, plain-text fallback when Telegram rejects the HTML |
| `config.py` | All runtime settings, read from environment variables only |
| `normalizer.py` | Nigerian phone parsing; returns E.164, digit variants and carrier/region metadata |
| `names.py` | Orthographic rules plus soundex/metaphone/nysiis to derive candidate handles |
| `websearch.py` | **The search engine**: Exa and Tavily queries, grounded-answer prompt, direct company-site fetch |
| `searcher.py` | Curated public-profile checks (TikTok, Facebook, LinkedIn, GitHub, Nairaland), used only when no search key is set |
| `extract.py` | Structured person/company fields pulled from the answer, the pages and the company site |
| `scorer.py` | Auditable per-page confidence score and the report threshold |
| `formatter.py` | Dossier layout, clipping and length trimming |
| `access.py` | Free-search allowance, paywall, payment seam, `/broadcast` owner |
| `db.py` | SQLite audit log (`searches`, `matches`), accounts (`users`), `meta` |

## Dossier layout

Every dossier is built in the same order, because a collector reads it under
time pressure:

```
🔎 SKIPTRACE DOSSIER
Subject            as entered
Phone number       full E.164 number, never masked
Gender             as selected
Company            as entered, else the company named in the sources
Audit              searched by @username at 2026-11-22 09:14 UTC
Sources scanned    N public pages (or variations tested in fallback mode)

🧠 What the sources say
   One paragraph from the provider's grounded answer, clipped at a sentence
   boundary (700 chars). Citation markers are stripped; sources are listed
   below.

📇 Contact details
   Phone           the subject's number(s), input number suppressed
   WhatsApp        only when a source says a number is WhatsApp
   Email           personal addresses only
   LinkedIn / Facebook / Instagram / X / TikTok   up to two links each

🏢 Company details
   Company, About (one sentence), Website, Address, Phone(s), Email(s), socials
   "No company supplied and none named in the sources." when there is none

🏅 Top matching profiles
   🥇 Site — score/100, display name, quoted snippet, URL, ✔ reasons
   Three blocks when the dossier already carries structured contact or company
   data, eight when the evidence is thin.

Suggested next action: one line derived from the top match's reasons
```

The message is capped at 3,900 characters: if it overruns, whole profile blocks
are dropped from the bottom rather than slicing HTML mid-tag.

## Free searches and payment

Every account gets **5 free searches**. After that, **SkipTrace Premium** is
**₦200 for 30 days**, carrying up to **20 dossiers**. `FREE_SEARCHES`,
`PLAN_PRICE_NGN`, `PLAN_SEARCHES` and `PLAN_DAYS` change those numbers without
a code change.

- The allowance is checked when `/trace` starts, so nobody fills in four steps
  for a search they cannot run.
- A search is charged only after its dossier has been delivered: a failed
  search costs nothing.
- Each dossier is followed by one short receipt line (`🎟 4 of 5 free searches
  left.` or `⭐ Premium · 18 of 20 searches left · renews 21 Dec.`).
- A plan is a 30-day pass, not an auto-renewing subscription. When it lapses
  the leftover searches go with it; a renewal starts a clean 20.
- **No payment provider is connected yet.** The paywall says so. When one is
  added, `access.grant_plan(telegram_id)` is the only call needed after a
  confirmed ₦200 payment. Manual activation:
  `python3 -c "import access; access.grant_plan(123456789)"`.

Why 20 rather than 50: a trace with a company is seven Exa requests (about
$0.05, ₦70), so 50 searches would cost roughly ₦3,500 in API credits against
₦200 of revenue. Exa's free monthly credit (about 200 company traces) carries
the launch; 20 per plan keeps ten paying users inside it.

## Operator broadcast

`/broadcast` messages every account that has ever used the bot. It is hidden:
absent from `/start`, from the help text and from the BotFather command menu, and
unknown to any account that does not own it.

- `/broadcast <text>` sends immediately; `/broadcast` alone asks for the body in
  the next message, so multi-line announcements work. `/cancel` aborts.
- Messages go out prefixed with `📢 SkipTrace update`; HTML is allowed.
- Sends are spaced ~20/s, pause on Telegram's flood error, and report
  `Delivered`/`Failed` counts.
- Ownership needs no Telegram ID: the first account to send `/broadcast` claims
  it and it is stored in the database. Set `OWNER_ID` instead to lock the
  command to a known account. The owner is also exempt from the search quota.

`MANUAL.md` is the handover document: data model, allowance flow, broadcast
ownership, checks, deployment and open decisions.

## Search engine: Exa + Tavily

The engine is a real web-search API, not a username walker. With
`EXA_API_KEY` or `TAVILY_API_KEY` set, every trace fires several search queries
plus one grounded-answer prompt in parallel and gets back live pages with
extracted text and a cited summary. A full trace (name + company) finishes in
roughly 3-4 seconds including the company-site fetch.

Which provider runs:

- both keys set → **Exa** (cheaper per request, and more traces per free credit)
- only `EXA_API_KEY` set → **Exa** (`/search` for pages, `/answer` for the cited summary) — the provider in use here
- only `TAVILY_API_KEY` set → **Tavily** (pages and the grounded answer come back in one request)
- neither key set → the curated checks below

`SEARCH_MODE` defaults to `auto` / `web`, which means the Exa/Tavily web search.
It also accepts `http`, which forces the curated checks.

### Curated checks (no key required)

`searcher.py` is the only non-web path left: five public profile URLs (TikTok,
Facebook, LinkedIn, GitHub, Nairaland) requested directly, a page counting only
when its own `<title>` names the handle. It runs when no search key is
configured, when every web query comes back with zero pages, or when
`SEARCH_MODE=http` forces it. `PROFILE_TIMEOUT=10` and `PROFILE_CONCURRENCY=4`
bound it.

Maigret was deleted from the project. It walked ~3,500 sites per handle —
~89,000 requests for a full trace, measured at 4-5 hours — and pulled in Flask
plus a reporting stack, an unreasonable install for a 512 MB host. It also
checked far more sites than a dossier ever uses: the top eight leads are all a
collector reads.

### The queries

`build_queries` sends what a human skiptracer would type, all in parallel:

- with a company: `"name" "company"`, `"name" company Nigeria`, a
  `"company" official website address phone email contact` query and a
  `"company" facebook OR instagram OR twitter OR linkedin page` query
- without one: `"name" Nigeria`
- always: `"name" linkedin OR facebook OR instagram profile`
- the first phone variant with 10+ digits, quoted

A separate grounded-answer prompt asks for job title, employer, city, email,
mobile/WhatsApp number and profile URLs, then the company's description,
website, address, phone, email and social pages — so the dossier can lift those
fields by name instead of hoping a biography mentions them.

### The company site fetch

Search snippets rarely carry an office address, but the company's own pages do,
so once its domain is known that site is fetched directly:

- the domain comes from the hit hostnames and any domain named in the answer,
  scored by the summed length of matched company tokens (with a bonus when the
  answer names it) — this is what makes `generalgasng.com` beat `niglpgas.com`
  for "General Gas Nigeria Limited"
- the homepage is tried first, then `/contact`, on `www.` then the bare host,
  stopping as soon as a page already carries both an email and a `tel:`/`mailto:`
- `tel:`, `mailto:` and social `href`s are extracted, plus the visible text
- `WEB_SITE_TIMEOUT=3` per page, `WEB_SITE_TOTAL=4` for the whole fetch, and the
  merged evidence is capped at `WEB_SITE_CHARS=3200`
- a host that fails is not retried for 10 minutes, so a dead site cannot slow
  down every later trace of the same company

### What counts as a lead

A web page only becomes a lead if it also resembles the subject's name
(`WEB_MIN_NAME_SIMILARITY=0.55`). Without that floor, tutorials, blogs and data
lists that merely quote the debtor's phone number outrank the person's real
profile. Only profile hosts (LinkedIn, Facebook, Instagram, TikTok, X, YouTube,
Nairaland, GitHub) have a handle read from the URL; taking the first path
segment of any URL yields words like `reference` or `software`.

## Scoring

Deterministic and reproducible: every point comes from a named rule and each
rule that fires appends a human-readable reason. No model calls.

| Signal | Points |
|---|---|
| Public page or profile exists | +10 |
| Name similarity (order-independent, handles split on dots/underscores) | up to +30 |
| Handle *is* the debtor's phone number | +35 |
| Phone number appears in page text or bio | +25 |
| Gender matches | +10 |
| Location matches carrier region (partial) | +10 (+3) |
| Company name appears in the bio | +10 |
| Same handle on 2+ platforms | +10 |
| Page has both a display name and a bio | +5 |
| Login wall or private profile | −15 |
| Web page scoring below the name-similarity floor | score forced to 0 |

Anything under `MIN_SCORE_TO_REPORT=40` is dropped. Handles are tokenized on
dots, underscores and hyphens and checked for packed name tokens
(`chineduokafor`), so a normal full-name trace reports leads rather than always
coming back empty.

## What gets extracted

`extract.py` applies explicit rules to the grounded answer, the search snippets
and the company site, so every line traces back to a sentence or a URL a human
can open. A field no source supports stays empty and the dossier says so.

- **Phones** — Nigerian mobile and landline shapes only, normalized to
  `+234 803 123 4567`. Placeholder numbers from tutorials
  (`08031234567`, `08012345678`, repeated digits, ascending/descending runs) are
  rejected. A number is filed as the company's when the sentence names the
  company or uses office words (office, desk, enquiries, hotline, branch,
  customer care…), as WhatsApp when the sentence says so, otherwise as the
  subject's. The number already printed in the header is suppressed.
- **Emails** — freemail addresses (gmail, yahoo, outlook, icloud, proton…) or
  addresses in a sentence naming the subject go to the subject; addresses on
  the company domain go to the company. An unrelated domain in a page that
  merely mentions the company is dropped.
- **Social links** — split into person vs company by handle and by page title,
  with `www.`/non-`www.` forms treated as one link, up to two per platform.
- **Website** — only a hostname containing part of the company name, and never
  a directory or social/professional-network host.
- **Address** — a sentence from the answer or the company site containing a
  street/estate/plot hint, a digit, a comma or a state, with the lead-in and any
  glued-on "and its phone…" clause stripped.
- **About** — one sentence that mentions the company, does not mention the
  subject, and reads like a description ("is a", "provides", "supplies",
  "distributes"…).

## Setup

```bash
pip install -r requirements.txt
export BOT_TOKEN=123456789:your_botfather_token
export EXA_API_KEY=your_exa_key      # or TAVILY_API_KEY
python bot.py
```

Create the bot with [@BotFather](https://t.me/BotFather). Then open a chat with
it, send `/start`, and send `/trace` to run a search:

- `/start` — welcome text and the free-search allowance
- `/trace` — start a trace (name → phone → gender → company)
- `/skip` — accept an empty company at step 4
- `/cancel` — reset the conversation (also stops a `/broadcast` draft)

`/broadcast` exists but is deliberately undocumented in the bot: see
[Operator broadcast](#operator-broadcast).

A `.env` file is read automatically if it sits next to `bot.py` (git-ignored,
loaded at import, never overriding a variable that is already exported):

```
BOT_TOKEN=123456789:your_botfather_token
EXA_API_KEY=your_exa_key
```

`BOT_TOKEN` is the documented name; the project's own secret name
`Skiptracingbotkey` is accepted as a fallback. Phone numbers must be Nigerian
(+234). `DB_PATH` is optional and defaults to `skiptrace.db`.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `BOT_TOKEN` | — | BotFather token (required) |
| `Skiptracingbotkey` | — | Accepted fallback for `BOT_TOKEN` |
| `DB_PATH` | `skiptrace.db` | SQLite audit log |
| `SEARCH_MODE` | `auto` | `auto`/`web` (Exa/Tavily engine) or `http` (curated checks) |
| `ENV_FILE` | `.env` | Path read for keys when they are not exported |
| `EXA_API_KEY` | — | Enables the Exa engine |
| `TAVILY_API_KEY` | — | Enables the Tavily engine (Exa still wins when both are set) |
| `FREE_SEARCHES` | `5` | Free dossiers per account |
| `PLAN_PRICE_NGN` | `200` | Launch plan price shown in the paywall |
| `PLAN_SEARCHES` | `20` | Dossiers included in one plan period |
| `PLAN_DAYS` | `30` | Length of one plan period |
| `OWNER_ID` | — | Optional: locks `/broadcast` to one account |

Tuning constants live in `config.py`: `WEB_MAX_RESULTS=8`,
`WEB_HIGHLIGHT_CHARS=600`, `WEB_SEARCH_TIMEOUT=12`,
`WEB_SEARCH_OVERALL_TIMEOUT=30`, `WEB_SITE_TIMEOUT=3`, `WEB_SITE_TOTAL=4`,
`WEB_SITE_CHARS=3200`, `WEB_MIN_NAME_SIMILARITY=0.55`,
`MIN_SCORE_TO_REPORT=40`, `MAX_USERNAMES=25`, `PROFILE_TIMEOUT=10`,
`PROFILE_CONCURRENCY=4`.

Keys can come from the environment **or** from a `.env` file in the working
directory: `config.py` reads it at import time and never overrides a variable
that is already set. `.env` is git-ignored — never commit it, because this
repository is public.

## Deploy notes

Deploy as a worker process, not a web service: there is no HTTP port, no
webhook and no inbound traffic.

- Use a Python image. The included `Dockerfile` is based on `python:3.12-slim`,
  installs `gcc` and `libffi-dev` for the compiled dependencies, installs the
  pinned requirements, and copies only the runtime modules.
- Set `BOT_TOKEN` and the search key in the platform's secret settings. Never
  bake them into the image or commit them.
- Start command: `python bot.py`.
- Make sure the filesystem holding `DB_PATH` persists if the audit log should
  survive restarts. One instance only; Telegram long polling does not support
  two pollers on one token.

### Wispbyte

1. Create a free server, technology **Python**. 512 MB RAM and 1 GB disk are
   enough now that Maigret is gone.
2. **GitHub Integration** → repository URL, branch `main` → **Clone**. Wispbyte
   has no push webhook: after a commit, press **Pull**, then restart the server.
3. **Startup** → command: `pip install -r requirements.txt && python bot.py`.
4. **Startup → Server Configuration** → add `BOT_TOKEN` and `EXA_API_KEY` (or
   `TAVILY_API_KEY`). If the egg refuses custom variables, upload a `.env` with
   those two lines through **Files**; `config.py` reads it automatically.
5. Start, then watch **Console** for `Authenticated as @...`.

Free servers have no SFTP, and a pull only takes effect on the next restart.
Never run two pollers on one token: stop any other instance first.

## Security and hard rules

- Public sources only. The bot follows the same public URLs a browser would;
  no login-walled endpoints, no BVN/NIN data, no leaked-database lookups.
- No auto-calling and no auto-messaging. The dossier reports links and numbers;
  any contact is a human decision made outside the bot.
- No LLM calls from the bot's own code, and scoring is deterministic. The prose
  under "What the sources say" is the search provider's grounded answer,
  generated only over the pages just retrieved, and every claim links back to a
  listed source. It is a summary of search evidence, not proof of identity.
- Secrets come only from environment variables. `.env`, `*.db`, `bot.log`,
  `.venv/` and `__pycache__` are git-ignored, and `.env.example` holds
  placeholders only. `.dockerignore` keeps `.env`, `*.db`, `.venv` and `.git`
  out of the image.
- Every search is written to the local SQLite audit log with the collector's
  Telegram ID and username, the subject details and each reported match with
  its score and reasons.

## Why name variation is rule-based, not an LLM

Nigerian names are orthographically noisy but phonetically stable: a surname
that appears as "Chukwu", "Chuku" or "Chukwuka" on three platforms is still the
same identity to a human ear, and the same holds for "Olawale"/"Olaolu" and the
many d/t, b/p, k/c/q and s/z spellings in circulation. A deterministic rule set
(consonant-group swaps, vowel-digraph swaps, doubled-letter collapse, suffix
expansions, plus soundex, metaphone and nysiis agreement) covers that noise
cheaply and identically on every run, and every point in a dossier traces back
to a named rule — which matters when a collector has to justify an action. An
LLM would invent handles that do not exist, vary between identical inputs, and
blur the audit trail. Handle generation still feeds the no-key fallback checks,
which search by handle; the Exa/Tavily path matches on page titles, snippets
and URLs instead, so handle variation no longer decides a lead.

## Dependency pins

`maigret`, `python-Levenshtein` and Maigret's reporting dependencies are gone.
Maigret's 0.6.6 line hard-pins `aiohttp` and drags in Flask and a web report
module the bot never imports. With it removed the install is
`pyTelegramBotAPI`, `phonenumbers`, `jellyfish` and `aiohttp`, which fits
comfortably in a 512 MB container. The Levenshtein package went with it:
`names.py` uses a pure-Python ratio alongside `jellyfish`'s Jaro-Winkler, so the
C extension was never needed.

## Limitations

- The dossier is a set of leads to verify manually, not proof of identity.
- Search and grounded answers depend on the provider's coverage of Nigerian
  pages; an uncommon name or an unlisted company can come back thin or empty.
- A company site that is unreachable from the host (or down) yields no address
  or company email; the fetch is capped and then skipped for 10 minutes.
- The curated checks do not cover Instagram: logged-out Instagram serves a
  login page that cannot reliably distinguish a real profile, so it is skipped
  rather than reported as a match. Instagram profiles are still found through
  Exa/Tavily.
- Phone numbers must be Nigerian (+234).
