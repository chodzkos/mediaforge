"""CLI Typer — ``--help`` i podstawowe komendy headless."""

from __future__ import annotations

import io
import sys

import pytest
from typer.testing import CliRunner

from mediaforge import __version__
from mediaforge.cli.main import _force_utf8_stdio, app

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


# ── M22: doctor nie wywala się na polskiej konsoli (cp1250) ────────────────────

_FAKE_REPORT = {
    "system": {"os": "Windows", "python": "3.12"},
    # ffmpeg/gpu niedostępne → render emituje znaki ✗ i „→" (których cp1250 nie koduje).
    "ffmpeg": {"available": False, "encoders": {}, "encoders_usable": {}},
    "whispercpp": {"available": False},
    "compute": {"tier": "C", "note": "brak GPU"},
    "ytdlp": {"available": True, "version": "1"},
    "gpu": {"available": False},
    "litellm": {"available": False, "base_url": "http://localhost:4000"},
    "summary": {},
    "providers": {"openai": False},
}


def test_doctor_survives_cp1250_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regresja M22: raport z ✓/✗/→ nie rzuca UnicodeEncodeError na konsoli cp1250 (Windows).

    Bez poprawki: echo znaków raportu do strumienia cp1250 → UnicodeEncodeError (exit ≠ 0).
    Z poprawką: callback CLI przełącza stdout na UTF-8 (bo platform == win32) → brak crashu.
    """
    from mediaforge.core import detection

    monkeypatch.setattr(detection, "check_all", lambda **_kw: _FAKE_REPORT)
    monkeypatch.setattr(sys, "platform", "win32")  # aktywuje reconfigure w callbacku

    result = CliRunner(charset="cp1250").invoke(app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert result.exception is None  # dawniej: UnicodeEncodeError
    assert "System:" in result.stdout  # nazwy sekcji (ASCII) przetrwały
    assert "FFmpeg:" in result.stdout


def test_force_utf8_stdio_reconfigures_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Helper przełącza cp1250 → UTF-8 na Windows; znaki raportu nie rzucają na strumieniu."""
    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1250")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", stream)

    _force_utf8_stdio()

    assert sys.stdout.encoding == "utf-8"
    sys.stdout.write("✓ ✗ → ·")  # dawniej UnicodeEncodeError na cp1250 — teraz OK


def test_force_utf8_stdio_noop_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Poza Windows helper nie dotyka strumieni (UTF-8 jest tam domyślnie)."""
    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1250")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys, "stdout", stream)

    _force_utf8_stdio()

    assert sys.stdout.encoding == "cp1250"  # nietknięte
