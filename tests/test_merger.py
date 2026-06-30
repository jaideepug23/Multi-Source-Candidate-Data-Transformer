"""
test_merger.py — unit tests for transformer/merger.py.

Covers: match-key clustering (email/phone), the strict "no fuzzy name
matching" policy, scalar conflict resolution by confidence + source
priority, list-field union (emails/phones/skills), experience/education
dedup-and-enrich, and confidence scoring.
"""

from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from transformer.schema import CandidateProfile, SkillEntry, ExperienceEntry, EducationEntry, ProvenanceEntry
from transformer.merger import cluster_profiles, merge_all, merge_cluster, SOURCE_PRIORITY


def _profile(
    candidate_id: str,
    source: str,
    full_name=None,
    emails=None,
    phones=None,
    headline=None,
    confidence=0.5,
    skills=None,
    experience=None,
    education=None,
) -> CandidateProfile:
    """Helper to build a minimal profile with a provenance entry tagging
    its source, mirroring what a real source extractor would produce."""
    prov = [ProvenanceEntry(field="full_name", source=source, method="direct")] if full_name else []
    return CandidateProfile(
        candidate_id=candidate_id,
        full_name=full_name,
        emails=emails or [],
        phones=phones or [],
        headline=headline,
        skills=skills or [],
        experience=experience or [],
        education=education or [],
        provenance=prov,
        overall_confidence=confidence,
    )


# ─── Matching / clustering ───────────────────────────────────────────────────

def test_same_email_merges_into_one_cluster():
    a = _profile("a", "csv", emails=["jane@example.com"])
    b = _profile("b", "ats_json", emails=["jane@example.com"])
    clusters = cluster_profiles([a, b])
    assert len(clusters) == 1
    assert len(clusters[0]) == 2


def test_same_phone_merges_into_one_cluster():
    a = _profile("a", "csv", phones=["+14155550192"])
    b = _profile("b", "github", phones=["+14155550192"])
    clusters = cluster_profiles([a, b])
    assert len(clusters) == 1


def test_different_email_and_phone_stay_separate():
    a = _profile("a", "csv", emails=["jane@example.com"], phones=["+14155550192"])
    b = _profile("b", "ats_json", emails=["someone.else@example.com"], phones=["+19998887777"])
    clusters = cluster_profiles([a, b])
    assert len(clusters) == 2


def test_same_name_no_shared_contact_does_not_merge():
    """
    Critical policy test: the strict match key is email-or-phone ONLY.
    Two records with the identical name but no shared email/phone must
    NOT be merged, even though a human might suspect they're the same
    person — a false merge silently corrupts data, which the assignment
    explicitly calls out as worse than staying split.
    """
    a = _profile("a", "csv", full_name="Amy Chen", emails=["amy.chen@example.com"])
    b = _profile("b", "github", full_name="Amy Chen")  # no email/phone at all
    clusters = cluster_profiles([a, b])
    assert len(clusters) == 2


def test_transitive_merge_via_shared_intermediate_record():
    """A has email1+phone1, B has phone1+email2, C has email2 only.
    A-B share phone1, B-C share email2 -> all three should merge transitively."""
    a = _profile("a", "csv", emails=["one@example.com"], phones=["+1111111111"])
    b = _profile("b", "ats_json", phones=["+1111111111"], emails=["two@example.com"])
    c = _profile("c", "github", emails=["two@example.com"])
    clusters = cluster_profiles([a, b, c])
    assert len(clusters) == 1
    assert len(clusters[0]) == 3


def test_no_contact_info_is_always_a_singleton():
    a = _profile("a", "github", full_name="No Contact Person")
    b = _profile("b", "csv", emails=["someone@example.com"])
    clusters = cluster_profiles([a, b])
    assert len(clusters) == 2


# ─── Conflict resolution ─────────────────────────────────────────────────────

def test_higher_confidence_source_wins_scalar_conflict():
    high = _profile("a", "github", full_name="Low Quality Name", confidence=0.9,
                     emails=["x@example.com"])
    low = _profile("b", "csv", full_name="Jane Doe", confidence=0.3,
                    emails=["x@example.com"])
    merged = merge_all([high, low])
    assert len(merged) == 1
    assert merged[0].full_name == "Low Quality Name"  # higher confidence wins despite source


def test_tie_break_uses_source_priority_order():
    """
    When confidences are exactly equal, SOURCE_PRIORITY decides the winner:
    ats_json > csv > github.
    """
    csv_profile = _profile("a", "csv", full_name="From CSV", confidence=0.5,
                            emails=["x@example.com"])
    ats_profile = _profile("b", "ats_json", full_name="From ATS", confidence=0.5,
                            emails=["x@example.com"])
    merged = merge_all([csv_profile, ats_profile])
    assert merged[0].full_name == "From ATS"
    assert SOURCE_PRIORITY.index("ats_json") < SOURCE_PRIORITY.index("csv")


