---
name: attestory
description: Deterministic formula auditing, local Parquet versioning, and semantic diffing for Excel (.xlsx) workbooks inside Claude Cowork.
---

# Attestory Workflow Rules for Claude Cowork

Whenever you work in a folder containing `.xlsx` spreadsheets:
1. **Before editing:** Check if `.attestory/rules.yaml` and `.attestory/changelog.md` exist. Read them first so you preserve all saved business rules, metric exclusions, and formula structures from prior sessions. If `.attestory/` does not exist, run `attestory init`.
2. **Never overwrite dynamic Excel formulas (`=SUM(...)`, `=INDEX(...)`, etc.) with hardcoded numbers** unless explicitly instructed by the user.
3. **After editing any `.xlsx` file:** Run `attestory commit <filename.xlsx> -m "<summary of changes>"` via bash/terminal.
4. **Self-Correction Gate:** Inspect the `Attestory Receipt` output. If any `HARDCODE OVERRIDE` or Excel error (`#REF!`, `#DIV/0!`, `#VALUE!`) is flagged unintentionally, fix the workbook immediately and re-run `attestory commit` before presenting the result to the user.
