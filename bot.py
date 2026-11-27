"""SkipTrace V1 Telegram bot.

Headless, long-polling, synchronous telebot handlers around an async
search pipeline. No web server and no webhook. The web path asks Tavily or
Exa for a grounded, cited answer; the curated HTTP checks are only the
fallback for a host with no search key.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time

import telebot
from telebot.apihelper import ApiTelegramException
from telebot.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

import access
import config
import db
import extract
import formatter
import names
import normalizer
import scorer
import searcher
import websearch

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("skiptrace.bot")

# Placeholder keeps imports safe; main() refuses to poll without a real token.
bot = telebot.TeleBot(config.BOT_TOKEN or "0:" + "A" * 35, parse_mode="HTML")

STATE: dict[int, dict] = {}

GENDER_CHOICES = {
    "male": "Male",
    "female": "Female",
    "prefer not to say": "Prefer not to say",
}

WELCOME_TEXT = (
    "👋 <b>SkipTrace V1</b>\n"
    "Public-profile lookup for loan collectors.\n\n"
    "Send /trace to begin:\n"
    "1. Debtor's full name\n"
    "2. Phone number\n"
    "3. Gender\n"
    "4. Company (optional)\n"
    "5. City/state (optional)\n\n"
    f"{access.welcome_line()}\n"
    "/plan shows what is left, /premium how to pay.\n\n"
    "Only public profiles are searched. The number is used to match "
    "profiles, never as a search query, and the bot never messages or calls "
    "anyone.\n"
    "/cancel stops the current trace."
)

TEXT_LIMIT = 4000
UNKNOWN_COMMAND = "Unknown command. Send /trace to start a search or /cancel to stop."

# The guide is reached from /guide and from the button under a trace that came
# back empty. It teaches the form, not "try again": a second attempt on the
# same details is exactly what makes a thin result feel like a dead end.
GUIDE_TEXT = (
    "📖 <b>How to get the most out of SkipTrace</b>\n\n"
    "The bot reads public pages. The closer the details you send are to the "
    "way the debtor appears on those pages, the better the chance a real "
    "profile comes back.\n\n"
    "<b>Details that help</b>\n"
    "• The full name as he uses it — first name and surname, spelled the way "
    "he writes it.\n"
    "• The company or business name when you know it. A business page often "
    "carries a phone, address and email that a personal profile never shows.\n"
    "• The city and state where he lives or works. Many people share a name; a "
    "place is what separates them.\n"
    "• A married name, a surname-first version, or a common short form, if you "
    "know he uses one.\n\n"
    "<b>What the bot cannot do</b>\n"
    "• It cannot look a phone number up. The number is only used to confirm a "
    "profile that already shows it.\n"
    "• It cannot see private accounts, bank records, or anything behind a "
    "login.\n"
    "• A thin result usually means the person keeps little public presence — "
    "not that the details were wrong.\n\n"
    "<b>Reading a dossier</b>\n"
    "• Every line is a lead to check, not proof of identity.\n"
    "• Open the source link before you act on a phone, email or address.\n"
    "• A trace that finds nothing is never charged.\n\n"
    "/guide opens this any time."
)


def _guide_markup() -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton(
            "📖 How to get better results", callback_data="gd:open"
        )
    )
    return markup


def _new_state() -> dict:
    return {
        "step": "idle",
        "name": "",
        "phone": "",
        "gender": "",
        "company": "",
        "city": "",
        "username": "",
    }


def _reset(uid: int) -> None:
    STATE.pop(uid, None)


def _gender_keyboard() -> ReplyKeyboardMarkup:
    markup = ReplyKeyboardMarkup(
        row_width=3, one_time_keyboard=True, resize_keyboard=True
    )
    markup.add("Male", "Female", "Prefer not to say")
    return markup


def _truncate(text: str) -> str:
    if len(text) <= TEXT_LIMIT:
        return text
    cut = text[:TEXT_LIMIT]
    cut = cut.rsplit("\n", 1)[0]
    return cut + "\n…"


def _strip_html(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))


# A dossier is kept in memory under a short token so the buttons under it can
# swap the one message between its views. Nothing is re-sent and nothing extra
# is written to the database; a restart simply makes old buttons say so.
VIEW_TTL = 6 * 3600
VIEW_LIMIT = 300
VIEWS: dict[str, dict] = {}


def _remember_view(view: dict) -> str:
    """Store a rendered dossier and return the token its buttons carry."""
    now = time.time()
    for key in [
        key for key, item in VIEWS.items() if now - item.get("created", 0) > VIEW_TTL
    ]:
        VIEWS.pop(key, None)
    while len(VIEWS) >= VIEW_LIMIT:
        VIEWS.pop(next(iter(VIEWS)), None)

    token = secrets.token_urlsafe(6)
    view["created"] = now
    VIEWS[token] = view
    return token


def _view_markup(token: str) -> InlineKeyboardMarkup:
    """The two tabs under a dossier, one above the other."""
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton(
            "🏅 Top matching profiles", callback_data=f"pv:{token}:prof"
        )
    )
    markup.add(
        InlineKeyboardButton("🏦 Associated bank", callback_data=f"pv:{token}:bank")
    )
    return markup


def _back_markup(token: str) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("⬅️ Back to dossier", callback_data=f"pv:{token}:back")
    )
    return markup


def _profiles_limit(structured: dict) -> int:
    """Fewer profile cards when the dossier already carries contacts."""
    if structured["person"].get("phones") or structured["company"].get("website"):
        return formatter.MATCHES_WITH_STRUCTURE
    return formatter.MAX_MATCHES


# One question per feature, stored in ``feature_votes`` so the answers can be
# counted in /stats and the revenue export.
BANK_FEATURE = db.FEATURE_ASSOCIATED_BANK

# Deliberately neutral: it describes the feature and says why it is not built,
# without promising a recovery outcome or nudging either answer.
BANK_INTEREST_TEXT = (
    "🏦 <b>Associated bank</b>\n"
    "<i>A feature that has not been built yet.</i>\n\n"
    "<b>What it would do</b>\n"
    "After you send the debtor's phone number, the bot would show the bank "
    "the account behind that number is held with.\n\n"
    "<b>Why it is not available</b>\n"
    "A phone number on its own does not say which bank an account is with, so "
    "the bot would need a data source it does not have today.\n\n"
    "<b>Would you use it if it existed?</b>\n"
    "Either answer is useful — it only decides what gets built next."
)

BANK_VOTED_TEXT = {
    "yes": (
        "✅ <b>Recorded: you would use it.</b>\n\n"
        "Thanks — that goes into the count of how many collectors want an "
        "associated-bank lookup."
    ),
    "no": (
        "🙅 <b>Recorded: you would not use it.</b>\n\n"
        "Thanks — that is just as useful to know."
    ),
}


def _bank_interest_markup(token: str) -> InlineKeyboardMarkup:
    """Yes/no under the feature description, with a way back to the dossier."""
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("✅ Yes, I would use it", callback_data=f"fb:{token}:yes"),
        InlineKeyboardButton("🙅 No, I would not", callback_data=f"fb:{token}:no"),
    )
    markup.add(
        InlineKeyboardButton("⬅️ Back to dossier", callback_data=f"pv:{token}:back")
    )
    return markup


@bot.callback_query_handler(
    func=lambda call: (call.data or "").split(":", 2)[0] == "pv"
)
def handle_dossier_view(call):
    """Swap the dossier message between views instead of sending more messages."""
    parts = (call.data or "").split(":", 2)
    if len(parts) != 3:
        bot.answer_callback_query(call.id, "This dossier is too old to open.")
        return

    _prefix, token, action = parts
    view = VIEWS.get(token)
    if not view:
        bot.answer_callback_query(
            call.id, "This dossier has expired — send /trace for a new one."
        )
        return
    if int(call.from_user.id) != int(view.get("uid") or 0):
        bot.answer_callback_query(call.id, "This dossier belongs to another account.")
        return

    answered = False
    if action == "prof":
        text, markup = view["profiles"], _back_markup(token)
    elif action == "bank":
        # No lookup runs here: the feature this button stands for cannot be
        # built from a phone number, so the tab asks whether it is wanted.
        text, markup = BANK_INTEREST_TEXT, _bank_interest_markup(token)
    else:
        text, markup = view["main"], _view_markup(token)

    try:
        bot.edit_message_text(
            _truncate(text),
            call.message.chat.id,
            call.message.message_id,
            parse_mode="HTML",
            reply_markup=markup,
            disable_web_page_preview=True,
        )
    except ApiTelegramException as exc:
        if "not modified" not in str(exc).lower():
            logger.warning("Dossier view switch failed: %s", exc)
            bot.answer_callback_query(
                call.id, "Could not switch the view — send /trace again."
            )
            return
    if not answered:
        bot.answer_callback_query(call.id)


def _send_html(chat_id: int, text: str, reply_markup=None):
    """Send HTML, falling back to plain text if Telegram rejects the markup."""
    text = _truncate(text)
    try:
        return bot.send_message(
            chat_id, text, parse_mode="HTML", reply_markup=reply_markup
        )
    except ApiTelegramException:
        logger.warning("HTML send rejected; retrying as plain text")
        return bot.send_message(
            chat_id, _strip_html(text), reply_markup=reply_markup
        )


@bot.callback_query_handler(
    func=lambda call: (call.data or "").split(":", 1)[0] == "gd"
)
def handle_guide_button(call):
    """Open the guide from the button under an empty trace."""
    bot.answer_callback_query(call.id)
    try:
        bot.send_message(
            call.message.chat.id, GUIDE_TEXT, parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except ApiTelegramException:
        bot.send_message(call.message.chat.id, _strip_html(GUIDE_TEXT))


@bot.callback_query_handler(
    func=lambda call: (call.data or "").split(":", 1)[0] == "fb"
)
def handle_feature_vote(call):
    """Record a yes/no answer to a feature question.

    The vote belongs to the account that ran the trace, and answering again
    replaces the earlier answer, so a change of mind is not counted twice.
    """
    parts = (call.data or "").split(":", 2)
    if len(parts) != 3 or parts[2] not in BANK_VOTED_TEXT:
        bot.answer_callback_query(call.id)
        return

    _prefix, token, vote = parts
    view = VIEWS.get(token)
    if view and int(call.from_user.id) != int(view.get("uid") or 0):
        bot.answer_callback_query(call.id, "This dossier belongs to another account.")
        return

    uid = int(call.from_user.id)
    try:
        db.record_vote(uid, BANK_FEATURE, vote)
    except Exception:  # noqa: BLE001 - a poll must never break the chat
        logger.warning("Feature vote write failed for telegram_id=%s", uid, exc_info=True)

    if view:
        # The view stays valid, so the collector can still go back to it.
        try:
            bot.edit_message_text(
                _truncate(BANK_VOTED_TEXT[vote]),
                call.message.chat.id,
                call.message.message_id,
                parse_mode="HTML",
                reply_markup=_back_markup(token),
                disable_web_page_preview=True,
            )
        except ApiTelegramException as exc:
            if "not modified" not in str(exc).lower():
                logger.warning("Could not show the vote confirmation: %s", exc)
    bot.answer_callback_query(call.id, "Recorded")
    logger.info("Feature vote %s=%s by telegram_id=%s", BANK_FEATURE, vote, uid)


@bot.message_handler(commands=["guide"])
def cmd_guide(message):
    """The usage guide, available to anyone at any time."""
    _reset(message.from_user.id)
    bot.send_message(
        message.chat.id,
        GUIDE_TEXT,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


@bot.message_handler(commands=["start"])
def cmd_start(message):
    uid = message.from_user.id
    _reset(uid)
    db.touch_user(uid, getattr(message.from_user, "username", "") or "")
    bot.send_message(message.chat.id, WELCOME_TEXT, parse_mode="HTML")


@bot.message_handler(commands=["cancel"])
def cmd_cancel(message):
    _reset(message.from_user.id)
    bot.send_message(
        message.chat.id,
        "Trace cancelled. Send /trace to start again.",
        reply_markup=ReplyKeyboardRemove(),
    )


@bot.message_handler(commands=["trace"])
def cmd_trace(message):
    uid = message.from_user.id
    db.touch_user(uid, getattr(message.from_user, "username", "") or "")
    # Checked here, before five steps are collected, so a user never fills in a
    # whole form only to be told they cannot run it.
    if not access.can_search(uid):
        # Reaching the paywall is the demand signal, so it is recorded before
        # the text goes out.
        _join_waitlist(uid, message.from_user)
        bot.send_message(message.chat.id, access.paywall(uid), parse_mode="HTML")
        return
    STATE[uid] = _new_state()
    STATE[uid]["step"] = "name"
    bot.send_message(
        message.chat.id,
        "Step 1/5 — Send the debtor's <b>full name</b> (first and surname).",
        parse_mode="HTML",
    )


@bot.message_handler(commands=["skip"])
def cmd_skip(message):
    uid = message.from_user.id
    state = STATE.get(uid)
    if not state or state.get("step") not in ("company", "city"):
        bot.send_message(
            message.chat.id,
            "/skip is only valid at the company or city/state step. "
            "Send /trace to start a search.",
        )
        return
    state[state["step"]] = ""
    if state["step"] == "company":
        state["step"] = "city"
        bot.send_message(
            message.chat.id,
            "Step 5/5 — Send the debtor's <b>city and state</b> (optional) "
            "or /skip to continue.",
            parse_mode="HTML",
        )
        return
    state["step"] = "idle"
    _run_and_reply(message, state)


def _join_waitlist(uid: int, user) -> None:
    """Record paid-plan interest; a waitlist write must never break a reply."""
    try:
        access.join_waitlist(uid, getattr(user, "username", "") or "")
    except Exception:  # noqa: BLE001 - demand tracking is not critical path
        logger.warning("Waitlist write failed for telegram_id=%s", uid, exc_info=True)


@bot.message_handler(commands=["plan"])
def cmd_plan(message):
    """The collector's own plan and remaining searches."""
    uid = message.from_user.id
    db.touch_user(uid, getattr(message.from_user, "username", "") or "")
    _reset(uid)
    bot.send_message(message.chat.id, access.plan_text(uid), parse_mode="HTML")


