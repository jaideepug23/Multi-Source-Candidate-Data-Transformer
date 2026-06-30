"""
merger.py — Cross-source candidate matching, conflict resolution, and
confidence scoring.

Design (see design_doc.py for the one-page rationale):

  1. MATCH: two partial CandidateProfiles (possibly from different sources)
     are considered the same person iff they share at least one normalized
     email OR at least one normalized phone (both already E.164/lowercased
     by the source extractors). This is intentionally strict — no fuzzy
     name matching — because a false merge silently corrupts a profile
     ("wrong-but-confident is worse than honestly-empty"), whereas a missed
     merge just leaves two separate (less complete) profiles, which is
     recoverable and visible.

  2. RESOLVE: for every scalar field (full_name, headline, years_experience,
     location) the candidate value with the highest per-source confidence
     wins. Ties are broken by a fixed SOURCE_PRIORITY order:
         ats_json > csv > github
     (rationale: structured, recruiter/HR-curated sources outrank
     scraped/public ones when confidence is equal).

  3. LIST fields (emails, phones, skills, links) are unioned across all
     sources in a cluster, not winner-takes-all — losing a second valid
     email because another source "won" would destroy real information.
     Skills are merged by canonical name: if multiple sources report the
     same skill, we keep the highest confidence and union the `sources`
     list so the final record shows everywhere that skill came from.

  4. PROVENANCE on the merged record always points to the source(s) that
     actually contributed the winning value — never the source that lost
     a conflict.

  5. CONFIDENCE: overall_confidence on the merged record blends the mean
     per-source confidence of contributing sources with how complete the
     merged record ended up, plus a small bonus when multiple independent
     sources corroborate the same person (agreement is positive evidence).
"""

from __future__ import annotations
import logging
from typing import Optional

from transformer.schema import (
    CandidateProfile, SkillEntry, ExperienceEntry, EducationEntry,
    LocationEntry, LinksEntry, ProvenanceEntry,
)

logger = logging.getLogger(__name__)

# Tie-break order when two sources report a scalar field with EQUAL confidence.
# Earlier = wins.
SOURCE_PRIORITY: list[str] = ["ats_json", "csv", "github"]


def _priority_rank(source: str) -> int:
    try:
        return SOURCE_PRIORITY.index(source)
    except ValueError:
        return len(SOURCE_PRIORITY)  # unknown sources sort last


# ─── Matching ────────────────────────────────────────────────────────────────

def _match_keys(profile: CandidateProfile) -> set[str]:
    """
    Keys used for matching: normalized emails (already lowercased by
    normalize_email) and normalized phones (already E.164 by normalize_phone).
    Both are prefixed so an email can never collide with a phone string.
    """
    keys: set[str] = set()
    for e in profile.emails:
        if e:
            keys.add(f"email:{e}")
    for p in profile.phones:
        if p:
            keys.add(f"phone:{p}")
    return keys


def cluster_profiles(profiles: list[CandidateProfile]) -> list[list[CandidateProfile]]:
    """
    Group profiles into clusters representing the same person, using
    union-find over shared email/phone keys. Profiles with no email and no
    phone at all can never be matched to anything (strict policy) and each
    form their own singleton cluster — we'd rather keep them separate than
    risk a false merge.
    """
    n = len(profiles)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    key_to_first_idx: dict[str, int] = {}
    for idx, profile in enumerate(profiles):
        for key in _match_keys(profile):
            if key in key_to_first_idx:
                union(key_to_first_idx[key], idx)
            else:
                key_to_first_idx[key] = idx

    clusters: dict[int, list[CandidateProfile]] = {}
    for idx, profile in enumerate(profiles):
        root = find(idx)
        clusters.setdefault(root, []).append(profile)

    return list(clusters.values())


# ─── Field-level helpers ─────────────────────────────────────────────────────

def _provenance_method_for(profile: CandidateProfile, field: str) -> str:
    for p in profile.provenance:
        if p.field == field:
            return p.method
    return "direct"


def _source_of(profile: CandidateProfile) -> str:
    return profile.provenance[0].source if profile.provenance else "unknown"


def _scalar_candidates(
    cluster: list[CandidateProfile], getter
) -> list[tuple[CandidateProfile, object]]:
    """Return (profile, value) pairs where getter(profile) is truthy."""
    out = []
    for profile in cluster:
        val = getter(profile)
        if val:
            out.append((profile, val))
    return out


