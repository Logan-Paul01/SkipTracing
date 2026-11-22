"""SQLite audit log for searches, reported matches and search allowances."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager

import config


@contextmanager
def _connect():
    """Open a connection, commit on success, roll back on failure."""
    connection = sqlite3.connect(config.DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db() -> None:
    with _connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS searches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER,
                telegram_username TEXT,
                full_name TEXT,
                phone TEXT,
                gender TEXT,
                company TEXT,
                variations_count INTEGER,
                matches_count INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                search_id INTEGER,
                site TEXT,
                url TEXT,
                display_name TEXT,
                score INTEGER,
                reasons TEXT,
                FOREIGN KEY (search_id) REFERENCES searches (id)
            )
            """
        )
        # One row per account that has ever messaged the bot. The plan columns
        # are the seam for a subscription later; today only the two counters
        # are read by access.py.
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                telegram_username TEXT,
                first_seen TEXT DEFAULT CURRENT_TIMESTAMP,
                last_seen TEXT DEFAULT CURRENT_TIMESTAMP,
                free_searches_used INTEGER NOT NULL DEFAULT 0,
                paid_searches_left INTEGER NOT NULL DEFAULT 0,
                plan TEXT NOT NULL DEFAULT 'free',
                plan_expires_at TEXT
            )
            """
        )
        # Small key/value store: the claimed owner of /broadcast lives here.
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        # Accounts were only recorded once the allowance existed, so anyone who
        # ran a search before that is backfilled here: /broadcast must reach
        # early users too. INSERT OR IGNORE keeps the counters of live accounts.
        connection.execute(
            """
            INSERT OR IGNORE INTO users (telegram_id, telegram_username)
            SELECT telegram_id, MAX(telegram_username) FROM searches
            WHERE telegram_id IS NOT NULL
            GROUP BY telegram_id
            """
        )


def log_search(
    telegram_id,
    telegram_username,
    full_name,
    phone,
    gender,
    company,
    variations_count,
    matches_count,
) -> int:
    with _connect() as connection:
        cursor = connection.execute(
            """
            INSERT INTO searches (
                telegram_id, telegram_username, full_name, phone, gender,
                company, variations_count, matches_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                telegram_id,
                telegram_username,
                full_name,
                phone,
                gender,
                company,
                variations_count,
                matches_count,
            ),
        )
        return int(cursor.lastrowid)


def log_matches(search_id: int, matches) -> None:
    rows = [
        (
            search_id,
            match.get("site"),
            match.get("url"),
            match.get("display_name") or match.get("username"),
            int(match.get("score") or 0),
            " · ".join(match.get("reasons") or []),
        )
        for match in (matches or [])
    ]
    if not rows:
        return

    with _connect() as connection:
        connection.executemany(
            """
            INSERT INTO matches (search_id, site, url, display_name, score, reasons)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            rows,
        )


USER_DEFAULTS = {
    "free_searches_used": 0,
    "paid_searches_left": 0,
    "plan": "free",
    "plan_expires_at": None,
}


def touch_user(telegram_id, telegram_username="") -> None:
    """Create the account row on first contact and keep the username fresh."""
    if not telegram_id:
        return
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO users (telegram_id, telegram_username)
            VALUES (?, ?)
            ON CONFLICT (telegram_id) DO UPDATE SET
                telegram_username = COALESCE(
                    NULLIF(excluded.telegram_username, ''),
                    users.telegram_username
                ),
                last_seen = CURRENT_TIMESTAMP
            """,
            (int(telegram_id), (telegram_username or "").lstrip("@")),
        )


def get_user(telegram_id) -> dict:
    """The account row, or harmless defaults for someone not seen yet."""
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (int(telegram_id),)
        ).fetchone()
    if row is None:
        return {"telegram_id": int(telegram_id), "telegram_username": "", **USER_DEFAULTS}
    return dict(row)


def charge_search(telegram_id) -> None:
    """Spend one paid credit, else one free search.

    Paid credits go first so a topped-up account is never charged twice for
    the same dossier.
    """
    with _connect() as connection:
        cursor = connection.execute(
            """
            UPDATE users SET paid_searches_left = paid_searches_left - 1
            WHERE telegram_id = ? AND paid_searches_left > 0
            """,
            (int(telegram_id),),
        )
        if cursor.rowcount:
            return
        connection.execute(
            "UPDATE users SET free_searches_used = free_searches_used + 1 "
            "WHERE telegram_id = ?",
            (int(telegram_id),),
        )


def grant_searches(telegram_id, count: int, plan=None, days=None) -> None:
    """Add paid credits to an account.

    This is the only function a future payment integration has to call: it
    already records the plan name and expiry, so nothing in the search
    pipeline changes when billing goes live.
    """
    count = max(0, int(count or 0))
    touch_user(telegram_id)
    with _connect() as connection:
        connection.execute(
            "UPDATE users SET paid_searches_left = paid_searches_left + ? "
            "WHERE telegram_id = ?",
            (count, int(telegram_id)),
        )
        if plan:
            connection.execute(
                """
                UPDATE users SET plan = ?, plan_expires_at =
                    CASE WHEN ? IS NULL THEN NULL
                         ELSE datetime('now', ?) END
                WHERE telegram_id = ?
                """,
                (str(plan), days, f"+{int(days)} days" if days else None, int(telegram_id)),
            )


def all_users() -> list[dict]:
    """Every account that has ever used the bot, oldest first."""
    with _connect() as connection:
        rows = connection.execute(
            "SELECT telegram_id, telegram_username, first_seen, last_seen, "
            "free_searches_used, paid_searches_left FROM users "
            "ORDER BY first_seen ASC"
        ).fetchall()
    return [dict(row) for row in rows]


def meta_get(key: str, default: str = "") -> str:
    with _connect() as connection:
        row = connection.execute(
            "SELECT value FROM meta WHERE key = ?", (str(key),)
        ).fetchone()
    return str(row["value"]) if row is not None and row["value"] is not None else default


def meta_set(key: str, value) -> None:
    with _connect() as connection:
        connection.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (str(key), str(value)),
        )
