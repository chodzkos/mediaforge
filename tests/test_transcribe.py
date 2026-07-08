"""Backend transkrypcji: czyste buildery + parser JSON/runtime + orkiestracja (atrapa runnera)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mediaforge.core.ai.transcribe import (
    LineCb,
    RunResult,
    TranscribeOptions,
    TranscriptionError,
    WhisperCppBackend,
    build_silence_wav_command,
    build_whisper_command,
    detect_whisper_runtime,
    parse_whisper_json,
    parse_whisper_progress,
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

    def fake_runner(cmd: list[str], on_line: LineCb | None = None) -> RunResult:
        commands.append(cmd)
        if "-of" in cmd:  # przebieg whisper-cli → zapisz pliki wyjściowe + log CUDA
            prefix = cmd[cmd.index("-of") + 1]
            Path(prefix + ".json").write_text(json.dumps(_SAMPLE_JSON), encoding="utf-8")
            Path(prefix + ".srt").write_text("1\n", encoding="utf-8")
            return RunResult(0, _CUDA_LOG)
        Path(cmd[-1]).write_bytes(b"RIFF")  # konwersja WAV → utwórz plik audio
        return RunResult(0, "")

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


def test_transcribe_removes_intermediate_wav_on_success(tmp_path: Path) -> None:
    """Po udanym transkrypcie (JSON sparsowany) półprodukt audio16k.wav jest kasowany."""
    src = tmp_path / "lecture.mp4"
    src.write_bytes(b"x")
    out_dir = tmp_path / "material"

    def fake_runner(cmd: list[str], on_line: LineCb | None = None) -> RunResult:
        if "-of" in cmd:  # whisper-cli → json/srt
            prefix = cmd[cmd.index("-of") + 1]
            Path(prefix + ".json").write_text(json.dumps(_SAMPLE_JSON), encoding="utf-8")
            Path(prefix + ".srt").write_text("1\n", encoding="utf-8")
            return RunResult(0, _CUDA_LOG)
        Path(cmd[-1]).write_bytes(b"RIFF")  # konwersja WAV → utwórz plik audio
        return RunResult(0, "")

    backend = WhisperCppBackend(model="/m/medium.bin", runner=fake_runner)
    result = backend.transcribe(src, out_dir, TranscribeOptions())

    assert result.json_path is not None and result.json_path.is_file()  # transkrypt został
    assert not (out_dir / "audio16k.wav").exists()  # półprodukt sprzątnięty po sukcesie


# ── Głośna porażka (nie „done" po cichu) ──────────────────────────────────────


def test_transcribe_raises_when_ffmpeg_fails(tmp_path: Path) -> None:
    """ffmpeg returncode != 0 (brak WAV) → TranscriptionError, nie cichy „done"."""
    src = tmp_path / "lecture.mp4"
    src.write_bytes(b"x")

    def failing_runner(cmd: list[str], on_line: LineCb | None = None) -> RunResult:
        return RunResult(1, "linia\nffmpeg: Invalid data found when processing input\n")

    backend = WhisperCppBackend(model="/m/medium.bin", runner=failing_runner)
    with pytest.raises(TranscriptionError, match="ffmpeg nie przygotował audio"):
        backend.transcribe(src, tmp_path / "material", TranscribeOptions())


def test_transcribe_raises_when_whisper_makes_no_json(tmp_path: Path) -> None:
    """whisper-cli returncode 0, ale brak pliku .json → TranscriptionError."""
    src = tmp_path / "lecture.mp4"
    src.write_bytes(b"x")

    def fake_runner(cmd: list[str], on_line: LineCb | None = None) -> RunResult:
        if "-of" not in cmd:  # konwersja WAV OK
            Path(cmd[-1]).write_bytes(b"RIFF")
            return RunResult(0, "")
        return RunResult(0, _CUDA_LOG)  # whisper „udany", ale nie zapisał .json

    backend = WhisperCppBackend(model="/m/medium.bin", runner=fake_runner)
    with pytest.raises(TranscriptionError, match="whisper-cli nie wytworzył transkryptu"):
        backend.transcribe(src, tmp_path / "material", TranscribeOptions())


def test_transcribe_raises_on_empty_model_without_running(tmp_path: Path) -> None:
    """Pusty model → jednoznaczny TranscriptionError PRZED odpaleniem runnera."""
    src = tmp_path / "lecture.mp4"
    src.write_bytes(b"x")

    def boom(cmd: list[str], on_line: LineCb | None = None) -> RunResult:
        raise AssertionError("runner nie powinien być wołany bez modelu")

    backend = WhisperCppBackend(model="", runner=boom)
    with pytest.raises(TranscriptionError, match="nie skonfigurowano modelu whisper"):
        backend.transcribe(src, tmp_path / "material", TranscribeOptions())


