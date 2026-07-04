"""Strażnik architektury: ``core`` NIE importuje Qt (twarda reguła CLAUDE.md).

Sprawdzane w osobnym procesie, żeby import PySide6 przez inne testy/pytest-qt nie
zafałszował wyniku (``sys.modules`` jest współdzielone w obrębie procesu).
"""

from __future__ import annotations

import subprocess
import sys

_PROBE = """
import sys
import mediaforge.core.config
import mediaforge.core.secrets
import mediaforge.core.logging_setup
import mediaforge.core.detection
import mediaforge.core.compute
import mediaforge.core.jobs
import mediaforge.core.library
import mediaforge.core.ai.providers
import mediaforge.core.ai.transcribe
import mediaforge.core.ai.routing
import mediaforge.core.ai.summarize
import mediaforge.core.engines.base
import mediaforge.core.engines.download_engine
import mediaforge.core.engines.podcast
import mediaforge.core.library.profiles
leaked = sorted(m for m in sys.modules if m == "PySide6" or m.startswith("PySide6."))
assert not leaked, f"core importuje Qt: {leaked}"
"""


def test_core_does_not_import_qt() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
