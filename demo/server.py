"""Local demo server: finds HealthKit / Health Connect exports in ~/Downloads,
maps them with the real mappers in apple/ and android/, and serves a UI for
inspecting and comparing the canonical output.

Nothing is reimplemented here. Mapping is apple.mapper.map_record and
android.mapper.map_record; validation is the same schema and validator the two
CLIs use. This module only locates files, calls them, and diffs the results.

Usage (from the repo root):
    python -m demo.server
    python -m demo.server --port 8765 --watch ~/Downloads
"""

from __future__ import annotations

import argparse
import json
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import jsonschema
from flask import Flask, jsonify, request, send_from_directory

from android import mapper as android_mapper
from apple import mapper as apple_mapper

_ROOT = Path(__file__).resolve().parent.parent
_SCHEMA_PATH = _ROOT / "schema" / "vital_sign.schema.json"
# Where the Android stand-in exports live when no real export is in the watched
# folder. These are real Health Connect exports from the bridge app, generated
# from the shared spec in test_data/test_cases.json — not the synthetic
# hand-written records under tests/fixtures/android/, which exist for the unit
# tests and are not representative of device output.
_FIXTURE_DIR = _ROOT / "exports" / "android"

KINDS = ("heart_rate", "spo2", "hrv")

KIND_LABELS = {
    "heart_rate": "Heart rate",
    "spo2": "Blood oxygen",
    "hrv": "Heart rate variability",
}

# LOINC code -> canonical kind slug, so both platforms land on the same key.
_KIND_BY_CODE = {"8867-4": "heart_rate", "2708-6": "spo2", "80404-7": "hrv"}

_APPLE_KIND_BY_IDENTIFIER = {
    "HKQuantityTypeIdentifierHeartRate": "heart_rate",
    "HKQuantityTypeIdentifierOxygenSaturation": "spo2",
    "HKQuantityTypeIdentifierHeartRateVariabilitySDNN": "hrv",
}

# Clinical fields that must agree across platforms for records to be comparable.
_CLINICAL_FIELDS = ("status", "category", "code", "subject", "method")


def _load_schema() -> dict[str, Any]:
    with _SCHEMA_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def _validator() -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(_load_schema(), format_checker=jsonschema.FormatChecker())


# --------------------------------------------------------------------------
# detection
# --------------------------------------------------------------------------

def detect(records: list[Any]) -> tuple[str | None, str | None]:
    """Identify (platform, kind) from a record's shape rather than its filename.

    Both bridge apps name their exports the same way, so the filename can't be
    trusted. Returns (None, None) if the file isn't a recognised export.
    """
    first = next((r for r in records if isinstance(r, dict)), None)
    if first is None:
        return None, None

    if "quantityTypeIdentifier" in first:
        return "apple", _APPLE_KIND_BY_IDENTIFIER.get(first["quantityTypeIdentifier"])

    try:
        code = android_mapper._detect_code(first)
    except android_mapper.MappingError:
        return None, None
    return "android", _KIND_BY_CODE.get(code)


def scan(folder: Path) -> list[dict[str, Any]]:
    """Every recognised export in `folder`, newest first."""
    found = []
    for path in sorted(folder.glob("*.json")):
        try:
            records = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(records, list) or not records:
            continue
        platform, kind = detect(records)
        if platform is None or kind is None:
            continue
        stat = path.stat()
        found.append({
            "path": str(path),
            "filename": path.name,
            "platform": platform,
            "kind": kind,
            "raw_count": len(records),
            "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            "mtime": stat.st_mtime,
            "source": "downloads",
        })
    return sorted(found, key=lambda f: f["mtime"], reverse=True)


