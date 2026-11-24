"""Bank-account details a page already published next to the subject.

A NUBAN cannot be predicted from a phone number. The 10-digit account number is
issued by the bank, its check digit only validates a number you already hold,
and Nigerian mobile prefixes carry no bank information (portability removed
even the weak signal). So nothing here guesses an account.

What this module does is read the pages a trace already returned, and one extra
targeted search when the collector taps the button, and report the account
details a person or company has published themselves: "Acct: 0123456789
GTBank" on a marketplace listing, a business page or a forum post. Every line
keeps the source URL so the collector can open it before acting.

Precision rules, because a wrong account number is worse than none:

* exactly ten digits, never a tutorial placeholder and never the subject's own
  phone number;
* a strong account label ("account", "acct", "acc", "a/c", "nuban") within 45
  characters of the number; "accounts" and "accountant" do not count;
* a nearer phone word ("phone", "whatsapp", "mobile", "tel", "call", "gsm",
  "contact") means the digits are a phone line, so the match is dropped;
* a bank name (whole word, or glued as "ZenithBank") within 45 characters;
* a currency amount such as "₦1000000000" is not an account number.
"""

from __future__ import annotations

import html
import re
from urllib.parse import urlparse

# Longest alias first, so "guaranty trust bank" wins over "gtb" when both match.
BANKS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Access Bank", ("access bank", "accessbank", "diamond bank")),
    ("GTBank", ("guaranty trust bank", "guaranty trust", "gtbank", "gtb")),
    ("Zenith Bank", ("zenith bank", "zenith")),
    ("UBA", ("united bank for africa", "uba")),
    ("First Bank", ("first bank of nigeria", "first bank", "firstbank", "firstmonie")),
    ("Union Bank", ("union bank",)),
    ("Fidelity Bank", ("fidelity bank", "fidelity")),
    ("Wema Bank", ("wema bank", "wema", "alat")),
    ("Sterling Bank", ("sterling bank", "sterling")),
    ("Polaris Bank", ("polaris bank", "polaris", "skye bank")),
    ("Keystone Bank", ("keystone bank", "keystone")),
    ("Unity Bank", ("unity bank",)),
    ("FCMB", ("first city monument bank", "fcmb")),
    ("Stanbic IBTC", ("stanbic ibtc", "stanbic", "ibtc")),
    ("Ecobank", ("ecobank",)),
    ("Jaiz Bank", ("jaiz bank", "jaiz")),
    ("Providus Bank", ("providus bank", "providus")),
    ("Suntrust Bank", ("suntrust bank", "suntrust")),
    ("Globus Bank", ("globus bank", "globus")),
    ("Titan Trust Bank", ("titan trust bank", "titan trust")),
    ("Parallex Bank", ("parallex bank", "parallex")),
    ("Optimus Bank", ("optimus bank", "optimus")),
    ("Signature Bank", ("signature bank",)),
    ("PremiumTrust Bank", ("premiumtrust", "premium trust bank")),
    ("Lotus Bank", ("lotus bank",)),
    ("Rubies MFB", ("rubies mfb", "rubies bank", "rubies")),
    ("Kuda", ("kuda bank", "kuda mfb", "kuda")),
    ("OPay", ("opay",)),
    ("Moniepoint", ("moniepoint", "monie point")),
    ("PalmPay", ("palmpay", "palm pay")),
    ("FairMoney", ("fairmoney",)),
    ("Carbon", ("carbon mfb",)),
    ("Sparkle", ("sparkle mfb", "sparkle")),
    ("VFD", ("vfd mfb", "vbank", "vfd")),
    ("Paga", ("paga",)),
    ("MoMo PSB", ("momo psb", "momo payment service bank")),
    ("SmartCash PSB", ("smartcash psb", "smartcash")),
    ("9PSB", ("9psb", "9 payment service bank")),
    ("Hope PSB", ("hope psb",)),
)

ACCOUNT_RE = re.compile(r"(?<![\d+])(\d{10})(?!\d)")
# A strong account word and nothing looser: "accounts", "accountant" and a
# bare "payment" are far too common to label a number on their own.
LABEL_RE = re.compile(r"(?<![a-z0-9])(?:a/c|account|acct|acc|nuban)(?![a-z0-9])")
# When one of these sits closer to the digits than the account label does, the
# page is printing a phone line, not an account number.
PHONE_RE = re.compile(
    r"(?<![a-z0-9])(?:phone|whatsapp|mobile|tel|call|gsm|contact)(?![a-z0-9])"
)
MONEY_RE = re.compile(r"₦|ngn|naira")
# "Account balance 1000000000" is an amount, not an account number. Only the
# dozen characters right before the digits are checked, so "amount to account
# 0123456789" is not thrown away.
AMOUNT_RE = re.compile(
    r"(?<![a-z])(?:balance|amount|total|loan|limit|fee|salary|price|budget|"
    r"turnover|revenue)(?![a-z])"
)
NAME_RE = re.compile(
    r"(?:account|acct)\s*name\s*[:\-]\s*([A-Za-z][A-Za-z .'\-]{2,60})", re.I
)
WINDOW = 60
MATCH_WINDOW = 45
SNIPPET_LIMIT = 170
MAX_ACCOUNTS = 8


