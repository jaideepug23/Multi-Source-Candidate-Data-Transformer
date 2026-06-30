"""
csv_source.py — Extracts candidate data from a Recruiter CSV export.

Expected columns (flexible — we map whatever is present):
  name, email, phone, current_company, title, location, linkedin, github,
  skills, years_experience, education, notes

Missing columns and malformed values are handled gracefully.
"""

from __future__ import annotations
import csv
import io
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

SOURCE_NAME = "csv"

# Flexible column name mapping → canonical field
_COL_MAP: dict[str, str] = {
    # name variants
    "name": "full_name", "full_name": "full_name", "candidate_name": "full_name",
    "fullname": "full_name", "candidate": "full_name",
    # email
    "email": "email", "email_address": "email", "e-mail": "email",
    # phone
    "phone": "phone", "phone_number": "phone", "mobile": "phone", "tel": "phone",
    # company / title
    "current_company": "current_company", "company": "current_company",
    "employer": "current_company", "organization": "current_company",
    "title": "title", "job_title": "title", "current_title": "title",
    "position": "title", "role": "title",
    # location
    "location": "location", "city": "city", "country": "country",
    "region": "region", "state": "region",
    # links
    "linkedin": "linkedin", "linkedin_url": "linkedin", "linkedin_profile": "linkedin",
    "github": "github", "github_url": "github", "github_profile": "github",
    # skills
    "skills": "skills", "skill_set": "skills", "technologies": "skills",
    "tech_stack": "skills", "expertise": "skills",
    # experience
    "years_experience": "years_experience", "years": "years_experience",
    "experience_years": "years_experience",
    # education
    "education": "education", "degree": "degree", "school": "school",
    "university": "school", "institution": "school",
    # notes / headline
    "headline": "headline", "summary": "headline", "bio": "headline",
    "notes": "notes",
    # id
    "id": "id", "candidate_id": "id",
}


def _normalize_col(col: str) -> str:
    return col.strip().lower().replace(" ", "_").replace("-", "_")


def _map_row(row: dict[str, str]) -> dict[str, Any]:
    """Map a raw CSV row to canonical field names."""
    mapped: dict[str, Any] = {}
    for raw_col, value in row.items():
        normalized_col = _normalize_col(raw_col)
        canonical = _COL_MAP.get(normalized_col)
        if canonical and value and value.strip():
            # Don't overwrite already-set canonical fields
            if canonical not in mapped:
                mapped[canonical] = value.strip()
    return mapped


def _parse_skills(raw: str) -> list[str]:
    """Parse a comma/semicolon/pipe delimited skills string."""
    if not raw:
        return []
    for sep in [",", ";", "|"]:
        if sep in raw:
            return [s.strip() for s in raw.split(sep) if s.strip()]
    return [raw.strip()] if raw.strip() else []


def _parse_years(raw: str) -> Optional[float]:
    """Extract a number from strings like '5', '5 years', '5.5'."""
    import re
    if not raw:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)", raw)
    return float(m.group(1)) if m else None


def extract(source_path: str) -> list[CandidateProfile]:
    """
    Read a recruiter CSV file and return a list of (partial) CandidateProfiles.
    Never raises — logs errors and skips bad rows.
    """
    path = Path(source_path)
    if not path.exists():
        logger.warning(f"[csv_source] File not found: {source_path}")
        return []

    profiles: list[CandidateProfile] = []

    try:
        content = path.read_text(encoding="utf-8-sig", errors="replace")
    except Exception as e:
        logger.error(f"[csv_source] Cannot read file {source_path}: {e}")
        return []

    try:
        reader = csv.DictReader(io.StringIO(content))
    except Exception as e:
        logger.error(f"[csv_source] Cannot parse CSV {source_path}: {e}")
        return []

    for i, row in enumerate(reader):
        try:
            profile = _build_profile(row, i, source_path)
            if profile:
                profiles.append(profile)
        except Exception as e:
            logger.warning(f"[csv_source] Skipping row {i}: {e}")

    logger.info(f"[csv_source] Extracted {len(profiles)} profiles from {source_path}")
    return profiles


