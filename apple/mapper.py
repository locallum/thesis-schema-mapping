"""Maps a single HealthKit export record (as produced by HKBridge) to the
canonical vital sign schema in schema/vital_sign.schema.json.
"""

from __future__ import annotations

from typing import Any

_LOINC_BY_IDENTIFIER = {
    "HKQuantityTypeIdentifierHeartRate": "8867-4",
    "HKQuantityTypeIdentifierOxygenSaturation": "2708-6",
    "HKQuantityTypeIdentifierHeartRateVariabilitySDNN": "80404-7",
}

# The HealthKit-native unit each quantity type is always exported in, used to
# sanity-check `raw["unit"]` before applying the fixed conversion below.
_NATIVE_UNIT_BY_CODE = {
    "8867-4": "count/min",
    "2708-6": "%",
    "80404-7": "ms",
}

_CANONICAL_UNIT_BY_CODE = {
    "8867-4": "/min",
    "2708-6": "%",
    "80404-7": "ms",
}

_REQUIRED_FIELDS = (
    "uuid",
    "quantityTypeIdentifier",
    "value",
    "unit",
    "startDate",
    "endDate",
    "metadata",
    "sourceRevision",
)


class MappingError(ValueError):
    """Raised when a raw HealthKit export record can't be mapped to the canonical schema."""


def map_record(raw: dict[str, Any], subject: str) -> dict[str, Any]:
    """Map one raw HealthKit export record to one canonical vital sign observation.

    Raises MappingError if the record is missing required fields, has an
    unrecognized quantityTypeIdentifier, or its unit/value don't match what
    that quantity type is expected to export.
    """
    if not isinstance(raw, dict):
        raise MappingError(f"record must be an object, got {raw!r}")

    missing = [field for field in _REQUIRED_FIELDS if field not in raw]
    if missing:
        raise MappingError(f"record missing required field(s): {', '.join(missing)}")

    identifier = raw["quantityTypeIdentifier"]
    code = _LOINC_BY_IDENTIFIER.get(identifier)
    if code is None:
        raise MappingError(f"unrecognized quantityTypeIdentifier: {identifier!r}")

    expected_unit = _NATIVE_UNIT_BY_CODE[code]
    if raw["unit"] != expected_unit:
        raise MappingError(
            f"unexpected unit {raw['unit']!r} for {identifier!r}, expected {expected_unit!r}"
        )

    if not isinstance(raw["value"], (int, float)) or isinstance(raw["value"], bool):
        raise MappingError(f"value must be numeric, got {raw['value']!r}")

    if not isinstance(raw["metadata"], dict):
        raise MappingError(f"metadata must be an object, got {raw['metadata']!r}")

    canonical: dict[str, Any] = {
        "status": "final",
        "category": "vital-signs",
        "code": code,
        "subject": subject,
        "effectiveDateTime": raw["startDate"],
        "valueQuantity": {
            "value": _normalize_value(code, raw["value"]),
            "unit": _CANONICAL_UNIT_BY_CODE[code],
        },
        "metadata": {
            "uuid": raw["uuid"],
            "sourceRevision": raw["sourceRevision"],
            "recordingMethod": None,
            "lastModifiedTime": None,
            "sourceMetadata": dict(raw["metadata"]),
        },
    }

    if code == "80404-7":
        canonical["method"] = "SDNN"

    return canonical


def _normalize_value(code: str, raw_value: float) -> float:
    if code == "2708-6":
        # HealthKit stores SpO2 as a 0-1 fraction; canonical unit is a 0-100 percent.
        return raw_value * 100
    return raw_value
