# Attestory for Claude Cowork

Attestory for Cowork captures an original `.xlsx` workbook, creates a separate
working copy, and audits the saved candidate after Cowork edits it. Ask Claude
to prepare and audit a workbook in one request, or run
`/attestory-cowork:audit-workbook`.

The plugin reads and writes files only in the workbook folder you authorize.
Workbook bytes, baselines, candidates, and HTML receipts stay on that device;
the plugin does not upload them to Attestory. It does not intercept every
editor or script write. Use its managed working-copy flow for edits. This
initial version reports `INCOMPLETE` whenever a required check is outside its
standalone audit coverage; a receipt is not a signature or a guarantee that
Excel recalculated formulas. The bundled script uses Python's standard
library only and runs locally through Cowork; users do not install Python
packages or run terminal commands. If Cowork cannot provide its local script
runtime, the workflow reports `INCOMPLETE`.
