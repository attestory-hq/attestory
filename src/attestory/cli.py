import json
from html import escape
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from attestory.engine import (
    ATTESTORY_DIR,
    commit_file,
    diff_file_snapshots,
    init_workspace,
)

app = typer.Typer(
    help="Attestory — Local version control & deterministic math auditing for analytics."
)
console = Console()


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
def commit(
    file: Path = typer.Argument(
        ..., exists=True, help="Path to the .xlsx or .pptx file to audit and snapshot."
    ),
    message: str = typer.Option(
        "", "-m", "--message", help="Optional note describing this update."
    ),
):
    """Audit and snapshot an .xlsx workbook or .pptx presentation."""
    result = commit_file(Path.cwd(), file.resolve(), message=message)
    rec = result["record"]
    diff = result["diff"]

    status_color = "green"
    if rec["rule_violations"] or (
        file.suffix.lower() == ".xlsx"
        and (rec["stats"]["errors"] or (diff and diff.get("hardcode_overrides")))
    ):
        status_color = "yellow"

    summary_lines = [
        f"[bold]File:[/bold] {rec['file']}  |  [bold]Version:[/bold] v{rec['version']}",
        f"[bold]SHA-256:[/bold] {rec['sha256'][:20]}...",
    ]
    if file.suffix.lower() == ".xlsx":
        summary_lines.append(
            f"[bold]Populated Cells:[/bold] {rec['stats']['populated_cells']}  |  "
            f"[bold]Formula Errors:[/bold] {len(rec['stats']['errors'])}"
        )
    else:
        summary_lines.append(
            f"[bold]Slides:[/bold] {rec['stats']['slides']}  |  "
            f"[bold]Extracted Elements:[/bold] {rec['stats']['elements']}"
        )

    if diff:
        if "hardcode_overrides" in diff:
            summary_lines.append(
                f"[bold]Changes vs v{rec['version'] - 1}:[/bold] {diff['total_changes']} total "
                f"([red]{len(diff['hardcode_overrides'])} hardcode overrides[/red], "
                f"{len(diff['formula_changes'])} formula edits, {len(diff['value_changes'])} value updates)"
            )
        else:
            summary_lines.append(
                f"[bold]Changes vs v{rec['version'] - 1}:[/bold] "
                f"{diff['total_changes']} presentation changes"
            )

    console.print(
        Panel(
            "\n".join(summary_lines),
            title="Attestory Receipt",
            border_style=status_color,
        )
    )

    if diff and diff.get("hardcode_overrides"):
        table = Table(title="⚠️ Formula Overwritten With Hardcoded Value", style="red")
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
        ..., help="Filename (e.g. Q4_Forecast.xlsx) to compare last two snapshots."
    ),
):
    """Compare the latest two Parquet snapshots of a tracked workbook or presentation."""
    root = Path.cwd()
    manifest = root / ATTESTORY_DIR / "history.json"
    if not manifest.exists():
        console.print(
            "[red]No .attestory/ history found. Run `attestory init` first.[/red]"
        )
        raise typer.Exit(1)

    history = json.loads(manifest.read_text(encoding="utf-8"))
    commits = [c for c in history["commits"] if c["file"] == file.name]
    if len(commits) < 2:
        console.print(
            f"[yellow]Need at least 2 versions of {file.name} to diff.[/yellow]"
        )
        raise typer.Exit(0)

    res = diff_file_snapshots(
        root / commits[-2]["snapshot"],
        root / commits[-1]["snapshot"],
        file.name,
    )
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
    else:
        for item in res["changes"][:50]:
            console.print(
                f"  [cyan]SLIDE {item['slide_num']} {item['element_type'].upper()}[/cyan] "
                f"{item['label'] or item['element_id']}: {item['old_val']} -> {item['new_val']}"
            )


