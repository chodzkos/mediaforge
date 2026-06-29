"""Testy warstwy detekcji: kontrakt probe_tool, override whisper.cpp, arch, raport."""

from pathlib import Path

import pytest

from mediaforge.core import detection
from mediaforge.core.compute import GPUArch
from mediaforge.core.detection import hardware, report, tools


def test_check_all_is_resilient() -> None:
    rep = detection.check_all()
    keys = ("system", "ffmpeg", "whispercpp", "ytdlp", "gpu", "compute", "litellm", "providers")
    for key in keys:
        assert key in rep
    assert isinstance(rep["ffmpeg"]["available"], bool)
    assert isinstance(rep["ffmpeg"]["encoders"], dict)  # warstwa mediaforge obok probe_tool
    assert "arch" in rep["gpu"]
    assert rep["compute"]["tier"] in {"A", "B", "C"}
    assert "whisper_cuda_ok" in rep["compute"]  # placeholder do S3


def test_probe_tool_contract_has_path() -> None:
    # Kontrakt = nadzbiór pakietu: {available, version, path}.
    t = tools.probe_tool("definitely-not-a-real-binary-xyz")
    assert set(t) == {"available", "version", "path"}
    assert t["available"] is False
    assert t["path"] is None
    # Realne narzędzie obecne w sandboxie (ffmpeg) — path to Path.
    ff = tools.probe_tool("ffmpeg")
    if ff["available"]:
        assert isinstance(ff["path"], Path)


def test_whispercpp_override_used_first(monkeypatch: pytest.MonkeyPatch) -> None:
    # Brak whisper.cpp w PATH — fallback deterministyczny niezależnie od środowiska CI
    # (kandydat "main" bywa obecny na PATH np. na runnerach Windows → mockujemy which).
    monkeypatch.setattr(tools.shutil, "which", lambda _cmd: None)
    # Override istniejącej ścieżki → available + path (gałąź override, omija PATH).
    existing = __file__  # dowolny istniejący plik
    wh = tools.check_whispercpp(override_path=existing)
    assert wh["available"] is True
    assert wh["path"] == Path(existing)
    assert set(wh) >= {"available", "version", "path"}
    # Nieistniejąca ścieżka override → fallback do which (zamockowany brak → niedostępne).
    wh2 = tools.check_whispercpp(override_path="/no/such/whispercpp/binary")
    assert wh2["available"] is False


def test_arch_detection_and_fallback() -> None:
    assert hardware.detect_arch("12.0") is GPUArch.BLACKWELL
    assert hardware.detect_arch("6.1") is GPUArch.PASCAL
    assert hardware.detect_arch("") is GPUArch.UNKNOWN
    assert hardware.arch_from_name("NVIDIA GeForce RTX 5090 Laptop GPU") is GPUArch.BLACKWELL
    assert hardware.arch_from_name("NVIDIA GeForce GTX 1070") is GPUArch.PASCAL
    assert hardware.resolved_arch({"compute_cap": "", "name": "RTX 5090"}) is GPUArch.BLACKWELL


def test_check_gpu_is_raw_no_arch() -> None:
    gpu = hardware.check_gpu()
    assert "arch" not in gpu  # surowa sonda generyczna — arch dokłada dopiero report
    assert {"name", "vram_gb", "compute_cap"} <= set(gpu)


def test_providers_are_booleans_only() -> None:
    providers = tools.check_providers()
    assert set(providers) == {"anthropic", "openai", "gemini", "deepseek"}
    assert all(isinstance(v, bool) for v in providers.values())


def test_render_report_text() -> None:
    text = report.render_report(detection.check_all())
    assert isinstance(text, str)
    assert "System:" in text and "Tier" in text and "yt-dlp:" in text
