import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import yaml

from attestory.parsers.excel import extract_xlsx_to_parquet
from attestory.parsers.pptx import diff_pptx_snapshots, extract_pptx_to_parquet
from attestory.rules import evaluate_rules

ATTESTORY_DIR = ".attestory"


def sha256_file(path: Path) -> str:
    """Compute SHA-256 hash of a file without loading the entire file into RAM."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def init_workspace(root: Path) -> Path:
    """Initialize a local .attestory/ directory with rules.yaml, history.json, and changelog.md."""
    meta_dir = root / ATTESTORY_DIR
    (meta_dir / "snapshots").mkdir(parents=True, exist_ok=True)

    rules_path = meta_dir / "rules.yaml"
    if not rules_path.exists():
        rules_path.write_text(
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
            encoding="utf-8",
        )

    manifest_path = meta_dir / "history.json"
    if not manifest_path.exists():
        manifest_path.write_text(
            json.dumps({"commits": []}, indent=2), encoding="utf-8"
        )

    changelog_path = meta_dir / "changelog.md"
    if not changelog_path.exists():
        changelog_path.write_text("# Attestory Changelog\n\n", encoding="utf-8")

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
    if Path(file_name).suffix.lower() == ".pptx":
        return diff_pptx_snapshots(prev_pq, curr_pq)
    if Path(file_name).suffix.lower() == ".xlsx":
        return diff_snapshots(prev_pq, curr_pq)
    raise ValueError(f"Unsupported file type: {file_name}")


def commit_file(root: Path, target_file: Path, message: str = "") -> dict:
    """Create a new Parquet snapshot for target_file, diff against prior version, and append to changelog.md."""
    meta_dir = root / ATTESTORY_DIR
    if not meta_dir.exists():
        init_workspace(root)

    extension = target_file.suffix.lower()
    if extension not in {".xlsx", ".pptx"}:
        raise ValueError("Attestory supports .xlsx and .pptx files.")

    manifest_path = meta_dir / "history.json"
    history = json.loads(manifest_path.read_text(encoding="utf-8"))

    file_commits = [c for c in history["commits"] if c["file"] == target_file.name]
    next_ver = len(file_commits) + 1
    stem = target_file.stem.replace(" ", "_")

    curr_pq = meta_dir / "snapshots" / f"{stem}_v{next_ver}.parquet"
    stats = (
        extract_xlsx_to_parquet(target_file, curr_pq)
        if extension == ".xlsx"
        else extract_pptx_to_parquet(target_file, curr_pq)
    )
    file_hash = sha256_file(target_file)

    diff_report = None
    if file_commits:
        prev_pq = root / file_commits[-1]["snapshot"]
        diff_report = diff_file_snapshots(prev_pq, curr_pq, target_file.name)

    rule_violations = evaluate_rules(root, target_file, curr_pq, history)
    record = {
        "version": next_ver,
        "file": target_file.name,
        "sha256": file_hash,
        "timestamp": datetime.now(UTC).isoformat(),
        "message": message or f"Snapshot v{next_ver}",
        "snapshot": str(curr_pq.relative_to(root)),
        "stats": stats,
        "rule_violations": rule_violations,
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
    manifest_path.write_text(json.dumps(history, indent=2), encoding="utf-8")

    lines = [
        f"## `{target_file.name}` — v{next_ver} ({record['timestamp']})",
        f"- **SHA-256:** `{file_hash[:16]}...` | **Note:** {record['message']}",
    ]
    if extension == ".xlsx":
        lines.append(
            f"- **Cells:** {stats['populated_cells']} | **Excel Errors:** {len(stats['errors'])}"
        )
    else:
        lines.append(
            f"- **Slides:** {stats['slides']} | **Elements:** {stats['elements']}"
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
        else:
            lines.append(
                f"- **Diff vs v{next_ver - 1}:** {diff_report['total_changes']} presentation changes"
            )
    if rule_violations:
        lines.append(f"- **Rule violations:** {len(rule_violations)}")
        for violation in rule_violations:
            lines.append(
                f"  - ⚠️ **RULE VIOLATION** ({violation['rule']}) at "
                f"`{violation['location']}`: {violation.get('description') or violation['expected']}"
            )
    lines.append("\n")

    with open(meta_dir / "changelog.md", "a", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return {"record": record, "diff": diff_report}
