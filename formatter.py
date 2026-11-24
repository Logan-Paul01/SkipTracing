"""Telegram HTML dossier formatting.

Layout, in the order a collector reads a lead:

    header      subject, phone, gender, company, audit line
    answer      what the sources say, in one paragraph
    contact     the subject's phone, WhatsApp, email and profile links
    company     what the company does, where it is, how to reach it
    matches     ranked public profiles with their evidence
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

import config
import extract

MAX_MATCHES = 8
MATCHES_WITH_STRUCTURE = 3
BIO_LIMIT = 200
SNIPPET_LIMIT = 180
ANSWER_LIMIT = 700
TEXT_LIMIT = 3900
MEDALS = ("🥇", "🥈", "🥉")
NOT_FOUND = "not found in public sources"
EVIDENCE_LIMIT = 3

# Search results carry the source page's own markdown and site suffix
# ("## Experience ###", "... | LinkedIn"). A collector reading a dossier should
# never see those markers, so the renderer strips them instead of trusting the
# engine to have cleaned its snippets.
SITE_SUFFIX_RE = re.compile(
    r"\s*[|\-–—·]\s*(?:linkedin|facebook|instagram|twitter|x|tiktok|"
    r"nairaland(?:\s+forum)?|wikipedia|crunchbase|zoominfo|signalhire|"
    r"rocketreach|theorg|pinterest|youtube)\.?\s*$",
    re.I,
)

# Rules the reader already assumes from the layout, and their short labels.
REASON_SKIP = (
    "public web page found",
    "public profile exists",
    "profile has both a display name and a bio",
)
REASON_LABELS = (
    (r"^name similarity (\d+)%$", r"Name match \1%"),
    (r"^company mentioned in bio.*$", "Company in bio"),
    (r"^phone number appears in profile text$", "Phone in profile"),
    (r"^username is the debtor's phone number$", "Handle is the phone number"),
    (r"^location matches \((.+)\)$", r"Location \1"),
    (r"^location partially matches \((.+)\)$", r"Location near \1"),
    (r"^login wall or private profile.*$", "Login wall"),
    (r"^gender consistent.*$", "Gender consistent"),
    (r"^same username found on 2\+ platforms$", "Same handle on 2+ platforms"),
)


def _clip(value, limit: int = BIO_LIMIT) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _clip_sentence(value, limit: int) -> str:
    """Clip prose at a sentence end so the answer never stops mid-word."""
    text = " ".join(str(value or "").split())
    # Grounded answers carry citation markers like [1][3]; the sources are
    # listed below, so the markers only add clutter here.
    text = re.sub(r"\s*\[\d+\]", "", text)
    if len(text) <= limit:
        return text
    window = text[:limit]
    cut = max(window.rfind(". "), window.rfind("! "), window.rfind("? "))
    if cut > limit // 2:
        return window[: cut + 1]
    return window.rsplit(" ", 1)[0].rstrip(",;:") + "…"


def _clean_text(value) -> str:
    """Scraped title or snippet with markdown and spacing noise removed."""
    text = html.unescape(str(value or ""))
    # Markdown headings, but not a real "#4" in an address.
    text = re.sub(r"\s*#{1,6}(?!\d)\s*", " ", text)
    text = text.replace("**", "").replace("__", "").replace("`", "")
    text = re.sub(r"\s*[|•·]+\s*", " · ", text)
    text = re.sub(r",(?=\S)", ", ", text)
    text = re.sub(r"\s*[–—]+\s*", " — ", text)
    text = re.sub(r"\.{3,}", "…", text)
    return " ".join(text.split()).strip(" ·")


def _clean_title(value) -> str:
    """Page title without the site's own suffix, clipped for the card line."""
    text = _clean_text(value)
    text = SITE_SUFFIX_RE.sub("", text)
    text = re.sub(r"\s*·\s*$", "", text)
    return _clip(text, 70)


def _pretty_handle(value: str) -> str:
    """A bare profile handle as a readable name: wizkid-ayodeji-b-127b59 -> Wizkid Ayodeji B.

    Search hits often arrive without a page title, and a dossier line reading
    "wizkid-ayodeji-balogun-127b59122" looks like a database dump. The id
    fragments are dropped; the link below still carries the real URL.
    """
    parts = [
        part
        for part in re.split(r"[-_.]+", str(value or "").strip())
        if part and not re.fullmatch(r"(?:[0-9a-f]{4,}|\d+)", part.lower())
    ]
    if not parts:
        return ""
    return " ".join(part[:1].upper() + part[1:] for part in parts)


