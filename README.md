# Attestory

**Make sure your numbers are Attestoried.**
Local version control, deterministic formula auditing, and semantic diffs for AI-generated Excel (`.xlsx`) and PowerPoint (`.pptx`) deliverables.

---

## Why Attestory?
AI workspaces like **Claude Cowork** and **ChatGPT** make editing spreadsheets effortless—but they also silently overwrite dynamic formulas (`=SUM(...)`) with hardcoded numbers, introduce `#REF!` errors, and create `Forecast_v7_FINAL.xlsx` chaos across email and Slack.

**Attestory** creates a lightweight, local-first `.attestory/` audit folder right alongside your spreadsheets:
- **Zero Cloud Uploads Required:** Structural snapshots are stored locally as Zstandard-compressed **Apache Parquet** files inside `.attestory/`.
- **Deterministic Math & Hardcode Audits:** Uses `openpyxl` and **DuckDB** SQL diffs to catch silent formula-to-hardcode overwrites and Excel formula errors in milliseconds.
- **PowerPoint Semantic Diffs:** Extracts slide text, native table cells, and embedded chart values into Zstandard-compressed Parquet snapshots, with DuckDB-powered comparisons between versions.
- **Persistent Assertions & Receipts:** Configure formula and numeric guardrails in `.attestory/rules.yaml`, then generate self-contained HTML audit receipts.
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

# Snapshot and compare a presentation the same way
attestory commit Board_Deck.pptx -m "Initial board deck"
attestory commit Board_Deck.pptx -m "Updated ARR chart"
attestory diff Board_Deck.pptx

# Create a standalone report in .attestory/reports/
attestory report Board_Deck.pptx
```

## Rules and cross-file checks

Edit `.attestory/rules.yaml` to add deterministic assertions. Excel formula and
numeric rules are evaluated whenever the workbook is committed. Cross-file
checks run when either configured file is committed and compare the latest
snapshots available for both files:

```yaml
must_be_formula:
  - Revenue!B4
assertions:
  - location: Revenue!B4
    min_value: 100000
    description: Total ARR floor
  - location: Revenue!B5
    max_value: 250000
    description: Keep the forecast within approved bounds
cross_file_checks:
  - pptx_file: Board_Deck.pptx
    xlsx_file: Q4_Forecast.xlsx
    slide_num: 2
    element_type: chart
    label: ARR
    location: Revenue!B4
    tolerance: 0.01
    description: Slide 2 ARR chart must match the forecast
```

Numeric assertions support `min_value`, `max_value`, and `equals`. Cross-file
checks select a chart/table/text item by slide and `label`, `metric` text, or
`element_id`, then compare its first numeric value with the Excel cell. Rule
violations appear in the terminal receipt, commit history, and changelog.

## ChatGPT Actions

`skills/chatgpt-action/openapi.yaml` describes the commit, diff, and report
operations for an Action-compatible local bridge. ChatGPT Actions require an
HTTPS-reachable API adapter; the OpenAPI file is a contract and does not itself
expose a local CLI process to ChatGPT.

---

## License
Apache-2.0
