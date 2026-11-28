"""In-process Python API for snapshotting and verifying supported files."""

import hashlib
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from attestory.engine import commit_file, init_workspace


@dataclass(frozen=True)
class AuditResult:
    """Structured result from an audit, with a stable JSON representation."""

    passed: bool
    hardcoded_regressions: list[dict[str, str]]
    formula_errors: list[dict[str, str]]
    rule_violations: list[dict[str, Any]]
    summary_stats: dict[str, Any]
    record: dict[str, Any]
    diff: dict[str, Any] | None
    formula_changes: list[dict[str, Any]] = field(default_factory=list)
    formula_to_blanks: list[dict[str, Any]] = field(default_factory=list)
    missing_formula_cells: list[dict[str, Any]] = field(default_factory=list)
    status: str = "PASS"
    incomplete_findings: list[dict[str, Any]] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the versioned-by-shape diagnostic payload used by ``--json``."""
        return {
            "passed": self.passed,
            "status": self.status,
            "hardcoded_regressions": self.hardcoded_regressions,
            "formula_changes": self.formula_changes,
            "formula_to_blanks": self.formula_to_blanks,
            "missing_formula_cells": self.missing_formula_cells,
            "formula_errors": self.formula_errors,
            "rule_violations": self.rule_violations,
            "incomplete_findings": self.incomplete_findings or [],
            "summary_stats": self.summary_stats,
        }

    @classmethod
    def from_findings(
        cls,
        *,
        hardcoded_regressions: list[dict[str, Any]] | None = None,
        formula_changes: list[dict[str, Any]] | None = None,
        formula_to_blanks: list[dict[str, Any]] | None = None,
        missing_formula_cells: list[dict[str, Any]] | None = None,
        formula_errors: list[dict[str, Any]] | None = None,
        rule_violations: list[dict[str, Any]] | None = None,
        incomplete_findings: list[dict[str, Any]] | None = None,
        summary_stats: dict[str, Any] | None = None,
        record: dict[str, Any] | None = None,
        diff: dict[str, Any] | None = None,
    ) -> "AuditResult":
        hardcodes = [
            {
                "cell": item["location"],
                "old_formula": item["old_formula"],
                "new_hardcoded_value": item["new_hardcoded_val"],
            }
            for item in (hardcoded_regressions or [])
        ]
        changed_formulas = formula_changes or []
        cleared_formulas = formula_to_blanks or []
        removed_formulas = missing_formula_cells or []
        errors = [
            {
                "cell": item["location"],
                "error_type": item["error"],
                "formula": item.get("formula", ""),
            }
            for item in (formula_errors or [])
        ]
        violations = [
            {
                "rule_id": item["rule"],
                "location": item["location"],
                "expected": item.get("expected", ""),
                "actual": item.get("actual", ""),
                "remediation_hint": item.get("description")
                or item.get("remediation_hint")
                or "",
            }
            for item in (rule_violations or [])
        ]
        incomplete = incomplete_findings or []
        status = (
            "FAIL"
            if hardcodes or cleared_formulas or removed_formulas or errors or violations
            else "INCOMPLETE"
            if incomplete
            else "PASS"
        )
        return cls(
            passed=status == "PASS",
            hardcoded_regressions=hardcodes,
            formula_changes=changed_formulas,
            formula_to_blanks=cleared_formulas,
            missing_formula_cells=removed_formulas,
            formula_errors=errors,
            rule_violations=violations,
            summary_stats=summary_stats or {},
            record=record or {},
            diff=diff,
            status=status,
            incomplete_findings=incomplete,
        )


def _resolve_file_and_root(
    path: str | Path, root: str | Path | None = None
) -> tuple[Path, Path]:
    target = Path(path).expanduser().resolve()
    if not target.is_file():
        raise FileNotFoundError(
            f"Audit target does not exist or is not a file: {target}"
        )

    if root is None:
        cwd = Path.cwd().resolve()
        try:
            target.relative_to(cwd)
            workspace = cwd
        except ValueError:
            workspace = target.parent
    else:
        workspace = Path(root).expanduser().resolve()
        try:
            target.relative_to(workspace)
        except ValueError as error:
            raise ValueError(
                f"Audit target {target} is outside workspace {workspace}."
            ) from error
    return target, workspace


def snapshot(path: str | Path, message: str = "") -> dict[str, Any]:
    """Create a local snapshot and return the underlying commit and diff details.

    Relative paths are resolved from the current directory. Absolute paths inside
    the current directory use it as the workspace; otherwise their parent is used.
    """
    target, workspace = _resolve_file_and_root(path)
    return commit_file(workspace, target, message=message)


def verify(
    path: str | Path,
    message: str = "",
    *,
    root: str | Path | None = None,
) -> AuditResult:
    """Snapshot ``path`` and return a PASS/FAIL/INCOMPLETE result in-process."""
    target, workspace = _resolve_file_and_root(path, root=root)
    result = commit_file(workspace, target, message=message)
    record = result["record"]
    diff = result["diff"]
    hardcodes = diff.get("hardcode_overrides", []) if diff else []
    formula_changes = diff.get("formula_changes", []) if diff else []
    formula_to_blanks = diff.get("formula_to_blanks", []) if diff else []
    missing_formula_cells = diff.get("missing_formula_cells", []) if diff else []
    formula_errors = (
        record.get("stats", {}).get("errors", [])
        if target.suffix.lower() == ".xlsx"
        else []
    )
    violations = record.get("rule_violations", [])
    return AuditResult.from_findings(
        hardcoded_regressions=hardcodes,
        formula_changes=formula_changes,
        formula_to_blanks=formula_to_blanks,
        missing_formula_cells=missing_formula_cells,
        formula_errors=formula_errors,
        rule_violations=violations,
        incomplete_findings=record.get("incomplete_findings", []),
        summary_stats={
            "total_changes": diff["total_changes"] if diff else 0,
            "hardcode_overrides": len(hardcodes),
            "formula_changes": len(formula_changes),
            "formula_to_blanks": len(formula_to_blanks),
            "missing_formula_cells": len(missing_formula_cells),
            "formula_errors": len(formula_errors),
            "value_changes": len(diff.get("value_changes", [])) if diff else 0,
            "rule_violations": len(violations),
            "incomplete_findings": len(record.get("incomplete_findings", [])),
        },
        record=record,
        diff=diff,
    )


@dataclass(frozen=True)
class PreparedExcelEdit:
    """An immutable capture of original XLSX bytes and their working copy."""

    filename: str
    original_sha256: str
    working_copy: bytes

    @property
    def working_copy_bytes(self) -> bytes:
        return self.working_copy


@dataclass(frozen=True)
class ExcelEditResult:
    """Structured outcome for an edit-and-verify XLSX integration workflow."""

    status: str
    verified: bool
    original_sha256: str | None
    candidate_sha256: str | None
    baseline_sha256: str | None
    outcome: dict[str, Any]
    findings: list[dict[str, Any]]
    cell_findings: list[dict[str, Any]]
    record: dict[str, Any] | None
    diff: dict[str, Any] | None
    receipt_html: str | None = None
    receipt_sha256: str | None = None

    @property
    def passed(self) -> bool:
        return self.status == "PASS"

    @property
    def receipt(self) -> dict[str, Any] | None:
        if self.receipt_html is None:
            return None
        return {
            "format": "html",
            "sha256": self.receipt_sha256,
            "candidate_sha256": self.candidate_sha256,
            "baseline_sha256": self.baseline_sha256,
            "status": self.status,
            "html": self.receipt_html,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "outcome": self.status,
            "verified": self.verified,
            "passed": self.passed,
            "original_sha256": self.original_sha256,
            "candidate_sha256": self.candidate_sha256,
            "baseline_sha256": self.baseline_sha256,
            "findings": self.findings,
            "cell_findings": self.cell_findings,
            "incomplete_findings": self.outcome.get("incomplete_findings", []),
            "required_checks": self.record.get("required_checks", [])
            if self.record
            else [],
            "receipt": self.receipt,
        }


def _excel_filename(filename: str | Path) -> str:
    name = Path(filename).name
    if not name:
        name = "workbook.xlsx"
    if Path(name).suffix.lower() != ".xlsx":
        raise ValueError("The edit-and-verify workflow accepts XLSX bytes only.")
    return name


def prepare_excel_edit(
    original_bytes: bytes | bytearray | memoryview | None = None,
    *,
    original: bytes | bytearray | memoryview | None = None,
    filename: str | Path = "workbook.xlsx",
) -> PreparedExcelEdit:
    """Capture an original workbook and return an independent working copy."""
    if original_bytes is None:
        original_bytes = original
    if original_bytes is None:
        raise ValueError("The original XLSX baseline is unavailable.")

    name = _excel_filename(filename)
    capture = bytes(original_bytes)
    if not capture:
        raise ValueError("The original XLSX baseline is unavailable.")
    return PreparedExcelEdit(
        filename=name,
        original_sha256=hashlib.sha256(capture).hexdigest(),
        working_copy=capture,
    )


def _incomplete_excel_result(
    *,
    original_sha256: str | None,
    candidate_sha256: str | None,
    reason: str,
    rule: str,
) -> ExcelEditResult:
    finding = {
        "rule": rule,
        "location": "workbook",
        "reason": reason,
    }
    return ExcelEditResult(
        status="INCOMPLETE",
        verified=False,
        original_sha256=original_sha256,
        candidate_sha256=candidate_sha256,
        baseline_sha256=original_sha256,
        outcome={
            "status": "INCOMPLETE",
            "passed": False,
            "incomplete_findings": [finding],
        },
        findings=[{"kind": "incomplete", **finding}],
        cell_findings=[],
        record=None,
        diff=None,
    )


def verify_excel_edit(
    original_bytes: bytes | bytearray | memoryview | PreparedExcelEdit | None = None,
    candidate_bytes: bytes | bytearray | memoryview | None = None,
    *,
    original: bytes | bytearray | memoryview | PreparedExcelEdit | None = None,
    candidate: bytes | bytearray | memoryview | None = None,
    filename: str | Path = "workbook.xlsx",
) -> ExcelEditResult:
    """Audit exact original and candidate XLSX bytes with Attestory's engine."""
    if isinstance(original_bytes, PreparedExcelEdit):
        prepared = original_bytes
        original_bytes = prepared.working_copy
        if filename == "workbook.xlsx":
            filename = prepared.filename
    if original is None:
        original = original_bytes
    elif original_bytes is None:
        original_bytes = original
    if candidate is None:
        candidate = candidate_bytes
    elif candidate_bytes is None:
        candidate_bytes = candidate

    name = _excel_filename(filename)
    original_capture = (
        bytes(original)
        if original is not None and not isinstance(original, PreparedExcelEdit)
        else None
    )
    candidate_capture = bytes(candidate) if candidate is not None else None
    original_sha256 = (
        hashlib.sha256(original_capture).hexdigest()
        if original_capture
        else None
    )
    candidate_sha256 = (
        hashlib.sha256(candidate_capture).hexdigest()
        if candidate_capture
        else None
    )

    if original_capture is None:
        if candidate_capture is not None:
            try:
                from attestory.cli import generate_report

                name = _excel_filename(filename)
                with tempfile.TemporaryDirectory(prefix="attestory-excel-") as temporary:
                    root = Path(temporary)
                    workbook = root / name
                    init_workspace(root)
                    workbook.write_bytes(candidate_capture)
                    result = commit_file(
                        root,
                        workbook,
                        message="Assistant integration candidate without baseline",
                    )
                    report = generate_report(root, name)
                    receipt_html = (
                        root / report["path"]
                    ).read_text(encoding="utf-8")
            except (OSError, RuntimeError, ValueError, EOFError, zipfile.BadZipFile) as error:
                reason = (
                    "The original XLSX baseline is unavailable, and required "
                    f"candidate checks could not run: {error}"
                )
            else:
                record = result["record"]
                finding = {
                    "rule": "missing_baseline",
                    "location": "workbook",
                    "reason": "The original XLSX baseline is unavailable.",
                }
                findings = [{"kind": "incomplete", **finding}]
                receipt_sha256 = hashlib.sha256(
                    receipt_html.encode("utf-8")
                ).hexdigest()
                return ExcelEditResult(
                    status="INCOMPLETE",
                    verified=False,
                    original_sha256=None,
                    candidate_sha256=candidate_sha256,
                    baseline_sha256=None,
                    outcome={
                        "status": "INCOMPLETE",
                        "passed": False,
                        "incomplete_findings": [finding],
                        "required_checks": record.get("required_checks", []),
                    },
                    findings=findings,
                    cell_findings=[],
                    record=record,
                    diff=result["diff"],
                    receipt_html=receipt_html,
                    receipt_sha256=receipt_sha256,
                )
        else:
            reason = "The original XLSX baseline is unavailable."
        return _incomplete_excel_result(
            original_sha256=None,
            candidate_sha256=candidate_sha256,
            reason=reason,
            rule="missing_baseline",
        )
    if candidate_capture is None:
        return _incomplete_excel_result(
            original_sha256=original_sha256,
            candidate_sha256=None,
            reason="The candidate XLSX is unavailable.",
            rule="missing_candidate",
        )

    with tempfile.TemporaryDirectory(prefix="attestory-excel-") as temporary:
        root = Path(temporary)
        workbook = root / name
        try:
            from attestory.cli import generate_report

            init_workspace(root)
            workbook.write_bytes(original_capture)
            original_record = commit_file(
                root,
                workbook,
                message="Assistant integration original baseline",
            )
            workbook.write_bytes(candidate_capture)
            result = commit_file(
                root,
                workbook,
                message="Assistant integration candidate",
            )

            report = generate_report(root, name)
            report_path = root / report["path"]
            receipt_html = report_path.read_text(encoding="utf-8")
        except (OSError, RuntimeError, ValueError, EOFError, zipfile.BadZipFile) as error:
            return _incomplete_excel_result(
                original_sha256=original_sha256,
                candidate_sha256=candidate_sha256,
                reason=f"Required XLSX checks could not run: {error}",
                rule="audit_unavailable",
            )

    record = result["record"]
    diff = result["diff"]
    status = record.get("audit_status", "INCOMPLETE")
    baseline_sha256 = record.get("baseline_sha256") or original_record["record"]["sha256"]
    findings = record.get("findings", [])
    cell_findings = [
        finding
        for finding in findings
        if finding.get("location") not
        in {None, "", "workbook"}
        or finding.get("kind") in {
            "hardcode_override",
            "formula_change",
            "formula_to_blank",
            "missing_formula_cell",
            "value_change",
            "formula_error",
        }
    ]
    receipt_sha256 = hashlib.sha256(receipt_html.encode("utf-8")).hexdigest()
    outcome = {
        "status": status,
        "passed": status == "PASS",
        "record": record,
        "diff": diff,
        "incomplete_findings": record.get("incomplete_findings", []),
        "required_checks": record.get("required_checks", []),
    }
    return ExcelEditResult(
        status=status,
        verified=status == "PASS",
        original_sha256=original_sha256,
        candidate_sha256=candidate_sha256,
        baseline_sha256=baseline_sha256,
        outcome=outcome,
        findings=findings,
        cell_findings=cell_findings,
        record=record,
        diff=diff,
        receipt_html=receipt_html,
        receipt_sha256=receipt_sha256,
    )


def audit_excel_edit(
    original_bytes: bytes | bytearray | memoryview | PreparedExcelEdit | None = None,
    candidate_bytes: bytes | bytearray | memoryview | None = None,
    **kwargs: Any,
) -> ExcelEditResult:
    """Compatibility name for the shared Excel edit-and-verify integration API."""
    return verify_excel_edit(original_bytes, candidate_bytes, **kwargs)


def edit_and_verify_excel(
    original_bytes: bytes | bytearray | memoryview | PreparedExcelEdit | None = None,
    candidate_bytes: bytes | bytearray | memoryview | None = None,
    **kwargs: Any,
) -> ExcelEditResult:
    """Prepare and audit an Excel edit through the shared integration workflow."""
    return verify_excel_edit(original_bytes, candidate_bytes, **kwargs)
