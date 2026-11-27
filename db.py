"""SQLite counters for searches, revenue and search allowances.

Nothing about the debtor is stored. A dossier row records that a trace ran,
which account ran it and how many profiles came back, so usage and revenue can
be counted without keeping a name, a phone number or a profile link.
"""

from __future__ import annotations

import csv
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


def _purge_debtor_data(connection) -> None:
    """Blank the debtor details an earlier build kept, exactly once.

    The bot used to store the traced name, phone, gender, company and a table of
    every profile link it reported. That is gone: the columns are emptied and
    the old ``matches`` table is dropped. ``meta`` records the purge so it never
    runs again and no new row is ever written with those fields.
    """
    done = connection.execute(
        "SELECT value FROM meta WHERE key = 'pii_purged'"
    ).fetchone()
    if done is not None:
        return

    columns = _columns(connection, "searches")
    legacy = [name for name in ("full_name", "phone", "gender", "company")
              if name in columns]
    if legacy:
        connection.execute(
            "UPDATE searches SET "
            + ", ".join(f"{name} = NULL" for name in legacy)
        )
        # The data is gone either way; dropping the columns also takes them off
        # the file, so a backup carries no trace of the person. Older SQLite
        # builds do not support DROP COLUMN, and a refusal here is harmless —
        # the columns stay empty and are never written again.
        for name in legacy:
            try:
                connection.execute(f"ALTER TABLE searches DROP COLUMN {name}")
            except sqlite3.Error:
                continue
    connection.execute("DROP TABLE IF EXISTS matches")
    connection.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('pii_purged', '1')"
    )


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
        # A dossier row is a counter, not a record: who ran it, when, and how
        # many profiles came back. ``found`` is NULL on rows written before the
        # flag existed, so an old row is never counted as "nothing found".
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS searches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER,
                telegram_username TEXT,
                variations_count INTEGER,
                matches_count INTEGER,
                found INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        # Feature-interest votes: one row per account per feature, replaced if
        # the same account answers again.
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS feature_votes (
                telegram_id INTEGER,
                feature TEXT,
                vote TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (telegram_id, feature)
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
        _ensure_column(connection, "searches", "found", "found INTEGER")
        _purge_debtor_data(connection)
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
    variations_count,
    matches_count,
    found: bool = False,
) -> int:
    """Record that a trace ran, with no detail about the person traced."""
    with _connect() as connection:
        cursor = connection.execute(
            """
            INSERT INTO searches (
                telegram_id, telegram_username, variations_count, matches_count,
                found
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                telegram_id,
                telegram_username,
                variations_count,
                matches_count,
                1 if found else 0,
            ),
        )
        return int(cursor.lastrowid)


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

        votes = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                "SELECT vote, COUNT(*) FROM feature_votes WHERE feature = ? "
                "GROUP BY vote",
                (FEATURE_ASSOCIATED_BANK,),
            ).fetchall()
        }

        return {
            "users": count("SELECT COUNT(*) FROM users"),
            "collectors": count(
                "SELECT COUNT(DISTINCT telegram_id) FROM searches "
                "WHERE telegram_id IS NOT NULL"
            ),
            "dossiers": count("SELECT COUNT(*) FROM searches"),
            "with_record": count("SELECT COUNT(*) FROM searches WHERE found = 1"),
            "no_record": count("SELECT COUNT(*) FROM searches WHERE found = 0"),
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
            "bank_yes": votes.get("yes", 0),
            "bank_no": votes.get("no", 0),
        }


def record_vote(telegram_id, feature: str, vote: str) -> None:
    """Store (or replace) one account's answer to a feature question."""
    if not telegram_id:
        return
    touch_user(telegram_id)
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO feature_votes (telegram_id, feature, vote)
            VALUES (?, ?, ?)
            ON CONFLICT (telegram_id, feature) DO UPDATE SET
                vote = excluded.vote,
                created_at = CURRENT_TIMESTAMP
            """,
            (int(telegram_id), str(feature), str(vote)),
        )


def vote_totals(feature: str) -> dict:
    """How many accounts answered each way for one feature question."""
    with _connect() as connection:
        rows = connection.execute(
            "SELECT vote, COUNT(*) FROM feature_votes WHERE feature = ? "
            "GROUP BY vote",
            (str(feature),),
        ).fetchall()
    return {str(row[0]): int(row[1]) for row in rows}


# The one feature question asked in the dossier. Kept here so the poll, /stats
# and the export all read the same key.
FEATURE_ASSOCIATED_BANK = "associated_bank"

REVENUE_COLUMNS = (
    "telegram_id",
    "telegram_username",
    "first_seen",
    "last_seen",
    "plan",
    "plan_expires_at",
    "purchases",
    "paid_searches_left",
    "free_searches_used",
    "dossiers",
    "payment",
)


def revenue_rows() -> list[dict]:
    """One row per account: what it has paid, spent and still holds.

    This is the revenue side of the database only — money, plans and usage.
    Nothing about the people who were traced appears here.
    """
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT
                u.telegram_id,
                u.telegram_username,
                u.first_seen,
                u.last_seen,
                u.plan,
                u.plan_expires_at,
                u.purchases,
                u.paid_searches_left,
                u.free_searches_used,
                (SELECT COUNT(*) FROM searches s
                  WHERE s.telegram_id = u.telegram_id) AS dossiers,
                CASE
                    WHEN w.telegram_id IS NULL THEN ''
                    WHEN w.resolved_at IS NOT NULL THEN 'confirmed'
                    WHEN w.claimed_paid_at IS NOT NULL THEN 'claimed'
                    ELSE 'waitlist'
                END AS payment
            FROM users u
            LEFT JOIN waitlist w ON w.telegram_id = u.telegram_id
            ORDER BY u.first_seen ASC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def export_revenue_csv(path) -> int:
    """Write the revenue rows to ``path`` and return how many were written."""
    rows = revenue_rows()
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(REVENUE_COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in REVENUE_COLUMNS})
    return len(rows)


def backup_to(path) -> None:
    """Write a consistent copy of the live database while the bot keeps running."""
    source = sqlite3.connect(config.DB_PATH, timeout=10)
    try:
        destination = sqlite3.connect(path)
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()


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
