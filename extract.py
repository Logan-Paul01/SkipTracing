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

import phonenumbers

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


# A grounded answer often states what it could not find; those sentences are
# never evidence for a field.
NEGATIVE_PHRASES = (
    "not found",
    "not available",
    "not disclosed",
    "not listed",
    "no public",
    "could not be found",
    "were not",
    "was not",
    "unknown",
    # An answer that reports other businesses by name has not described this
    # one, however much its wording looks like a description.
    "none of",
    "no mention",
    "not mention",
    "no information",
    "not provided",
    "not included",
)


def _is_negative(sentence: str) -> bool:
    low = sentence.lower()
    return any(phrase in low for phrase in NEGATIVE_PHRASES)


def _sentences(text: str) -> list[str]:
    """Split prose into sentences, keeping bullet-style lines separate.

    Citation markers like ``[3]`` are dropped first: left in, they satisfy the
    "contains a digit" checks and a citation turns into a fake street number.
    """
    text = re.sub(r"\s*\[\d+\]", "", str(text or ""))
    parts = re.split(r"(?<=[.!?])\s+|\n+", text)
    return [" ".join(part.split()) for part in parts if part and part.strip()]


def _sig_tokens(value) -> set[str]:
    tokens = {
        token
        for token in re.split(r"[^a-z0-9]+", str(value or "").lower())
        if len(token) >= 3
    }
    return {token for token in tokens if token not in COMPANY_STOPWORDS}


def _names_subject(low: str, person_tokens: set[str]) -> bool:
    """True when the text carries the subject's whole name.

    A freemail address proves nothing about who reads it: an Egyptian shop
    page repeating a brand name also repeats a gmail inbox, and the surname
    alone is a dozen namesakes.
    """
    return bool(person_tokens) and all(token in low for token in person_tokens)


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
    """Format a Nigerian number, or reject the string entirely.

    A page abroad prints its own local shape - a Turkish office line reads
    "05325081900" and a UK line "0845 0573142" - and prefixing those ten
    digits with +234 invented numbers that cannot exist. Only a string that
    starts with the Nigerian country code or the Nigerian trunk 0 qualifies,
    and the digits must pass libphonenumber's NG validation before they reach
    a dossier.
    """
    digits = re.sub(r"\D", "", str(raw or ""))
    if digits.startswith("2340") and len(digits) == 14:
        # "+234 (0) 803 ..." is the same number written with the trunk zero.
        digits = "234" + digits[4:]
    if digits.startswith("234"):
        candidate = "+" + digits
    elif digits.startswith("0"):
        candidate = "+234" + digits[1:]
    else:
        return ""

    try:
        parsed = phonenumbers.parse(candidate, "NG")
    except phonenumbers.NumberParseException:
        return ""
    if not phonenumbers.is_valid_number_for_region(parsed, "NG"):
        return ""

    national = re.sub(
        r"\D",
        "",
        phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.NATIONAL),
    )
    if len(national) != 11 or not national.startswith("0"):
        return ""
    return f"+234 {national[1:4]} {national[4:7]} {national[7:]}"


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
    if _matches_company(low, company_tokens):
        return "company"
    if any(word in low for word in OFFICE_WORDS):
        return "company"
    if any(word in low for word in PERSON_WORDS):
        return "person"
    return "person"


def _token_present(token: str, text: str) -> bool:
    """True when the text carries the token, allowing a plural/singular swap."""
    if token in text:
        return True
    return len(token) >= 5 and token.endswith("s") and token[:-1] in text


def _matches_company(text: str, tokens: set[str]) -> bool:
    """A domain or sentence belongs to the company only if it carries its name.

    One shared word is not the company: "pukka" alone pulled in
    pukkayayinlari.com.tr, an Istanbul publisher whose contact page then
    printed a Turkish phone number as the company's, and "pukka" + "sales"
    matched a UK stationery firm's Dorset head office.

    Every significant word has to be there. A two-thirds score was not enough:
    "Jacobs Memoria Farms" of Owerri matched "Grace Jacobs Farms Limited" of
    Benin City, whose directory card then supplied a stranger's office address,
    three phone lines and a US LinkedIn page. The distinctive word ("Memoria")
    is the whole difference between two businesses, so a partial name match is
    no match at all.
    """
    if not tokens:
        return False
    return all(_token_present(token, text) for token in tokens)


