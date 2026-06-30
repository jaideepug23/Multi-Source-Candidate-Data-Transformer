"""
ats_json_source.py — Extracts candidate data from an ATS JSON blob.

ATS systems use their own field naming that does NOT match our schema.
This extractor tries a wide range of known ATS field name patterns
and falls back gracefully when fields are absent or malformed.

Supports both single-object and array-of-objects JSON blobs.
"""

from __future__ import annotations
import json
import logging
from pathlib import Path
from typing import Any, Optional

from transformer.schema import (
    CandidateProfile, LocationEntry, LinksEntry,
    SkillEntry, ExperienceEntry, EducationEntry, ProvenanceEntry,
)
from transformer.normalizers import (
    normalize_email, normalize_phone, normalize_name,
    normalize_skill, normalize_country, normalize_date,
)

logger = logging.getLogger(__name__)
SOURCE_NAME = "ats_json"


# ─── ATS field path probes ────────────────────────────────────────────────────
# Each entry is a list of dot-paths to try in order; first non-null wins.

_FIELD_PROBES: dict[str, list[str]] = {
    "full_name": [
        "applicant.fullName", "applicant.name", "candidate.fullName",
        "candidate.name", "person.displayName", "person.name",
        "firstName+lastName",  # special: combine
        "name", "full_name", "fullName", "displayName",
    ],
    "email": [
        "applicant.email", "applicant.emailAddress", "candidate.email",
        "candidate.emailAddress", "contact.email", "person.email",
        "email", "emailAddress", "email_address",
    ],
    "phone": [
        "applicant.phone", "applicant.phoneNumber", "candidate.phone",
        "candidate.phoneNumber", "contact.phone", "person.phone",
        "phone", "phoneNumber", "phone_number", "mobile", "mobilePhone",
    ],
    "linkedin": [
        "applicant.linkedinUrl", "candidate.linkedinUrl", "social.linkedin",
        "links.linkedin", "linkedinUrl", "linkedin_url", "linkedin",
    ],
    "github": [
        "applicant.githubUrl", "candidate.githubUrl", "social.github",
        "links.github", "githubUrl", "github_url", "github",
    ],
    "headline": [
        "applicant.headline", "candidate.headline", "applicant.summary",
        "candidate.summary", "applicant.bio", "profile.summary",
        "headline", "summary", "bio", "about",
    ],
    "years_experience": [
        "applicant.yearsOfExperience", "candidate.yearsOfExperience",
        "yearsOfExperience", "years_of_experience", "experience_years",
        "totalExperience", "total_experience",
    ],
    "skills": [
        "applicant.skills", "candidate.skills", "profile.skills",
        "skills", "skillSet", "skill_set", "technologies", "techStack",
    ],
    "experience": [
        "applicant.workHistory", "candidate.workHistory",
        "applicant.experience", "candidate.experience",
        "workHistory", "work_history", "employmentHistory",
        "employment_history", "experience", "positions",
    ],
    "education": [
        "applicant.education", "candidate.education",
        "education", "educationHistory", "education_history", "schoolHistory",
    ],
    "location_city": [
        "applicant.location.city", "candidate.location.city",
        "location.city", "address.city", "city",
    ],
    "location_country": [
        "applicant.location.country", "candidate.location.country",
        "location.country", "address.country", "country",
    ],
    "location_region": [
        "applicant.location.state", "candidate.location.state",
        "location.state", "location.region", "address.state", "state", "region",
    ],
    "id": [
        "applicant.id", "candidate.id", "applicantId", "candidateId",
        "id", "_id",
    ],
    "first_name": ["applicant.firstName", "candidate.firstName", "firstName", "first_name"],
    "last_name": ["applicant.lastName", "candidate.lastName", "lastName", "last_name"],
}


def _get_nested(obj: Any, path: str) -> Any:
    """Traverse a dot-separated path into a nested dict. Returns None on failure."""
    if not isinstance(obj, dict):
        return None
    parts = path.split(".")
    current = obj
    for part in parts:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
        if current is None:
            return None
    return current


def _probe(obj: dict, paths: list[str]) -> Any:
    """Try each path probe in order, return first non-None non-empty value."""
    for path in paths:
        if path == "firstName+lastName":
            # Special combinator
            fn = _probe(obj, _FIELD_PROBES["first_name"])
            ln = _probe(obj, _FIELD_PROBES["last_name"])
            if fn or ln:
                return f"{fn or ''} {ln or ''}".strip()
            continue
        val = _get_nested(obj, path)
        if val is not None and val != "" and val != []:
            return val
    return None


