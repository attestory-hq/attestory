import json
from pathlib import Path

import openpyxl

from attestory import (
    AuditResult,
    audit_excel_edit,
    prepare_excel_edit,
    snapshot,
    verify,
)


def _write_workbook(path: Path, hardcode_total: bool = False) -> None:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Revenue"
    sheet["B2"] = 250
    sheet["B3"] = 162
    sheet["B4"] = 412 if hardcode_total else "=SUM(B2:B3)"
    workbook.save(path)


def test_snapshot_and_verify_are_available_without_cli(tmp_path: Path):
    workbook = tmp_path / "forecast.xlsx"
    _write_workbook(workbook)

    baseline = snapshot(workbook, message="baseline")

    assert baseline["record"]["version"] == 1
    _write_workbook(workbook, hardcode_total=True)
    result = verify(workbook, message="edited")

    assert isinstance(result, AuditResult)
    assert result.passed is False
    assert result.hardcoded_regressions == [
        {
            "cell": "Revenue!B4",
            "old_formula": "=SUM(B2:B3)",
            "new_hardcoded_value": "412",
        }
    ]
    assert result.record["version"] == 2


def test_audit_result_json_has_stable_compact_diagnostic_schema(tmp_path: Path):
    workbook = tmp_path / "forecast.xlsx"
    _write_workbook(workbook)
    snapshot(workbook)
    result = verify(workbook)

    payload = result.to_dict()
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))

    assert set(payload) == {
        "passed",
        "status",
        "hardcoded_regressions",
        "formula_changes",
        "formula_to_blanks",
        "missing_formula_cells",
        "formula_errors",
        "rule_violations",
        "incomplete_findings",
        "summary_stats",
    }
    assert payload["passed"] is False
    assert payload["status"] == "INCOMPLETE"
    assert payload["incomplete_findings"]
    assert ": " not in encoded


def test_excel_edit_workflow_accepts_clean_edit_and_preserves_original():
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Revenue"
    sheet["B2"] = 250
    sheet["B3"] = 162
    sheet["B4"] = 412

    import io

    original_buffer = io.BytesIO()
    workbook.save(original_buffer)
    original_bytes = original_buffer.getvalue()

    prepared = prepare_excel_edit(original_bytes, filename="Revenue.xlsx")
    assert prepared.working_copy == original_bytes
    assert prepared.original_sha256 != ""

    sheet["B2"] = 260
    candidate_buffer = io.BytesIO()
    workbook.save(candidate_buffer)
    candidate_bytes = candidate_buffer.getvalue()

    result = audit_excel_edit(prepared, candidate_bytes, filename="Revenue.xlsx")

    assert result.status == "PASS"
    assert result.verified is True
    assert result.original_sha256 == prepared.original_sha256
    assert result.candidate_sha256
    assert result.receipt is not None
    assert result.receipt["candidate_sha256"] == result.candidate_sha256
    assert prepared.working_copy == original_bytes


def test_excel_edit_workflow_detects_same_value_formula_override():
    import io

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Revenue"
    sheet["B2"] = 250
    sheet["B3"] = 162
    sheet["B4"] = "=SUM(B2:B3)"
    original_buffer = io.BytesIO()
    workbook.save(original_buffer)

    sheet["B4"] = 412
    candidate_buffer = io.BytesIO()
    workbook.save(candidate_buffer)

    result = audit_excel_edit(
        original_buffer.getvalue(),
        candidate_buffer.getvalue(),
        filename="Revenue.xlsx",
    )

    assert result.status == "FAIL"
    assert result.verified is False
    assert any(
        finding["kind"] == "hardcode_override"
        and finding["location"] == "Revenue!B4"
        for finding in result.findings
    )


def test_excel_edit_workflow_returns_incomplete_for_missing_baseline():
    import io

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Revenue"
    sheet["B2"] = 250
    candidate_buffer = io.BytesIO()
    workbook.save(candidate_buffer)

    result = audit_excel_edit(
        None,
        candidate_buffer.getvalue(),
        filename="Revenue.xlsx",
    )

    assert result.status == "INCOMPLETE"
    assert result.verified is False
    assert result.outcome["incomplete_findings"]
    assert "baseline" in result.outcome["incomplete_findings"][0]["reason"].lower()
