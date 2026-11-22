"""Curated public-profile checks over plain HTTP.

This is the fallback used when no web-search key is configured, or when
``SEARCH_MODE=http`` forces it: a handful of public profile URLs are requested
directly, and a page only counts when its own title names the handle.

Maigret was removed from this project on purpose. Walking its bundled
~3,500-site database per handle meant ~89,000 requests for a full trace, which
measured at 4-5 hours, and it pulled in Flask plus a reporting stack that made
the bot far too heavy for a small host. ``websearch.py`` (Exa or Tavily) is the
search engine; nothing here runs during a normal trace.

Only public profile pages are touched; no login-walled endpoints are used.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re

import aiohttp

import config

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Curated fallback sites only. Each URL is a public profile page.
FALLBACK_SITES = (
    {
        "site": "TikTok",
        "url": "https://www.tiktok.com/@{username}",
        "tags": ["social"],
    },
    {
        "site": "Facebook",
        "url": "https://www.facebook.com/{username}",
        "tags": ["social"],
    },
    {
        "site": "LinkedIn",
        "url": "https://www.linkedin.com/in/{username}",
        "tags": ["social"],
    },
    {
        "site": "GitHub",
        "url": "https://github.com/{username}",
        "tags": ["social", "misc"],
    },
    {
        "site": "Nairaland",
        "url": "https://www.nairaland.com/{username}",
        "tags": ["forum"],
    },
)

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def _page_title(body: str) -> str:
    match = _TITLE_RE.search(body or "")
    if not match:
        return ""
    return " ".join(html.unescape(match.group(1)).split())


def _title_confirms_handle(body: str, username: str) -> bool:
    """Only count a page whose title names the requested handle.

    Logged-out Instagram serves a login page for every handle, and login walls
    are not profile evidence. A title containing the handle is a crude but
    reliable positive signal for GitHub, TikTok and similar sites.
    """
    title = _page_title(body).lower()
    return bool(title) and username.lower() in title


async def _check_one(username: str) -> dict:
    """Check the curated public sites directly over HTTP."""
    hits: dict[str, dict] = {}
    timeout = aiohttp.ClientTimeout(total=config.PROFILE_TIMEOUT)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.8",
    }

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        for site in FALLBACK_SITES:
            url = site["url"].format(username=username)
            try:
                async with session.get(url, allow_redirects=True) as response:
                    if response.status != 200:
                        continue
                    body = await response.text(errors="ignore")
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.debug("Fallback lookup failed for %s: %s", url, exc)
                continue
            except Exception as exc:  # noqa: BLE001 - public sites are flaky
                logger.debug("Fallback lookup error for %s: %s", url, exc)
                continue

            if not _title_confirms_handle(body, username):
                logger.debug("Skipping %s: no profile title for %s", url, username)
                continue

            hits[site["site"]] = {
                "site": site["site"],
                "url": url,
                "username": username,
                "tags": list(site["tags"]),
                "source": "http",
            }

    return hits


async def search_usernames(usernames) -> list[dict]:
    """Check a list of usernames in small batches and return flat hit dicts."""
    unique: list[str] = []
    for username in usernames or ():
        cleaned = str(username or "").strip().lower()
        if cleaned and cleaned not in unique:
            unique.append(cleaned)

    hits: list[dict] = []
    seen_urls: set[str] = set()
    batch_size = max(1, config.PROFILE_CONCURRENCY)

    for start in range(0, len(unique), batch_size):
        batch = unique[start : start + batch_size]
        results = await asyncio.gather(
            *(_check_one(username) for username in batch), return_exceptions=True
        )

        for username, result in zip(batch, results):
            if isinstance(result, Exception):
                logger.warning("Lookup failed for %s: %s", username, result)
                continue
            for hit in result.values():
                url = hit.get("url")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                hits.append(hit)

        if start + batch_size < len(unique):
            await asyncio.sleep(config.BATCH_SLEEP_SECONDS)

    return hits
