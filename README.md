# SkipTrace V1

SkipTrace V1 is a headless Python Telegram bot that builds a public-source
dossier for a Nigerian loan collector. The collector sends a debtor's full name,
phone number, gender and (optionally) company; the bot normalizes the number,
searches the open web for that person and their employer, pulls structured
contact and company fields out of the evidence it finds, scores every page
deterministically, and replies with one HTML dossier. It runs on long polling,
writes an audit log to SQLite, and never calls or messages the debtor.

Search is a real web-search API — **Exa** or **Tavily** — which returns live
pages plus a grounded, cited answer in a couple of seconds. Maigret is not the
engine and is not part of a normal trace; it survives only as an optional
legacy fallback for a host with no search key.

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
| `searcher.py` | Legacy fallback only — optional Maigret walk and the curated HTTP checks, reached when no search key is configured or the web search returns nothing |
| `extract.py` | Structured person/company fields pulled from the answer, the pages and the company site |
| `scorer.py` | Auditable per-page confidence score and the report threshold |
| `formatter.py` | Dossier layout, clipping and length trimming |
| `db.py` | SQLite audit log (`searches`, `matches`) |

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

## Search engine: Exa + Tavily

The engine is a real web-search API, not a username walker. With
`EXA_API_KEY` or `TAVILY_API_KEY` set, every trace fires several search queries
plus one grounded-answer prompt in parallel and gets back live pages with
extracted text and a cited summary. A full trace (name + company) finishes in
roughly 3-4 seconds including the company-site fetch.

Which provider runs:

- both keys set → **Tavily** (pages and the grounded answer come back in one request)
- only `EXA_API_KEY` set → **Exa** (`/search` for pages, `/answer` for the cited summary) — the provider in use wherever only the Exa key is configured
- neither key set → the legacy fallback below

`SEARCH_MODE` defaults to `auto` / `web`, which means the Exa/Tavily web search.
It also accepts `maigret` and `http`, which force the legacy paths.

### Legacy fallback (not used in normal operation)

`searcher.py` keeps two non-web paths for hosts with no search key and for
smoke tests. Neither is part of the Exa/Tavily pipeline:

- `maigret` — the classic username walk over ~3,500 sites per handle. With up
to 25 generated usernames that is ~89,000 requests, measured at 4-5 hours per
trace on this host. This is exactly why it is no longer the default.
- `http` — five curated public sites (TikTok, Facebook, LinkedIn, GitHub,
Nairaland) in a few seconds; useful on a blocked network.

The web path only drops to Maigret if no key is configured at all, or if every
query comes back with zero pages.

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

- `/trace` — start a trace (name → phone → gender → company)
- `/skip` — accept an empty company at step 4
- `/cancel` — reset the conversation

A `.env` file is not read automatically; export the variables in your shell or
inject them from your platform's secret store. `BOT_TOKEN` is the documented
name; the project's own secret name `Skiptracingbotkey` is accepted as a
fallback. Phone numbers must be Nigerian (+234). `DB_PATH` is optional and
defaults to `skiptrace.db`.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `BOT_TOKEN` | — | BotFather token (required) |
| `Skiptracingbotkey` | — | Accepted fallback for `BOT_TOKEN` |
| `DB_PATH` | `skiptrace.db` | SQLite audit log |
| `SEARCH_MODE` | `auto` | `auto`/`web` (Exa/Tavily engine), or the legacy `maigret` / `http` |
| `EXA_API_KEY` | — | Enables the Exa engine |
| `TAVILY_API_KEY` | — | Enables the Tavily engine (used instead of Exa when both are set) |

Tuning constants live in `config.py`: `WEB_MAX_RESULTS=8`,
`WEB_HIGHLIGHT_CHARS=600`, `WEB_SEARCH_TIMEOUT=12`,
`WEB_SEARCH_OVERALL_TIMEOUT=30`, `WEB_SITE_TIMEOUT=3`, `WEB_SITE_TOTAL=4`,
`WEB_SITE_CHARS=3200`, `WEB_MIN_NAME_SIMILARITY=0.55`,
`MIN_SCORE_TO_REPORT=40`, `MAX_USERNAMES=25`, `MAIGRET_TIMEOUT=12`,
`MAIGRET_MAX_CONNECTIONS=4`.

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
blur the audit trail. This governs only the legacy Maigret fallback, which
searches by handle; the Exa/Tavily path matches on page titles, snippets and
URLs instead, so handle variation no longer decides a lead.

## Dependency pins

`maigret==0.6.6` is pinned rather than `0.4.4` because `0.4.4` hard-pins
`aiohttp==3.8.1`, which conflicts with the required `aiohttp==3.14.5` and has no
Python 3.12 wheel, so the pair cannot be installed on `python:3.12-slim` at all.
`aiohttp==3.14.5` is the client version Maigret 0.6.6 requires and the bot only
uses its standard client API. Maigret also pulls optional reporting dependencies
(including Flask); `bot.py` never imports or runs them, so the bot stays a
headless long-polling process.

## Limitations

- The dossier is a set of leads to verify manually, not proof of identity.
- Search and grounded answers depend on the provider's coverage of Nigerian
  pages; an uncommon name or an unlisted company can come back thin or empty.
- A company site that is unreachable from the host (or down) yields no address
  or company email; the fetch is capped and then skipped for 10 minutes.
- The HTTP fallback does not check Instagram: logged-out Instagram serves a
  login page that cannot reliably distinguish a real profile, so it is skipped
  rather than reported as a match. The legacy Maigret path still checks it.
- Phone numbers must be Nigerian (+234).