def test_emails_and_phones_are_unioned_not_winner_take_all():
    a = _profile("a", "csv", emails=["primary@example.com"], phones=["+14155550192"])
    b = _profile("b", "ats_json", emails=["primary@example.com", "secondary@example.com"])
    merged = merge_all([a, b])
    assert set(merged[0].emails) == {"primary@example.com", "secondary@example.com"}
    assert merged[0].phones == ["+14155550192"]


def test_skills_merged_by_canonical_name_keeping_higher_confidence():
    a = _profile("a", "csv", emails=["x@example.com"],
                 skills=[SkillEntry(name="Python", confidence=0.6, sources=["csv"])])
    b = _profile("b", "ats_json", emails=["x@example.com"],
                 skills=[SkillEntry(name="Python", confidence=0.9, sources=["ats_json"]),
                         SkillEntry(name="Go", confidence=0.8, sources=["ats_json"])])
    merged = merge_all([a, b])
    by_name = {s.name: s for s in merged[0].skills}
    assert by_name["Python"].confidence == 0.9
    assert set(by_name["Python"].sources) == {"csv", "ats_json"}
    assert "Go" in by_name


def test_experience_dedup_prefers_richer_entry():
    sparse = ExperienceEntry(company="Acme", title="Engineer")
    rich = ExperienceEntry(company="Acme", title="Engineer", start="2020-01", end="2022-01", summary="Did things")
    a = _profile("a", "csv", emails=["x@example.com"], experience=[sparse])
    b = _profile("b", "ats_json", emails=["x@example.com"], experience=[rich])
    merged = merge_all([a, b])
    assert len(merged[0].experience) == 1
    assert merged[0].experience[0].start == "2020-01"
    assert merged[0].experience[0].summary == "Did things"


def test_education_dedup_by_institution_keeps_richer_entry():
    sparse = EducationEntry(institution="Stanford University")
    rich = EducationEntry(institution="Stanford University", degree="M.S.", field="CS", end_year=2017)
    a = _profile("a", "csv", emails=["x@example.com"], education=[sparse])
    b = _profile("b", "ats_json", emails=["x@example.com"], education=[rich])
    merged = merge_all([a, b])
    assert len(merged[0].education) == 1
    assert merged[0].education[0].degree == "M.S."


def test_education_two_distinct_degrees_at_same_institution_both_kept():
    """
    Regression test: a person with both a B.Tech and an M.Tech from the
    SAME institution must end up with two education entries, not one
    (institution-only dedup would silently drop one degree). Also covers
    sources disagreeing on format: one gives degree+field as one combined
    string ("M.Tech Computer Science"), the other gives them split
    (degree="M.Tech", field="Computer Science") — these must still
    collapse to ONE entry for the M.Tech, not create a third.
    """
    btech = EducationEntry(institution="IIT Delhi", degree="B.Tech", field="CSE", end_year=2022)
    mtech_combined = EducationEntry(institution="IIT Delhi", degree="M.Tech Computer Science")
    mtech_split = EducationEntry(institution="IIT Delhi", degree="M.Tech", field="Computer Science", end_year=2024)

    a = _profile("a", "csv", emails=["x@example.com"], education=[btech, mtech_combined])
    b = _profile("b", "resume", emails=["x@example.com"], education=[mtech_split])
    merged = merge_all([a, b])

    assert len(merged[0].education) == 2
    degree_levels = {e.degree.split()[0] for e in merged[0].education}
    assert degree_levels == {"B.Tech", "M.Tech"}
    mtech_entry = next(e for e in merged[0].education if e.degree.startswith("M.Tech"))
    assert mtech_entry.end_year == 2024  # richer (split) version won over the combined one


# ─── Provenance ──────────────────────────────────────────────────────────────

def test_provenance_points_only_to_contributing_sources():
    a = _profile("a", "csv", full_name="Loser Name", confidence=0.2, emails=["x@example.com"])
    b = _profile("b", "ats_json", full_name="Winner Name", confidence=0.9, emails=["x@example.com"])
    merged = merge_all([a, b])
    name_provenance = [p for p in merged[0].provenance if p.field == "full_name"]
    assert len(name_provenance) == 1
    assert name_provenance[0].source == "ats_json"


# ─── Confidence ──────────────────────────────────────────────────────────────

def test_empty_input_returns_empty_list():
    assert merge_all([]) == []


def test_single_source_profile_passes_through_with_its_own_confidence_blended():
    a = _profile("a", "csv", full_name="Solo Person", confidence=0.8, emails=["solo@example.com"])
    merged = merge_all([a])
    assert len(merged) == 1
    assert merged[0].full_name == "Solo Person"
    assert 0.0 <= merged[0].overall_confidence <= 1.0


def test_corroborated_record_scores_at_least_as_high_as_either_single_source():
    a = _profile("a", "csv", full_name="Jane", confidence=0.6, emails=["jane@example.com"])
    b = _profile("b", "ats_json", full_name="Jane", confidence=0.6, emails=["jane@example.com"])
    merged_pair = merge_all([a, b])[0]
    merged_solo = merge_all([a])[0]
    assert merged_pair.overall_confidence >= merged_solo.overall_confidence


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))