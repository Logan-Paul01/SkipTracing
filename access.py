"""Search allowance per account, and the owner of the hidden /broadcast.

Every account gets ``FREE_SEARCHES`` dossiers (5 by default). After that the
bot replies with the price and stops, because no payment provider is connected
yet. When one is added the only integration point is
:func:`access.grant_searches`: call it once a payment is confirmed and the next
search is charged against that credit. Nothing in the search pipeline changes.

The ``plan`` / ``plan_expires_at`` columns on the account row exist so a
subscription can be attached later; today the quota only reads
``free_searches_used`` and ``paid_searches_left``.
"""

from __future__ import annotations

import config
import db

FREE_SEARCHES = config.FREE_SEARCHES
PRICE_NGN = config.PRICE_PER_SEARCH_NGN


def owner_id() -> int:
    """The account allowed to use /broadcast.

    ``OWNER_ID`` wins when it is configured. Otherwise the first account to
    send /broadcast claims ownership and it is stored in the database, so the
    owner's Telegram ID never has to be written down anywhere.
    """
    configured = str(config.OWNER_ID or "").strip().lstrip("@")
    if configured.isdigit():
        return int(configured)
    stored = db.meta_get("owner_id").strip()
    return int(stored) if stored.isdigit() else 0


def is_owner(telegram_id) -> bool:
    return bool(telegram_id) and owner_id() == int(telegram_id)


def claim_owner(telegram_id) -> bool:
    """Make this account the owner when nobody owns the command yet."""
    current = owner_id()
    if current:
        return current == int(telegram_id)
    db.meta_set("owner_id", int(telegram_id))
    return True


def status(telegram_id) -> dict:
    """Free searches left, paid credits left and the counters behind them."""
    user = db.get_user(telegram_id)
    used = int(user.get("free_searches_used") or 0)
    paid = int(user.get("paid_searches_left") or 0)
    return {
        "free_used": used,
        "free_left": max(0, FREE_SEARCHES - used),
        "paid_left": max(0, paid),
        "plan": user.get("plan") or "free",
        "plan_expires_at": user.get("plan_expires_at"),
    }


def can_search(telegram_id) -> bool:
    """The owner tests the bot for free; everyone else needs an allowance."""
    if is_owner(telegram_id):
        return True
    account = status(telegram_id)
    return bool(account["free_left"] or account["paid_left"])


def charge(telegram_id) -> None:
    """Spend one search. Called only after a dossier was actually delivered."""
    if is_owner(telegram_id):
        return
    db.charge_search(telegram_id)


def grant_searches(telegram_id, count: int, plan=None, days=None) -> None:
    """Top up an account after a confirmed payment (manual for now)."""
    db.grant_searches(telegram_id, count, plan=plan, days=days)


def receipt(telegram_id) -> str:
    """One short line after a dossier, so the collector sees what is left."""
    if is_owner(telegram_id):
        return ""
    account = status(telegram_id)
    if account["paid_left"]:
        label = "paid search" if account["paid_left"] == 1 else "paid searches"
        return f"💳 {account['paid_left']} {label} left."
    if account["free_left"]:
        return (
            f"🎟 {account['free_left']} of {FREE_SEARCHES} free searches left."
        )
    return f"🎟 Free searches used up. Next search: ₦{PRICE_NGN:,}."


def paywall(telegram_id) -> str:
    """What a user sees once the free searches are gone."""
    account = status(telegram_id)
    if account["paid_left"]:
        # Should not happen, but never show a paywall to a paid account.
        return receipt(telegram_id)
    return (
        "🎟 <b>Your free searches are used up</b>\n\n"
        f"You have used all {FREE_SEARCHES} free searches. A dossier now costs "
        f"<b>₦{PRICE_NGN:,}</b> per search.\n\n"
        "Payments are not switched on in this build yet. Ask the operator to "
        "top up your account, then send /trace again."
    )


def welcome_line() -> str:
    """The allowance, phrased for /start."""
    return (
        f"Each account gets {FREE_SEARCHES} free searches, then ₦{PRICE_NGN:,} "
        "per dossier."
    )
