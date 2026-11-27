# Attestory

**Make sure your numbers are Attestoried.**
Local version control, deterministic formula auditing, and semantic diffs for AI-generated Excel (`.xlsx`), PowerPoint (`.pptx`), and HTML deliverables.

---

## Why Attestory?
AI workspaces like **Claude Cowork** and **ChatGPT** make editing spreadsheets effortless—but they also silently overwrite dynamic formulas (`=SUM(...)`) with hardcoded numbers, introduce `#REF!` errors, and create `Forecast_v7_FINAL.xlsx` chaos across email and Slack.

**Attestory** creates a lightweight, local-first `.attestory/` audit folder right alongside your spreadsheets:
- **Zero Cloud Uploads Required:** Structural snapshots are stored locally as Zstandard-compressed **Apache Parquet** files inside `.attestory/`.
- **Deterministic Math & Hardcode Audits:** Uses `openpyxl` and **DuckDB** SQL diffs to catch silent formula-to-hardcode overwrites and Excel formula errors in milliseconds.
- **Native AI Continuity:** Drop `skills/claude-cowork/SKILL.md` into your workspace so Claude Cowork reads saved rules before editing and self-corrects formula mistakes before presenting results.

---

## Quickstart

```bash
pip install -e .

# 1. Initialize .attestory/ in your working folder
attestory init

# 2. Audit and snapshot your workbook
attestory commit Q4_Forecast.xlsx -m "Initial baseline"

# 3. After editing in Excel, Claude Cowork, or ChatGPT, commit again to catch silent regressions
attestory commit Q4_Forecast.xlsx -m "Updated November actuals"

# 4. Inspect cell-level formula & value diffs
attestory diff Q4_Forecast.xlsx
```

---

## License
Apache-2.0