def _coerce_str(val: Any) -> Optional[str]:
    if val is None:
        return None
    if isinstance(val, str):
        return val.strip() or None
    if isinstance(val, (int, float)):
        return str(val)
    return None


def _parse_experience_list(raw: Any) -> list[ExperienceEntry]:
    """Parse various ATS experience list shapes."""
    if not isinstance(raw, list):
        return []
    entries: list[ExperienceEntry] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        company = (
            _coerce_str(item.get("company") or item.get("employer") or
                        item.get("organization") or item.get("companyName"))
        )
        title = (
            _coerce_str(item.get("title") or item.get("jobTitle") or
                        item.get("position") or item.get("role"))
        )
        if not company and not title:
            continue
        start_raw = _coerce_str(
            item.get("startDate") or item.get("start_date") or
            item.get("from") or item.get("startYear")
        )
        end_raw = _coerce_str(
            item.get("endDate") or item.get("end_date") or
            item.get("to") or item.get("endYear")
        )
        summary = _coerce_str(item.get("description") or item.get("summary") or item.get("responsibilities"))

        entries.append(ExperienceEntry(
            company=company or "",
            title=title or "",
            start=normalize_date(start_raw) if start_raw else None,
            end=normalize_date(end_raw) if end_raw else None,
            summary=summary,
        ))
    return entries


def _parse_education_list(raw: Any) -> list[EducationEntry]:
    """Parse various ATS education list shapes."""
    if not isinstance(raw, list):
        return []
    entries: list[EducationEntry] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        institution = _coerce_str(
            item.get("institution") or item.get("school") or
            item.get("university") or item.get("college") or item.get("name")
        )
        if not institution:
            continue
        degree = _coerce_str(item.get("degree") or item.get("qualification"))
        field = _coerce_str(item.get("field") or item.get("fieldOfStudy") or item.get("major"))
        end_year_raw = item.get("endYear") or item.get("graduationYear") or item.get("end_year")
        end_year: Optional[int] = None
        try:
            if end_year_raw:
                end_year = int(str(end_year_raw)[:4])
        except (ValueError, TypeError):
            pass
        entries.append(EducationEntry(
            institution=institution,
            degree=degree,
            field=field,
            end_year=end_year,
        ))
    return entries


def _parse_skills_raw(raw: Any) -> list[str]:
    """ATS skills can be strings, lists of strings, or lists of dicts."""
    if not raw:
        return []
    if isinstance(raw, str):
        for sep in [",", ";", "|"]:
            if sep in raw:
                return [s.strip() for s in raw.split(sep) if s.strip()]
        return [raw.strip()] if raw.strip() else []
    if isinstance(raw, list):
        result: list[str] = []
        for item in raw:
            if isinstance(item, str) and item.strip():
                result.append(item.strip())
            elif isinstance(item, dict):
                name = _coerce_str(item.get("name") or item.get("skill") or item.get("label"))
                if name:
                    result.append(name)
        return result
    return []


def extract(source_path: str) -> list[CandidateProfile]:
    """
    Read an ATS JSON blob and return CandidateProfiles.
    Handles both single object and array of objects.
    """
    path = Path(source_path)
    if not path.exists():
        logger.warning(f"[ats_json] File not found: {source_path}")
        return []

    try:
        content = path.read_text(encoding="utf-8-sig", errors="replace")
        data = json.loads(content)
    except json.JSONDecodeError as e:
        logger.error(f"[ats_json] Invalid JSON in {source_path}: {e}")
        return []
    except Exception as e:
        logger.error(f"[ats_json] Cannot read {source_path}: {e}")
        return []

    # Normalize to list
    if isinstance(data, dict):
        # Maybe wrapped: {"candidates": [...]}
        for key in ("candidates", "applicants", "results", "data", "records"):
            if key in data and isinstance(data[key], list):
                data = data[key]
                break
        else:
            data = [data]
    elif not isinstance(data, list):
        logger.error(f"[ats_json] Unexpected top-level type: {type(data)}")
        return []

    profiles: list[CandidateProfile] = []
    for i, obj in enumerate(data):
        if not isinstance(obj, dict):
            continue
        try:
            profile = _build_profile(obj, i, source_path)
            if profile:
                profiles.append(profile)
        except Exception as e:
            logger.warning(f"[ats_json] Skipping record {i}: {e}")

    logger.info(f"[ats_json] Extracted {len(profiles)} profiles from {source_path}")
    return profiles