@bot.message_handler(commands=["premium"])
def cmd_premium(message):
    """The paid plan and how to pay it, available at any time."""
    uid = message.from_user.id
    db.touch_user(uid, getattr(message.from_user, "username", "") or "")
    _reset(uid)
    _join_waitlist(uid, message.from_user)
    account = access.status(uid)
    if account["paid_left"]:
        text = access.receipt(uid)
    else:
        text = access.offer()
    bot.send_message(message.chat.id, text, parse_mode="HTML")


@bot.message_handler(commands=["paid"])
def cmd_paid(message):
    """The collector has transferred the plan price; collect the proof."""
    uid = message.from_user.id
    username = getattr(message.from_user, "username", "") or ""
    db.touch_user(uid, username)

    if access.status(uid)["paid_left"]:
        _reset(uid)
        bot.send_message(message.chat.id, access.receipt(uid), parse_mode="HTML")
        return

    parts = (message.text or "").split(maxsplit=1)
    proof = parts[1].strip() if len(parts) > 1 else ""
    if proof:
        _submit_payment_proof(message, proof)
        return

    STATE[uid] = _new_state()
    STATE[uid]["step"] = "proof"
    bot.send_message(
        message.chat.id,
        "🧾 <b>Confirming your payment</b>\n\n"
        "Send the <b>sender name</b> exactly as it appears on the transfer "
        "(for example: <i>ADEBAYO TUNDE</i>). A receipt screenshot works "
        "too.\n\n"
        "/cancel to stop.",
        parse_mode="HTML",
    )


