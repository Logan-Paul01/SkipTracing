"""Fast web-search evidence gathering.

Tavily and Exa are web-search APIs: you send a query and get back real pages
with titles, URLs, extracted text and (optionally) a grounded answer with
citations. That is the same pattern as Google's AI mode, and it answers in
roughly one to two seconds, where walking thousands of profile sites takes
hours.


This module turns whichever provider is configured into the plain hit dicts
the scorer already understands, so the ranking rules stay unchanged and every
lead still points at a real URL. `searcher.py` keeps a few curated HTTP checks
as the fallback when no web-search key is present.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from urllib.parse import urlparse

import aiohttp

import config
import extract

logger = logging.getLogger(__name__)

EXA_SEARCH_URL = "https://api.exa.ai/search"
EXA_ANSWER_URL = "https://api.exa.ai/answer"
TAVILY_SEARCH_URL = "https://api.tavily.com/search"

# Friendly labels for the platforms that matter for this use case; anything
# else is shown by its hostname.
SITE_LABELS = {
    "linkedin.com": "LinkedIn",
    "facebook.com": "Facebook",
    "instagram.com": "Instagram",
    "tiktok.com": "TikTok",
    "x.com": "X",
    "twitter.com": "X",
    "nairaland.com": "Nairaland",
    "ng-check.com": "NG-Check",
    "ngcheck.com": "NG-Check",
    "facebook.com.ng": "Facebook",
    "github.com": "GitHub",
    "youtube.com": "YouTube",
}


def provider() -> str | None:
    """Which web-search backend is configured, if any."""
    if config.TAVILY_API_KEY:
        return "tavily"
    if config.EXA_API_KEY:
        return "exa"
    return None


def _site_label(url: str) -> str:
    host = (urlparse(url).netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    for domain, label in SITE_LABELS.items():
        if host == domain or host.endswith("." + domain):
            return label
    return host or "web"


# Only these hosts expose a handle as their first path segment. Taking the
# first segment of any URL yields words like "reference" or "software", which
# then get scored as if they were usernames.
PROFILE_HOSTS = (
    "linkedin.com",
    "facebook.com",
    "instagram.com",
    "tiktok.com",
    "x.com",
    "twitter.com",
    "nairaland.com",
    "github.com",
    "youtube.com",
)

_PROFILE_PATH_PREFIXES = ("in", "company", "u", "user", "users", "profile", "pages", "p")


def _username_from_url(url: str) -> str:
    """Best-effort handle from a profile URL, for name/phone similarity."""
    host = (urlparse(url).netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not any(host == domain or host.endswith("." + domain) for domain in PROFILE_HOSTS):
        return ""

    parts = [part for part in (urlparse(url).path or "").strip("/").split("/") if part]
    if not parts:
        return ""
    if parts[0] in _PROFILE_PATH_PREFIXES and len(parts) > 1:
        candidate = parts[1]
    else:
        candidate = parts[0]
    candidate = candidate.split("?")[0].lstrip("@")
    return candidate if re.fullmatch(r"[A-Za-z0-9._-]{3,40}", candidate or "") else ""


def build_queries(name: str, company: str = "", phone_variants=()) -> list[str]:
    """The queries a human skiptracer would actually type."""
    name = " ".join(str(name or "").split())
    company = " ".join(str(company or "").split())
    queries: list[str] = []

    if company:
        queries.append(f'"{name}" "{company}"')
        queries.append(f'"{name}" {company} Nigeria')
        # The company's own site carries the address, phone lines and socials
        # that the dossier reports under Company details.
        queries.append(f'"{company}" official website address phone email contact')
        queries.append(f'"{company}" facebook OR instagram OR twitter OR linkedin page')
    else:
        queries.append(f'"{name}" Nigeria')

    queries.append(f'"{name}" linkedin OR facebook OR instagram profile')

    for variant in phone_variants or ():
        digits = re.sub(r"\D", "", str(variant or ""))
        if len(digits) >= 10:
            queries.append(f'"{digits}" OR "{digits}" Nigeria')
            break

    return queries


def answer_prompt(name: str, company: str = "") -> str:
    """Question for the provider's grounded-answer endpoint.

    The dossier pulls its structured fields out of this answer, so the prompt
    asks for them by name instead of hoping they show up in a biography.
    """
    subject = f'"{name}"'
    if company:
        subject += f" of {company}"

    parts = [
        f"Identify {subject}.",
        "Report only what public sources support: full name, job title, employer, "
        "city and state, education, personal email address, mobile or WhatsApp "
        "number, and links to their public LinkedIn, Facebook, Instagram, TikTok "
        "and X profiles.",
    ]
    if company:
        parts.append(
            f"Then describe {company}: what the business does, its official website, "
            "its office address, its phone numbers and email addresses, and its "
            "social media pages."
        )
    parts.append(
        "Give phone numbers in full international format and full URLs. If a "
        "detail is not public, say it was not found."
    )
    return " ".join(parts)


def _hit_from(url: str, title: str, text: str, backend: str) -> dict | None:
    url = (url or "").strip()
    if not urlparse(url).netloc:
        return None
    snippet = " ".join(str(text or "").split())
    return {
        "site": _site_label(url),
        "url": url,
        "title": " ".join(str(title or "").split()),
        "snippet": snippet[: config.WEB_HIGHLIGHT_CHARS],
        "username": _username_from_url(url),
        "source": backend,
    }


async def _post_json(session, url: str, payload: dict, headers: dict) -> dict:
    timeout = aiohttp.ClientTimeout(total=config.WEB_SEARCH_TIMEOUT)
    async with session.post(url, json=payload, headers=headers, timeout=timeout) as response:
        if response.status != 200:
            body = (await response.text())[:200]
            raise RuntimeError(f"{url} returned HTTP {response.status}: {body}")
        return await response.json()


async def _exa_search(session, query: str, headers: dict) -> list[dict]:
    payload = {
        "query": query,
        "type": "auto",
        "numResults": config.WEB_MAX_RESULTS,
        "contents": {
            "text": {"maxCharacters": config.WEB_HIGHLIGHT_CHARS},
            "highlights": True,
        },
    }
    data = await _post_json(session, EXA_SEARCH_URL, payload, headers)
    hits = []
    for item in data.get("results") or ():
        highlights = item.get("highlights") or []
        text = " ".join(str(part) for part in highlights if part) or item.get("text") or ""
        hit = _hit_from(item.get("url"), item.get("title"), text, "exa")
        if hit:
            hits.append(hit)
    return hits


async def _exa_answer(session, prompt: str, headers: dict) -> str:
    data = await _post_json(session, EXA_ANSWER_URL, {"query": prompt}, headers)
    return " ".join(str(data.get("answer") or "").split())


async def _tavily_search(session, query: str, headers: dict, with_answer: bool = False) -> tuple[list[dict], str]:
    payload = {
        "query": query,
        "search_depth": "basic",
        "max_results": config.WEB_MAX_RESULTS,
        "include_answer": "advanced" if with_answer else False,
    }
    data = await _post_json(session, TAVILY_SEARCH_URL, payload, headers)
    hits = []
    for item in data.get("results") or ():
        hit = _hit_from(
            item.get("url"),
            item.get("title"),
            item.get("content") or item.get("raw_content") or "",
            "tavily",
        )
        if hit:
            hits.append(hit)
    answer = " ".join(str(data.get("answer") or "").split())
    return hits, answer


async def _run(backend: str, name: str, company: str, phone_variants) -> tuple[list[dict], str]:
    headers = {"Content-Type": "application/json"}
    if backend == "exa":
        headers["x-api-key"] = config.EXA_API_KEY
    else:
        headers["Authorization"] = f"Bearer {config.TAVILY_API_KEY}"

    queries = build_queries(name, company, phone_variants)

    async with aiohttp.ClientSession() as session:
        if backend == "exa":
            tasks = [_exa_search(session, query, headers) for query in queries]
            tasks.append(_exa_answer(session, answer_prompt(name, company), headers))
        else:
            tasks = [_tavily_search(session, query, headers) for query in queries]
            tasks.append(_tavily_search(session, answer_prompt(name, company), headers, with_answer=True))

        results = await asyncio.gather(*tasks, return_exceptions=True)

    hits: list[dict] = []
    seen: set[str] = set()
    answer = ""

    for result in results[:-1]:
        if isinstance(result, Exception):
            logger.warning("Web search query failed: %s", result)
            continue
        for hit in result:
            if hit["url"] in seen:
                continue
            seen.add(hit["url"])
            hits.append(hit)

    final = results[-1]
    if isinstance(final, Exception):
        logger.warning("Grounded answer unavailable: %s", final)
    elif backend == "exa":
        answer = final
    else:
        _answer_hits, answer = final

    return hits, answer


USER_AGENT = "Mozilla/5.0 (compatible; SkipTraceBot/1.0)"
EMAIL_PROBE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# A host that just timed out is not retried for a while, so a dead website
# cannot add latency to every later trace of the same company.
_SITE_FAILURES: dict[str, float] = {}
SITE_RETRY_SECONDS = 600


def _visible_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw or "")
    return " ".join(html.unescape(re.sub(r"(?s)<[^>]+>", " ", raw)).split())


def _site_hit(domain: str, pages: list[tuple[str, str]]) -> list[dict]:
    """Merge the fetched pages into one evidence hit."""
    social: set[str] = set()
    tel: set[str] = set()
    mail: set[str] = set()
    body: list[str] = []
    for _url, raw in pages:
        social.update(
            link.rstrip("\\/\"',.;:)")
            for link in re.findall(r"https?://[^\s\"'<>]+", raw)
            if any(
                host in link
                for host in (
                    "facebook.com",
                    "instagram.com",
                    "tiktok.com",
                    "twitter.com",
                    "x.com",
                    "linkedin.com",
                    "wa.me",
                    "youtube.com",
                )
            )
        )
        tel.update(re.findall(r'href=["\']?tel:([^"\'>]+)', raw, flags=re.I))
        mail.update(re.findall(r'href=["\']?mailto:([^"\'>]+)', raw, flags=re.I))
        body.append(_visible_text(raw))

    snippet = " ".join(
        [" ".join(sorted(tel)), " ".join(sorted(mail)), " ".join(sorted(social)), " ".join(body)]
    ).strip()
    if not snippet:
        return []

    return [
        {
            "site": "Company website",
            "url": pages[0][0],
            "title": domain,
            "snippet": snippet[: config.WEB_SITE_CHARS],
            "username": "",
            "source": "site",
        }
    ]


async def _gather_site(domain: str) -> list[dict]:
    """Homepage plus one contact page, trying the www and bare host."""
    timeout = aiohttp.ClientTimeout(total=config.WEB_SITE_TIMEOUT, connect=3)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for base in (f"https://www.{domain}", f"https://{domain}"):
            pages: list[tuple[str, str]] = []
            for path in ("", "/contact"):
                fetched = ""
                try:
                    async with session.get(
                        base + path, headers={"User-Agent": USER_AGENT}
                    ) as response:
                        if response.status == 200:
                            fetched = await response.text(errors="ignore")
                            pages.append((str(response.url), fetched))
                except Exception:  # noqa: BLE001 - one page failure is not fatal
                    continue
                # A homepage already carrying contact details is enough;
                # otherwise one contact page normally holds the mailto/address.
                if fetched and EMAIL_PROBE.search(fetched) and (
                    "tel:" in fetched.lower() or "mailto:" in fetched.lower()
                ):
                    break
            if pages:
                return _site_hit(domain, pages)
    return []


async def _fetch_company_site(hits, company: str, answer: str = "") -> list[dict]:
    """Fetch the company's own pages for address, phones and social links.

    The model answer names the domain; the company's pages are where the
    working phone lines, office address and Facebook page actually live.

    The whole fetch is capped, because a slow or unreachable host must never
    slow a trace down: it only means the company block reports fewer fields.
    """
    domain = extract.company_domain_from_hits(hits, company, answer)
    if not domain:
        return []
    if _SITE_FAILURES.get(domain, 0) > time.time():
        return []

    try:
        pages = await asyncio.wait_for(_gather_site(domain), timeout=config.WEB_SITE_TOTAL)
    except asyncio.TimeoutError:
        logger.info("Company site fetch timed out: %s", domain)
        pages = []
    except Exception:  # noqa: BLE001 - enrichment must never break a search
        logger.warning("Company site fetch failed: %s", domain, exc_info=True)
        pages = []

    if not pages:
        _SITE_FAILURES[domain] = time.time() + SITE_RETRY_SECONDS
    return pages


async def search_subject(name: str, company: str = "", phone_variants=()) -> tuple[list[dict], str]:
    """Search the open web for the subject; return (hits, grounded_answer).

    Never raises: a provider failure degrades to an empty result set so the
    caller can fall back to the curated checks or report an honest "nothing
    found".
    """
    backend = provider()
    if backend is None:
        return [], ""

    try:
        hits, answer = await asyncio.wait_for(
            _run(backend, name, company, phone_variants),
            timeout=config.WEB_SEARCH_OVERALL_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning("Web search exceeded %ss", config.WEB_SEARCH_OVERALL_TIMEOUT)
        return [], ""
    except Exception:  # noqa: BLE001 - a search failure must not crash the bot
        logger.warning("Web search failed", exc_info=True)
        return [], ""

    if company:
        hits = hits + await _fetch_company_site(hits, company, answer)
    return hits, answer
