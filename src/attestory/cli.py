import json
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from attestory.engine import ATTESTORY_DIR, commit_file, diff_snapshots, init_workspace

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
        ..., exists=True, help="Path to the .xlsx workbook to audit and snapshot."
    ),
    message: str = typer.Option(
        "", "-m", "--message", help="Optional note describing this update."
    ),
):
    """Audit an .xlsx workbook, save a Parquet snapshot, and diff against the prior version."""
    result = commit_file(Path.cwd(), file.resolve(), message=message)
    rec = result["record"]
    diff = result["diff"]

    status_color = "green"
    if rec["stats"]["errors"] or (diff and diff["hardcode_overrides"]):
        status_color = "yellow"

    summary_lines = [
        f"[bold]File:[/bold] {rec['file']}  |  [bold]Version:[/bold] v{rec['version']}",
        f"[bold]SHA-256:[/bold] {rec['sha256'][:20]}...",
        f"[bold]Populated Cells:[/bold] {rec['stats']['populated_cells']}  |  [bold]Formula Errors:[/bold] {len(rec['stats']['errors'])}",
    ]

    if diff:
        summary_lines.append(
            f"[bold]Changes vs v{rec['version'] - 1}:[/bold] {diff['total_changes']} total "
            f"([red]{len(diff['hardcode_overrides'])} hardcode overrides[/red], "
            f"{len(diff['formula_changes'])} formula edits, {len(diff['value_changes'])} value updates)"
        )

    console.print(
        Panel(
            "\n".join(summary_lines),
            title="Attestory Receipt",
            border_style=status_color,
        )
    )

    if diff and diff["hardcode_overrides"]:
        table = Table(title="⚠️ Formula Overwritten With Hardcoded Value", style="red")
        table.add_column("Location")
        table.add_column("Previous Formula")
        table.add_column("New Hardcoded Value")
        for item in diff["hardcode_overrides"]:
            table.add_row(
                item["location"], item["old_formula"], item["new_hardcoded_val"]
            )
        console.print(table)


@app.command()
def diff(
    file: Path = typer.Argument(
        ..., help="Filename (e.g. Q4_Forecast.xlsx) to compare last two snapshots."
    ),
):
    """Compare the latest two Parquet snapshots of a tracked workbook."""
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

    res = diff_snapshots(root / commits[-2]["snapshot"], root / commits[-1]["snapshot"])
    console.print(
        f"[bold]Diff {file.name}: v{commits[-2]['version']} -> v{commits[-1]['version']}[/bold] "
        f"({res['total_changes']} changes)"
    )
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
