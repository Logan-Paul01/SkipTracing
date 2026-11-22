"""Structured contact and company fields pulled from public evidence.

The grounded answer is prose and the hits are pages (plus the company's own
homepage when the domain is known). All of them are scanned with explicit
rules so every line in the dossier traces back to a sentence or a URL a human
can open. Nothing is invented: a field no source supports stays empty and the
dossier says so.

Two groups come out of this module:

* person   - phone, WhatsApp, email and profile links for the subject
* company  - name, summary, website, address, phone, email and social pages
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

SOCIAL_HOSTS = {
    "linkedin.com": "LinkedIn",
    "facebook.com": "Facebook",
    "instagram.com": "Instagram",
    "tiktok.com": "TikTok",
    "x.com": "X",
    "twitter.com": "X",
}

# Search results are full of directories and provider hosts that quote a
# company but are not the company's own site, so they can never be "website".
DIRECTORY_HOSTS = {
    "linkedin.com",
    "facebook.com",
    "facebook.com.ng",
    "instagram.com",
    "tiktok.com",
    "x.com",
    "twitter.com",
    "youtube.com",
    "pinterest.com",
    "threads.net",
    "t.me",
    "wa.me",
    "whatsapp.com",
    "nairaland.com",
    "wikipedia.org",
    "google.com",
    "bing.com",
    "bloomberg.com",
    "crunchbase.com",
    "zoominfo.com",
    "rocketreach.co",
    "signalhire.com",
    "apollo.io",
    "theorg.com",
    "dnb.com",
    "ng-check.com",
    "ngcheck.com",
    "whois.com",
    "medium.com",
    "exa.ai",
    "tavily.com",
}

FREEMAIL_DOMAINS = {
    "gmail.com",
    "yahoo.com",
    "yahoo.co.uk",
    "outlook.com",
    "hotmail.com",
    "live.com",
    "icloud.com",
    "protonmail.com",
    "proton.me",
    "aol.com",
    "mail.com",
    "yandex.com",
    "gmx.com",
}

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# Nigerian mobile and landline shapes: 08075482431 or +234 807 548 2431.
PHONE_RE = re.compile(r"(?<!\d)(?:\+?234|0)[\s\-.]?\d(?:[\s\-.]?\d){8,11}(?!\d)")
DOMAIN_RE = re.compile(
    r"\b(?:[a-z0-9](?:[a-z0-9\-]*[a-z0-9])?\.)+"
    r"(?:com|ng|org|net|io|co|biz|info|africa|edu|gov)\b",
    re.I,
)
URL_RE = re.compile(r"https?://[^\s<>\"')]+", re.I)
COMPANY_SUFFIX_RE = re.compile(
    r"\b([A-Z][A-Za-z&.,'\-]*(?:\s+[A-Z][A-Za-z&.,'\-]*){0,5}?\s+"
    r"(?:Limited|Ltd\.?|Plc|PLC|Holdings|Group|Enterprises|Ventures|Company|Bank))\b"
)

ADDRESS_HINTS = (
    "plot",
    "block",
    "street",
    "road",
    "avenue",
    "estate",
    "suite",
    "floor",
    "close",
    "crescent",
    "expressway",
    "p.o. box",
    "junction",
    "bus stop",
)
OFFICE_WORDS = (
    "office",
    "desk",
    "customer",
    "corporate",
    "branch",
    "headquarters",
    "landline",
    "switchboard",
    "enquiries",
    "inquiries",
    "support",
    "reception",
    "hotline",
    "call centre",
    "call center",
    "main line",
)
PERSON_WORDS = ("his ", "her ", "direct", "personal", "mobile", "private")
ABOUT_WORDS = (
    "is a",
    "is an",
    "company",
    "firm",
    "business",
    "speciali",
    "provides",
    "operates",
    "deals",
    "supplies",
    "manufactur",
    "distribut",
)
COMPANY_STOPWORDS = {
    "limited",
    "ltd",
    "plc",
    "nigeria",
    "company",
    "group",
    "enterprises",
    "ventures",
    "and",
    "the",
    "of",
    "services",
    "service",
    "holdings",
    "bank",
    "international",
    "global",
}


def _sentences(text: str) -> list[str]:
    """Split prose into sentences, keeping bullet-style lines separate."""
    parts = re.split(r"(?<=[.!?])\s+|\n+", str(text or ""))
    return [" ".join(part.split()) for part in parts if part and part.strip()]


def _sig_tokens(value) -> set[str]:
    tokens = {
        token
        for token in re.split(r"[^a-z0-9]+", str(value or "").lower())
        if len(token) >= 3
    }
    return {token for token in tokens if token not in COMPANY_STOPWORDS}


def _bare_host(url: str) -> str:
    host = (urlparse(url).netloc or "").lower().split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


def _is_directory(host: str) -> bool:
    return any(host == bad or host.endswith("." + bad) for bad in DIRECTORY_HOSTS)


def _platform(url: str) -> tuple[str, str]:
    host = _bare_host(url)
    for domain, label in SOCIAL_HOSTS.items():
        if host == domain or host.endswith("." + domain):
            return label, domain
    return "", ""


# A profile link reaches the person; a post, video or status link does not, and
# search results are full of them, so they are never reported as profiles.
NOISE_PATHS = ("posts/", "pulse/", "video/", "reel/", "status/", "photo/", "watch")


def _is_post_link(path: str) -> bool:
    low = (path or "").lower()
    return any(marker in low for marker in NOISE_PATHS)


def _norm_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", str(raw or ""))
    if digits.startswith("234"):
        digits = digits[3:]
    elif digits.startswith("0"):
        digits = digits[1:]
    if len(digits) != 10:
        return ""
    return f"+234 {digits[:3]} {digits[3:6]} {digits[6:]}"


def _is_placeholder(digits: str) -> bool:
    """Reject example numbers from tutorials: 8012345678, 8031234567, ..."""
    if len(set(digits)) <= 4:
        return True
    ascending = "01234567890123456789"
    descending = ascending[::-1]
    for size in range(len(digits), 3, -1):
        for start in range(len(digits) - size + 1):
            chunk = digits[start : start + size]
            if chunk in ascending or chunk in descending:
                return True
    return False


def _clip(value, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _company_name_from_answer(answer: str) -> str:
    match = COMPANY_SUFFIX_RE.search(str(answer or ""))
    return " ".join(match.group(1).split()) if match else ""


def _classify_phone(sentence: str, company_tokens: set[str]) -> str:
    low = sentence.lower()
    if any(token in low for token in company_tokens):
        return "company"
    if any(word in low for word in OFFICE_WORDS):
        return "company"
    if any(word in low for word in PERSON_WORDS):
        return "person"
    return "person"


def company_domain_from_hits(hits, company: str, answer: str = "") -> str:
    """The company's own domain, matched on the hostname itself.

    Only a hostname containing part of the company name qualifies, and the
    best match wins rather than the shortest: for "General Gas Nigeria
    Limited", generalgasng.com (general + gas) must beat niglpgas.com, which
    is a different business that merely also contains "gas".

    A domain named by the answer is credited too, because the grounded answer
    usually points at the company's real site.
    """
    tokens = _sig_tokens(company)
    if not tokens:
        return ""

    answer_low = str(answer or "").lower()
    candidates = [
        _bare_host(hit.get("url") or "") for hit in hits or ()
    ] + DOMAIN_RE.findall(answer_low)

    best, best_score = "", 0
    for host in candidates:
        host = str(host or "").lower()
        if not host or _platform("https://" + host)[0] or _is_directory(host):
            continue
        score = sum(len(token) for token in tokens if token in host)
        if not score:
            continue
        if host in answer_low:
            score += 5
        if score > best_score:
            best, best_score = host, score
    return best


def _pick_website(answer: str, hits, company: str) -> str:
    """The company's own domain, else the first real domain the answer names."""
    matched = company_domain_from_hits(hits, company, answer)
    if matched:
        return matched

    for match in DOMAIN_RE.finditer(str(answer or "")):
        host = match.group(0).lower()
        if not _is_directory(host):
            return host
    return ""