def _submit_payment_proof(message, sender_name: str, photo_id: str = "") -> None:
    """Record the claim, thank the collector, hand it to the operator."""
    uid = message.from_user.id
    username = getattr(message.from_user, "username", "") or ""
    db.touch_user(uid, username)
    _reset(uid)

    try:
        first = access.claim_payment(uid, sender_name)
    except Exception:  # noqa: BLE001 - never lose the acknowledgement
        logger.warning("Payment claim write failed for telegram_id=%s", uid, exc_info=True)
        first = False

    bot.send_message(
        message.chat.id,
        "✅ <b>Received.</b>\n\nYour payment is with the operator for "
        "confirmation. Premium is switched on as soon as it clears — you will "
        "get a message here.",
        parse_mode="HTML",
    )
    _notify_owner_of_claim(uid, username, sender_name, first, photo_id)


def _claim_card(uid: int, username: str, sender_name: str, first: bool = True):
    """The one card the operator decides on: details plus two buttons."""
    label = f"@{username}" if username else "no username"
    text = (
        "💳 <b>Payment claim</b>\n"
        f"{html.escape(label)} · <code>{uid}</code>\n"
        f"Sender: <b>{html.escape(sender_name or 'not given')}</b>\n"
        f"Plan: ₦{access.PLAN_PRICE_NGN:,} · {access.PLAN_DAYS} days · "
        f"{access.PLAN_SEARCHES} searches"
    )
    if not first:
        text += "\n<i>Repeat claim</i>"

    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("✅ Activate", callback_data=f"act:{uid}"),
        InlineKeyboardButton("❌ Not found", callback_data=f"rej:{uid}"),
    )
    return text, markup


