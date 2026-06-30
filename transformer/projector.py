"""
projector.py — Runtime config → output reshaping + validation.

This is the projection layer: it NEVER mutates the canonical CandidateProfile.
It reads a config (see config/default_config.json, config/custom_config.json)
and produces a brand-new, reshaped dict per candidate, then validates that
dict against the same config before handing it back.

Config shape (matches the assignment's example):
{
  "fields": [
    { "path": "full_name", "type": "string", "required": true },
    { "path": "primary_email", "from": "emails[0]", "type": "string", "required": true },
    { "path": "phone", "from": "phones[0]", "type": "string", "normalize": "E164" },
    { "path": "skills", "from": "skills[].name", "type": "string[]", "normalize": "canonical" }
  ],
  "include_confidence": true,
  "include_provenance": true,
  "on_missing": "null"   # "null" | "omit" | "error"
}

- "path": dotted output field name (supports nesting via dots, e.g. "location.city")
- "from": dotted/indexed path into the CANONICAL record. If absent, defaults
  to the same string as "path". Supports:
    "emails[0]"        -> first element of a list field
    "skills[].name"    -> map: take .name from every element of a list
    "location.city"    -> nested attribute access
- "type": "string" | "number" | "boolean" | "string[]" | "object" | "any"
  (used for validation, and to decide whether a list-extraction collapses
  to a scalar or stays a list)
- "normalize": optional post-processing applied to the extracted value
  ("E164" for phones, "canonical" for skill names, "YYYY-MM" for dates,
  "ISO2" for country) — re-applies our normalizers in case the requested
  shape pulled a raw nested value that bypassed the source-level normalization
  (e.g. pulling a raw sub-field straight off a list).
- "required": if true and the resolved value is missing, this is always an
  error (overrides on_missing) — required fields cannot be silently omitted.
"""

from __future__ import annotations
import logging
import re
from typing import Any, Optional

from transformer.schema import CandidateProfile
from transformer.normalizers import (
    normalize_phone, normalize_skill, normalize_date, normalize_country,
)

logger = logging.getLogger(__name__)

VALID_TYPES = {"string", "number", "boolean", "string[]", "object", "any"}
VALID_ON_MISSING = {"null", "omit", "error"}

_NORMALIZERS = {
    "E164": normalize_phone,
    "canonical": normalize_skill,
    "YYYY-MM": normalize_date,
    "ISO2": normalize_country,
}

_INDEX_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\[(\d*)\]$")


class ProjectionError(Exception):
    """Raised when on_missing == 'error' and a field cannot be resolved,
    or when a required field is missing, or when the config itself is invalid."""


# ─── Config validation ───────────────────────────────────────────────────────

def validate_config(config: dict) -> list[str]:
    """Return a list of human-readable problems with the config (empty = valid)."""
    problems: list[str] = []

    if not isinstance(config, dict):
        return ["config must be a JSON object"]

    fields = config.get("fields")
    if not isinstance(fields, list) or not fields:
        problems.append("config.fields must be a non-empty list")
        fields = []

    seen_paths: set[str] = set()
    for i, f in enumerate(fields):
        if not isinstance(f, dict):
            problems.append(f"fields[{i}] must be an object")
            continue
        path = f.get("path")
        if not path or not isinstance(path, str):
            problems.append(f"fields[{i}].path is required and must be a string")
        elif path in seen_paths:
            problems.append(f"fields[{i}].path '{path}' is duplicated")
        else:
            seen_paths.add(path)

        ftype = f.get("type", "any")
        if ftype not in VALID_TYPES:
            problems.append(f"fields[{i}].type '{ftype}' is not one of {sorted(VALID_TYPES)}")

        norm = f.get("normalize")
        if norm is not None and norm not in _NORMALIZERS:
            problems.append(f"fields[{i}].normalize '{norm}' is not one of {sorted(_NORMALIZERS)}")

    on_missing = config.get("on_missing", "null")
    if on_missing not in VALID_ON_MISSING:
        problems.append(f"on_missing '{on_missing}' is not one of {sorted(VALID_ON_MISSING)}")

    return problems


# ─── Path resolution against the canonical record ────────────────────────────

