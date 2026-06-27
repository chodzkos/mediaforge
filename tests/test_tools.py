"""Detekcja środowiska — mapowanie architektury GPU i opis paska statusu."""

from __future__ import annotations

from mediaforge.core.compute import ComputeTier, GPUArch, classify
from mediaforge.core.tools import (
    Environment,
    GpuInfo,
    ToolStatus,
    _arch_from_name,
    status_line,
)


def test_arch_from_name() -> None:
    assert _arch_from_name("NVIDIA GeForce RTX 5090 Laptop GPU") is GPUArch.BLACKWELL
    assert _arch_from_name("NVIDIA GeForce RTX 4090") is GPUArch.ADA
    assert _arch_from_name("NVIDIA GeForce GTX 1070") is GPUArch.PASCAL
    assert _arch_from_name("Some Unknown Card") is GPUArch.UNKNOWN


def test_status_line_contains_all_sections() -> None:
    gpu = GpuInfo(has_cuda=True, name="RTX 5090", vram_gb=24.0, arch=GPUArch.BLACKWELL)
    env = Environment(
        ffmpeg=ToolStatus("ffmpeg", True, "/usr/bin/ffmpeg"),
        whisper=ToolStatus("whisper.cpp", False, "brak"),
        gpu=gpu,
        compute=classify(True, 24.0, GPUArch.BLACKWELL),
    )
    line = status_line(env)
    assert "FFmpeg: OK" in line
    assert "whisper.cpp: brak" in line
    assert "RTX 5090" in line
    assert f"Tier: {ComputeTier.A.value}" in line


def test_status_line_no_cuda() -> None:
    env = Environment(
        ffmpeg=ToolStatus("ffmpeg", False),
        whisper=ToolStatus("whisper.cpp", False),
        gpu=GpuInfo(False, "brak", 0.0, GPUArch.NONE),
        compute=classify(False, 0.0),
    )
    line = status_line(env)
    assert "CUDA: brak" in line
    assert f"Tier: {ComputeTier.C.value}" in line
