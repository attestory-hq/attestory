# Claude Cowork plugin

Install **Attestory for Cowork** from the Attestory marketplace repository in
Claude Desktop: open **Customize → Plugins → Add → Add marketplace**, enter
`https://github.com/attestory-hq/attestory`, then add the plugin. Start a Cowork
task, grant access to the workbook folder when prompted, and ask Claude to edit
and audit the workbook in one request. You can also run
`/attestory-cowork:audit-workbook`.

The plugin does not rely on file-write interception. Cowork has no supported
pre-write hook for all direct editor, connector, and script-driven writes. The
skill instead stages the workbook before editing and directs every edit,
including edits made by scripts, to a separate sibling candidate. The original
is left untouched. Direct edits to the original or edits performed outside the
managed candidate flow cannot be claimed as audited.

The plugin stages a baseline under `.attestory/cowork/baselines/`, keeps the
candidate beside the original, and writes an HTML receipt under
`.attestory/cowork/reports/`. Workbook bytes are read and processed locally by
the bundled script; this plugin has no network connector and does not upload
workbooks. The user must authorize local read/write access to the workbook
folder in Cowork. Captured baselines remain in `.attestory/cowork/baselines/`
until the user removes them. The script uses Python's standard library only
and runs as a Cowork local script action;
the user does not install Python packages or run terminal commands. If Cowork
cannot execute its local script runtime or access the folder, the workflow
stops with `INCOMPLETE`.

## Outcomes and supported checks

- **PASS**: The candidate was saved, its formula structure was compared with
  the captured original, no formula override/deletion or cached Excel error was
  found, and no known unsupported required check was detected.
- **FAIL**: A baseline formula was hardcoded, cleared, or removed, or an Excel
  error cell was found.
- **INCOMPLETE**: A baseline/candidate is unavailable, the workbook cannot be
  read, a required worksheet/formula check is unsupported, an
  `.attestory/rules.yaml` file is present, or external workbook references or
  defined names require evaluation.

The plugin checks `.xlsx` formula structure and cached error cells. It does
not calculate formulas, verify external workbooks, evaluate `.attestory`
business rules, or cryptographically sign its receipt. For the full Attestory
engine, rule semantics, and signed manifests, use the Python package workflow
documented in [Integrations](INTEGRATIONS.md).

## Edit-path coverage

| Edit path | Behavior |
|---|---|
| Cowork edits to the plugin-staged candidate | Supported; audit the saved candidate against the captured baseline. |
| Script edits that target the staged candidate | Supported; run the audit after the script has finished saving. |
| Cowork or external tools directly overwrite the original | Not intercepted; no pre-write capture is guaranteed. |
| Background scripts, spreadsheet applications, or connectors outside the flow | Not intercepted; use the managed candidate if they are part of the task. |

The receipt binds the candidate and baseline hashes and states the plugin's
coverage. It is an audit summary, not a cryptographic signature or proof that
all spreadsheet behavior was evaluated.
