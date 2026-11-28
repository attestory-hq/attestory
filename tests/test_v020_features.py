import json
from pathlib import Path

import openpyxl
import pyarrow.parquet as pq
import yaml
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.util import Inches
from typer.testing import CliRunner

from attestory.cli import app
from attestory.engine import commit_file, init_workspace
from attestory.parsers.pptx import diff_pptx_snapshots, extract_pptx_to_parquet

runner = CliRunner()


def _build_presentation(path: Path, metric: int, title: str) -> None:
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    textbox = slide.shapes.add_textbox(Inches(1), Inches(0.5), Inches(5), Inches(1))
    textbox.name = "Summary"
    textbox.text_frame.text = title

    table_shape = slide.shapes.add_table(
        2, 2, Inches(1), Inches(2), Inches(4), Inches(1)
    )
    table_shape.table.cell(0, 0).text = "Metric"
    table_shape.table.cell(0, 1).text = "Value"
    table_shape.table.cell(1, 0).text = "ARR"
    table_shape.table.cell(1, 1).text = str(metric)

    chart_data = CategoryChartData()
    chart_data.categories = ["Q4"]
    chart_data.add_series("ARR", [metric])
    slide.shapes.add_chart(
        XL_CHART_TYPE.COLUMN_CLUSTERED,
        Inches(1),
        Inches(4),
        Inches(5),
        Inches(3),
        chart_data,
    )
    presentation.save(path)


def _build_workbook(path: Path, formula: bool = True) -> None:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Revenue"
    sheet["B2"] = 250
    sheet["B4"] = "=SUM(B2:B3)" if formula else 428
    workbook.save(path)


def test_pptx_snapshot_and_semantic_diff(tmp_path: Path):
    v1 = tmp_path / "deck_v1.pptx"
    v2 = tmp_path / "deck_v2.pptx"
    _build_presentation(v1, 250, "Quarterly Results")
    _build_presentation(v2, 300, "Updated Results")
    first = tmp_path / "first.parquet"
    second = tmp_path / "second.parquet"

    stats = extract_pptx_to_parquet(v1, first)
    extract_pptx_to_parquet(v2, second)
    assert stats["slides"] == 1
    assert stats["text_elements"] == 1
    schema = pq.read_schema(first)
    assert schema.names == [
        "slide_num",
        "element_type",
        "element_id",
        "label",
        "val_str",
        "numeric_val",
    ]
    snapshot_rows = pq.read_table(first).to_pylist()
    assert any(row["element_id"] == "r1c1" for row in snapshot_rows)
    assert any(row["element_id"] == "series_1_pt_1" for row in snapshot_rows)

    changes = diff_pptx_snapshots(first, second)
    assert len(changes["text_changes"]) == 1
    assert len(changes["table_changes"]) == 1
    assert len(changes["chart_changes"]) == 1
    assert changes["chart_changes"][0]["numeric_delta"] == 50


def _build_nested_presentation(path: Path, kpi: int) -> None:
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])

    textbox = slide.shapes.add_textbox(Inches(0.5), Inches(0.25), Inches(4), Inches(1))
    textbox.name = "TopLevelSummary"
    textbox.text_frame.text = "Top-level summary"

    outer_group = slide.shapes.add_group_shape()
    outer_group.name = "OuterKPIGroup"
    inner_group = outer_group.shapes.add_group_shape()
    inner_group.name = "NestedKPIGroup"

    kpi_text = inner_group.shapes.add_textbox(
        Inches(0), Inches(0), Inches(3), Inches(1)
    )
    kpi_text.name = "NestedKPI"
    kpi_text.text_frame.text = f"ARR {kpi}"

    grouped_table = slide.shapes.add_table(
        2, 2, Inches(5), Inches(1), Inches(3), Inches(1)
    )
    grouped_table.name = "GroupedMetrics"
    grouped_table.table.cell(0, 0).text = "Metric"
    grouped_table.table.cell(0, 1).text = "Value"
    grouped_table.table.cell(1, 0).text = "ARR"
    grouped_table.table.cell(1, 1).text = str(kpi)
    inner_group._element.append(grouped_table._element)

    presentation.save(path)