# ── Empiryczna sonda runtime (doctor) ─────────────────────────────────────────


def test_silence_command_generates_16k_mono() -> None:
    cmd = build_silence_wav_command(Path("p.wav"))
    assert "anullsrc=r=16000:cl=mono" in cmd and "pcm_s16le" in cmd
    assert cmd[cmd.index("-t") + 1] == "0.1"


def test_detect_runtime_cuda() -> None:
    def fake(cmd: list[str], on_line: LineCb | None = None) -> RunResult:
        if "anullsrc=r=16000:cl=mono" in cmd:  # cisza → utwórz wav, by sonda szła dalej
            Path(cmd[-1]).write_bytes(b"RIFF")
            return RunResult(0, "")
        return RunResult(0, _CUDA_LOG)  # przebieg whisper

    assert detect_whisper_runtime("whisper-cli", "/m/model.bin", runner=fake) == "cuda"


def test_detect_runtime_cpu() -> None:
    def fake(cmd: list[str], on_line: LineCb | None = None) -> RunResult:
        if "anullsrc=r=16000:cl=mono" in cmd:
            Path(cmd[-1]).write_bytes(b"RIFF")
            return RunResult(0, "")
        return RunResult(0, _CPU_LOG)

    assert detect_whisper_runtime("whisper-cli", "/m/model.bin", runner=fake) == "cpu"


def test_detect_runtime_unknown_without_model() -> None:
    # Bez modelu nie odpalamy whisper (runner nie jest wołany) → unknown (degraded).
    def boom(cmd: list[str], on_line: LineCb | None = None) -> RunResult:
        raise AssertionError("runner nie powinien być wołany bez modelu")

    assert detect_whisper_runtime("whisper-cli", None, runner=boom) == "unknown"
    assert detect_whisper_runtime("whisper-cli", "", runner=boom) == "unknown"


def test_detect_runtime_unknown_when_wav_fails() -> None:
    def fake(cmd: list[str], on_line: LineCb | None = None) -> RunResult:
        return RunResult(1, "")  # nie tworzy wav → sonda nie ma czego transkrybować

    assert detect_whisper_runtime("whisper-cli", "/m/model.bin", runner=fake) == "unknown"


# ── Postęp transkrypcji (procent) ─────────────────────────────────────────────


def test_parse_whisper_progress() -> None:
    assert parse_whisper_progress("whisper_print_progress_callback: progress = 45%") == 45
    assert parse_whisper_progress("progress =  0%") == 0
    assert parse_whisper_progress("progress = 100%") == 100
    assert parse_whisper_progress("whisper_full: jakaś inna linia") is None
    assert parse_whisper_progress("progress = 150%") == 100  # clamp


def test_whisper_command_has_print_progress() -> None:
    assert "--print-progress" in build_whisper_command("m", Path("a.wav"), Path("base"))


def test_backend_streams_progress_with_throttle(tmp_path: Path) -> None:
    """on_progress woła się tylko przy ZMIANIE procentu; backend nadal z pełnego buforu."""
    src = tmp_path / "lecture.mp4"
    src.write_bytes(b"x")
    out_dir = tmp_path / "material"
    # Sekwencja linii whisper-cli: powtórzone 0% i 50% (test throttle) + log CUDA na końcu.
    progress_lines = [
        "progress = 0%\n",
        "progress = 0%\n",
        "progress = 10%\n",
        "progress = 50%\n",
        "progress = 50%\n",
        "progress = 100%\n",
        "whisper_backend_init_gpu: using CUDA0 backend\n",
    ]

    def fake_runner(cmd: list[str], on_line: LineCb | None = None) -> RunResult:
        if "-of" not in cmd:
            Path(cmd[-1]).write_bytes(b"RIFF")  # konwersja WAV → utwórz plik audio
            return RunResult(0, "")
        full = "".join(progress_lines)
        if on_line is not None:
            for line in progress_lines:
                on_line(line)
        prefix = cmd[cmd.index("-of") + 1]
        Path(prefix + ".json").write_text(json.dumps(_SAMPLE_JSON), encoding="utf-8")
        return RunResult(0, full)

    seen: list[int] = []
    backend = WhisperCppBackend(model="/m/medium.bin", runner=fake_runner)
    result = backend.transcribe(src, out_dir, TranscribeOptions(), on_progress=seen.append)

    assert seen == [0, 10, 50, 100]  # throttle: bez powtórzeń
    assert result.runtime == "cuda"  # backend z PEŁNEGO buforu (mimo strumieniowania)
