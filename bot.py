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
import re
import sys
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
    "4. Company (optional)\n\n"
    f"{access.welcome_line()}\n"
    "/plan shows what is left, /premium how to pay.\n\n"
    "Only public profiles are searched. The number is used to match "
    "profiles, never as a search query, and the bot never messages or calls "
    "anyone.\n"
    "/cancel stops the current trace."
)

TEXT_LIMIT = 4000
UNKNOWN_COMMAND = "Unknown command. Send /trace to start a search or /cancel to stop."


def _new_state() -> dict:
    return {
        "step": "idle",
        "name": "",
        "phone": "",
        "gender": "",
        "company": "",
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


def _send_html(chat_id: int, text: str) -> None:
    """Send HTML, falling back to plain text if Telegram rejects the markup."""
    text = _truncate(text)
    try:
        bot.send_message(chat_id, text, parse_mode="HTML")
    except ApiTelegramException:
        logger.warning("HTML send rejected; retrying as plain text")
        bot.send_message(chat_id, _strip_html(text))


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
    # Checked here, before four steps are collected, so a user never fills in a
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
        "Step 1/4 — Send the debtor's <b>full name</b> (first and surname).",
        parse_mode="HTML",
    )


@bot.message_handler(commands=["skip"])
def cmd_skip(message):
    uid = message.from_user.id
    state = STATE.get(uid)
    if not state or state.get("step") != "company":
        bot.send_message(
            message.chat.id,
            "/skip is only valid at the company step. Send /trace to start a search.",
        )
        return
    state["company"] = ""
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
            "Step 2/4 — Send the debtor's <b>phone number</b> "
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
            "Step 3/4 — Select the debtor's <b>gender</b>.",
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
            "Step 4/4 — Send the <b>company</b> (optional) or /skip to continue.",
            parse_mode="HTML",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    if step == "company":
        state["company"] = text
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


async def run_search(uid, user_input) -> tuple[str, list[dict]]:
    """Run the full lookup pipeline and return (dossier_text, matches)."""
    name = user_input.get("name") or ""
    gender = user_input.get("gender") or ""
    company = user_input.get("company") or ""
    telegram_username = user_input.get("username") or "unknown"

    e164, phone_variants, meta = normalizer.normalize_phone(user_input.get("phone"))
    usernames = names.generate_usernames(name, phone_variants)

    mode = (config.SEARCH_MODE or "auto").strip().lower()
    answer = ""
    web_hits = 0

    if mode in ("auto", "web") and websearch.provider() is not None:
        # Fast path: a real web search returns pages plus a grounded answer in
        # seconds. Fall through to the curated checks only if it finds nothing.
        hits, answer = await websearch.search_subject(name, company)
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
        "phone_e164": e164,
        "phone_variants": phone_variants,
        "meta": meta,
    }

    matches = scorer.rank_hits(hits, profiles_by_url, input_ctx)

    try:
        search_id = db.log_search(
            telegram_id=uid,
            telegram_username=telegram_username,
            full_name=name,
            phone=e164,
            gender=gender,
            company=company,
            variations_count=len(usernames),
            matches_count=len(matches),
        )
        db.log_matches(search_id, matches)
    except Exception:  # noqa: BLE001 - audit log must never break a search
        logger.warning("Audit log write failed", exc_info=True)

    dossier = formatter.format_dossier(
        input_ctx,
        matches,
        len(usernames),
        telegram_username,
        answer=answer,
        sources_count=web_hits or None,
        hits=hits,
    )
    return dossier, matches


def _run_and_reply(message, state) -> None:
    """Reply immediately, then run the async pipeline from a sync handler."""
    uid = message.from_user.id
    chat_id = message.chat.id
    payload = {
        "name": state.get("name") or "",
        "phone": state.get("phone") or "",
        "gender": state.get("gender") or "",
        "company": state.get("company") or "",
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
        dossier, _matches = asyncio.run(run_search(uid, payload))
    except ValueError as exc:
        bot.send_message(chat_id, f"⚠️ {html.escape(str(exc))}", parse_mode="HTML")
        return
    except Exception:  # noqa: BLE001 - report, do not crash the poller
        logger.exception("Search pipeline failed for telegram_id=%s", uid)
        bot.send_message(
            chat_id, "⚠️ The search failed. Please try again with /trace."
        )
        return

    _send_html(chat_id, dossier)

    # Charged only once the dossier has actually gone out, so a failed search
    # never costs the collector a paid search.
    try:
        access.charge(uid)
        note = access.receipt(uid)
        if note:
            bot.send_message(chat_id, note)
    except Exception:  # noqa: BLE001 - billing must never break a trace
        logger.warning("Search allowance update failed", exc_info=True)


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
    logger.info("Long polling started")
    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=30)
    except KeyboardInterrupt:
        logger.info("Stopped by user")


if __name__ == "__main__":
    main()
