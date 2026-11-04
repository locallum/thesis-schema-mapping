"""Tests for the provenance pipeline (pipeline/): logging, comparison and tracing."""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from pipeline.__main__ import main  # noqa: E402
from pipeline.provenance import sha256_json  # noqa: E402

_FIX = _ROOT / "tests" / "fixtures"
_HAS_APPLE = (_ROOT / "apple" / "mapper.py").exists()
needs_apple = pytest.mark.skipif(not _HAS_APPLE, reason="apple/ (Callum's mapper) not on this branch")


@pytest.fixture
def workdir(tmp_path):
    """Copies the fixtures so tests can modify them freely."""
    shutil.copytree(_FIX / "android", tmp_path / "android")
    if (_FIX / "apple").exists():
        shutil.copytree(_FIX / "apple", tmp_path / "apple")
    return tmp_path


def _run(workdir, *platforms, extra=()):
    argv = ["--db", str(workdir / "p.db"), "run", "--subject", "mrn-test", "--out", str(workdir / "out")]
    for p in platforms:
        argv += [f"--{p}", str(workdir / p)]
    return main(argv + list(extra))


def _db(workdir):
    conn = sqlite3.connect(workdir / "p.db")
    conn.row_factory = sqlite3.Row
    return conn


def _kinds(conn):
    return sorted(r["kind"] for r in conn.execute("SELECT kind FROM discrepancies"))


# --- logging ---

def test_android_only_run_logs_every_reading(workdir):
    assert _run(workdir, "android") == 0
    conn = _db(workdir)
    rows = conn.execute("SELECT * FROM provenance").fetchall()
    assert len(rows) == 7 and all(r["status"] == "mapped" for r in rows)
    assert _kinds(conn) == []  # nothing to compare against


def test_raw_record_and_hash_are_stored_verbatim(workdir):
    _run(workdir, "android")
    raw_file = json.loads((workdir / "android" / "spo2.json").read_text())
    row = _db(workdir).execute(
        "SELECT * FROM provenance WHERE test_id = 'SPO2-002'").fetchone()
    assert json.loads(row["raw_record"]) == raw_file[1]
    assert row["raw_record_sha256"] == sha256_json(raw_file[1])
    assert row["record_index"] == 1
    assert json.loads(row["rules"])[:2] == ["HC_DETECT_TYPE_BY_FIELD", "HC_SPO2_PASSTHROUGH_0_100"]


def test_output_links_back_to_provenance(workdir):
    _run(workdir, "android")
    out = json.loads((workdir / "out" / "android_heart_rate.canonical.json").read_text())
    conn = _db(workdir)
    for reading in out:
        pid = reading["metadata"]["sourceMetadata"]["provenanceId"]
        row = conn.execute("SELECT canonical FROM provenance WHERE provenance_id = ?", (pid,)).fetchone()
        logged = json.loads(row["canonical"])
        reading["metadata"]["sourceMetadata"].pop("provenanceId")
        assert logged == reading


def test_heart_rate_samples_get_their_own_rows(workdir):
    _run(workdir, "android")
    rows = _db(workdir).execute(
        "SELECT sample_index, canonical_value FROM provenance WHERE test_id = 'HR-013' ORDER BY sample_index"
    ).fetchall()
    assert [(r[0], r[1]) for r in rows] == [(0, 98.0), (1, 99.0), (2, 100.0)]


def test_rejected_record_is_logged_not_dropped(workdir):
    records = json.loads((workdir / "android" / "spo2.json").read_text())
    del records[0]["percentage"]
    (workdir / "android" / "spo2.json").write_text(json.dumps(records))
    assert _run(workdir, "android") == 1
    conn = _db(workdir)
    bad = conn.execute("SELECT * FROM provenance WHERE status = 'rejected'").fetchone()
    assert bad["test_id"] == "SPO2-001"
    assert json.loads(bad["rules"]) == ["REJECTED_BY_MAPPER"]
    assert "can't identify record type" in bad["error"]
    assert _kinds(conn) == ["REJECTED_RECORD"]


# --- cross-platform comparison ---

@needs_apple
def test_discrepancies_found(workdir):
    assert _run(workdir, "android", "apple") == 0
    conn = _db(workdir)
    assert _kinds(conn) == [
        "METADATA_GAP", "METADATA_GAP", "METHOD_MISMATCH", "MISSING_ON_APPLE", "VALUE_MISMATCH",
    ]
    v = conn.execute("SELECT * FROM discrepancies WHERE kind = 'VALUE_MISMATCH'").fetchone()
    assert (v["test_id"], v["android_value"], v["apple_value"]) == ("SPO2-002", 89.5, 89.0)
    assert v["severity"] == "error"
    m = conn.execute("SELECT * FROM discrepancies WHERE kind = 'MISSING_ON_APPLE'").fetchone()
    assert m["test_id"] == "HR-013" and m["apple_provenance_id"] is None