def _bank_pattern(alias: str) -> re.Pattern:
    """Whole-word alias, also accepting the glued form ("ZenithBank")."""
    return re.compile(
        r"(?<![a-z0-9])" + re.escape(alias) + r"(?:bank|mfb|psb)?(?![a-z0-9])"
    )


BANK_MATCHERS: tuple[tuple[str, tuple[re.Pattern, ...]], ...] = tuple(
    (label, tuple(_bank_pattern(alias) for alias in aliases))
    for label, aliases in BANKS
)


def _looks_like_example(number: str) -> bool:
    """Reject tutorials and placeholders, not every number with a run in it.

    A real account number can hold "4567", so only a whole ascending or
    descending sequence, or a number made of one or two digits, is dropped
    here. The phone-number detector in ``extract`` is deliberately stricter and
    would throw away valid accounts.
    """
    if len(set(number)) <= 2:
        return True
    return number in ("0123456789", "1234567890", "9876543210")


def _closest(pattern: re.Pattern, window: str, at: int) -> int | None:
    """Distance from ``at`` to the nearest match, or None when nothing matches."""
    near = None
    for match in pattern.finditer(window):
        distance = abs(match.start() - at)
        if near is None or distance < near:
            near = distance
    return near


def _bank_in(window: str, number_at: int) -> str:
    """The bank closest to the number, if one sits close enough to mean it."""
    best_label, best_distance = "", None
    for label, patterns in BANK_MATCHERS:
        for pattern in patterns:
            distance = _closest(pattern, window, number_at)
            if distance is None:
                continue
            # Strictly nearer only, so the longest alias wins a tie.
            if best_distance is None or distance < best_distance:
                best_label, best_distance = label, distance
    if best_distance is None or best_distance > MATCH_WINDOW:
        return ""
    return best_label


def _source_label(url: str) -> str:
    host = (urlparse(url).netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host or url


def find_accounts(texts, ignore_digits: str = "") -> list[dict]:
    """Account details stated in ``(text, url)`` pairs.

    ``ignore_digits`` is the subject's own number: a page that prints it is not
    publishing an account number, and the two are both ten digits long locally.
    """
    found: list[dict] = []
    seen: set[tuple[str, str]] = set()
    ignore = re.sub(r"\D", "", str(ignore_digits or ""))[-10:]

    for text, url in texts or ():
        blob = " ".join(str(text or "").split())
        if not blob:
            continue
        low = blob.lower()
        for match in ACCOUNT_RE.finditer(low):
            number = match.group(1)
            if number == ignore:
                continue
            if _looks_like_example(number):
                continue

            start = max(0, match.start() - WINDOW)
            end = min(len(blob), match.end() + WINDOW)
            window = low[start:end]
            number_at = match.start() - start

            label_distance = _closest(LABEL_RE, window, number_at)
            if label_distance is None or label_distance > MATCH_WINDOW:
                continue
            phone_distance = _closest(PHONE_RE, window, number_at)
            if phone_distance is not None and phone_distance < label_distance:
                continue
            before = low[max(0, match.start() - 12) : match.start()]
            if MONEY_RE.search(before) or AMOUNT_RE.search(before):
                continue

            bank = _bank_in(window, number_at)
            if not bank:
                continue

            key = (bank, number)
            if key in seen:
                continue
            seen.add(key)

            name_match = NAME_RE.search(blob[start:end])
            found.append(
                {
                    "bank": bank,
                    "number": number,
                    "holder": " ".join(name_match.group(1).split()) if name_match else "",
                    "url": str(url or "").strip(),
                    "source": _source_label(str(url or "")),
                    "snippet": " ".join(blob[start:end].split())[:SNIPPET_LIMIT],
                }
            )
            if len(found) >= MAX_ACCOUNTS:
                return found
    return found


def merge(*groups) -> list[dict]:
    """Combine result sets, keeping the first mention of each account."""
    merged: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for group in groups:
        for item in group or ():
            key = (item.get("bank", ""), item.get("number", ""))
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
    return merged[:MAX_ACCOUNTS]


def format_view(
    accounts: list[dict], subject: str = "", company: str = "", searched: bool = True
) -> str:
    """The "Associated banks" tab of a dossier."""
    lines = [
        "🏦 <b>Associated banks</b>",
        "<i>Account details this person or company has published on a public "
        "page.</i>",
        "",
    ]

    if not accounts:
        lines.append("No public page linked an account number to this name")
        lines[-1] += f" or to {html.escape(company)}." if company else "."
        if searched:
            lines.append("")
            lines.append(
                "<i>A targeted search was run for published account details and "
                "came back empty.</i>"
            )
    else:
        for item in accounts:
            lines.append(
                f"• <b>{html.escape(item['bank'])}</b> — "
                f"<code>{html.escape(item['number'])}</code>"
            )
            if item.get("holder"):
                lines.append(f"  Name: {html.escape(item['holder'])}")
            if item.get("url"):
                label = html.escape(item.get("source") or "source")
                lines.append(
                    f'  <a href="{html.escape(item["url"], quote=True)}">{label}</a>'
                )
            lines.append("")

    lines.append(
        "<blockquote><b>How this is found</b>\n"
        "<i>A NUBAN cannot be predicted from a phone number: the bank issues "
        "it, the check digit only validates a number you already have, and "
        "mobile prefixes no longer identify a network or a bank. Every number "
        "above is a detail someone published themselves — verify it with the "
        "bank before you rely on it.</i></blockquote>"
    )
    return "\n".join(lines)
