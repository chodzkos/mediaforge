"""Testy doctora — odporność, mapowanie/fallback architektury, rozdział warstw."""

from mediaforge.core import dependencies as dep
from mediaforge.core.compute import GPUArch


def test_check_all_is_resilient() -> None:
    report = dep.check_all()
    for key in ("system", "ffmpeg", "whispercpp", "gpu", "compute", "litellm", "providers"):
        assert key in report
    assert isinstance(report["ffmpeg"]["available"], bool)
    assert isinstance(report["ffmpeg"]["encoders"], dict)
    assert isinstance(report["gpu"]["available"], bool)
    assert "arch" in report["gpu"]  # check_all wzbogaca o arch
    assert report["compute"]["tier"] in {"A", "B", "C"}


def test_detect_arch_from_compute_cap() -> None:
    assert dep.detect_arch("12.0") is GPUArch.BLACKWELL  # RTX 5090 / sm_120
    assert dep.detect_arch("8.9") is GPUArch.ADA
    assert dep.detect_arch("8.6") is GPUArch.AMPERE
    assert dep.detect_arch("7.5") is GPUArch.TURING
    assert dep.detect_arch("6.1") is GPUArch.PASCAL  # GTX 1070
    assert dep.detect_arch("") is GPUArch.UNKNOWN


def test_arch_from_name_fallback() -> None:
    # Fallback gdy compute_cap niedostępne (starsze nvidia-smi).
    assert dep.arch_from_name("NVIDIA GeForce RTX 5090 Laptop GPU") is GPUArch.BLACKWELL
    assert dep.arch_from_name("NVIDIA GeForce RTX 4090") is GPUArch.ADA
    assert dep.arch_from_name("NVIDIA GeForce RTX 3090") is GPUArch.AMPERE
    assert dep.arch_from_name("NVIDIA GeForce RTX 2080 Ti") is GPUArch.TURING
    assert dep.arch_from_name("NVIDIA GeForce GTX 1660") is GPUArch.TURING  # 16xx = Turing
    assert dep.arch_from_name("NVIDIA GeForce GTX 1070") is GPUArch.PASCAL  # 10xx = Pascal
    assert dep.arch_from_name("NVIDIA A100") is GPUArch.AMPERE
    assert dep.arch_from_name("Intel Arc A770") is GPUArch.UNKNOWN


def test_resolved_arch_prefers_compute_cap_then_name() -> None:
    # compute_cap ma pierwszeństwo...
    assert dep.resolved_arch({"compute_cap": "12.0", "name": "cokolwiek"}) is GPUArch.BLACKWELL
    # ...a gdy go brak, fallback z nazwy.
    assert dep.resolved_arch({"compute_cap": "", "name": "RTX 5090"}) is GPUArch.BLACKWELL
    assert dep.resolved_arch({"compute_cap": "", "name": "nieznane"}) is GPUArch.UNKNOWN


def test_check_gpu_is_raw_no_arch() -> None:
    # Granica ekstrakcji: surowe check_gpu NIE zawiera interpretacji arch.
    gpu = dep.check_gpu()
    assert "arch" not in gpu  # arch dokłada dopiero mediaforge w check_all
    assert "compute_cap" in gpu and "name" in gpu and "vram_gb" in gpu


def test_universal_probes_return_bool() -> None:
    assert dep.command_in_path("definitely-not-a-real-binary-xyz") is False
    assert isinstance(dep.api_key_present("mediaforge", "api_key_nonexistent"), bool)


def test_providers_are_booleans_only() -> None:
    providers = dep.check_providers()
    assert set(providers) == {"anthropic", "openai", "gemini", "deepseek"}
    assert all(isinstance(v, bool) for v in providers.values())


def test_render_report_is_text_and_decoupled() -> None:
    text = dep.render_report(dep.check_all())
    assert isinstance(text, str)
    assert "System:" in text and "Tier" in text


def test_ytdlp_and_whisper_placeholder_present() -> None:
    report = dep.check_all()
    assert "ytdlp" in report
    assert isinstance(report["ytdlp"]["available"], bool)
    # placeholder whisper_cuda_ok jest w sekcji compute (zastąpiony realną sondą w S3)
    assert "whisper_cuda_ok" in report["compute"]
    assert isinstance(report["compute"]["whisper_cuda_ok"], bool)
    assert isinstance(dep.whisper_cuda_ok(), bool)
