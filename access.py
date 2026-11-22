"""Search allowance per account, and the owner of the hidden /broadcast.

Every account gets ``FREE_SEARCHES`` dossiers (5 by default). After that it
needs SkipTrace Premium: ``PLAN_PRICE_NGN`` (₦200) for ``PLAN_DAYS`` (30) days,
which carries ``PLAN_SEARCHES`` (20) dossiers. No payment provider is connected
yet, so the paywall only prints those numbers.

The only integration point for a payment provider is :func:`grant_plan` (or
:func:`grant_searches` for plain credits): call it once a payment is confirmed.
Nothing in the search pipeline changes. A plan is a 30-day pass, not an
auto-renewing subscription, until billing exists.
"""

from __future__ import annotations

from datetime import datetime

import config
import db

FREE_SEARCHES = config.FREE_SEARCHES
PLAN_PRICE_NGN = config.PLAN_PRICE_NGN
PLAN_SEARCHES = config.PLAN_SEARCHES
PLAN_DAYS = config.PLAN_DAYS

_STAMP = "%Y-%m-%d %H:%M:%S"


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
    """Free searches left, plan allowance left and the period behind them."""
    # A lapsed plan is cleared here, so last month's leftovers can never be
    # spent in the next period.
    db.expire_plan(telegram_id)
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


def grant_plan(telegram_id, days=None) -> None:
    """Start or renew Premium after a confirmed payment.

    A renewal resets the allowance to the full plan, so the new period starts
    clean instead of stacking last month's leftovers.
    """
    db.grant_searches(
        telegram_id,
        PLAN_SEARCHES,
        plan="premium",
        days=days or PLAN_DAYS,
        reset=True,
    )


def grant_searches(telegram_id, count: int) -> None:
    """Add plain search credits with no period (manual goodwill top-up)."""
    db.grant_searches(telegram_id, count)


def _renewal(expires) -> str:
    try:
        return datetime.strptime(str(expires), _STAMP).strftime("%d %b")
    except (TypeError, ValueError):
        return ""


def receipt(telegram_id) -> str:
    """One short line after a dossier, so the collector sees what is left."""
    if is_owner(telegram_id):
        return ""
    account = status(telegram_id)
    if account["paid_left"]:
        if account["plan"] == "premium":
            renews = _renewal(account.get("plan_expires_at"))
            tail = f" · renews {renews}" if renews else ""
            return (
                f"⭐ Premium · {account['paid_left']} of {PLAN_SEARCHES} "
                f"searches left{tail}."
            )
        label = "paid search" if account["paid_left"] == 1 else "paid searches"
        return f"💳 {account['paid_left']} {label} left."
    if account["free_left"]:
        return f"🎟 {account['free_left']} of {FREE_SEARCHES} free searches left."
    return (
        f"🎟 Free searches used up. Premium: ₦{PLAN_PRICE_NGN:,} for "
        f"{PLAN_DAYS} days."
    )


def paywall(telegram_id) -> str:
    """What a user sees once the free searches are gone."""
    account = status(telegram_id)
    if account["paid_left"]:
        # Should not happen, but never show a paywall to a paid account.
        return receipt(telegram_id)
    return (
        "🎟 <b>Your free searches are used up</b>\n\n"
        f"You have used all {FREE_SEARCHES} free searches.\n\n"
        "⭐ <b>SkipTrace Premium</b>\n"
        f"<b>₦{PLAN_PRICE_NGN:,} for {PLAN_DAYS} days</b> — up to "
        f"{PLAN_SEARCHES} dossiers.\n\n"
        "Payments are not switched on in this build yet. Ask the operator to "
        "activate your plan, then send /trace again."
    )


def welcome_line() -> str:
    """The allowance, phrased for /start."""
    return (
        f"Each account gets {FREE_SEARCHES} free searches. After that, Premium "
        f"is ₦{PLAN_PRICE_NGN:,} for {PLAN_DAYS} days "
        f"(up to {PLAN_SEARCHES} searches)."
    )