def _notify_owner_of_claim(
    uid: int, username: str, sender_name: str, first: bool, photo_id: str = ""
) -> None:
    """Send the claim card to the operator, if there is one yet."""
    owner = access.owner_id()
    if not owner or owner == uid:
        logger.warning(
            "Payment claim stored for telegram_id=%s but no operator is set to "
            "review it; it stays pending until ownership is claimed",
            uid,
        )
        return

    text, markup = _claim_card(uid, username, sender_name, first)
    try:
        if photo_id:
            # The receipt is forwarded by file id: nothing is downloaded or
            # stored, and the operator sees exactly what the buyer sent.
            bot.send_photo(
                owner,
                photo_id,
                caption=text,
                parse_mode="HTML",
                reply_markup=markup,
            )
        else:
            bot.send_message(owner, text, parse_mode="HTML", reply_markup=markup)
    except Exception:  # noqa: BLE001 - the owner may have blocked the bot
        logger.warning(
            "Could not notify the owner about a payment claim; it stays pending"
        )


def _resend_pending_claims(owner_id: int) -> None:
    """Deliver claims that arrived before anyone owned the command.

    A payment must never be lost because the operator had not set up
    /broadcast yet: the claim is already in the database, so claiming
    ownership is enough to see it.
    """
    try:
        claims = db.pending_claims()
    except Exception:  # noqa: BLE001 - review is best effort
        logger.warning("Could not read pending payment claims", exc_info=True)
        return

    delivered = 0
    for claim in claims:
        text, markup = _claim_card(
            int(claim["telegram_id"]),
            claim.get("telegram_username") or "",
            claim.get("sender_name") or "",
        )
        try:
            bot.send_message(
                owner_id, text, parse_mode="HTML", reply_markup=markup
            )
            delivered += 1
        except Exception:  # noqa: BLE001 - stop quietly, they stay pending
            logger.warning("Could not resend a pending payment claim")
            break
    if delivered:
        logger.info("Resent %s pending payment claim(s) to the operator", delivered)


