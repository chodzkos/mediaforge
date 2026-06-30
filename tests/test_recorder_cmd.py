"""Testy budowania komend FFmpeg dla nagrywania (czysta logika, bez subprocess)."""

from __future__ import annotations

from mediaforge.core.engines.base import QualityOption
from mediaforge.core.engines.ffmpeg_cmd import (
    PRESETS,
    AudioConfig,
    CaptureMode,
    CaptureSource,
    build_record_command,
    estimate_size_mb,
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


def _arg_value(cmd: list[str], flag: str) -> str:
    return cmd[cmd.index(flag) + 1]


# ── Wybór enkodera (NVENC + fallback programowy) ───────────────────────────────


def test_encoder_prefers_nvenc_when_available() -> None:
    choice = select_video_encoder("hevc", _ALL_ENCODERS)
    assert choice.name == "hevc_nvenc"
    assert choice.hardware is True


def test_encoder_falls_back_to_software_without_nvenc() -> None:
    choice = select_video_encoder("hevc", _NO_NVENC)
    assert choice.name == "libx265"
    assert choice.hardware is False


def test_encoder_av1_falls_back_to_hevc_software_chain() -> None:
    # Brak av1 (sprzętowego i programowego) → łańcuch schodzi do dostępnego programowego.
    choice = select_video_encoder("av1", _NO_NVENC)
    assert choice.name in {"libx265", "libx264"}
    assert choice.hardware is False


def test_encoder_last_resort_is_libx264() -> None:
    choice = select_video_encoder("hevc", {"hevc_nvenc": False, "libx265": False})
    assert choice.name == "libx264"


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
    assert _has_substr(cmd, "ddagrab=output_idx=0:framerate=30")


def test_monitor_selects_output_idx() -> None:
    # Wybór monitora (GUI: mode=REGION, monitor=N) → output_idx ddagrab, nie offset/region.
    cmd = build_record_command(
        source=CaptureSource(mode=CaptureMode.REGION, monitor=1, region=(100, 200, 1280, 720)),
        audio=AudioConfig(system_audio=False),
        quality=PRESETS["standard"],
        encoders=_ALL_ENCODERS,
        segment_pattern="/tmp/seg_%03d.mkv",
    )
    assert _has_substr(cmd, "ddagrab=output_idx=1:")
    assert "-offset_x" not in cmd and "-video_size" not in cmd


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


def test_mix_two_sources_uses_amix() -> None:
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
    assert "amix=inputs=2" in fc
    assert "[aout]" in cmd  # zmapowany zmiksowany ślad


def test_two_sources_without_mix_map_separately() -> None:
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
    assert "-filter_complex" not in cmd
    # wideo = 0, audio = 1 i 2
    maps = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-map"]
    assert maps == ["0:v", "1:a", "2:a"]


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


def test_presets_cover_required_set() -> None:
    labels = {opt.label for opt in PRESETS.values()}
    assert labels == {"Ekonomiczny", "Standard", "Wysoka", "Archiwum", "Tylko audio"}
    assert PRESETS["audio_only"].audio_only is True
