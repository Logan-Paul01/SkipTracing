"""Runtime configuration for SkipTrace V1.

Every secret comes from the environment or an optional git-ignored ``.env``
file. Nothing here is committed with a value, and the module never writes
configuration to disk.
"""

import os


def _int_env(name: str, default: int) -> int:
    """A whole number from the environment, never crashing on a bad value."""
    try:
        return int(str(os.environ.get(name, "")).strip() or default)
    except ValueError:
        return default


def _load_dotenv(path: str) -> None:
    """Load KEY=VALUE lines from an optional .env file.

    Real environment variables always win, so a host's secret store is never
    overridden by a stale file. The file is git-ignored and must never be
    committed: this repository is public.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(os.environ.get("ENV_FILE", ".env"))

# Required. bot.py exits with a clear error when this is missing. The
# Skiptracingbotkey fallback matches the secret name configured in this
# project, so the documented start command works without renaming it.
BOT_TOKEN = (
    os.environ.get("BOT_TOKEN", "").strip()
    or os.environ.get("Skiptracingbotkey", "").strip()
)

# SQLite audit log location.
DB_PATH = os.environ.get("DB_PATH", "skiptrace.db")

# Free dossiers per account before the bot asks for payment. PRICE is only the
# number printed in the paywall: no payment provider is connected yet.
FREE_SEARCHES = max(0, _int_env("FREE_SEARCHES", 5))
PRICE_PER_SEARCH_NGN = max(0, _int_env("PRICE_PER_SEARCH_NGN", 200))

# Optional pin for the hidden /broadcast command. When it is empty the first
# account to send /broadcast claims ownership, so no Telegram ID has to be
# configured anywhere. Set it only to lock the command to a known account.
OWNER_ID = os.environ.get("OWNER_ID", "").strip()

# "auto"/"web" uses Tavily or Exa when a key is set, and the curated profile
# checks when it is not. "http" forces the curated checks: a fast smoke test,
# or a network where the profile sites block the host.
SEARCH_MODE = os.environ.get("SEARCH_MODE", "auto").strip().lower()

# Fast web-search backends. Both return real pages with extracted text and a
# grounded answer in about a second, which is the whole point of "web" mode.
# Tavily is preferred when both keys are present.
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "").strip()
EXA_API_KEY = os.environ.get("EXA_API_KEY", "").strip()
WEB_SEARCH_TIMEOUT = 12
WEB_SEARCH_OVERALL_TIMEOUT = 30
WEB_MAX_RESULTS = 8
WEB_HIGHLIGHT_CHARS = 600
# The company's own homepage is fetched directly once its domain is known:
# that page is where the address, phone lines and social links actually live.
# Per-request cap and a hard cap on the whole company-site fetch, so a slow
# host costs a couple of seconds at worst rather than stalling the trace.
WEB_SITE_TIMEOUT = 3
WEB_SITE_TOTAL = 4
WEB_SITE_CHARS = 3200
# A web page must resemble the subject's name before any other signal counts.
# Without this floor, a tutorial that merely quotes the debtor's phone number
# outranks their real profile.
WEB_MIN_NAME_SIMILARITY = 0.55

# Curated fallback checks (searcher.py), used only when no search key is set.
PROFILE_TIMEOUT = 10
PROFILE_CONCURRENCY = 4

# Scoring / reporting thresholds.
MIN_SCORE_TO_REPORT = 40

# Name variation caps.
MAX_NAME_VARIATIONS = 6
MAX_SURNAME_VARIATIONS = 6
MAX_USERNAMES = 25
FUZZY_THRESHOLD = 0.80

# Politeness delay between batches of username lookups.
BATCH_SLEEP_SECONDS = 1.2
