"""Deterministic confidence scoring for candidate profiles.

No model calls: every point comes from an explicit, auditable rule and
each applied rule appends a human-readable reason.
"""

from __future__ import annotations

import re

import jellyfish

import config


def _tokens(value) -> list[str]:
    """Split names and handles into comparable words.

    Handles use dots/underscores as separators (chinedu.okafor) and often pack
    both names into one token (chineduokafor), so whitespace alone is not
    enough to compare a full name against a handle.
    """
    return [
        token
        for token in re.split(r"[\s._\-@]+", str(value or "").strip().lower())
        if token
    ]


def _name_similarity(input_name, profile_name) -> float:
    """Compare names against a display name or handle, order-independently."""
    left = _tokens(input_name)
    right = _tokens(profile_name)
    if not left or not right:
        return 0.0

    # A single-token handle may contain every name token (chineduokafor).
    if len(right) == 1 and len(left) > 1:
        packed = right[0]
        covered = sum(
            1 for token in left if len(token) >= 3 and token in packed
        )
        if covered:
            return covered / len(left)

    # With a single input token (a collector typed only a first name), a
    # handle like chinedu.okafor should still match strongly.
    if len(left) == 1:
        return max(
            jellyfish.jaro_winkler_similarity(left[0], token) for token in right
        )

    total = 0.0
    for token in left:
        total += max(
            jellyfish.jaro_winkler_similarity(token, other) for other in right
        )
    return total / max(len(left), len(right))


def _phone_in_text(phone_variants, text):
    """Return the first phone variant (digits) found in text, else None."""
    text_digits = re.sub(r"\D", "", str(text or ""))
    if not text_digits:
        return None

    for variant in phone_variants or ():
        digits = re.sub(r"\D", "", str(variant or ""))
        if len(digits) >= 7 and digits in text_digits:
            return variant
    return None


def _normalize_gender(value) -> str:
    value = str(value or "").strip().lower()
    if value in {"m", "male", "man"}:
        return "male"
    if value in {"f", "female", "woman"}:
        return "female"
    return ""


def _profile_is_walled(profile: dict, hit: dict) -> bool:
    if profile.get("login_wall") or profile.get("private"):
        return True
    markers = ("login", "sign in", "sign up", "private account", "log in")
    haystack = " ".join(
        str(profile.get(key) or "").lower()
        for key in ("bio", "location", "display_name")
    )
    return any(marker in haystack for marker in markers)


def _location_points(profile_location: str, region: str) -> tuple[int, str]:
    location = str(profile_location or "").strip().lower()
    region = str(region or "").strip().lower()
    if not location or not region:
        return 0, ""

    if region in location or location in region:
        return 10, f"Location matches ({profile_location})"

    region_tokens = [token for token in re.split(r"\W+", region) if len(token) > 3]
    if any(token in location for token in region_tokens):
        return 3, f"Location partially matches ({profile_location})"
    return 0, ""


def score_hit(hit: dict, profile: dict, input_ctx: dict) -> dict:
    """Score one hit from 0-100 and explain every signal that fired."""
    hit = hit or {}
    profile = profile or {}
    input_ctx = input_ctx or {}

    score = 0.0
    reasons: list[str] = []

    # A claimed public profile is a weak lead on its own. Without this an
    # exact name handle scored 30 and was always discarded by the 40 threshold.
    score += 10
    reasons.append("Public profile exists")

    display_name = profile.get("display_name") or hit.get("username") or ""
    similarity = _name_similarity(input_ctx.get("name"), display_name)
    if similarity > 0:
        score += similarity * 30
        reasons.append(f"Name similarity {int(round(similarity * 100))}%")

    phone_variants = input_ctx.get("phone_variants") or ()
    username_digits = re.sub(r"\D", "", str(hit.get("username") or ""))
    phone_handle = ""
    if len(username_digits) >= 7:
        for variant in phone_variants:
            if re.sub(r"\D", "", str(variant or "")) == username_digits:
                phone_handle = variant
                break

    if phone_handle:
        score += 35
        reasons.append("Username is the debtor's phone number")
    else:
        haystack = " ".join(
            str(part or "")
            for part in (
                profile.get("bio"),
                profile.get("display_name"),
                hit.get("username"),
            )
        )
        matched_phone = _phone_in_text(phone_variants, haystack)
        if matched_phone:
            score += 25
            reasons.append("Phone number appears in profile text")

    wanted_gender = _normalize_gender(input_ctx.get("gender"))
    profile_gender = _normalize_gender(profile.get("gender"))
    if wanted_gender and profile_gender and wanted_gender == profile_gender:
        score += 10
        reasons.append("Gender consistent with debtor details")

    location_points, location_reason = _location_points(
        profile.get("location"), (input_ctx.get("meta") or {}).get("region")
    )
    if location_points:
        score += location_points
        reasons.append(location_reason)

    company = str(input_ctx.get("company") or "").strip().lower()
    bio = str(profile.get("bio") or "").lower()
    if company and company in bio:
        score += 10
        reasons.append(f"Company mentioned in bio ({input_ctx.get('company')})")

    username = str(hit.get("username") or "").strip().lower()
    if username and username in (input_ctx.get("cross_platform_usernames") or set()):
        score += 10
        reasons.append("Same username found on 2+ platforms")

    if display_name and profile.get("bio"):
        score += 5
        reasons.append("Profile has both a display name and a bio")

    if _profile_is_walled(profile, hit):
        score -= 15
        reasons.append("Login wall or private profile (lower confidence)")

    score = int(round(max(0.0, min(100.0, score))))
    return {"score": score, "reasons": reasons}


def rank_hits(hits, profiles_by_url, input_ctx) -> list[dict]:
    """Score and sort hits, dropping anything under the report threshold."""
    counts: dict[str, int] = {}
    for hit in hits or ():
        username = str(hit.get("username") or "").strip().lower()
        if username:
            counts[username] = counts.get(username, 0) + 1

    context = dict(input_ctx or {})
    context["cross_platform_usernames"] = {
        username for username, count in counts.items() if count >= 2
    }

    ranked: list[dict] = []
    for hit in hits or ():
        profile = (profiles_by_url or {}).get(hit.get("url"), {})
        result = score_hit(hit, profile, context)
        if result["score"] < config.MIN_SCORE_TO_REPORT:
            continue
        item = dict(hit)
        item.update(result)
        ranked.append(item)

    ranked.sort(key=lambda item: (-item["score"], item.get("site", ""), item.get("url", "")))
    return ranked
