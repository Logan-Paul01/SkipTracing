"""Search allowance per account, the paid-plan waitlist and /broadcast owner.

Every account gets ``FREE_SEARCHES`` dossiers (10 by default). After that it
needs SkipTrace Premium: ``PLAN_PRICE_NGN`` (₦200) for ``PLAN_DAYS`` (30) days,
which carries ``PLAN_SEARCHES`` (20) dossiers.

No payment provider is connected. Payment is a manual transfer to the account
in ``PAYMENT_*``, so the offer is a waitlist: the collector joins it, sends
/paid once the transfer is done, and the operator confirms the transfer and
calls :func:`grant_plan`. Nothing in the search pipeline changes.
"""

from __future__ import annotations

import html
from datetime import datetime

import config
import db

FREE_SEARCHES = config.FREE_SEARCHES
PLAN_PRICE_NGN = config.PLAN_PRICE_NGN
PLAN_SEARCHES = config.PLAN_SEARCHES
PLAN_DAYS = config.PLAN_DAYS
PAYMENT_BANK = config.PAYMENT_BANK
PAYMENT_ACCOUNT_NAME = config.PAYMENT_ACCOUNT_NAME
PAYMENT_ACCOUNT_NUMBER = config.PAYMENT_ACCOUNT_NUMBER

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


def join_waitlist(telegram_id, username="") -> bool:
    """Put an account on the paid-plan demand list."""
    return db.join_waitlist(telegram_id, username)


def claim_payment(telegram_id, sender_name="") -> bool:
    """The collector says the transfer is done; True when it is the first time."""
    return db.mark_payment_claim(telegram_id, sender_name)


def payment_block() -> str:
    """Where to send the ₦200, from configuration only.

    The values are never written down in code or committed, so the operator can
    change banks without a release. With no account configured the offer stays
    honest instead of printing a blank account number.
    """
    lines: list[str] = []
    if PAYMENT_ACCOUNT_NUMBER:
        lines.append("Pay to:")
        if PAYMENT_BANK:
            lines.append(f"Bank: {html.escape(PAYMENT_BANK)}")
        if PAYMENT_ACCOUNT_NAME:
            lines.append(f"Account name: {html.escape(PAYMENT_ACCOUNT_NAME)}")
        lines.append(f"Account number: <b>{html.escape(PAYMENT_ACCOUNT_NUMBER)}</b>")
    else:
        lines.append(
            "Payment details are being set up — the operator will send you "
            "the account to use."
        )
    return "\n".join(lines) + "\n\n"


def offer() -> str:
    """The Premium offer and how to pay, shown by /premium at any time."""
    return (
        "⭐ <b>SkipTrace Premium</b>\n"
        f"<b>₦{PLAN_PRICE_NGN:,} for {PLAN_DAYS} days</b> — up to "
        f"{PLAN_SEARCHES} dossiers.\n\n"
        f"{payment_block()}"
        "After paying, send /paid with the <b>sender name</b> on the transfer. "
        "The operator checks it and switches the plan on."
    )


def stats_text() -> str:
    """Launch-demand counters for the operator.

    The decision rule is stated here too, so the number that matters (paid
    accounts as a share of all accounts) is never read out of context.
    """
    data = db.stats()
    users = data["users"]
    payers = data["payers"]
    rate = (payers / users * 100) if users else 0.0
    return "\n".join(
        (
            "📊 <b>SkipTrace usage</b>",
            f"Accounts: <b>{users}</b>",
            f"Collectors who ran a trace: {data['collectors']}",
            f"Dossiers delivered: {data['dossiers']}",
            f"On the waitlist: {data['waitlist']}",
            f"Said they paid: {data['claims']} ({data['pending']} waiting)",
            f"Paid accounts (ever): <b>{payers}</b>",
            f"Paid accounts with an allowance now: {data['active_paid']}",
            f"Conversion: <b>{rate:.1f}%</b> of accounts (go/no-go: 30%)",
        )
    )


def _renewal(expires) -> str:
    try:
        return datetime.strptime(str(expires), _STAMP).strftime("%d %b")
    except (TypeError, ValueError):
        return ""


