import csv
from pathlib import Path

import openpyxl
import yaml
from typer.testing import CliRunner

from attestory.cli import app
from attestory.engine import commit_file, init_workspace
from attestory.watcher import is_ignored_path, snapshot_if_changed

runner = CliRunner()


def _write_csv(path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(["Revenue"])
        writer.writerow([250])


def _write_workbook(path: Path) -> None:
    workbook = openpyxl.Workbook()
    workbook.active["B4"] = 250
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
    assert "VERIFIED" in result.stdout
    assert "NEEDS REVIEW" in result.stdout
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


def test_publish_workflow_is_valid_yaml():
    workflow_path = Path(__file__).parents[1] / ".github" / "workflows" / "publish.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    trigger = workflow.get("on", workflow.get(True))

    assert trigger["release"]["types"] == ["published"]
    assert workflow["jobs"]["pypi-publish"]["permissions"]["id-token"] == "write"
    assert workflow["jobs"]["pypi-publish"]["steps"][-1]["uses"] == (
        "pypa/gh-action-pypi-publish@release/v1"
    )
