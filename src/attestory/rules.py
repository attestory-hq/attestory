from pathlib import Path

import duckdb
import yaml


def _latest_snapshot(history: dict, filename: str) -> Path | None:
    commits = [
        commit for commit in history.get("commits", []) if commit["file"] == filename
    ]
    return Path(commits[-1]["snapshot"]) if commits else None


def _resolve_snapshot(root: Path, snapshot: Path | None) -> Path | None:
    if snapshot is None or snapshot.is_absolute():
        return snapshot
    return root / snapshot


def _cell_value(snapshot: Path, location: str) -> tuple[bool, str, bool]:
    if "!" not in location:
        raise ValueError(f"Invalid Excel cell location {location!r}; expected Sheet!A1")
    sheet_name, cell_id = location.rsplit("!", 1)
    con = duckdb.connect(":memory:")
    row = con.execute(
        "SELECT val_str, is_formula FROM read_parquet(?) WHERE sheet_name = ? AND cell_id = ?",
        [snapshot.as_posix(), sheet_name, cell_id],
    ).fetchone()
    con.close()
    return (row is not None, row[0] if row else "", bool(row[1]) if row else False)


def _numeric_cell(snapshot: Path, location: str) -> tuple[bool, float | None]:
    exists, value, _ = _cell_value(snapshot, location)
    if not exists or value == "":
        return exists, None
    try:
        return exists, float(value)
    except (TypeError, ValueError):
        return exists, None


def _pptx_metric(snapshot: Path, check: dict) -> tuple[bool, float | None]:
    clauses = ["slide_num = ?"]
    params: list[object] = [int(check["slide_num"])]
    if check.get("element_type"):
        clauses.append("element_type = ?")
        params.append(check["element_type"])
    selector = check.get("label") or check.get("metric")
    if selector:
        clauses.append("(label = ? OR contains(lower(val_str), lower(?)))")
        params.extend([selector, selector])
    if check.get("element_id"):
        clauses.append("element_id = ?")
        params.append(check["element_id"])
    if not selector and not check.get("element_id"):
        raise ValueError(
            "Each cross_file_checks entry requires a metric, label, or element_id selector."
        )
    con = duckdb.connect(":memory:")
    rows = con.execute(
        f"""
        SELECT numeric_val FROM read_parquet(?)
        WHERE {" AND ".join(clauses)}
        ORDER BY CASE WHEN label = ? THEN 0 ELSE 1 END
        """,
        [snapshot.as_posix(), *params, selector or ""],
    ).fetchall()
    con.close()
    values = [row[0] for row in rows if row[0] is not None]
    return (bool(values), float(values[0]) if values else None)


def _snapshot_metric(
    snapshot: Path, filename: str, selector: dict, check: dict
) -> tuple[bool, float | None]:
    extension = Path(filename).suffix.lower()
    if extension == ".xlsx":
        location = selector.get("location") or check.get("location")
        if not location:
            raise ValueError(f"Cross-file metric for {filename} requires a location.")
        return _numeric_cell(snapshot, location)
    if extension == ".pptx":
        return _pptx_metric(snapshot, {**check, **selector})
    if extension == ".csv":
        column_name = selector.get("column_name") or selector.get("column")
        metric_type = selector.get("metric_type")
        label = selector.get("label")
        if not column_name:
            raise ValueError(f"Cross-file metric for {filename} requires column_name.")
        if metric_type is None:
            metric_type = "dataset" if column_name == "__all__" else "summary_stat"
        if label is None:
            label = "row_count" if metric_type == "dataset" else "SUM"
        con = duckdb.connect(":memory:")
        row = con.execute(
            """
            SELECT numeric_val FROM read_parquet(?)
            WHERE metric_type = ? AND column_name = ? AND label = ?
            """,
            [snapshot.as_posix(), metric_type, column_name, label],
        ).fetchone()
        con.close()
        return (row is not None and row[0] is not None, float(row[0]) if row else None)
    if extension == ".html":
        clauses = []
        params: list[object] = [snapshot.as_posix()]
        if selector.get("element_type"):
            clauses.append("element_type = ?")
            params.append(selector["element_type"])
        if selector.get("element_id"):
            clauses.append("element_id = ?")
            params.append(selector["element_id"])
        label = selector.get("label") or selector.get("metric")
        if label:
            clauses.append("(label = ? OR contains(lower(val_str), lower(?)))")
            params.extend([label, label])
        if not label and not selector.get("element_id"):
            raise ValueError(
                f"Cross-file metric for {filename} requires label or element_id."
            )
        con = duckdb.connect(":memory:")
        row = con.execute(
            f"""
            SELECT numeric_val FROM read_parquet(?)
            WHERE {" AND ".join(clauses)}
            ORDER BY CASE WHEN label = ? THEN 0 ELSE 1 END
            LIMIT 1
            """,
            [*params, label or ""],
        ).fetchone()
        con.close()
        return (row is not None and row[0] is not None, float(row[0]) if row else None)
    raise ValueError(f"Unsupported cross-file metric format: {filename}")


