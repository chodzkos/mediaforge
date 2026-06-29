"""Warstwa detekcji mediaforge: hardware (GPU) + tools (narzędzia) + report (doctor).

Część generyczna (`hardware.check_gpu`, `tools.probe_tool`/`command_in_path`/`api_key_present`)
jest przygotowana do wydzielenia do osobnego, Qt-free pakietu `chodzkos-detection` — struktura
`detection/` celowo lustruje pdf2md, by przyszłe `git mv detection/ → chodzkos-detection` było
czyste. Render i polityka tierów (compute.classify) zostają app-side. Patrz docs/ARCHITECTURE.md.
"""

from . import hardware, report, tools
from .hardware import arch_from_name, check_gpu, detect_arch, resolved_arch, resolved_profile
from .report import check_all, render_report, whisper_cuda_ok
from .tools import check_ffmpeg, check_providers, check_whispercpp, check_ytdlp, probe_tool

__all__ = [
    "hardware",
    "tools",
    "report",
    "check_all",
    "render_report",
    "whisper_cuda_ok",
    "probe_tool",
    "check_gpu",
    "check_ffmpeg",
    "check_whispercpp",
    "check_ytdlp",
    "check_providers",
    "detect_arch",
    "arch_from_name",
    "resolved_arch",
    "resolved_profile",
]
