"""Testy budowania komend FFmpeg dla nagrywania (czysta logika + smoke z realnym ffmpeg)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from mediaforge.core.engines.base import QualityOption
from mediaforge.core.engines.ffmpeg_cmd import (
    PRESETS,
    AudioConfig,
    CaptureMode,
    CaptureSource,
    EncoderChoice,
    build_record_command,
    build_video_filter,
    encoder_label,
    encoder_quality_args,
    estimate_size_mb,
    resolve_encoder,
    select_video_encoder,
)


def _has_substr(cmd: list[str], needle: str) -> bool:
    return any(needle in arg for arg in cmd)


_ALL_ENCODERS = {
    "h264_nvenc": True,
    "hevc_nvenc": True,
    "av1_nvenc": True,
    "libx264": True,
    "libx265": True,
}
_NO_NVENC = {
    "h264_nvenc": False,
    "hevc_nvenc": False,
    "av1_nvenc": False,
    "libx264": True,
    "libx265": True,
}
# NVENC martwy, ale AMF (Radeon) żyje — tor sprzętowy nie jest wyczerpany, więc NIE schodzimy
# na software. Radeon 780M: h264_amf usable (hevc_amf martwy) → spodziewany h264_amf, nie libx265.
_AMF_ONLY = {
    "h264_nvenc": False,
    "hevc_nvenc": False,
    "av1_nvenc": False,
    "h264_amf": True,
    "hevc_amf": False,
    "libx264": True,
    "libx265": True,
}


def _arg_value(cmd: list[str], flag: str) -> str:
    return cmd[cmd.index(flag) + 1]


# ── Wybór enkodera (NVENC + fallback programowy) ───────────────────────────────


def test_encoder_prefers_nvenc_when_available() -> None:
    choice = select_video_encoder("hevc", _ALL_ENCODERS)
    assert choice.name == "hevc_nvenc"
    assert choice.hardware is True


def test_encoder_full_nvenc_h264_chain() -> None:
    choice = select_video_encoder("h264", _ALL_ENCODERS)
    assert choice.name == "h264_nvenc"
    assert choice.hardware is True


def test_encoder_dead_nvenc_falls_to_amf_not_libx265() -> None:
    # NVENC martwy + AMF żywy → h264_amf (sprzęt niewyczerpany). NIGDY libx265 (za wolny na CPU).
    choice = select_video_encoder("hevc", _AMF_ONLY)
    assert choice.name == "h264_amf"
    assert choice.hardware is True


def test_encoder_software_only_is_libx264() -> None:
    # Cały sprzęt martwy → ostateczny software-fallback to WYŁĄCZNIE libx264 (nie libx265).
    choice = select_video_encoder("hevc", _NO_NVENC)
    assert choice.name == "libx264"
    assert choice.hardware is False


def test_encoder_av1_exhausts_hardware_before_software() -> None:
    # Brak av1 sprzętowego → schodzimy po torze sprzętowym (nvenc/amf/qsv), a nie na software-AV1.
    choice = select_video_encoder("av1", _AMF_ONLY)
    assert choice.name == "h264_amf"
    assert choice.hardware is True
    # Bez żadnego sprzętu av1 kończy na libx264 (nie libsvtav1/libaom/libx265).
    assert select_video_encoder("av1", _NO_NVENC).name == "libx264"


def test_encoder_libx265_never_selected_for_recording() -> None:
    # Nawet gdy w buildzie zostaje SAM libx265 (dziwny build), wybór go pomija → libx264 nominalnie.
    for codec in ("hevc", "h264", "av1"):
        choice = select_video_encoder(codec, {"libx265": True})
        assert choice.name != "libx265"
        assert choice.name == "libx264"


def test_encoder_last_resort_is_libx264() -> None:
    choice = select_video_encoder("hevc", {"hevc_nvenc": False, "libx265": False})
    assert choice.name == "libx264"


def test_encoder_label_marks_gpu_vs_cpu_degradation() -> None:
    assert encoder_label(EncoderChoice("hevc_nvenc", True)) == "hevc_nvenc (GPU)"
    soft = encoder_label(EncoderChoice("libx264", False))
    assert soft == "libx264 (CPU) — ograniczono do 30 fps / 1080p"


def test_resolve_encoder_none_for_audio_only() -> None:
    assert resolve_encoder(PRESETS["audio_only"], _ALL_ENCODERS) is None
    assert resolve_encoder(PRESETS["standard"], _ALL_ENCODERS) == EncoderChoice("hevc_nvenc", True)


# ── Flagi jakości PER RODZINA enkodera (bug: p5/-tune NVENC doklejane wszystkim) ─


def test_encoder_quality_args_per_family() -> None:
    # NVENC: presety p1-p7 istnieją tylko tu.
    assert encoder_quality_args("h264_nvenc") == ["-preset", "p5", "-tune", "hq"]
    assert encoder_quality_args("hevc_nvenc") == ["-preset", "p5", "-tune", "hq"]
    # QSV: nazwy jak x264, BEZ p5 i BEZ -tune.
    qsv = encoder_quality_args("h264_qsv")
    assert qsv == ["-preset", "veryfast"] and "p5" not in qsv and "-tune" not in qsv
    # AMF: BEZ -preset (ma -quality/-usage).
    amf = encoder_quality_args("h264_amf")
    assert amf == ["-quality", "balanced"] and "-preset" not in amf
    # libx264: veryfast + crf.
    assert encoder_quality_args("libx264") == ["-preset", "veryfast", "-crf", "23"]
    # Nieznana rodzina → brak flag jakości (domyślne enkodera).
    assert encoder_quality_args("libx265") == []


def _build_with_encoders(encoders: dict[str, bool]) -> list[str]:
    return build_record_command(
        source=CaptureSource(),
        audio=AudioConfig(system_audio=False),
        quality=PRESETS["standard"],
        encoders=encoders,
        segment_pattern="/tmp/seg_%03d.mkv",
    )


def test_record_command_qsv_has_no_nvenc_preset() -> None:
    """Regresja: fallback na h264_qsv NIE dostaje p5/-tune (inaczej ffmpeg nie startuje)."""
    cmd = _build_with_encoders({"h264_qsv": True})
    assert _arg_value(cmd, "-c:v") == "h264_qsv"
    assert "p5" not in cmd and "-tune" not in cmd


def test_record_command_amf_has_no_preset() -> None:
    cmd = _build_with_encoders({"h264_amf": True})
    assert _arg_value(cmd, "-c:v") == "h264_amf"
    assert "-preset" not in cmd
    assert _arg_value(cmd, "-quality") == "balanced"


def test_record_command_nvenc_keeps_p5_hq() -> None:
    """Regresja 5090: NVENC nadal dostaje p5 + hq."""
    cmd = _build_with_encoders({"hevc_nvenc": True})
    assert _arg_value(cmd, "-c:v") == "hevc_nvenc"
    assert _arg_value(cmd, "-preset") == "p5"
    assert _arg_value(cmd, "-tune") == "hq"


def test_record_command_libx264_has_veryfast_crf() -> None:
    cmd = _build_with_encoders(_NO_NVENC)
    assert _arg_value(cmd, "-c:v") == "libx264"
    assert _arg_value(cmd, "-preset") == "veryfast"
    assert _arg_value(cmd, "-crf") == "23"


def test_encoder_quality_smoke_libx264(tmp_path: Path) -> None:
    """Smoke: flagi jakości libx264 REALNIE parsują się w ffmpeg (1 klatka testsrc → null).

    Pomijane, gdy brak ffmpeg (CI Windows/kontener zwykle bez binarki) — łapie regresję flag
    tam, gdzie ffmpeg jest. NVENC/QSV/AMF wymagają sprzętu → poza zakresem sondy na CI.
    """
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("brak ffmpeg w PATH")
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-f",
        "lavfi",
        "-i",
        "testsrc=duration=0.1:size=64x64:rate=30",
        "-frames:v",
        "1",
        "-c:v",
        "libx264",
        *encoder_quality_args("libx264"),
        "-f",
        "null",
        "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr[-500:]


# ── Budowa komendy: tryby źródła ───────────────────────────────────────────────


def test_fullscreen_uses_ddagrab_output() -> None:
    cmd = build_record_command(
        source=CaptureSource(mode=CaptureMode.FULLSCREEN),
        audio=AudioConfig(system_audio=False),
        quality=PRESETS["standard"],
        encoders=_ALL_ENCODERS,
        segment_pattern="/tmp/seg_%03d.mkv",
    )
    assert "gdigrab" not in cmd  # GDI (gubił klatki) zastąpiony przez Desktop Duplication
    assert _arg_value(cmd, "-f") == "lavfi"
    assert _has_substr(cmd, "ddagrab=output_idx=0:framerate=60")  # min 60 fps (standard=30→60)
    # Wspólny zegar + większa kolejka na wejściu wideo (sync A/V, brak głodzenia strumieni).
    i = cmd.index("-f")  # pierwsze -f = wejście wideo (lavfi)
    assert cmd[:i].count("-use_wallclock_as_timestamps") == 1
    assert "-thread_queue_size" in cmd[:i]


def test_monitor_selects_output_idx() -> None:
    # Wybór monitora → output_idx ddagrab (bez region = cały monitor, bez crop).
    cmd = build_record_command(
        source=CaptureSource(mode=CaptureMode.FULLSCREEN, monitor=1),
        audio=AudioConfig(system_audio=False),
        quality=PRESETS["standard"],
        encoders=_ALL_ENCODERS,
        segment_pattern="/tmp/seg_%03d.mkv",
    )
    assert _has_substr(cmd, "ddagrab=output_idx=1:")
    assert "crop=" not in _arg_value(cmd, "-vf")  # cały monitor → bez crop
    assert "-offset_x" not in cmd and "-video_size" not in cmd


def test_region_adds_crop_to_filter() -> None:
    cmd = build_record_command(
        source=CaptureSource(mode=CaptureMode.REGION, monitor=0, region=(100, 200, 1280, 720)),
        audio=AudioConfig(system_audio=False),
        quality=PRESETS["standard"],
        encoders=_ALL_ENCODERS,
        segment_pattern="/tmp/seg_%03d.mkv",
    )
    assert "crop=1280:720:100:200" in _arg_value(cmd, "-vf")


# ── Filtr wideo (hwdownload + warunkowy crop) ──────────────────────────────────


def test_filter_no_region_has_no_crop() -> None:
    vf = build_video_filter(None)
    assert vf == "hwdownload,format=bgra,format=yuv420p"


def test_filter_region_inserts_crop_between_formats() -> None:
    vf = build_video_filter((10, 20, 800, 600))
    assert vf == "hwdownload,format=bgra,crop=800:600:10:20,format=yuv420p"
    # crop MIĘDZY format=bgra a format=yuv420p (po pobraniu z GPU, przed yuv420p).
    assert vf.index("format=bgra") < vf.index("crop=") < vf.index("format=yuv420p")


def test_filter_rounds_odd_dimensions_down() -> None:
    # yuv420p wymaga parzystych w/h → 1281x721 → 1280x720 (offset też parzysty).
    assert "crop=1280:720:0:0" in build_video_filter((1, 1, 1281, 721))


def test_filter_degenerate_region_raises() -> None:
    with pytest.raises(ValueError, match="niepoprawny"):
        build_video_filter((0, 0, 100, 1))  # h=1 → po _even h=0


def test_filter_never_has_trim() -> None:
    # Regresja: trim samego wideo rozjeżdżał A/V — filtr NIGDY nie tnie głowy (robi to pre-roll UX).
    assert "trim" not in build_video_filter(None)
    assert "trim" not in build_video_filter((10, 20, 800, 600))


def test_record_command_vf_has_no_trim() -> None:
    cmd = build_record_command(
        source=CaptureSource(mode=CaptureMode.FULLSCREEN),
        audio=AudioConfig(system_audio=False),
        quality=PRESETS["standard"],
        encoders=_ALL_ENCODERS,
        segment_pattern="/tmp/seg_%03d.mkv",
    )
    vf = _arg_value(cmd, "-vf")
    assert "trim" not in vf and "setpts" not in vf


def test_filter_software_scale_after_crop() -> None:
    # Software: scale ≤1920 PO crop, przed yuv420p; przecinek w min() chroniony ''.
    vf = build_video_filter((10, 20, 3840, 2160), max_width=1920)
    assert "crop=3840:2160:10:20" in vf
    assert "scale='min(1920,iw)':-2" in vf
    assert vf.index("crop=") < vf.index("scale=") < vf.index("format=yuv420p")


def test_filter_no_scale_without_max_width() -> None:
    # Sprzęt (bez max_width): filtr 1:1 jak dziś, bez scale.
    assert "scale" not in build_video_filter(None)
    assert "scale" not in build_video_filter((0, 0, 800, 600))


# ── Adaptacja software: fps ≤ 30 + scale ≤ 1920 (bez niej fallback = szarpanie) ──


def test_software_encoder_caps_fps_and_scales() -> None:
    # Tor software (tylko libx264): fps wymuszony na 30 (mimo presetu 60) + scale w -vf, veryfast.
    cmd = build_record_command(
        source=CaptureSource(mode=CaptureMode.FULLSCREEN),
        audio=AudioConfig(system_audio=False),
        quality=PRESETS["high"],  # fps=60, hevc
        encoders=_NO_NVENC,  # cały sprzęt martwy → libx264
        segment_pattern="/tmp/seg_%03d.mkv",
    )
    assert _arg_value(cmd, "-c:v") == "libx264"
    assert _has_substr(cmd, "ddagrab=output_idx=0:framerate=30")  # fps ≤ 30
    assert "scale='min(1920,iw)':-2" in _arg_value(cmd, "-vf")
    assert _arg_value(cmd, "-preset") == "veryfast"
    assert "-tune" not in cmd  # -tune hq tylko dla toru sprzętowego


def test_software_scale_after_crop_in_command() -> None:
    # crop najpierw, scale po — także w pełnej komendzie (region + software).
    cmd = build_record_command(
        source=CaptureSource(mode=CaptureMode.REGION, monitor=0, region=(0, 0, 3840, 2160)),
        audio=AudioConfig(system_audio=False),
        quality=PRESETS["high"],
        encoders=_NO_NVENC,
        segment_pattern="/tmp/seg_%03d.mkv",
    )
    vf = _arg_value(cmd, "-vf")
    assert vf.index("crop=") < vf.index("scale=")


def test_hardware_encoder_keeps_native_fps_and_no_scale() -> None:
    # Regresja 5090: sprzęt → 60 fps, bez scale w -vf, preset p5 + tune hq (zachowanie 1:1).
    cmd = build_record_command(
        source=CaptureSource(mode=CaptureMode.FULLSCREEN),
        audio=AudioConfig(system_audio=False),
        quality=PRESETS["high"],  # fps=60
        encoders=_ALL_ENCODERS,
        segment_pattern="/tmp/seg_%03d.mkv",
    )
    assert _arg_value(cmd, "-c:v") == "hevc_nvenc"
    assert _has_substr(cmd, "ddagrab=output_idx=0:framerate=60")  # natywne 60 fps
    assert "scale" not in _arg_value(cmd, "-vf")  # sprzęt → bez skalowania
    assert _arg_value(cmd, "-preset") == "p5"
    assert _arg_value(cmd, "-tune") == "hq"


def test_video_pipeline_ddagrab_nvenc_cfr() -> None:
    """Rdzeń poprawki: ddagrab + hwdownload/format + NVENC + CFR (koniec skokowości)."""
    cmd = build_record_command(
        source=CaptureSource(mode=CaptureMode.FULLSCREEN),
        audio=AudioConfig(system_audio=False),
        quality=QualityOption(label="Test", video_codec="h264", fps=60, bitrate_kbps=12000),
        encoders=_ALL_ENCODERS,
        segment_pattern="/tmp/seg_%03d.mkv",
    )
    assert _has_substr(cmd, "ddagrab=output_idx=0:framerate=60")
    vf = _arg_value(cmd, "-vf")
    assert "hwdownload" in vf and "format=bgra" in vf and "format=yuv420p" in vf
    assert _arg_value(cmd, "-c:v") == "h264_nvenc"
    assert _arg_value(cmd, "-fps_mode") == "cfr"
    assert _arg_value(cmd, "-tune") == "hq"


# ── Budowa komendy: audio ──────────────────────────────────────────────────────


def test_system_audio_adds_dshow_input() -> None:
    cmd = build_record_command(
        source=CaptureSource(),
        audio=AudioConfig(system_audio=True, system_device="Loopback (Realtek)"),
        quality=PRESETS["standard"],
        encoders=_ALL_ENCODERS,
        segment_pattern="/tmp/seg_%03d.mkv",
    )
    assert "audio=Loopback (Realtek)" in cmd
    assert _arg_value(cmd, "-c:a") == "aac"
    # dshow: wspólny wallclock + audio_buffer_size 50 (tnie 500 ms paczki → brak non-monotonic DTS).
    assert _arg_value(cmd, "-audio_buffer_size") == "50"
    assert "-use_wallclock_as_timestamps" in cmd and "-thread_queue_size" in cmd
    # pojedyncze audio: aresample=async=1 jako prosty filtr toru.
    assert _arg_value(cmd, "-af") == "aresample=async=1"


def test_dshow_input_has_wallclock_before_its_i() -> None:
    cmd = build_record_command(
        source=CaptureSource(),
        audio=AudioConfig(system_audio=True, system_device="Loopback"),
        quality=PRESETS["standard"],
        encoders=_ALL_ENCODERS,
        segment_pattern="/tmp/seg_%03d.mkv",
    )
    # Flagi wejściowe (wallclock/thread_queue/audio_buffer) PRZED swoim -i "audio=…".
    i_audio = cmd.index("audio=Loopback") - 1  # pozycja -i
    window = cmd[:i_audio]
    for flag in ("-use_wallclock_as_timestamps", "-thread_queue_size", "-audio_buffer_size"):
        assert flag in window


def test_mix_two_sources_aresample_per_input_before_amix() -> None:
    cmd = build_record_command(
        source=CaptureSource(),
        audio=AudioConfig(
            system_audio=True,
            microphone=True,
            system_device="Loopback",
            mic_device="Mic",
            mix=True,
        ),
        quality=PRESETS["standard"],
        encoders=_ALL_ENCODERS,
        segment_pattern="/tmp/seg_%03d.mkv",
    )
    fc = _arg_value(cmd, "-filter_complex")
    # aresample PER WEJŚCIE, PRZED amix.
    assert fc.count("aresample=async=1") == 2
    assert fc.index("aresample=async=1") < fc.index("amix=inputs=2")
    assert "[aout]" in cmd  # zmapowany zmiksowany ślad


def test_two_sources_without_mix_aresample_and_separate_maps() -> None:
    cmd = build_record_command(
        source=CaptureSource(),
        audio=AudioConfig(
            system_audio=True,
            microphone=True,
            system_device="Loopback",
            mic_device="Mic",
            mix=False,
        ),
        quality=PRESETS["standard"],
        encoders=_ALL_ENCODERS,
        segment_pattern="/tmp/seg_%03d.mkv",
    )
    fc = _arg_value(cmd, "-filter_complex")
    assert fc.count("aresample=async=1") == 2 and "amix" not in fc
    maps = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-map"]
    assert maps == ["0:v", "[a0]", "[a1]"]  # wideo + dwa osobne, przeresamplowane ślady


def test_audio_only_has_no_video_input() -> None:
    cmd = build_record_command(
        source=CaptureSource(),
        audio=AudioConfig(system_audio=True, system_device="Loopback"),
        quality=PRESETS["audio_only"],
        encoders=_ALL_ENCODERS,
        segment_pattern="/tmp/seg_%03d.mka",
    )
    assert not _has_substr(cmd, "ddagrab")  # brak wejścia wideo
    assert "-c:v" not in cmd
    assert "audio=Loopback" in cmd
    assert _arg_value(cmd, "-map") == "0:a"  # audio-only: pierwsze wejście to audio
    assert _arg_value(cmd, "-af") == "aresample=async=1"


# ── Segmentacja + estymacja rozmiaru ──────────────────────────────────────────


def test_segmentation_args_present() -> None:
    cmd = build_record_command(
        source=CaptureSource(),
        audio=AudioConfig(system_audio=False),
        quality=PRESETS["standard"],
        encoders=_ALL_ENCODERS,
        segment_pattern="/tmp/seg_%03d.mkv",
        segment_seconds=120,
        segment_start_number=4,
    )
    # Jest kilka `-f` (lavfi dla wejścia ddagrab, segment dla muxera) — sprawdzamy muxer.
    assert "segment" in cmd and cmd[cmd.index("segment") - 1] == "-f"
    assert _arg_value(cmd, "-segment_time") == "120"
    assert _arg_value(cmd, "-segment_start_number") == "4"
    assert cmd[-1] == "/tmp/seg_%03d.mkv"


def test_estimate_size_grows_with_time() -> None:
    q = PRESETS["standard"]
    a = AudioConfig(system_audio=True, system_device="x")
    small = estimate_size_mb(q, 10, a)
    big = estimate_size_mb(q, 100, a)
    assert 0 < small < big


def test_estimate_zero_for_audio_only_without_devices() -> None:
    """M19: audio_only bez urządzeń → podgląd 0 MB (nagranie i tak nie ruszy)."""
    q = PRESETS["audio_only"]
    assert estimate_size_mb(q, 3600, AudioConfig(system_audio=False, microphone=False)) == 0.0
    # Z jednym urządzeniem szacuje normalnie (>0) — zachowanie bez zmian.
    assert estimate_size_mb(q, 3600, AudioConfig(system_audio=True, system_device="Mikrofon")) > 0


def test_presets_cover_required_set() -> None:
    labels = {opt.label for opt in PRESETS.values()}
    assert labels == {"Ekonomiczny", "Standard", "Wysoka", "Archiwum", "Tylko audio"}
    assert PRESETS["audio_only"].audio_only is True
