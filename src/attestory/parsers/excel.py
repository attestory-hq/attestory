import re
from pathlib import Path

import openpyxl
import pyarrow as pa
import pyarrow.parquet as pq

EXCEL_ERRORS = {"#REF!", "#DIV/0!", "#VALUE!", "#NAME?", "#N/A", "#NUM!"}


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
    }
