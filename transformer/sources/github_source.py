"""
github_source.py — Extracts candidate data from a public GitHub profile
via the real GitHub REST API.

Unstructured source: GitHub does not know about our schema at all. We infer
headline/skills/experience-adjacent signal from profile fields, bio, and repos.

Input: a GitHub profile URL or bare username, e.g.
    "https://github.com/octocat"  or  "octocat"

Network behavior (never raises, always returns a list — possibly empty):
  - 404 (user not found)         → log + return []
  - 403 rate-limited              → log + return [] (checks X-RateLimit-Remaining)
  - timeout / connection error    → log + return []
  - malformed/unexpected payload  → log + skip the offending field only
"""

from __future__ import annotations
import logging
import re
from pathlib import Path
from typing import Any, Optional

import requests

from transformer.schema import (
    CandidateProfile, LinksEntry, SkillEntry, ExperienceEntry, ProvenanceEntry,
)
from transformer.normalizers import normalize_name, normalize_skill, normalize_date

logger = logging.getLogger(__name__)
SOURCE_NAME = "github"

API_BASE = "https://api.github.com"
REQUEST_TIMEOUT_SECONDS = 10

# GitHub profile URL shapes we accept:
#   https://github.com/<user>
#   https://www.github.com/<user>
#   github.com/<user>
#   <user>   (bare username, last resort)
_URL_RE = re.compile(r"github\.com/([A-Za-z0-9\-]+)/?$")


def _extract_username(raw: str) -> Optional[str]:
    """Pull a GitHub username out of a URL or bare string."""
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip()
    m = _URL_RE.search(s)
    if m:
        return m.group(1)
    # Bare username — only accept simple username-looking tokens, no spaces/slashes
    if re.match(r"^[A-Za-z0-9\-]+$", s):
        return s
    return None


