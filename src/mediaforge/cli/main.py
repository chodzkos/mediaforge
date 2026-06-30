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
    # doctor odpala empiryczną sondę runtime whisper.cpp (probe_whisper=True; cache).
    report = detection.check_all(
        whispercpp_path=config.get_whispercpp_path(cfg),
        litellm_base_url=config.get_litellm_base_url(cfg),
        whisper_model=config.get_whisper_model(cfg),
        probe_whisper=True,
    )
    if as_json:
        import json

        # default=str — path to obiekt Path (niesserializowalny wprost).
        typer.echo(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    else:
        typer.echo(detection.render_report(report))


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
