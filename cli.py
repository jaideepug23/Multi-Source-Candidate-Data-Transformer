#!/usr/bin/env python3
"""
cli.py — Command-line interface for the multi-source candidate transformer.

Usage:
    python cli.py --input samples/recruiter.csv --input samples/ats_blob.json \
                   --config config/default_config.json \
                   --output out.json

    # Explicit kind override when auto-detection can't tell .txt apart:
    # GitHub unstructured source (URL):
    python cli.py --input https://github.com/octocat \
                   --input samples/recruiter.csv \
                   --config config/default_config.json

Input format for --input is "path_or_url" or "path_or_url:kind", where kind
is one of: csv, ats_json, github, resume. The ":kind" suffix is only
needed when auto-detection is ambiguous.

NOTE: LinkedIn is intentionally not supported as a live source. LinkedIn
provides no public API for profile data and automated scraping violates
their Terms of Service. LinkedIn profile URLs stored in CSV/ATS data are
preserved in the output schema (links.linkedin) — only live LinkedIn
ingestion is excluded.

If --output is omitted, JSON is printed to stdout.
"""

from __future__ import annotations
import argparse
import json
import logging
import sys
from pathlib import Path

# Allow running as `python cli.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from transformer.pipeline import SourceInput, run_pipeline, load_config
from transformer.projector import ProjectionError

VALID_KINDS = {"csv", "ats_json", "github", "resume"}


def _parse_input_spec(spec: str) -> SourceInput:
    """Parse 'path[:kind]' into a SourceInput."""
    if ":" in spec:
        # Careful: Windows paths and URLs ("https://") also contain colons.
        # Only treat the LAST colon-separated token as a kind if it's a
        # recognized kind string; otherwise treat the whole spec as a path.
        maybe_path, _, maybe_kind = spec.rpartition(":")
        if maybe_kind in VALID_KINDS and maybe_path:
            return SourceInput(path=maybe_path, kind=maybe_kind)
    return SourceInput(path=spec)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="Multi-source candidate data transformer — detect, extract, "
                    "normalize, merge, and project candidate profiles into a "
                    "canonical or custom JSON shape.",
    )
    parser.add_argument(
        "--input", "-i",
        action="append",
        dest="inputs",
        required=True,
        metavar="PATH_OR_URL[:KIND]",
        help="An input source: a file path or URL, optionally suffixed with "
             "':csv', ':ats_json', ':github', or ':resume' when auto-detection "
             "is ambiguous. Repeat --input for multiple sources.",
    )
    parser.add_argument(
        "--config", "-c",
        default="config/default_config.json",
        help="Path to a runtime output config JSON file (default: %(default)s).",
    )
    parser.add_argument(
        "--output", "-o",
        default=None,
        help="Path to write the resulting JSON. If omitted, prints to stdout.",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable INFO-level logging to stderr (warnings/errors are always shown).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        config = load_config(args.config)
    except ProjectionError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2

    inputs = [_parse_input_spec(spec) for spec in args.inputs]

    try:
        results = run_pipeline(inputs, config)
    except ProjectionError as e:
        print(f"Pipeline error: {e}", file=sys.stderr)
        return 2

    output_json = json.dumps(results, indent=2, ensure_ascii=False)

    if args.output:
        Path(args.output).write_text(output_json, encoding="utf-8")
        print(f"Wrote {len(results)} candidate(s) to {args.output}", file=sys.stderr)
    else:
        print(output_json)

    return 0


if __name__ == "__main__":
    sys.exit(main())