def _clean_address(text: str) -> str:
    """Drop the lead-in and any contact clause glued onto the address."""
    text = re.sub(
        r"^(?:its|the|our|his|her)?\s*(?:office|head office|registered|company)?\s*address\s*(?:is|:)\s*",
        "",
        str(text or "").strip(),
        flags=re.I,
    )
    text = re.split(
        r",?\s+and\s+(?:its|the|their)\s+(?:phone|telephone|email|contact|website)",
        text,
        flags=re.I,
    )[0]
    return text.strip(" ,.;")


def _pick_address(tagged: list[tuple[str, str]]) -> str:
    """An office address, preferring the answer and the company's own site."""
    for allowed in (("answer", "site"), ("hit",)):
        for sentence, kind in tagged:
            if kind not in allowed:
                continue
            low = sentence.lower()
            if not any(hint in low for hint in ADDRESS_HINTS):
                continue
            if not re.search(r"\d", sentence):
                continue
            if len(sentence) > 240 or sentence.count("#") > 1:
                continue
            if "," not in sentence and "state" not in low:
                continue
            cleaned = _clean_address(sentence)
            if len(cleaned) < 12:
                continue
            return _clip(cleaned, 240)
    return ""


def _pick_about(
    tagged, company_tokens: set[str], person_tokens: set[str], address: str
) -> str:
    """One sentence describing the business, not the subject's job title."""
    for sentence, kind in tagged:
        if kind not in ("answer", "site"):
            continue
        low = sentence.lower()
        if address and sentence in address:
            continue
        if not any(token in low for token in company_tokens):
            continue
        if any(token in low for token in person_tokens):
            continue
        if not any(word in low for word in ABOUT_WORDS):
            continue
        return _clip(sentence, 280)
    return ""


