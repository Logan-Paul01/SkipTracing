"""Runtime configuration for SkipTrace V1.

Every secret comes from the environment. Nothing here is committed with a
value, and the module never writes configuration to disk.
"""

import os

# Required. bot.py exits with a clear error when this is missing. The
# Skiptracingbotkey fallback matches the secret name configured in this
# project, so the documented start command works without renaming it.
BOT_TOKEN = (
    os.environ.get("BOT_TOKEN", "").strip()
    or os.environ.get("Skiptracingbotkey", "").strip()
)

# SQLite audit log location.
DB_PATH = os.environ.get("DB_PATH", "skiptrace.db")

# "auto"/"web" uses Tavily or Exa when a key is set, Maigret otherwise.
# "maigret" forces Maigret, "http" forces the curated checks (fast smoke test
# or a network where Maigret's sites block the host).
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

# Maigret search behaviour.
MAIGRET_TIMEOUT = 12
MAIGRET_MAX_CONNECTIONS = 4
MAIGRET_TAGS = ["social", "dating", "forum", "misc"]

# Scoring / reporting thresholds.
MIN_SCORE_TO_REPORT = 40

# Name variation caps.
MAX_NAME_VARIATIONS = 6
MAX_SURNAME_VARIATIONS = 6
MAX_USERNAMES = 25
FUZZY_THRESHOLD = 0.80

# Politeness delay between batches of username lookups.
BATCH_SLEEP_SECONDS = 1.2
