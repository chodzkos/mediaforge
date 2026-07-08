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
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from mediaforge.core.compute import GPUArch, classify
from mediaforge.core.engines.import_engine import build_extract_wav_command
from mediaforge.core.winutil import NO_WINDOW_FLAGS


class TranscriptionError(RuntimeError):
    """Transkrypcja nie powiodła się — ffmpeg/whisper-cli zwrócił błąd lub nie ma pliku wyjściowego.

    Rzucany, gdy proceduralny kontrakt runnera zostaje złamany (returncode != 0 albo brak
    oczekiwanego pliku). Podnoszony do handlera kolejki → job trafia w ``mark_failed`` z retry.
    """


def _stderr_tail(stderr: str, *, lines: int = 5) -> str:
    """Końcówka stderr (ostatnie ~``lines`` niepustych linii) do komunikatu błędu.

    Runner zwraca CAŁY bufor stderr (bywa bardzo długi) — do wyjątku bierzemy tylko końcówkę,
    nigdy całości (nie logujemy pełnego bufora na poziomie error).
    """
    tail = [line for line in stderr.splitlines() if line.strip()][-lines:]
    return "\n".join(tail)


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
        self,
        source: Path,
        out_dir: Path,
        opts: TranscribeOptions,
        *,
        on_progress: Callable[[int], None] | None = None,
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
        "--print-progress",  # emituje „progress = N%" na stderr (do paska postępu)
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


_PROGRESS_RE = re.compile(r"progress\s*=\s*(\d+)\s*%")


def parse_whisper_progress(line: str) -> int | None:
    """Wyciąga procent (0-100) z linii postępu whisper-cli, albo None gdy to nie ta linia."""
    match = _PROGRESS_RE.search(line)
    if not match:
        return None
    return max(0, min(100, int(match.group(1))))


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


LineCb = Callable[[str], None]


class Runner(Protocol):
    """Uruchamia proces; opcjonalnie strumieniuje stderr linia po linii do ``on_line``."""

    def __call__(self, command: list[str], on_line: LineCb | None = None, /) -> RunResult: ...


def _default_runner(command: list[str], on_line: LineCb | None = None) -> RunResult:
    """Popen ze strumieniowaniem stderr: woła ``on_line`` na bieżąco (np. „progress = N%")
    i akumuluje pełny bufor, który zwraca — pełny stderr zostaje do detekcji backendu."""
    try:
        proc = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=NO_WINDOW_FLAGS,
        )
    except (OSError, ValueError) as exc:
        return RunResult(returncode=1, stderr=str(exc))
    lines: list[str] = []
    if proc.stderr is not None:
        for line in proc.stderr:
            lines.append(line)
            if on_line is not None:
                on_line(line)
    proc.wait()
    return RunResult(returncode=proc.returncode, stderr="".join(lines))


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
        self,
        source: Path,
        out_dir: Path,
        opts: TranscribeOptions,
        *,
        on_progress: Callable[[int], None] | None = None,
    ) -> TranscriptionResult:
        """Konwersja → transkrypcja; zapisuje .json/.srt do ``out_dir`` (folder materiału).

        ``on_progress(pct)`` woła się przy ZMIANIE procentu (throttle) — strumieniowane ze
        stderr whisper-cli (``--print-progress``). Backend (cuda/cpu) wykrywany z pełnego buforu.

        Głośna porażka: błąd ffmpeg/whisper-cli lub brak pliku wyjściowego →
        :class:`TranscriptionError` (nie „done"). Wyjątek leci do handlera → ``mark_failed``.
        """
        if not self.model:  # sprawdzamy PRZED odpaleniem runnera (nie ma czym transkrybować)
            raise TranscriptionError("nie skonfigurowano modelu whisper (whisper_model)")

        out_dir.mkdir(parents=True, exist_ok=True)
        wav = out_dir / "audio16k.wav"
        extract = self.runner(build_extract_wav_command(source, wav, self.ffmpeg))
        if extract.returncode != 0 or not wav.is_file():
            raise TranscriptionError(
                f"ffmpeg nie przygotował audio ({source}): {_stderr_tail(extract.stderr)}"
            )

        language = opts.language or "auto"
        out_prefix = out_dir / source.stem
        last_pct = -1

        def on_line(line: str) -> None:
            nonlocal last_pct
            if on_progress is None:
                return
            pct = parse_whisper_progress(line)
            if pct is not None and pct != last_pct:  # throttle: tylko przy zmianie
                last_pct = pct
                on_progress(pct)

        run = self.runner(
            build_whisper_command(
                self.model,
                wav,
                out_prefix,
                language=language,
                threads=self.threads,
                whisper_cli=self.whisper_cli,
            ),
            on_line if on_progress is not None else None,
        )
        runtime = whisper_backend_from_output(run.stderr)

        model_name = Path(self.model).stem
        json_path = out_dir / f"{source.stem}.json"
        srt_path = out_dir / f"{source.stem}.srt"
        if run.returncode != 0 or not json_path.is_file():
            raise TranscriptionError(
                f"whisper-cli nie wytworzył transkryptu ({source}): {_stderr_tail(run.stderr)}"
            )

        transcript = Transcript(language=language, model=model_name)
        parsed = False
        with contextlib.suppress(OSError, ValueError):
            transcript = parse_whisper_json(
                json.loads(json_path.read_text(encoding="utf-8")), model=model_name
            )
            parsed = True
        if parsed:
            # Półprodukt WAV (16 kHz mono) jest zbędny po udanym transkrypcie — kasujemy. Przy
            # błędzie parsowania zostaje obok .json/.srt do diagnostyki.
            with contextlib.suppress(OSError):
                wav.unlink()
        return TranscriptionResult(
            transcript=transcript,
            runtime=runtime,
            json_path=json_path,
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
