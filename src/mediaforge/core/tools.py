"""Wykrywanie narzędzi i GPU (ffmpeg / whisper.cpp / CUDA) — czysty Python.

Zasila pasek statusu GUI oraz profil obliczeniowy (:mod:`core.compute`). Detekcja
jest idempotentna i bez efektów ubocznych (``PATH`` + ``nvidia-smi``). Na Windows
subprocesy startują z ``CREATE_NO_WINDOW``, żeby nie migało okno konsoli.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass

from mediaforge.core.compute import ComputeProfile, GPUArch, classify

# Flaga ukrywająca okno konsoli przy subprocess na Windows.
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_TIMEOUT = 5  # sekundy

# Kandydaci na binarkę whisper.cpp (nazwa zmieniała się między wydaniami).
_WHISPER_BINARIES = ("whisper-cli", "whisper-cpp", "whisper", "main")


@dataclass(slots=True)
class ToolStatus:
    """Status pojedynczego narzędzia (dostępność + szczegół do paska)."""

    name: str
    available: bool
    detail: str = ""


@dataclass(slots=True)
class GpuInfo:
    """Wynik detekcji GPU/CUDA."""

    has_cuda: bool
    name: str
    vram_gb: float
    arch: GPUArch


def _which(*names: str) -> str | None:
    """Pierwsza nazwa znaleziona w ``PATH`` albo ``None``."""
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def detect_ffmpeg() -> ToolStatus:
    """Wykrywa ``ffmpeg`` w ``PATH`` (akwizycja, ekstrakcja audio, detekcja slajdów)."""
    path = _which("ffmpeg")
    return ToolStatus("ffmpeg", path is not None, path or "brak w PATH")


def detect_whisper_cpp() -> ToolStatus:
    """Wykrywa binarkę whisper.cpp (domyślny backend transkrypcji)."""
    path = _which(*_WHISPER_BINARIES)
    return ToolStatus("whisper.cpp", path is not None, path or "brak w PATH")


def _arch_from_name(name: str) -> GPUArch:
    """Mapuje nazwę GPU (z ``nvidia-smi``) na architekturę — heurystyka."""
    n = name.lower()
    if any(tag in n for tag in ("rtx 50", "rtx50", "5090", "5080", "5070", "5060")):
        return GPUArch.BLACKWELL
    if "rtx 40" in n or "rtx40" in n:
        return GPUArch.ADA
    if "rtx 30" in n or "rtx30" in n or "a100" in n or "a6000" in n or "a40" in n:
        return GPUArch.AMPERE
    if "rtx 20" in n or "rtx20" in n or "gtx 16" in n or "gtx16" in n:
        return GPUArch.TURING
    if "gtx 10" in n or "gtx10" in n:
        return GPUArch.PASCAL
    return GPUArch.UNKNOWN


def detect_cuda() -> GpuInfo:
    """Wykrywa GPU NVIDIA przez ``nvidia-smi`` (nazwa + VRAM → architektura).

    Brak ``nvidia-smi`` lub błąd → ``has_cuda=False`` (Tier C). Czytamy tylko
    pierwszą kartę — profil obliczeniowy jest per maszyna.
    """
    exe = _which("nvidia-smi")
    if exe is None:
        return GpuInfo(False, "brak", 0.0, GPUArch.NONE)
    try:
        result = subprocess.run(
            [exe, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_TIMEOUT,
            creationflags=_NO_WINDOW,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return GpuInfo(False, "brak", 0.0, GPUArch.NONE)
    line = (result.stdout or "").strip().splitlines()
    if not line:
        return GpuInfo(False, "brak", 0.0, GPUArch.NONE)
    parts = [p.strip() for p in line[0].split(",")]
    name = parts[0] if parts else "GPU"
    vram_gb = 0.0
    if len(parts) > 1:
        try:
            vram_gb = round(float(parts[1]) / 1024, 1)  # MiB → GiB
        except ValueError:
            vram_gb = 0.0
    return GpuInfo(True, name, vram_gb, _arch_from_name(name))


@dataclass(slots=True)
class Environment:
    """Zbiorczy wynik detekcji środowiska (narzędzia + GPU + profil obliczeniowy)."""

    ffmpeg: ToolStatus
    whisper: ToolStatus
    gpu: GpuInfo
    compute: ComputeProfile


def detect_environment() -> Environment:
    """Pełna detekcja: ffmpeg, whisper.cpp, CUDA → profil obliczeniowy (tier)."""
    gpu = detect_cuda()
    compute = classify(gpu.has_cuda, gpu.vram_gb, gpu.arch)
    return Environment(detect_ffmpeg(), detect_whisper_cpp(), gpu, compute)


def status_line(env: Environment) -> str:
    """Zwięzły opis środowiska do paska statusu (bez hexów, sam tekst)."""
    ffmpeg = "OK" if env.ffmpeg.available else "brak"
    whisper = "OK" if env.whisper.available else "brak"
    cuda = f"{env.gpu.name} {env.gpu.vram_gb:g} GB" if env.gpu.has_cuda else "brak"
    return (
        f"FFmpeg: {ffmpeg}  |  whisper.cpp: {whisper}  |  "
        f"CUDA: {cuda}  |  Tier: {env.compute.tier.value}"
    )
