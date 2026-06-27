"""Rejestr dostawców chmury per zadanie.

Routing realizuje istniejący gateway LiteLLM (jedno OpenAI-kompatybilne API,
pod spodem Anthropic/OpenAI/Google/DeepSeek). Tu trzymamy tylko: który model do
którego zadania, oraz świadomość możliwości (vision / długi kontekst), bo dostawcy
NIE są wymienni jeden do jednego na każde zadanie.

Klucze API per dostawca trzymamy w keyring (core.secrets), nigdy w configu/repo.
Przełącznik "tylko lokalnie" (per kategoria/materiał) może zablokować wysyłkę do chmury.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Provider(str, Enum):
    LOCAL = "local"        # przez LiteLLM do Ollama (Devstral/Qwen)
    ANTHROPIC = "anthropic"  # Claude
    OPENAI = "openai"        # ChatGPT
    GEMINI = "gemini"        # Google
    DEEPSEEK = "deepseek"


class Task(str, Enum):
    NAMING = "naming"            # nazwa pliku — tani model wystarczy
    SUMMARY = "summary"          # streszczenie / notatka edukacyjna
    LONG_CONTEXT = "long_context"  # bardzo długie transkrypty — duże okno
    SLIDES_VLM = "slides_vlm"    # opis slajdów — WYMAGA vision
    RAG = "rag"                  # pytania do biblioteki
    TRANSCRIBE_CLOUD = "transcribe_cloud"  # fallback transkrypcji


@dataclass(slots=True)
class ModelSpec:
    provider: Provider
    model: str
    supports_vision: bool = False
    context_tokens: int = 0


@dataclass(slots=True)
class ProviderRegistry:
    """Mapowanie zadanie -> wybrany model. Edytowalne w ustawieniach."""

    assignments: dict[Task, ModelSpec] = field(default_factory=dict)

    def get(self, task: Task) -> ModelSpec | None:
        return self.assignments.get(task)

    def validate(self) -> list[str]:
        """Zwraca ostrzeżenia o niedopasowaniu modelu do wymagań zadania."""
        warnings: list[str] = []
        vlm = self.assignments.get(Task.SLIDES_VLM)
        if vlm is not None and not vlm.supports_vision:
            warnings.append(
                f"Zadanie SLIDES_VLM wymaga modelu z vision, a wybrano {vlm.model}."
            )
        return warnings
