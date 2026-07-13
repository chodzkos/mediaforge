"""Testy warstwy detekcji: kontrakt probe_tool, override whisper.cpp, arch, raport."""

import shutil
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
    # Override istniejącej ścieżki → available + path (gałąź override, bez PATH).
    existing = __file__  # dowolny istniejący plik
    wh = tools.check_whispercpp(override_path=existing)
    assert wh["available"] is True
    assert wh["path"] == Path(existing)
    assert set(wh) >= {"available", "version", "path"}
    # Hermetycznie: brak czegokolwiek w PATH → fallback deterministyczny niezależnie od OS/CI
    # (na Windows generyczne nazwy mogłyby się rozwiązać do przypadkowej binarki).
    # Patchujemy współdzielony moduł shutil (tools.py używa tego samego obiektu) — bez
    # sięgania po nie-eksportowany tools.shutil (no_implicit_reexport pod mypy --strict).
    monkeypatch.setattr(shutil, "which", lambda _cmd: None)
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


def test_check_providers_matches_set_provider_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kontrakt: zapis i odczyt klucza dostawcy idą przez ten sam name-builder (nie rozjazd nazw).

    Regresja na buga ``api_key_<p>`` (check_providers) vs ``api_key:<p>`` (secrets). Keyring
    in-memory: gdyby strony budowały nazwę inaczej, odczyt nie znalazłby zapisanego klucza.
    """
    import keyring

    from mediaforge.core.secrets import set_provider_api_key

    store: dict[tuple[str, str], str] = {}

    def _get(service: str, key: str) -> str | None:
        return store.get((service, key))

    def _set(service: str, key: str, value: str) -> None:
        store[(service, key)] = value

    monkeypatch.setattr(keyring, "get_password", _get)
    monkeypatch.setattr(keyring, "set_password", _set)

    set_provider_api_key("openai", "x")
    result = tools.check_providers()
    assert result["openai"] is True  # zapis widoczny w odczycie → spójny name-builder
    assert result["anthropic"] is False  # bez klucza → False (kontrola negatywna)


def test_render_report_text() -> None:
    text = report.render_report(detection.check_all())
    assert isinstance(text, str)
    assert "System:" in text and "Tier" in text and "yt-dlp:" in text


def _report_with_whisper(*, model_set: bool, runtime: str) -> dict[str, object]:
    return {
        "whispercpp": {"available": True, "path": "/usr/bin/whisper-cli"},
        "compute": {"whisper_model_set": model_set, "whisper_runtime": runtime, "tier": "A"},
    }


def test_render_whisper_model_not_set() -> None:
    text = report.render_report(_report_with_whisper(model_set=False, runtime="unknown"))
    assert "model nieustawiony" in text


def test_render_whisper_runtime_cuda() -> None:
    text = report.render_report(_report_with_whisper(model_set=True, runtime="cuda"))
    assert "runtime: CUDA" in text


def test_check_all_no_probe_keeps_runtime_unknown() -> None:
    # Bez probe_whisper sonda nie biegnie (status bar nie zamarza) → runtime 'unknown'.
    rep = detection.check_all(whisper_model="/m/x.bin")
    assert rep["compute"]["whisper_runtime"] == "unknown"
    assert rep["compute"]["whisper_cuda_ok"] is False


def test_status_line_from_report() -> None:
    # Pasek statusu czyta te same DANE co doctor (check_all) — krótka prezentacja.
    rep = {
        "ffmpeg": {"available": True},
        "whispercpp": {"available": False},
        "gpu": {"available": True, "name": "RTX 5090", "vram_gb": 24.0},
        "compute": {"tier": "A"},
    }
    line = report.status_line(rep)
    assert "FFmpeg: OK" in line
    assert "whisper.cpp: brak" in line
    assert "RTX 5090" in line
    assert "Tier: A" in line


def test_status_line_no_cuda() -> None:
    line = report.status_line({"gpu": {"available": False}, "compute": {"tier": "C"}})
    assert "CUDA: brak" in line
    assert "Tier: C" in line


# ── M21: sonda używalności enkoderów w runtime (nie tylko obecność w buildzie) ─────


def test_probe_encoder_reads_runtime_returncode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sonda rozstrzyga po kodzie wyjścia realnego kodowania testsrc, nie po liście buildu."""
    calls: list[list[str]] = []

    def _rc(cmd: list[str], timeout: int) -> int:
        calls.append(cmd)
        return 0

    monkeypatch.setattr(tools, "_run_returncode", _rc)
    tools.probe_encoder.cache_clear()
    assert tools.probe_encoder("libx264") is True
    # Komenda to jednoklatkowe kodowanie testsrc z żądanym enkoderem do null.
    assert calls and calls[0][calls[0].index("-c:v") + 1] == "libx264"

    monkeypatch.setattr(tools, "_run_returncode", lambda cmd, timeout: -40)
    tools.probe_encoder.cache_clear()
    assert tools.probe_encoder("hevc_nvenc") is False
    tools.probe_encoder.cache_clear()  # nie zostawiaj zaślepki w cache dla innych testów


