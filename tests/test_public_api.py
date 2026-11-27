import json
from pathlib import Path

import openpyxl

from attestory import AuditResult, snapshot, verify


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
        "hardcoded_regressions",
        "formula_errors",
        "rule_violations",
        "summary_stats",
    }
    assert payload["passed"] is True
    assert ": " not in encoded
