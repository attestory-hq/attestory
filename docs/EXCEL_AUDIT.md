# Excel verification coverage

Attestory snapshots Excel workbooks from `openpyxl` formula/value views and
writes Zstandard-compressed Parquet fingerprints. It compares worksheets,
cells, formulas, normalized formulas, cached values, named ranges, and Excel
formula errors against a saved baseline.

## Supported checks

| Check | Result |
|---|---|
| Formula remains a formula | `FAIL` for a hardcoded replacement or missing formula |
| Formula changed | Included as a formula-edit finding |
| Formula cleared or removed | `FAIL` |
| Numeric assertion | `PASS` when executed and satisfied; `FAIL` on mismatch; `INCOMPLETE` when the value or cache is unavailable |
| Named range | Supported for a single worksheet cell |
| Formula cache | Missing/stale cached values are reported |
| LibreOffice recalculation | Runs against a temporary copy; failures make required inputs `INCOMPLETE` |
| External reference | Listed when detectable; it does not prove the external workbook is valid |
| Excel formula error | Reported, for example `#REF!`, `#DIV/0!`, `#SPILL!`, or `#CALC!` |

## Required check execution

Every manifest and receipt can list required checks with:

- `executed`
- `failed`
- `skipped`
- `unsupported`

Each entry includes the check, artifact location, required status, and reason.
Skipped or unsupported required checks block PASS.

## Known boundaries

- Formula macros and VBA execution are not performed.
- External workbooks are not opened or recalculated.
- Attestory does not replace the submitted workbook during LibreOffice
  recalculation.
- Single-cell named ranges are supported. Ambiguous ranges or unsupported
  range expressions are incomplete.
- LibreOffice is optional and must be installed for `--recalculate`.
- OneDrive/SharePoint synchronization and merge behavior are deployment-specific.

## Reproducible corpus

The labeled fixtures in
[`tests/fixtures/excel_verification/cases.yaml`](../tests/fixtures/excel_verification/cases.yaml)
cover:

- Row insertion with a named range
- Formula deletion/hardcoded replacement
- Missing formula caches
- Unresolved external references

The cases run in the pytest matrix in CI on Python 3.11 and 3.12.
