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
import base64
import logging
import re
from pathlib import Path
from typing import Any, Optional

import requests

from transformer.schema import (
    CandidateProfile, LinksEntry, LocationEntry, SkillEntry, ExperienceEntry, ProvenanceEntry,
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

    # Social accounts (LinkedIn, Twitter/X, Mastodon, etc.) are a separate
    # endpoint from the main user profile — GitHub profile pages show these
    # under "Social accounts" in the sidebar, but /users/{username} does not
    # include them. Best-effort enrichment; failure here must not lose the
    # profile we already built.
    social_data, social_status = _get(f"{API_BASE}/users/{username}/social_accounts")
    if isinstance(social_data, list):
        _enrich_with_social_accounts(profile, social_data)
    else:
        logger.info(f"[github] No social account enrichment for {username} (status={social_status})")

    # Profile README (the special repo named exactly {username}/{username},
    # which GitHub renders on the profile overview page) often lists a much
    # richer, self-reported skill set than repo languages alone reveal —
    # e.g. frameworks/libraries/tools that never show up as a repo's
    # primary language (Django, Firebase, React Native, TensorFlow, etc.).
    # Best-effort enrichment; many users have no profile README at all.
    readme_data, readme_status = _get(f"{API_BASE}/repos/{username}/{username}/readme")
    if isinstance(readme_data, dict):
        _enrich_with_profile_readme(profile, readme_data)
    else:
        logger.info(f"[github] No profile README for {username} (status={readme_status})")

    # Confidence is finalized once, after every enrichment step has had a
    # chance to populate skills/experience/etc. — finalizing earlier (e.g.
    # right after repo enrichment) would understate confidence for profiles
    # whose skill signal comes mainly from the README rather than repo
    # languages, or would be skipped entirely if the repos call failed.
    _finalize_confidence(profile, had_repos=isinstance(repos_data, list) and len(repos_data) > 0)

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

    # Location: GitHub gives free-text location, e.g. "San Francisco, CA" or
    # "Bengaluru, India". We surface it as a best-effort city string only —
    # GitHub's free-text field is too unreliable to confidently split into
    # city/region/country, so we don't try to parse those out here and
    # leave that finer-grained parsing to structured sources (CSV/ATS).
    location: Optional[LocationEntry] = None
    raw_location = user_data.get("location")
    if isinstance(raw_location, str) and raw_location.strip():
        location = LocationEntry(city=raw_location.strip())
        record("location", "inferred")

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
        location=location,
        links=links,
        headline=headline,
        years_experience=None,
        skills=[],
        experience=experience,
        education=[],
        provenance=provenance,
        overall_confidence=0.0,  # finalized after repo enrichment, see _finalize_confidence
    )


def _enrich_with_profile_readme(profile: CandidateProfile, readme_data: dict) -> None:
    """
    Mutate profile in place: decode the profile README and pull out
    skill-like tokens, merging them in with (not replacing) whatever
    repo-language-derived skills _enrich_with_repos already found.

    Two patterns cover the vast majority of real profile READMEs:
      1. Inline code spans used as skill "chips", e.g.:
           Programming Languages: `Python` `Java` `Swift` `Kotlin`
         (Markdown backtick spans, the most common manual-list style)
      2. shields.io / img.shields.io badge images, e.g.:
           ![Python](https://img.shields.io/badge/Python-3776AB?...)
         where the badge label (first segment of the badge path, or the
         alt text) is the skill name.

    We deliberately do NOT try to parse arbitrary prose for skill mentions
    (too unreliable, too easy to invent false positives) — only these two
    structured, intentional "I am listing my skills here" patterns.
    """
    content_b64 = readme_data.get("content")
    encoding = readme_data.get("encoding")
    if not content_b64 or encoding != "base64":
        return

    try:
        raw_bytes = base64.b64decode(content_b64)
        text = raw_bytes.decode("utf-8", errors="replace")
    except Exception as e:
        logger.warning(f"[github] Could not decode profile README: {e}")
        return

    found_tokens: list[str] = []

    # Pattern 1: inline code spans, e.g. `Python` `Django` `Docker`
    # Skip spans that are clearly not skill names: file paths, commands,
    # very long phrases, or pure punctuation/numbers.
    for m in re.finditer(r"`([^`\n]{2,30})`", text):
        token = m.group(1).strip()
        if _looks_like_skill_token(token):
            found_tokens.append(token)

    # Pattern 2: shields.io badges — the label is usually the alt text
    # (between the first [ and ]) or, failing that, the first path segment
    # after "badge/".
    for m in re.finditer(r"!\[([^\]]{1,40})\]\([^)]*shields\.io[^)]*\)", text, re.IGNORECASE):
        alt_text = m.group(1).strip()
        if _looks_like_skill_token(alt_text):
            found_tokens.append(alt_text)
    for m in re.finditer(r"shields\.io/badge/([A-Za-z0-9_.\-+]{2,30})", text, re.IGNORECASE):
        label = m.group(1).replace("_", " ").replace("-", " ").strip()
        if _looks_like_skill_token(label):
            found_tokens.append(label)

    if not found_tokens:
        return

    canonical_existing = {s.name for s in profile.skills}
    added_any = False
    for raw in found_tokens:
        canonical = normalize_skill(raw)
        if not canonical or canonical in canonical_existing:
            continue
        canonical_existing.add(canonical)
        # Self-reported in a README is direct evidence (the candidate
        # explicitly listed it), so it gets a solid confidence — similar
        # tier to a resume/CSV self-report, slightly below a verified
        # repo-language signal isn't really comparable since this is
        # intentional self-disclosure, not inferred from usage.
        profile.skills.append(SkillEntry(name=canonical, confidence=0.8, sources=[SOURCE_NAME]))
        added_any = True

    if added_any:
        profile.provenance.append(
            ProvenanceEntry(field="skills", source=SOURCE_NAME, method="direct")
        )