def _pick_winner(
    candidates: list[tuple[CandidateProfile, object]],
) -> Optional[tuple[CandidateProfile, object]]:
    """
    Highest profile.overall_confidence wins; tie-break by SOURCE_PRIORITY.
    `candidates` is a list of (profile, value) for profiles that have a
    non-empty value for this field.
    """
    if not candidates:
        return None

    def sort_key(pair: tuple[CandidateProfile, object]) -> tuple[float, int]:
        profile, _ = pair
        return (-profile.overall_confidence, _priority_rank(_source_of(profile)))

    return sorted(candidates, key=sort_key)[0]


# ─── Per-field mergers ───────────────────────────────────────────────────────

def _merge_emails(cluster: list[CandidateProfile]) -> tuple[list[str], list[ProvenanceEntry]]:
    seen: dict[str, str] = {}  # email -> source that first contributed it
    for profile in cluster:
        src = _source_of(profile)
        for e in profile.emails:
            if e and e not in seen:
                seen[e] = src
    emails = list(seen.keys())
    prov = [ProvenanceEntry(field="emails", source=src, method="direct")
            for src in dict.fromkeys(seen.values())]
    return emails, prov


def _merge_phones(cluster: list[CandidateProfile]) -> tuple[list[str], list[ProvenanceEntry]]:
    seen: dict[str, str] = {}
    for profile in cluster:
        src = _source_of(profile)
        for p in profile.phones:
            if p and p not in seen:
                seen[p] = src
    phones = list(seen.keys())
    prov = [ProvenanceEntry(field="phones", source=src, method="normalized")
            for src in dict.fromkeys(seen.values())]
    return phones, prov


def _merge_scalar(
    cluster: list[CandidateProfile], field_name: str, getter
) -> tuple[Optional[object], list[ProvenanceEntry]]:
    candidates = _scalar_candidates(cluster, getter)
    winner = _pick_winner(candidates)
    if winner is None:
        return None, []
    profile, value = winner
    method = _provenance_method_for(profile, field_name)
    prov = [ProvenanceEntry(field=field_name, source=_source_of(profile), method=method)]
    return value, prov


def _merge_skills(cluster: list[CandidateProfile]) -> tuple[list[SkillEntry], list[ProvenanceEntry]]:
    by_name: dict[str, SkillEntry] = {}
    for profile in cluster:
        for skill in profile.skills:
            if skill.name in by_name:
                existing = by_name[skill.name]
                if skill.confidence > existing.confidence:
                    existing.confidence = skill.confidence
                existing.sources = list(dict.fromkeys(existing.sources + skill.sources))
            else:
                by_name[skill.name] = SkillEntry(
                    name=skill.name, confidence=skill.confidence, sources=list(skill.sources)
                )
    skills = sorted(by_name.values(), key=lambda s: (-s.confidence, s.name))
    contributing = dict.fromkeys(src for skill in skills for src in skill.sources)
    prov = [ProvenanceEntry(field="skills", source=src, method="merged") for src in contributing]
    return skills, prov


def _merge_experience(cluster: list[CandidateProfile]) -> tuple[list[ExperienceEntry], list[ProvenanceEntry]]:
    """
    Dedup key: (company.lower(), title.lower()). When the same role appears
    from multiple sources, prefer the entry with more populated fields
    (start/end/summary) rather than a strict confidence comparison, since
    experience entries don't carry their own confidence score in the schema.
    """
    by_key: dict[tuple[str, str], tuple[ExperienceEntry, str]] = {}
    for profile in cluster:
        src = _source_of(profile)
        for exp in profile.experience:
            key = (exp.company.strip().lower(), exp.title.strip().lower())
            if not key[0] and not key[1]:
                continue
            richness = sum([bool(exp.start), bool(exp.end), bool(exp.summary)])
            if key not in by_key:
                by_key[key] = (exp, src)
            else:
                existing_exp, _ = by_key[key]
                existing_richness = sum([bool(existing_exp.start), bool(existing_exp.end), bool(existing_exp.summary)])
                if richness > existing_richness:
                    by_key[key] = (exp, src)

    if not by_key:
        return [], []

    entries = [v[0] for v in by_key.values()]
    sources_used = list(dict.fromkeys(v[1] for v in by_key.values()))
    prov = [ProvenanceEntry(field="experience", source=src, method="merged") for src in sources_used]
    return entries, prov


