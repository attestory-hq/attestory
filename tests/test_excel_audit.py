import hashlib
import json
import shutil
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from types import SimpleNamespace

import openpyxl
import yaml
from openpyxl.workbook.defined_name import DefinedName
from typer.testing import CliRunner

from attestory.cli import app
from attestory.engine import commit_file, init_workspace
from attestory.parsers.excel import normalize_formula

runner = CliRunner()


def _build_sample_workbook(
    path: Path, overwrite_total_with_hardcode: bool = False
) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Revenue"
    ws["A1"] = "Segment"
    ws["B1"] = "ARR"
    ws["A2"] = "Enterprise"
    ws["B2"] = 250000
    ws["A3"] = "Mid-Market"
    ws["B3"] = 162000
    ws["A4"] = "Total"

    if overwrite_total_with_hardcode:
        ws["B4"] = 428000
    else:
        ws["B4"] = "=SUM(B2:B3)"

    wb.save(path)


def _build_formula_workbook(path: Path, formula: str) -> None:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Revenue"
    sheet["B2"] = 250
    sheet["B3"] = 162
    sheet["B4"] = formula
    sheet["B4"].number_format = "0.00"
    workbook.save(path)


def _remove_stored_cell(path: Path, cell_id: str) -> None:
    temp_path = path.with_suffix(".tmp.xlsx")
    namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with (
        zipfile.ZipFile(path) as source,
        zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED) as target,
    ):
        for item in source.infolist():
            contents = source.read(item.filename)
            if item.filename == "xl/worksheets/sheet1.xml":
                root = ET.fromstring(contents)
                for row in root.findall(f".//{namespace}row"):
                    for cell in row.findall(f"{namespace}c"):
                        if cell.attrib.get("r") == cell_id:
                            row.remove(cell)
                contents = ET.tostring(root, encoding="utf-8")
            target.writestr(item, contents)
    temp_path.replace(path)


def _set_cached_formula_value(path: Path, value: int) -> None:
    temp_path = path.with_suffix(".tmp.xlsx")
    namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with (
        zipfile.ZipFile(path) as source,
        zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED) as target,
    ):
        for item in source.infolist():
            contents = source.read(item.filename)
            if item.filename == "xl/worksheets/sheet1.xml":
                root = ET.fromstring(contents)
                cell = root.find(f".//{namespace}c[@r='B4']")
                cached_value = cell.find(f"{namespace}v")
                if cached_value is None:
                    cached_value = ET.SubElement(cell, f"{namespace}v")
                cached_value.text = str(value)
                contents = ET.tostring(root, encoding="utf-8")
            target.writestr(item, contents)
    temp_path.replace(path)


def test_hardcoded_formula_override_detected(tmp_path: Path):
    init_workspace(tmp_path)
    xlsx_path = tmp_path / "Q4_Forecast.xlsx"

    _build_sample_workbook(xlsx_path, overwrite_total_with_hardcode=False)
    res_v1 = commit_file(tmp_path, xlsx_path, message="Initial forecast")
    assert res_v1["record"]["version"] == 1
    assert res_v1["diff"] is None

    _build_sample_workbook(xlsx_path, overwrite_total_with_hardcode=True)
    res_v2 = commit_file(tmp_path, xlsx_path, message="Updated Q4 numbers")

    assert res_v2["record"]["version"] == 2
    diff = res_v2["diff"]
    assert diff is not None
    assert len(diff["hardcode_overrides"]) == 1
    override = diff["hardcode_overrides"][0]
    assert override["location"] == "Revenue!B4"
    assert override["old_formula"] == "=SUM(B2:B3)"
    assert override["new_hardcoded_val"] == "428000"


def test_formula_normalization_preserves_literals_and_intersections():
    assert normalize_formula('=IF(A1="A B",SUM(B2:B3),0)') != normalize_formula(
        '=IF(A1="AB",SUM(B2:B3),0)'
    )
    assert normalize_formula("=SUM(B2:B3)") == normalize_formula(
        "=sum( B2:B3 )"
    )
    assert normalize_formula("=A1:B2 C3:D4") != normalize_formula("=A1:B2+C3:D4")