@bot.callback_query_handler(
    func=lambda call: (call.data or "").split(":", 1)[0] in {"act", "rej"}
)
def handle_payment_decision(call):
    """Owner-only: switch Premium on, or close the claim as not found."""
    if not access.is_owner(call.from_user.id):
        bot.answer_callback_query(call.id, "This is the operator's decision.")
        return

    action, _, raw = (call.data or "").partition(":")
    try:
        uid = int(raw)
    except ValueError:
        bot.answer_callback_query(call.id, "Bad claim.")
        return

    row = db.waitlist_row(uid)
    if row.get("resolved_at"):
        bot.answer_callback_query(call.id, "Already handled.")
        return

    if action == "act":
        access.grant_plan(uid)
        db.resolve_claim(uid)
        try:
            bot.send_message(
                uid,
                "⭐ <b>SkipTrace Premium is active</b>\n\n"
                f"{access.PLAN_SEARCHES} dossiers over {access.PLAN_DAYS} days.\n"
                f"{access.receipt(uid)}\n\nSend /trace to run your next one.",
                parse_mode="HTML",
            )
        except Exception:  # noqa: BLE001 - the buyer may have blocked the bot
            logger.warning("Could not notify telegram_id=%s of activation", uid)
        bot.answer_callback_query(call.id, "Activated")
    else:
        db.resolve_claim(uid)
        try:
            bot.send_message(
                uid,
                "We could not find that transfer yet. If you have paid, send "
                "/paid again with the sender name on the receipt, or check the "
                "account details with /premium.",
            )
        except Exception:  # noqa: BLE001 - a dead chat must not break the run
            pass
        bot.answer_callback_query(call.id, "Marked as not found")

    try:
        bot.edit_message_reply_markup(
            call.message.chat.id, call.message.message_id, reply_markup=None
        )
    except Exception:  # noqa: BLE001 - an old card may be too old to edit
        pass
    logger.info(
        "Payment claim telegram_id=%s %s by owner %s",
        uid,
        "activated" if action == "act" else "rejected",
        call.from_user.id,
    )


@bot.message_handler(commands=["stats"])
def cmd_stats(message):
    """Hidden operator command: usage and paid-conversion counters."""
    if not access.is_owner(message.from_user.id):
        bot.send_message(message.chat.id, UNKNOWN_COMMAND)
        return
    bot.send_message(
        message.chat.id,
        access.stats_text() + access.pending_claims_text(),
        parse_mode="HTML",
    )


def _broadcast(chat_id: int, owner_id: int, body: str) -> None:
    """Send one message to every account that has ever used the bot."""
    recipients = db.all_users()
    if not recipients:
        bot.send_message(chat_id, "📣 No one has used the bot yet, so there is nobody to broadcast to.")
        return

    notice = bot.send_message(
        chat_id, f"📣 Broadcasting to {len(recipients)} users…"
    )
    # A fixed header keeps an operator message recognisable in a chat list.
    text = f"<b>📢 SkipTrace update</b>\n\n{body}"
    sent = 0
    failed = 0

    for index, account in enumerate(recipients, start=1):
        recipient = account.get("telegram_id")
        try:
            _send_html(int(recipient), text)
            sent += 1
        except ApiTelegramException as exc:
            failed += 1
            if getattr(exc, "error_code", None) == 429:
                # Telegram's flood limit: wait it out instead of dropping the
                # rest of the list.
                time.sleep(3)
        except Exception:  # noqa: BLE001 - one bad chat must not stop the run
            failed += 1
            logger.warning("Broadcast failed for telegram_id=%s", recipient)

        # ~20 messages a second, below Telegram's ~30/s ceiling.
        time.sleep(0.05)
        if index % 25 == 0:
            try:
                bot.edit_message_text(
                    f"📣 Broadcasting… {index}/{len(recipients)}",
                    chat_id,
                    notice.message_id,
                )
            except Exception:  # noqa: BLE001 - progress text is cosmetic
                pass

    summary = (
        f"✅ Broadcast finished.\n"
        f"Delivered: {sent}\n"
        f"Failed: {failed}"
    )
    try:
        bot.edit_message_text(summary, chat_id, notice.message_id)
    except Exception:  # noqa: BLE001 - fall back to a fresh message
        bot.send_message(chat_id, summary)
    logger.info("Broadcast by telegram_id=%s: %s sent, %s failed", owner_id, sent, failed)