@app.command()
def report(
    file: Path = typer.Argument(
        ..., help="Tracked .xlsx or .pptx file to create an HTML audit receipt for."
    ),
):
    """Generate a standalone HTML audit receipt for the latest snapshot."""
    root = Path.cwd()
    history_path = root / ATTESTORY_DIR / "history.json"
    if not history_path.exists():
        console.print("[red]No .attestory/ history found. Run `attestory init` first.[/red]")
        raise typer.Exit(1)

    history = json.loads(history_path.read_text(encoding="utf-8"))
    commits = [entry for entry in history["commits"] if entry["file"] == file.name]
    if not commits:
        console.print(f"[red]No commits recorded for {file.name}.[/red]")
        raise typer.Exit(1)

    latest = commits[-1]
    diff = (
        diff_file_snapshots(
            root / commits[-2]["snapshot"],
            root / latest["snapshot"],
            file.name,
        )
        if len(commits) > 1
        else None
    )
    hardcodes = len(diff.get("hardcode_overrides", [])) if diff else 0
    formulas = len(diff.get("formula_changes", [])) if diff else 0
    violations = latest.get("rule_violations", [])
    review_needed = bool(
        latest.get("stats", {}).get("errors")
        or hardcodes
        or violations
    )
    status = "NEEDS REVIEW" if review_needed else "VERIFIED"
    total_changes = diff["total_changes"] if diff else 0

    if diff is None:
        rows = []
    elif "changes" in diff:
        rows = [
            (
                (
                    f"Slide {change['slide_num']} · {change['element_type']} · "
                    f"{change['label'] or change['element_id']}"
                ),
                change["old_val"],
                change["new_val"],
                change["numeric_delta"],
                change["element_type"],
            )
            for change in diff["changes"]
        ]
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
                        change.get("new_val", change.get("new_hardcoded_val", change.get("new_formula", ""))),
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
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Attestory Receipt — {escape(latest['file'])} v{latest['version']}</title>
<style>
:root{{color-scheme:light dark;--bg:#f4f6fa;--card:#fff;--ink:#172033;--muted:#667085;--line:#dce2eb;--accent:#2563eb;}}
@media(prefers-color-scheme:dark){{:root{{--bg:#111827;--card:#1f2937;--ink:#f3f4f6;--muted:#aab4c4;--line:#374151;--accent:#60a5fa;}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}}
main{{max-width:1100px;margin:40px auto;padding:0 24px}}header,.card,.diff{{background:var(--card);border:1px solid var(--line);border-radius:14px}}
header{{padding:26px;margin-bottom:18px}}h1{{margin:0 0 8px;font-size:25px}}p,.muted{{color:var(--muted)}}.badge{{display:inline-block;padding:6px 12px;border-radius:20px;font-weight:750;background:{'#b42318' if review_needed else '#067647'};color:white}}
.meta{{display:flex;flex-wrap:wrap;gap:12px 24px;margin-top:18px}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:18px 0}}
.card{{padding:18px}}.card span{{display:block;color:var(--muted);font-size:13px}}.card strong{{font-size:26px}}
.diff{{overflow:hidden}}table{{border-collapse:collapse;width:100%;font-size:14px}}th,td{{padding:12px 14px;text-align:left;border-bottom:1px solid var(--line);overflow-wrap:anywhere}}
th{{background:color-mix(in srgb,var(--accent) 10%,var(--card));color:var(--ink)}}tbody tr:nth-child(even){{background:color-mix(in srgb,var(--accent) 4%,var(--card))}}.change-hardcode td:last-child{{color:#b42318;font-weight:700}}.change-formula td:last-child{{color:#b54708;font-weight:700}}.change-value td:last-child,.change-chart td:last-child{{color:#067647;font-weight:700}}.change-text td:last-child,.change-table td:last-child{{color:var(--accent);font-weight:700}}.empty{{text-align:center;color:var(--muted);padding:30px}}.violations{{color:#b42318}}
@media print{{:root{{color-scheme:light}}body{{background:white}}main{{margin:0 auto}}}}
</style></head><body><main>
<header><span class="badge">{status}</span><h1>Attestory Receipt</h1><div>{escape(latest['file'])} · Version {latest['version']}</div>
<div class="meta"><span><strong>SHA-256</strong> {escape(latest['sha256'])}</span>
<span><strong>Timestamp</strong> {escape(latest['timestamp'])}</span>
<span><strong>Commit note</strong> {escape(latest['message'])}</span></div></header>
<section class="cards"><div class="card"><span>Total Changes</span><strong>{total_changes}</strong></div>
<div class="card"><span>Hardcode Overrides</span><strong>{hardcodes}</strong></div>
<div class="card"><span>Formula Edits</span><strong>{formulas}</strong></div>
<div class="card"><span>Rule Violations</span><strong>{len(violations)}</strong></div></section>
{"<section class='card violations'><strong>Rule violations</strong><ul>" + violation_html + "</ul></section>" if violations else ""}
<section class="diff"><table><thead><tr><th>Element</th><th>Previous</th><th>Current</th><th>Numeric delta</th><th>Change type</th></tr></thead>
<tbody>{row_html}</tbody></table></section>
</main></body></html>"""
    reports_dir = root / ATTESTORY_DIR / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_stem = Path(latest["file"]).stem.replace(" ", "_")
    report_path = reports_dir / f"{report_stem}_v{latest['version']}_receipt.html"
    report_path.write_text(html, encoding="utf-8")
    console.print(f"[green]HTML audit receipt written to {report_path}[/green]")


@app.command()
def log(
    file: Path = typer.Argument(
        ..., help="Filename (e.g. Q4_Forecast.xlsx) to display commit history."
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
    commits = [c for c in history["commits"] if c["file"] == file.name]
    if not commits:
        console.print(f"[yellow]No commits recorded yet for {file.name}.[/yellow]")
        raise typer.Exit(0)

    table = Table(title=f"Attestory History — {file.name}")
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


if __name__ == "__main__":
    app()
