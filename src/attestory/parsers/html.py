import re
from pathlib import Path
from urllib.parse import urlparse

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
from bs4 import BeautifulSoup

_NUMBER_PATTERN = re.compile(
    r"(?<![\w.])[-+]?\(?\s*(?:[$€£]\s*)?\d[\d,]*(?:\.\d+)?\s*%?\)?(?![\w.])"
)
_METRIC_SELECTOR = "[data-kpi], [data-metric], .kpi, .metric, .stat"


def _numeric_value(value: str) -> float | None:
    match = _NUMBER_PATTERN.search(value)
    if not match:
        return None
    number = (
        match.group()
        .strip()
        .replace(",", "")
        .replace("$", "")
        .replace("€", "")
        .replace("£", "")
        .replace("%", "")
        .strip()
    )
    negative = number.startswith("(") and number.endswith(")")
    try:
        parsed = float(number.strip("()").strip())
    except ValueError:
        return None
    return -parsed if negative else parsed


def _external_host(url: str) -> str | None:
    clean_url = url.strip()
    parsed = urlparse(f"https:{clean_url}" if clean_url.startswith("//") else clean_url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    return parsed.hostname.lower()


def extract_html_to_parquet(html_path: Path, parquet_out: Path) -> dict:
    """Extract HTML KPIs, table cells, and external telemetry references."""
    soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "lxml")
    rows = []

    def add_row(
        element_type: str,
        element_id: str,
        label: str,
        value: str,
        numeric_val: float | None = None,
    ) -> None:
        rows.append(
            {
                "element_type": element_type,
                "element_id": element_id,
                "label": label,
                "val_str": value,
                "numeric_val": numeric_val
                if numeric_val is not None
                else (
                    None if element_type == "script_audit" else _numeric_value(value)
                ),
            }
        )

    for table_idx, table in enumerate(soup.find_all("table"), start=1):
        table_rows = table.find_all("tr")
        headers = [
            cell.get_text(" ", strip=True)
            for cell in (table_rows[0].find_all(["th", "td"]) if table_rows else [])
        ]
        for row_idx, row in enumerate(table_rows, start=1):
            cells = row.find_all(["th", "td"])
            row_label = (
                cells[0].get_text(" ", strip=True) if cells and row_idx > 1 else ""
            )
            for col_idx, cell in enumerate(cells, start=1):
                value = cell.get_text(" ", strip=True)
                header = headers[col_idx - 1] if col_idx <= len(headers) else ""
                label = " / ".join(part for part in (header, row_label) if part)
                add_row(
                    "table",
                    f"table_{table_idx}_r{row_idx}c{col_idx}",
                    label,
                    value,
                )

    metric_nodes = list(soup.select(_METRIC_SELECTOR))
    seen_nodes = {id(node) for node in metric_nodes}
    for node in soup.find_all(re.compile(r"^h[1-3]$")):
        if (
            id(node) not in seen_nodes
            and _numeric_value(node.get_text(" ", strip=True)) is not None
        ):
            metric_nodes.append(node)

    for kpi_idx, node in enumerate(
        metric_nodes,
        start=1,
    ):
        value = node.get_text(" ", strip=True)
        if not value:
            continue
        label = (
            node.get("aria-label")
            or node.get("data-kpi")
            or node.get("data-metric")
            or node.get("id")
            or " ".join(node.get("class", []))
            or node.name
        )
        add_row("kpi", f"kpi_{kpi_idx}", str(label), value)

    audit_idx = 0
    for node in soup.find_all(["script", "img"]):
        src = node.get("src")
        if not src:
            continue
        host = _external_host(src)
        if host is None:
            continue
        audit_idx += 1
        add_row(
            "script_audit",
            f"external_{audit_idx}",
            "external script" if node.name == "script" else "external image",
            host,
        )

    schema = pa.schema(
        [
            ("element_type", pa.string()),
            ("element_id", pa.string()),
            ("label", pa.string()),
            ("val_str", pa.string()),
            ("numeric_val", pa.float64()),
        ]
    )
    pq.write_table(
        pa.Table.from_pylist(rows, schema=schema),
        parquet_out,
        compression="zstd",
    )
    return {
        "elements": len(rows),
        "kpis": sum(row["element_type"] == "kpi" for row in rows),
        "table_cells": sum(row["element_type"] == "table" for row in rows),
        "external_references": sum(
            row["element_type"] == "script_audit" for row in rows
        ),
    }


def diff_html_snapshots(prev_pq: Path, curr_pq: Path) -> dict:
    """Compare HTML snapshot values and report newly added external references."""
    con = duckdb.connect(":memory:")
    rows = con.execute(
        """
        SELECT
            COALESCE(p.element_type, c.element_type) AS element_type,
            COALESCE(p.element_id, c.element_id) AS element_id,
            COALESCE(c.label, p.label) AS label,
            p.val_str AS old_val,
            c.val_str AS new_val,
            p.numeric_val AS old_numeric,
            c.numeric_val AS new_numeric
        FROM read_parquet(?) p
        FULL OUTER JOIN read_parquet(?) c
            ON p.element_type = c.element_type AND p.element_id = c.element_id
        WHERE p.label IS DISTINCT FROM c.label
           OR p.val_str IS DISTINCT FROM c.val_str
           OR p.numeric_val IS DISTINCT FROM c.numeric_val
        ORDER BY element_type, element_id
        """,
        [prev_pq.as_posix(), curr_pq.as_posix()],
    ).fetchall()
    con.close()

    changes = [
        {
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
        "kpi_changes": [item for item in changes if item["element_type"] == "kpi"],
        "table_changes": [item for item in changes if item["element_type"] == "table"],
        "script_audit_changes": [
            item for item in changes if item["element_type"] == "script_audit"
        ],
    }