@bot.message_handler(commands=["broadcast"])
def cmd_broadcast(message):
    """Hidden operator command: message every account.

    It is deliberately absent from /start and from the command menu. Only the
    owner gets a reply; anyone else sees the ordinary unknown-command text, so
    the command name reveals nothing to a user who stumbles on it.
    """
    uid = message.from_user.id
    if not access.is_owner(uid):
        if not access.claim_owner(uid):
            bot.send_message(message.chat.id, UNKNOWN_COMMAND)
            return
        logger.info("Broadcast ownership claimed by telegram_id=%s", uid)
        # Claims filed before this moment had nobody to go to; hand them over
        # now instead of leaving a paying collector waiting.
        _resend_pending_claims(uid)

    _reset(uid)
    db.touch_user(uid, getattr(message.from_user, "username", "") or "")

    parts = (message.text or "").split(maxsplit=1)
    body = parts[1].strip() if len(parts) > 1 else ""
    if body:
        _broadcast(message.chat.id, uid, body)
        return

    STATE[uid] = _new_state()
    STATE[uid]["step"] = "broadcast"
    bot.send_message(
        message.chat.id,
        "📣 <b>Broadcast</b>\nSend the message to deliver to every user "
        "(HTML is allowed), or /cancel to stop.",
        parse_mode="HTML",
    )