def test_check_ffmpeg_usable_probes_only_build_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """encoders_usable powstaje z sondy runtime TYLKO dla enkoderów obecnych w buildzie."""
    monkeypatch.setattr(
        tools,
        "probe_tool",
        lambda *a, **k: {"available": True, "version": "8.1", "path": Path("ffmpeg")},
    )
    # Listing -encoders: hevc_nvenc + libx264 obecne; h264_nvenc/av1_nvenc/libx265 nieobecne.
    monkeypatch.setattr(tools, "_run", lambda *a, **k: "V..... hevc_nvenc x\nV..... libx264 y\n")
    probed: list[str] = []

    def _probe(name: str) -> bool:
        probed.append(name)
        return name == "libx264"  # NVENC w buildzie, ale martwy w runtime

    result = tools.check_ffmpeg(probe_encoders=True, probe=_probe)

    assert result["encoders"]["hevc_nvenc"] is True  # build: obecny
    assert result["encoders"]["av1_nvenc"] is False  # build: brak
    assert set(probed) == {"hevc_nvenc", "libx264"}  # sonda TYLKO dla obecnych w buildzie
    assert set(result["encoders_usable"]) == {"hevc_nvenc", "libx264"}
    assert result["encoders_usable"]["hevc_nvenc"] is False  # runtime: martwy → nie do wyboru
    assert result["encoders_usable"]["libx264"] is True


def test_check_ffmpeg_probes_amf_and_qsv(monkeypatch: pytest.MonkeyPatch) -> None:
    """AMF (Radeon/APU) i QSV (Intel) sondowane tą samą sondą runtime co NVENC — droga dla 780M."""
    monkeypatch.setattr(
        tools,
        "probe_tool",
        lambda *a, **k: {"available": True, "version": "8.1", "path": Path("ffmpeg")},
    )
    # Build: h264_amf + libx264 obecne (typowy Radeon 780M); NVENC/QSV nieobecne.
    monkeypatch.setattr(tools, "_run", lambda *a, **k: "V..... h264_amf x\nV..... libx264 y\n")
    probed: list[str] = []

    def _probe(name: str) -> bool:
        probed.append(name)
        return True  # AMF żyje w runtime (spodziewany h264_amf ✓)

    result = tools.check_ffmpeg(probe_encoders=True, probe=_probe)
    assert result["encoders"]["h264_amf"] is True  # klucz AMF w mapie
    assert result["encoders"]["h264_qsv"] is False  # QSV nieobecny w tym buildzie
    assert set(probed) == {"h264_amf", "libx264"}  # sonda TYLKO dla obecnych w buildzie
    assert result["encoders_usable"]["h264_amf"] is True


def test_check_ffmpeg_without_probe_leaves_usable_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bez probe_encoders sonda runtime nie biegnie (status bar/testy nie ruszają ffmpeg)."""
    monkeypatch.setattr(
        tools,
        "probe_tool",
        lambda *a, **k: {"available": True, "version": "8.1", "path": Path("ffmpeg")},
    )
    monkeypatch.setattr(tools, "_run", lambda *a, **k: "V..... libx264 y\n")

    def _boom(name: str) -> bool:
        raise AssertionError("sonda nie powinna ruszyć bez probe_encoders")

    result = tools.check_ffmpeg(probe=_boom)  # probe_encoders domyślnie False
    assert result["encoders"]["libx264"] is True
    assert result["encoders_usable"] == {}


def test_render_report_marks_build_but_unusable_encoder() -> None:
    """Enkoder w buildzie a martwy w runtime → ✗ + hint runtime; usable=True → ✓."""
    rep = {
        "ffmpeg": {
            "available": True,
            "version": "8.1",
            "encoders": {"hevc_nvenc": True, "libx264": True},
            "encoders_usable": {"hevc_nvenc": False, "libx264": True},
        },
    }
    text = report.render_report(rep)
    assert "hevc_nvenc ✗" in text
    assert "libx264 ✓" in text
    assert "nie działa w runtime" in text  # rozróżnienie build vs runtime w doktorze


def test_render_report_pascal_nvenc_hint() -> None:
    """NVENC martwy + GPU Pascal (cc < 7.5) → dopisek o FFmpeg 7.x release zamiast 8.x/git."""
    rep = {
        "ffmpeg": {
            "available": True,
            "version": "8.1",
            "encoders": {"hevc_nvenc": True},
            "encoders_usable": {"hevc_nvenc": False},
        },
        "gpu": {"available": True, "name": "GTX 1070", "compute_cap": "6.1", "arch": "pascal"},
    }
    text = report.render_report(rep)
    assert "hevc_nvenc ✗" in text
    assert "Pascal: sterownik ≥610 nie istnieje" in text
    assert "FFmpeg 7.x RELEASE" in text


def test_render_report_pascal_hint_from_arch_without_compute_cap() -> None:
    """Stary sterownik bez compute_cap → ocena po arch (pascal) i tak dopisuje hint Pascala."""
    rep = {
        "ffmpeg": {
            "available": True,
            "encoders": {"hevc_nvenc": True},
            "encoders_usable": {"hevc_nvenc": False},
        },
        "gpu": {"available": True, "name": "GTX 1070", "compute_cap": "", "arch": "pascal"},
    }
    assert "Pascal: sterownik ≥610 nie istnieje" in report.render_report(rep)


def test_render_report_no_pascal_hint_on_modern_gpu() -> None:
    """Nowoczesny GPU (cc ≥ 7.5) z martwym NVENC → hint sterownika BEZ dopisku Pascala."""
    rep = {
        "ffmpeg": {
            "available": True,
            "encoders": {"hevc_nvenc": True},
            "encoders_usable": {"hevc_nvenc": False},
        },
        "gpu": {"available": True, "name": "RTX 5090", "compute_cap": "12.0", "arch": "blackwell"},
    }
    text = report.render_report(rep)
    assert "nie działa w runtime" in text
    assert "Pascal" not in text