def test_pptx_nested_groups_text_and_tables(tmp_path: Path):
    v1 = tmp_path / "nested_v1.pptx"
    v2 = tmp_path / "nested_v2.pptx"
    _build_nested_presentation(v1, 250)
    _build_nested_presentation(v2, 300)
    first = tmp_path / "nested_first.parquet"
    second = tmp_path / "nested_second.parquet"

    stats = extract_pptx_to_parquet(v1, first)
    extract_pptx_to_parquet(v2, second)

    assert stats["text_elements"] == 2
    assert stats["table_cells"] == 4
    snapshot_rows = pq.read_table(first).to_pylist()
    top_level = [row for row in snapshot_rows if row["element_id"] == "shape_1"]
    assert top_level == [
        {
            "slide_num": 1,
            "element_type": "text",
            "element_id": "shape_1",
            "label": "TopLevelSummary",
            "val_str": "Top-level summary",
            "numeric_val": None,
        }
    ]
    nested_text = [row for row in snapshot_rows if row["label"] == "NestedKPI"]
    assert len(nested_text) == 1
    assert nested_text[0]["element_id"].startswith("group_2/group_")
    grouped_table = [row for row in snapshot_rows if row["label"] == "GroupedMetrics"]
    assert len(grouped_table) == 4
    assert all(
        row["element_id"].startswith("group_2/group_")
        for row in grouped_table
    )

    changes = diff_pptx_snapshots(first, second)
    nested_kpi_change = next(
        change for change in changes["text_changes"] if change["label"] == "NestedKPI"
    )
    assert nested_kpi_change["old_val"] == "ARR 250"
    assert nested_kpi_change["new_val"] == "ARR 300"
    assert nested_kpi_change["numeric_delta"] == 50

    grouped_table_change = next(
        change
        for change in changes["table_changes"]
        if change["label"] == "GroupedMetrics" and change["old_val"] == "250"
    )
    assert grouped_table_change["new_val"] == "300"


def test_commit_routes_pptx_and_evaluates_rules(tmp_path: Path):
    init_workspace(tmp_path)
    xlsx_path = tmp_path / "Q4_Forecast.xlsx"
    pptx_path = tmp_path / "Board_Deck.pptx"
    _build_workbook(xlsx_path, formula=False)
    _build_presentation(pptx_path, 300, "ARR 300")
    rules = {
        "must_be_formula": ["Revenue!B4"],
        "assertions": [
            {
                "location": "Revenue!B2",
                "min_value": 300,
                "description": "Total ARR floor",
            },
            {"location": "Revenue!B2", "max_value": 200},
            {"location": "Revenue!B2", "equals": 251},
        ],
        "cross_file_checks": [
            {
                "pptx_file": "Board_Deck.pptx",
                "xlsx_file": "Q4_Forecast.xlsx",
                "slide_num": 1,
                "element_type": "chart",
                "label": "ARR",
                "location": "Revenue!B2",
                "tolerance": 0.01,
            }
        ],
    }
    (tmp_path / ".attestory" / "rules.yaml").write_text(
        yaml.safe_dump(rules), encoding="utf-8"
    )

    workbook_commit = commit_file(tmp_path, xlsx_path)
    assert {item["rule"] for item in workbook_commit["record"]["rule_violations"]} == {
        "must_be_formula",
        "numeric_assertion",
    }
    assert sum(
        item["rule"] == "numeric_assertion"
        for item in workbook_commit["record"]["rule_violations"]
    ) == 3
    presentation_commit = commit_file(tmp_path, pptx_path)
    violations = presentation_commit["record"]["rule_violations"]
    assert len(violations) == 1
    assert violations[0]["rule"] == "cross_file_check"
    assert violations[0]["actual"] == 300

    history = json.loads(
        (tmp_path / ".attestory" / "history.json").read_text(encoding="utf-8")
    )
    assert history["commits"][-1]["file"] == "Board_Deck.pptx"
    assert "RULE VIOLATION" in (
        tmp_path / ".attestory" / "changelog.md"
    ).read_text(encoding="utf-8")


def test_report_command_generates_standalone_html(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    init_workspace(tmp_path)
    deck = tmp_path / "Board_Deck.pptx"
    _build_presentation(deck, 250, "Baseline")
    assert runner.invoke(app, ["commit", str(deck), "-m", "Initial deck"]).exit_code == 0
    _build_presentation(deck, 300, "Revised deck")
    assert runner.invoke(app, ["commit", str(deck), "-m", "Updated deck"]).exit_code == 0

    diff_result = runner.invoke(app, ["diff", "Board_Deck.pptx"])
    assert diff_result.exit_code == 0, diff_result.stdout
    assert "SLIDE 1 CHART" in diff_result.stdout

    result = runner.invoke(app, ["report", "Board_Deck.pptx"])
    report_path = (
        tmp_path / ".attestory" / "reports" / "Board_Deck_v2_receipt.html"
    )
    assert result.exit_code == 0, result.stdout
    html = report_path.read_text(encoding="utf-8")
    assert "INCOMPLETE" not in html
    assert "PASS" in html
    assert "Updated deck" in html
    assert "Numeric delta" in html
    assert "change-chart" in html
    assert "https://" not in html