def _collect_social_links(
    answer: str,
    hits,
    person_tokens: set[str],
    company_tokens: set[str],
    surname: str = "",
):
    """Split profile URLs into person vs company by handle and page title.

    A person link must carry the surname (or every name token). Matching on a
    first name alone attached other people's profiles to the subject, which is
    worse than reporting nothing: `oluwaseyi-ajayi` is not `oluwaseyi-olowoye`.
    """
    seen: set[str] = set()
    person: dict[str, list[str]] = {}
    company: dict[str, list[str]] = {}

    candidates: list[tuple[str, str]] = [(hit.get("url") or "", hit.get("title") or "") for hit in hits or ()]
    for url in URL_RE.findall(str(answer or "")):
        candidates.append((url, ""))

    for url, title in candidates:
        label, _domain = _platform(url)
        if not label:
            continue
        clean = url.split("?")[0].split("#")[0].rstrip("/,.;:")
        if not clean:
            continue
        # www and non-www forms of one page are the same lead.
        path_only = urlparse(clean).path.rstrip("/")
        canon = f"https://{_bare_host(clean)}{path_only}"
        if canon.lower() in seen:
            continue
        seen.add(canon.lower())
        clean = canon

        path = path_only.lower()
        title_low = str(title or "").lower()

        if _is_post_link(path):
            continue

        surname_hit = bool(surname) and surname in path
        name_in_title = bool(surname) and surname in title_low
        all_tokens = bool(person_tokens) and all(
            token in path or token in title_low for token in person_tokens
        )
        # A LinkedIn /in/ URL is a person even when the headline names the
        # company; it must never be reported as a company page.
        linkedin_person = _domain == "linkedin.com" and path.startswith("/in/")

        if surname_hit or all_tokens:
            bucket = person
        elif not linkedin_person and any(
            token in path or token in title_low for token in company_tokens
        ):
            bucket = company
        elif name_in_title:
            bucket = person
        else:
            continue

        urls = bucket.setdefault(label, [])
        if clean not in urls:
            urls.append(clean)

    return person, company


def collect(input_ctx, answer: str = "", hits=()) -> dict:
    """Return {'person': {...}, 'company': {...}} built from answer and hits."""
    ctx = input_ctx or {}
    name = str(ctx.get("name") or "")
    company_name = str(ctx.get("company") or "").strip() or _company_name_from_answer(answer)

    person_tokens = _sig_tokens(name)
    company_tokens = _sig_tokens(company_name)
    name_parts = [
        part
        for part in re.split(r"[^A-Za-z0-9]+", name.lower())
        if len(part) >= 3
    ]
    surname = name_parts[-1] if len(name_parts) > 1 else ""
    input_digits = re.sub(r"\D", "", str(ctx.get("phone_e164") or ""))

    tagged: list[tuple[str, str]] = [(sentence, "answer") for sentence in _sentences(answer)]
    for hit in hits or ():
        kind = "site" if hit.get("source") == "site" else "hit"
        blob = f"{hit.get('title') or ''}. {hit.get('snippet') or ''}"
        tagged.extend((sentence, kind) for sentence in _sentences(blob))

    phones: list[str] = []
    whatsapp: list[str] = []
    company_phones: list[str] = []
    person_emails: list[str] = []
    company_emails: list[str] = []

    website = _pick_website(answer, hits, company_name)
    company_domain = website
    address = _pick_address(tagged)

    for sentence, _kind in tagged:
        low = sentence.lower()

        relevant = (
            any(token in low for token in person_tokens)
            or any(token in low for token in company_tokens)
            or any(word in low for word in OFFICE_WORDS)
            or "whatsapp" in low
            or any(word in low for word in PERSON_WORDS)
        )

        if relevant:
            is_whatsapp = "whatsapp" in low
            for raw in PHONE_RE.findall(sentence):
                digits = re.sub(r"\D", "", raw)
                local = digits[3:] if digits.startswith("234") else digits.lstrip("0")
                if _is_placeholder(local):
                    continue
                number = _norm_phone(raw)
                if not number:
                    continue
                if _classify_phone(sentence, company_tokens) == "company":
                    bucket = company_phones
                elif is_whatsapp:
                    bucket = whatsapp
                elif input_digits and re.sub(r"\D", "", number) == input_digits:
                    # Already printed in the header as the subject's number.
                    continue
                else:
                    bucket = phones
                if number not in bucket:
                    bucket.append(number)

        for email in EMAIL_RE.findall(sentence):
            clean = email.lower().strip(".,;:")
            domain = clean.rsplit("@", 1)[-1]
            if company_domain and (domain == company_domain or domain.endswith("." + company_domain)):
                bucket = company_emails
            elif domain in FREEMAIL_DOMAINS or any(token in low for token in person_tokens):
                bucket = person_emails
            else:
                # An unrelated domain in a page that merely mentions the
                # company belongs to some other business; never report it.
                continue
            if clean not in bucket:
                bucket.append(clean)

    person_socials, company_socials = _collect_social_links(
        answer, hits, person_tokens, company_tokens, surname
    )

    return {
        "person": {
            "phones": phones,
            "whatsapp": whatsapp,
            "emails": person_emails,
            "socials": person_socials,
        },
        "company": {
            "name": company_name,
            "about": _pick_about(tagged, company_tokens, person_tokens, address),
            "website": website,
            "address": address,
            "phones": company_phones,
            "emails": company_emails,
            "socials": company_socials,
        },
    }
