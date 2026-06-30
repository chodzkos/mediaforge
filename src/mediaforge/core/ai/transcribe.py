"""Kontrakt backendu transkrypcji (wymienny — kluczowe pod Blackwell/sm_120).

Default: whisper.cpp (CUDA, subprocess) — bez PyTorcha/CTranslate2/transformers,
więc omija całą klasę problemów sm_120. Alternatywy wybierane świadomie:
  - faster-whisper DZIAŁA na sm_120 wyłącznie z compute_type="float16"
    (domyślny int8 -> CUBLAS_STATUS_NOT_SUPPORTED).
  - insanely-fast-whisper: tor mocy, wymaga nightly PyTorcha (cu12x) + pinów transformers.
  - cloud: fallback przez gateway LiteLLM (słaby sprzęt / bardzo długie materiały).
Patrz docs/CLAUDE.md (sekcja Blackwell).
"""

from __future__ import annotations

import contextlib
import json
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from mediaforge.core.compute import GPUArch, classify
from mediaforge.core.engines.import_engine import build_extract_wav_command

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class Backend(StrEnum):
    WHISPERCPP = "whispercpp"  # DEFAULT
    FASTER_WHISPER = "faster_whisper"  # tylko compute_type=float16 na sm_120
    INSANELY_FAST = "insanely_fast"  # tor mocy (extra: transcribe-hf)
    CLOUD = "cloud"  # przez LiteLLM


@dataclass(slots=True)
class TranscribeOptions:
    backend: Backend = Backend.WHISPERCPP
    language: str | None = None  # None = autodetekcja (PL/EN)
    diarize: bool = False  # wymaga pyannote (extra) + tokenu HF w keyring


@dataclass(slots=True)
class Segment:
    start: float
    end: float
    text: str
    speaker: str | None = None


@dataclass(slots=True)
class Transcript:
    language: str
    model: str
    segments: list[Segment] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(s.text.strip() for s in self.segments)


# Faktycznie użyty runtime whisper.cpp, sparsowany z logu (EMPIRYKA, nie próg arch).
WhisperRuntime = Literal["cuda", "cpu", "unknown"]


@dataclass(slots=True)
class TranscriptionResult:
    """Wynik transkrypcji: tekst + segmenty, realny runtime i ścieżki plików wyjściowych."""

    transcript: Transcript
    runtime: WhisperRuntime
    json_path: Path | None
    srt_path: Path | None


@runtime_checkable
class TranscriptionBackend(Protocol):
    """Wymienny backend: źródło + folder materiału → wynik (pliki w folderze = źródło prawdy)."""

    name: str

    def transcribe(
        self, source: Path, out_dir: Path, opts: TranscribeOptions
    ) -> TranscriptionResult: ...


# ── Detekcja realnego backendu z logu whisper-cli (EMPIRYKA, znosi próg sm_75/cu130) ──

_CUDA_POS = (
    re.compile(r"using CUDA\d*\s+backend", re.IGNORECASE),
    re.compile(r"ggml_cuda_init:\s*found\s*[1-9]"),
    re.compile(r"CUDA\s*:\s*ARCHS\s*=\s*[1-9]"),
)
_CUDA_NEG = (
    re.compile(r"ggml_cuda_init:\s*found\s*0"),
    re.compile(r"CUDA\s*:\s*ARCHS\s*=\s*0"),
    re.compile(r"use gpu\s*=\s*0"),
)


def whisper_backend_from_output(stderr: str) -> WhisperRuntime:
    """Parsuje stderr whisper-cli → czy CUDA realnie ruszyła (``cuda``/``cpu``/``unknown``)."""
    if any(pattern.search(stderr) for pattern in _CUDA_POS):
        return "cuda"
    if any(pattern.search(stderr) for pattern in _CUDA_NEG):
        return "cpu"
    return "unknown"


def build_silence_wav_command(
    out: Path, *, seconds: float = 0.1, ffmpeg: str = "ffmpeg"
) -> list[str]:
    """Komenda generująca krótką ciszę 16 kHz mono — sygnał próbny do sondy runtime."""
    return [
        ffmpeg,
        "-hide_banner",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=r=16000:cl=mono",
        "-t",
        str(seconds),
        "-c:a",
        "pcm_s16le",
        str(out),
    ]


def detect_whisper_runtime(
    whisper_cli: str,
    model: str | None,
    *,
    ffmpeg: str = "ffmpeg",
    runner: Runner | None = None,
) -> WhisperRuntime:
    """Empiryczna sonda: krótka cisza → whisper-cli na modelu → realny backend.

    EMPIRYKA zamiast progu arch (znosi sm_75/cu130 — na Pascalu po prostu zobaczysz wynik).
    Bez modelu nie da się odpalić → ``unknown`` (doctor pokaże „model nieustawiony"). Heurystyka
    arch+VRAM (``compute.classify``) ZOSTAJE fallbackiem decyzji o tierze — to tylko obserwacja.
    """
    if not model:
        return "unknown"
    run = runner if runner is not None else _default_runner
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "probe.wav"
        run(build_silence_wav_command(wav, ffmpeg=ffmpeg))
        if not wav.is_file():
            return "unknown"
        result = run(
            build_whisper_command(
                model,
                wav,
                Path(tmp) / "probe",
                language="auto",
                beam_size=None,
                whisper_cli=whisper_cli,
            )
        )
    return whisper_backend_from_output(result.stderr)


