import re
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE

_NUMBER_PATTERN = re.compile(
    r"(?<![\w.])[-+]?\(?\s*(?:[$€£]\s*)?\d[\d,]*(?:\.\d+)?\s*%?\)?(?![\w.])"
)


def _numeric_value(value: str) -> float | None:
    match = _NUMBER_PATTERN.search(value)
    if not match:
        return None
    number = match.group().strip().replace(",", "").replace("$", "")
    number = number.replace("€", "").replace("£", "").replace("%", "").strip()
    negative = number.startswith("(") and number.endswith(")")
    number = number.strip("()").strip()
    try:
        parsed = float(number)
    except ValueError:
        return None
    return -parsed if negative else parsed


def extract_pptx_to_parquet(pptx_path: Path, parquet_out: Path) -> dict:
    """Extract presentation text, table cells, and chart values into a Parquet snapshot."""
    presentation = Presentation(pptx_path)
    rows = []

    def add_row(slide_num: int, element_type: str, element_id: str, label: str, value: str):
        rows.append(
            {
                "slide_num": slide_num,
                "element_type": element_type,
                "element_id": element_id,
                "label": label,
                "val_str": value,
                "numeric_val": _numeric_value(value),
            }
        )

    for slide_num, slide in enumerate(presentation.slides, start=1):
        def walk_shapes(
            container,
            slide_num: int = slide_num,
            group_path: tuple[str, ...] = (),
        ) -> None:
            for shape_idx, shape in enumerate(container, start=1):
                hierarchy = (
                    group_path + (f"group_{shape_idx}",)
                    if shape.shape_type == MSO_SHAPE_TYPE.GROUP
                    else group_path
                )

                if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
                    walk_shapes(
                        shape.shapes,
                        slide_num=slide_num,
                        group_path=hierarchy,
                    )
                    continue

                prefix = "/".join(hierarchy)
                prefix = f"{prefix}/" if prefix else ""

                if shape.has_table:
                    for row_idx, row in enumerate(shape.table.rows, start=1):
                        for col_idx, cell in enumerate(row.cells, start=1):
                            add_row(
                                slide_num,
                                "table",
                                (
                                    f"{prefix}table_r{row_idx}c{col_idx}"
                                    if hierarchy
                                    else f"r{row_idx}c{col_idx}"
                                ),
                                shape.name,
                                cell.text.strip(),
                            )
                elif shape.has_chart:
                    for series_idx, series in enumerate(shape.chart.series, start=1):
                        for point_idx, value in enumerate(series.values, start=1):
                            add_row(
                                slide_num,
                                "chart",
                                (
                                    f"{prefix}chart_series_{series_idx}_pt_{point_idx}"
                                    if hierarchy
                                    else f"series_{series_idx}_pt_{point_idx}"
                                ),
                                series.name,
                                str(value),
                            )
                elif shape.has_text_frame:
                    text = shape.text_frame.text.strip()
                    if text:
                        add_row(
                            slide_num,
                            "text",
                            (
                                f"{prefix}text_shape_{shape_idx}"
                                if hierarchy
                                else f"shape_{shape_idx}"
                            ),
                            shape.name,
                            text,
                        )

        walk_shapes(slide.shapes)

    schema = pa.schema(
        [
            ("slide_num", pa.int32()),
            ("element_type", pa.string()),
            ("element_id", pa.string()),
            ("label", pa.string()),
            ("val_str", pa.string()),
            ("numeric_val", pa.float64()),
        ]
    )
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), parquet_out, compression="zstd")
    return {
        "slides": len(presentation.slides),
        "elements": len(rows),
        "text_elements": sum(row["element_type"] == "text" for row in rows),
        "table_cells": sum(row["element_type"] == "table" for row in rows),
        "chart_values": sum(row["element_type"] == "chart" for row in rows),
    }


def diff_pptx_snapshots(prev_pq: Path, curr_pq: Path) -> dict:
    """Compare two presentation snapshots by their stable slide element identifiers."""
    con = duckdb.connect(":memory:")
    prev_path = prev_pq.as_posix().replace("'", "''")
    curr_path = curr_pq.as_posix().replace("'", "''")
    rows = con.execute(
        f"""
        SELECT
            COALESCE(p.slide_num, c.slide_num) AS slide_num,
            COALESCE(p.element_type, c.element_type) AS element_type,
            COALESCE(p.element_id, c.element_id) AS element_id,
            COALESCE(c.label, p.label) AS label,
            p.val_str AS old_val,
            c.val_str AS new_val,
            p.numeric_val AS old_numeric,
            c.numeric_val AS new_numeric
        FROM read_parquet('{prev_path}') p
        FULL OUTER JOIN read_parquet('{curr_path}') c
            ON p.slide_num = c.slide_num
           AND p.element_type = c.element_type
           AND p.element_id = c.element_id
        WHERE p.val_str IS DISTINCT FROM c.val_str
           OR p.label IS DISTINCT FROM c.label
        ORDER BY slide_num, element_type, element_id
        """
    ).fetchall()
    con.close()

    changes = [
        {
            "slide_num": slide_num,
            "element_type": element_type,
            "element_id": element_id,
            "label": label or "",
            "old_val": old_val,
            "new_val": new_val,
            "old_numeric": old_numeric,
            "new_numeric": new_numeric,
            "numeric_delta": (
                new_numeric - old_numeric
                if old_numeric is not None and new_numeric is not None
                else None
            ),
        }
        for (
            slide_num,
            element_type,
            element_id,
            label,
            old_val,
            new_val,
            old_numeric,
            new_numeric,
        ) in rows
    ]
    return {
        "total_changes": len(changes),
        "changes": changes,
        "text_changes": [change for change in changes if change["element_type"] == "text"],
        "table_changes": [change for change in changes if change["element_type"] == "table"],
        "chart_changes": [change for change in changes if change["element_type"] == "chart"],
    }
