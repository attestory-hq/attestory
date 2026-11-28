import shutil
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from types import SimpleNamespace

import openpyxl
from typer.testing import CliRunner

from attestory.cli import app
from attestory.engine import commit_file, init_workspace

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