def test_formula_reference_change_is_reported_as_formula_edit(tmp_path: Path):
    init_workspace(tmp_path)
    workbook = tmp_path / "Revenue.xlsx"
    _build_formula_workbook(workbook, "=SUM(B2:B3)")
    commit_file(tmp_path, workbook, message="Baseline")

    _build_formula_workbook(workbook, "=SUM(B2:B2)")
    result = commit_file(tmp_path, workbook, message="Changed formula reference")

    assert result["diff"]["formula_changes"] == [
        {
            "location": "Revenue!B4",
            "old_formula": "=SUM(B2:B3)",
            "new_formula": "=SUM(B2:B2)",
            "old_val": "",
            "new_val": "",
        }
    ]


def test_formula_cleared_to_blank_is_reported_and_fails_audit(
    tmp_path: Path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    init_workspace(tmp_path)
    workbook = tmp_path / "Revenue.xlsx"
    _build_formula_workbook(workbook, "=SUM(B2:B3)")
    commit_file(tmp_path, workbook, message="Baseline")

    edited = openpyxl.load_workbook(workbook)
    edited.active["B4"] = None
    edited.save(workbook)
    result = runner.invoke(app, ["commit", "Revenue.xlsx", "--json"])
    payload = json.loads(result.stdout)

    assert result.exit_code == 1
    assert payload["status"] == "FAIL"
    assert payload["formula_to_blanks"] == [
        {
            "location": "Revenue!B4",
            "formula": "=SUM(B2:B3)",
            "old_val": "",
        }
    ]
    report = runner.invoke(app, ["report", "Revenue.xlsx", "--json"])
    report_payload = json.loads(report.stdout)
    receipt = (
        tmp_path / ".attestory" / "reports" / "Revenue_v2_receipt.html"
    ).read_text(
        encoding="utf-8"
    )
    assert report_payload["status"] == "FAIL"
    assert report_payload["formula_to_blanks"] == payload["formula_to_blanks"]
    assert "Formula cleared" in receipt


def test_missing_formula_cell_is_reported_separately(tmp_path: Path):
    init_workspace(tmp_path)
    workbook = tmp_path / "Revenue.xlsx"
    _build_formula_workbook(workbook, "=SUM(B2:B3)")
    commit_file(tmp_path, workbook, message="Baseline")
    _remove_stored_cell(workbook, "B4")

    result = commit_file(tmp_path, workbook, message="Removed formula cell")

    assert result["diff"]["formula_to_blanks"] == []
    assert result["diff"]["missing_formula_cells"] == [
        {
            "location": "Revenue!B4",
            "formula": "=SUM(B2:B3)",
            "old_val": "",
        }
    ]
    assert result["record"]["audit_status"] == "FAIL"


def test_modern_excel_calculation_errors_are_detected(tmp_path: Path):
    init_workspace(tmp_path)
    workbook = openpyxl.Workbook()
    workbook.active["A1"] = "#SPILL!"
    workbook.active["A1"].data_type = "e"
    workbook.active["A2"] = "#CALC!"
    workbook.active["A2"].data_type = "e"
    workbook_path = tmp_path / "Errors.xlsx"
    workbook.save(workbook_path)

    result = commit_file(tmp_path, workbook_path)

    assert {error["error"] for error in result["record"]["stats"]["errors"]} == {
        "#SPILL!",
        "#CALC!",
    }
    from attestory.cli import generate_report

    receipt = generate_report(tmp_path, "Errors.xlsx")
    report_html = (tmp_path / receipt["path"]).read_text(encoding="utf-8")
    assert "#SPILL!" in report_html
    assert "#CALC!" in report_html


def _build_named_workbook(
    path: Path,
    *,
    metric_row: int,
    metric_name: str = "MetricTotal",
    named_range: str | None = None,
):
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Revenue"
    sheet["A1"] = "Header"
    sheet[f"B{metric_row - 1}"] = 250
    sheet[f"B{metric_row}"] = f"=SUM(B{metric_row - 1}:B{metric_row - 1})"
    workbook.defined_names.add(
        DefinedName(metric_name, attr_text=named_range or f"'Revenue'!$B${metric_row}")
    )
    if named_range is None:
        workbook.defined_names.add(
            DefinedName(
                "MetricValue",
                attr_text=f"'Revenue'!$B${metric_row - 1}",
            )
        )
    workbook.save(path)


def test_named_range_moves_with_row_insertion(tmp_path: Path):
    init_workspace(tmp_path)
    workbook = tmp_path / "Revenue.xlsx"
    (tmp_path / ".attestory" / "rules.yaml").write_text(
        "must_be_formula:\n"
        "  - MetricTotal\n"
        "assertions:\n"
        "  - location: MetricValue\n"
        "    equals: 250\n",
        encoding="utf-8",
    )

    _build_named_workbook(workbook, metric_row=4)
    first = commit_file(tmp_path, workbook)

    _build_named_workbook(workbook, metric_row=5)
    second = commit_file(tmp_path, workbook)

    assert first["record"]["rule_violations"] == []
    assert second["record"]["rule_violations"] == []
    assert not any(
        finding["location"] == "MetricTotal" or finding["location"] == "MetricValue"
        for finding in second["record"]["incomplete_findings"]
    )


def test_named_range_missing_ambiguous_and_coordinate_rules(tmp_path: Path):
    init_workspace(tmp_path)
    workbook = tmp_path / "Revenue.xlsx"
    _build_named_workbook(
        workbook,
        metric_row=4,
        metric_name="AmbiguousMetric",
        named_range="'Revenue'!$B$3:$B$4",
    )
    (tmp_path / ".attestory" / "rules.yaml").write_text(
        "must_be_formula:\n"
        "  - MissingMetric\n"
        "  - AmbiguousMetric\n"
        "  - Revenue!B4\n"
        "assertions:\n"
        "  - location: MissingMetric\n"
        "    equals: 250\n",
        encoding="utf-8",
    )

    result = commit_file(tmp_path, workbook)

    incomplete = result["record"]["incomplete_findings"]
    reasons = {finding["location"]: finding["reason"] for finding in incomplete}
    assert "MissingMetric" in reasons
    assert "was not found" in reasons["MissingMetric"]
    assert "AmbiguousMetric" in reasons
    assert "ambiguous" in reasons["AmbiguousMetric"]
    assert not any(
        finding["rule"] == "must_be_formula"
        and finding["location"] == "Revenue!B4"
        for finding in result["record"]["rule_violations"]
    )


def test_uncached_formula_cannot_pass_numeric_assertion(tmp_path: Path):
    init_workspace(tmp_path)
    (tmp_path / ".attestory" / "rules.yaml").write_text(
        "assertions:\n  - location: Revenue!B4\n    equals: 412\n",
        encoding="utf-8",
    )
    workbook = tmp_path / "Revenue.xlsx"
    _build_formula_workbook(workbook, "=SUM(B2:B3)")

    result = commit_file(tmp_path, workbook)

    assert result["record"]["rule_violations"] == []
    assert any(
        finding["rule"] == "numeric_assertion"
        and finding["location"] == "Revenue!B4"
        for finding in result["record"]["incomplete_findings"]
    )
    assert result["record"]["audit_status"] == "INCOMPLETE"


def test_uncached_formula_is_flagged_in_commit_and_html_receipt(
    tmp_path: Path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init"])
    workbook = tmp_path / "Model.xlsx"
    _build_sample_workbook(workbook)

    result = runner.invoke(app, ["commit", "Model.xlsx"])

    assert result.exit_code == 0
    assert "UNCACHED FORMULA CACHE" in result.stdout
    record = __import__("json").loads(
        (tmp_path / ".attestory" / "history.json").read_text(encoding="utf-8")
    )["commits"][0]
    assert record["stats"]["formula_cache_warnings"] == [
        {
            "location": "Revenue!B4",
            "formula": "=SUM(B2:B3)",
            "warning": "uncached",
        }
    ]

    report = runner.invoke(app, ["report", "Model.xlsx"])

    assert report.exit_code == 0
    html = (tmp_path / ".attestory" / "reports" / "Model_v1_receipt.html").read_text(
        encoding="utf-8"
    )
    assert "INCOMPLETE" in html
    assert "No prior snapshot exists" in html
    assert "UNCACHED FORMULA CACHE" in html


def test_recalculate_detects_stale_cached_formula_values(tmp_path: Path, monkeypatch):
    workbook = tmp_path / "Model.xlsx"
    _build_sample_workbook(workbook)
    _set_cached_formula_value(workbook, 410000)
    init_workspace(tmp_path)
    (tmp_path / ".attestory" / "rules.yaml").write_text(
        "assertions:\n  - location: Revenue!B4\n    equals: 412000\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("attestory.parsers.excel.shutil.which", lambda _: "soffice")

    def fake_soffice(command, **_kwargs):
        source = Path(command[-1])
        output_dir = Path(command[command.index("--outdir") + 1])
        output = output_dir / source.name
        shutil.copy2(source, output)
        _set_cached_formula_value(output, 412000)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("attestory.parsers.excel.subprocess.run", fake_soffice)

    result = commit_file(tmp_path, workbook, recalculate=True)

    assert result["record"]["rule_violations"] == []
    assert result["record"]["stats"]["formula_cache_warnings"] == [
        {
            "location": "Revenue!B4",
            "formula": "=SUM(B2:B3)",
            "warning": "stale",
            "previous_value": 410000,
            "recalculated_value": 412000,
        }
    ]
    assert (
        openpyxl.load_workbook(workbook, data_only=True)["Revenue"]["B4"].value
        == 412000
    )


def test_recalculate_requires_libreoffice(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init"])
    workbook = tmp_path / "Model.xlsx"
    _build_sample_workbook(workbook)
    monkeypatch.setattr("attestory.parsers.excel.shutil.which", lambda _: None)

    result = runner.invoke(app, ["commit", "Model.xlsx", "--recalculate"])

    assert result.exit_code == 1
    assert "LibreOffice (soffice) is required" in result.stdout


def test_first_excel_capture_is_incomplete_in_python_api(tmp_path: Path):
    workbook = tmp_path / "Model.xlsx"
    wb = openpyxl.Workbook()
    wb.active["B4"] = 412
    wb.save(workbook)

    from attestory import verify

    result = verify(workbook)

    assert result.status == "INCOMPLETE"
    assert result.passed is False
    assert result.incomplete_findings == [
        {
            "rule": "missing_baseline",
            "location": "Model.xlsx",
            "reason": "No prior snapshot exists for this workbook; comparative checks cannot run.",
        }
    ]


def test_first_excel_capture_status_matches_cli_and_receipt(
    tmp_path: Path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    init_workspace(tmp_path)
    workbook = tmp_path / "Model.xlsx"
    wb = openpyxl.Workbook()
    wb.active["B4"] = 412
    wb.save(workbook)

    commit_result = runner.invoke(app, ["commit", "Model.xlsx", "--json"])
    commit_payload = __import__("json").loads(commit_result.stdout)
    report_result = runner.invoke(app, ["report", "Model.xlsx", "--json"])
    report_payload = __import__("json").loads(report_result.stdout)
    html = (tmp_path / ".attestory" / "reports" / "Model_v1_receipt.html").read_text(
        encoding="utf-8"
    )

    assert commit_payload["status"] == "INCOMPLETE"
    assert "No prior snapshot exists" in commit_payload["incomplete_findings"][0]["reason"]
    assert report_payload["status"] == commit_payload["status"]
    assert report_payload["incomplete_findings"] == commit_payload["incomplete_findings"]
    assert "INCOMPLETE" in html and "No prior snapshot exists" in html


def test_historical_receipt_is_immutable_across_workbook_and_rule_changes(
    tmp_path: Path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    init_workspace(tmp_path)
    rules_path = tmp_path / ".attestory" / "rules.yaml"
    workbook = tmp_path / "Model.xlsx"

    wb = openpyxl.Workbook()
    wb.active["B4"] = 250
    wb.save(workbook)
    rules_path.write_text(
        yaml.safe_dump({"must_be_formula": ["Sheet!B4"]}),
        encoding="utf-8",
    )
    first = commit_file(tmp_path, workbook)
    first_record = first["record"]
    assert first_record["manifest_schema_version"] == 1
    assert first_record["audit_id"].startswith("aud_")
    assert first_record["baseline_sha256"] is None
    assert first_record["baseline_snapshot_sha256"] is None
    assert first_record["candidate_sha256"]
    assert first_record["candidate_snapshot_sha256"]
    assert first_record["evaluated_ruleset_digest"]
    assert first_record["engine_version"]
    assert first_record["outcome"] == first_record["audit_status"]
    assert first_record["findings"]

    wb = openpyxl.load_workbook(workbook)
    wb.active["B4"] = 300
    wb.save(workbook)
    rules_path.write_text(
        yaml.safe_dump({"must_be_formula": ["Sheet!A1"]}),
        encoding="utf-8",
    )
    second = commit_file(tmp_path, workbook)
    assert second["record"]["baseline_sha256"] == first_record["sha256"]
    assert (
        second["record"]["baseline_snapshot_sha256"]
        == first_record["snapshot_sha256"]
    )

    result = runner.invoke(app, ["report", "Model.xlsx", "--version", "1", "--json"])
    assert result.exit_code == 1
    historical_html = (
        tmp_path / ".attestory" / "reports" / "Model_v1_receipt.html"
    ).read_text(encoding="utf-8")
    assert "missing_baseline" in historical_html
    assert first_record["audit_id"] in historical_html


def test_claude_hooks_capture_excel_before_first_and_repeated_edits(
    tmp_path: Path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    workbook = tmp_path / "Model.xlsx"
    wb = openpyxl.Workbook()
    wb.active["B2"] = 250000
    wb.save(workbook)
    original_hash = hashlib.sha256(workbook.read_bytes()).hexdigest()

    def invoke_hook(command: str):
        return runner.invoke(
            app,
            [command],
            input=json.dumps(
                {
                    "cwd": str(tmp_path),
                    "tool_name": "Edit",
                    "tool_input": {"file_path": str(workbook)},
                }
            ),
        )

    before_first_edit = invoke_hook("hook-pre-tool-use")
    assert before_first_edit.exit_code == 0, before_first_edit.output
    initial_record = json.loads(
        (tmp_path / ".attestory" / "history.json").read_text(encoding="utf-8")
    )["commits"][0]
    assert initial_record["sha256"] == original_hash

    wb = openpyxl.load_workbook(workbook)
    wb.active["B2"] = 260000
    wb.save(workbook)
    after_first_edit = invoke_hook("hook-post-tool-use")
    assert after_first_edit.exit_code == 0, after_first_edit.output

    before_repeated_edit = invoke_hook("hook-pre-tool-use")
    assert before_repeated_edit.exit_code == 0, before_repeated_edit.output
    history_path = tmp_path / ".attestory" / "history.json"
    history = json.loads(history_path.read_text(encoding="utf-8"))
    assert len(history["commits"]) == 2
    assert json.loads(before_repeated_edit.stdout)["captured"] is False

    wb = openpyxl.load_workbook(workbook)
    wb.active["B3"] = 170000
    wb.save(workbook)
    after_repeated_edit = invoke_hook("hook-post-tool-use")
    assert after_repeated_edit.exit_code == 0, after_repeated_edit.output
    history = json.loads(history_path.read_text(encoding="utf-8"))
    assert len(history["commits"]) == 3
    assert history["commits"][1]["sha256"] != history["commits"][2]["sha256"]


def test_pre_edit_hook_reports_capture_failure(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    workbook = tmp_path / "Model.xlsx"
    wb = openpyxl.Workbook()
    wb.active["B2"] = 250000
    wb.save(workbook)

    def fail_capture(*_args, **_kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr("attestory.cli.snapshot_before", fail_capture)

    result = runner.invoke(
        app,
        ["hook-pre-tool-use"],
        input=json.dumps(
            {
                "cwd": str(tmp_path),
                "tool_name": "Write",
                "tool_input": {"file_path": str(workbook)},
            }
        ),
    )

    assert result.exit_code == 2
    assert "Could not capture pre-edit baseline: disk unavailable" in result.output


def test_new_excel_file_without_pre_edit_contents_remains_incomplete(
    tmp_path: Path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    workbook = tmp_path / "New.xlsx"
    event = json.dumps(
        {
            "cwd": str(tmp_path),
            "tool_name": "Write",
            "tool_input": {"file_path": str(workbook)},
        }
    )

    before_write = runner.invoke(app, ["hook-pre-tool-use"], input=event)
    assert before_write.exit_code == 0
    assert json.loads(before_write.stdout)["status"] == "INCOMPLETE"
    assert not (tmp_path / ".attestory").exists()

    wb = openpyxl.Workbook()
    wb.active["A1"] = 1
    wb.save(workbook)
    after_write = runner.invoke(app, ["hook-post-tool-use"], input=event)

    assert after_write.exit_code == 1
    assert json.loads(after_write.stdout)["status"] == "INCOMPLETE"
    history = json.loads(
        (tmp_path / ".attestory" / "history.json").read_text(encoding="utf-8")
    )
    assert history["commits"][0]["incomplete_findings"][0]["rule"] == "missing_baseline"


def test_explicit_baseline_supports_uncovered_excel_edit_paths(
    tmp_path: Path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    workbook = tmp_path / "Model.xlsx"
    wb = openpyxl.Workbook()
    wb.active["B2"] = 250000
    wb.save(workbook)
    baseline = runner.invoke(app, ["snapshot-before", "Model.xlsx"])
    assert baseline.exit_code == 0, baseline.output
    baseline_hash = hashlib.sha256(workbook.read_bytes()).hexdigest()

    wb = openpyxl.load_workbook(workbook)
    wb.active["B2"] = 275000
    wb.save(workbook)
    audit = runner.invoke(app, ["commit", "Model.xlsx", "--json"])
    assert audit.exit_code == 0, audit.output

    history = json.loads(
        (tmp_path / ".attestory" / "history.json").read_text(encoding="utf-8")
    )
    assert len(history["commits"]) == 2
    assert history["commits"][0]["sha256"] == baseline_hash
    assert history["commits"][1]["diff_summary"]["total_changes"] > 0


def test_unsupported_cross_file_check_is_incomplete(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    init_workspace(tmp_path)
    (tmp_path / ".attestory" / "rules.yaml").write_text(
        "cross_file_checks:\n"
        "  - file_a: Model.xlsx\n"
        "    file_b: legacy.xls\n"
        "    metric_a: {location: Revenue!B4}\n"
        "    metric_b: {location: Revenue!B4}\n",
        encoding="utf-8",
    )
    workbook = tmp_path / "Model.xlsx"
    wb = openpyxl.Workbook()
    wb.active.title = "Revenue"
    wb.active["B4"] = 412
    wb.save(workbook)

    result = runner.invoke(app, ["commit", "Model.xlsx", "--json"])
    payload = __import__("json").loads(result.stdout)

    assert payload["status"] == "INCOMPLETE"
    assert any(
        "Unsupported cross-file metric format: legacy.xls" in item["reason"]
        for item in payload["incomplete_findings"]
    )


def test_failed_check_keeps_incomplete_findings_across_interfaces(
    tmp_path: Path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    init_workspace(tmp_path)
    (tmp_path / ".attestory" / "rules.yaml").write_text(
        "cross_file_checks:\n"
        "  - file_a: Model.xlsx\n"
        "    file_b: actuals.csv\n"
        "    metric_a: {location: Revenue!B4}\n"
        "    metric_b: {column_name: Revenue, label: SUM}\n"
        "  - file_a: Model.xlsx\n"
        "    file_b: missing.csv\n"
        "    metric_a: {location: Revenue!B4}\n"
        "    metric_b: {column_name: Revenue, label: SUM}\n",
        encoding="utf-8",
    )
    (tmp_path / "actuals.csv").write_text("Revenue\n100\n", encoding="utf-8")
    workbook = tmp_path / "Model.xlsx"
    wb = openpyxl.Workbook()
    wb.active.title = "Revenue"
    wb.active["B4"] = 412
    wb.save(workbook)
    commit_file(tmp_path, tmp_path / "actuals.csv")

    cli_result = runner.invoke(app, ["commit", "Model.xlsx", "--json"])
    cli_payload = __import__("json").loads(cli_result.stdout)
    from attestory import verify

    api_result = verify(workbook)
    report_result = runner.invoke(app, ["report", "Model.xlsx", "--json"])
    report_payload = __import__("json").loads(report_result.stdout)
    receipt = (tmp_path / ".attestory" / "reports" / "Model_v2_receipt.html").read_text(
        encoding="utf-8"
    )

    assert cli_payload["status"] == "FAIL"
    assert cli_payload["rule_violations"]
    assert cli_payload["incomplete_findings"]
    assert api_result.status == "FAIL"
    assert api_result.incomplete_findings
    assert report_payload["status"] == "FAIL"
    assert report_payload["incomplete_findings"]
    assert "FAIL" in receipt and "Incomplete checks" in receipt


def test_cli_end_to_end(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    init_res = runner.invoke(app, ["init"])
    assert init_res.exit_code == 0
    assert (tmp_path / ".attestory").exists()

    xlsx_path = tmp_path / "Model.xlsx"
    _build_sample_workbook(xlsx_path, overwrite_total_with_hardcode=False)
    c1 = runner.invoke(app, ["commit", "Model.xlsx", "-m", "v1"])
    assert c1.exit_code == 0

    _build_sample_workbook(xlsx_path, overwrite_total_with_hardcode=True)
    c2 = runner.invoke(app, ["commit", "Model.xlsx", "-m", "v2"])
    assert c2.exit_code == 0
    assert "HARDCODE OVERRIDE" in c2.stdout or "hardcode overrides" in c2.stdout

    diff_res = runner.invoke(app, ["diff", "Model.xlsx"])
    assert diff_res.exit_code == 0
    assert "Revenue!B4" in diff_res.stdout


def test_commit_json_output_and_failure_exit(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init"])
    xlsx_path = tmp_path / "Model.xlsx"
    _build_sample_workbook(xlsx_path)
    assert runner.invoke(app, ["commit", "Model.xlsx"]).exit_code == 0

    _build_sample_workbook(xlsx_path, overwrite_total_with_hardcode=True)
    result = runner.invoke(app, ["commit", "Model.xlsx", "--json"])

    assert result.exit_code == 1
    payload = __import__("json").loads(result.stdout)
    assert payload["passed"] is False
    assert payload["hardcoded_regressions"] == [
        {
            "cell": "Revenue!B4",
            "old_formula": "=SUM(B2:B3)",
            "new_hardcoded_value": "428000",
        }
    ]
    assert payload["formula_errors"] == []
    assert payload["rule_violations"] == []
    assert payload["summary_stats"]["hardcode_overrides"] == 1


def test_diff_json_output(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init"])
    xlsx_path = tmp_path / "Model.xlsx"
    _build_sample_workbook(xlsx_path)
    runner.invoke(app, ["commit", "Model.xlsx"])
    _build_sample_workbook(xlsx_path, overwrite_total_with_hardcode=True)
    runner.invoke(app, ["commit", "Model.xlsx"])

    result = runner.invoke(app, ["diff", "Model.xlsx", "--json"])

    assert result.exit_code == 1
    payload = __import__("json").loads(result.stdout)
    assert payload["passed"] is False
    assert payload["hardcoded_regressions"][0]["cell"] == "Revenue!B4"