def _merge_education(cluster: list[CandidateProfile]) -> tuple[list[EducationEntry], list[ProvenanceEntry]]:
    """
    Dedup key is (institution, degree_level). An entry with NO degree
    specified at all has no signal to disambiguate which degree it
    belongs to, so it's treated as matching whatever entry already exists
    for that institution (richer entry wins) rather than creating a
    spurious extra "no degree" record. Entries with different *specified*
    degree levels (B.Tech vs M.Tech) at the same institution are kept as
    separate entries.
    """
    by_institution: dict[str, list[tuple[EducationEntry, str]]] = {}
    for profile in cluster:
        src = _source_of(profile)
        for edu in profile.education:
            institution_key = edu.institution.strip().lower()
            if not institution_key:
                continue
            degree_level = (edu.degree or "").strip().split()[0].lower() if edu.degree else ""
            richness = sum([bool(edu.degree), bool(edu.field), bool(edu.end_year)])

            bucket = by_institution.setdefault(institution_key, [])

            if degree_level == "":
                # No degree info: merge into the richest existing entry for
                # this institution if one exists, else start a new bucket entry.
                if bucket:
                    best_idx = max(range(len(bucket)), key=lambda i: sum([
                        bool(bucket[i][0].degree), bool(bucket[i][0].field), bool(bucket[i][0].end_year)
                    ]))
                    existing_edu, _ = bucket[best_idx]
                    existing_richness = sum([bool(existing_edu.degree), bool(existing_edu.field), bool(existing_edu.end_year)])
                    if richness > existing_richness:
                        bucket[best_idx] = (edu, src)
                    continue
                bucket.append((edu, src))
                continue

            # Has a specified degree level: find an entry in this bucket with
            # the SAME degree level (including a previously-placed no-degree
            # entry, which we now know belongs to this degree level), else
            # start a new entry for this degree level.
            matched_idx = None
            for i, (existing_edu, _) in enumerate(bucket):
                existing_level = (existing_edu.degree or "").strip().split()[0].lower() if existing_edu.degree else ""
                if existing_level == degree_level or existing_level == "":
                    matched_idx = i
                    break
            if matched_idx is None:
                bucket.append((edu, src))
            else:
                existing_edu, _ = bucket[matched_idx]
                existing_richness = sum([bool(existing_edu.degree), bool(existing_edu.field), bool(existing_edu.end_year)])
                if richness > existing_richness:
                    bucket[matched_idx] = (edu, src)

    if not by_institution:
        return [], []

    all_pairs = [pair for bucket in by_institution.values() for pair in bucket]
    entries = [v[0] for v in all_pairs]
    sources_used = list(dict.fromkeys(v[1] for v in all_pairs))
    prov = [ProvenanceEntry(field="education", source=src, method="merged") for src in sources_used]
    return entries, prov


def _merge_location(cluster: list[CandidateProfile]) -> tuple[Optional[LocationEntry], list[ProvenanceEntry]]:
    """
    The highest-confidence source with *any* location wins the base record,
    then we backfill any sub-field (city/region/country) the winner left
    empty from other cluster members — without overwriting anything the
    winner already supplied.
    """
    candidates = _scalar_candidates(cluster, lambda p: p.location)
    winner = _pick_winner(candidates)
    if winner is None:
        return None, []
    profile, location = winner

    merged = LocationEntry(city=location.city, region=location.region, country=location.country)
    for other in cluster:
        if other.location:
            if not merged.city and other.location.city:
                merged.city = other.location.city
            if not merged.region and other.location.region:
                merged.region = other.location.region
            if not merged.country and other.location.country:
                merged.country = other.location.country

    prov = [ProvenanceEntry(field="location", source=_source_of(profile), method="normalized")]
    return merged, prov


def _merge_links(cluster: list[CandidateProfile]) -> tuple[Optional[LinksEntry], list[ProvenanceEntry]]:
    candidates = [p for p in cluster if p.links]
    if not candidates:
        return None, []
    merged = LinksEntry()
    other: list[str] = []
    contributing_sources: list[str] = []
    for profile in cluster:
        if not profile.links:
            continue
        src = _source_of(profile)
        l = profile.links
        used = False
        if l.linkedin and not merged.linkedin:
            merged.linkedin = l.linkedin
            used = True
        if l.github and not merged.github:
            merged.github = l.github
            used = True
        if l.portfolio and not merged.portfolio:
            merged.portfolio = l.portfolio
            used = True
        for o in l.other:
            if o not in other:
                other.append(o)
                used = True
        if used:
            contributing_sources.append(src)
    merged.other = other
    prov = [ProvenanceEntry(field="links", source=src, method="merged")
            for src in dict.fromkeys(contributing_sources)]
    return merged, prov


