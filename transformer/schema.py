"""
schema.py — Canonical candidate profile schema.
All internal data flows through these types before projection.
"""

from __future__ import annotations
from typing import Any, Optional
from dataclasses import dataclass, field


@dataclass
class SkillEntry:
    name: str
    confidence: float = 1.0
    sources: list[str] = field(default_factory=list)


@dataclass
class ExperienceEntry:
    company: str
    title: str
    start: Optional[str] = None   # YYYY-MM
    end: Optional[str] = None     # YYYY-MM or None if current
    summary: Optional[str] = None


@dataclass
class EducationEntry:
    institution: str
    degree: Optional[str] = None
    field: Optional[str] = None
    end_year: Optional[int] = None


@dataclass
class LocationEntry:
    city: Optional[str] = None
    region: Optional[str] = None
    country: Optional[str] = None  # ISO-3166 alpha-2


@dataclass
class LinksEntry:
    linkedin: Optional[str] = None
    github: Optional[str] = None
    portfolio: Optional[str] = None
    other: list[str] = field(default_factory=list)


@dataclass
class ProvenanceEntry:
    field: str
    source: str   # e.g. "csv", "ats_json", "github", "linkedin"
    method: str   # e.g. "direct", "inferred", "normalized"


@dataclass
class CandidateProfile:
    candidate_id: str
    full_name: Optional[str] = None
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)          # E.164 format
    location: Optional[LocationEntry] = None
    links: Optional[LinksEntry] = None
    headline: Optional[str] = None
    years_experience: Optional[float] = None
    skills: list[SkillEntry] = field(default_factory=list)
    experience: list[ExperienceEntry] = field(default_factory=list)
    education: list[EducationEntry] = field(default_factory=list)
    provenance: list[ProvenanceEntry] = field(default_factory=list)
    overall_confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Serialize full canonical profile to dict."""
        return {
            "candidate_id": self.candidate_id,
            "full_name": self.full_name,
            "emails": self.emails,
            "phones": self.phones,
            "location": (
                {
                    "city": self.location.city,
                    "region": self.location.region,
                    "country": self.location.country,
                }
                if self.location
                else None
            ),
            "links": (
                {
                    "linkedin": self.links.linkedin,
                    "github": self.links.github,
                    "portfolio": self.links.portfolio,
                    "other": self.links.other,
                }
                if self.links
                else None
            ),
            "headline": self.headline,
            "years_experience": self.years_experience,
            "skills": [
                {
                    "name": s.name,
                    "confidence": s.confidence,
                    "sources": s.sources,
                }
                for s in self.skills
            ],
            "experience": [
                {
                    "company": e.company,
                    "title": e.title,
                    "start": e.start,
                    "end": e.end,
                    "summary": e.summary,
                }
                for e in self.experience
            ],
            "education": [
                {
                    "institution": ed.institution,
                    "degree": ed.degree,
                    "field": ed.field,
                    "end_year": ed.end_year,
                }
                for ed in self.education
            ],
            "provenance": [
                {
                    "field": p.field,
                    "source": p.source,
                    "method": p.method,
                }
                for p in self.provenance
            ],
            "overall_confidence": self.overall_confidence,
        }