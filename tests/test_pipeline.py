"""
test_pipeline.py — end-to-end pipeline test against a gold profile, plus
edge-case coverage (missing file, malformed JSON, garbage CSV row, strict
no-false-merge policy, required-field config enforcement).

The gold profile asserts the full detect -> extract -> merge -> project
chain against the actual sample inputs in samples/, so a regression
anywhere in the pipeline will break this test.
"""

from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest

from transformer.pipeline import SourceInput, run_pipeline, run_pipeline_to_canonical, load_config
from transformer.projector import ProjectionError

SAMPLES = ROOT / "samples"
CONFIG_DIR = ROOT / "config"


# ─── Gold profile: Jane Doe merged from CSV + ATS JSON ──────────────────────

def test_aarav_sharma_gold_profile_default_config():
    """
    Aarav Sharma (NSUT) appears in both samples/recruiter.csv and
    samples/ats_blob.json with the same email AND the same phone number
    (in two different raw formats that normalize to the same E.164 string),
    so he must merge into exactly one record with:
      - full_name from ats_json (tie-broken by SOURCE_PRIORITY)
      - emails unioned to one value (both sources agree)
      - phones: csv's bare "9811023456" normalizes to +919811023456 (India
        default country code) and ats_json's "+919811023456" normalizes to
        the same string, so the union correctly collapses to ONE phone, not
        two — this is also the regression check for the default country
        code fix (was +1/US, now +91/India, see normalizers.py CHANGED note)
      - skills unioned across both sources, deduped by canonical name
      - experience: 2 Razorpay entries (full-time + earlier internship),
        both contributed by ats_json since csv's sample has no experience
        dates for him
      - education from ats_json (csv sample has no education row for him)
      - overall_confidence at the ceiling (full completeness + corroboration)
    """
    config = load_config(str(CONFIG_DIR / "default_config.json"))
    inputs = [
        SourceInput(str(SAMPLES / "recruiter.csv")),
        SourceInput(str(SAMPLES / "ats_blob.json")),
    ]
    results = run_pipeline(inputs, config)

    aarav = next((r for r in results if r.get("full_name") == "Aarav Sharma"), None)
    assert aarav is not None, "Aarav Sharma should appear exactly once in the merged output"

    assert aarav["emails"] == ["aarav.sharma@nsut.ac.in"]
    assert aarav["phones"] == ["+919811023456"], (
        "csv's bare 9811023456 and ats_json's +919811023456 must normalize "
        "to the SAME E.164 string and collapse to one phone in the union"
    )

    skill_names = set(aarav["skills"])
    assert "C++" in skill_names
    assert "Python" in skill_names
    assert "System Design" in skill_names

    companies = [e["company"] for e in aarav["experience"]]
    assert companies == ["Razorpay", "Razorpay"]
    titles = {e["title"] for e in aarav["experience"]}
    assert titles == {"Software Development Engineer", "Software Engineer Intern"}

    assert aarav["education"][0]["institution"] == "Netaji Subhas University of Technology"
    assert aarav["overall_confidence"] == 1.0

    name_prov = [p for p in aarav["provenance"] if p["field"] == "full_name"]
    assert len(name_prov) == 1
    assert name_prov[0]["source"] == "ats_json"


def test_aarav_sharma_count_is_exactly_one_not_split_or_duplicated():
    config = load_config(str(CONFIG_DIR / "default_config.json"))
    inputs = [
        SourceInput(str(SAMPLES / "recruiter.csv")),
        SourceInput(str(SAMPLES / "ats_blob.json")),
    ]
    results = run_pipeline(inputs, config)
    count = sum(1 for r in results if r.get("full_name") == "Aarav Sharma")
    assert count == 1


# ─── Custom config (assignment's example shape) ─────────────────────────────

def test_custom_config_renames_and_required_fields_enforced():
    config = load_config(str(CONFIG_DIR / "custom_config.json"))
    inputs = [
        SourceInput(str(SAMPLES / "recruiter.csv")),
        SourceInput(str(SAMPLES / "ats_blob.json")),
    ]
    results = run_pipeline(inputs, config)

    # Custom config renames full_name->full_name (passthrough), emails[0]->primary_email
    aarav = next(r for r in results if r.get("full_name") == "Aarav Sharma")
    assert "primary_email" in aarav
    assert aarav["primary_email"] == "aarav.sharma@nsut.ac.in"
    assert "emails" not in aarav  # field subset selection: only configured fields appear

    # Candidates missing a *required* field (full_name or primary_email)
    # must be silently dropped from custom-config output, not emitted with
    # nulls — required means required.
    for r in results:
        assert r.get("full_name")
        assert r.get("primary_email")


# ─── Strict match policy (no false merge) ───────────────────────────────────

def test_aarav_sharma_namesake_resume_does_not_falsely_merge_with_nsut_aarav():
    """
    samples/resume_aarav_sharma_namesake.docx is a deliberate same-name
    coincidence: a different Aarav Sharma (mechanical engineer, no
    email/phone given) who shares a name with the NSUT software engineer
    in recruiter.csv/ats_blob.json but has zero contact-info overlap.
    Per the strict match policy, these must stay as two separate
    candidates rather than being merged on name alone — exactly the
    scenario the strict policy exists to protect against.
    """
    config = load_config(str(CONFIG_DIR / "default_config.json"))
    inputs = [
        SourceInput(str(SAMPLES / "recruiter.csv")),
        SourceInput(str(SAMPLES / "ats_blob.json")),
        SourceInput(str(SAMPLES / "resume_aarav_sharma_namesake.docx"), kind="resume"),
    ]
    results = run_pipeline(inputs, config)
    aarav_records = [r for r in results if r.get("full_name") == "Aarav Sharma"]
    assert len(aarav_records) == 2, (
        "Two different people sharing a name with no shared email/phone "
        "key must NOT be merged (strict policy: a false merge is worse "
        "than two correctly-separate records)"
    )
    headlines = {r.get("headline") for r in aarav_records}
    assert any(h and "Razorpay" in h for h in headlines)
    assert any(h and "Mechanical" in h for h in headlines)


