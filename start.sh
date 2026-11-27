#!/usr/bin/env bash
# Startup for a panel host (Wispbyte and any Pterodactyl-style egg).
#
# Installs the pinned dependencies into the interpreter the panel provides and
# then runs the single long-polling worker in the foreground, so the panel keeps
# the process alive and shows its console. Unbuffered output matters: without
# -u the console stays empty for minutes.
#
# The install is stamped with a hash of requirements.txt. A restart on an
# unchanged requirements file therefore skips pip entirely: it starts in
# seconds, and a deploy cannot fail because the package index was slow or
# unreachable. Change requirements.txt and the install runs again, as it must.
set -euo pipefail

cd "$(dirname "$0")"

STAMP=".deps-stamp"
WANT="$(python3 -c 'import hashlib; print(hashlib.sha256(open("requirements.txt", "rb").read()).hexdigest())')"

if [ -f "$STAMP" ] && [ "$(cat "$STAMP")" = "$WANT" ]; then
    echo "Dependencies unchanged since the last install; skipping pip."
else
    echo "Installing dependencies…"
    if python3 -m pip install --no-cache-dir --disable-pip-version-check -r requirements.txt \
        || pip install --no-cache-dir --disable-pip-version-check -r requirements.txt; then
        printf '%s\n' "$WANT" > "$STAMP"
    else
        echo "ERROR: dependency install failed; see the lines above." >&2
        exit 1
    fi
fi

exec python3 -u bot.py
