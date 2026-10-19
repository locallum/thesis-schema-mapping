"""Maps a single Health Connect export record (as produced by the Android
HealthConnectBridge app) to the canonical vital sign schema

Counterpart of apple/mapper.py. One difference in the return type: a Health
Connect HeartRateRecord is an interval holding one or more samples, so
map_record() returns a *list* of canonical observations (one per sample).
SpO2 and HRV records always map to a list of exactly one.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

_HEART_RATE = "8867-4"
_SPO2 = "2708-6"
_HRV = "80404-7"

_CANONICAL_UNIT_BY_CODE = {
    _HEART_RATE: "/min",
    _SPO2: "%",
    _HRV: "ms",
}

# The field that identifies each Health Connect record type in the export,
# and the record type name it corresponds to.
_RECORD_TYPE_BY_CODE = {
    _HEART_RATE: "HeartRateRecord",
    _SPO2: "OxygenSaturationRecord",
    _HRV: "HeartRateVariabilityRmssdRecord",
}

# androidx.health.connect.client.records.metadata.Metadata.RECORDING_METHOD_*
_RECORDING_METHOD_BY_INT = {
    0: "RECORDING_METHOD_UNKNOWN",
    1: "ACTIVELY_RECORDED",
    2: "AUTOMATICALLY_RECORDED",
    3: "MANUAL_ENTRY",
}

# androidx.health.connect.client.records.metadata.Device.TYPE_*
_DEVICE_TYPE_NAMES = {
    0: "unknown",
    1: "watch",
    2: "phone",
    3: "scale",
    4: "ring",
    5: "head_mounted",
    6: "fitness_band",
    7: "chest_strap",
    8: "smart_display",
}

_REQUIRED_METADATA_FIELDS = ("id", "dataOrigin", "recordingMethod", "lastModifiedTime")

_FRACTION = re.compile(r"\.(\d+)")


class MappingError(ValueError):
    """Raised when a raw Health Connect export record can't be mapped to the canonical schema."""


def map_record(raw: dict[str, Any], subject: str) -> list[dict[str, Any]]:
    """Map one raw Health Connect export record to canonical vital sign observations.

    Returns one observation per heart-rate sample, or exactly one for SpO2 / HRV.
    Raises MappingError if the record is missing required fields, can't be
    identified as one of the three supported record types, or holds a
    non-numeric value.
    """
    if not isinstance(raw, dict):
        raise MappingError(f"record must be an object, got {raw!r}")

    code = _detect_code(raw)
    metadata = raw.get("metadata")
    if not isinstance(metadata, dict):
        raise MappingError(f"metadata must be an object, got {metadata!r}")
    missing = [field for field in _REQUIRED_METADATA_FIELDS if field not in metadata]
    if missing:
        raise MappingError(f"metadata missing required field(s): {', '.join(missing)}")

    if code == _HEART_RATE:
        return _map_heart_rate(raw, metadata, subject)

    time = _require(raw, "time")
    value_field = "percentage" if code == _SPO2 else "heartRateVariabilityMillis"
    value = _numeric(_require(raw, value_field), value_field)
    extra = {"zoneOffset": raw.get("zoneOffset")}

    # SpO2: Health Connect's Percentage is already 0-100, the canonical scale,
    # so it is passed through unchanged (HealthKit's 0-1 fraction is x100'd in apple/).
    canonical = _observation(code, value, time, subject, metadata, extra)
    if code == _HRV:
        # Health Connect has no SDNN record type; its only HRV record is RMSSD.
        canonical["method"] = "RMSSD"
    return [canonical]


def _map_heart_rate(raw: dict[str, Any], metadata: dict[str, Any], subject: str) -> list[dict[str, Any]]:
    samples = _require(raw, "samples")
    if not isinstance(samples, list) or not samples:
        raise MappingError(f"samples must be a non-empty array, got {samples!r}")

    observations = []
    for index, sample in enumerate(samples):
        if not isinstance(sample, dict):
            raise MappingError(f"samples[{index}] must be an object, got {sample!r}")
        extra = {
            # Samples have no id of their own, so every sample shares the record's
            # uuid; sampleIndex + clientRecordId tell them apart.
            "sampleIndex": index,
            "sampleCount": len(samples),
            "recordStartTime": _to_utc_iso(raw.get("startTime")),
            "recordEndTime": _to_utc_iso(raw.get("endTime")),
            "startZoneOffset": raw.get("startZoneOffset"),
            "endZoneOffset": raw.get("endZoneOffset"),
        }
        value = _numeric(_require(sample, "beatsPerMinute", f"samples[{index}]"), f"samples[{index}].beatsPerMinute")
        time = _require(sample, "time", f"samples[{index}]")
        observations.append(_observation(_HEART_RATE, value, time, subject, metadata, extra))
    return observations