@bot.message_handler(content_types=["text"])
def handle_text(message):
    uid = message.from_user.id
    state = STATE.get(uid)
    text = (message.text or "").strip()
    db.touch_user(uid, getattr(message.from_user, "username", "") or "")

    if text.startswith("/"):
        bot.send_message(message.chat.id, UNKNOWN_COMMAND)
        return

    if state and state.get("step") == "broadcast" and access.is_owner(uid):
        _reset(uid)
        _broadcast(message.chat.id, uid, text)
        return

    if state and state.get("step") == "proof":
        if len(text) < 2:
            bot.send_message(
                message.chat.id,
                "Send the sender name on the transfer (at least 2 characters), "
                "or /cancel to stop.",
            )
            return
        _submit_payment_proof(message, text)
        return

    if not state or state.get("step") in (None, "idle"):
        bot.send_message(
            message.chat.id, "Send /trace to start a new search."
        )
        return

    step = state.get("step")

    if step == "name":
        if len(text) < 2:
            bot.send_message(
                message.chat.id,
                "The name must be at least 2 characters. Send the debtor's full name.",
                reply_markup=ReplyKeyboardRemove(),
            )
            return
        state["name"] = text
        state["step"] = "phone"
        bot.send_message(
            message.chat.id,
            "Step 2/5 — Send the debtor's <b>phone number</b> "
            "(e.g. 08031234567 or +2348031234567).",
            parse_mode="HTML",
        )
        return

    if step == "phone":
        try:
            e164, _variants, _meta = normalizer.normalize_phone(text)
        except ValueError as exc:
            bot.send_message(message.chat.id, f"⚠️ {html.escape(str(exc))}", parse_mode="HTML")
            return
        state["phone"] = e164
        state["step"] = "gender"
        bot.send_message(
            message.chat.id,
            "Step 3/5 — Select the debtor's <b>gender</b>.",
            parse_mode="HTML",
            reply_markup=_gender_keyboard(),
        )
        return

    if step == "gender":
        choice = GENDER_CHOICES.get(text.lower())
        if not choice:
            bot.send_message(
                message.chat.id,
                "Please choose one of the three options below.",
                reply_markup=_gender_keyboard(),
            )
            return
        state["gender"] = choice
        state["step"] = "company"
        bot.send_message(
            message.chat.id,
            "Step 4/5 — Send the <b>company</b> (optional) or /skip to continue.",
            parse_mode="HTML",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    if step == "company":
        state["company"] = text
        state["step"] = "city"
        bot.send_message(
            message.chat.id,
            "Step 5/5 — Send the debtor's <b>city and state</b> (optional) "
            "or /skip to continue.",
            parse_mode="HTML",
        )
        return

    if step == "city":
        state["city"] = text
        state["step"] = "idle"
        _run_and_reply(message, state)
        return

    _reset(uid)
    bot.send_message(message.chat.id, "Send /trace to start a new search.")


@bot.message_handler(content_types=["photo"])
def handle_photo(message):
    """A transfer receipt can stand in for the typed sender name."""
    uid = message.from_user.id
    db.touch_user(uid, getattr(message.from_user, "username", "") or "")
    state = STATE.get(uid)
    if state and state.get("step") == "proof":
        _submit_payment_proof(message, "", message.photo[-1].file_id)
        return
    bot.send_message(
        message.chat.id,
        "Send /trace to start a search, or /paid if you have already paid.",
    )


async def run_search(uid, user_input) -> dict:
    """Run the full lookup pipeline and return everything the views need."""
    name = user_input.get("name") or ""
    gender = user_input.get("gender") or ""
    company = user_input.get("company") or ""
    city = user_input.get("city") or ""
    telegram_username = user_input.get("username") or "unknown"

    e164, phone_variants, meta = normalizer.normalize_phone(user_input.get("phone"))
    usernames = names.generate_usernames(name, phone_variants)

    mode = (config.SEARCH_MODE or "auto").strip().lower()
    answer = ""
    web_hits = 0

    if mode in ("auto", "web") and websearch.provider() is not None:
        # Fast path: a real web search returns pages plus a grounded answer in
        # seconds. Fall through to the curated checks only if it finds nothing.
        hits, answer = await websearch.search_subject(name, company, city)
        web_hits = len(hits)
        profiles_by_url = {
            hit["url"]: {
                "display_name": hit.get("title", ""),
                "bio": hit.get("snippet", ""),
                "location": "",
                "gender": "",
            }
            for hit in hits
            if hit.get("url")
        }
    else:
        hits = []

    if not hits:
        hits = await searcher.search_usernames(usernames)
        # The curated checks only confirm a profile exists; bio fields are not
        # extracted. Display name falls back to the handle.
        profiles_by_url = {
            hit["url"]: {
                "display_name": hit.get("username", ""),
                "bio": "",
                "location": "",
                "gender": "",
            }
            for hit in hits
            if hit.get("url")
        }

    input_ctx = {
        "name": name,
        "gender": gender,
        "company": company,
        "city": city,
        "phone_e164": e164,
        "phone_variants": phone_variants,
        "meta": meta,
    }

    matches = scorer.rank_hits(hits, profiles_by_url, input_ctx)

    structured = extract.collect(input_ctx, answer, hits)
    findings = formatter.findings(structured, matches)

    # Counters only: the row records that a trace ran and how it went. The
    # name, phone, company and the profiles themselves are not stored.
    try:
        db.log_search(
            telegram_id=uid,
            telegram_username=telegram_username,
            variations_count=len(usernames),
            matches_count=len(matches),
            found=bool(findings.get("anything")),
        )
    except Exception:  # noqa: BLE001 - a counter must never break a search
        logger.warning("Search counter write failed", exc_info=True)
    dossier = formatter.format_dossier(
        input_ctx,
        matches,
        len(usernames),
        telegram_username,
        answer=answer,
        sources_count=web_hits or None,
        hits=hits,
        structured=structured,
    )
    return {
        "dossier": dossier,
        "profiles": formatter.format_profiles(
            matches, _profiles_limit(structured)
        ),
        "findings": findings,
        "matches": matches,
        "hits": hits,
        "answer": answer,
    }


def _nothing_found_text(uid: int) -> str:
    """Said when a trace came back empty, so the credit is clearly untouched."""
    text = (
        "🟢 <b>No public record found</b>\n"
        "Nothing usable came back for this subject, so this search was "
        "<b>not</b> taken from your balance."
    )
    note = access.receipt(uid)
    return f"{text}\n\n{note}" if note else text


def _run_and_reply(message, state) -> None:
    """Reply immediately, then run the async pipeline from a sync handler."""
    uid = message.from_user.id
    chat_id = message.chat.id
    payload = {
        "name": state.get("name") or "",
        "phone": state.get("phone") or "",
        "gender": state.get("gender") or "",
        "company": state.get("company") or "",
        "city": state.get("city") or "",
        "username": getattr(message.from_user, "username", "") or "",
    }

    fast = (config.SEARCH_MODE or "auto").strip().lower() in ("auto", "web")
    if fast and websearch.provider() is not None:
        bot.send_message(chat_id, "🔎 Searching the web… this usually takes seconds.")
    else:
        bot.send_message(
            chat_id, "🔎 Searching public profiles… this takes a few seconds."
        )

    try:
        result = asyncio.run(run_search(uid, payload))
    except ValueError as exc:
        bot.send_message(chat_id, f"⚠️ {html.escape(str(exc))}", parse_mode="HTML")
        return
    except Exception:  # noqa: BLE001 - report, do not crash the poller
        logger.exception("Search pipeline failed for telegram_id=%s", uid)
        bot.send_message(
            chat_id, "⚠️ The search failed. Please try again with /trace."
        )
        return

    token = _remember_view(
        {
            "uid": uid,
            "chat_id": chat_id,
            "message_id": None,
            "main": result["dossier"],
            "profiles": result["profiles"],
        }
    )
    sent = _send_html(
        chat_id, result["dossier"], reply_markup=_view_markup(token)
    )
    if sent is not None:
        VIEWS[token]["message_id"] = getattr(sent, "message_id", None)

    # Charged only once the dossier has actually gone out *and* it came back
    # with something usable: "nothing found" is not worth a search while the
    # bot is still earning its first users.
    if not result["findings"]["anything"]:
        bot.send_message(
            chat_id,
            _nothing_found_text(uid),
            parse_mode="HTML",
            reply_markup=_guide_markup(),
        )
        logger.info("No findings for telegram_id=%s; not charged", uid)
        return

    try:
        access.charge(uid)
        note = access.receipt(uid)
        if note:
            bot.send_message(chat_id, note)
    except Exception:  # noqa: BLE001 - billing must never break a trace
        logger.warning("Search allowance update failed", exc_info=True)


@bot.message_handler(commands=["backup"])
def cmd_backup(message):
    """Owner-only: send the database and a revenue CSV to the operator.

    The panel host has no external disk, so a copy that leaves the server is
    the only backup that survives it. Both files are sent to Telegram and the
    temporary copies are deleted afterwards; neither contains debtor details.
    """
    uid = message.from_user.id
    if not access.is_owner(uid):
        bot.send_message(message.chat.id, UNKNOWN_COMMAND)
        return

    stamp = time.strftime("%Y%m%d-%H%M%S")
    folder = tempfile.mkdtemp(prefix="skiptrace-backup-")
    database_copy = os.path.join(folder, f"skiptrace-{stamp}.db")
    revenue_csv = os.path.join(folder, f"revenue-{stamp}.csv")
    try:
        db.backup_to(database_copy)
        accounts = db.export_revenue_csv(revenue_csv)
    except Exception:  # noqa: BLE001 - report it instead of failing silently
        logger.exception("Backup could not be written")
        shutil.rmtree(folder, ignore_errors=True)
        bot.send_message(message.chat.id, "⚠️ The backup could not be written.")
        return

    bot.send_message(
        message.chat.id,
        f"🗄 <b>Backup ready</b>\n{accounts} account(s) in the revenue export.",
        parse_mode="HTML",
    )
    for path, caption in (
        (database_copy, "Full database copy"),
        (revenue_csv, f"Revenue export — {accounts} accounts"),
    ):
        try:
            with open(path, "rb") as handle:
                bot.send_document(message.chat.id, handle, caption=caption)
        except Exception:  # noqa: BLE001 - one failed file must not lose the other
            logger.exception("Could not send backup file %s", path)
            bot.send_message(
                message.chat.id,
                f"⚠️ Could not send {os.path.basename(path)}.",
            )
    shutil.rmtree(folder, ignore_errors=True)
    logger.info("Backup sent to operator %s (%s accounts)", uid, accounts)


def _build_id() -> str:
    """The commit the running code came from, so the console proves the pull.

    A panel host keeps the process alive across a GitHub update, so the only
    reliable way to know which code is live is to print its commit at start.
    """
    try:
        finished = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True,
            text=True,
            timeout=5,
        )
        return finished.stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001 - the marker is informational only
        return "unknown"


