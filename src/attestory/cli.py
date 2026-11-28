import hashlib
import json
import sys
from enum import Enum
from html import escape
from importlib.resources import files
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from attestory.api import AuditResult, verify
from attestory.engine import (
    ATTESTORY_DIR,
    artifact_key,
    commit_file,
    commits_for_artifact,
    diff_file_snapshots,
    init_workspace,
    workspace_status,
)
from attestory.hooks import register_post_tool_use_hook

app = typer.Typer(
    help="Attestory — Local version control & deterministic math auditing for analytics."
)
console = Console()


def machine_output(
    *,
    hardcoded_regressions: list[dict] | None = None,
    formula_errors: list[dict] | None = None,
    rule_violations: list[dict] | None = None,
    summary_stats: dict | None = None,
) -> dict:
    """Build the stable JSON shape shared by machine-readable commands."""
    return AuditResult.from_findings(
        hardcoded_regressions=hardcoded_regressions,
        formula_errors=formula_errors,
        rule_violations=rule_violations,
        summary_stats=summary_stats,
    ).to_dict()


def print_machine_output(output: dict) -> None:
    typer.echo(json.dumps(output, sort_keys=True, separators=(",", ":")))


def formula_cache_warnings(stats: dict) -> list[dict]:
    return stats.get("formula_cache_warnings", [])


class SkillTarget(str, Enum):
    COWORK = "cowork"
    CLAUDE_CODE = "claude-code"
    ALL = "all"


@app.command()
def init():
    """Initialize a local .attestory/ audit folder in the current directory."""
    meta_dir = init_workspace(Path.cwd())
    console.print(
        Panel.fit(
            f"[bold green]Initialized Attestory workspace[/bold green] at [cyan]{meta_dir}[/cyan]\n"
            "Created [bold]rules.yaml[/bold], [bold]changelog.md[/bold], and [bold]snapshots/[/bold].",
            title="Attestory",
        )
    )


@app.command()
def install_skill(
    target: SkillTarget = typer.Option(
        SkillTarget.COWORK,
        "--target",
        "-t",
        help="Install for Cowork, Claude Code, or both.",
    ),
    force: bool = typer.Option(
        False, "--force", "-f", help="Overwrite an existing skill file."
    ),
    hook: bool = typer.Option(
        False,
        "--hook",
        "--register-hook",
        help="Register the Claude Code PostToolUse audit hook.",
    ),
):
    """Install the Attestory skill into the current workspace."""
    root = Path.cwd()
    if hook and target == SkillTarget.COWORK:
        raise typer.BadParameter(
            "--hook requires --target claude-code or --target all."
        )

    destinations = {
        SkillTarget.COWORK: [root / "SKILL.md"],
        SkillTarget.CLAUDE_CODE: [
            root / ".claude" / "skills" / "attestory" / "SKILL.md"
        ],
        SkillTarget.ALL: [
            root / "SKILL.md",
            root / ".claude" / "skills" / "attestory" / "SKILL.md",
        ],
    }[target]

    skill_already_exists = not force and any(
        destination.exists() for destination in destinations
    )
    installed_paths = []
    if skill_already_exists:
        console.print(
            "[yellow]SKILL.md already exists. Use --force to overwrite.[/yellow]"
        )
    else:
        skill_text = (
            files("attestory.templates")
            .joinpath("SKILL.md")
            .read_text(encoding="utf-8")
        )
        for destination in destinations:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(skill_text, encoding="utf-8")
            installed_paths.append(str(destination.relative_to(root)))

    hook_installed = False
    if hook:
        hook_installed = register_post_tool_use_hook(root)

    if installed_paths or hook:
        message = "[bold green]Attestory skill installed[/bold green]"
        if installed_paths:
            message += "\n" + "\n".join(
                f"[cyan]{path}[/cyan]" for path in installed_paths
            )
        if hook:
            message += (
                "\n[bold green]Claude Code PostToolUse hook "
                + ("registered" if hook_installed else "already registered")
                + "[/bold green]"
            )
        message += "\n\nVerify with: [bold]attestory commit <file.xlsx>[/bold]"
        console.print(Panel.fit(message, title="Attestory"))