def _period_end(expires) -> str:
    try:
        return datetime.strptime(str(expires), _STAMP).strftime("%d %b %Y")
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
        f"{PLAN_DAYS} days — send /premium to see how to pay."
    )


def plan_text(telegram_id) -> str:
    """The collector's own usage readout, for /plan.

    Free and Premium counters are reported separately because they are spent
    separately: a plan's searches go first, then any free ones left.
    """
    if is_owner(telegram_id):
        return (
            "👑 <b>Operator account</b>\n"
            "Searches are not counted on this account."
        )

    account = status(telegram_id)
    free_left = int(account["free_left"])
    paid_left = int(account["paid_left"])

    if account["plan"] == "premium" or paid_left:
        lines = [
            "⭐ <b>Your plan: SkipTrace Premium</b>",
            f"<b>{paid_left} of {PLAN_SEARCHES}</b> plan searches left",
        ]
        ends = _period_end(account.get("plan_expires_at"))
        if ends:
            lines.append(f"Period ends: <b>{ends}</b>")
        if free_left:
            lines.append(
                f"Free searches still available: <b>{free_left} of "
                f"{FREE_SEARCHES}</b>"
            )
        lines.append("")
        if paid_left:
            lines.append("Send /trace to run a dossier.")
        else:
            lines.append(
                "Plan searches are used up. Renew with /premium — "
                f"₦{PLAN_PRICE_NGN:,} for {PLAN_DAYS} days."
            )
        return "\n".join(lines)

    used = int(account["free_used"])
    lines = [
        "🎟 <b>Your plan: Free</b>",
        f"<b>{free_left} of {FREE_SEARCHES}</b> free searches left "
        f"({used} used)",
        "",
    ]
    if free_left:
        lines.append(
            f"After that: Premium — ₦{PLAN_PRICE_NGN:,} for {PLAN_DAYS} days, "
            f"up to {PLAN_SEARCHES} dossiers."
        )
        lines.append("Send /trace to run a dossier.")
    else:
        lines.append(
            f"Free searches are used up. Premium is ₦{PLAN_PRICE_NGN:,} for "
            f"{PLAN_DAYS} days — send /premium to see how to pay."
        )
    return "\n".join(lines)


def pending_claims_text(limit: int = 10) -> str:
    """The claims still waiting for a decision, for /stats.

    A payment card can be lost, scrolled past or sent before the operator
    claimed ownership, so the list has to be readable without the card.
    """
    claims = db.pending_claims()
    if not claims:
        return ""

    lines = ["", "<b>Waiting for your decision</b>"]
    for claim in claims[:limit]:
        who = (
            f"@{claim['telegram_username']}"
            if claim.get("telegram_username")
            else "no username"
        )
        sender = claim.get("sender_name") or "screenshot"
        lines.append(
            f"• <code>{int(claim['telegram_id'])}</code> · "
            f"{html.escape(who)} · sender: <b>{html.escape(sender)}</b>"
        )
    extra = len(claims) - limit
    if extra > 0:
        lines.append(f"…and {extra} more")
    lines.append(
        "Activate one: python3 -c \"import db, access; db.init_db(); "
        "access.grant_plan(&lt;telegram_id&gt;)\""
    )
    return "\n".join(lines)


def paywall(telegram_id) -> str:
    """What a user sees once the free searches are gone."""
    account = status(telegram_id)
    if account["paid_left"]:
        # Should not happen, but never show a paywall to a paid account.
        return receipt(telegram_id)
    return (
        "🎟 <b>Your free searches are used up</b>\n\n"
        f"You have used all {FREE_SEARCHES} free searches.\n\n" + offer()
    )


def welcome_line() -> str:
    """The allowance, phrased for /start."""
    return (
        f"Each account gets {FREE_SEARCHES} free searches. After that, Premium "
        f"is ₦{PLAN_PRICE_NGN:,} for {PLAN_DAYS} days "
        f"(up to {PLAN_SEARCHES} searches)."
    )
