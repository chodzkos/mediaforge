"""Drobne pomocniki specyficzne dla Windows (stdlib, Qt-free).

Jedno źródło prawdy dla flagi ``creationflags`` tłumiącej okno konsoli, którą FFmpeg/yt-dlp/
nvidia-smi/whisper-cli inaczej mignęłyby jako czarne okno przy każdym subprocessie w apce GUI.
Na nie-Windows to ``0`` (bez efektu), więc można podawać ją bezwarunkowo do ``subprocess.run``/
``Popen`` na każdym OS. ``subprocess.CREATE_NO_WINDOW`` (== ``0x08000000``) istnieje tylko na
Windows — dlatego sięgamy po atrybut wyłącznie w gałęzi win32 (na innych OS nie jest wyliczany).
"""

from __future__ import annotations

import subprocess
import sys

# Flaga ukrywająca okno konsoli podprocesu na Windows; 0 (no-op) na pozostałych OS. Blok `if`
# (nie wyrażenie warunkowe) — mypy zawęża po `sys.platform`, więc na nie-Windows nie sięga po
# atrybut istniejący tylko na Windows (a `--platform win32` w CI go widzi).
if sys.platform == "win32":
    NO_WINDOW_FLAGS = subprocess.CREATE_NO_WINDOW
else:
    NO_WINDOW_FLAGS = 0
