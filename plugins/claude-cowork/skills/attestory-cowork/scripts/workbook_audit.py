"""Stage an XLSX working copy and create a local formula-structure receipt."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import shutil
import sys
import tempfile
import uuid
import zipfile
from pathlib import Path
from xml.etree import ElementTree


class WorkbookAuditError(Exception):
    """Raised when an XLSX workbook cannot be inspected safely."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as workbook:
        for chunk in iter(lambda: workbook.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(element: ElementTree.Element, name: str) -> ElementTree.Element | None:
    return next(
        (child for child in element if _local_name(child.tag) == name),
        None,
    )


def _relationship_target(value: str) -> str:
    target = value.replace("\\", "/")
    if target.startswith("/"):
        target = target.lstrip("/")
    elif not target.startswith("xl/"):
        target = f"xl/{target}"
    parts: list[str] = []
    for part in target.split("/"):
        if part == "..":
            if not parts:
                raise WorkbookAuditError("Workbook contains an unsafe sheet path.")
            parts.pop()
        elif part not in {"", "."}:
            parts.append(part)
    return "/".join(parts)


def _workbook_cells(
    path: Path,
) -> tuple[dict[str, dict[str, tuple[str | None, bool]]], list[dict], list[dict]]:
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            required = {
                "xl/workbook.xml",
                "xl/_rels/workbook.xml.rels",
            }
            if not required.issubset(names):
                raise WorkbookAuditError("Required XLSX workbook metadata is missing.")

            workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
            relationships = ElementTree.fromstring(
                archive.read("xl/_rels/workbook.xml.rels")
            )
            targets = {}
            for relationship in relationships:
                if _local_name(relationship.tag) == "Relationship":
                    targets[relationship.attrib.get("Id", "")] = (
                        relationship.attrib.get("Target", "")
                    )

            sheets: dict[str, dict[str, tuple[str | None, bool]]] = {}
            formula_errors: list[dict] = []
            incomplete: list[dict] = []
            for sheet in workbook.iter():
                if _local_name(sheet.tag) != "sheet":
                    continue
                sheet_name = sheet.attrib.get("name", "")
                relationship_id = next(
                    (
                        value
                        for key, value in sheet.attrib.items()
                        if _local_name(key) == "id"
                    ),
                    "",
                )
                target = targets.get(relationship_id)
                if not sheet_name or not target:
                    incomplete.append(
                        {
                            "kind": "incomplete",
                            "location": "workbook",
                            "reason": "A worksheet relationship could not be resolved.",
                        }
                    )
                    continue
                sheet_path = _relationship_target(target)
                if sheet_path not in names:
                    incomplete.append(
                        {
                            "kind": "incomplete",
                            "location": sheet_name,
                            "reason": "A worksheet file is missing from the XLSX archive.",
                        }
                    )
                    continue
                sheet_xml = ElementTree.fromstring(archive.read(sheet_path))
                cells: dict[str, tuple[str | None, bool]] = {}
                for cell in sheet_xml.iter():
                    if _local_name(cell.tag) != "c":
                        continue
                    location = cell.attrib.get("r")
                    if not location:
                        incomplete.append(
                            {
                                "kind": "incomplete",
                                "location": sheet_name,
                                "reason": "A cell has no address.",
                            }
                        )
                        continue
                    formula = _child(cell, "f")
                    value = _child(cell, "v")
                    inline_value = _child(cell, "is")
                    has_value = (
                        value is not None and value.text is not None
                    ) or inline_value is not None
                    cells[location] = (None, has_value)
                    if formula is not None:
                        formula_text = formula.text or ""
                        if not formula_text or formula.attrib.get("t") in {
                            "array",
                            "shared",
                            "dataTable",
                        }:
                            incomplete.append(
                                {
                                    "kind": "incomplete",
                                    "location": f"{sheet_name}!{location}",
                                    "reason": "Shared or data-table formula structure is not supported.",
                                }
                            )
                        cells[location] = (formula_text, has_value)
                    if cell.attrib.get("t") == "e":
                        value = _child(cell, "v")
                        formula_errors.append(
                            {
                                "kind": "formula_error",
                                "location": f"{sheet_name}!{location}",
                                "error": value.text
                                if value is not None
                                else "Unknown Excel error",
                            }
                        )
                sheets[sheet_name] = cells

            if "xl/externalLinks/" in "/".join(names):
                incomplete.append(
                    {
                        "kind": "incomplete",
                        "location": "workbook",
                        "reason": "External workbook references are not evaluated.",
                    }
                )
            if any(
                _local_name(element.tag) == "definedName" for element in workbook.iter()
            ):
                incomplete.append(
                    {
                        "kind": "incomplete",
                        "location": "workbook",
                        "reason": "Defined names and named-range rules are not evaluated.",
                    }
                )
            return sheets, formula_errors, incomplete
    except (OSError, zipfile.BadZipFile, ElementTree.ParseError) as error:
        raise WorkbookAuditError(f"Could not read XLSX workbook: {error}") from error


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
        delete=False,
    ) as temporary:
        temporary_path = Path(temporary.name)
    try:
        shutil.copyfile(source, temporary_path)
        os.replace(temporary_path, destination)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _local_directory(workspace: Path, path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    resolved = path.resolve()
    try:
        resolved.relative_to(workspace)
    except ValueError as error:
        raise WorkbookAuditError(
            f"Audit data directory resolves outside the workbook folder: {path}"
        ) from error
    return resolved


def stage(workbook_path: Path) -> dict:
    requested_source = workbook_path.expanduser()
    if requested_source.is_symlink():
        raise WorkbookAuditError("Symbolic-link workbooks are not supported.")
    original = workbook_path.expanduser().resolve(strict=True)
    if not original.is_file() or original.suffix.lower() != ".xlsx":
        raise WorkbookAuditError("The source must be an existing .xlsx file.")

    workspace = original.parent
    resolved_audit_dir = _local_directory(
        workspace, workspace / ".attestory" / "cowork"
    )
    baseline_dir = _local_directory(workspace, resolved_audit_dir / "baselines")

    audit_id = uuid.uuid4().hex
    baseline = baseline_dir / f"{audit_id}.xlsx"
    candidate = workspace / (
        f"{original.stem}.attestory-candidate-{audit_id[:12]}{original.suffix}"
    )
    _atomic_copy(original, baseline)
    try:
        _atomic_copy(baseline, candidate)
    except BaseException:
        baseline.unlink(missing_ok=True)
        raise
    return {
        "status": "STAGED",
        "staged": True,
        "audit_id": audit_id,
        "source": str(original),
        "baseline": str(baseline),
        "candidate": str(candidate),
        "baseline_sha256": _sha256(baseline),
        "message": "Original bytes captured; edit only the candidate workbook.",
    }


def _audit_findings(baseline: Path, candidate: Path) -> tuple[list[dict], list[dict]]:
    baseline_sheets, _, baseline_notes = _workbook_cells(baseline)
    candidate_sheets, candidate_findings, candidate_notes = _workbook_cells(candidate)
    findings: list[dict] = candidate_findings
    incomplete = [*baseline_notes, *candidate_notes]

    for sheet_name, baseline_cells in baseline_sheets.items():
        candidate_cells = candidate_sheets.get(sheet_name, {})
        for cell, (old_formula, _) in baseline_cells.items():
            if old_formula is None:
                continue
            location = f"{sheet_name}!{cell}"
            if cell not in candidate_cells:
                findings.append(
                    {
                        "kind": "missing_formula_cell",
                        "location": location,
                        "reason": "A cell containing a baseline formula is missing.",
                    }
                )
            elif candidate_cells[cell][0] is None:
                value_present = candidate_cells[cell][1]
                findings.append(
                    {
                        "kind": "hardcode_override"
                        if value_present
                        else "formula_to_blank",
                        "location": location,
                        "old_formula": old_formula,
                        "reason": (
                            "A baseline formula was replaced by a non-formula value."
                            if value_present
                            else "A baseline formula was cleared."
                        ),
                    }
                )
            elif candidate_cells[cell][0] != old_formula:
                findings.append(
                    {
                        "kind": "formula_change",
                        "location": location,
                        "old_formula": old_formula,
                        "new_formula": candidate_cells[cell][0],
                    }
                )

    for sheet_name, candidate_cells in candidate_sheets.items():
        baseline_cells = baseline_sheets.get(sheet_name, {})
        for cell, (formula, _) in candidate_cells.items():
            if formula and baseline_cells.get(cell, (None, False))[0] is None:
                findings.append(
                    {
                        "kind": "formula_added",
                        "location": f"{sheet_name}!{cell}",
                        "new_formula": formula,
                    }
                )
    return findings, incomplete


def _receipt_html(result: dict) -> str:
    findings = result["findings"] + result["incomplete_findings"]

    def details(item: dict) -> str:
        detail = item.get("reason") or item.get("error")
        if detail:
            return str(detail)
        if "old_formula" in item or "new_formula" in item:
            return f"Formula: {item.get('old_formula', '(none)')} -> {item.get('new_formula', '(none)')}"
        return ""

    rows = "".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            html.escape(str(item.get("kind", "finding"))),
            html.escape(str(item.get("location", "workbook"))),
            html.escape(details(item)),
        )
        for item in findings
    )
    if not rows:
        rows = '<tr><td colspan="3">No formula regressions or incomplete checks found.</td></tr>'
    return f"""<!doctype html>
<html lang="en"><meta charset="utf-8"><title>Attestory Cowork audit receipt</title>
<h1>Attestory Cowork audit receipt</h1>
<p><strong>Outcome: {html.escape(result["status"])}</strong></p>
<p>Baseline SHA-256: <code>{html.escape(result["baseline_sha256"] or "unavailable")}</code></p>
<p>Candidate SHA-256: <code>{html.escape(result["candidate_sha256"] or "unavailable")}</code></p>
<p>This local receipt checks formula structure and cached Excel error cells.
It does not recalculate formulas or evaluate custom rules.</p>
<table border="1"><thead><tr><th>Kind</th><th>Location</th><th>Details</th></tr></thead>
<tbody>{rows}</tbody></table>
</html>
"""


