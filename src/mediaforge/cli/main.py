"""CLI mediaforge (Typer) — headless odpowiednik operacji GUI.

Komendy dochodzą wraz z kolejnymi etapami (record/import/transcribe/summarize…).
W S0: ``version``, ``info`` (środowisko) i ``paths`` (katalogi konfiguracji/logów).
"""

from __future__ import annotations

import contextlib
import io
import sys
from typing import cast

import typer

app = typer.Typer(
    help="mediaforge — archiwizacja i przetwarzanie materiałów edukacyjnych.",
    no_args_is_help=True,
)


def _force_utf8_stdio() -> None:
    """Na Windows wymusza UTF-8 na stdout/stderr (przed jakimkolwiek echo).

    Domyślna polska konsola to cp1250 — a raport ``doctor`` (render_report) używa znaków
    ✓/✗/→/·, których cp1250 nie koduje → ``UnicodeEncodeError`` wywala narzędzie diagnostyczne.
    Poprawka u WEJŚCIA CLI (nie w treści raportu — znaki są częścią czytelności). ``errors=
    "replace"`` chroni też przed egzotycznymi kodowaniami. ``suppress(AttributeError)`` bo stdout
    bywa przekierowany/nie-TTY (obiekt bez ``reconfigure`` albo ``None`` pod pythonw).
    """
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError):
            cast(io.TextIOWrapper, stream).reconfigure(encoding="utf-8", errors="replace")


@app.callback()
def _main() -> None:
    """Wspólne wejście CLI (callback Typer) — biegnie przed każdą komendą."""
    _force_utf8_stdio()


@app.command()
def version() -> None:
    """Wypisz wersję."""
    from mediaforge import __version__

    typer.echo(__version__)


@app.command()
def info() -> None:
    """Wykryte narzędzia i GPU (ffmpeg / whisper.cpp / CUDA + tier obliczeniowy)."""
    from mediaforge.core import config, detection

    cfg = config.load()
    report = detection.check_all(
        whispercpp_path=config.get_whispercpp_path(cfg),
        litellm_base_url=config.get_litellm_base_url(cfg),
    )
    typer.echo(detection.status_line(report))
    typer.echo(report["compute"]["note"])


@app.command()
def doctor(as_json: bool = typer.Option(False, "--json")) -> None:
    """Sprawdź dostępność narzędzi i zasobów (ffmpeg, whisper.cpp, GPU, LiteLLM)."""
    from mediaforge.core import config, detection

    cfg = config.load()
    # doctor odpala empiryczne sondy runtime: whisper.cpp (probe_whisper) i enkodery FFmpeg
    # (probe_encoders) — pokazuje, co REALNIE działa, nie co jest w buildzie. Obie cache'owane.
    report = detection.check_all(
        whispercpp_path=config.get_whispercpp_path(cfg),
        litellm_base_url=config.get_litellm_base_url(cfg),
        whisper_model=config.get_whisper_model(cfg),
        summary_model_local=config.get_summary_model_local(cfg),
        summary_model_cloud=config.get_summary_model_cloud(cfg),
        vlm_model_local=config.get_vlm_model_local(cfg),
        vlm_model_cloud=config.get_vlm_model_cloud(cfg),
        probe_whisper=True,
        probe_encoders=True,
    )
    if as_json:
        import json

        # default=str — path to obiekt Path (niesserializowalny wprost).
        typer.echo(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    else:
        typer.echo(detection.render_report(report))


@app.command(name="update-ytdlp")
def update_ytdlp() -> None:
    """Zaktualizuj yt-dlp (``-U`` dla binarki standalone; instrukcja uv dla modułu pythonowego)."""
    from mediaforge.core.detection import tools
    from mediaforge.core.engines.download_engine import run_ytdlp_update

    report = tools.check_ytdlp()
    typer.echo(run_ytdlp_update(available=bool(report.get("available")), path=report.get("path")))


@app.command()
def paths() -> None:
    """Pokaż katalogi konfiguracji i logów aplikacji."""
    from chodzkos_gui_kit.config import config_dir

    from mediaforge.core.config import APP_NAME
    from mediaforge.core.logging_setup import log_dir

    typer.echo(f"config: {config_dir(APP_NAME)}")
    typer.echo(f"logs:   {log_dir()}")


if __name__ == "__main__":
    app()
