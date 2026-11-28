import hashlib
import json
import os
import posixpath
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import ZipFile

import openpyxl
import pyarrow as pa
import pyarrow.parquet as pq
from openpyxl.formula import Tokenizer
from openpyxl.formula.tokenizer import TokenizerError

EXCEL_ERRORS = {
    "#NULL!",
    "#DIV/0!",
    "#VALUE!",
    "#REF!",
    "#NAME?",
    "#NUM!",
    "#N/A",
    "#GETTING_DATA",
    "#SPILL!",
    "#CALC!",
    "#FIELD!",
    "#DATA!",
    "#BLOCKED!",
    "#UNKNOWN!",
    "#CONNECT!",
    "#BUSY!",
}


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


def _stored_cell_ids(xlsx_path: Path) -> dict[str, set[str]]:
    """Return worksheet cell references physically stored in the workbook XML."""
    main_ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    document_rel_ns = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    package_rel_ns = "{http://schemas.openxmlformats.org/package/2006/relationships}"
    with ZipFile(xlsx_path) as workbook:
        workbook_xml = ET.fromstring(workbook.read("xl/workbook.xml"))
        relationships_xml = ET.fromstring(
            workbook.read("xl/_rels/workbook.xml.rels")
        )
        relationship_targets = {
            relationship.attrib["Id"]: relationship.attrib["Target"]
            for relationship in relationships_xml.findall(f"{package_rel_ns}Relationship")
        }
        cells_by_sheet = {}
        for sheet in workbook_xml.findall(f".//{main_ns}sheet"):
            target = relationship_targets[sheet.attrib[f"{document_rel_ns}id"]]
            worksheet_path = (
                target.lstrip("/")
                if target.startswith("/")
                else posixpath.normpath(posixpath.join("xl", target))
            )
            cell_ids = set()
            with workbook.open(worksheet_path) as worksheet_xml:
                for _, element in ET.iterparse(worksheet_xml, events=("end",)):
                    if element.tag == f"{main_ns}c":
                        cell_ids.add(element.attrib["r"])
                        element.clear()
                    elif element.tag == f"{main_ns}row":
                        element.clear()
            cells_by_sheet[sheet.attrib["name"]] = cell_ids
    return cells_by_sheet


def recalculate_xlsx(xlsx_path: Path) -> list[dict]:
    """Recalculate a working copy and return formula cache changes."""
    return recalculate_xlsx_with_metadata(xlsx_path)["changed_cached_values"]


