"""Strażnik: każde subprocess.run/Popen w src/ podaje ``creationflags`` (tłumienie okna konsoli).

Regresja na migające czarne okno konsoli na Windows (FFmpeg/yt-dlp/nvidia-smi/whisper-cli mignęłyby
oknem przy każdej sondzie w apce GUI). Jedno źródło flagi to :data:`core.winutil.NO_WINDOW_FLAGS`;
sam ``winutil`` (definicja flagi) jest wykluczony. Test AST (nie regex) — łapie wywołanie po
kształcie ``subprocess.run(...)`` / ``subprocess.Popen(...)`` bez argumentu ``creationflags``.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src" / "mediaforge"
_EXEMPT = {"winutil.py"}  # tu MIESZKA flaga — nie woła subprocess


def _calls_missing_flag(tree: ast.AST) -> list[int]:
    """Numery linii wywołań subprocess.run/Popen bez ``creationflags``."""
    missing: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in {"run", "Popen"}
            and isinstance(func.value, ast.Name)
            and func.value.id == "subprocess"
            and not any(kw.arg == "creationflags" for kw in node.keywords)
        ):
            missing.append(node.lineno)
    return missing


def test_all_subprocess_calls_pass_creationflags() -> None:
    offenders: dict[str, list[int]] = {}
    for path in _SRC.rglob("*.py"):
        if path.name in _EXEMPT:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        missing = _calls_missing_flag(tree)
        if missing:
            offenders[str(path.relative_to(_SRC))] = missing
    assert not offenders, (
        "subprocess.run/Popen bez creationflags (użyj winutil.NO_WINDOW_FLAGS): " + repr(offenders)
    )