def _resolve_path(record: dict, path: str) -> tuple[Any, bool]:
    """
    Resolve a dotted/indexed path against the canonical record dict
    (CandidateProfile.to_dict() output).
    Returns (value, found) — found=False means the path could not be
    resolved at all (missing key / index out of range / wrong shape), which
    is distinct from a found-but-empty value (e.g. an empty list).
    """
    segments = path.split(".")
    current: Any = record
    found = True

    for seg in segments:
        m = _INDEX_RE.match(seg)
        if m:
            key, idx_str = m.group(1), m.group(2)
            if not isinstance(current, dict) or key not in current:
                return None, False
            container = current[key]
            if not isinstance(container, list):
                return None, False
            if idx_str == "":
                # "skills[]" style: caller wants the whole list; defer the
                # ".subfield" mapping to the next segment via a marker.
                current = container
                continue
            idx = int(idx_str)
            if idx >= len(container) or idx < 0:
                return None, False
            current = container[idx]
            continue

        if isinstance(current, list):
            # We're inside a "[].subfield" map: project subfield across the list.
            mapped = []
            for item in current:
                if isinstance(item, dict) and seg in item:
                    mapped.append(item[seg])
            current = mapped
            continue

        if isinstance(current, dict):
            if seg not in current:
                return None, False
            current = current[seg]
            continue

        return None, False

    return current, found


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, (list, str, dict)) and len(value) == 0:
        return True
    return False


def _apply_normalize(value: Any, normalize: Optional[str]) -> Any:
    if normalize is None or value is None:
        return value
    fn = _NORMALIZERS.get(normalize)
    if fn is None:
        return value
    if isinstance(value, list):
        return [fn(v) if isinstance(v, str) else v for v in value]
    if isinstance(value, str):
        return fn(value)
    return value


def _coerce_type(value: Any, ftype: str) -> Any:
    """Best-effort coercion toward the requested type. Never raises."""
    if value is None:
        return None
    if ftype == "string[]":
        if isinstance(value, list):
            return value
        return [value]
    if ftype == "string":
        if isinstance(value, list):
            return value[0] if value else None
        return str(value) if not isinstance(value, str) else value
    if ftype == "number":
        if isinstance(value, list):
            value = value[0] if value else None
        if value is None:
            return None
        try:
            f = float(value)
            return int(f) if f.is_integer() else f
        except (TypeError, ValueError):
            return None
    if ftype == "boolean":
        if isinstance(value, list):
            value = value[0] if value else None
        return bool(value) if value is not None else None
    return value  # "object" / "any": leave as-is


# ─── Projection ──────────────────────────────────────────────────────────────

def _set_nested(out: dict, dotted_path: str, value: Any) -> None:
    parts = dotted_path.split(".")
    current = out
    for p in parts[:-1]:
        current = current.setdefault(p, {})
    current[parts[-1]] = value


def project_profile(profile: CandidateProfile, config: dict) -> dict:
    """
    Project a single canonical CandidateProfile into the shape described by
    `config`. Raises ProjectionError only when on_missing == 'error' (or a
    required field is missing) and the offending field is actually absent.
    """
    record = profile.to_dict()
    fields = config.get("fields", [])
    on_missing = config.get("on_missing", "null")
    include_confidence = bool(config.get("include_confidence", False))
    include_provenance = bool(config.get("include_provenance", False))

    out: dict[str, Any] = {}

    for f in fields:
        out_path = f["path"]
        from_path = f.get("from", out_path)
        ftype = f.get("type", "any")
        normalize = f.get("normalize")
        required = bool(f.get("required", False))

        value, found = _resolve_path(record, from_path)
        value = _apply_normalize(value, normalize)
        value = _coerce_type(value, ftype)

        missing = (not found) or _is_empty(value)

        if missing:
            if required:
                raise ProjectionError(
                    f"required field '{out_path}' (from '{from_path}') is missing "
                    f"for candidate '{profile.candidate_id}'"
                )
            if on_missing == "error":
                raise ProjectionError(
                    f"field '{out_path}' (from '{from_path}') is missing for "
                    f"candidate '{profile.candidate_id}' and on_missing='error'"
                )
            if on_missing == "omit":
                continue
            # on_missing == "null"
            _set_nested(out, out_path, None)
            continue

        _set_nested(out, out_path, value)

    if include_confidence:
        out["overall_confidence"] = profile.overall_confidence

    if include_provenance:
        out["provenance"] = record["provenance"]

    return out


def project_all(profiles: list[CandidateProfile], config: dict) -> list[dict]:
    """
    Project every profile. A ProjectionError for one candidate is logged and
    that candidate is skipped (degrade gracefully) rather than aborting the
    whole run — UNLESS the error is config-shaped, which validate_config
    should already have caught before this is ever called.
    """
    problems = validate_config(config)
    if problems:
        raise ProjectionError("invalid config: " + "; ".join(problems))

    results: list[dict] = []
    for profile in profiles:
        try:
            results.append(project_profile(profile, config))
        except ProjectionError as e:
            logger.error(f"[projector] Skipping candidate {profile.candidate_id}: {e}")
    return results