_SKILL_TOKEN_EXCLUDE_RE = re.compile(
    r"^(https?://|www\.|/|\.\.|[\d.]+$|[#$%@*_=\-]+$)", re.IGNORECASE
)


def _looks_like_skill_token(token: str) -> bool:
    """Filter out obviously-not-a-skill matches from README parsing: URLs,
    file paths, shell commands, numbers, pure punctuation, badge color hex
    codes, and anything containing whitespace-heavy prose."""
    if not token or len(token) < 2 or len(token) > 30:
        return False
    if _SKILL_TOKEN_EXCLUDE_RE.match(token):
        return False
    if any(c in token for c in ("/", "\\", "{", "}", "<", ">", "=")):
        return False
    word_count = len(token.split())
    if word_count > 3:
        return False  # likely a sentence fragment, not a skill name
    # Badge color hex codes look like "3776AB" or "000000"
    if re.match(r"^[0-9A-Fa-f]{6}$", token):
        return False
    return True


def _enrich_with_social_accounts(profile: CandidateProfile, social_accounts: list[Any]) -> None:
    """
    Mutate profile in place: pull a LinkedIn URL (and treat any other
    non-GitHub link as a portfolio fallback if we don't already have one)
    from GitHub's social accounts list.
    Each entry looks like: {"provider": "linkedin", "url": "https://www.linkedin.com/in/..."}
    """
    if not profile.links:
        return

    for entry in social_accounts:
        if not isinstance(entry, dict):
            continue
        provider = entry.get("provider")
        url = entry.get("url")
        if not isinstance(url, str) or not url.strip():
            continue
        url = url.strip()

        if provider == "linkedin" or "linkedin.com" in url.lower():
            if not profile.links.linkedin:
                profile.links.linkedin = url
                profile.provenance.append(
                    ProvenanceEntry(field="links", source=SOURCE_NAME, method="direct")
                )
            break  # only need the first linkedin match


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


def _finalize_confidence(profile: CandidateProfile, had_repos: bool) -> None:
    """
    Per-source confidence heuristic, mirroring csv_source/ats_json_source's
    filled-field ratio but over the fields GitHub can plausibly populate
    (name, email, headline, location, links, skills, experience) — phones/
    education are structurally out of scope for this source.
    """
    filled = sum([
        bool(profile.full_name),
        bool(profile.emails),
        bool(profile.headline),
        bool(profile.location),
        bool(profile.skills),
        bool(profile.experience),
    ])
    base = min(1.0, filled / 6.0)
    # Inferred skill signal (from repo languages) is weaker evidence than a
    # directly-stated skill, so cap overall confidence a bit when that's
    # most of what we have.
    if had_repos and profile.skills and filled <= 2:
        base = min(base, 0.6)
    profile.overall_confidence = base