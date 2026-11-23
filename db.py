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


def _columns(connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _ensure_column(connection, table: str, column: str, ddl: str) -> None:
    """Add a column to a database file created by an older build.

    ``init_db`` runs on every start against whatever file the host already
    has, so a new counter has to be added in place rather than requiring a
    manual migration step on the server.
    """
    if column not in _columns(connection, table):
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


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
        # Paid-plan demand list. Accounts land here when they hit the paywall
        # or send /premium; ``claimed_paid_at`` records that the collector says
        # the transfer is done, so the operator can confirm and activate.
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS waitlist (
                telegram_id INTEGER PRIMARY KEY,
                telegram_username TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                claimed_paid_at TEXT
            )
            """
        )
        _ensure_column(connection, "users", "purchases", "purchases INTEGER NOT NULL DEFAULT 0")
        _ensure_column(connection, "waitlist", "claimed_paid_at", "claimed_paid_at TEXT")
        _ensure_column(connection, "waitlist", "sender_name", "sender_name TEXT")
        _ensure_column(connection, "waitlist", "resolved_at", "resolved_at TEXT")
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


def grant_searches(
    telegram_id, count: int, plan=None, days=None, reset: bool = False
) -> None:
    """Add paid credits to an account, or start a fresh plan period.

    ``reset`` replaces the allowance instead of adding to it, which is what a
    plan renewal wants. This is the only function a future payment integration
    has to call: it already records the plan name and expiry, so nothing in the
    search pipeline changes when billing goes live.
    """
    count = max(0, int(count or 0))
    touch_user(telegram_id)
    with _connect() as connection:
        if reset:
            connection.execute(
                "UPDATE users SET paid_searches_left = ? WHERE telegram_id = ?",
                (count, int(telegram_id)),
            )
        else:
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
                         ELSE datetime('now', ?) END,
                    purchases = purchases + 1
                WHERE telegram_id = ?
                """,
                (str(plan), days, f"+{int(days)} days" if days else None, int(telegram_id)),
            )


def expire_plan(telegram_id) -> None:
    """Drop an allowance whose paid period has ended.

    Runs before the quota is read, so a lapsed plan never spends the previous
    period's leftovers. The timestamp is SQLite's UTC ``YYYY-MM-DD HH:MM:SS``,
    which sorts correctly as text.
    """
    with _connect() as connection:
        connection.execute(
            """
            UPDATE users
            SET paid_searches_left = 0, plan = 'free', plan_expires_at = NULL
            WHERE telegram_id = ? AND plan != 'free'
              AND plan_expires_at IS NOT NULL
              AND plan_expires_at <= datetime('now')
            """,
            (int(telegram_id),),
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


def join_waitlist(telegram_id, telegram_username="") -> bool:
    """Put an account on the paid-plan demand list.

    Called when the paywall is shown and by /premium, so the operator can see
    who actually reached the point of being willing to pay. Returns True only
    the first time an account joins.
    """
    if not telegram_id:
        return False
    touch_user(telegram_id, telegram_username)
    with _connect() as connection:
        existing = connection.execute(
            "SELECT 1 FROM waitlist WHERE telegram_id = ?", (int(telegram_id),)
        ).fetchone()
        connection.execute(
            """
            INSERT INTO waitlist (telegram_id, telegram_username)
            VALUES (?, ?)
            ON CONFLICT (telegram_id) DO UPDATE SET
                telegram_username = COALESCE(
                    NULLIF(excluded.telegram_username, ''),
                    waitlist.telegram_username
                )
            """,
            (int(telegram_id), (telegram_username or "").lstrip("@")),
        )
    return existing is None


def mark_payment_claim(telegram_id, sender_name="") -> bool:
    """Record a payment claim with the name on the transfer.

    Payment is manual, so the claim only reaches the operator to be matched
    against the bank statement; the plan itself is switched on by
    ``access.grant_plan`` once the transfer is confirmed. A repeat claim
    replaces the sender name and reopens the claim for checking. Returns True
    only on the first claim.
    """
    if not telegram_id:
        return False
    touch_user(telegram_id)
    sender = " ".join(str(sender_name or "").split())[:80]
    with _connect() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO waitlist (telegram_id) VALUES (?)",
            (int(telegram_id),),
        )
        row = connection.execute(
            "SELECT claimed_paid_at FROM waitlist WHERE telegram_id = ?",
            (int(telegram_id),),
        ).fetchone()
        first = row is None or row["claimed_paid_at"] is None
        connection.execute(
            """
            UPDATE waitlist SET
                claimed_paid_at = COALESCE(claimed_paid_at, CURRENT_TIMESTAMP),
                sender_name = COALESCE(NULLIF(?, ''), sender_name),
                resolved_at = NULL
            WHERE telegram_id = ?
            """,
            (sender, int(telegram_id)),
        )
    return first


def waitlist_row(telegram_id) -> dict:
    """The waitlist/claim row for one account, or an empty dict."""
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM waitlist WHERE telegram_id = ?", (int(telegram_id),)
        ).fetchone()
    return dict(row) if row is not None else {}


def pending_claims() -> list[dict]:
    """Payment claims the operator has not handled yet, oldest first."""
    with _connect() as connection:
        rows = connection.execute(
            "SELECT telegram_id, telegram_username, sender_name, claimed_paid_at "
            "FROM waitlist WHERE claimed_paid_at IS NOT NULL "
            "AND resolved_at IS NULL ORDER BY claimed_paid_at ASC"
        ).fetchall()
    return [dict(row) for row in rows]


def resolve_claim(telegram_id) -> None:
    """Mark a claim handled, so it leaves the operator's pending list."""
    with _connect() as connection:
        connection.execute(
            "UPDATE waitlist SET resolved_at = CURRENT_TIMESTAMP "
            "WHERE telegram_id = ?",
            (int(telegram_id),),
        )


def stats() -> dict:
    """Counts behind the launch-demand decision.

    ``payers`` is lifetime: a lapsed plan clears the allowance but not the
    ``purchases`` counter, so conversion still counts everyone who ever paid.
    """
    with _connect() as connection:
        def count(sql: str) -> int:
            return int(connection.execute(sql).fetchone()[0] or 0)

        return {
            "users": count("SELECT COUNT(*) FROM users"),
            "collectors": count(
                "SELECT COUNT(DISTINCT telegram_id) FROM searches "
                "WHERE telegram_id IS NOT NULL"
            ),
            "dossiers": count("SELECT COUNT(*) FROM searches"),
            "waitlist": count("SELECT COUNT(*) FROM waitlist"),
            "claims": count(
                "SELECT COUNT(*) FROM waitlist WHERE claimed_paid_at IS NOT NULL"
            ),
            "pending": count(
                "SELECT COUNT(*) FROM waitlist WHERE claimed_paid_at IS NOT NULL "
                "AND resolved_at IS NULL"
            ),
            "payers": count("SELECT COUNT(*) FROM users WHERE purchases > 0"),
            "active_paid": count(
                "SELECT COUNT(*) FROM users WHERE paid_searches_left > 0"
            ),
        }


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
