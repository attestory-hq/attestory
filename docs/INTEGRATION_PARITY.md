# Cowork and ChatGPT integration parity

The automated parity tests exercise Attestory's local ChatGPT Action endpoint
and the bundled Cowork audit script with the same generated XLSX bytes. They
check formula-override `FAIL` results, clean-edit `PASS` results, missing
baseline `INCOMPLETE` results, and candidate/receipt hashes:
[`tests/test_integration_parity.py`](../tests/test_integration_parity.py).

These are adapter-level tests, not live product UI tests. Claude Desktop and
ChatGPT UI access was unavailable for this run, so no claim is made that either
product completes setup, editing, and file handoff with zero follow-up prompts.
Keep parity unadvertised until the live smoke tests below have been run.

## Live smoke-test record

Fill in this record from fresh installs before claiming product parity. Record
the exact client version and permission prompts; do not infer them from the
automated tests.

| Product | Client/version and date | Permissions granted | Tested edit paths | FAIL/PASS/INCOMPLETE and output handoff |
|---|---|---|---|---|
| Claude Cowork | Pending desktop smoke test | Pending | Pending | Pending |
| ChatGPT GPT Action | Pending configured-GPT smoke test | Pending | Pending | Pending |

## Test procedure

Use the same workbook fixture in both products for each scenario. Configure the
ChatGPT Action with a secured HTTPS endpoint and disclose backend processing
and retention before the user consents. In Cowork, install the plugin and
authorize only the fixture's folder.

1. **Formula override:** Start with `Revenue!B4 = SUM(B2:B3)`. Ask the
   assistant to replace the formula with the same displayed number. Expect
   `FAIL` at `Revenue!B4`, and verify the returned candidate hash matches its
   receipt.
2. **Passing edit:** Use a clean fixture and request a harmless value edit.
   Expect `PASS`, the candidate file, and a receipt bound to that candidate.
3. **Missing baseline:** In Cowork, remove the staged baseline before audit.
   In ChatGPT, submit only the candidate to the Action. Expect `INCOMPLETE`,
   not a successful verification, and confirm the candidate and diagnostic
   receipt are still returned.
4. Record whether each scenario completed in one natural-language request
   with no terminal use, manual snapshots, or follow-up prompts. Record any
   unsupported edit path or permission prompt rather than counting it as a
   pass.

The ChatGPT Action requires the explicit processing-consent field. Its
original workbook upload is optional only for the missing-baseline scenario;
without it the endpoint returns `INCOMPLETE`.
