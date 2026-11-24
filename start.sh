#!/usr/bin/env bash
# Startup for a panel host (Wispbyte and any Pterodactyl-style egg).
#
# Installs the pinned dependencies into the interpreter the panel provides and
# then runs the single long-polling worker in the foreground, so the panel keeps
# the process alive and shows its console. Unbuffered output matters: without
# -u the console stays empty for minutes.
set -euo pipefail

cd "$(dirname "$0")"

python3 -m pip install --no-cache-dir --disable-pip-version-check -r requirements.txt \
    || pip install --no-cache-dir --disable-pip-version-check -r requirements.txt

exec python3 -u bot.py
