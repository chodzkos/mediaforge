"""Profil obliczeniowy per maszyna.

Decyduje, co liczyć lokalnie, a co w chmurze — i jak duży model lokalny ma sens.
KLUCZOWE: liczy się nie sam VRAM, ale też ARCHITEKTURA GPU. 1070 (Pascal, 8 GB,
bez rdzeni Tensor, słaby FP16) zmieści mały model, ale poleci wolno; RTX 5090
(Blackwell, 24 GB) uciągnie tor lokalny w całości.

Profil jest zapisywany per maszyna (jak "per-machine suggestion" w pdf2md), więc
ten sam kod inaczej zachowuje się na laptopie 5090 i na pececie z 1070.

Ten moduł jest niezależny od warstwy transkrypcji/LLM (brak importów w górę),
żeby uniknąć cykli — mapowanie na konkretny backend robi transcribe.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class GPUArch(str, Enum):
    BLACKWELL = "blackwell"  # RTX 50xx (sm_120) — uwaga: int8 w CTranslate2 nie działa
    ADA = "ada"              # RTX 40xx
    AMPERE = "ampere"        # RTX 30xx / A-series
    TURING = "turing"        # RTX 20xx / GTX 16xx
    PASCAL = "pascal"        # GTX 10xx (np. 1070) — bez Tensor cores, słaby FP16
    OLDER = "older"
    NONE = "none"            # brak CUDA
    UNKNOWN = "unknown"


class ComputeTier(str, Enum):
    A = "A"  # wszystko lokalnie (mocny, nowoczesny GPU)
    B = "B"  # transkrypcja lokalnie; LLM/VLM domyślnie chmura (lub mały model lokalny)
    C = "C"  # wszystko w chmurze (brak/za słaby GPU)


_MODERN = {GPUArch.BLACKWELL, GPUArch.ADA, GPUArch.AMPERE, GPUArch.TURING}


@dataclass(slots=True)
class ComputeProfile:
    has_cuda: bool
    vram_gb: float
    arch: GPUArch
    tier: ComputeTier
    transcription_local: bool   # transkrypcja na GPU lokalnie (whisper.cpp radzi sobie i na Pascalu)
    whisper_model: str          # podpowiedź rozmiaru modelu Whisper
    llm_local: bool             # czy streszczenia LLM lokalnie
    llm_model_hint: str | None  # sugerowany model lokalny (None = chmura)
    vlm_local: bool             # czy opis slajdów VLM lokalnie
    note: str


def classify(has_cuda: bool, vram_gb: float, arch: GPUArch = GPUArch.UNKNOWN) -> ComputeProfile:
    """Heurystyka tieru. Wynik to DOMYŚLNE, zawsze nadpisywalne ręcznie w ustawieniach."""
    # Tier C — brak CUDA albo za mało VRAM nawet na sensowną transkrypcję.
    if not has_cuda or vram_gb < 4:
        return ComputeProfile(
            has_cuda, vram_gb, arch, ComputeTier.C,
            transcription_local=False, whisper_model="cloud",
            llm_local=False, llm_model_hint=None, vlm_local=False,
            note="Brak wystarczającego GPU — całość przez chmurę (LiteLLM).",
        )

    modern_big = arch in _MODERN and vram_gb >= 16

    # Tier A — nowoczesny GPU z dużym VRAM: wszystko lokalnie.
    if modern_big:
        return ComputeProfile(
            has_cuda, vram_gb, arch, ComputeTier.A,
            transcription_local=True, whisper_model="large-v3",
            llm_local=True, llm_model_hint="qwen2.5:14b lub devstral:24b",
            vlm_local=True,
            note="Mocny GPU — transkrypcja, LLM i VLM lokalnie.",
        )

    # Tier B — np. 1070 (8 GB, Pascal): transkrypcja lokalnie, ale LLM/VLM rozsądniej w chmurze.
    whisper_model = "medium" if vram_gb >= 8 else "small"
    small_local_llm = vram_gb >= 8  # 7-8B w Q4 się zmieści, ale na Pascalu będzie wolno
    return ComputeProfile(
        has_cuda, vram_gb, arch, ComputeTier.B,
        transcription_local=True, whisper_model=whisper_model,
        llm_local=small_local_llm, llm_model_hint="qwen2.5:7b" if small_local_llm else None,
        vlm_local=False,
        note=(
            "Słabszy/starszy GPU — transkrypcja lokalnie (mniejszy model Whisper). "
            "LLM: mały model lokalny możliwy, ale powolny; domyślnie sugerowana chmura. "
            "VLM: chmura."
        ),
    )
