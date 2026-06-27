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

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

from mediaforge.core.compute import GPUArch, classify


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


@runtime_checkable
class TranscriptionBackend(Protocol):
    def transcribe(self, audio: Path, opts: TranscribeOptions) -> Transcript: ...


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
