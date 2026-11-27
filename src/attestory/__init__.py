"""Attestory — Local version control and deterministic math auditing for analytics."""

from attestory.api import AuditResult, snapshot, verify

__version__ = "0.3.2"

__all__ = ["AuditResult", "__version__", "snapshot", "verify"]