def _observation(
    code: str,
    value: float,
    time: str,
    subject: str,
    metadata: dict[str, Any],
    extra: dict[str, Any],
) -> dict[str, Any]:
    return {
        "status": "final",
        "category": "vital-signs",
        "code": code,
        "subject": subject,
        "effectiveDateTime": _to_utc_iso(time),
        "valueQuantity": {
            "value": float(value),
            "unit": _CANONICAL_UNIT_BY_CODE[code],
        },
        "metadata": {
            "uuid": str(metadata["id"]),
            "sourceRevision": _source_revision(metadata),
            "recordingMethod": _recording_method(metadata["recordingMethod"]),
            "lastModifiedTime": _to_utc_iso(metadata["lastModifiedTime"]),
            "sourceMetadata": _source_metadata(code, metadata, extra),
        },
    }


def _detect_code(raw: dict[str, Any]) -> str:
    if "samples" in raw:
        return _HEART_RATE
    if "percentage" in raw:
        return _SPO2
    if "heartRateVariabilityMillis" in raw:
        return _HRV
    raise MappingError(
        "can't identify record type: expected one of 'samples', 'percentage', "
        f"'heartRateVariabilityMillis', got fields {sorted(raw)}"
    )


def _source_metadata(code: str, metadata: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    """Everything from the Health Connect metadata not promoted to a named field."""
    device = metadata.get("device")
    return {
        "platform": "health_connect",
        "recordType": _RECORD_TYPE_BY_CODE[code],
        "clientRecordId": metadata.get("clientRecordId"),
        "clientRecordVersion": metadata.get("clientRecordVersion"),
        "dataOrigin": metadata.get("dataOrigin"),
        "device": None if not device else {
            "manufacturer": device.get("manufacturer"),
            "model": device.get("model"),
            "type": device.get("type"),
            "typeName": _DEVICE_TYPE_NAMES.get(device.get("type")),
        },
        "recordingMethodRaw": metadata.get("recordingMethod"),
        **extra,
    }


def _source_revision(metadata: dict[str, Any]) -> str:
    """Opaque description of the writing app and device, analogous to HKSourceRevision."""
    parts = [f"dataOrigin={metadata.get('dataOrigin') or 'unknown'}"]
    device = metadata.get("device")
    if device:
        type_name = _DEVICE_TYPE_NAMES.get(device.get("type"), f"type_{device.get('type')}")
        name = " ".join(str(p) for p in (device.get("manufacturer"), device.get("model")) if p)
        parts.append(f"device={name + ' ' if name else ''}({type_name})")
    else:
        parts.append("device=null")
    return "; ".join(parts)


def _recording_method(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise MappingError(f"recordingMethod must be an integer, got {value!r}")
    return _RECORDING_METHOD_BY_INT.get(value, "RECORDING_METHOD_UNKNOWN")


def _to_utc_iso(value: Any) -> str | None:
    """Normalise a java.time.Instant / ISO 8601 string to UTC with a trailing Z.

    Whole seconds when there's no sub-second part (matching Swift's
    ISO8601DateFormatter on the HealthKit side), otherwise milliseconds.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise MappingError(f"timestamp must be a string, got {value!r}")
    text = value.strip()
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    # Instant.toString() can emit up to 9 fractional digits; Python accepts 6.
    text = _FRACTION.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), text, count=1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise MappingError(f"invalid timestamp {value!r}: {exc}") from exc
    if parsed.tzinfo is None:
        raise MappingError(f"timestamp has no UTC offset: {value!r}")
    parsed = parsed.astimezone(timezone.utc)
    if parsed.microsecond:
        return parsed.strftime("%Y-%m-%dT%H:%M:%S.") + f"{parsed.microsecond // 1000:03d}Z"
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def _require(obj: dict[str, Any], key: str, where: str = "record") -> Any:
    if key not in obj or obj[key] is None:
        raise MappingError(f"{where} missing required field: {key}")
    return obj[key]


def _numeric(value: Any, name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise MappingError(f"{name} must be numeric, got {value!r}")
    return value
