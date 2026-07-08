"""Logowanie do pliku + globalna obsługa nieobsłużonych wyjątków.

Czysty Python (bez Qt) — log idzie do katalogu logów wg ``platformdirs``
(``%LOCALAPPDATA%\\mediaforge\\Logs`` na Windows). GUI woła :func:`setup_logging`
i :func:`install_excepthook` przy starcie; CLI może wołać samo ``setup_logging``.

Nigdy nie logujemy sekretów (cookies/tokeny/hasła) — patrz LEGAL_BOUNDARIES.md.
"""

from __future__ import annotations

import logging
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import TracebackType

import platformdirs

from mediaforge.core.config import APP_NAME

_LOG_FILE = "mediaforge.log"
_MAX_BYTES = 2_000_000
_BACKUPS = 3
_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"

logger = logging.getLogger("mediaforge")


def log_dir() -> Path:
    """Katalog logów aplikacji (wg konwencji systemu, przez platformdirs)."""
    return Path(platformdirs.user_log_dir(APP_NAME, appauthor=False))


def setup_logging(level: int = logging.INFO) -> Path:
    """Konfiguruje logowanie do rotowanego pliku i zwraca jego ścieżkę.

    Idempotentne — wielokrotne wywołanie nie dubluje handlerów (np. test + start).
    """
    directory = log_dir()
    directory.mkdir(parents=True, exist_ok=True)
    log_path = directory / _LOG_FILE

    root = logging.getLogger()
    root.setLevel(level)
    already = any(
        isinstance(h, RotatingFileHandler) and Path(getattr(h, "baseFilename", "")) == log_path
        for h in root.handlers
    )
    if not already:
        handler = RotatingFileHandler(
            log_path, maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter(_FORMAT))
        root.addHandler(handler)
    return log_path


def install_excepthook() -> None:
    """Przekierowuje nieobsłużone wyjątki do logu (zamiast cichego zniknięcia).

    Obejmuje główny wątek (``sys.excepthook``) ORAZ wątki robocze (``threading.excepthook``:
    dyspozytor kolejki jobs, sondy ``QThreadPool`` z M9, worker stop+concat z M7) — bez tego
    drugiego wyjątek w ``Thread.run`` omija hook głównego wątku i znika z logu.
    ``KeyboardInterrupt`` przepuszczamy do domyślnej obsługi (czysty Ctrl+C).
    """
    previous = sys.excepthook

    def _hook(
        exc_type: type[BaseException],
        exc: BaseException,
        tb: TracebackType | None,
    ) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            previous(exc_type, exc, tb)
            return
        logger.critical("Nieobsłużony wyjątek", exc_info=(exc_type, exc, tb))
        previous(exc_type, exc, tb)

    sys.excepthook = _hook

    previous_thread = threading.excepthook

    def _thread_hook(args: threading.ExceptHookArgs) -> None:
        if issubclass(args.exc_type, KeyboardInterrupt):
            previous_thread(args)  # spójnie z hookiem głównym: czysty Ctrl+C nie idzie do logu
            return
        thread_name = args.thread.name if args.thread is not None else "?"
        if args.exc_value is not None:
            logger.critical(
                "Nieobsłużony wyjątek w wątku %s",
                thread_name,
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )
        else:
            # exc_value bywa None przy zamykaniu interpretera — logujemy bez tracebacku.
            logger.critical("Nieobsłużony wyjątek w wątku %s (bez szczegółów)", thread_name)
        previous_thread(args)  # zachowaj domyślną diagnostykę (traceback na stderr)

    threading.excepthook = _thread_hook