@app.command()
def status(
    json_output: bool = typer.Option(
        False, "--json", help="Output strict machine-readable JSON."
    ),
):
    """Show the latest audit status for every tracked file."""
    entries = workspace_status(Path.cwd())
    if json_output:
        root = Path.cwd()
        manifest = root / ATTESTORY_DIR / "history.json"
        commits = (
            json.loads(manifest.read_text(encoding="utf-8"))["commits"]
            if manifest.exists()
            else []
        )
        latest_by_file = {}
        previous_by_file = {}
        for commit in commits:
            identity = commit.get("artifact_key") or f"legacy:{commit['file']}"
            if identity in latest_by_file:
                previous_by_file[identity] = latest_by_file[identity]
            latest_by_file[identity] = commit
        formula_errors = []
        violations = []
        hardcodes = []
        identity_warnings = []
        for identity, commit in latest_by_file.items():
            formula_errors.extend(commit.get("stats", {}).get("errors", []))
            violations.extend(commit.get("rule_violations", []))
            if not commit.get("artifact_key"):
                identity_warnings.append(
                    {
                        "rule": "ambiguous_legacy_identity",
                        "location": commit["file"],
                        "expected": "workspace-relative artifact path",
                        "actual": "basename-only legacy history",
                    }
                )
            previous = previous_by_file.get(identity)
            if previous:
                diff = diff_file_snapshots(
                    root / previous["snapshot"],
                    root / commit["snapshot"],
                    commit["file"],
                )
                hardcodes.extend(diff.get("hardcode_overrides", []))
        violations.extend(identity_warnings)
        output = machine_output(
            hardcoded_regressions=hardcodes,
            formula_errors=formula_errors,
            rule_violations=violations,
            summary_stats={
                "tracked_files": len(latest_by_file),
                "open_issues": sum(
                    len(commit.get("rule_violations", []))
                    + len(commit.get("stats", {}).get("errors", []))
                    for commit in latest_by_file.values()
                )
                + len(identity_warnings),
            },
        )
        print_machine_output(output)
        if not output["passed"]:
            raise typer.Exit(1)
        return
    if not entries:
        console.print(
            "[yellow]No tracked files yet. Run 'attestory demo' or "
            "'attestory commit <file>'[/yellow]"
        )
        return

    table = Table(title="Attestory Workspace Status")
    table.add_column("File")
    table.add_column("Latest Version")
    table.add_column("Last Updated (UTC)")
    table.add_column("SHA-256 Prefix")
    table.add_column("Open Errors / Rule Violations", justify="right")
    table.add_column("Status")
    for entry in entries:
        status_style = "green" if entry["status"] == "VERIFIED" else "yellow"
        table.add_row(
            entry["file"],
            f"v{entry['version']}",
            entry["timestamp"],
            entry["sha256"][:12],
            str(entry["open_issues"]),
            f"[{status_style}]{entry['status']}[/{status_style}]",
        )
    Console(width=140).print(table)


@app.command("hook-post-tool-use")
def hook_post_tool_use():
    """Audit supported files reported by a Claude Code PostToolUse event."""
    event = json.load(sys.stdin)
    if not isinstance(event, dict):
        raise typer.BadParameter("Hook input must be a JSON object.")
    tool_input = event.get("tool_input", {})
    if not isinstance(tool_input, dict):
        raise typer.BadParameter("Hook input 'tool_input' must be an object.")
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        return
    target = Path(file_path)
    if target.suffix.lower() not in {".xlsx", ".pptx", ".csv"}:
        return

    root_value = event.get("cwd", str(Path.cwd()))
    if not isinstance(root_value, str) or not root_value:
        raise typer.BadParameter("Hook input 'cwd' must be a non-empty path.")
    root = Path(root_value).expanduser().resolve()
    if not target.is_absolute():
        target = root / target

    result = verify(target, message="Claude Code PostToolUse hook", root=root)
    print_machine_output(result.to_dict())
    if not result.passed:
        raise typer.Exit(1)


