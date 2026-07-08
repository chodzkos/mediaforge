"""Globalny excepthook: główny wątek (sys.excepthook) + wątki robocze (threading.excepthook)."""

from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Iterator

import pytest

from mediaforge.core.logging_setup import install_excepthook


@pytest.fixture(autouse=True)
def _restore_hooks() -> Iterator[None]:
    """install_excepthook mutuje globalne hooki — przywróć oryginały po każdym teście."""
    saved_sys = sys.excepthook
    saved_thread = threading.excepthook
    try:
        yield
    finally:
        sys.excepthook = saved_sys
        threading.excepthook = saved_thread


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_thread_excepthook_logs_critical_with_thread_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Wyjątek w wątku roboczym trafia do logu jako CRITICAL, z nazwą wątku i tracebackiem."""
    install_excepthook()

    def boom() -> None:
        raise ValueError("bum w wątku")

    with caplog.at_level(logging.CRITICAL, logger="mediaforge"):
        t = threading.Thread(target=boom, name="worker-x")
        t.start()
        t.join()

    critical = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert critical, "brak wpisu CRITICAL z wątku roboczego"
    rec = critical[-1]
    assert "worker-x" in rec.getMessage()  # nazwa wątku w komunikacie
    assert rec.exc_info is not None  # traceback dołączony do wpisu


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_thread_excepthook_ignores_keyboard_interrupt(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """KeyboardInterrupt w wątku nie loguje CRITICAL (spójnie z hookiem głównym)."""
    install_excepthook()

    def interrupt() -> None:
        raise KeyboardInterrupt

    with caplog.at_level(logging.CRITICAL, logger="mediaforge"):
        t = threading.Thread(target=interrupt, name="worker-ki")
        t.start()
        t.join()

    assert not [r for r in caplog.records if r.levelno == logging.CRITICAL]
