import json
from pathlib import Path

from click.utils import strip_ansi
from typer.testing import CliRunner

from attestory.cli import app

runner = CliRunner()


def test_install_skill_writes_cowork_skill_with_frontmatter(
    tmp_path: Path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["install-skill"])

    skill_path = tmp_path / "SKILL.md"
    assert result.exit_code == 0, result.stdout
    assert skill_path.is_file()
    assert skill_path.read_text(encoding="utf-8").startswith("---\nname: attestory\n")
    assert "attestory commit <file.xlsx>" in result.stdout


def test_install_skill_writes_claude_code_skill(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        ["install-skill", "--target", "claude-code"],
    )

    skill_path = tmp_path / ".claude" / "skills" / "attestory" / "SKILL.md"
    assert result.exit_code == 0, result.stdout
    assert skill_path.is_file()
    assert skill_path.read_text(encoding="utf-8").startswith("---\nname: attestory\n")


def test_install_skill_writes_both_targets(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["install-skill", "--target", "all"])

    assert result.exit_code == 0, result.stdout
    assert (tmp_path / "SKILL.md").is_file()
    assert (tmp_path / ".claude" / "skills" / "attestory" / "SKILL.md").is_file()


def test_install_skill_protects_existing_file_unless_forced(
    tmp_path: Path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    skill_path = tmp_path / "SKILL.md"
    skill_path.write_text("existing skill", encoding="utf-8")

    result = runner.invoke(app, ["install-skill"])

    assert result.exit_code == 0
    assert "Use --force to overwrite." in result.stdout
    assert skill_path.read_text(encoding="utf-8") == "existing skill"

    result = runner.invoke(app, ["install-skill", "--force"])

    assert result.exit_code == 0, result.stdout
    assert skill_path.read_text(encoding="utf-8").startswith("---\nname: attestory\n")


def test_install_skill_registers_post_tool_use_hook_idempotently(
    tmp_path: Path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        json.dumps(
            {
                "permissions": {"allow": ["Read"]},
                "hooks": {
                    "PostToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [{"type": "command", "command": "custom-audit"}],
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    for _ in range(2):
        result = runner.invoke(
            app, ["install-skill", "--target", "claude-code", "--hook"]
        )
        assert result.exit_code == 0, result.stdout

    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert settings["permissions"] == {"allow": ["Read"]}
    hooks = settings["hooks"]["PostToolUse"]
    assert len(hooks) == 2
    assert hooks[0]["hooks"][0]["command"] == "custom-audit"
    assert hooks[1]["matcher"] == "Write|Edit|MultiEdit"
    assert hooks[1]["hooks"][0]["command"] == "attestory hook-post-tool-use"


def test_install_skill_rejects_hook_for_cowork_only(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["install-skill", "--hook"])

    assert result.exit_code != 0
    assert "--hook requires --target claude-code or --target all" in strip_ansi(
        result.output
    )


def test_post_tool_use_hook_audits_supported_files_only(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    csv_path = tmp_path / "actuals.csv"
    csv_path.write_text("Revenue\n412\n", encoding="utf-8")
    event = json.dumps(
        {
            "cwd": str(tmp_path),
            "tool_name": "Write",
            "tool_input": {"file_path": str(csv_path)},
        }
    )

    result = runner.invoke(app, ["hook-post-tool-use"], input=event)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["passed"] is True
    assert (tmp_path / ".attestory" / "history.json").is_file()

    ignored = runner.invoke(
        app,
        ["hook-post-tool-use"],
        input=json.dumps(
            {
                "cwd": str(tmp_path),
                "tool_name": "Write",
                "tool_input": {"file_path": str(tmp_path / "notes.txt")},
            }
        ),
    )

    assert ignored.exit_code == 0
    assert ignored.stdout == ""