@app.command()
def commit(
    file: Path = typer.Argument(
        ..., exists=True, help="Path to an .xlsx, .pptx, .html, or .csv file to audit."
    ),
    message: str = typer.Option(
        "", "-m", "--message", help="Optional note describing this update."
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Output strict machine-readable JSON."
    ),
    recalculate: bool = typer.Option(
        False,
        "--recalculate",
        help="Recalculate an .xlsx workbook with LibreOffice before auditing.",
    ),
):
    """Audit and snapshot a supported local file."""
    try:
        result = commit_file(
            Path.cwd(), file.resolve(), message=message, recalculate=recalculate
        )
    except RuntimeError as error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(1) from error
    rec = result["record"]
    diff = result["diff"]

    if json_output:
        hardcodes = diff.get("hardcode_overrides", []) if diff else []
        formula_errors = (
            rec["stats"].get("errors", []) if file.suffix.lower() == ".xlsx" else []
        )
        violations = rec["rule_violations"]
        output = machine_output(
            hardcoded_regressions=hardcodes,
            formula_errors=formula_errors,
            rule_violations=violations,
            summary_stats={
                "total_changes": diff["total_changes"] if diff else 0,
                "hardcode_overrides": len(hardcodes),
                "formula_cache_warnings": len(formula_cache_warnings(rec["stats"])),
                "formula_changes": len(diff["formula_changes"])
                if diff and "formula_changes" in diff
                else 0,
                "value_changes": len(diff["value_changes"])
                if diff and "value_changes" in diff
                else 0,
                "rule_violations": len(violations),
            },
        )
        print_machine_output(output)
        if not output["passed"]:
            raise typer.Exit(1)
        return

    status_color = "green"
    if (
        rec["rule_violations"]
        or (
            file.suffix.lower() == ".xlsx"
            and (
                rec["stats"]["errors"]
                or formula_cache_warnings(rec["stats"])
                or (diff and diff.get("hardcode_overrides"))
            )
        )
        or (file.suffix.lower() == ".html" and rec["stats"]["external_references"])
    ):
        status_color = "yellow"

    summary_lines = [
        f"[bold]File:[/bold] {rec['file']}  |  [bold]Version:[/bold] v{rec['version']}",
        f"[bold]SHA-256:[/bold] {rec['sha256'][:20]}...",
    ]
    extension = file.suffix.lower()
    if extension == ".xlsx":
        summary_lines.append(
            f"[bold]Populated Cells:[/bold] {rec['stats']['populated_cells']}  |  "
            f"[bold]Formula Errors:[/bold] {len(rec['stats']['errors'])}  |  "
            f"[bold]Uncached/Stale Formulas:[/bold] "
            f"{len(formula_cache_warnings(rec['stats']))}"
        )
        for warning in formula_cache_warnings(rec["stats"]):
            console.print(
                f"[yellow]⚠️ {warning['warning'].upper()} FORMULA CACHE[/yellow] "
                f"at {warning['location']}: {warning['formula']}"
            )
    elif extension == ".pptx":
        summary_lines.append(
            f"[bold]Slides:[/bold] {rec['stats']['slides']}  |  "
            f"[bold]Extracted Elements:[/bold] {rec['stats']['elements']}"
        )
    elif extension == ".html":
        summary_lines.append(
            f"[bold]HTML Elements:[/bold] {rec['stats']['elements']}  |  "
            f"[bold]External References:[/bold] {rec['stats']['external_references']}"
        )
    else:
        summary_lines.append(
            f"[bold]CSV Rows:[/bold] {rec['stats']['rows']}  |  "
            f"[bold]Columns:[/bold] {rec['stats']['columns']}"
        )

    if diff:
        if "hardcode_overrides" in diff:
            summary_lines.append(
                f"[bold]Changes vs v{rec['version'] - 1}:[/bold] {diff['total_changes']} total "
                f"([red]{len(diff['hardcode_overrides'])} hardcode overrides[/red], "
                f"{len(diff['formula_changes'])} formula edits, {len(diff['value_changes'])} value updates)"
            )
        elif extension == ".pptx":
            summary_lines.append(
                f"[bold]Changes vs v{rec['version'] - 1}:[/bold] "
                f"{diff['total_changes']} presentation changes"
            )
        else:
            summary_lines.append(
                f"[bold]Changes vs v{rec['version'] - 1}:[/bold] "
                f"{diff['total_changes']} {extension[1:].upper()} changes"
            )

    console.print(
        Panel(
            "\n".join(summary_lines),
            title="Attestory Receipt",
            border_style=status_color,
        )
    )

    if diff and diff.get("hardcode_overrides"):
        table = Table(title="⚠️ HARDCODE OVERRIDE", style="red")
        table.add_column("Location")
        table.add_column("Previous Formula")
        table.add_column("New Hardcoded Value")
        for item in diff["hardcode_overrides"]:
            table.add_row(
                item["location"], item["old_formula"], item["new_hardcoded_val"]
            )
        console.print(table)
    if rec["rule_violations"]:
        table = Table(title="RULE VIOLATIONS", style="bold red")
        table.add_column("Rule")
        table.add_column("Location")
        table.add_column("Expected")
        table.add_column("Actual")
        for violation in rec["rule_violations"]:
            table.add_row(
                violation["rule"],
                violation["location"],
                str(violation.get("expected", "")),
                str(violation.get("actual", "")),
            )
        console.print(table)


