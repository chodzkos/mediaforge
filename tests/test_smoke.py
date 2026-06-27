"""Smoke testy scaffoldu — rozbudowywane od etapu S0."""

from mediaforge import __version__
from mediaforge.core.ai.providers import ModelSpec, Provider, ProviderRegistry, Task
from mediaforge.core.ai.transcribe import Backend, select_backend
from mediaforge.core.compute import ComputeTier, GPUArch, classify


def test_version() -> None:
    assert __version__


def test_backend_autoselect() -> None:
    # Mocny GPU -> tor lokalny (whisper.cpp).
    assert select_backend(has_cuda=True, vram_gb=24, arch=GPUArch.BLACKWELL) is Backend.WHISPERCPP
    # Brak GPU -> chmura.
    assert select_backend(has_cuda=False, vram_gb=0) is Backend.CLOUD
    # 1070 (Pascal, 8 GB): transkrypcja zostaje lokalna.
    assert select_backend(has_cuda=True, vram_gb=8, arch=GPUArch.PASCAL) is Backend.WHISPERCPP


def test_compute_tiers() -> None:
    # 5090 -> Tier A: wszystko lokalnie.
    a = classify(has_cuda=True, vram_gb=24, arch=GPUArch.BLACKWELL)
    assert a.tier is ComputeTier.A and a.llm_local and a.vlm_local
    # 1070 -> Tier B: transkrypcja lokalnie, VLM w chmurze.
    b = classify(has_cuda=True, vram_gb=8, arch=GPUArch.PASCAL)
    assert b.tier is ComputeTier.B and b.transcription_local and not b.vlm_local
    # brak GPU -> Tier C: chmura.
    c = classify(has_cuda=False, vram_gb=0)
    assert c.tier is ComputeTier.C and not c.transcription_local


def test_provider_registry_vision_warning() -> None:
    reg = ProviderRegistry(
        assignments={Task.SLIDES_VLM: ModelSpec(Provider.DEEPSEEK, "deepseek-chat")}
    )
    # Model bez vision przypisany do zadania VLM -> ostrzeżenie.
    assert reg.validate()