def test_rohan_verma_merges_across_csv_and_resume():
    """
    Unlike the namesake case above, Rohan Verma's resume DOES include his
    email and phone, matching samples/recruiter.csv, so this is the
    positive case: a 3-source-eligible merge (csv + resume here; ats_blob
    doesn't mention him) should correctly combine into one record with
    richer experience/education than either source alone.
    """
    config = load_config(str(CONFIG_DIR / "default_config.json"))
    inputs = [
        SourceInput(str(SAMPLES / "recruiter.csv")),
        SourceInput(str(SAMPLES / "resume_rohan_verma.docx"), kind="resume"),
    ]
    results = run_pipeline(inputs, config)
    rohan_records = [r for r in results if r.get("full_name") == "Rohan Verma"]
    assert len(rohan_records) == 1
    rohan = rohan_records[0]
    # Resume contributes 2 education entries (M.Tech + B.Tech); csv only had one.
    assert len(rohan["education"]) == 2


# ─── Robustness / graceful degradation ──────────────────────────────────────

def test_missing_input_file_does_not_crash_pipeline():
    config = load_config(str(CONFIG_DIR / "default_config.json"))
    inputs = [
        SourceInput(str(SAMPLES / "recruiter.csv")),
        SourceInput("this/file/does/not/exist.csv"),
    ]
    results = run_pipeline(inputs, config)
    assert len(results) > 0  # the good file still produces output


def test_garbage_csv_row_does_not_crash_and_yields_partial_record():
    profiles = run_pipeline_to_canonical([SourceInput(str(SAMPLES / "recruiter.csv"))])
    garbage = next(
        (p for p in profiles if p.full_name and "garbage" in p.full_name.lower()), None
    )
    assert garbage is not None
    assert garbage.emails == []
    assert garbage.overall_confidence < 0.3  # low confidence, not invented data


def test_malformed_json_source_degrades_to_empty_not_a_crash(tmp_path):
    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{not valid json", encoding="utf-8")
    config = load_config(str(CONFIG_DIR / "default_config.json"))
    inputs = [SourceInput(str(SAMPLES / "recruiter.csv")), SourceInput(str(bad_json), kind="ats_json")]
    results = run_pipeline(inputs, config)
    assert len(results) > 0  # csv data still comes through; bad json contributes nothing


def test_invalid_config_raises_projection_error_not_silent_failure(tmp_path):
    bad_config_path = tmp_path / "bad_config.json"
    bad_config_path.write_text(
        '{"fields": [{"path": "x", "type": "not_a_type"}], "on_missing": "bogus"}',
        encoding="utf-8",
    )
    with pytest.raises(ProjectionError):
        load_config(str(bad_config_path))


def test_required_field_missing_for_all_candidates_skips_them_gracefully(tmp_path):
    """on_missing alone shouldn't crash the batch; only required+missing does,
    and only for the affected candidate, not the whole run."""
    config = {
        "fields": [{"path": "full_name", "type": "string", "required": True}],
        "on_missing": "null",
    }
    profiles = run_pipeline_to_canonical([SourceInput(str(SAMPLES / "recruiter.csv"))])
    from transformer.projector import project_all
    results = project_all(profiles, config)
    # Every candidate in the output must have a non-empty full_name, since
    # it's required; any candidate missing it should have been dropped.
    assert all(r.get("full_name") for r in results)
    assert len(results) < len(profiles)  # at least the garbage/no-name rows were dropped


# ─── Resume unstructured source sanity check ─────────────────────────────────

def test_resume_source_extracts_structured_fields_from_docx():
    profiles = run_pipeline_to_canonical(
        [SourceInput(str(SAMPLES / "resume_rohan_verma.docx"), kind="resume")]
    )
    assert len(profiles) == 1
    rohan = profiles[0]
    assert rohan.full_name == "Rohan Verma"
    assert rohan.emails == ["rohan.verma@sric.iitd.ac.in"]
    assert rohan.phones == ["+919650012345"]
    assert rohan.links is not None and rohan.links.github
    assert rohan.headline is not None
    assert len(rohan.experience) == 2
    assert len(rohan.education) == 2
    assert "Python" in [s.name for s in rohan.skills]


def test_resume_source_namesake_has_no_contact_info():
    """The deliberately-no-contact-info namesake resume should extract
    name/experience/education/skills but never invent an email or phone."""
    profiles = run_pipeline_to_canonical(
        [SourceInput(str(SAMPLES / "resume_aarav_sharma_namesake.docx"), kind="resume")]
    )
    assert len(profiles) == 1
    namesake = profiles[0]
    assert namesake.full_name == "Aarav Sharma"
    assert namesake.emails == []
    assert namesake.phones == []
    assert "Tata Motors" in [e.company for e in namesake.experience]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))