"""Backend transkrypcji: czyste buildery + parser JSON/runtime + orkiestracja (atrapa runnera)."""

from __future__ import annotations

import json
from pathlib import Path

from mediaforge.core.ai.transcribe import (
    RunResult,
    TranscribeOptions,
    WhisperCppBackend,
    build_whisper_command,
    parse_whisper_json,
    whisper_backend_from_output,
)
from mediaforge.core.engines.import_engine import build_extract_wav_command

# ── Buildery ──────────────────────────────────────────────────────────────────


def test_wav_command_is_16k_mono_pcm() -> None:
    cmd = build_extract_wav_command(Path("in.mp4"), Path("a.wav"))
    assert cmd[cmd.index("-ar") + 1] == "16000"
    assert cmd[cmd.index("-ac") + 1] == "1"
    assert "pcm_s16le" in cmd and cmd[-1] == "a.wav"


def test_whisper_command_outputs_json_and_srt() -> None:
    cmd = build_whisper_command(
        "model.bin", Path("a.wav"), Path("out/base"), language="pl", threads=8
    )
    assert cmd[cmd.index("-m") + 1] == "model.bin"
    assert cmd[cmd.index("-l") + 1] == "pl"
    assert cmd[cmd.index("-t") + 1] == "8"
    assert "--output-json" in cmd and "--output-srt" in cmd
    assert cmd[cmd.index("-of") + 1].endswith("base")
    assert "-bs" in cmd


def test_whisper_command_defaults_auto_and_omits_threads() -> None:
    cmd = build_whisper_command("m", Path("a.wav"), Path("base"))
    assert cmd[cmd.index("-l") + 1] == "auto"
    assert "-t" not in cmd


# ── Detekcja runtime (na realnych logach) ─────────────────────────────────────

_CUDA_LOG = (
    "ggml_cuda_init: found 1 CUDA devices:\n"
    "  Device 0: NVIDIA GeForce RTX 5090 Laptop GPU, compute capability 12.0\n"
    "whisper_backend_init_gpu: using CUDA0 backend\n"
)
_CPU_LOG = "ggml_cuda_init: found 0 CUDA devices\nwhisper_backend_init: using CPU backend\n"


def test_backend_detect_cuda() -> None:
    assert whisper_backend_from_output(_CUDA_LOG) == "cuda"


def test_backend_detect_cpu_fallback() -> None:
    assert whisper_backend_from_output(_CPU_LOG) == "cpu"


def test_backend_detect_unknown() -> None:
    assert whisper_backend_from_output("nic istotnego w logu") == "unknown"


# ── Parser JSON ───────────────────────────────────────────────────────────────

_SAMPLE_JSON = {
    "result": {"language": "pl"},
    "transcription": [
        {"offsets": {"from": 0, "to": 2000}, "text": " Dzień dobry"},
        {"offsets": {"from": 2000, "to": 4500}, "text": " witam na wykładzie"},
    ],
}


def test_parse_whisper_json() -> None:
    transcript = parse_whisper_json(_SAMPLE_JSON, model="medium")
    assert transcript.language == "pl" and transcript.model == "medium"
    assert len(transcript.segments) == 2
    assert transcript.segments[0].start == 0.0 and transcript.segments[0].end == 2.0
    assert transcript.segments[0].text == "Dzień dobry"
    assert transcript.text == "Dzień dobry witam na wykładzie"


# ── Orkiestracja backendu (atrapa runnera, bez whisper.cpp/ffmpeg) ────────────


def test_whispercpp_backend_orchestration(tmp_path: Path) -> None:
    src = tmp_path / "lecture.mp4"
    src.write_bytes(b"x")
    out_dir = tmp_path / "material"
    commands: list[list[str]] = []

    def fake_runner(cmd: list[str]) -> RunResult:
        commands.append(cmd)
        if "-of" in cmd:  # przebieg whisper-cli → zapisz pliki wyjściowe + log CUDA
            prefix = cmd[cmd.index("-of") + 1]
            Path(prefix + ".json").write_text(json.dumps(_SAMPLE_JSON), encoding="utf-8")
            Path(prefix + ".srt").write_text("1\n", encoding="utf-8")
            return RunResult(0, _CUDA_LOG)
        return RunResult(0, "")  # konwersja WAV

    backend = WhisperCppBackend(model="/m/medium.bin", runner=fake_runner)
    result = backend.transcribe(src, out_dir, TranscribeOptions(language="pl"))

    assert result.runtime == "cuda"  # realny backend z logu
    assert result.transcript.language == "pl"
    assert result.transcript.segments[0].text == "Dzień dobry"
    assert result.json_path is not None and result.json_path.is_file()
    assert result.srt_path is not None
    # Najpierw konwersja do WAV, potem whisper z --output-json.
    assert any("pcm_s16le" in c for c in commands)
    assert any("--output-json" in c for c in commands)
