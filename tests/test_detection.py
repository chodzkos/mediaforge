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


def test_render_report_shows_summary_and_notes_models() -> None:
    """Doctor wypisuje skonfigurowane modele streszczeń ORAZ notatek VLM (lokalny/chmurowy)."""
    rep = detection.check_all(
        summary_model_local="ollama/qwen3:27b",
        vlm_model_local="ollama/qwen-vl-local",
        vlm_model_cloud="gemini/gemini-vision",
    )
    text = report.render_report(rep)
    assert "streszczenia: lokalny ollama/qwen3:27b" in text
    assert "notatki: VLM lokalny ollama/qwen-vl-local · chmura gemini/gemini-vision" in text


def test_render_report_notes_without_models_shows_local_only() -> None:
    """Brak modeli VLM → linia notatek pokazuje „— (tylko lokalnie)" (jak streszczenia)."""
    text = report.render_report(detection.check_all())
    assert "notatki: VLM lokalny — · chmura — (tylko lokalnie)" in text


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


def test_encoder_probe_cmd_matches_recording_pipeline() -> None:
    """Strażnik kontraktu sonda↔pipeline: 640x360 (min NVENC) + format=yuv420p (jak realny -vf)."""
    cmd = tools._encoder_probe_cmd("h264_nvenc", "ffmpeg")
    src = cmd[cmd.index("-i") + 1]
    assert "size=640x360" in src  # fix #1: 64x64 to fałszywy negatyw NVENC
    # fix #2: testsrc jest RGB → bez format=yuv420p konwersja do YUV444 wywalała av1_nvenc.
    assert cmd[cmd.index("-vf") + 1] == "format=yuv420p"
    assert cmd[cmd.index("-c:v") + 1] == "h264_nvenc"


def test_stderr_tail_surfaces_real_cause_over_generic() -> None:
    """Ogon pomija „Conversion failed!" i pokazuje właściwą przyczynę wyżej („YUV444P…")."""
    stderr = (
        "[av1_nvenc @ 0x1] Provided YUV444P not supported\n"
        "[av1_nvenc @ 0x1] No capable devices found\n"
        "Error submitting a packet to the muxer\n"
        "Conversion failed!\n"
    )
    tail = tools._stderr_tail(stderr)
    assert "YUV444P not supported" in tail
    assert "No capable devices found" in tail


def test_stderr_tail_falls_back_to_raw_when_all_generic() -> None:
    """Same ogólniki → mniej niż 2 linie sensowne → surowe ostatnie linie (nie pusto)."""
    stderr = "Error submitting a packet to the muxer\nConversion failed!\n"
    tail = tools._stderr_tail(stderr)
    assert "Conversion failed!" in tail  # nie gubimy wszystkiego, gdy nic „sensownego" nie zostało


def test_probe_encoder_result_ok_ignores_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    """rc=0 → available=True nawet ze stderr (warning ≠ awaria); brak stderr_tail przy sukcesie."""
    monkeypatch.setattr(
        tools, "_run_capture", lambda cmd, timeout: (0, "[libx264] using SAR 1:1\n")
    )
    tools.probe_encoder_result.cache_clear()
    res = tools.probe_encoder_result("libx264")
    assert res.available is True and res.stderr_tail == ""
    tools.probe_encoder_result.cache_clear()


def test_probe_encoder_result_failure_captures_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    """rc≠0 → available=False + ogon stderr (realny powód do doctora zamiast zgadywania)."""
    stderr = "some earlier line\n[h264_nvenc] Unable to parse preset option value p5\n"
    monkeypatch.setattr(tools, "_run_capture", lambda cmd, timeout: (1, stderr))
    tools.probe_encoder_result.cache_clear()
    res = tools.probe_encoder_result("h264_nvenc")
    assert res.available is False
    assert "Unable to parse preset" in res.stderr_tail
    tools.probe_encoder_result.cache_clear()