@app.command()
def diff(
    file: Path = typer.Argument(
        ..., help="Workspace-relative artifact path to compare snapshots."
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Output strict machine-readable JSON."
    ),
):
    """Compare the latest two Parquet snapshots of a tracked file."""
    root = Path.cwd()
    manifest = root / ATTESTORY_DIR / "history.json"
    if not manifest.exists():
        if json_output:
            print_machine_output(
                machine_output(summary_stats={"total_changes": 0, "rule_violations": 0})
            )
            raise typer.Exit(1)
        console.print(
            "[red]No .attestory/ history found. Run `attestory init` first.[/red]"
        )
        raise typer.Exit(1)

    history = json.loads(manifest.read_text(encoding="utf-8"))
    key = artifact_key(root, file)
    commits = commits_for_artifact(history, key)
    if len(commits) < 2:
        if json_output:
            print_machine_output(
                machine_output(summary_stats={"total_changes": 0, "rule_violations": 0})
            )
            raise typer.Exit(0)
        console.print(
            f"[yellow]Need at least 2 versions of {file.name} to diff.[/yellow]"
        )
        raise typer.Exit(0)

    res = diff_file_snapshots(
        root / commits[-2]["snapshot"],
        root / commits[-1]["snapshot"],
        key,
    )
    if json_output:
        latest = commits[-1]
        violations = latest.get("rule_violations", [])
        formula_errors = (
            latest.get("stats", {}).get("errors", [])
            if file.suffix.lower() == ".xlsx"
            else []
        )
        hardcodes = res.get("hardcode_overrides", [])
        output = machine_output(
            hardcoded_regressions=hardcodes,
            formula_errors=formula_errors,
            rule_violations=violations,
            summary_stats={
                "total_changes": res["total_changes"],
                "hardcode_overrides": len(hardcodes),
                "formula_changes": len(res["formula_changes"])
                if "formula_changes" in res
                else 0,
                "value_changes": len(res["value_changes"])
                if "value_changes" in res
                else 0,
                "rule_violations": len(violations),
            },
        )
        print_machine_output(output)
        if not output["passed"]:
            raise typer.Exit(1)
        return
    console.print(
        f"[bold]Diff {file.name}: v{commits[-2]['version']} -> v{commits[-1]['version']}[/bold] "
        f"({res['total_changes']} changes)"
    )
    if "hardcode_overrides" in res:
        for item in res["hardcode_overrides"]:
            console.print(
                f"  [red]HARDCODE[/red] {item['location']}: {item['old_formula']} -> {item['new_hardcoded_val']}"
            )
        for item in res["formula_changes"]:
            console.print(
                f"  [yellow]FORMULA[/yellow]  {item['location']}: {item['old_formula']} -> {item['new_formula']}"
            )
        for item in res["value_changes"][:20]:
            console.print(
                f"  [cyan]VALUE[/cyan]    {item['location']}: {item['old_val']} -> {item['new_val']}"
            )
    elif "changes" in res:
        for item in res["changes"][:50]:
            if "slide_num" in item:
                description = (
                    f"SLIDE {item['slide_num']} {item['element_type'].upper()} "
                    f"{item['label'] or item['element_id']}"
                )
            elif "metric_type" in item:
                description = (
                    f"{item['metric_type'].upper()} {item['column_name']} "
                    f"{item['label']}"
                )
            else:
                description = (
                    f"{item['element_type'].upper()} "
                    f"{item['label'] or item['element_id']}"
                )
            console.print(
                f"  [cyan]{description}[/cyan]: {item['old_val']} -> {item['new_val']}"
            )


