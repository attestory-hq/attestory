import csv
from pathlib import Path

import openpyxl
import pyarrow.parquet as pq
import pytest
import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from attestory.cli import app
from attestory.engine import commit_file, init_workspace
from attestory.parsers.csv_extract import (
    diff_csv_snapshots,
    extract_csv_to_parquet,
)
from attestory.parsers.html import (
    diff_html_snapshots,
    extract_html_to_parquet,
)
from attestory.server import create_app

runner = CliRunner()


def _write_html(
    path: Path, amount: int, customer_count: int, extra_script: bool = False
) -> None:
    additional_script = (
        '<script src="https://new-telemetry.example.org/collect.js"></script>'
        if extra_script
        else ""
    )
    path.write_text(
        f"""<!doctype html>
<html><body>
  <div class="kpi" data-kpi="ARR">${amount:,}</div>
  <table>
    <tr><th>Customer</th><th>ARR</th></tr>
    <tr><td>Acme</td><td>${customer_count:,}</td></tr>
  </table>
  <script src="https://analytics.example.net/track.js"></script>
  <img width="1" height="1" src="//pixel.example.org/view.gif">
  {additional_script}
</body></html>""",
        encoding="utf-8",
    )


def _write_csv(path: Path, values: list[int], add_region: bool = False) -> None:
    columns = ["Revenue", "Units"] + (["Region"] if add_region else [])
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=columns)
        writer.writeheader()
        for index, value in enumerate(values):
            row = {"Revenue": value, "Units": index + 1}
            if add_region:
                row["Region"] = "North"
            writer.writerow(row)


def _write_workbook(path: Path, amount: int) -> None:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Revenue"
    sheet["B4"] = amount
    workbook.save(path)


def test_html_parser_extracts_kpis_tables_and_audit_diff(tmp_path: Path):
    source = tmp_path / "dashboard.html"
    first = tmp_path / "html_v1.parquet"
    second = tmp_path / "html_v2.parquet"
    _write_html(source, 412000, 250000)
    stats = extract_html_to_parquet(source, first)
    rows = pq.read_table(first).to_pylist()

    assert stats["kpis"] == 1
    assert stats["table_cells"] == 4
    assert stats["external_references"] == 2
    assert any(
        row["element_type"] == "kpi" and row["numeric_val"] == 412000 for row in rows
    )
    assert any(
        row["element_type"] == "script_audit"
        and row["val_str"] == "analytics.example.net"
        for row in rows
    )
    assert any(
        row["element_type"] == "script_audit"
        and row["val_str"] == "pixel.example.org"
        and row["numeric_val"] is None
        for row in rows
    )

    _write_html(source, 428000, 260000, extra_script=True)
    extract_html_to_parquet(source, second)
    diff = diff_html_snapshots(first, second)
    assert len(diff["kpi_changes"]) == 1
    assert len(diff["table_changes"]) == 1
    assert diff["kpi_changes"][0]["numeric_delta"] == 16000
    assert len(diff["script_audit_changes"]) == 1
    assert diff["script_audit_changes"][0]["new_val"] == "new-telemetry.example.org"


def test_csv_parser_diff_and_cross_file_rule(tmp_path: Path):
    first_csv = tmp_path / "actuals_v1.csv"
    second_csv = tmp_path / "actuals_v2.csv"
    first_pq = tmp_path / "csv_v1.parquet"
    second_pq = tmp_path / "csv_v2.parquet"
    _write_csv(first_csv, [100, 200])
    _write_csv(second_csv, [100, 250, 300], add_region=True)

    stats = extract_csv_to_parquet(first_csv, first_pq)
    rows = pq.read_table(first_pq).to_pylist()
    assert stats["rows"] == 2
    assert stats["columns"] == 2
    assert any(
        row["metric_type"] == "dataset"
        and row["label"] == "row_count"
        and row["numeric_val"] == 2
        for row in rows
    )
    assert any(
        row["column_name"] == "Revenue"
        and row["label"] == "SUM"
        and row["numeric_val"] == 300
        for row in rows
    )
    assert any(
        row["column_name"] == "Revenue"
        and row["label"] == "AVG"
        and row["numeric_val"] == 150
        for row in rows
    )
    assert any(
        row["metric_type"] == "schema"
        and row["column_name"] == "Revenue"
        and row["label"] == "null_count"
        and row["numeric_val"] == 0
        for row in rows
    )

    extract_csv_to_parquet(second_csv, second_pq)
    csv_diff = diff_csv_snapshots(first_pq, second_pq)
    assert csv_diff["total_changes"] > 0
    assert any(item["column_name"] == "Region" for item in csv_diff["schema_changes"])
    assert any(item["metric_type"] == "dataset" for item in csv_diff["changes"])

    init_workspace(tmp_path)
    rules = {
        "cross_file_checks": [
            {
                "file_a": "actuals.csv",
                "file_b": "Q4_Forecast.xlsx",
                "metric_a": {"column_name": "Revenue", "label": "SUM"},
                "metric_b": {"location": "Revenue!B4"},
                "tolerance": 0.01,
            }
        ]
    }
    (tmp_path / ".attestory" / "rules.yaml").write_text(
        yaml.safe_dump(rules), encoding="utf-8"
    )
    source = tmp_path / "actuals.csv"
    workbook = tmp_path / "Q4_Forecast.xlsx"
    _write_csv(source, [100, 200])
    _write_workbook(workbook, 300)
    commit_file(tmp_path, source)
    good = commit_file(tmp_path, workbook)
    assert good["record"]["rule_violations"] == []

    _write_workbook(workbook, 350)
    mismatch = commit_file(tmp_path, workbook)
    assert mismatch["record"]["rule_violations"][0]["rule"] == "cross_file_check"


