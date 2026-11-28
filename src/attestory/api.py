"""In-process Python API for snapshotting and verifying supported files."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from attestory.engine import commit_file


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
