"""CLI mediaforge (Typer) — headless odpowiednik operacji GUI.

Komendy dochodzą wraz z kolejnymi etapami (record/import/transcribe/summarize…).
W S0: ``version``, ``info`` (środowisko) i ``paths`` (katalogi konfiguracji/logów).
"""

from __future__ import annotations

import typer

app = typer.Typer(
    help="mediaforge — archiwizacja i przetwarzanie materiałów edukacyjnych.",
    no_args_is_help=True,
)


@app.command()
def version() -> None:
    """Wypisz wersję."""
    from mediaforge import __version__

    typer.echo(__version__)


@app.command()
def info() -> None:
    """Wykryte narzędzia i GPU (ffmpeg / whisper.cpp / CUDA + tier obliczeniowy)."""
    from mediaforge.core.tools import detect_environment, status_line

    env = detect_environment()
    typer.echo(status_line(env))
    typer.echo(env.compute.note)


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