# ─── Confidence ──────────────────────────────────────────────────────────────

def _compute_overall_confidence(cluster: list[CandidateProfile], merged: CandidateProfile) -> float:
    """
    Blend of (a) mean per-source confidence of all profiles in the cluster,
    (b) how complete the merged record ended up across the 9 top-level
    fields that matter most, and (c) a small corroboration bonus when more
    than one independent source agrees this is the same person.
    """
    field_checks = [
        bool(merged.full_name), bool(merged.emails), bool(merged.phones),
        bool(merged.location), bool(merged.headline), bool(merged.skills),
        bool(merged.experience), bool(merged.education), bool(merged.links),
    ]
    populated = sum(field_checks)
    if populated == 0:
        return 0.0

    source_confidences = [p.overall_confidence for p in cluster]
    base = sum(source_confidences) / len(source_confidences) if source_confidences else 0.0

    completeness = populated / len(field_checks)
    corroboration_bonus = min(0.1, 0.02 * (len(cluster) - 1))  # multiple sources agreeing

    overall = (0.6 * base) + (0.4 * completeness) + corroboration_bonus
    return round(min(1.0, overall), 3)


# ─── Top-level merge ─────────────────────────────────────────────────────────

def merge_cluster(cluster: list[CandidateProfile], candidate_id: str) -> CandidateProfile:
    """Merge one cluster of same-person profiles into a single canonical record."""
    if len(cluster) == 1:
        single = cluster[0]
        merged = CandidateProfile(
            candidate_id=candidate_id,
            full_name=single.full_name,
            emails=list(single.emails),
            phones=list(single.phones),
            location=single.location,
            links=single.links,
            headline=single.headline,
            years_experience=single.years_experience,
            skills=list(single.skills),
            experience=list(single.experience),
            education=list(single.education),
            provenance=list(single.provenance),
            overall_confidence=0.0,
        )
        merged.overall_confidence = _compute_overall_confidence(cluster, merged)
        return merged

    full_name, p1 = _merge_scalar(cluster, "full_name", lambda p: p.full_name)
    emails, p2 = _merge_emails(cluster)
    phones, p3 = _merge_phones(cluster)
    location, p4 = _merge_location(cluster)
    links, p5 = _merge_links(cluster)
    headline, p6 = _merge_scalar(cluster, "headline", lambda p: p.headline)
    years_experience, p7 = _merge_scalar(cluster, "years_experience", lambda p: p.years_experience)
    skills, p8 = _merge_skills(cluster)
    experience, p9 = _merge_experience(cluster)
    education, p10 = _merge_education(cluster)

    provenance: list[ProvenanceEntry] = []
    for p in (p1, p2, p3, p4, p5, p6, p7, p8, p9, p10):
        provenance.extend(p)

    merged = CandidateProfile(
        candidate_id=candidate_id,
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
        overall_confidence=0.0,
    )
    merged.overall_confidence = _compute_overall_confidence(cluster, merged)
    return merged


def merge_all(profiles: list[CandidateProfile]) -> list[CandidateProfile]:
    """
    Cluster all extracted profiles (from every source) by shared email/phone,
    then merge each cluster into one canonical CandidateProfile.
    Deterministic: cluster order follows first-appearance order of profiles,
    and within a cluster all tie-breaks are deterministic (priority list).
    """
    if not profiles:
        return []

    clusters = cluster_profiles(profiles)
    merged_profiles: list[CandidateProfile] = []

    for cluster in clusters:
        # Deterministic candidate_id: prefer an id contributed by a
        # structured source (more likely to be a stable recruiter/ATS id);
        # else fall back to the first profile's id in input order.
        candidate_id = next(
            (p.candidate_id for p in cluster if _source_of(p) in ("ats_json", "csv")),
            cluster[0].candidate_id,
        )
        merged_profiles.append(merge_cluster(cluster, candidate_id))

    logger.info(f"[merger] {len(profiles)} extracted profiles -> {len(merged_profiles)} merged candidates")
    return merged_profiles