def _short_url(url: str, limit: int = 46) -> str:
    """A link label short enough to read instead of a wrapping raw URL."""
    parsed = urlparse(str(url or ""))
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    label = host + parsed.path.rstrip("/")
    if len(label) > limit:
        label = label[: limit - 1].rstrip("/-_.") + "…"
    return label or str(url or "")


def _evidence_line(reasons) -> str:
    """The scoring signals that actually separate one match from another."""
    items: list[str] = []
    for reason in reasons or ():
        text = " ".join(str(reason or "").split())
        low = text.lower()
        if not text or low in REASON_SKIP:
            continue
        for pattern, label in REASON_LABELS:
            if re.match(pattern, low):
                text = re.sub(pattern, label, text, count=1, flags=re.I)
                break
        if text and text not in items:
            items.append(text)
    return " · ".join(items[:EVIDENCE_LIMIT])


def _next_action(matches: list[dict]) -> str:
    if not matches:
        return "try different spelling or check CAC"

    reasons = " ".join(matches[0].get("reasons") or []).lower()
    if "phone number appears" in reasons:
        return "verify before calling"
    if "2+ platforms" in reasons:
        return "high identity confidence"
    if "login wall" in reasons:
        return "try manual LinkedIn search"
    return "manually review the top match before acting"


def _pretty_phone(value: str) -> str:
    """Nigerian E.164 to +234 803 123 4567, which is easier to dial and read."""
    raw = str(value or "").strip()
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("234") and len(digits) == 13:
        return f"+234 {digits[3:6]} {digits[6:9]} {digits[9:]}"
    return raw