def newest_per_slot(files: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    """Pick one file per (platform, kind) — the most recently modified."""
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for f in files:
        slot = (f["platform"], f["kind"])
        if slot not in best or f["mtime"] > best[slot]["mtime"]:
            best[slot] = f
    return best


def fixture_fallbacks() -> dict[tuple[str, str], dict[str, Any]]:
    """Stand-in Android exports, used only when the watched folder has none.

    Filenames are timestamped, so each kind is matched by prefix and the newest
    wins — the same convention android/pull.py uses. `watch_*` exports are
    skipped: a streamed session isn't the paired test dataset, and it would
    otherwise shadow heart_rate_* for the heart-rate slot.

    Everything here is flagged `source: "fixture"` so the UI can never present
    it as a freshly pulled export.
    """
    out: dict[tuple[str, str], dict[str, Any]] = {}
    if not _FIXTURE_DIR.is_dir():
        return out

    for kind in KINDS:
        candidates = [
            p for p in _FIXTURE_DIR.glob(f"{kind}*.json")
            if p.name == f"{kind}.json" or p.name.startswith(f"{kind}_")
        ]
        if not candidates:
            continue
        path = max(candidates, key=lambda p: p.stat().st_mtime)
        try:
            records = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(records, list) or not records:
            continue
        out[("android", kind)] = {
            "path": str(path),
            "filename": str(path.relative_to(_ROOT)),
            "platform": "android",
            "kind": kind,
            "raw_count": len(records),
            "modified": None,
            "mtime": 0,
            "source": "fixture",
        }
    return out


# --------------------------------------------------------------------------
# mapping
# --------------------------------------------------------------------------

def map_file(entry: dict[str, Any], subject: str, validator) -> dict[str, Any]:
    """Run the platform's real mapper over one export file.

    Mirrors the CLIs: a record that fails mapping or validation is skipped with
    a warning rather than aborting the file.
    """
    raw_records = json.loads(Path(entry["path"]).read_text(encoding="utf-8"))
    is_apple = entry["platform"] == "apple"
    mapper = apple_mapper if is_apple else android_mapper

    canonical: list[dict[str, Any]] = []
    warnings: list[str] = []

    for index, raw in enumerate(raw_records):
        try:
            mapped = mapper.map_record(raw, subject=subject)
            produced = [mapped] if is_apple else list(mapped)
            for record in produced:
                validator.validate(record)
        except mapper.MappingError as exc:
            warnings.append(f"record {index}: {exc}")
            continue
        except jsonschema.ValidationError as exc:
            warnings.append(f"record {index}: mapper produced invalid output: {exc.message}")
            continue
        canonical.extend(produced)

    values = [r["valueQuantity"]["value"] for r in canonical]
    return {
        **entry,
        "raw": raw_records,
        "canonical": canonical,
        "warnings": warnings,
        "raw_count": len(raw_records),
        "canonical_count": len(canonical),
        "skipped": len(warnings),
        "exit_code": 1 if warnings else 0,
        "unit": canonical[0]["valueQuantity"]["unit"] if canonical else None,
        "method": canonical[0].get("method") if canonical else None,
        "value_min": min(values) if values else None,
        "value_max": max(values) if values else None,
    }


# --------------------------------------------------------------------------
# comparison
# --------------------------------------------------------------------------

def _flat(record: dict[str, Any]) -> dict[str, Any]:
    """The comparable surface of a canonical record."""
    flat = {f: record.get(f, None) for f in _CLINICAL_FIELDS}
    flat["unit"] = record["valueQuantity"]["unit"]
    return flat


def compare(apple: dict[str, Any] | None, android: dict[str, Any] | None) -> dict[str, Any] | None:
    """Field-by-field comparison of one kind across both platforms."""
    if not apple or not android or not apple["canonical"] or not android["canonical"]:
        return None

    a, b = apple["canonical"][0], android["canonical"][0]
    fa, fb = _flat(a), _flat(b)

    fields = []
    for name in list(_CLINICAL_FIELDS) + ["unit"]:
        av, bv = fa.get(name), fb.get(name)
        fields.append({
            "field": name,
            "apple": av,
            "android": bv,
            "agree": av == bv,
            # method legitimately differs by platform capability, not by error
            "expected": name == "method" and av != bv,
        })

    nulls = []
    for name in ("recordingMethod", "lastModifiedTime"):
        nulls.append({
            "field": name,
            "apple": a["metadata"].get(name),
            "android": b["metadata"].get(name),
        })

    return {
        "fields": fields,
        "shape": {
            "top_level_identical": sorted(a) == sorted(b),
            "metadata_identical": sorted(a["metadata"]) == sorted(b["metadata"]),
            "apple_keys": sorted(a),
            "android_keys": sorted(b),
        },
        "nulls": nulls,
        "divergences": [f["field"] for f in fields if not f["agree"]],
        "counts": {"apple": apple["canonical_count"], "android": android["canonical_count"]},
    }


def build_state(folder: Path, subject: str, use_fixtures: bool) -> dict[str, Any]:
    validator = _validator()
    files = scan(folder)
    slots = newest_per_slot(files)

    fixtures_used = []
    if use_fixtures:
        for slot, entry in fixture_fallbacks().items():
            if slot not in slots:
                slots[slot] = entry
                fixtures_used.append(entry["kind"])

    mapped: dict[tuple[str, str], dict[str, Any]] = {}
    errors = []
    for slot, entry in slots.items():
        try:
            mapped[slot] = map_file(entry, subject, validator)
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{entry['filename']}: {exc}")

    kinds = {}
    for kind in KINDS:
        apple = mapped.get(("apple", kind))
        android = mapped.get(("android", kind))
        kinds[kind] = {
            "kind": kind,
            "label": KIND_LABELS[kind],
            "apple": apple,
            "android": android,
            "comparison": compare(apple, android),
        }

    total = sum(m["canonical_count"] for m in mapped.values())
    skipped = sum(m["skipped"] for m in mapped.values())

    # Every distinct subject present in the output, so the UI can say what is
    # actually there when a filter matches nothing.
    subjects = sorted({r["subject"] for m in mapped.values() for r in m["canonical"]})

    return {
        "subject": subject,
        "subjects": subjects,
        "folder": str(folder),
        "generated": datetime.now(timezone.utc).isoformat(),
        "discovered": files,
        "kinds": kinds,
        "fixtures_used": fixtures_used,
        "errors": errors,
        "totals": {
            "files": len(mapped),
            "canonical": total,
            "skipped": skipped,
            "platforms": sorted({slot[0] for slot in mapped}),
            # per-platform, so the run sequence can report each mapper separately
            "by_platform": {
                p: {
                    "files": sum(1 for s in mapped if s[0] == p),
                    "raw": sum(m["raw_count"] for s, m in mapped.items() if s[0] == p),
                    "canonical": sum(m["canonical_count"] for s, m in mapped.items() if s[0] == p),
                }
                for p in ("apple", "android")
            },
            "paired": sum(1 for k in KINDS if kinds[k]["comparison"]),
        },
    }


# --------------------------------------------------------------------------
# app
# --------------------------------------------------------------------------

app = Flask(__name__, static_folder=None)
_config: dict[str, Any] = {"folder": Path.home() / "Downloads", "subject": "mrn-00123"}


_BRAND_DIR = Path(__file__).resolve().parent / "brand"
_LOGO_STEMS = ("unsw-logo", "logo")
_LOGO_TYPES = {".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg",
               ".jpeg": "image/jpeg", ".webp": "image/webp"}


@app.get("/")
def index():
    return send_from_directory(Path(__file__).resolve().parent, "app.html")


@app.get("/brand/logo")
def brand_logo():
    """Serve an institutional logo if one has been dropped into demo/brand/.

    Nothing is bundled: the real UNSW mark is a trademark and has to come from
    the university's own brand assets. Absent a file, the UI falls back to a
    plain type lockup rather than an imitation.
    """
    if _BRAND_DIR.is_dir():
        for stem in _LOGO_STEMS:
            for ext, mime in _LOGO_TYPES.items():
                candidate = _BRAND_DIR / f"{stem}{ext}"
                if candidate.is_file():
                    return send_from_directory(_BRAND_DIR, candidate.name, mimetype=mime)
    return jsonify({"error": "no logo installed"}), 404


@app.get("/api/scan")
def api_scan():
    """Phase one: what's in the folder, before any mapping runs."""
    folder = Path(request.args.get("folder") or _config["folder"]).expanduser()
    if not folder.is_dir():
        return jsonify({"error": f"not a folder: {folder}"}), 400

    files = scan(folder)
    selected = list(newest_per_slot(files).values())
    return jsonify({
        "folder": str(folder),
        "discovered": files,
        "selected": selected,
        "counts": {
            "files": len(files),
            "apple": sum(1 for f in selected if f["platform"] == "apple"),
            "android": sum(1 for f in selected if f["platform"] == "android"),
        },
    })


@app.get("/api/state")
def api_state():
    """Phase two: run the mappers and assign the subject supplied for this run."""
    subject = (request.args.get("subject") or "").strip() or _config["subject"]
    folder = Path(request.args.get("folder") or _config["folder"]).expanduser()
    use_fixtures = request.args.get("fixtures", "1") != "0"

    if not folder.is_dir():
        return jsonify({"error": f"not a folder: {folder}"}), 400
    return jsonify(build_state(folder, subject, use_fixtures))


@app.get("/api/schema")
def api_schema():
    return jsonify(_load_schema())


def main() -> None:
    parser = argparse.ArgumentParser(description="Local demo UI for the vital sign mappers.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--watch", type=Path, default=Path.home() / "Downloads",
                        help="Folder to scan for exports (default: ~/Downloads).")
    parser.add_argument("--subject", default="mrn-00123", help="Default patient ID / synthetic MRN.")
    parser.add_argument("--no-open", action="store_true", help="Don't open a browser on start.")
    args = parser.parse_args()

    _config["folder"] = args.watch.expanduser()
    _config["subject"] = args.subject

    url = f"http://127.0.0.1:{args.port}/"
    print(f"Watching  {_config['folder']}")
    print(f"Subject   {args.subject}")
    print(f"Serving   {url}\n")
    if not args.no_open:
        webbrowser.open(url)
    app.run(port=args.port, debug=False)


if __name__ == "__main__":
    main()