def audit(baseline_path: Path, candidate_path: Path) -> dict:
    requested_candidate = candidate_path.expanduser().absolute()
    workspace = requested_candidate.parent.resolve()
    candidate = workspace / requested_candidate.name
    requested_baseline = baseline_path.expanduser().absolute()
    baseline = requested_baseline
    result = {
        "status": "INCOMPLETE",
        "verified": False,
        "baseline": str(baseline),
        "candidate": str(candidate),
        "baseline_sha256": None,
        "candidate_sha256": None,
        "findings": [],
        "incomplete_findings": [],
        "receipt": None,
    }
    candidate_safe = (
        not requested_candidate.is_symlink()
        and requested_candidate.parent.resolve() == workspace
        and ".attestory-candidate-" in requested_candidate.name
        and requested_candidate.suffix.lower() == ".xlsx"
    )
    if not candidate_safe:
        result["incomplete_findings"].append(
            {
                "kind": "incomplete",
                "location": "workbook",
                "reason": "The candidate is not a regular staged sibling .xlsx file.",
            }
        )
    expected_baseline_dir = workspace / ".attestory" / "cowork" / "baselines"
    resolved_baseline_dir = expected_baseline_dir.resolve()
    try:
        resolved_baseline_dir.relative_to(workspace)
        requested_baseline.resolve().relative_to(resolved_baseline_dir)
    except ValueError:
        result["incomplete_findings"].append(
            {
                "kind": "incomplete",
                "location": "workbook",
                "reason": "The baseline is outside this workbook's Attestory staging folder.",
            }
        )
    baseline_safe = (
        not requested_baseline.is_symlink()
        and ".xlsx" == requested_baseline.suffix.lower()
        and resolved_baseline_dir.is_relative_to(workspace)
        and requested_baseline.resolve().is_relative_to(resolved_baseline_dir)
    )
    if requested_baseline.is_symlink():
        result["incomplete_findings"].append(
            {
                "kind": "incomplete",
                "location": "workbook",
                "reason": "Symbolic-link baselines are not supported.",
            }
        )
    if not baseline_safe or not baseline.is_file():
        result["incomplete_findings"].append(
            {
                "kind": "incomplete",
                "location": "workbook",
                "reason": "The captured original baseline is unavailable.",
            }
        )
    else:
        result["baseline_sha256"] = _sha256(baseline)
    if not candidate_safe or not candidate.is_file():
        result["incomplete_findings"].append(
            {
                "kind": "incomplete",
                "location": "workbook",
                "reason": "The saved candidate workbook is unavailable.",
            }
        )
    else:
        result["candidate_sha256"] = _sha256(candidate)

    rules_path = workspace / ".attestory" / "rules.yaml"
    if rules_path.exists() or rules_path.is_symlink():
        result["incomplete_findings"].append(
            {
                "kind": "incomplete",
                "location": ".attestory/rules.yaml",
                "reason": "Attestory rules files are not evaluated by this Cowork plugin version.",
            }
        )
    if baseline_safe and candidate_safe and baseline.is_file() and candidate.is_file():
        try:
            result["findings"], workbook_notes = _audit_findings(baseline, candidate)
            result["incomplete_findings"].extend(workbook_notes)
        except WorkbookAuditError as error:
            result["incomplete_findings"].append(
                {
                    "kind": "incomplete",
                    "location": "workbook",
                    "reason": str(error),
                }
            )

    blocking_kinds = {
        "formula_error",
        "hardcode_override",
        "formula_to_blank",
        "missing_formula_cell",
    }
    if any(item.get("kind") in blocking_kinds for item in result["findings"]):
        result["status"] = "FAIL"
    elif not result["incomplete_findings"]:
        result["status"] = "PASS"
        result["verified"] = True

    receipt_dir = _local_directory(
        workspace, workspace / ".attestory" / "cowork" / "reports"
    )
    receipt_path = receipt_dir / f"{candidate.stem}_receipt.html"
    receipt_path.write_text(_receipt_html(result), encoding="utf-8")
    result["receipt"] = str(receipt_path)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    stage_command = commands.add_parser(
        "stage", help="Capture an original and create a candidate."
    )
    stage_command.add_argument("workbook", type=Path)
    audit_command = commands.add_parser(
        "audit", help="Audit a saved candidate against its baseline."
    )
    audit_command.add_argument("baseline", type=Path)
    audit_command.add_argument("candidate", type=Path)
    arguments = parser.parse_args(argv)
    try:
        result = (
            stage(arguments.workbook)
            if arguments.command == "stage"
            else audit(arguments.baseline, arguments.candidate)
        )
    except (OSError, WorkbookAuditError) as error:
        result = {
            "status": "INCOMPLETE",
            "verified": False,
            "error": str(error),
        }
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] in {"PASS", "STAGED"} else 1


if __name__ == "__main__":
    sys.exit(main())