def evaluate_rules(
    root: Path, target_file: Path, curr_pq: Path, history: dict
) -> list[dict]:
    """Evaluate configured formula, numeric, and cross-file assertions."""
    rules_path = root / ".attestory" / "rules.yaml"
    if not rules_path.exists():
        return []
    config = yaml.safe_load(rules_path.read_text(encoding="utf-8")) or {}
    violations = []
    target_is_excel = target_file.suffix.lower() == ".xlsx"

    if target_is_excel:
        for location in config.get("must_be_formula", []):
            exists, value, is_formula = _cell_value(curr_pq, location)
            if not exists or not is_formula:
                violations.append(
                    {
                        "rule": "must_be_formula",
                        "location": location,
                        "expected": "Excel formula",
                        "actual": value if exists else "missing cell",
                        "description": f"{location} must remain a formula.",
                    }
                )

        for assertion in config.get("assertions", config.get("numeric_assertions", [])):
            location = assertion["location"]
            exists, value = _numeric_cell(curr_pq, location)
            expected = {
                key: assertion[key]
                for key in ("min_value", "max_value", "equals")
                if key in assertion
            }
            failed = not exists or value is None
            if value is not None:
                failed = (
                    ("min_value" in expected and value < expected["min_value"])
                    or ("max_value" in expected and value > expected["max_value"])
                    or ("equals" in expected and value != expected["equals"])
                )
            if failed:
                violations.append(
                    {
                        "rule": "numeric_assertion",
                        "location": location,
                        "expected": expected,
                        "actual": value if exists else "missing cell",
                        "description": assertion.get("description", ""),
                    }
                )

    for check in config.get("cross_file_checks", []):
        first_file = check.get("file_a")
        second_file = check.get("file_b")
        if first_file and second_file:
            file_specs = (
                (first_file, check.get("metric_a", {})),
                (second_file, check.get("metric_b", {})),
            )
        else:
            pptx_file = check.get("pptx_file") or check.get("presentation")
            xlsx_file = check.get("xlsx_file") or check.get("workbook")
            if not pptx_file or not xlsx_file:
                raise ValueError(
                    "Each cross_file_checks entry requires file_a/file_b or "
                    "pptx_file/xlsx_file."
                )
            file_specs = (
                (pptx_file, {**check, "slide_num": check.get("slide_num")}),
                (xlsx_file, {"location": check.get("location")}),
            )
        if target_file.name not in {file_specs[0][0], file_specs[1][0]}:
            continue

        snapshots = []
        for filename, selector in file_specs:
            if filename == target_file.name:
                snapshots.append(curr_pq)
            else:
                snapshots.append(
                    _resolve_snapshot(root, _latest_snapshot(history, filename))
                )
        if not all(snapshots):
            continue
        first_exists, first_value = _snapshot_metric(
            snapshots[0], file_specs[0][0], file_specs[0][1], check
        )
        second_exists, second_value = _snapshot_metric(
            snapshots[1], file_specs[1][0], file_specs[1][1], check
        )
        tolerance = float(check.get("tolerance", 0.01))
        if (
            not first_exists
            or not second_exists
            or first_value is None
            or second_value is None
            or abs(first_value - second_value) > tolerance
        ):
            location = f"{file_specs[0][0]} / {file_specs[1][0]}"
            violations.append(
                {
                    "rule": "cross_file_check",
                    "location": location,
                    "expected": second_value,
                    "actual": first_value,
                    "tolerance": tolerance,
                    "description": check.get("description", ""),
                }
            )
    return violations
