# Sample audit receipt

**Outcome: FAIL** · `Q4_Forecast.xlsx` · Version 2

## Formula override

- **Cell:** `Revenue!B4`
- **Baseline formula:** `=SUM(B2:B3)`
- **Edited value:** `412000`
- **Finding:** The required formula was replaced by a hardcoded value.

The value is still `$412,000`, but the formula is gone. The `must_be_formula`
check fails intentionally for this example.

## Required checks

- `must_be_formula` at `Revenue!B4` — **failed**
- Cross-file ARR check against `Board_Deck.pptx` — **failed** (expected
  `412000`; found `428000`)

[Open the standalone HTML receipt](sample_receipt.html).
