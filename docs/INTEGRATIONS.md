# Integrations and configuration

## Python API

```python
import attestory

attestory.snapshot("Q4_Forecast.xlsx", message="Approved baseline")
result = attestory.verify("Q4_Forecast.xlsx", message="Updated forecast")
print(result.to_dict())
```

`verify()` creates a snapshot and returns `PASS`, `FAIL`, or `INCOMPLETE`
alongside formula changes, formula errors, rule violations, and summary fields.

### Shared Excel edit-and-verify workflow

Assistant integrations can pass XLSX bytes directly and keep the original
bytes untouched:

```python
import attestory

prepared = attestory.prepare_excel_edit(original_bytes, filename="forecast.xlsx")
# Pass prepared.working_copy_bytes to the spreadsheet editor.
result = attestory.verify_excel_edit(
    prepared,
    candidate_bytes,
    filename="forecast.xlsx",
)
print(result.to_dict())
```

The original SHA-256 and an independent working copy are captured before
editing. Verification audits exact candidate bytes and returns a structured
`PASS`, `FAIL`, or `INCOMPLETE` outcome, cell-level findings, and an HTML
receipt bound to the candidate and baseline hashes. Only `PASS` sets
`verified=True`.

## Local storage

Attestory keeps audit data in the `.attestory/` directory beside the
workbook. It contains:

- `snapshots/` — versioned, content-addressed Parquet snapshots.
- `history.json` and `changelog.md` — recorded audit manifests and a readable
  change history.
- `rules.yaml` — workspace checks and business rules.
- `reports/` — standalone HTML audit receipts.
- `signing/` — the workspace Ed25519 signing keys. Protect the private key
  when backing up or sharing audit data.

Keep `.attestory/` with the workbook when you need its baseline and audit
history; a workbook copied without its snapshots cannot be compared to that
baseline.

## Claude hooks

Install the Claude Cowork or Claude Code skill:

```bash
attestory install-skill
attestory install-skill --target claude-code --hook
```

The Claude Code pre/post hooks preserve existing settings and only run for
supported Claude tool events. A pre-edit capture is not a substitute for a
candidate snapshot and cannot create a missing baseline for a new file.

## Rules

Edit `.attestory/rules.yaml` after initialization. A complete example is
available in [`examples/rules.yaml`](../examples/rules.yaml).

Supported rule groups include:

- `must_be_formula`
- `assertions` / `numeric_assertions`
- `cross_file_checks`
- Presentation, HTML, and CSV selectors

Missing baselines and unsupported required checks are `INCOMPLETE`. A mismatch
or failed assertion is `FAIL`.

## Offline manifest verification

Generate or load workspace signing keys:

```bash
attestory keygen
attestory verify-manifest .attestory/history.json Q4_Forecast.xlsx \
  --public-key .attestory/signing/public_key.pem \
  --audit-id <audit-id>
```

Manifests use deterministic JSON and Ed25519 signatures. The verifier reports
signature validity and workbook hash match separately from the recorded audit
outcome.

## Local HTTP bridge

```bash
pip install "attestory[server]"
attestory serve --host 127.0.0.1 --port 8765
```

The bridge is scoped to the current workspace and listens only on the supplied
host/port. Bind to localhost for local use; do not expose it directly to an
untrusted network. The OpenAPI bridge specification is at
[`skills/chatgpt-action/openapi.yaml`](../skills/chatgpt-action/openapi.yaml).
The ChatGPT Action prototype and its two-file fallback, consent disclosure,
and artifact-retention behavior are documented in
[`CHATGPT_ACTION.md`](CHATGPT_ACTION.md).
