"""CLI mediaforge (Typer). Komendy dochodzą wraz z kolejnymi etapami."""

from __future__ import annotations

import typer

app = typer.Typer(help="mediaforge — archiwizacja i przetwarzanie materiałów edukacyjnych.")


@app.command()
def version() -> None:
    """Wypisz wersję."""
    from mediaforge import __version__

    typer.echo(__version__)


if __name__ == "__main__":
    app()
