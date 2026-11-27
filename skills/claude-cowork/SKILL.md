---
name: attestory
description: Deterministic formula auditing, local Parquet versioning, and semantic diffing for Excel (.xlsx) and PowerPoint (.pptx) files inside Claude Cowork or ChatGPT.
---

# Attestory Workflow Rules for Claude Cowork and ChatGPT

Whenever you work in a folder containing `.xlsx` spreadsheets or `.pptx` presentations:
1. **Before editing:** Check if `.attestory/rules.yaml` and `.attestory/changelog.md` exist. Read them first so you preserve all saved business rules, metric exclusions, and formula structures from prior sessions. If `.attestory/` does not exist, run `attestory init`.
2. **Never overwrite dynamic Excel formulas (`=SUM(...)`, `=INDEX(...)`, etc.) with hardcoded numbers** unless explicitly instructed by the user.
3. **After editing an `.xlsx` or `.pptx` file:** Run `attestory commit <filename> -m "<summary of changes>"` via bash/terminal.
4. **Inspect changes:** Run `attestory diff <filename>` to review the latest cell-level or slide-level semantic changes.
5. **Share an audit receipt:** Run `attestory report <filename>` to generate a standalone HTML receipt in `.attestory/reports/`.
6. **Self-Correction Gate:** Inspect the `Attestory Receipt` output. If any `HARDCODE OVERRIDE`, rule violation, or Excel error (`#REF!`, `#DIV/0!`, `#VALUE!`) is flagged unintentionally, fix the source file immediately and re-run `attestory commit` before presenting the result.

ChatGPT users can follow these same CLI commands in a local terminal. For
ChatGPT Actions integrations, use `skills/chatgpt-action/openapi.yaml` with an
Action-compatible HTTPS bridge that runs the local Attestory CLI.
