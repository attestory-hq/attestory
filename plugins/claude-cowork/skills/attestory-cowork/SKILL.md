---
name: attestory-cowork
description: Use when a user asks to edit or audit an Excel workbook in Claude Cowork and wants a safe candidate plus an Attestory audit receipt.
---

# Attestory workbook workflow for Cowork

Use the local workbook folder the user authorized. Workbook bytes are processed
by the bundled Python-standard-library script on that device; they are not sent
to Attestory or another service.

## Required managed-copy workflow

Cowork does not provide a supported pre-write interception hook for every
editor and script-driven write path. Therefore, do not edit the user's original
workbook. Keep writes auditable by using this plugin-managed copy flow:

1. Confirm the user authorized the workbook's folder and read any existing
   `.attestory/rules.yaml` and `.attestory/changelog.md`. Do not modify the
   original or metadata files.
2. Before editing, ask Cowork to run the bundled local script (not to ask the
   user to use a terminal):

   ```text
   python3 "${CLAUDE_SKILL_DIR}/scripts/workbook_audit.py" stage "<workbook.xlsx>"
   ```

   This captures the original bytes under `.attestory/cowork/baselines/` and
   creates a visible sibling candidate named `*.attestory-candidate-*.xlsx`.
   Do not proceed if staging fails or reports `INCOMPLETE`.
3. Make every requested edit, including script-driven edits, to the candidate
   path only. Save the candidate before auditing. Never run an edit script
   against the original.
4. Audit the saved bytes using the baseline path and candidate path returned
   by `stage`. Cowork runs this bundled local script; the user does not use a
   terminal:

   ```text
   python3 "${CLAUDE_SKILL_DIR}/scripts/workbook_audit.py" audit \
     "<baseline.xlsx>" "<candidate.xlsx>"
   ```

5. Return the saved candidate and HTML receipt using Cowork's file-output
   mechanism, not just as path text. Report the exact outcome and findings.
   `FAIL` and `INCOMPLETE` are not successful verification; do not describe
   them as verified.

If the script runtime or workbook-folder permission is unavailable, stop and
report `INCOMPLETE`; do not ask the user to install Python or run a terminal
command as a workaround.

## Coverage and limitations

- Supported edit path: this plugin's staged sibling candidate, edited and saved
  using Cowork's authorized file tools. Script-driven edits are supported only
  when the script targets that candidate.
- Not intercepted: direct edits to the original, other application writes,
  background processes, or scripts that bypass the candidate. A later audit
  cannot reconstruct a missing pre-edit baseline.
- Supported workbook format: `.xlsx`. The local audit compares formula
  structure and cached formula errors. It does not execute or recalculate
  formulas, interpret `.attestory/rules.yaml`, or verify external workbooks.
  Such required checks produce `INCOMPLETE`.
- The baseline and receipt are local files in the authorized workbook folder.
  The HTML receipt includes SHA-256 hashes for the captured baseline and saved
  candidate; it is not a cryptographic signature.