def company_domain_from_hits(hits, company: str, answer: str = "") -> str:
    """The company's own domain, matched on the hostname itself.

    Only a hostname carrying the company name qualifies, and the best match
    wins rather than the shortest: for "General Gas Nigeria Limited",
    generalgasng.com (general + gas) must beat niglpgas.com, which is a
    different business that merely also contains "gas".

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
        if not _matches_company(host, tokens):
            continue
        score = sum(len(token) for token in tokens if _token_present(token, host))
        if not score:
            continue
        if host in answer_low:
            score += 5
        if score > best_score:
            best, best_score = host, score
    return best


def _pick_website(answer: str, hits, company: str) -> str:
    """The company's own domain, or nothing at all.

    A domain that does not carry the company name belongs to some other
    business, and printing it points the collector at a stranger's office.
    """
    return company_domain_from_hits(hits, company, answer)


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
    # A contact clause glued to the address is not part of it: the publisher
    # list prints "... Umuahia Abia State Tel: 07034909274", and that line
    # belongs to the listing, not to this company.
    text = re.split(
        r"[,;]?\s+(?:tel|telephone|phone|mobile|whatsapp|fax|e-?mail|email|website|call)\b\s*[:.]",
        text,
        flags=re.I,
    )[0]
    return text.strip(" ,.;")


# Directory cards (InfoIsInfo, Medioq, businesslist) print one labelled field
# per line, and the search snippet of that card is often the only public source
# for a small firm's street address and landline.
FIELD_LABEL_RE = re.compile(
    r"(?:^|[\s#|•·>,;])((?:office\s+|head\s+office\s+|business\s+|postal\s+)?"
    r"(?:address|telephone|phone|tel|mobile|whatsapp|e-?mail|email))\s*[:.]?\s*",
    re.I,
)


def _listed_fields(text: str) -> dict[str, list[str]]:
    """Label → values out of a directory card's labelled lines.

    A card's heading often carries the label with nothing after it ("... -
    address, contacts, owners") and the usable value arrives on a later line,
    so every value is kept and the caller takes the first one that is real.
    """
    parts = FIELD_LABEL_RE.split(str(text or ""))
    fields: dict[str, list[str]] = {}
    for index in range(1, len(parts) - 1, 2):
        label = " ".join(parts[index].lower().split())
        value = re.split(r"\s*(?:##|[|•·#]|📍|📞|☎)\s*", parts[index + 1])[0]
        value = " ".join(value.split()).strip(" ,.;:-–")
        if value:
            fields.setdefault(label, []).append(value)
    return fields


# A search engine's own result page is not a listing: its title is the query
# echoed back ("Jacobs Memoria Farms Imo Owerri address phone"), so it carries
# the company name by construction and its "fields" are other businesses.
QUERY_ECHO_HOSTS = {"exa.ai", "google.com", "bing.com", "duckduckgo.com"}


def _listing_fields(hits, company_tokens: set[str]):
    """Labelled fields from a card whose own title names the company.

    The company-name check lives on the page title, because the address line
    itself rarely repeats the business name. A card titled "Pukka Publishing
    and Sales Company, 26 mission Hill" is that company's listing; a card for
    another business never matches.
    """
    for hit in hits or ():
        if _bare_host(hit.get("url") or "") in QUERY_ECHO_HOSTS:
            continue
        title = str(hit.get("title") or "")
        if not _matches_company(title.lower(), company_tokens):
            continue
        fields = _listed_fields(f"{title}. {hit.get('snippet') or ''}")
        if fields:
            yield fields


def _clean_listed_address(value: str) -> str:
    """Tidy a labelled address field, dropping duplicated city words."""
    text = re.sub(r"\b(\w+)\s+\1\b", r"\1", str(value or ""), flags=re.I)
    # The label can be followed by the next field's label instead of a value.
    if re.match(
        r"(?i)^(?:phone|telephone|tel|mobile|whatsapp|e-?mail|contact|fax|website)\b",
        text,
    ):
        return ""
    text = _clean_address(text)
    text = " ".join(text.split()).strip(" ,.;:-–")
    if len(text) < 8 or not re.search(r"\d", text):
        return ""
    return _clip(text, 160)


def _pick_address(tagged: list[tuple[str, str]], company_tokens: set[str]) -> str:
    """An office address, preferring the answer and the company's own site.

    The sentence must also name the company. Without that check the dossier
    reported a different business's street address, taken verbatim from a
    long directory page that merely also mentioned the company.
    """
    for allowed in (("answer", "site"), ("hit",)):
        for sentence, kind in tagged:
            if kind not in allowed:
                continue
            low = sentence.lower()
            if _is_negative(sentence):
                continue
            if not _matches_company(low, company_tokens):
                continue
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
        if _is_negative(sentence):
            continue
        if address and sentence in address:
            continue
        if not _matches_company(low, company_tokens):
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
        elif not linkedin_person and _matches_company(
            f"{path} {title_low}", company_tokens
        ):
            bucket = company
        elif name_in_title:
            bucket = person
        else:
            continue

        urls = bucket.setdefault(label, [])
        if clean not in urls:
            urls.append(clean)

    # A surname-only match is a namesake: "chima" appears in a dozen unrelated
    # LinkedIn handles. When any link carries the full name, the surname-only
    # ones are dropped; without one they stay, so a hyphenated or shortened
    # handle is still reported.
    if person_tokens:
        full = {
            label: [url for url in urls if all(token in url.lower() for token in person_tokens)]
            for label, urls in person.items()
        }
        if any(full.values()):
            person = {label: urls for label, urls in full.items() if urls}

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

    tagged: list[tuple[str, str]] = []
    # The page a hit came from can name the subject even when the sentence
    # holding the address does not, so each sentence remembers its page too.
    page_names_subject: list[bool] = []

    def _tag(sentences, kind: str, page: str = "") -> None:
        named = _names_subject(page.lower(), person_tokens)
        for sentence in sentences:
            tagged.append((sentence, kind))
            page_names_subject.append(named)

    _tag(_sentences(answer), "answer")
    for hit in hits or ():
        kind = "site" if hit.get("source") == "site" else "hit"
        blob = f"{hit.get('title') or ''}. {hit.get('snippet') or ''}"
        _tag(_sentences(blob), kind, f"{blob} {hit.get('url') or ''}")

    phones: list[str] = []
    whatsapp: list[str] = []
    company_phones: list[str] = []
    person_emails: list[str] = []
    company_emails: list[str] = []

    website = _pick_website(answer, hits, company_name)
    company_domain = website
    address = _pick_address(tagged, company_tokens)

    for index, (sentence, _kind) in enumerate(tagged):
        low = sentence.lower()

        relevant = (
            any(token in low for token in person_tokens)
            or any(token in low for token in company_tokens)
            or any(word in low for word in OFFICE_WORDS)
            or "whatsapp" in low
            or any(word in low for word in PERSON_WORDS)
        )

        # A sentence that carries one word of the company name but not the
        # name itself belongs to some other business ("Grace Jacobs Farms
        # Limited" for "Jacobs Memoria Farms"); its numbers are nobody's here.
        names_person = any(token in low for token in person_tokens)
        other_business = (
            any(token in low for token in company_tokens)
            and not _matches_company(low, company_tokens)
            and not names_person
        )

        if relevant and not other_business:
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
            elif domain in FREEMAIL_DOMAINS:
                # A freemail inbox is unowned evidence: it reaches Contact
                # details only when the sentence or its page names the whole
                # subject, never on the domain or a surname alone.
                if not (_names_subject(low, person_tokens) or page_names_subject[index]):
                    continue
                bucket = person_emails
            elif any(token in low for token in person_tokens):
                bucket = person_emails
            else:
                # An unrelated domain, or a freemail inbox nobody ties to the
                # subject, belongs to some other business; never report it.
                continue
            if clean not in bucket:
                bucket.append(clean)

    # A directory card is the only source for some small firms' street address
    # and landline, and its fields are already labelled, so no sentence has to
    # be guessed at. Only fills what the answer and the site left empty.
    listed_addresses: list[str] = []
    for fields in _listing_fields(hits, company_tokens):
        for label, values in fields.items():
            if "address" not in label:
                continue
            for value in values:
                candidate = _clean_listed_address(value)
                if candidate:
                    listed_addresses.append(candidate)
        if not company_phones:
            for label, values in fields.items():
                if not any(key in label for key in ("telephone", "phone", "tel", "mobile")):
                    continue
                for value in values:
                    for raw in PHONE_RE.findall(value):
                        number = _norm_phone(raw)
                        if number and number not in company_phones:
                            company_phones.append(number)

    # One card prints the street only, another the full line with the road and
    # state; the most complete address is the useful one.
    if not address and listed_addresses:
        address = max(listed_addresses, key=len)

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
