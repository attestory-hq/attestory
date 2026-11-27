import json
import shutil
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request

from attestory import __version__
from attestory.cli import generate_report
from attestory.engine import ATTESTORY_DIR, commit_file, diff_file_snapshots

SUPPORTED_EXTENSIONS = {".xlsx", ".pptx", ".html", ".csv"}


def create_app(root: Path | None = None) -> FastAPI:
    """Create the local HTTP bridge scoped to a single workspace."""
    workspace = (root or Path.cwd()).resolve()
    app = FastAPI(title="Attestory Local Audit API", version=__version__)

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

    @app.post("/v1/commit")
    async def commit(request: Request) -> dict:
        content_type = request.headers.get("content-type", "")
        staged_dir: Path | None = None
        target_file: Path
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

            result = commit_file(workspace, target_file, message=message)
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
            "rule_violations": record["rule_violations"],
        }

    @app.get("/v1/diff/{filename}")
    async def diff(filename: str) -> dict:
        history = load_history()
        commits = [item for item in history["commits"] if item["file"] == filename]
        if len(commits) < 2:
            raise HTTPException(
                status_code=404,
                detail=f"At least two versions of {filename} are required.",
            )
        report = diff_file_snapshots(
            workspace / commits[-2]["snapshot"],
            workspace / commits[-1]["snapshot"],
            filename,
        )
        return {
            "file": filename,
            "from_version": commits[-2]["version"],
            "to_version": commits[-1]["version"],
            **report,
        }

    @app.post("/v1/report/{filename}")
    async def report(filename: str) -> dict:
        try:
            return generate_report(workspace, filename)
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    return app
