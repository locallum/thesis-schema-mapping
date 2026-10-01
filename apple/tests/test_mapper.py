import json
from pathlib import Path

import jsonschema
import pytest

from apple.cli import run
from apple.mapper import MappingError, map_record

FIXTURES = Path(__file__).parent / "fixtures"
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schema" / "vital_sign.schema.json"
SUBJECT = "mrn-00123"


def _load_fixture(name: str) -> list[dict]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _validator() -> jsonschema.Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())


@pytest.fixture(scope="module")
def validator() -> jsonschema.Draft202012Validator:
    return _validator()


def test_heart_rate_maps_and_validates(validator):
    raw = _load_fixture("heart_rate_sample.json")[0]
    result = map_record(raw, subject=SUBJECT)

    assert result["code"] == "8867-4"
    assert result["valueQuantity"] == {"value": 72.0, "unit": "/min"}
    assert result["subject"] == SUBJECT
    assert result["status"] == "final"
    assert "method" not in result
    assert result["metadata"]["uuid"] == raw["uuid"]
    assert result["metadata"]["recordingMethod"] is None
    assert result["metadata"]["lastModifiedTime"] is None
    assert result["metadata"]["sourceMetadata"] == raw["metadata"]
    validator.validate(result)


def test_spo2_fraction_converted_to_percent(validator):
    raw = _load_fixture("spo2_sample.json")[0]
    result = map_record(raw, subject=SUBJECT)

    assert result["code"] == "2708-6"
    assert result["valueQuantity"] == {"value": 97.0, "unit": "%"}
    validator.validate(result)


def test_hrv_requires_sdnn_method(validator):
    raw = _load_fixture("hrv_sample.json")[0]
    result = map_record(raw, subject=SUBJECT)

    assert result["code"] == "80404-7"
    assert result["valueQuantity"] == {"value": 45.0, "unit": "ms"}
    assert result["method"] == "SDNN"
    validator.validate(result)


def test_missing_fields_raise_mapping_error():
    with pytest.raises(MappingError, match="missing required field"):
        map_record({"uuid": "x", "value": 1}, subject=SUBJECT)


def test_unknown_quantity_type_raises_mapping_error():
    raw = {
        "uuid": "x",
        "quantityTypeIdentifier": "HKQuantityTypeIdentifierBodyTemperature",
        "value": 37.0,
        "unit": "degC",
        "startDate": "2026-08-14T09:59:45Z",
        "endDate": "2026-08-14T09:59:45Z",
        "metadata": {},
        "sourceRevision": "src",
    }
    with pytest.raises(MappingError, match="unrecognized quantityTypeIdentifier"):
        map_record(raw, subject=SUBJECT)


def test_unexpected_unit_raises_mapping_error():
    raw = {
        "uuid": "x",
        "quantityTypeIdentifier": "HKQuantityTypeIdentifierHeartRate",
        "value": 70.0,
        "unit": "bpm",
        "startDate": "2026-08-14T09:59:45Z",
        "endDate": "2026-08-14T09:59:45Z",
        "metadata": {},
        "sourceRevision": "src",
    }
    with pytest.raises(MappingError, match="unexpected unit"):
        map_record(raw, subject=SUBJECT)


def test_cli_skips_malformed_records_and_reports(tmp_path, capsys):
    input_path = FIXTURES / "malformed_sample.json"
    out_path = tmp_path / "out.json"

    exit_code = run(["--subject", SUBJECT, "--out", str(out_path), str(input_path)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "skipped 3 of 4 record(s)" in captured.err

    output = json.loads(out_path.read_text(encoding="utf-8"))
    assert len(output) == 1
    assert output[0]["metadata"]["uuid"] == "AAA-valid"


def test_cli_writes_valid_output_to_stdout(capsys, validator):
    input_path = FIXTURES / "heart_rate_sample.json"

    exit_code = run(["--subject", SUBJECT, str(input_path)])
    captured = capsys.readouterr()

    assert exit_code == 0
    output = json.loads(captured.out)
    assert len(output) == 1
    validator.validate(output[0])