@lru_cache(maxsize=8)
def cached_whisper_runtime(whisper_cli: str, model: str) -> WhisperRuntime:
    """Sonda runtime z cache (model ładuje się ~1-2 s) — patrz :func:`detect_whisper_runtime`."""
    return detect_whisper_runtime(whisper_cli, model)


# ── Czyste buildery i parser (testowalne bez whisper.cpp/ffmpeg) ──────────────


def build_whisper_command(
    model: str,
    wav: Path,
    out_prefix: Path,
    *,
    language: str = "auto",
    threads: int | None = None,
    beam_size: int | None = 5,
    whisper_cli: str = "whisper-cli",
) -> list[str]:
    """Komenda whisper-cli: model + wav → ``<out_prefix>.json`` i ``.srt`` w folderze materiału."""
    cmd = [
        whisper_cli,
        "-m",
        str(model),
        "-f",
        str(wav),
        "-l",
        language,
        "--output-json",
        "--output-srt",
        "-of",
        str(out_prefix),
    ]
    if threads:
        cmd += ["-t", str(threads)]
    if beam_size:
        cmd += ["-bs", str(beam_size)]
    return cmd


def parse_whisper_json(data: dict[str, Any], *, model: str = "") -> Transcript:
    """Parsuje JSON whisper.cpp (``--output-json``) → :class:`Transcript` (offsety ms→s)."""
    raw_result = data.get("result")
    result: dict[str, Any] = raw_result if isinstance(raw_result, dict) else {}
    raw_params = data.get("params")
    params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
    language = str(result.get("language") or params.get("language") or "")
    segments: list[Segment] = []
    for item in data.get("transcription") or []:
        raw_offsets = item.get("offsets")
        offsets: dict[str, Any] = raw_offsets if isinstance(raw_offsets, dict) else {}
        try:
            start = float(offsets.get("from", 0)) / 1000.0
            end = float(offsets.get("to", 0)) / 1000.0
        except (TypeError, ValueError):
            start = end = 0.0
        segments.append(Segment(start=start, end=end, text=str(item.get("text", "")).strip()))
    return Transcript(language=language, model=model, segments=segments)


# ── Wykonawca (subprocess, wstrzykiwalny) + backend whisper.cpp ───────────────


@dataclass(slots=True)
class RunResult:
    """Wynik uruchomienia procesu — kod wyjścia + stderr (do detekcji runtime)."""

    returncode: int
    stderr: str


Runner = Callable[[list[str]], RunResult]


def _default_runner(command: list[str]) -> RunResult:
    try:
        proc = subprocess.run(
            command, capture_output=True, text=True, creationflags=_NO_WINDOW, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return RunResult(returncode=1, stderr=str(exc))
    return RunResult(returncode=proc.returncode, stderr=proc.stderr or "")


@dataclass(slots=True)
class WhisperCppBackend:
    """Domyślny backend: ffmpeg → 16 kHz mono WAV → whisper-cli (CUDA, torch-free).

    Binarka (``whispercpp_path``) i model (``whisper_model``) z configu — podawane przez
    wiring (CLI/GUI). Runner wstrzykiwany, więc orkiestracja jest testowalna bez whisper.cpp.
    """

    model: str  # ścieżka .bin (config: whisper_model)
    whisper_cli: str = "whisper-cli"
    ffmpeg: str = "ffmpeg"
    threads: int | None = None
    runner: Runner = _default_runner
    name: str = "whispercpp"

    def transcribe(
        self, source: Path, out_dir: Path, opts: TranscribeOptions
    ) -> TranscriptionResult:
        """Konwersja → transkrypcja; zapisuje .json/.srt do ``out_dir`` (folder materiału)."""
        out_dir.mkdir(parents=True, exist_ok=True)
        wav = out_dir / "audio16k.wav"
        self.runner(build_extract_wav_command(source, wav, self.ffmpeg))

        language = opts.language or "auto"
        out_prefix = out_dir / source.stem
        run = self.runner(
            build_whisper_command(
                self.model,
                wav,
                out_prefix,
                language=language,
                threads=self.threads,
                whisper_cli=self.whisper_cli,
            )
        )
        runtime = whisper_backend_from_output(run.stderr)

        model_name = Path(self.model).stem
        json_path = out_dir / f"{source.stem}.json"
        srt_path = out_dir / f"{source.stem}.srt"
        transcript = Transcript(language=language, model=model_name)
        if json_path.is_file():
            with contextlib.suppress(OSError, ValueError):
                transcript = parse_whisper_json(
                    json.loads(json_path.read_text(encoding="utf-8")), model=model_name
                )
        return TranscriptionResult(
            transcript=transcript,
            runtime=runtime,
            json_path=json_path if json_path.is_file() else None,
            srt_path=srt_path if srt_path.is_file() else None,
        )


def select_backend(
    has_cuda: bool,
    vram_gb: float,
    arch: GPUArch = GPUArch.UNKNOWN,
) -> Backend:
    """Auto-dobór backendu transkrypcji na podstawie profilu obliczeniowego maszyny.

    UWAGA: o wykonalności lokalnej decyduje VRAM i ARCHITEKTURA (nie systemowy RAM).
    whisper.cpp radzi sobie nawet na Pascalu (1070), więc transkrypcja zostaje lokalna
    także w Tier B — w chmurę schodzimy dopiero, gdy GPU jest za słaby (Tier C).

    Na sm_120 (Blackwell) NIE używaj faster-whisper z domyślnym int8 — zob. CLAUDE.md.
    """
    profile = classify(has_cuda, vram_gb, arch)
    return Backend.WHISPERCPP if profile.transcription_local else Backend.CLOUD
