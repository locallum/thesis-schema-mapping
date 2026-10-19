"""Tests for android/mapper.py and android/cli.py (Health Connect to canonical schema)."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import jsonschema
import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from android.cli import run  # noqa: E402
from android.mapper import MappingError, map_record  # noqa: E402

_FIXTURES = _ROOT / "tests" / "fixtures" / "android"
_SCHEMA = json.loads((_ROOT / "schema" / "vital_sign.schema.json").read_text(encoding="utf-8"))
_SUBJECT = "mrn-test"


def _load(kind: str) -> list[dict]:
    return json.loads(next(_FIXTURES.glob(f"{kind}*.json")).read_text(encoding="utf-8"))


def _map_all(kind: str) -> list[dict]:
    return [obs for raw in _load(kind) for obs in map_record(raw, subject=_SUBJECT)]


@pytest.fixture(scope="module")
def validator():
    return jsonschema.Draft202012Validator(_SCHEMA, format_checker=jsonschema.FormatChecker())


# --- every mapped record satisfies the schema ---

@pytest.mark.parametrize("kind", ["heart_rate", "spo2", "hrv"])
def test_mapped_records_are_schema_valid(kind, validator):
    for obs in _map_all(kind):
        validator.validate(obs)


def test_format_checker_actually_checks_date_time(validator):
    # jsonschema skips `format: date-time` unless rfc3339-validator is installed.
    obs = _map_all("spo2")[0]
    obs["effectiveDateTime"] = "not-a-date"
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(obs)


# --- heart rate ---

def test_heart_rate_one_observation_per_sample():
    hr = _map_all("heart_rate")
    assert [o["valueQuantity"] for o in hr] == [
        {"value": 72.0, "unit": "/min"},
        {"value": 98.0, "unit": "/min"},
        {"value": 99.0, "unit": "/min"},
        {"value": 100.0, "unit": "/min"},
    ]
    assert {o["code"] for o in hr} == {"8867-4"}


def test_multi_sample_record_is_traceable():
    raw = _load("heart_rate")[1]  # HR-013, three samples
    obs = map_record(raw, subject=_SUBJECT)
    assert [o["effectiveDateTime"] for o in obs] == [
        "2026-08-14T08:10:00Z", "2026-08-14T08:11:00Z", "2026-08-14T08:12:00Z",
    ]
    assert {o["metadata"]["uuid"] for o in obs} == {raw["metadata"]["id"]}
    src = [o["metadata"]["sourceMetadata"] for o in obs]
    assert [s["sampleIndex"] for s in src] == [0, 1, 2]
    assert {s["clientRecordId"] for s in src} == {"HR-013"}
    assert src[0]["recordStartTime"] == "2026-08-14T08:10:00Z"
    assert src[0]["recordEndTime"] == "2026-08-14T08:12:00Z"


def test_empty_samples_rejected():
    raw = copy.deepcopy(_load("heart_rate")[0])
    raw["samples"] = []
    with pytest.raises(MappingError, match="non-empty"):
        map_record(raw, subject=_SUBJECT)


# --- SpO2 ---

def test_spo2_passes_through_on_0_to_100_scale():
    spo2 = _map_all("spo2")
    assert [o["valueQuantity"] for o in spo2] == [
        {"value": 97.0, "unit": "%"},
        {"value": 89.5, "unit": "%"},
    ]
    assert {o["code"] for o in spo2} == {"2708-6"}
    assert all("method" not in o for o in spo2)


def test_device_and_recording_method():
    meta = _map_all("spo2")[1]["metadata"]
    assert meta["sourceRevision"] == "dataOrigin=com.example.healthconnectbridge; device=Google Pixel Watch 3 (watch)"
    assert meta["recordingMethod"] == "AUTOMATICALLY_RECORDED"
    assert meta["sourceMetadata"]["device"]["typeName"] == "watch"
    assert meta["sourceMetadata"]["zoneOffset"] == "+10:00"


# --- HRV ---

def test_hrv_is_rmssd():
    (obs,) = _map_all("hrv")
    assert obs["code"] == "80404-7"
    assert obs["method"] == "RMSSD"
    assert obs["valueQuantity"] == {"value": 42.5, "unit": "ms"}


# --- shared metadata ---

def test_named_metadata_fields():
    obs = _map_all("spo2")[0]
    assert obs["status"] == "final"
    assert obs["category"] == "vital-signs"
    assert obs["subject"] == _SUBJECT
    meta = obs["metadata"]
    assert meta["uuid"] == "b7d1e2f3-4a5b-4c6d-8e9f-0a1b2c3d4e01"
    assert meta["recordingMethod"] == "MANUAL_ENTRY"
    assert meta["lastModifiedTime"] == "2026-10-12T07:58:31.988Z"
    assert meta["sourceMetadata"]["platform"] == "health_connect"
    assert meta["sourceMetadata"]["clientRecordId"] == "SPO2-001"
    assert meta["sourceMetadata"]["recordType"] == "OxygenSaturationRecord"


@pytest.mark.parametrize("raw_time, expected", [
    ("2026-08-14T08:00:00Z", "2026-08-14T08:00:00Z"),
    ("2026-08-14T08:00:00.123Z", "2026-08-14T08:00:00.123Z"),
    ("2026-08-14T08:00:00.123456789Z", "2026-08-14T08:00:00.123Z"),
    ("2026-08-14T18:00:00+10:00", "2026-08-14T08:00:00Z"),
])
def test_timestamps_normalised_to_utc(raw_time, expected):
    raw = copy.deepcopy(_load("hrv")[0])
    raw["time"] = raw_time
    assert map_record(raw, subject=_SUBJECT)[0]["effectiveDateTime"] == expected


# --- error handling ---

@pytest.mark.parametrize("mutate, message", [
    (lambda r: r.pop("percentage"), "can't identify record type"),
    (lambda r: r.update(percentage="97"), "must be numeric"),
    (lambda r: r.update(percentage=True), "must be numeric"),
    (lambda r: r.update(metadata="x"), "metadata must be an object"),
    (lambda r: r["metadata"].pop("id"), "missing required field"),
    (lambda r: r.update(time="yesterday"), "invalid timestamp"),
    (lambda r: r.update(time="2026-08-14T08:00:00"), "no UTC offset"),
])
def test_bad_records_raise(mutate, message):
    raw = copy.deepcopy(_load("spo2")[0])
    mutate(raw)
    with pytest.raises(MappingError, match=message):
        map_record(raw, subject=_SUBJECT)


def test_non_object_record_raises():
    with pytest.raises(MappingError):
        map_record(["not", "a", "record"], subject=_SUBJECT)  # type: ignore[arg-type]


# --- CLI ---

@pytest.mark.parametrize("kind, expected_count", [("heart_rate", 4), ("spo2", 2), ("hrv", 1)])
def test_cli_maps_file(tmp_path, kind, expected_count):
    src = next(_FIXTURES.glob(f"{kind}*.json"))
    out = tmp_path / "out.json"
    assert run([str(src), "--subject", _SUBJECT, "--out", str(out)]) == 0
    assert len(json.loads(out.read_text(encoding="utf-8"))) == expected_count


def test_cli_skips_bad_record_and_returns_1(tmp_path, capsys):
    records = _load("spo2")
    del records[0]["percentage"]
    src = tmp_path / "spo2_bad.json"
    src.write_text(json.dumps(records), encoding="utf-8")
    out = tmp_path / "out.json"
    assert run([str(src), "--subject", _SUBJECT, "--out", str(out)]) == 1
    assert len(json.loads(out.read_text(encoding="utf-8"))) == 1
    assert "skipping record 0 (SPO2-001)" in capsys.readouterr().err
