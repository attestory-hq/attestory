import csv
import json
from pathlib import Path

import openpyxl
import yaml
from typer.testing import CliRunner

from attestory.cli import app
from attestory.engine import commit_file, init_workspace, workspace_status
from attestory.watcher import is_ignored_path, snapshot_if_changed

runner = CliRunner()


def _write_csv(path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(["Revenue"])
        writer.writerow([250])


def _write_workbook(path: Path, amount: int = 250) -> None:
    workbook = openpyxl.Workbook()
    workbook.active["B4"] = amount
    workbook.save(path)


def test_status_lists_verified_and_rule_violating_files(tmp_path: Path, monkeypatch):
    init_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".attestory" / "rules.yaml").write_text(
        yaml.safe_dump({"must_be_formula": ["Sheet!B4"]}), encoding="utf-8"
    )
    csv_path = tmp_path / "actuals.csv"
    workbook_path = tmp_path / "Q4_Forecast.xlsx"
    _write_csv(csv_path)
    _write_workbook(workbook_path)
    commit_file(tmp_path, csv_path)
    commit_file(tmp_path, workbook_path)

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0, result.stdout
    assert "actuals.csv" in result.stdout
    assert "Q4_Forecast.xlsx" in result.stdout
    assert "PASS" in result.stdout
    assert "FAIL" in result.stdout
    assert "Open Errors / Rule Violations" in result.stdout


def test_watcher_ignores_noise_and_deduplicates_content(tmp_path: Path):
    assert is_ignored_path(Path("~$lock.xlsx"))
    assert is_ignored_path(Path(".attestory/history.csv"))
    assert is_ignored_path(Path("working.tmp"))
    assert not is_ignored_path(Path("actuals.csv"))

    init_workspace(tmp_path)
    source = tmp_path / "actuals.csv"
    _write_csv(source)
    first = snapshot_if_changed(tmp_path, source)
    assert first is not None
    assert first["record"]["version"] == 1
    assert snapshot_if_changed(tmp_path, source) is None

    source.write_text("Revenue\n500\n", encoding="utf-8")
    second = snapshot_if_changed(tmp_path, source)
    assert second is not None
    assert second["record"]["version"] == 2


def test_nested_files_with_same_name_have_independent_history(tmp_path: Path):
    init_workspace(tmp_path)
    finance = tmp_path / "Finance" / "Forecast.xlsx"
    sales = tmp_path / "Sales" / "Forecast.xlsx"
    finance.parent.mkdir()
    sales.parent.mkdir()
    _write_workbook(finance)
    _write_workbook(sales, 900)

    finance_v1 = commit_file(tmp_path, finance)
    sales_v1 = commit_file(tmp_path, sales)

    assert finance_v1["record"]["file"] == "Finance/Forecast.xlsx"
    assert sales_v1["record"]["file"] == "Sales/Forecast.xlsx"
    assert finance_v1["record"]["snapshot"] != sales_v1["record"]["snapshot"]

    _write_workbook(finance, 300)
    finance_v2 = commit_file(tmp_path, finance)
    assert finance_v2["record"]["version"] == 2
    assert finance_v2["diff"]["total_changes"] == 1
    assert sales_v1["record"]["version"] == 1


def test_same_stem_with_different_extensions_has_distinct_snapshots(
    tmp_path: Path,
):
    init_workspace(tmp_path)
    workbook = tmp_path / "Forecast.xlsx"
    csv_file = tmp_path / "Forecast.csv"
    _write_workbook(workbook)
    _write_csv(csv_file)

    workbook_commit = commit_file(tmp_path, workbook)
    csv_commit = commit_file(tmp_path, csv_file)

    assert workbook_commit["record"]["artifact_key"] == "Forecast.xlsx"
    assert csv_commit["record"]["artifact_key"] == "Forecast.csv"
    assert workbook_commit["record"]["snapshot"] != csv_commit["record"]["snapshot"]
    assert Path(tmp_path / workbook_commit["record"]["snapshot"]).is_file()
    assert Path(tmp_path / csv_commit["record"]["snapshot"]).is_file()


def test_legacy_history_is_preserved_and_flagged_for_review(tmp_path: Path):
    init_workspace(tmp_path)
    manifest_path = tmp_path / ".attestory" / "history.json"
    legacy_record = {
        "version": 1,
        "file": "Forecast.xlsx",
        "sha256": "abc123",
        "timestamp": "2024-01-01T00:00:00+00:00",
        "message": "legacy baseline",
        "snapshot": ".attestory/snapshots/Forecast_v1.parquet",
        "stats": {},
        "rule_violations": [],
        "diff_summary": {"total_changes": 0, "hardcode_overrides": 0},
    }
    manifest_path.write_text(
        json.dumps({"commits": [legacy_record]}), encoding="utf-8"
    )
    workbook = tmp_path / "Finance" / "Forecast.xlsx"
    workbook.parent.mkdir()
    _write_workbook(workbook)

    result = commit_file(tmp_path, workbook)
    history = json.loads(manifest_path.read_text(encoding="utf-8"))
    statuses = workspace_status(tmp_path)

    assert history["commits"][0]["file"] == "Forecast.xlsx"
    assert history["commits"][0]["identity_ambiguous"] is True
    assert result["record"]["version"] == 1
    assert result["record"]["file"] == "Finance/Forecast.xlsx"
    assert any(
        entry["file"] == "Forecast.xlsx" and entry["status"] == "INCOMPLETE"
        for entry in statuses
    )


def test_publish_workflow_is_valid_yaml():
    workflow_path = Path(__file__).parents[1] / ".github" / "workflows" / "publish.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    trigger = workflow.get("on", workflow.get(True))

    assert trigger["release"]["types"] == ["published"]
    assert workflow["jobs"]["pypi-publish"]["permissions"]["id-token"] == "write"
    assert workflow["jobs"]["pypi-publish"]["steps"][-1]["uses"] == (
        "pypa/gh-action-pypi-publish@release/v1"
    )