def main() -> None:
    if not config.BOT_TOKEN:
        print(
            "ERROR: BOT_TOKEN is not set.\n"
            "Export the token from @BotFather first, for example:\n"
            "  export BOT_TOKEN='123456789:AA...'\n"
            "then run: python bot.py",
            file=sys.stderr,
        )
        raise SystemExit(1)

    db.init_db()

    if not access.owner_id():
        logger.warning(
            "No operator set: /broadcast and /stats are unclaimed and payment "
            "claims cannot be delivered. Send /broadcast once from the "
            "operator account to claim it, or set OWNER_ID."
        )

    try:
        me = bot.get_me()
    except Exception as exc:  # noqa: BLE001 - fail clearly on a bad token
        print(
            "ERROR: Telegram rejected BOT_TOKEN or the API is unreachable.\n"
            f"Details: {type(exc).__name__}. Check the token from @BotFather "
            "and the host's network access, then try again.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    logger.info("Authenticated as @%s", getattr(me, "username", "unknown"))
    logger.info("SkipTrace V1 starting (audit log: %s)", config.DB_PATH)
    logger.info("Build %s", _build_id())
    logger.info("Long polling started")
    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=30)
    except KeyboardInterrupt:
        logger.info("Stopped by user")


if __name__ == "__main__":
    main()