def test_probe_encoder_result_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drugi odczyt NIE odpala procesu (lru_cache) — licznik atrapy."""
    calls = {"n": 0}

    def _fake(cmd: list[str], timeout: int) -> tuple[int, str]:
        calls["n"] += 1
        return 0, ""

    monkeypatch.setattr(tools, "_run_capture", _fake)
    tools.probe_encoder_result.cache_clear()
    tools.probe_encoder_result("libx264")
    tools.probe_encoder_result("libx264")
    assert calls["n"] == 1
    tools.probe_encoder_result.cache_clear()


def test_probe_encoder_bool_wrapper(monkeypatch: pytest.MonkeyPatch) -> None:
    """probe_encoder (bool) = podsumowanie probe_encoder_result; komenda niesie żądany enkoder."""
    captured: list[list[str]] = []

    def _fake(cmd: list[str], timeout: int) -> tuple[int, str]:
        captured.append(cmd)
        return (0, "") if cmd[cmd.index("-c:v") + 1] == "libx264" else (-40, "boom")

    monkeypatch.setattr(tools, "_run_capture", _fake)
    tools.probe_encoder_result.cache_clear()
    assert tools.probe_encoder("libx264") is True
    assert tools.probe_encoder("hevc_nvenc") is False
    assert captured[0][captured[0].index("-c:v") + 1] == "libx264"
    tools.probe_encoder_result.cache_clear()


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
    # Wstrzyknięty bool-probe nie niesie stderr → brak wpisów w encoder_probe_errors.
    assert result["encoder_probe_errors"] == {}


def test_check_ffmpeg_collects_probe_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    """Realna ścieżka sondy: martwy enkoder trafia do encoder_probe_errors z ogonem stderr."""
    monkeypatch.setattr(
        tools,
        "probe_tool",
        lambda *a, **k: {"available": True, "version": "8.1", "path": Path("ffmpeg")},
    )
    monkeypatch.setattr(tools, "_run", lambda *a, **k: "V..... hevc_nvenc x\nV..... libx264 y\n")

    def _cap(cmd: list[str], timeout: int) -> tuple[int, str]:
        return (
            (0, "") if cmd[cmd.index("-c:v") + 1] == "libx264" else (1, "init failed: bad thing\n")
        )

    monkeypatch.setattr(tools, "_run_capture", _cap)
    tools.probe_encoder_result.cache_clear()

    result = tools.check_ffmpeg(probe_encoders=True)  # realna sonda (probe=None)

    assert result["encoders_usable"] == {"hevc_nvenc": False, "libx264": True}
    assert "init failed: bad thing" in result["encoder_probe_errors"]["hevc_nvenc"]
    assert "libx264" not in result["encoder_probe_errors"]  # OK → brak wpisu
    tools.probe_encoder_result.cache_clear()


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
    """NVENC martwy + GPU Pascal (cc < 7.5, sterownik < 610) → dopisek o FFmpeg 7.x release."""
    rep = {
        "ffmpeg": {
            "available": True,
            "version": "8.1",
            "encoders": {"hevc_nvenc": True},
            "encoders_usable": {"hevc_nvenc": False},
        },
        # Pascal jest EOL — max sterownik ~580, więc < 610 (jak GTX 1070 z audytu: 582.66).
        "gpu": {
            "available": True,
            "name": "GTX 1070",
            "compute_cap": "6.1",
            "arch": "pascal",
            "driver": "582.66",
        },
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
        "gpu": {
            "available": True,
            "name": "GTX 1070",
            "compute_cap": "",
            "arch": "pascal",
            "driver": "580.0",
        },
    }
    assert "Pascal: sterownik ≥610 nie istnieje" in report.render_report(rep)


def test_render_report_driver_hint_only_below_610() -> None:
    """Hint „≥610" TYLKO gdy sterownik faktycznie za stary. 610.62 → brak; 555 → obecny."""
    base_ff = {
        "available": True,
        "encoders": {"hevc_nvenc": True},
        "encoders_usable": {"hevc_nvenc": False},
    }
    # 610.62 (RTX 5090) — sterownik OK: hint sterownikowy NIE może się pojawić (dawniej kłamał).
    ok = report.render_report(
        {"ffmpeg": base_ff, "gpu": {"available": True, "name": "RTX 5090", "driver": "610.62"}}
    )
    assert "nie działa w runtime" in ok
    assert "≥610" not in ok and "Pascal" not in ok
    # 555 na nowoczesnym GPU (cc ≥ 7.5) — za stary sterownik: hint obecny, ale bez dopisku Pascala.
    old = report.render_report(
        {
            "ffmpeg": base_ff,
            "gpu": {"available": True, "name": "RTX 3060", "compute_cap": "8.6", "driver": "555"},
        }
    )
    assert "wymaga sterownika NVIDIA ≥610" in old
    assert "Pascal" not in old


def test_render_report_shows_probe_stderr() -> None:
    """Doctor przy „✗" pokazuje ogon stderr sondy (realny powód, nie zgadywanie)."""
    rep = {
        "ffmpeg": {
            "available": True,
            "encoders": {"hevc_nvenc": True},
            "encoders_usable": {"hevc_nvenc": False},
            "encoder_probe_errors": {"hevc_nvenc": "Cannot load nvcuda.dll"},
        },
        "gpu": {"available": True, "name": "RTX 5090", "driver": "610.62"},
    }
    assert "Cannot load nvcuda.dll" in report.render_report(rep)