def _build_profile(obj: dict, idx: int, source_path: str) -> Optional[CandidateProfile]:
    provenance: list[ProvenanceEntry] = []

    def record(field: str, method: str = "direct") -> None:
        provenance.append(ProvenanceEntry(field=field, source=SOURCE_NAME, method=method))

    # ID
    cid_raw = _probe(obj, _FIELD_PROBES["id"])
    cid = _coerce_str(cid_raw) or f"{Path(source_path).stem}_ats_{idx}"

    # Name
    full_name: Optional[str] = None
    name_raw = _probe(obj, _FIELD_PROBES["full_name"])
    if name_raw:
        full_name = normalize_name(_coerce_str(name_raw) or "")
        if full_name:
            record("full_name")

    # Email
    emails: list[str] = []
    email_raw = _probe(obj, _FIELD_PROBES["email"])
    if email_raw:
        # Could be list or string
        raw_list = email_raw if isinstance(email_raw, list) else [email_raw]
        for r in raw_list:
            em = normalize_email(_coerce_str(r) or "")
            if em and em not in emails:
                emails.append(em)
        if emails:
            record("emails")

    # Phone
    phones: list[str] = []
    phone_raw = _probe(obj, _FIELD_PROBES["phone"])
    if phone_raw:
        raw_list = phone_raw if isinstance(phone_raw, list) else [phone_raw]
        for r in raw_list:
            ph = normalize_phone(_coerce_str(r) or "")
            if ph and ph not in phones:
                phones.append(ph)
        if phones:
            record("phones", "normalized")

    # Links
    links: Optional[LinksEntry] = None
    linkedin = _coerce_str(_probe(obj, _FIELD_PROBES["linkedin"]))
    github = _coerce_str(_probe(obj, _FIELD_PROBES["github"]))
    if linkedin or github:
        links = LinksEntry(linkedin=linkedin, github=github)
        record("links")

    # Headline
    headline: Optional[str] = None
    headline_raw = _probe(obj, _FIELD_PROBES["headline"])
    if headline_raw:
        headline = _coerce_str(headline_raw)
        if headline:
            record("headline")

    # Years experience
    years_experience: Optional[float] = None
    years_raw = _probe(obj, _FIELD_PROBES["years_experience"])
    if years_raw is not None:
        try:
            years_experience = float(years_raw)
            record("years_experience")
        except (ValueError, TypeError):
            pass

    # Skills
    skills: list[SkillEntry] = []
    skills_raw = _probe(obj, _FIELD_PROBES["skills"])
    raw_skill_names = _parse_skills_raw(skills_raw)
    for rs in raw_skill_names:
        canonical = normalize_skill(rs)
        if canonical:
            skills.append(SkillEntry(name=canonical, confidence=0.85, sources=[SOURCE_NAME]))
    if skills:
        record("skills", "normalized")

    # Experience
    experience: list[ExperienceEntry] = []
    exp_raw = _probe(obj, _FIELD_PROBES["experience"])
    experience = _parse_experience_list(exp_raw)
    if experience:
        record("experience", "normalized")

    # Education
    education: list[EducationEntry] = []
    edu_raw = _probe(obj, _FIELD_PROBES["education"])
    education = _parse_education_list(edu_raw)
    if education:
        record("education", "normalized")

    # Location
    location: Optional[LocationEntry] = None
    city = _coerce_str(_probe(obj, _FIELD_PROBES["location_city"]))
    country_raw = _coerce_str(_probe(obj, _FIELD_PROBES["location_country"]))
    region = _coerce_str(_probe(obj, _FIELD_PROBES["location_region"]))
    country = normalize_country(country_raw) if country_raw else None
    if city or country or region:
        location = LocationEntry(city=city, region=region, country=country or country_raw)
        record("location", "normalized")

    # Confidence
    filled = sum([
        bool(full_name), bool(emails), bool(phones),
        bool(location), bool(skills), bool(experience),
    ])
    confidence = min(1.0, filled / 6.0)

    return CandidateProfile(
        candidate_id=cid,
        full_name=full_name,
        emails=emails,
        phones=phones,
        location=location,
        links=links,
        headline=headline,
        years_experience=years_experience,
        skills=skills,
        experience=experience,
        education=education,
        provenance=provenance,
        overall_confidence=confidence,
    )