from pathlib import Path

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
