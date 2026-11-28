---
description: Edit an Excel workbook using an Attestory-managed working copy, then return the candidate and local audit receipt.
---

Follow the `attestory-cowork` skill. Ask for the workbook if none was specified.
Before changing anything, use its bundled script to capture the original and
create a separate candidate. Make the requested changes only to that candidate.
After the final save, audit the candidate against the captured baseline. Return
the candidate file and generated HTML receipt as Cowork file outputs, state the
receipt's exact PASS, FAIL, or INCOMPLETE outcome, and summarize the findings.
Never label an INCOMPLETE candidate as verified.
