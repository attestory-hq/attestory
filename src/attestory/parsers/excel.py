import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import openpyxl
import pyarrow as pa
import pyarrow.parquet as pq

EXCEL_ERRORS = {"#REF!", "#DIV/0!", "#VALUE!", "#NAME?", "#N/A", "#NUM!"}


def _formula_cache_values(xlsx_path: Path) -> dict[str, tuple[str, object]]:
    wb_formulas = openpyxl.load_workbook(xlsx_path, data_only=False, read_only=True)
    wb_values = openpyxl.load_workbook(xlsx_path, data_only=True, read_only=True)
    formulas = {}
    for sheet_name in wb_formulas.sheetnames:
        ws_formulas = wb_formulas[sheet_name]
        ws_values = wb_values[sheet_name]
        for row_idx, (formula_row, value_row) in enumerate(
            zip(
                ws_formulas.iter_rows(values_only=True),
                ws_values.iter_rows(values_only=True),
            ),
            start=1,
        ):
            for col_idx, (formula, value) in enumerate(
                zip(formula_row, value_row), start=1
            ):
                if isinstance(formula, str) and formula.startswith("="):
                    cell = openpyxl.utils.get_column_letter(col_idx) + str(row_idx)
                    formulas[f"{sheet_name}!{cell}"] = (formula, value)
    wb_formulas.close()
    wb_values.close()
    return formulas


def recalculate_xlsx(xlsx_path: Path) -> list[dict]:
    """Recalculate an XLSX with LibreOffice and report formula cache values that changed."""
    soffice = shutil.which("soffice")
    if soffice is None:
        raise RuntimeError(
            "LibreOffice (soffice) is required for --recalculate. "
            "Install LibreOffice or omit the option."
        )

    before = _formula_cache_values(xlsx_path)
    with tempfile.TemporaryDirectory(
        prefix=".attestory-recalc-", dir=xlsx_path.parent
    ) as temp_dir:
        output_dir = Path(temp_dir) / "output"
        profile_dir = Path(temp_dir) / "profile"
        output_dir.mkdir()
        profile_dir.mkdir()
        try:
            result = subprocess.run(
                [
                    soffice,
                    f"-env:UserInstallation={profile_dir.as_uri()}",
                    "--headless",
                    "--convert-to",
                    "xlsx",
                    "--outdir",
                    str(output_dir),
                    str(xlsx_path),
                ],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(
                "LibreOffice recalculation timed out after 120 seconds."
            ) from error

        recalculated = output_dir / xlsx_path.name
        if result.returncode != 0 or not recalculated.is_file():
            detail = (result.stderr or result.stdout).strip()
            message = "LibreOffice failed to recalculate the workbook."
            if detail:
                message += f" {detail}"
            raise RuntimeError(message)

        after = _formula_cache_values(recalculated)
        warnings = []
        for location, (formula, previous_value) in before.items():
            current = after.get(location)
            if current is None or formula != current[0]:
                continue
            recalculated_value = current[1]
            if previous_value == recalculated_value:
                continue
            warnings.append(
                {
                    "location": location,
                    "formula": formula,
                    "warning": "uncached" if previous_value is None else "stale",
                    "previous_value": previous_value,
                    "recalculated_value": recalculated_value,
                }
            )
        os.replace(recalculated, xlsx_path)
    return warnings


def normalize_formula(formula: str | None) -> str | None:
    """Normalize Excel formulas by stripping whitespace and uppercasing for AST-like comparison."""
    if not formula or not isinstance(formula, str) or not formula.startswith("="):
        return None
    return re.sub(r"\s+", "", formula).upper()


def extract_xlsx_to_parquet(xlsx_path: Path, parquet_out: Path) -> dict:
    """Parse an .xlsx workbook in read-only mode and write a Zstandard-compressed Parquet fingerprint."""
    wb_formulas = openpyxl.load_workbook(xlsx_path, data_only=False, read_only=True)
    wb_values = openpyxl.load_workbook(xlsx_path, data_only=True, read_only=True)

    rows = []
    errors = []
    formula_cache_warnings = []
    sheet_names = list(wb_formulas.sheetnames)

    for sheet_name in sheet_names:
        ws_f = wb_formulas[sheet_name]
        ws_v = wb_values[sheet_name]

        for r_idx, (row_f, row_v) in enumerate(
            zip(ws_f.iter_rows(values_only=True), ws_v.iter_rows(values_only=True)),
            start=1,
        ):
            for c_idx, (raw_f, raw_v) in enumerate(zip(row_f, row_v), start=1):
                if raw_f is None and raw_v is None:
                    continue

                col_letter = openpyxl.utils.get_column_letter(c_idx)
                cell_id = f"{col_letter}{r_idx}"
                is_formula = isinstance(raw_f, str) and raw_f.startswith("=")
                formula_str = raw_f if is_formula else ""
                norm_formula = normalize_formula(raw_f) or ""
                val_str = "" if raw_v is None else str(raw_v)

                if is_formula and raw_v is None:
                    formula_cache_warnings.append(
                        {
                            "location": f"{sheet_name}!{cell_id}",
                            "formula": formula_str,
                            "warning": "uncached",
                        }
                    )

                if val_str in EXCEL_ERRORS:
                    errors.append(
                        {
                            "location": f"{sheet_name}!{cell_id}",
                            "error": val_str,
                            "formula": formula_str,
                        }
                    )

                rows.append(
                    {
                        "sheet_name": sheet_name,
                        "cell_id": cell_id,
                        "is_formula": is_formula,
                        "formula": formula_str,
                        "norm_formula": norm_formula,
                        "val_str": val_str,
                    }
                )

    wb_formulas.close()
    wb_values.close()

    schema = pa.schema(
        [
            ("sheet_name", pa.string()),
            ("cell_id", pa.string()),
            ("is_formula", pa.bool_()),
            ("formula", pa.string()),
            ("norm_formula", pa.string()),
            ("val_str", pa.string()),
        ]
    )
    table = pa.Table.from_pylist(rows, schema=schema)
    pq.write_table(table, parquet_out, compression="zstd")

    return {
        "sheets": len(sheet_names),
        "populated_cells": len(rows),
        "errors": errors,
        "formula_cache_warnings": formula_cache_warnings,
    }
