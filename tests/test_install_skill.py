from pathlib import Path

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
