from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

_NUMERIC_TYPES = ("int", "uint", "float", "double", "decimal", "hugeint")


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _as_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def extract_csv_to_parquet(csv_path: Path, parquet_out: Path) -> dict:
    """Fingerprint CSV dataset size, column schema, nulls, and numeric summaries."""
    con = duckdb.connect(":memory:")
    csv_ref = "read_csv_auto(?)"
    parameters = [csv_path.resolve().as_posix()]
    row_count = con.execute(f"SELECT count(*) FROM {csv_ref}", parameters).fetchone()[0]
    columns = con.execute(f"DESCRIBE SELECT * FROM {csv_ref}", parameters).fetchall()
    rows = [
        {
            "metric_type": "dataset",
            "column_name": "__all__",
            "label": "row_count",
            "val_str": str(row_count),
            "numeric_val": float(row_count),
        }
    ]

    for column in columns:
        column_name, data_type = column[0], column[1]
        quoted_name = _quote_identifier(column_name)
        null_count = con.execute(
            f"SELECT count(*) FILTER (WHERE {quoted_name} IS NULL) FROM {csv_ref}",
            parameters,
        ).fetchone()[0]
        rows.extend(
            [
                {
                    "metric_type": "schema",
                    "column_name": column_name,
                    "label": "data_type",
                    "val_str": data_type,
                    "numeric_val": None,
                },
                {
                    "metric_type": "schema",
                    "column_name": column_name,
                    "label": "null_count",
                    "val_str": str(null_count),
                    "numeric_val": float(null_count),
                },
            ]
        )
        if any(marker in data_type.lower() for marker in _NUMERIC_TYPES):
            stats = con.execute(
                f"""
                SELECT sum({quoted_name}), avg({quoted_name}),
                       min({quoted_name}), max({quoted_name})
                FROM {csv_ref}
                """,
                parameters,
            ).fetchone()
            for label, value in zip(("SUM", "AVG", "MIN", "MAX"), stats):
                numeric_value = _as_float(value)
                rows.append(
                    {
                        "metric_type": "summary_stat",
                        "column_name": column_name,
                        "label": label,
                        "val_str": "" if value is None else str(value),
                        "numeric_val": numeric_value,
                    }
                )
    con.close()

    schema = pa.schema(
        [
            ("metric_type", pa.string()),
            ("column_name", pa.string()),
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
        "rows": row_count,
        "columns": len(columns),
        "metrics": len(rows),
    }


def diff_csv_snapshots(prev_pq: Path, curr_pq: Path) -> dict:
    """Detect CSV schema, row-count, and numeric-summary changes."""
    con = duckdb.connect(":memory:")
    rows = con.execute(
        """
        SELECT
            COALESCE(p.metric_type, c.metric_type) AS metric_type,
            COALESCE(p.column_name, c.column_name) AS column_name,
            COALESCE(p.label, c.label) AS label,
            p.val_str AS old_val,
            c.val_str AS new_val,
            p.numeric_val AS old_numeric,
            c.numeric_val AS new_numeric
        FROM read_parquet(?) p
        FULL OUTER JOIN read_parquet(?) c
            ON p.metric_type = c.metric_type
           AND p.column_name = c.column_name
           AND p.label = c.label
        WHERE p.val_str IS DISTINCT FROM c.val_str
           OR p.numeric_val IS DISTINCT FROM c.numeric_val
        ORDER BY metric_type, column_name, label
        """,
        [prev_pq.as_posix(), curr_pq.as_posix()],
    ).fetchall()
    con.close()

    changes = [
        {
            "metric_type": metric_type,
            "column_name": column_name,
            "label": label,
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
            metric_type,
            column_name,
            label,
            old_val,
            new_val,
            old_numeric,
            new_numeric,
        ) in rows
    ]
    schema_changes = [item for item in changes if item["metric_type"] == "schema"]
    return {
        "total_changes": len(changes),
        "changes": changes,
        "schema_changes": schema_changes,
        "summary_changes": [
            item for item in changes if item["metric_type"] != "schema"
        ],
    }