def generate_report(root: Path, filename: str) -> dict:
    """Create a self-contained HTML receipt for the latest snapshot."""
    key = artifact_key(root, Path(filename))
    history_path = root / ATTESTORY_DIR / "history.json"
    if not history_path.exists():
        raise FileNotFoundError(
            "No .attestory/ history found. Run `attestory init` first."
        )

    history = json.loads(history_path.read_text(encoding="utf-8"))
    commits = commits_for_artifact(history, key)
    if not commits:
        raise FileNotFoundError(f"No commits recorded for {key}.")

    latest = commits[-1]
    diff = (
        diff_file_snapshots(
            root / commits[-2]["snapshot"],
            root / latest["snapshot"],
            key,
        )
        if len(commits) > 1
        else None
    )
    hardcodes = len(diff.get("hardcode_overrides", [])) if diff else 0
    formulas = len(diff.get("formula_changes", [])) if diff else 0
    violations = latest.get("rule_violations", [])
    review_needed = bool(
        latest.get("stats", {}).get("errors")
        or formula_cache_warnings(latest.get("stats", {}))
        or hardcodes
        or violations
        or latest.get("stats", {}).get("external_references")
        or latest.get("identity_ambiguous")
        or not latest.get("artifact_key")
    )
    status = "NEEDS REVIEW" if review_needed else "VERIFIED"
    total_changes = diff["total_changes"] if diff else 0

    if diff is None:
        rows = []
    elif "changes" in diff:
        rows = []
        for change in diff["changes"]:
            if "slide_num" in change:
                label = (
                    f"Slide {change['slide_num']} · {change['element_type']} · "
                    f"{change['label'] or change['element_id']}"
                )
            elif "metric_type" in change:
                label = (
                    f"{change['metric_type']} · {change['column_name']} · "
                    f"{change['label']}"
                )
            else:
                label = (
                    f"{change['element_type']} · "
                    f"{change['label'] or change['element_id']}"
                )
            rows.append(
                (
                    label,
                    change["old_val"],
                    change["new_val"],
                    change.get("numeric_delta"),
                    change.get("element_type", change.get("metric_type", "change")),
                )
            )
    else:
        rows = []
        for category, entries in (
            ("Hardcode override", diff["hardcode_overrides"]),
            ("Formula edit", diff["formula_changes"]),
            ("Value update", diff["value_changes"]),
        ):
            for change in entries:
                rows.append(
                    (
                        change["location"],
                        change.get("old_val", change.get("old_formula", "")),
                        change.get(
                            "new_val",
                            change.get(
                                "new_hardcoded_val", change.get("new_formula", "")
                            ),
                        ),
                        "",
                        category,
                    )
                )

    def render_diff_row(row):
        label, old, new, delta, category = row
        category_class = {
            "hardcode override": "hardcode",
            "formula edit": "formula",
            "value update": "value",
            "text": "text",
            "table": "table",
            "chart": "chart",
        }.get(str(category).lower(), "other")
        return (
            f'<tr class="change-{category_class}">'
            f"<td>{escape(str(label))}</td><td>{escape(str(old or '—'))}</td>"
            f"<td>{escape(str(new or '—'))}</td>"
            f"<td>{escape(str(delta if delta is not None else ''))}</td>"
            f"<td>{escape(str(category))}</td></tr>"
        )

    row_html = "".join(render_diff_row(row) for row in rows) or (
        '<tr><td colspan="5" class="empty">No changes in this snapshot.</td></tr>'
    )
    violation_html = "".join(
        f"<li><strong>{escape(item['rule'])}</strong> — {escape(item['location'])}: "
        f"{escape(str(item.get('description') or item.get('expected', 'Assertion failed')))}</li>"
        for item in violations
    )
    formula_cache_html = "".join(
        f"<li><strong>{escape(item['warning'].upper())} FORMULA CACHE</strong> — "
        f"{escape(item['location'])}: {escape(item['formula'])}. "
        "Recalculate the workbook in Excel/LibreOffice.</li>"
        for item in formula_cache_warnings(latest.get("stats", {}))
    )
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Attestory Receipt — {escape(latest["file"])} v{latest["version"]}</title>
<style>
:root{{color-scheme:light dark;--bg:#f4f6fa;--card:#fff;--ink:#172033;--muted:#667085;--line:#dce2eb;--accent:#2563eb;}}
@media(prefers-color-scheme:dark){{:root{{--bg:#111827;--card:#1f2937;--ink:#f3f4f6;--muted:#aab4c4;--line:#374151;--accent:#60a5fa;}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}}
main{{max-width:1100px;margin:40px auto;padding:0 24px}}header,.card,.diff{{background:var(--card);border:1px solid var(--line);border-radius:14px}}
header{{padding:26px;margin-bottom:18px}}h1{{margin:0 0 8px;font-size:25px}}p,.muted{{color:var(--muted)}}.badge{{display:inline-block;padding:6px 12px;border-radius:20px;font-weight:750;background:{"#b42318" if review_needed else "#067647"};color:white}}
.meta{{display:flex;flex-wrap:wrap;gap:12px 24px;margin-top:18px}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:18px 0}}
.card{{padding:18px}}.card span{{display:block;color:var(--muted);font-size:13px}}.card strong{{font-size:26px}}
.diff{{overflow:hidden}}table{{border-collapse:collapse;width:100%;font-size:14px}}th,td{{padding:12px 14px;text-align:left;border-bottom:1px solid var(--line);overflow-wrap:anywhere}}
th{{background:color-mix(in srgb,var(--accent) 10%,var(--card));color:var(--ink)}}tbody tr:nth-child(even){{background:color-mix(in srgb,var(--accent) 4%,var(--card))}}.change-hardcode td:last-child{{color:#b42318;font-weight:700}}.change-formula td:last-child{{color:#b54708;font-weight:700}}.change-value td:last-child,.change-chart td:last-child{{color:#067647;font-weight:700}}.change-text td:last-child,.change-table td:last-child{{color:var(--accent);font-weight:700}}.empty{{text-align:center;color:var(--muted);padding:30px}}.violations{{color:#b42318}}
@media print{{:root{{color-scheme:light}}body{{background:white}}main{{margin:0 auto}}}}
</style></head><body><main>
<header><span class="badge">{status}</span><h1>Attestory Receipt</h1><div>{escape(latest["file"])} · Version {latest["version"]}</div>
<div class="meta"><span><strong>SHA-256</strong> {escape(latest["sha256"])}</span>
<span><strong>Timestamp</strong> {escape(latest["timestamp"])}</span>
<span><strong>Commit note</strong> {escape(latest["message"])}</span></div></header>
<section class="cards"><div class="card"><span>Total Changes</span><strong>{total_changes}</strong></div>
<div class="card"><span>Hardcode Overrides</span><strong>{hardcodes}</strong></div>
<div class="card"><span>Formula Edits</span><strong>{formulas}</strong></div>
<div class="card"><span>Rule Violations</span><strong>{len(violations)}</strong></div>
<div class="card"><span>Formula Cache Warnings</span><strong>{len(formula_cache_warnings(latest.get("stats", {})))}</strong></div>
<div class="card"><span>External References</span><strong>{latest.get("stats", {}).get("external_references", 0)}</strong></div></section>
{"<section class='card violations'><strong>Rule violations</strong><ul>" + violation_html + "</ul></section>" if violations else ""}
{"<section class='card violations'><strong>Formula cache warnings</strong><ul>" + formula_cache_html + "</ul></section>" if formula_cache_html else ""}
<section class="diff"><table><thead><tr><th>Element</th><th>Previous</th><th>Current</th><th>Numeric delta</th><th>Change type</th></tr></thead>
<tbody>{row_html}</tbody></table></section>
</main></body></html>"""
    reports_dir = root / ATTESTORY_DIR / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_stem = Path(latest["file"]).stem.replace(" ", "_")
    matching_artifacts = {
        commit.get("artifact_key") or commit["file"]
        for commit in history["commits"]
        if Path(commit.get("artifact_key", commit["file"])).stem
        == Path(latest["file"]).stem
    }
    report_identity = ""
    if "/" in key or len(matching_artifacts) > 1:
        report_identity = f"_{hashlib.sha256(key.encode('utf-8')).hexdigest()}"
    report_path = reports_dir / (
        f"{report_stem}{report_identity}_v{latest['version']}_receipt.html"
    )
    report_path.write_text(html, encoding="utf-8")
    return {
        "path": str(report_path.relative_to(root)),
        "status": status,
        "summary": {
            "file": latest["file"],
            "version": latest["version"],
            "total_changes": total_changes,
            "hardcode_overrides": hardcodes,
            "formula_changes": formulas,
            "rule_violations": len(violations),
            "formula_cache_warnings": len(
                formula_cache_warnings(latest.get("stats", {}))
            ),
            "external_references": latest.get("stats", {}).get(
                "external_references", 0
            ),
        },
    }


@app.command()
def report(
    file: Path = typer.Argument(
        ..., help="Tracked .xlsx, .pptx, .html, or .csv file to report on."
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Output strict machine-readable JSON."
    ),
):
    """Generate a standalone HTML audit receipt for the latest snapshot."""
    try:
        root = Path.cwd()
        key = artifact_key(root, file)
        result = generate_report(root, key)
    except FileNotFoundError as error:
        if json_output:
            print_machine_output(machine_output(summary_stats={"rule_violations": 0}))
            raise typer.Exit(1) from error
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(1) from error
    if json_output:
        root = Path.cwd()
        history = json.loads(
            (root / ATTESTORY_DIR / "history.json").read_text(encoding="utf-8")
        )
        latest = commits_for_artifact(history, key)[-1]
        file_commits = commits_for_artifact(history, key)
        violations = latest.get("rule_violations", [])
        formula_errors = (
            latest.get("stats", {}).get("errors", [])
            if file.suffix.lower() == ".xlsx"
            else []
        )
        diff = (
            diff_file_snapshots(
                root / file_commits[-2]["snapshot"],
                root / latest["snapshot"],
                key,
            )
            if len(file_commits) >= 2
            else None
        )
        hardcodes = diff.get("hardcode_overrides", []) if diff else []
        output = machine_output(
            hardcoded_regressions=hardcodes,
            formula_errors=formula_errors,
            rule_violations=violations,
            summary_stats=result["summary"],
        )
        print_machine_output(output)
        if not output["passed"]:
            raise typer.Exit(1)
        return
    console.print(f"[green]HTML audit receipt written to {result['path']}[/green]")


@app.command()
def watch(
    recursive: bool = typer.Option(
        False, "--recursive", help="Watch subdirectories recursively."
    ),
    debounce: float = typer.Option(
        2.0, "--debounce", help="Seconds to wait after a save before snapshotting."
    ),
):
    """Automatically snapshot supported files after they are saved."""
    if debounce <= 0:
        raise typer.BadParameter("--debounce must be greater than zero.")

    from attestory.watcher import run_watcher

    console.print(
        f"Watching {Path.cwd()} ({'recursive' if recursive else 'non-recursive'}); "
        f"debounce {debounce:g}s. Press Ctrl+C to stop."
    )

    def on_commit(result):
        record = result["record"]
        console.print(
            f"[green]Auto-snapshot[/green] {record['file']} v{record['version']} "
            f"— SHA-256 {record['sha256'][:12]}"
        )

    def on_error(path, error):
        console.print(f"[red]Could not snapshot {path}: {error}[/red]")

    try:
        run_watcher(
            Path.cwd(),
            recursive=recursive,
            debounce=debounce,
            on_commit=on_commit,
            on_error=on_error,
        )
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped watching.[/yellow]")


@app.command()
def log(
    file: Path = typer.Argument(
        ..., help="Workspace-relative artifact path to display commit history."
    ),
):
    """Display the Attestory version history for a tracked workbook."""
    root = Path.cwd()
    manifest = root / ATTESTORY_DIR / "history.json"
    if not manifest.exists():
        console.print(
            "[red]No .attestory/ history found. Run `attestory init` first.[/red]"
        )
        raise typer.Exit(1)

    history = json.loads(manifest.read_text(encoding="utf-8"))
    key = artifact_key(root, file)
    commits = commits_for_artifact(history, key)
    if not commits:
        console.print(f"[yellow]No commits recorded yet for {file.name}.[/yellow]")
        raise typer.Exit(0)

    table = Table(title=f"Attestory History — {key}")
    table.add_column("Version")
    table.add_column("Timestamp (UTC)")
    table.add_column("SHA-256")
    table.add_column("Changes")
    table.add_column("Hardcode Overrides")
    table.add_column("Message")

    for c in commits:
        table.add_row(
            f"v{c['version']}",
            c["timestamp"][:19],
            c["sha256"][:12] + "...",
            str(c["diff_summary"]["total_changes"]),
            str(c["diff_summary"]["hardcode_overrides"]),
            c["message"],
        )
    console.print(table)


@app.command()
def demo(
    directory: Path = typer.Option(
        Path("./attestory-demo"),
        "--dir",
        help="Directory to create the self-contained demonstration workspace in.",
    ),
):
    """Create and run a five-second example of Attestory's audit workflow."""
    target = directory.expanduser().resolve()
    if target.exists() and any(target.iterdir()):
        console.print(
            f"[red]Demo directory is not empty: {target}. Choose an empty directory.[/red]"
        )
        raise typer.Exit(1)
    target.mkdir(parents=True, exist_ok=True)
    meta_dir = init_workspace(target)
    rules = {
        "version": 1,
        "guardrails": {
            "flag_hardcoded_formula_overrides": True,
            "flag_excel_errors": True,
        },
        "must_be_formula": ["Revenue!B4"],
        "assertions": [],
        "cross_file_checks": [
            {
                "pptx_file": "Board_Deck.pptx",
                "xlsx_file": "Q4_Forecast.xlsx",
                "slide_num": 2,
                "element_type": "text",
                "label": "ARR",
                "location": "Revenue!B4",
                "tolerance": 0.01,
                "description": "Slide 2 ARR must match the forecast total.",
            }
        ],
        "business_rules": [],
    }
    import yaml

    (meta_dir / "rules.yaml").write_text(
        yaml.safe_dump(rules, sort_keys=False), encoding="utf-8"
    )

    import openpyxl
    from pptx import Presentation
    from pptx.util import Inches

    workbook_path = target / "Q4_Forecast.xlsx"

    def save_workbook(hardcoded: bool) -> None:
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "Revenue"
        sheet["A1"] = "Segment"
        sheet["B1"] = "ARR"
        sheet["A2"] = "Enterprise"
        sheet["B2"] = 250000
        sheet["A3"] = "Mid-Market"
        sheet["B3"] = 162000
        sheet["A4"] = "Total"
        sheet["B4"] = 428000 if hardcoded else "=SUM(B2:B3)"
        workbook.save(workbook_path)

    save_workbook(hardcoded=False)
    commit_file(target, workbook_path, "Clean formula baseline")
    save_workbook(hardcoded=True)
    workbook_result = commit_file(target, workbook_path, "Simulated AI hardcode edit")
    overrides = workbook_result["diff"]["hardcode_overrides"]
    for override in overrides:
        console.print(
            f"[bold yellow]HARDCODE OVERRIDE[/bold yellow] at "
            f"{override['location']}: {override['old_formula']} -> "
            f"{override['new_hardcoded_val']}"
        )

    presentation = Presentation()
    presentation.slides.add_slide(presentation.slide_layouts[6])
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    shape = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(6), Inches(1))
    shape.name = "ARR"
    shape.text_frame.text = "ARR $412,000"
    deck_path = target / "Board_Deck.pptx"
    presentation.save(deck_path)
    deck_result = commit_file(target, deck_path, "Board deck ARR figure")
    for violation in deck_result["record"]["rule_violations"]:
        console.print(
            f"[bold red]RULE VIOLATION[/bold red] "
            f"{violation['description']}: expected {violation['expected']}, "
            f"actual {violation['actual']}"
        )

    report_result = generate_report(target, "Q4_Forecast.xlsx")
    console.print(
        Panel.fit(
            f"[bold green]Demo workspace ready[/bold green] at [cyan]{target}[/cyan]\n"
            f"Inspect the receipt: [bold]{target / report_result['path']}[/bold]\n"
            f"Inspect the audit trail: [bold]{target / ATTESTORY_DIR / 'changelog.md'}[/bold]",
            title="Attestory Demo",
        )
    )


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Interface to bind to."),
    port: int = typer.Option(8765, "--port", help="Port to listen on."),
):
    """Start the local FastAPI bridge for ChatGPT Actions and integrations."""
    try:
        import uvicorn

        from attestory.server import create_app
    except ImportError as error:
        console.print(
            '[yellow]Install server extras with: pip install "attestory[server]"[/yellow]'
        )
        raise typer.Exit(1) from error
    uvicorn.run(create_app(Path.cwd()), host=host, port=port)


if __name__ == "__main__":
    app()
