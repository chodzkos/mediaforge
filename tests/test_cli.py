"""CLI Typer — ``--help`` i podstawowe komendy headless."""

from __future__ import annotations

from typer.testing import CliRunner

from mediaforge import __version__
from mediaforge.cli.main import app

runner = CliRunner()


def test_help_runs() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "mediaforge" in result.stdout


def test_version_command() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_paths_command() -> None:
    result = runner.invoke(app, ["paths"])
    assert result.exit_code == 0
    assert "config:" in result.stdout
    assert "logs:" in result.stdout
