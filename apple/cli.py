"""CLI entrypoint: maps one HealthKit export file to the canonical vital sign schema.

Usage:
    python -m apple.cli --subject mrn-00123 heart_rate_export.json --out heart_rate_fhir.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import jsonschema

from apple.mapper import MappingError, map_record

_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema" / "vital_sign.schema.json"


def _load_schema() -> dict[str, Any]:
    with _SCHEMA_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Map a HealthKit export file (from HKBridge) to the canonical vital sign schema."
    )
    parser.add_argument("input", type=Path, help="Path to a HealthKit export JSON file (one vital-sign kind).")
    parser.add_argument("--subject", required=True, help="Patient ID / synthetic MRN to attach to every record.")
    parser.add_argument("--out", type=Path, default=None, help="Output file path. Defaults to stdout.")
    return parser.parse_args(argv)


def _describe(raw: Any) -> str:
    if isinstance(raw, dict):
        return str(raw.get("uuid", "?"))
    return repr(raw)[:50]


def run(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    try:
        raw_records = json.loads(args.input.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: couldn't read {args.input}: {exc}", file=sys.stderr)
        return 1

    if not isinstance(raw_records, list):
        print(f"error: {args.input} must contain a JSON array of records", file=sys.stderr)
        return 1

    validator = jsonschema.Draft202012Validator(_load_schema(), format_checker=jsonschema.FormatChecker())

    canonical_records: list[dict[str, Any]] = []
    skipped = 0

    for index, raw in enumerate(raw_records):
        try:
            canonical = map_record(raw, subject=args.subject)
            validator.validate(canonical)
        except MappingError as exc:
            print(f"warning: skipping record {index} ({_describe(raw)}): {exc}", file=sys.stderr)
            skipped += 1
            continue
        except jsonschema.ValidationError as exc:
            print(
                f"warning: skipping record {index} ({_describe(raw)}): mapper produced invalid output: {exc.message}",
                file=sys.stderr,
            )
            skipped += 1
            continue
        canonical_records.append(canonical)

    output = json.dumps(canonical_records, indent=2)
    if args.out is not None:
        args.out.write_text(output + "\n", encoding="utf-8")
    else:
        print(output)

    if skipped:
        print(f"warning: skipped {skipped} of {len(raw_records)} record(s)", file=sys.stderr)
        return 1

    return 0


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