def test_demo_creates_warning_violation_and_receipt(tmp_path: Path):
    demo_dir = tmp_path / "demo"
    result = runner.invoke(app, ["demo", "--dir", str(demo_dir)])
    assert result.exit_code == 0, result.stdout
    assert "HARDCODE OVERRIDE" in result.stdout
    assert "RULE VIOLATION" in result.stdout
    assert (demo_dir / ".attestory" / "rules.yaml").is_file()
    assert (
        demo_dir / ".attestory" / "reports" / "Q4_Forecast_v2_receipt.html"
    ).is_file()
    changelog = (demo_dir / ".attestory" / "changelog.md").read_text(encoding="utf-8")
    assert "HARDCODE OVERRIDE" in changelog
    assert "RULE VIOLATION" in changelog
    assert "412000" in changelog and "428000" in changelog


def test_fastapi_bridge_endpoints(tmp_path: Path):
    init_workspace(tmp_path)
    client = TestClient(create_app(tmp_path))

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok", "version": "0.3.1"}
    context = client.get("/v1/context")
    assert context.status_code == 200
    assert "must_be_formula" in context.json()["rules_yaml"]

    workbook = tmp_path / "Model.xlsx"
    _write_workbook(workbook, 100)
    first = client.post(
        "/v1/commit", json={"file_path": "Model.xlsx", "message": "baseline"}
    )
    assert first.status_code == 200
    _write_workbook(workbook, 125)
    second = client.post(
        "/v1/commit", json={"file_path": "Model.xlsx", "message": "updated"}
    )
    assert second.status_code == 200
    assert second.json()["version"] == 2
    assert second.json()["diff_summary"]["total_changes"] == 1

    diff = client.get("/v1/diff/Model.xlsx")
    assert diff.status_code == 200
    assert diff.json()["from_version"] == 1
    assert diff.json()["to_version"] == 2
    assert diff.json()["total_changes"] == 1

    report = client.post("/v1/report/Model.xlsx")
    assert report.status_code == 200
    assert (tmp_path / report.json()["path"]).is_file()

    upload = client.post(
        "/v1/commit",
        files={"file": ("uploaded.csv", "Revenue\n42\n", "text/csv")},
        data={"message": "uploaded source"},
    )
    assert upload.status_code == 200
    assert upload.json()["file"] == "uploaded.csv"
    assert not list((tmp_path / ".attestory" / ".incoming").glob("*/uploaded.csv"))

    outside = tmp_path.parent / f"{tmp_path.name}-outside.csv"
    outside.write_text("value\n1\n", encoding="utf-8")
    try:
        rejected = client.post("/v1/commit", json={"file_path": str(outside)})
        assert rejected.status_code == 400
    finally:
        outside.unlink(missing_ok=True)


@pytest.mark.parametrize("extension", [".html", ".csv"])
def test_commit_routes_new_formats(tmp_path: Path, extension: str):
    init_workspace(tmp_path)
    source = tmp_path / f"source{extension}"
    if extension == ".html":
        _write_html(source, 10, 5)
    else:
        _write_csv(source, [10, 20])
    result = commit_file(tmp_path, source)
    assert result["record"]["version"] == 1
    assert Path(tmp_path / result["record"]["snapshot"]).is_file()
