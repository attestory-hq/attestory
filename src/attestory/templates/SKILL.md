---
name: attestory
description: Deterministic auditing, local Parquet versioning, and semantic diffing for Excel (.xlsx), PowerPoint (.pptx), HTML, and CSV files inside Claude Cowork or ChatGPT.
---

# Attestory Workflow Rules for Claude Cowork and ChatGPT

Whenever you work in a folder containing `.xlsx`, `.pptx`, `.html`, or `.csv` deliverables:
1. **Before editing:** Check if `.attestory/rules.yaml` and `.attestory/changelog.md` exist. Read them first so you preserve all saved business rules, metric exclusions, and formula structures from prior sessions. If `.attestory/` does not exist, run `attestory init`.
2. **Never overwrite dynamic Excel formulas (`=SUM(...)`, `=INDEX(...)`, etc.) with hardcoded numbers** unless explicitly instructed by the user.
3. **After editing a supported file:** Run `attestory commit <filename> -m "<summary of changes>"` via bash/terminal. HTML snapshots include KPI/table values and external script or tracking-image domains; CSV snapshots include schema, row count, null counts, and numeric summaries.
4. **Inspect changes:** Run `attestory diff <filename>` to review the latest semantic changes.
5. **Share an audit receipt:** Run `attestory report <filename>` to generate a standalone HTML receipt in `.attestory/reports/`.
6. **Self-Correction Gate:** Inspect the `Attestory Receipt` output. If any `HARDCODE OVERRIDE`, rule violation, or Excel error (`#REF!`, `#DIV/0!`, `#VALUE!`) is flagged unintentionally, fix the source file immediately and re-run `attestory commit` before presenting the result.
7. **Agent Output:** Use `attestory commit <filename> --json` when parsing audit results programmatically. A non-zero exit code means the JSON diagnostics contain a hardcode regression, formula error, or rule violation.

ChatGPT users can follow these same CLI commands in a local terminal. Run
`attestory serve --host 127.0.0.1 --port 8765` to start the optional local
FastAPI bridge. For ChatGPT Actions integrations, use
`skills/chatgpt-action/openapi.yaml` with a separately secured HTTPS adapter;
do not expose the unauthenticated local bridge to an untrusted network.