def _build_profile(row: dict[str, str], row_idx: int, source_path: str) -> Optional[CandidateProfile]:
    mapped = _map_row(row)

    if not mapped:
        return None

    provenance: list[ProvenanceEntry] = []

    def record(field: str, method: str = "direct") -> None:
        provenance.append(ProvenanceEntry(field=field, source=SOURCE_NAME, method=method))

    # candidate_id: use explicit id or generate from row index + file
    cid = mapped.get("id") or f"{Path(source_path).stem}_row{row_idx}"

    # full_name
    full_name: Optional[str] = None
    if "full_name" in mapped:
        full_name = normalize_name(mapped["full_name"])
        if full_name:
            record("full_name")

    # emails
    emails: list[str] = []
    if "email" in mapped:
        em = normalize_email(mapped["email"])
        if em:
            emails.append(em)
            record("emails")

    # phones
    phones: list[str] = []
    if "phone" in mapped:
        ph = normalize_phone(mapped["phone"])
        if ph:
            phones.append(ph)
            record("phones", "normalized")

    # location
    location: Optional[LocationEntry] = None
    loc_parts: dict[str, Optional[str]] = {}
    if "location" in mapped:
        # Try to parse "City, Country" or "City, State, Country"
        parts = [p.strip() for p in mapped["location"].split(",")]
        if len(parts) == 1:
            loc_parts["city"] = parts[0]
        elif len(parts) == 2:
            loc_parts["city"] = parts[0]
            country = normalize_country(parts[1])
            loc_parts["country"] = country or parts[1]
        elif len(parts) >= 3:
            loc_parts["city"] = parts[0]
            loc_parts["region"] = parts[1]
            loc_parts["country"] = normalize_country(parts[2]) or parts[2]
    if "city" in mapped:
        loc_parts["city"] = mapped["city"]
    if "region" in mapped:
        loc_parts["region"] = mapped["region"]
    if "country" in mapped:
        loc_parts["country"] = normalize_country(mapped["country"]) or mapped["country"]

    if loc_parts:
        location = LocationEntry(**{k: v for k, v in loc_parts.items() if k in ("city", "region", "country")})
        record("location", "normalized")

    # links
    links: Optional[LinksEntry] = None
    link_data: dict[str, Any] = {}
    if "linkedin" in mapped:
        link_data["linkedin"] = mapped["linkedin"]
    if "github" in mapped:
        link_data["github"] = mapped["github"]
    if link_data:
        links = LinksEntry(**link_data)
        record("links")

    # headline
    headline: Optional[str] = None
    if "headline" in mapped:
        headline = mapped["headline"]
        record("headline")
    elif "notes" in mapped and not headline:
        headline = mapped["notes"][:200]  # truncate free notes
        record("headline", "inferred")

    # years_experience
    years_experience: Optional[float] = None
    if "years_experience" in mapped:
        years_experience = _parse_years(mapped["years_experience"])
        if years_experience is not None:
            record("years_experience", "normalized")

    # skills
    skills: list[SkillEntry] = []
    if "skills" in mapped:
        raw_skills = _parse_skills(mapped["skills"])
        for rs in raw_skills:
            canonical = normalize_skill(rs)
            if canonical:
                skills.append(SkillEntry(name=canonical, confidence=0.9, sources=[SOURCE_NAME]))
        if skills:
            record("skills", "normalized")

    # experience (current company + title)
    experience: list[ExperienceEntry] = []
    if "current_company" in mapped or "title" in mapped:
        exp = ExperienceEntry(
            company=mapped.get("current_company", ""),
            title=mapped.get("title", ""),
        )
        experience.append(exp)
        record("experience")

    # education (basic: degree + school)
    education: list[EducationEntry] = []
    if "school" in mapped or "degree" in mapped:
        edu = EducationEntry(
            institution=mapped.get("school", ""),
            degree=mapped.get("degree"),
        )
        education.append(edu)
        record("education")

    # Confidence: based on how many key fields we have
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
