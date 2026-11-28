import hashlib
import json
import os
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import yaml

from attestory.parsers.csv_extract import (
    diff_csv_snapshots,
    extract_csv_to_parquet,
)
from attestory.parsers.excel import extract_xlsx_to_parquet, recalculate_xlsx
from attestory.parsers.html import diff_html_snapshots, extract_html_to_parquet
from attestory.parsers.pptx import diff_pptx_snapshots, extract_pptx_to_parquet
from attestory.rules import evaluate_rules

ATTESTORY_DIR = ".attestory"


@contextmanager
def _workspace_lock(meta_dir: Path) -> Iterator[None]:
    """Serialize local snapshot and history writes across processes."""
    meta_dir.mkdir(parents=True, exist_ok=True)
    lock_path = meta_dir / "commit.lock"
    with lock_path.open("a+b") as lock_file:
        try:
            if os.name == "nt":
                import msvcrt

                lock_file.seek(0, os.SEEK_END)
                if lock_file.tell() == 0:
                    lock_file.write(b"\0")
                    lock_file.flush()
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        except OSError as error:
            raise RuntimeError(
                f"Could not acquire Attestory workspace lock at {lock_path}: {error}"
            ) from error

        try:
            yield
        finally:
            if os.name == "nt":
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _atomic_write_text(path: Path, content: str) -> None:
    """Write and fsync a sibling temporary file before atomically replacing path."""
    temporary_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        mode = None
    descriptor = os.open(
        temporary_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary_file:
            if mode is not None:
                os.chmod(temporary_path, mode)
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _verify_parquet_snapshot(path: Path) -> None:
    """Read the staged Parquet data so truncated or invalid snapshots are rejected."""
    connection = duckdb.connect(":memory:")
    try:
        connection.execute(
            "SELECT COUNT(*) FROM read_parquet(?)", [str(path)]
        ).fetchone()
    except duckdb.Error as error:
        raise RuntimeError(
            f"Staged snapshot is not a complete Parquet file: {path}"
        ) from error
    finally:
        connection.close()


def sha256_file(path: Path) -> str:
    """Compute SHA-256 hash of a file without loading the entire file into RAM."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def artifact_key(root: Path, target_file: Path) -> str:
    """Return a normalized workspace-relative key for a tracked artifact."""
    workspace = root.expanduser().resolve()
    target = target_file.expanduser()
    if not target.is_absolute():
        target = workspace / target
    try:
        return target.resolve().relative_to(workspace).as_posix()
    except ValueError as error:
        raise ValueError(
            f"Artifact must be inside workspace {workspace}: {target}"
        ) from error


def commits_for_artifact(
    history: dict, key: str, *, include_legacy: bool = True
) -> list[dict]:
    """Find commits for an artifact, falling back to basename-only legacy history."""
    commits = history.get("commits", [])
    identified = [commit for commit in commits if commit.get("artifact_key") == key]
    if identified or not include_legacy or Path(key).name != key:
        return identified
    return [
        commit
        for commit in commits
        if not commit.get("artifact_key") and commit.get("file") == key
    ]


def init_workspace(root: Path) -> Path:
    """Initialize a local .attestory/ directory safely alongside concurrent commits."""
    root = root.expanduser().resolve()
    meta_dir = root / ATTESTORY_DIR
    with _workspace_lock(meta_dir):
        return _init_workspace_locked(root)


def _init_workspace_locked(root: Path) -> Path:
    """Initialize a workspace while its commit lock is held."""
    meta_dir = root / ATTESTORY_DIR
    (meta_dir / "snapshots").mkdir(parents=True, exist_ok=True)

    rules_path = meta_dir / "rules.yaml"
    if not rules_path.exists():
        _atomic_write_text(
            rules_path,
            yaml.safe_dump(
                {
                    "version": 1,
                    "guardrails": {
                        "flag_hardcoded_formula_overrides": True,
                        "flag_excel_errors": True,
                    },
                    "must_be_formula": [],
                    "assertions": [],
                    "cross_file_checks": [],
                    "business_rules": [
                        "Add recurring metric definitions or exclusions here for Claude/ChatGPT continuity."
                    ],
                },
                sort_keys=False,
            ),
        )

    manifest_path = meta_dir / "history.json"
    if not manifest_path.exists():
        _atomic_write_text(manifest_path, json.dumps({"commits": []}, indent=2))

    changelog_path = meta_dir / "changelog.md"
    if not changelog_path.exists():
        _atomic_write_text(changelog_path, "# Attestory Changelog\n\n")

    return meta_dir


def diff_snapshots(prev_pq: Path, curr_pq: Path) -> dict:
    """Use DuckDB FULL OUTER JOIN over Parquet snapshots to detect hardcode overrides, formula edits, and value changes."""
    con = duckdb.connect(":memory:")
    query = f"""
        SELECT
            COALESCE(p.sheet_name, c.sheet_name) AS sheet_name,
            COALESCE(p.cell_id, c.cell_id) AS cell_id,
            p.is_formula AS was_formula,
            c.is_formula AS is_formula,
            p.formula AS old_formula,
            c.formula AS new_formula,
            p.val_str AS old_val,
            c.val_str AS new_val
        FROM read_parquet('{prev_pq.as_posix()}') p
        FULL OUTER JOIN read_parquet('{curr_pq.as_posix()}') c
            ON p.sheet_name = c.sheet_name AND p.cell_id = c.cell_id
        WHERE p.norm_formula IS DISTINCT FROM c.norm_formula
           OR p.val_str IS DISTINCT FROM c.val_str
        ORDER BY sheet_name, cell_id
    """
    changes = con.execute(query).fetchall()
    con.close()

    hardcode_overrides = []
    formula_changes = []
    value_changes = []

    for sheet, cell, was_f, is_f, old_f, new_f, old_v, new_v in changes:
        loc = f"{sheet}!{cell}"
        if was_f and not is_f and new_v != "":
            hardcode_overrides.append(
                {
                    "location": loc,
                    "old_formula": old_f,
                    "old_val": old_v,
                    "new_hardcoded_val": new_v,
                }
            )
        elif old_f != new_f:
            formula_changes.append(
                {
                    "location": loc,
                    "old_formula": old_f,
                    "new_formula": new_f,
                    "old_val": old_v,
                    "new_val": new_v,
                }
            )
        else:
            value_changes.append({"location": loc, "old_val": old_v, "new_val": new_v})

    return {
        "total_changes": len(changes),
        "hardcode_overrides": hardcode_overrides,
        "formula_changes": formula_changes,
        "value_changes": value_changes,
    }


def diff_file_snapshots(prev_pq: Path, curr_pq: Path, file_name: str) -> dict:
    extension = Path(file_name).suffix.lower()
    if extension == ".pptx":
        return diff_pptx_snapshots(prev_pq, curr_pq)
    if extension == ".xlsx":
        return diff_snapshots(prev_pq, curr_pq)
    if extension == ".html":
        return diff_html_snapshots(prev_pq, curr_pq)
    if extension == ".csv":
        return diff_csv_snapshots(prev_pq, curr_pq)
    raise ValueError(f"Unsupported file type: {file_name}")


def commit_file(
    root: Path,
    target_file: Path,
    message: str = "",
    *,
    recalculate: bool = False,
    artifact_path: str | Path | None = None,
) -> dict:
    """Create and publish one snapshot while holding the workspace commit lock."""
    root = root.expanduser().resolve()
    with _workspace_lock(root / ATTESTORY_DIR):
        _init_workspace_locked(root)
        return _commit_file_locked(
            root,
            target_file,
            message,
            recalculate=recalculate,
            artifact_path=artifact_path,
        )


def _commit_file_locked(
    root: Path,
    target_file: Path,
    message: str = "",
    *,
    recalculate: bool = False,
    artifact_path: str | Path | None = None,
) -> dict:
    """Create a new Parquet snapshot for target_file, diff against prior version, and append to changelog.md."""
    root = root.expanduser().resolve()
    target_file = target_file.expanduser()
    if not target_file.is_absolute():
        target_file = root / target_file
    target_file = target_file.resolve()
    meta_dir = root / ATTESTORY_DIR

    target_key = (
        Path(str(artifact_path).replace("\\", "/")).as_posix()
        if artifact_path is not None
        else artifact_key(root, target_file)
    )
    if (
        Path(target_key).is_absolute()
        or ".." in Path(target_key).parts
        or target_key in {"", "."}
    ):
        raise ValueError(f"Invalid workspace-relative artifact path: {target_key}")
    artifact_path = Path(target_key)
    if target_file != root and not target_file.is_relative_to(root):
        raise ValueError(f"Artifact must be inside workspace {root}: {target_file}")

    extension = target_file.suffix.lower()
    if extension not in {".xlsx", ".pptx", ".html", ".csv"}:
        raise ValueError("Attestory supports .xlsx, .pptx, .html, and .csv files.")
    if recalculate and extension != ".xlsx":
        raise ValueError("--recalculate is only supported for .xlsx workbooks.")

    recalculation_warnings = recalculate_xlsx(target_file) if recalculate else []

    manifest_path = meta_dir / "history.json"
    history = json.loads(manifest_path.read_text(encoding="utf-8"))

    for commit in history["commits"]:
        if not commit.get("artifact_key"):
            commit.setdefault("identity_ambiguous", True)
            commit.setdefault(
                "identity_warning",
                "Legacy history stores only a basename; verify the artifact path.",
            )

    file_commits = commits_for_artifact(history, target_key, include_legacy=False)
    next_ver = len(file_commits) + 1
    identity_hash = hashlib.sha256(target_key.encode("utf-8")).hexdigest()
    stem = target_file.stem.replace(" ", "_")
    curr_pq = meta_dir / "snapshots" / f"{stem}_{identity_hash}_v{next_ver}.parquet"
    extractors = {
        ".xlsx": extract_xlsx_to_parquet,
        ".pptx": extract_pptx_to_parquet,
        ".html": extract_html_to_parquet,
        ".csv": extract_csv_to_parquet,
    }
    staged_pq = meta_dir / "snapshots" / f".attestory-stage-{uuid.uuid4().hex}.parquet"
    file_hash = sha256_file(target_file)
    try:
        stats = extractors[extension](target_file, staged_pq)
        if sha256_file(target_file) != file_hash:
            raise RuntimeError(
                f"Artifact changed while creating its snapshot: {target_key}. "
                "Retry the commit after the file stops changing."
            )
        snapshot_hash = sha256_file(staged_pq)
        _verify_parquet_snapshot(staged_pq)
        if sha256_file(staged_pq) != snapshot_hash:
            raise RuntimeError(
                f"Staged snapshot changed during verification: {staged_pq}"
            )
        with staged_pq.open("rb+") as snapshot_file:
            os.fsync(snapshot_file.fileno())
        os.replace(staged_pq, curr_pq)
        if sha256_file(curr_pq) != snapshot_hash:
            raise RuntimeError(f"Published snapshot digest mismatch: {curr_pq}")
    finally:
        staged_pq.unlink(missing_ok=True)

    if recalculation_warnings:
        warnings_by_location = {
            warning["location"]: warning
            for warning in stats.get("formula_cache_warnings", [])
        }
        warnings_by_location.update(
            {warning["location"]: warning for warning in recalculation_warnings}
        )
        stats["formula_cache_warnings"] = list(warnings_by_location.values())

    diff_report = None
    if file_commits:
        prev_pq = root / file_commits[-1]["snapshot"]
        diff_report = diff_file_snapshots(prev_pq, curr_pq, target_key)

    rule_results = evaluate_rules(
        root, target_file, curr_pq, history, artifact_key=target_key
    )
    rule_violations = rule_results["violations"]
    incomplete_findings = rule_results["incomplete_findings"]
    if extension == ".xlsx" and not file_commits:
        incomplete_findings.append(
            {
                "rule": "missing_baseline",
                "location": target_key,
                "reason": "No prior snapshot exists for this workbook; comparative checks cannot run.",
            }
        )
    for warning in stats.get("formula_cache_warnings", []):
        if warning["warning"] == "uncached":
            incomplete_findings.append(
                {
                    "rule": "formula_cache",
                    "location": warning["location"],
                    "reason": "The formula has no cached result, so checks that depend on its value cannot run.",
                }
            )
    failed_checks = bool(
        stats.get("errors")
        or rule_violations
        or (diff_report and diff_report.get("hardcode_overrides"))
    )
    audit_status = (
        "FAIL"
        if failed_checks
        else "INCOMPLETE"
        if incomplete_findings
        else "PASS"
    )
    record = {
        "version": next_ver,
        "file": target_key,
        "artifact_key": target_key,
        "sha256": file_hash,
        "snapshot_sha256": snapshot_hash,
        "timestamp": datetime.now(UTC).isoformat(),
        "message": message or f"Snapshot v{next_ver}",
        "snapshot": str(curr_pq.relative_to(root)),
        "stats": stats,
        "rule_violations": rule_violations,
        "incomplete_findings": incomplete_findings,
        "audit_status": audit_status,
        "diff_summary": {
            "total_changes": diff_report["total_changes"] if diff_report else 0,
            "hardcode_overrides": len(diff_report.get("hardcode_overrides", []))
            if diff_report
            else 0,
            "formula_changes": len(diff_report["formula_changes"])
            if diff_report and "formula_changes" in diff_report
            else 0,
        },
    }
    history["commits"].append(record)
    _atomic_write_text(manifest_path, json.dumps(history, indent=2))

    lines = [
        f"## `{target_key}` — v{next_ver} ({record['timestamp']})",
        f"- **SHA-256:** `{file_hash[:16]}...` | **Note:** {record['message']}",
        f"- **Audit status:** {audit_status}",
    ]
    for finding in incomplete_findings:
        lines.append(
            f"- **INCOMPLETE** ({finding['rule']}) at `{finding['location']}`: "
            f"{finding['reason']}"
        )
    if extension == ".xlsx":
        lines.append(
            f"- **Cells:** {stats['populated_cells']} | **Excel Errors:** {len(stats['errors'])}"
        )
        for warning in stats.get("formula_cache_warnings", []):
            lines.append(
                f"- ⚠️ **{warning['warning'].upper()} FORMULA CACHE** at "
                f"`{warning['location']}`: `{warning['formula']}`"
            )
    elif extension == ".pptx":
        lines.append(
            f"- **Slides:** {stats['slides']} | **Elements:** {stats['elements']}"
        )
    elif extension == ".html":
        lines.append(
            f"- **HTML elements:** {stats['elements']} | "
            f"**External references:** {stats['external_references']}"
        )
    else:
        lines.append(
            f"- **CSV rows:** {stats['rows']} | **Columns:** {stats['columns']}"
        )
    if diff_report:
        if extension == ".xlsx":
            lines.append(
                f"- **Diff vs v{next_ver - 1}:** {diff_report['total_changes']} changes "
                f"({len(diff_report['hardcode_overrides'])} hardcoded formula overrides, "
                f"{len(diff_report['formula_changes'])} formula edits)"
            )
            for ov in diff_report["hardcode_overrides"][:10]:
                lines.append(
                    f"  - ⚠️ **HARDCODE OVERRIDE** at `{ov['location']}`: `{ov['old_formula']}` -> `{ov['new_hardcoded_val']}`"
                )
        elif extension == ".pptx":
            lines.append(
                f"- **Diff vs v{next_ver - 1}:** {diff_report['total_changes']} presentation changes"
            )
        else:
            lines.append(
                f"- **Diff vs v{next_ver - 1}:** {diff_report['total_changes']} "
                f"{'HTML' if extension == '.html' else 'CSV'} changes"
            )
    if rule_violations:
        lines.append(f"- **Rule violations:** {len(rule_violations)}")
        for violation in rule_violations:
            lines.append(
                f"  - ⚠️ **RULE VIOLATION** ({violation['rule']}) at "
                f"`{violation['location']}`: "
                f"{violation.get('description') or violation['expected']} "
                f"(expected: {violation.get('expected')}, actual: {violation.get('actual')})"
            )
    lines.append("\n")

    changelog_path = meta_dir / "changelog.md"
    try:
        changelog = changelog_path.read_text(encoding="utf-8")
        _atomic_write_text(changelog_path, changelog + "\n".join(lines))
    except OSError as error:
        raise RuntimeError(
            f"Snapshot {target_key} v{next_ver} was committed, but its changelog "
            f"could not be updated: {error}"
        ) from error

    return {"record": record, "diff": diff_report}


def snapshot_before(root: Path, target_file: Path, message: str = "") -> dict:
    """Capture a file before editing, avoiding duplicate snapshots of unchanged content."""
    root = root.expanduser().resolve()
    target_file = target_file.expanduser()
    if not target_file.is_absolute():
        target_file = root / target_file
    target_file = target_file.resolve()
    if not target_file.is_file():
        raise FileNotFoundError(f"Baseline target does not exist: {target_file}")

    target_key = artifact_key(root, target_file)
    if target_file.suffix.lower() not in {".xlsx", ".pptx", ".html", ".csv"}:
        raise ValueError("Attestory supports .xlsx, .pptx, .html, and .csv files.")

    with _workspace_lock(root / ATTESTORY_DIR):
        _init_workspace_locked(root)
        manifest_path = root / ATTESTORY_DIR / "history.json"
        history = json.loads(manifest_path.read_text(encoding="utf-8"))
        commits = commits_for_artifact(history, target_key, include_legacy=False)
        if commits and commits[-1].get("sha256") == sha256_file(target_file):
            return {"captured": False, "record": commits[-1], "diff": None}

        result = _commit_file_locked(
            root, target_file, message=message or "Pre-edit baseline"
        )
        return {"captured": True, **result}


def workspace_status(root: Path) -> list[dict]:
    """Return a latest-commit overview for each file tracked in this workspace."""
    manifest_path = root / ATTESTORY_DIR / "history.json"
    if not manifest_path.exists():
        return []

    history = json.loads(manifest_path.read_text(encoding="utf-8"))
    latest_by_file = {}
    for commit in history.get("commits", []):
        identity = commit.get("artifact_key") or f"legacy:{commit['file']}"
        latest_by_file[identity] = commit

    status = []
    for _, commit in sorted(latest_by_file.items()):
        filename = commit.get("artifact_key") or commit["file"]
        stats = commit.get("stats", {})
        violations = commit.get("rule_violations", [])
        diff_summary = commit.get("diff_summary", {})
        errors_and_violations = len(stats.get("errors", [])) + len(violations)
        audit_status = commit.get("audit_status")
        if audit_status is None:
            audit_status = (
                "FAIL"
                if errors_and_violations or diff_summary.get("hardcode_overrides")
                else "INCOMPLETE"
                if (
                    filename.lower().endswith(".xlsx") and commit["version"] == 1
                )
                or commit.get("identity_ambiguous")
                or not commit.get("artifact_key")
                else "PASS"
            )
        updated = datetime.fromisoformat(commit["timestamp"]).astimezone(UTC)
        status.append(
            {
                "file": filename,
                "version": commit["version"],
                "timestamp": updated.strftime("%Y-%m-%d %H:%M:%S"),
                "sha256": commit["sha256"],
                "open_issues": errors_and_violations,
                "status": audit_status,
            }
        )
    return status
