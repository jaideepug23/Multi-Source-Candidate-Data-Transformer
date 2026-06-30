"""
pipeline.py — Main orchestration: detect → extract → merge → project → validate.

This module deliberately knows nothing about CLI argument parsing or file
discovery beyond "given a list of (kind, source_path) pairs, run the
pipeline." cli.py is the thin surface on top of this.

Pipeline stages:
  1. DETECT  — given a path/URL, decide which source module should handle it
               (or accept an explicit kind override from the caller).
  2. EXTRACT — call that source's extract(path) -> list[CandidateProfile].
               Never raises: a bad/missing file yields [] and a warning,
               by construction of every source module.
  3. MERGE   — transformer.merger.merge_all() clusters by email/phone and
               resolves conflicts into one CandidateProfile per person.
  4. PROJECT — transformer.projector.project_all() reshapes each merged
               profile per the runtime config.
  5. VALIDATE — the projector validates the config up front and raises
               ProjectionError on a structurally invalid config; per-record
               issues are skipped with a logged warning rather than
               aborting the whole batch (robustness constraint).

Determinism: source extraction order is the order callers pass sources in;
within merger/projector everything downstream is a pure function of that
input order, so the same inputs always produce the same output.
"""

from __future__ import annotations
import json
import logging
from pathlib import Path
from typing import Optional

from transformer.schema import CandidateProfile
from transformer.merger import merge_all
from transformer.projector import project_all, validate_config, ProjectionError
from transformer.sources import csv_source, ats_json_source, github_source, resume_source

logger = logging.getLogger(__name__)

# Maps an explicit "kind" string to its extractor module.
_SOURCE_MODULES = {
    "csv": csv_source,
    "ats_json": ats_json_source,
    "github": github_source,
    "resume": resume_source,
}


def detect_source_kind(path: str) -> Optional[str]:
    """
    Best-effort auto-detection of source kind from a path/URL string.
    Returns None if we can't confidently guess — callers should then
    require an explicit --kind for that input rather than us guessing wrong.
    """
    s = path.strip()
    lower = s.lower()

    if "github.com" in lower:
        return "github"

    suffix = Path(s).suffix.lower()
    if suffix == ".csv":
        return "csv"
    if suffix == ".json":
        return "ats_json"
    if suffix in (".pdf", ".docx"):
        return "resume"
    if suffix == ".txt":
        # Ambiguous: plain recruiter notes vs. a .txt resume export both
        # look identical at the file-extension level. Leave undetected —
        # caller must disambiguate with an explicit kind.
        return None

    return None


class SourceInput:
    """One input the pipeline should extract from."""

    def __init__(self, path: str, kind: Optional[str] = None):
        self.path = path
        self.kind = kind or detect_source_kind(path)

    def __repr__(self) -> str:
        return f"SourceInput(path={self.path!r}, kind={self.kind!r})"


def extract_all(inputs: list[SourceInput]) -> list[CandidateProfile]:
    """
    Run extraction for every input. A given input that can't be detected,
    or whose module raises unexpectedly, contributes zero profiles and a
    logged error — it never aborts the rest of the batch.
    """
    all_profiles: list[CandidateProfile] = []

    for src in inputs:
        if src.kind is None:
            logger.error(
                f"[pipeline] Could not detect source kind for '{src.path}'; "
                f"skipping. Pass an explicit kind to fix this."
            )
            continue

        module = _SOURCE_MODULES.get(src.kind)
        if module is None:
            logger.error(f"[pipeline] Unknown source kind '{src.kind}' for '{src.path}'; skipping.")
            continue

        try:
            profiles = module.extract(src.path)
        except Exception as e:  # noqa: BLE001 - last-resort guard; sources should already not raise
            logger.error(f"[pipeline] Source '{src.kind}' raised on '{src.path}': {e}")
            profiles = []

        logger.info(f"[pipeline] {src.kind}:{src.path} -> {len(profiles)} profile(s)")
        all_profiles.extend(profiles)

    return all_profiles


def load_config(config_path: str) -> dict:
    """Load and validate a runtime output config. Raises ProjectionError on
    invalid JSON or a structurally invalid config — this is fail-fast by
    design, since a bad config would otherwise silently misshape every
    candidate the same way."""
    path = Path(config_path)
    if not path.exists():
        raise ProjectionError(f"config file not found: {config_path}")
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ProjectionError(f"config file '{config_path}' is not valid JSON: {e}")

    problems = validate_config(config)
    if problems:
        raise ProjectionError(
            f"config file '{config_path}' is invalid:\n  - " + "\n  - ".join(problems)
        )
    return config


def run_pipeline(inputs: list[SourceInput], config: dict) -> list[dict]:
    """
    The full detect -> extract -> merge -> project -> validate pipeline.
    Returns a list of projected candidate dicts ready to serialize as JSON.
    """
    extracted = extract_all(inputs)
    logger.info(f"[pipeline] Extracted {len(extracted)} raw profiles across {len(inputs)} source(s)")

    merged = merge_all(extracted)
    logger.info(f"[pipeline] Merged into {len(merged)} canonical candidate(s)")

    projected = project_all(merged, config)
    logger.info(f"[pipeline] Projected {len(projected)} candidate(s) per config")

    return projected


def run_pipeline_to_canonical(inputs: list[SourceInput]) -> list[CandidateProfile]:
    """Run detect -> extract -> merge only, returning canonical profiles
    without projection. Useful for tests and for any caller that wants the
    full internal record rather than a reshaped view."""
    extracted = extract_all(inputs)
    return merge_all(extracted)