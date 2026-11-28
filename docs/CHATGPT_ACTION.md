# ChatGPT Excel edit-and-verify prototype

The ChatGPT Action accepts an original `.xlsx` and an edited candidate
`.xlsx`, audits the exact candidate bytes against the original, and returns
cell-level findings with short-lived download links for the candidate and
HTML receipt. The action audits; ChatGPT is responsible for making the edit.
The automatic transfer of ChatGPT attachments into Action file parameters
depends on ChatGPT's current file-handling support and must be validated in
the configured GPT.

## Backend and retention disclosure

Show this disclosure before requesting consent or calling the Action:

> To audit this edit, ChatGPT will send both the original workbook and edited
> candidate to the configured Attestory backend. Uploads may be temporarily
> spooled by the server runtime while they are received. Attestory's audit
> engine uses a temporary workspace that is removed after the audit; the
> original is not added to the configured workspace. The exact candidate and
> receipt are held in backend process memory for up to 15 minutes so they can
> be downloaded, then the download links expire. ChatGPT's own handling and
> retention are governed by your ChatGPT settings. Continue only if you
> consent to this processing.

The Action requires `confirm_backend_processing=true`; set it only after the
user explicitly consents. Each file is limited to 25 MiB. The response is
`PASS`, `FAIL`, or `INCOMPLETE`; only `PASS` has `verified=true`. Always report
the cell-level findings and explain any `incomplete_findings`. The generated
fallback receipt for an unavailable audit is explicitly labeled
`INCOMPLETE` and is not a signed workspace report.
The original upload may be omitted only to exercise the missing-baseline case;
the candidate-only request returns `INCOMPLETE`.

## Configure a GPT Action

1. Install the optional server dependencies and start the bridge:

   ```sh
   python -m pip install "attestory[server]"
   attestory serve --host 127.0.0.1 --port 8765
   ```

2. Keep the bridge local for local tests. A remotely hosted ChatGPT Action
   cannot reach `127.0.0.1`; connect it only through an independently secured
   HTTPS adapter with authentication and TLS. The bridge itself does not
   provide remote authentication. Do not expose it directly to an untrusted
   network.
3. Configure the GPT Action using
   [`skills/chatgpt-action/openapi.yaml`](../skills/chatgpt-action/openapi.yaml)
   and replace its localhost server URL with the secured HTTPS base URL.
4. Put the backend disclosure above in the GPT's visible description or
   instructions. Tell it to disclose before sending files, ask for consent if
   it is not already explicit, preserve the original, audit both files, and
   never claim `INCOMPLETE` is verified.

## One-prompt request

Attach the original workbook and ask, for example:

> Update the assumptions on the Inputs sheet as follows: [describe the edit].
> Keep existing formulas intact. Then audit the original against your edited
> candidate with Attestory, show the cell-level findings, and return the
> audited candidate and receipt. I consent to sending both files to the
> configured Attestory backend under its disclosed processing and retention
> terms.

If the GPT can pass both exact XLSX files to the Action, it calls
`editAndVerifyExcel`, then returns the candidate and receipt download links in
the same conversation. It must identify the outcome and the candidate hash.

## Two-file fallback

If the GPT cannot pass its generated candidate directly to an Action:

1. Have ChatGPT produce the edited workbook as a downloadable `.xlsx`.
2. In the same conversation, attach both the unchanged original and that
   candidate.
3. Ask the GPT to compare those exact two files using Attestory and provide
   the processing consent if it has not already been given.
4. The action response includes cell-level findings and download links for
   the exact candidate bytes audited and the receipt.

This two-file route is the supported prototype while automatic candidate
handoff is being validated. Do not substitute a re-saved or further edited
candidate after the audit; its hash would no longer match the receipt.
