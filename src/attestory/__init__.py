"""Attestory — Local version control and deterministic math auditing for analytics."""

from attestory.api import (
    AuditResult,
    ExcelEditResult,
    PreparedExcelEdit,
    audit_excel_edit,
    edit_and_verify_excel,
    prepare_excel_edit,
    snapshot,
    verify,
    verify_excel_edit,
)
from attestory.signing import (
    deterministic_json,
    load_public_key,
    verify_audit,
)

__version__ = "0.3.2"

__all__ = [
    "AuditResult",
    "ExcelEditResult",
    "PreparedExcelEdit",
    "__version__",
    "audit_excel_edit",
    "deterministic_json",
    "edit_and_verify_excel",
    "load_public_key",
    "prepare_excel_edit",
    "snapshot",
    "verify",
    "verify_audit",
    "verify_excel_edit",
]