def _source_links(hits, limit: int = 4) -> str:
    """The evidence behind the summary, as clickable source names.

    Hosts are deduplicated, so ten LinkedIn pages collapse to one link and the
    reader sees how much breadth is actually behind the claim.
    """
    hosts: dict[str, tuple[str, str]] = {}
    for hit in hits or ():
        url = str(hit.get("url") or "").strip()
        if not url:
            continue
        host = (urlparse(url).netloc or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if not host or host in hosts:
            continue
        label = " ".join(str(hit.get("site") or "").split()) or host
        hosts[host] = (url, label)

    if not hosts:
        return ""

    shown = list(hosts.values())[:limit]
    links = [
        f'<a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>'
        for url, label in shown
    ]
    remaining = len(hosts) - len(shown)
    joined = ", ".join(links)
    if remaining > 0:
        joined += f" and {remaining} more"
    return joined


def _answer_block(answer: str, hits) -> list[str]:
    """The grounded summary as a quote, with its sources as blue text.

    No emoji and no heading glyph: the quote bar carries the hierarchy, the
    prose stays in italics, and the source names are real links.
    """
    text = _clip_sentence(answer, ANSWER_LIMIT)
    if not text:
        return []

    lines = [
        "<blockquote><b>What the sources say</b>",
        f"<i>{html.escape(text)}</i>",
    ]
    sources = _source_links(hits)
    if sources:
        lines.append("")
        lines.append(f"<b>Evidence:</b> {sources}")
    lines.append("</blockquote>")
    lines.append("")
    return lines


def _link(label: str, url: str) -> str:
    safe_url = html.escape(str(url or ""), quote=True)
    return f'<b>{html.escape(label)}:</b> <a href="{safe_url}">{html.escape(_short_url(str(url or "")))}</a>'


def _social_lines(socials: dict[str, list[str]]) -> list[str]:
    """One line per platform, in the order collectors look for them."""
    order = ("LinkedIn", "Facebook", "Instagram", "X", "TikTok")
    lines = []
    for label in order:
        urls = socials.get(label) or []
        if not urls:
            continue
        # Two links per platform is enough to reach the subject; more would
        # eat the space the ranked evidence needs.
        for url in urls[:2]:
            lines.append(
                f'<b>{html.escape(label)}:</b> '
                f'<a href="{html.escape(url, quote=True)}">{html.escape(_short_url(url))}</a>'
            )
    for label, urls in socials.items():
        if label in order or not urls:
            continue
        lines.append(_link(label, urls[0]))
    return lines


def _contact_block(person: dict) -> list[str]:
    lines = ["📇 <b>Contact details</b>"]

    phones = person.get("phones") or []
    if phones:
        lines.extend(f"<b>Phone:</b> {html.escape(number)}" for number in phones[:2])
    else:
        # The searched number is already printed in the header, so this line
        # reports the absence of an extra number rather than "not found".
        lines.append("<b>Phone:</b> no other number in public sources")

    if person.get("whatsapp"):
        lines.extend(
            f"<b>WhatsApp:</b> {html.escape(number)}" for number in person["whatsapp"]
        )

    emails = person.get("emails") or []
    if emails:
        lines.extend(f"<b>Email:</b> {html.escape(address)}" for address in emails[:2])
    else:
        lines.append(f"<b>Email:</b> {NOT_FOUND}")

    social_lines = _social_lines(person.get("socials") or {})
    if social_lines:
        lines.extend(social_lines)
    else:
        lines.append("<b>Social profiles:</b> not found in public sources")

    lines.append("")
    return lines


def _company_block(company: dict) -> list[str]:
    lines = ["🏢 <b>Company details</b>"]

    if not company.get("name"):
        lines.append("No company supplied and none named in the sources.")
        lines.append("")
        return lines

    lines.append(f"<b>Company:</b> {html.escape(company['name'])}")

    if company.get("about"):
        lines.append(f"<b>About:</b> {html.escape(company['about'])}")

    if company.get("website"):
        host = company["website"]
        lines.append(_link("Website", host if host.startswith("http") else f"https://{host}"))

    if company.get("address"):
        lines.append(f"<b>Address:</b> {html.escape(company['address'])}")

    for label in ("phones", "emails"):
        values = company.get(label) or []
        if not values:
            continue
        for value in values[:3]:
            name = "Phone" if label == "phones" else "Email"
            lines.append(f"<b>{name}:</b> {html.escape(value)}")

    social_lines = _social_lines(company.get("socials") or {})
    if social_lines:
        lines.extend(social_lines)

    lines.append("")
    return lines


def _match_blocks(matches: list[dict], limit: int) -> list[list[str]]:
    """One card per profile: rank, name, quoted excerpt, link, evidence.

    The card is the same shape every time, so a collector can scan a dossier
    top to bottom: who the page belongs to, what it says, where to open it and
    why it scored. The excerpt is a quote so it reads as evidence, not as the
    bot's own claim.
    """
    blocks: list[list[str]] = []
    for index, match in enumerate(matches[:limit]):
        medal = MEDALS[index] if index < len(MEDALS) else "▫️"
        site = html.escape(_clean_text(match.get("site")) or "Unknown site")
        score = int(match.get("score") or 0)
        display_name = _clean_title(
            match.get("display_name") or match.get("username") or ""
        )
        if display_name and " " not in display_name:
            display_name = _pretty_handle(display_name) or display_name
        raw_url = str(match.get("url") or "").strip()

        block = [f"{medal} <b>{site}</b> — {score}/100"]
        if display_name:
            block.append(f"<b>{html.escape(display_name)}</b>")

        excerpt = _clip_sentence(
            _clean_text(match.get("snippet") or match.get("bio") or ""), SNIPPET_LIMIT
        )
        # LinkedIn and directory snippets repeat the page title before the real
        # text, and the title is already the line above, so drop the repeat.
        for lead in (display_name, display_name.split(" - ")[0]):
            if lead and excerpt.lower().startswith(lead.lower()):
                excerpt = excerpt[len(lead):].lstrip(" -—·,:;")
                break
        if excerpt and excerpt.lower() != display_name.lower():
            block.append(f"<blockquote>{html.escape(excerpt)}</blockquote>")

        if raw_url:
            block.append(
                f'<a href="{html.escape(raw_url, quote=True)}">'
                f"{html.escape(_short_url(raw_url))}</a>"
            )

        evidence = _evidence_line(match.get("reasons"))
        if evidence:
            block.append(f"<i>{html.escape(evidence)}</i>")

        block.append("")
        blocks.append(block)
    return blocks


def format_dossier(
    input_ctx,
    matches,
    variations_count,
    telegram_username,
    answer: str = "",
    sources_count=None,
    hits=(),
    structured=None,
) -> str:
    """Render the dossier as Telegram-safe HTML.

    The ranked profiles are deliberately not in this text: the collector opens
    them with the button under the message, so the dossier itself stays the
    reading order that matters on a first pass (who, what the sources say,
    contacts, company).
    """
    input_ctx = input_ctx or {}
    matches = list(matches or [])
    if structured is None:
        structured = extract.collect(input_ctx, answer, hits)

    name = html.escape(str(input_ctx.get("name") or "Unknown"))
    # Collectors need the full number to dial it, so it is never masked here.
    phone = html.escape(_pretty_phone(str(input_ctx.get("phone_e164") or "").strip()) or NOT_FOUND)
    gender = html.escape(str(input_ctx.get("gender") or "Not stated"))
    company_name = html.escape(
        str(input_ctx.get("company") or "").strip()
        or structured["company"].get("name")
        or "not supplied"
    )
    searched_by = html.escape(str(telegram_username or "unknown").lstrip("@"))
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    head = [
        "🔎 <b>SKIPTRACE DOSSIER</b>",
        f"<b>Subject:</b> {name}",
        f"<b>Phone number:</b> {phone}",
        f"<b>Gender:</b> {gender}",
        f"<b>Company:</b> {company_name}",
        f"<b>Audit:</b> searched by @{searched_by} at {timestamp}",
    ]
    if sources_count:
        head.append(f"<b>Sources scanned:</b> {int(sources_count)} public pages")
    else:
        head.append(f"<b>Variations tested:</b> {int(variations_count or 0)} usernames")
    head.append("")

    body: list[str] = list(head)

    body.extend(_answer_block(answer, hits))

    body.extend(_contact_block(structured["person"]))

    if answer or hits:
        body.extend(_company_block(structured["company"]))
    else:
        body.append("🏢 <b>Company details</b>")
        body.append("No company supplied and the profile search found none.")
        body.append("")

    found = findings(structured, matches)
    if found["profiles"]:
        label = "profile" if found["profiles"] == 1 else "profiles"
        body.append(
            f"🏅 <b>{found['profiles']} matching {label}</b> — open them with "
            "the button below."
        )
    else:
        body.append("🏅 No qualifying public profile found in the sources.")
    body.append("")

    tail = [f"<b>Suggested next action:</b> {html.escape(_next_action(matches))}"]

    return "\n".join(list(body) + tail)


def findings(structured, matches) -> dict:
    """Did this trace actually come back with something usable?

    A trace costs the collector a search, so "nothing found" must not spend
    one. A company that exists is a finding even when the person is not, and
    so is any contact detail or any profile that clears the reporting floor;
    a page that merely repeats the searched name is not.
    """
    person = (structured or {}).get("person") or {}
    company = (structured or {}).get("company") or {}

    profiles = [
        match
        for match in matches or ()
        if int(match.get("score") or 0) >= config.MIN_SCORE_TO_REPORT
    ]
    person_found = bool(
        person.get("phones")
        or person.get("whatsapp")
        or person.get("emails")
        or person.get("socials")
    )
    company_found = bool(
        company.get("about")
        or company.get("address")
        or company.get("phones")
        or company.get("emails")
        or company.get("website")
        or company.get("socials")
    )
    return {
        "profiles": len(profiles),
        "person": person_found,
        "company": company_found,
        "anything": bool(profiles or person_found or company_found),
    }


def format_profiles(matches, limit=None) -> str:
    """The "Top matching profiles" tab, opened from the dossier buttons."""
    matches = list(matches or [])
    if limit is None:
        limit = MAX_MATCHES

    lines = [
        "🏅 <b>Top matching profiles</b>",
        "<i>Highest-confidence public pages first</i>",
        "",
    ]

    blocks = _match_blocks(matches, limit)
    if not blocks:
        lines.append(
            "No public profile cleared the reporting floor for this subject. "
            "A namesake's page or a company page is not the person."
        )
        return "\n".join(lines)

    def render(items: list[list[str]]) -> str:
        body = list(lines)
        for block in items:
            body.extend(block)
        return "\n".join(body)

    text = render(blocks)
    while blocks and len(text) > TEXT_LIMIT:
        blocks.pop()
        text = render(blocks)
    return text
