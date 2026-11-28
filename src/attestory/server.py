import asyncio
import hashlib
import html
import json
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response

from attestory import ExcelEditResult, __version__, verify_excel_edit
from attestory.cli import generate_report
from attestory.engine import (
    ATTESTORY_DIR,
    artifact_key,
    commit_file,
    commits_for_artifact,
    diff_file_snapshots,
)

SUPPORTED_EXTENSIONS = {".xlsx", ".pptx", ".html", ".csv"}
MAX_EXCEL_ACTION_UPLOAD_BYTES = 25 * 1024 * 1024
EXCEL_ACTION_RESULT_TTL_SECONDS = 15 * 60


def create_app(root: Path | None = None) -> FastAPI:
    """Create the local HTTP bridge scoped to a single workspace."""
    workspace = (root or Path.cwd()).resolve()
    app = FastAPI(title="Attestory Local Audit API", version=__version__)
    excel_action_results: dict[str, dict[str, Any]] = {}

    def resolve_workspace_file(file_path: str) -> Path:
        candidate = Path(file_path).expanduser()
        if not candidate.is_absolute():
            candidate = workspace / candidate
        resolved = candidate.resolve()
        if not resolved.is_relative_to(workspace):
            raise HTTPException(
                status_code=400, detail="file_path must be inside the workspace."
            )
        if resolved.suffix.lower() not in SUPPORTED_EXTENSIONS:
            raise HTTPException(
                status_code=400,
                detail="Supported formats are .xlsx, .pptx, .html, and .csv.",
            )
        if not resolved.is_file():
            raise HTTPException(status_code=404, detail="File not found.")
        return resolved

    def load_history() -> dict:
        path = workspace / ATTESTORY_DIR / "history.json"
        if not path.exists():
            raise HTTPException(status_code=404, detail="No Attestory history found.")
        return json.loads(path.read_text(encoding="utf-8"))

    def resolve_artifact_key(filename: str) -> str:
        try:
            return artifact_key(workspace, Path(filename))
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    def get_excel_action_result(audit_id: str) -> dict[str, Any]:
        result = excel_action_results.get(audit_id)
        if result is None or result["expires_at"] <= time.monotonic():
            excel_action_results.pop(audit_id, None)
            raise HTTPException(status_code=404, detail="Audit download has expired.")
        return result

    async def expire_excel_action_result(audit_id: str) -> None:
        await asyncio.sleep(EXCEL_ACTION_RESULT_TTL_SECONDS)
        excel_action_results.pop(audit_id, None)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "version": __version__}

    @app.get("/v1/context")
    async def context() -> dict:
        meta_dir = workspace / ATTESTORY_DIR
        rules_path = meta_dir / "rules.yaml"
        changelog_path = meta_dir / "changelog.md"
        if not rules_path.exists():
            raise HTTPException(status_code=404, detail="No Attestory rules found.")
        changelog = (
            changelog_path.read_text(encoding="utf-8")
            if changelog_path.exists()
            else ""
        )
        entries = changelog.split("\n## ")
        recent_entries = entries[-20:]
        if recent_entries and not recent_entries[0].startswith("#"):
            recent_entries[0] = "## " + recent_entries[0]
        return {
            "rules_yaml": rules_path.read_text(encoding="utf-8"),
            "recent_changelog": "\n".join(recent_entries)[-20000:],
        }

    @app.post("/v1/excel/edit-verify")
    async def edit_verify_excel(
        request: Request,
        original_file: UploadFile | None = File(None),
        candidate_file: UploadFile = File(...),
        confirm_backend_processing: bool = Form(...),
    ) -> dict[str, Any]:
        if not confirm_backend_processing:
            raise HTTPException(
                status_code=400,
                detail="Confirm backend processing before uploading workbooks.",
            )

        candidate_name = Path(candidate_file.filename or "").name
        if (
            (
                original_file is not None
                and Path(original_file.filename or "").suffix.lower() != ".xlsx"
            )
            or Path(candidate_name).suffix.lower() != ".xlsx"
        ):
            raise HTTPException(
                status_code=400,
                detail="Provided files must be XLSX files.",
            )

        original_bytes = (
            await original_file.read(MAX_EXCEL_ACTION_UPLOAD_BYTES + 1)
            if original_file is not None
            else None
        )
        candidate_bytes = await candidate_file.read(MAX_EXCEL_ACTION_UPLOAD_BYTES + 1)
        if (
            (
                original_bytes is not None
                and len(original_bytes) > MAX_EXCEL_ACTION_UPLOAD_BYTES
            )
            or len(candidate_bytes) > MAX_EXCEL_ACTION_UPLOAD_BYTES
        ):
            raise HTTPException(
                status_code=413,
                detail="Each XLSX upload must be 25 MiB or smaller.",
            )

        try:
            result = verify_excel_edit(
                original_bytes,
                candidate_bytes,
                filename=candidate_name,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

        audit_id = uuid.uuid4().hex
        candidate_sha256 = hashlib.sha256(candidate_bytes).hexdigest()
        receipt_html = result.receipt_html or _incomplete_excel_receipt(result)
        receipt_bytes = receipt_html.encode("utf-8")
        receipt_sha256 = hashlib.sha256(receipt_bytes).hexdigest()
        expires_at = time.monotonic() + EXCEL_ACTION_RESULT_TTL_SECONDS
        excel_action_results[audit_id] = {
            "candidate": candidate_bytes,
            "candidate_sha256": candidate_sha256,
            "receipt": receipt_bytes,
            "receipt_sha256": receipt_sha256,
            "expires_at": expires_at,
        }
        asyncio.create_task(expire_excel_action_result(audit_id))

        base_url = str(request.base_url).rstrip("/")
        return {
            "audit_id": audit_id,
            "filename": candidate_name,
            "status": result.status,
            "verified": result.verified,
            "original_sha256": result.original_sha256,
            "candidate_sha256": candidate_sha256,
            "baseline_sha256": result.baseline_sha256,
            "findings": result.findings,
            "cell_findings": result.cell_findings,
            "incomplete_findings": result.outcome.get("incomplete_findings", []),
            "required_checks": result.record.get("required_checks", [])
            if result.record
            else [],
            "candidate_download_url": (
                f"{base_url}/v1/excel/edit-verify/{audit_id}/candidate"
            ),
            "receipt_download_url": (
                f"{base_url}/v1/excel/edit-verify/{audit_id}/receipt"
            ),
            "downloads_expire_in_seconds": EXCEL_ACTION_RESULT_TTL_SECONDS,
        }

    @app.get("/v1/excel/edit-verify/{audit_id}/candidate")
    async def download_excel_candidate(audit_id: str) -> Response:
        result = get_excel_action_result(audit_id)
        return Response(
            content=result["candidate"],
            media_type=(
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            ),
            headers={
                "Content-Disposition": 'attachment; filename="candidate.xlsx"',
                "Cache-Control": "no-store",
                "X-Attestory-SHA256": result["candidate_sha256"],
            },
        )

    @app.get("/v1/excel/edit-verify/{audit_id}/receipt")
    async def download_excel_receipt(audit_id: str) -> Response:
        result = get_excel_action_result(audit_id)
        return Response(
            content=result["receipt"],
            media_type="text/html; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="audit-receipt.html"',
                "Cache-Control": "no-store",
                "X-Attestory-SHA256": result["receipt_sha256"],
            },
        )

    @app.post("/v1/commit")
    async def commit(request: Request) -> dict:
        content_type = request.headers.get("content-type", "")
        staged_dir: Path | None = None
        target_file: Path
        artifact_path: str | None = None
        message = ""
        try:
            if content_type.startswith("multipart/form-data"):
                form = await request.form()
                message = str(form.get("message", ""))
                upload = form.get("file") or form.get("upload")
                file_path = form.get("file_path")
                if upload is not None:
                    filename = Path(str(getattr(upload, "filename", ""))).name
                    if (
                        not filename
                        or Path(filename).suffix.lower() not in SUPPORTED_EXTENSIONS
                    ):
                        raise HTTPException(
                            status_code=400,
                            detail="Upload must use a supported file format.",
                        )
                    staged_dir = (
                        workspace / ATTESTORY_DIR / ".incoming" / uuid.uuid4().hex
                    )
                    staged_dir.mkdir(parents=True, exist_ok=False)
                    target_file = staged_dir / filename
                    artifact_path = filename
                    target_file.write_bytes(await upload.read())
                elif file_path:
                    target_file = resolve_workspace_file(str(file_path))
                else:
                    raise HTTPException(
                        status_code=400,
                        detail="Provide file_path or upload a file.",
                    )
            else:
                body = await request.json()
                if not isinstance(body, dict):
                    raise HTTPException(
                        status_code=400, detail="JSON request body must be an object."
                    )
                file_path = body.get("file_path")
                if not file_path:
                    raise HTTPException(
                        status_code=400, detail="file_path is required."
                    )
                message = str(body.get("message", ""))
                target_file = resolve_workspace_file(str(file_path))

            result = commit_file(
                workspace,
                target_file,
                message=message,
                artifact_path=artifact_path,
            )
        except HTTPException:
            raise
        except (ValueError, json.JSONDecodeError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        finally:
            if staged_dir is not None:
                shutil.rmtree(staged_dir)

        record = result["record"]
        diff = result["diff"] or {}
        return {
            "file": record["file"],
            "version": record["version"],
            "sha256": record["sha256"],
            "diff_summary": record["diff_summary"],
            "hardcode_overrides": diff.get("hardcode_overrides", []),
            "formula_changes": diff.get("formula_changes", []),
            "formula_to_blanks": diff.get("formula_to_blanks", []),
            "missing_formula_cells": diff.get("missing_formula_cells", []),
            "rule_violations": record["rule_violations"],
        }

    @app.get("/v1/diff/{filename:path}")
    async def diff(filename: str) -> dict:
        history = load_history()
        key = resolve_artifact_key(filename)
        commits = commits_for_artifact(history, key)
        if len(commits) < 2:
            raise HTTPException(
                status_code=404,
                detail=f"At least two versions of {key} are required.",
            )
        report = diff_file_snapshots(
            workspace / commits[-2]["snapshot"],
            workspace / commits[-1]["snapshot"],
            key,
        )
        return {
            "file": key,
            "from_version": commits[-2]["version"],
            "to_version": commits[-1]["version"],
            **report,
        }

    @app.post("/v1/report/{filename:path}")
    async def report(
        filename: str,
        version: int | None = None,
        audit_id: str | None = None,
    ) -> dict:
        try:
            return generate_report(
                workspace,
                resolve_artifact_key(filename),
                version=version,
                audit_id=audit_id,
            )
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    return app


def _incomplete_excel_receipt(result: ExcelEditResult) -> str:
    payload = html.escape(
        json.dumps(
            {
                "status": result.status,
                "original_sha256": result.original_sha256,
                "candidate_sha256": result.candidate_sha256,
                "findings": result.findings,
                "incomplete_findings": result.outcome.get(
                    "incomplete_findings", []
                ),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return (
        "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\">"
        "<title>Attestory incomplete audit receipt</title>"
        "<h1>INCOMPLETE - verification not completed</h1>"
        "<p>This integration receipt records unavailable audit evidence. "
        "It is not a signed workspace report.</p>"
        f"<pre>{payload}</pre></html>"
    )
