import hashlib
import json
import subprocess
import sys
from pathlib import Path

import openpyxl

SCRIPT = (
    Path(__file__).parents[1]
    / "plugins"
    / "claude-cowork"
    / "skills"
    / "attestory-cowork"
    / "scripts"
    / "workbook_audit.py"
)


def _write_formula_workbook(path: Path, formula: str = "=SUM(B2:B3)") -> None:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet["B2"] = 200
    sheet["B3"] = 212
    sheet["B4"] = formula
    workbook.save(path)


def _run(*arguments: str) -> dict:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        capture_output=True,
        check=False,
        text=True,
    )
    return json.loads(completed.stdout)


def test_stage_captures_original_and_creates_separate_candidate(tmp_path: Path):
    source = tmp_path / "Forecast.xlsx"
    _write_formula_workbook(source)
    original_bytes = source.read_bytes()

    result = _run("stage", str(source))

    baseline = Path(result["baseline"])
    candidate = Path(result["candidate"])
    assert result["status"] == "STAGED"
    assert source.read_bytes() == original_bytes
    assert baseline.read_bytes() == original_bytes
    assert candidate.read_bytes() == original_bytes
    assert candidate.parent == source.parent


def test_audit_reports_formula_override_and_binds_receipt_to_saved_candidate(
    tmp_path: Path,
):
    source = tmp_path / "Forecast.xlsx"
    _write_formula_workbook(source)
    staged = _run("stage", str(source))
    candidate = Path(staged["candidate"])

    workbook = openpyxl.load_workbook(candidate)
    workbook.active["B4"] = 412
    workbook.save(candidate)
    candidate_hash = hashlib.sha256(candidate.read_bytes()).hexdigest()

    result = _run("audit", staged["baseline"], str(candidate))

    assert result["status"] == "FAIL"
    assert result["verified"] is False
    assert result["candidate_sha256"] == candidate_hash
    assert any(
        finding["kind"] == "hardcode_override" and finding["location"] == "Sheet!B4"
        for finding in result["findings"]
    )
    receipt = Path(result["receipt"]).read_text(encoding="utf-8")
    assert "Outcome: FAIL" in receipt
    assert candidate_hash in receipt


def test_unchanged_candidate_passes_and_returns_candidate_and_receipt(
    tmp_path: Path,
):
    source = tmp_path / "Forecast.xlsx"
    _write_formula_workbook(source)
    staged = _run("stage", str(source))

    result = _run("audit", staged["baseline"], staged["candidate"])

    assert result["status"] == "PASS"
    assert result["verified"] is True
    assert result["candidate_sha256"]
    assert Path(result["candidate"]).is_file()
    assert Path(result["receipt"]).is_file()


def test_formula_change_is_reported_without_marking_the_audit_failed(
    tmp_path: Path,
):
    source = tmp_path / "Forecast.xlsx"
    _write_formula_workbook(source)
    staged = _run("stage", str(source))
    candidate = Path(staged["candidate"])
    workbook = openpyxl.load_workbook(candidate)
    workbook.active["B4"] = "=B2+B3"
    workbook.save(candidate)

    result = _run("audit", staged["baseline"], str(candidate))

    assert result["status"] == "PASS"
    assert any(
        finding["kind"] == "formula_change" and finding["location"] == "Sheet!B4"
        for finding in result["findings"]
    )


def test_cached_excel_error_cell_fails_the_audit(tmp_path: Path):
    source = tmp_path / "Forecast.xlsx"
    _write_formula_workbook(source)
    staged = _run("stage", str(source))
    candidate = Path(staged["candidate"])
    workbook = openpyxl.load_workbook(candidate)
    workbook.active["B5"] = "#DIV/0!"
    workbook.active["B5"].data_type = "e"
    workbook.save(candidate)

    result = _run("audit", staged["baseline"], str(candidate))

    assert result["status"] == "FAIL"
    assert any(
        finding["kind"] == "formula_error" and finding["location"] == "Sheet!B5"
        for finding in result["findings"]
    )


def test_missing_baseline_is_incomplete_and_receipt_is_still_saved(
    tmp_path: Path,
):
    source = tmp_path / "Forecast.xlsx"
    _write_formula_workbook(source)
    staged = _run("stage", str(source))
    Path(staged["baseline"]).unlink()

    result = _run("audit", staged["baseline"], staged["candidate"])

    assert result["status"] == "INCOMPLETE"
    assert result["verified"] is False
    assert any(
        "baseline is unavailable" in item["reason"]
        for item in result["incomplete_findings"]
    )
    assert Path(result["receipt"]).is_file()


def test_rules_file_prevents_a_false_pass(tmp_path: Path):
    source = tmp_path / "Forecast.xlsx"
    _write_formula_workbook(source)
    staged = _run("stage", str(source))
    (tmp_path / ".attestory").mkdir(exist_ok=True)
    (tmp_path / ".attestory" / "rules.yaml").write_text(
        "must_be_formula:\n  - Sheet!B4\n", encoding="utf-8"
    )

    result = _run("audit", staged["baseline"], staged["candidate"])

    assert result["status"] == "INCOMPLETE"
    assert result["verified"] is False
    assert any(
        item["location"] == ".attestory/rules.yaml"
        for item in result["incomplete_findings"]
    )