def _get(url: str) -> tuple[Optional[Any], Optional[int]]:
    """
    GET a GitHub API URL. Returns (json_or_none, status_code_or_none).
    Never raises.
    """
    try:
        resp = requests.get(
            url,
            headers={"Accept": "application/vnd.github+json"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.exceptions.Timeout:
        logger.error(f"[github] Timeout calling {url}")
        return None, None
    except requests.exceptions.RequestException as e:
        logger.error(f"[github] Network error calling {url}: {e}")
        return None, None

    if resp.status_code == 404:
        logger.warning(f"[github] Not found: {url}")
        return None, 404

    if resp.status_code == 403:
        remaining = resp.headers.get("X-RateLimit-Remaining")
        if remaining == "0":
            logger.warning(f"[github] Rate-limited (X-RateLimit-Remaining=0) on {url}")
        else:
            logger.warning(f"[github] 403 Forbidden on {url}")
        return None, 403

    if resp.status_code != 200:
        logger.warning(f"[github] Unexpected status {resp.status_code} on {url}")
        return None, resp.status_code

    try:
        return resp.json(), 200
    except ValueError:
        logger.error(f"[github] Non-JSON response from {url}")
        return None, 200


def extract(source_path: str) -> list[CandidateProfile]:
    """
    source_path is a GitHub profile URL or bare username (not a file on disk).
    Returns a single-element list with a partial CandidateProfile, or [] on
    any failure (not found, rate-limited, malformed, network error).
    """
    username = _extract_username(source_path)
    if not username:
        logger.warning(f"[github] Could not parse a username from: {source_path!r}")
        return []

    user_data, status = _get(f"{API_BASE}/users/{username}")
    if user_data is None:
        # 404 / 403 / network error / bad JSON — degrade to empty, never crash
        return []

    profile = _build_profile(username, user_data)
    if profile is None:
        return []

    # Repos are best-effort enrichment; failure here must not lose the profile
    # we already built from the user endpoint.
    repos_data, repos_status = _get(
        f"{API_BASE}/users/{username}/repos?per_page=100&sort=updated"
    )
    if isinstance(repos_data, list):
        _enrich_with_repos(profile, repos_data)
    else:
        logger.info(f"[github] No repo enrichment for {username} (status={repos_status})")

    logger.info(f"[github] Extracted profile for {username}")
    return [profile]


def _build_profile(username: str, user_data: dict) -> Optional[CandidateProfile]:
    if not isinstance(user_data, dict):
        return None

    provenance: list[ProvenanceEntry] = []

    def record(field: str, method: str = "direct") -> None:
        provenance.append(ProvenanceEntry(field=field, source=SOURCE_NAME, method=method))

    cid = f"github_{username}"

    # Name: GitHub "name" can be null/empty for many users — fall back to login.
    raw_name = user_data.get("name")
    full_name: Optional[str] = None
    if isinstance(raw_name, str) and raw_name.strip():
        full_name = normalize_name(raw_name)
        if full_name:
            record("full_name")
    # We deliberately do NOT invent a name from the login/handle — an
    # account handle like "octocat99" is not a real name and we must not
    # fabricate one (constraint: unknown → null, never invented).

    # Email: GitHub only exposes this if the user has made it public.
    emails: list[str] = []
    raw_email = user_data.get("email")
    if isinstance(raw_email, str) and raw_email.strip():
        emails.append(raw_email.strip().lower())
        record("emails")

    # Headline: bio doubles as headline for an unstructured source.
    headline: Optional[str] = None
    raw_bio = user_data.get("bio")
    if isinstance(raw_bio, str) and raw_bio.strip():
        headline = raw_bio.strip()
        record("headline", "direct")

    # Links
    links = LinksEntry(github=user_data.get("html_url") or f"https://github.com/{username}")
    blog = user_data.get("blog")
    if isinstance(blog, str) and blog.strip():
        portfolio = blog.strip()
        if not portfolio.startswith(("http://", "https://")):
            portfolio = f"https://{portfolio}"
        links.portfolio = portfolio
    record("links", "direct")

    # Location: GitHub gives free-text location, e.g. "San Francisco, CA".
    # We deliberately leave structured location parsing to the merger/CSV
    # source; GitHub's free-text field is too unreliable to split into
    # city/region/country with confidence, so we surface it only via
    # headline-adjacent context rather than fabricating a LocationEntry.
    # (See README "descoped" section.)

    # Company can hint at an experience entry, though GitHub's "company"
    # field is self-reported free text and often stale.
    experience: list[ExperienceEntry] = []
    raw_company = user_data.get("company")
    if isinstance(raw_company, str) and raw_company.strip():
        company = raw_company.strip().lstrip("@").strip()
        if company:
            experience.append(ExperienceEntry(company=company, title="", summary=None))
            record("experience", "inferred")

    return CandidateProfile(
        candidate_id=cid,
        full_name=full_name,
        emails=emails,
        phones=[],
        location=None,
        links=links,
        headline=headline,
        years_experience=None,
        skills=[],
        experience=experience,
        education=[],
        provenance=provenance,
        overall_confidence=0.0,  # finalized after repo enrichment, see _finalize_confidence
    )


# Best-effort mapping from GitHub language names to our skill vocabulary.
# Most already match normalize_skill's alias table or pass through title-cased.
def _enrich_with_repos(profile: CandidateProfile, repos: list[Any]) -> None:
    """Mutate profile in place: infer skills from repo languages."""
    lang_counts: dict[str, int] = {}
    for repo in repos:
        if not isinstance(repo, dict):
            continue
        if repo.get("fork"):
            continue  # forks don't reflect the candidate's own skill, just interest
        lang = repo.get("language")
        if isinstance(lang, str) and lang.strip():
            lang_counts[lang.strip()] = lang_counts.get(lang.strip(), 0) + 1

    if not lang_counts:
        _finalize_confidence(profile, had_repos=False)
        return

    # Confidence scales with how many repos use that language relative to
    # the candidate's total (non-fork) repo count — more evidence, more trust.
    total = sum(lang_counts.values())
    skills: list[SkillEntry] = []
    seen_canonical: set[str] = set()
    for lang, count in sorted(lang_counts.items(), key=lambda kv: -kv[1]):
        canonical = normalize_skill(lang)
        if not canonical or canonical in seen_canonical:
            continue
        seen_canonical.add(canonical)
        confidence = round(min(0.95, 0.5 + 0.45 * (count / total)), 2)
        skills.append(SkillEntry(name=canonical, confidence=confidence, sources=[SOURCE_NAME]))

    if skills:
        profile.skills = skills
        profile.provenance.append(
            ProvenanceEntry(field="skills", source=SOURCE_NAME, method="inferred")
        )

    _finalize_confidence(profile, had_repos=True)


def _finalize_confidence(profile: CandidateProfile, had_repos: bool) -> None:
    """
    Per-source confidence heuristic, mirroring csv_source/ats_json_source's
    filled-field ratio but over the fields GitHub can plausibly populate
    (name, email, headline, links, skills, experience) — phones/location/
    education are structurally out of scope for this source.
    """
    filled = sum([
        bool(profile.full_name),
        bool(profile.emails),
        bool(profile.headline),
        bool(profile.skills),
        bool(profile.experience),
    ])
    base = min(1.0, filled / 5.0)
    # Inferred skill signal (from repo languages) is weaker evidence than a
    # directly-stated skill, so cap overall confidence a bit when that's
    # most of what we have.
    if had_repos and profile.skills and filled <= 2:
        base = min(base, 0.6)
    profile.overall_confidence = base