def recalculate_xlsx_with_metadata(xlsx_path: Path) -> dict:
    """Recalculate a temporary copy and return success/failure provenance."""
    soffice = shutil.which("soffice")
    if soffice is None:
        raise RuntimeError(
            "LibreOffice (soffice) is required for --recalculate. "
            "Install LibreOffice or omit the option."
        )

    submitted_sha256 = hashlib.sha256(xlsx_path.read_bytes()).hexdigest()
    before = _formula_cache_values(xlsx_path)
    engine_version = "unknown"
    try:
        version_result = subprocess.run(
            [soffice, "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        engine_version = (version_result.stdout or version_result.stderr).strip() or "unknown"
    except (OSError, subprocess.TimeoutExpired, ValueError):
        engine_version = "unknown"

    with tempfile.TemporaryDirectory(
        prefix=".attestory-recalc-", dir=xlsx_path.parent
    ) as temp_dir:
        working_copy = Path(temp_dir) / xlsx_path.name
        shutil.copy2(xlsx_path, working_copy)
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
                    str(working_copy),
                ],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {
                "status": "failed",
                "submitted_sha256": submitted_sha256,
                "recalculated_sha256": None,
                "calculation_engine": "LibreOffice",
                "calculation_engine_version": engine_version,
                "changed_cached_values": [],
                "audit_artifact": "submitted_workbook",
                "audit_artifact_sha256": submitted_sha256,
                "error": "LibreOffice recalculation timed out after 120 seconds.",
            }

        recalculated = output_dir / working_copy.name
        if result.returncode != 0 or not recalculated.is_file():
            detail = (result.stderr or result.stdout).strip()
            message = "LibreOffice failed to recalculate the workbook."
            if detail:
                message += f" {detail}"
            return {
                "status": "failed",
                "submitted_sha256": submitted_sha256,
                "recalculated_sha256": None,
                "calculation_engine": "LibreOffice",
                "calculation_engine_version": engine_version,
                "changed_cached_values": [],
                "audit_artifact": "submitted_workbook",
                "audit_artifact_sha256": submitted_sha256,
                "error": message,
            }

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
        recalculated_sha256 = hashlib.sha256(recalculated.read_bytes()).hexdigest()
        descriptor, persisted_copy = tempfile.mkstemp(
            prefix=".attestory-recalculated-", suffix=".xlsx"
        )
        os.close(descriptor)
        persisted_copy = Path(persisted_copy)
        shutil.copy2(recalculated, persisted_copy)
        return {
            "status": "success",
            "submitted_sha256": submitted_sha256,
            "recalculated_sha256": recalculated_sha256,
            "calculation_engine": "LibreOffice",
            "calculation_engine_version": engine_version,
            "changed_cached_values": warnings,
            "audit_artifact": "recalculated_working_copy",
            "audit_artifact_sha256": recalculated_sha256,
            "audit_path": persisted_copy,
            "error": None,
        }


def normalize_formula(formula: str | None) -> str | None:
    """Normalize formula tokens without changing quoted literals or intersections."""
    if not formula or not isinstance(formula, str) or not formula.startswith("="):
        return None
    try:
        tokens = Tokenizer(formula).items
    except TokenizerError:
        return formula

    normalized = []
    for index, token in enumerate(tokens):
        if token.type == "WHITE-SPACE":
            previous = next(
                (item for item in reversed(tokens[:index]) if item.type != "WHITE-SPACE"),
                None,
            )
            following = next(
                (
                    item
                    for item in tokens[index + 1 :]
                    if item.type != "WHITE-SPACE"
                ),
                None,
            )
            if (
                previous
                and following
                and (
                    previous.type == "PAREN" and previous.subtype == "CLOSE"
                    or previous.type == "OPERAND"
                    and previous.subtype == "RANGE"
                )
                and (
                    following.type == "PAREN" and following.subtype == "OPEN"
                    or following.type == "OPERAND"
                    and following.subtype == "RANGE"
                )
            ):
                normalized.append(" ")
            continue
        if token.type == "OPERAND" and token.subtype == "TEXT":
            normalized.append(token.value)
        else:
            normalized.append(token.value.upper())
    return "".join(normalized)


def extract_xlsx_to_parquet(xlsx_path: Path, parquet_out: Path) -> dict:
    """Parse an .xlsx workbook in read-only mode and write a Zstandard-compressed Parquet fingerprint."""
    wb_formulas = openpyxl.load_workbook(xlsx_path, data_only=False, read_only=True)
    wb_values = openpyxl.load_workbook(xlsx_path, data_only=True, read_only=True)

    rows = []
    errors = []
    formula_cache_warnings = []
    sheet_names = list(wb_formulas.sheetnames)
    stored_cells = _stored_cell_ids(xlsx_path)

    named_ranges: dict[str, dict] = {}
    for name, defined_name in wb_formulas.defined_names.items():
        try:
            destinations = list(defined_name.destinations)
            cells = []
            for sheet_name, ref in destinations:
                min_col, min_row, max_col, max_row = openpyxl.utils.range_boundaries(
                    ref
                )
                for row in range(min_row, max_row + 1):
                    for col in range(min_col, max_col + 1):
                        cell_id = openpyxl.utils.get_column_letter(col) + str(row)
                        cells.append(f"{sheet_name}!{cell_id}")
        except (AttributeError, TypeError, ValueError):
            named_ranges[name] = {"status": "unsupported"}
            continue
        if len(cells) == 1:
            named_ranges[name] = {"status": "single", "location": cells[0]}
        elif len(cells) > 1:
            named_ranges[name] = {
                "status": "ambiguous",
                "locations": sorted(set(cells)),
            }
        else:
            named_ranges[name] = {"status": "unsupported"}

    named_ranges_json = json.dumps(named_ranges, sort_keys=True)

    for sheet_name in sheet_names:
        ws_f = wb_formulas[sheet_name]
        ws_v = wb_values[sheet_name]

        for r_idx, (row_f, row_v) in enumerate(
            zip(ws_f.iter_rows(values_only=True), ws_v.iter_rows(values_only=True)),
            start=1,
        ):
            for c_idx, (raw_f, raw_v) in enumerate(zip(row_f, row_v), start=1):
                col_letter = openpyxl.utils.get_column_letter(c_idx)
                cell_id = f"{col_letter}{r_idx}"
                if (
                    raw_f is None
                    and raw_v is None
                    and cell_id not in stored_cells.get(sheet_name, set())
                ):
                    continue

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
                        "named_ranges": named_ranges_json,
                    }
                )

    if not rows:
        rows.append(
            {
                "sheet_name": "",
                "cell_id": "",
                "is_formula": False,
                "formula": "",
                "norm_formula": "",
                "val_str": "",
                "named_ranges": named_ranges_json,
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
            ("named_ranges", pa.string()),
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