@needs_apple
def test_every_discrepancy_points_at_real_provenance(workdir):
    _run(workdir, "android", "apple")
    conn = _db(workdir)
    for d in conn.execute("SELECT * FROM discrepancies WHERE kind != 'METADATA_GAP'"):
        for side in ("android_provenance_id", "apple_provenance_id"):
            if d[side]:
                assert conn.execute("SELECT 1 FROM provenance WHERE provenance_id = ?", (d[side],)).fetchone()


@needs_apple
def test_spo2_float_artifact_is_not_a_mismatch(workdir):
    # 0.524 * 100 == 52.400000000000006 in floating point; must still match 52.4.
    a = json.loads((workdir / "android" / "spo2.json").read_text())
    h = json.loads((workdir / "apple" / "spo2.json").read_text())
    a[1]["percentage"], h[1]["value"] = 52.4, 0.524
    (workdir / "android" / "spo2.json").write_text(json.dumps(a))
    (workdir / "apple" / "spo2.json").write_text(json.dumps(h))
    _run(workdir, "android", "apple")
    assert "VALUE_MISMATCH" not in _kinds(_db(workdir))


@needs_apple
def test_duplicate_readings_flagged(workdir):
    h = json.loads((workdir / "apple" / "spo2.json").read_text())
    dup = dict(h[0], uuid="DUPLICATE-UUID")
    (workdir / "apple" / "spo2.json").write_text(json.dumps(h + [dup]))
    _run(workdir, "android", "apple")
    kinds = _kinds(_db(workdir))
    assert "DUPLICATE_READINGS" in kinds and "MISSING_ON_ANDROID" in kinds


# --- trace / report ---

@needs_apple
def test_trace_discrepancy_shows_both_sides(workdir, capsys):
    _run(workdir, "android", "apple")
    did = _db(workdir).execute("SELECT discrepancy_id FROM discrepancies WHERE kind='VALUE_MISMATCH'").fetchone()[0]
    capsys.readouterr()
    assert main(["--db", str(workdir / "p.db"), "trace", f"#{did}"]) == 0
    out = capsys.readouterr().out
    assert "ANDROID:" in out and "APPLE:" in out
    assert "HK_SPO2_FRACTION_X100" in out and "HC_SPO2_PASSTHROUGH_0_100" in out
    assert "unchanged since the run" in out


def test_trace_detects_changed_source_file(workdir, capsys):
    _run(workdir, "android")
    (workdir / "android" / "hrv.json").write_text("[]")
    capsys.readouterr()
    assert main(["--db", str(workdir / "p.db"), "trace", "HRV-001"]) == 0
    assert "CHANGED since the run" in capsys.readouterr().out


def test_report_on_latest_run(workdir, capsys):
    _run(workdir, "android")
    capsys.readouterr()
    assert main(["--db", str(workdir / "p.db"), "report"]) == 0
    assert "android  mapped    7" in capsys.readouterr().out


def test_runs_accumulate_in_one_db(workdir):
    _run(workdir, "android")
    _run(workdir, "android")
    conn = _db(workdir)
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM provenance").fetchone()[0] == 14


# --- pairing when the platforms don't share timestamps (HKBridge uses "now minus N minutes") ---

def _shift_apple_times(workdir, drop_test_ids: bool):
    """Rewrite the Apple fixtures the way HKBridge really exports them: wall-clock times,
    optionally with no test ID (only HKBridgeSessionID)."""
    for name in ("heart_rate", "spo2", "hrv"):
        path = workdir / "apple" / f"{name}.json"
        recs = json.loads(path.read_text())
        for r in recs:
            r["startDate"] = r["endDate"] = r["startDate"].replace("2026-08-14T08:", "2026-08-14T09:")
            if drop_test_ids:
                r["metadata"].pop("HKBridgeTestID")
        path.write_text(json.dumps(recs))


@needs_apple
def test_pairs_by_test_id_when_times_differ(workdir):
    _shift_apple_times(workdir, drop_test_ids=False)
    _run(workdir, "android", "apple")
    conn = _db(workdir)
    kinds = _kinds(conn)
    assert kinds.count("TIME_MISMATCH") == 6          # every one of the 6 pairs
    assert "VALUE_MISMATCH" in kinds and "METHOD_MISMATCH" in kinds
    assert "MISSING_ON_ANDROID" not in kinds
    assert kinds.count("MISSING_ON_APPLE") == 1       # HR-013's third sample


@needs_apple
def test_no_shared_key_explains_itself(workdir, capsys):
    _shift_apple_times(workdir, drop_test_ids=True)
    capsys.readouterr()
    _run(workdir, "android", "apple")
    out = capsys.readouterr().out
    assert "no readings could be paired" in out
    assert "HKBridgeTestID" in out
    kinds = _kinds(_db(workdir))
    assert kinds.count("MISSING_ON_APPLE") == 7 and kinds.count("MISSING_ON_ANDROID") == 6


@needs_apple
def test_apple_test_id_read_from_hkbridge_metadata(workdir):
    _run(workdir, "android", "apple")
    row = _db(workdir).execute(
        "SELECT test_id FROM provenance WHERE platform = 'apple' AND code = '2708-6' ORDER BY record_index"
    ).fetchone()
    assert row["test_id"] == "SPO2-001"
