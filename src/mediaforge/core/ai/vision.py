"""Klient VLM (analiza slajdów) przez gateway LiteLLM (Qt-free).

TWARDA GRANICA (identyczna jak streszczenia): aplikacja rozmawia WYŁĄCZNIE z gatewayem LiteLLM.
ZERO SDK dostawców, ZERO kluczy dostawców. Model (lokalny/chmurowy) wybiera
:func:`~core.ai.routing.resolve_route`; wysyłka do chmury bramkowana ``assert_route_allowed``.

Kształt żądania to OpenAI vision (content-lista z ``image_url`` jako data-URL base64) — LiteLLM
przekazuje go do Ollamy (qwen3-vl). Transport, podział błędów i detekcja ucięcia są wspólne z
klientem streszczeń (:mod:`core.ai.gateway`) — tu tylko budowa payloadu z obrazem i parser
odpowiedzi na trzy pola (TYTUŁ/TEKST/OPIS).

Uwagi zmierzone w pre-flight:

* ``max_tokens`` domyślnie **2048** (nie 1024) — gęste slajdy tabelaryczne potrzebują zapasu na
  pełny odczyt sekcji TEKST.
* ``prompt_suffix`` (``/no_think``) jest KONIECZNOŚCIĄ, nie ostrożnością: qwen3-vl bez niego
  spala cały budżet na rozumowanie (``completion_tokens == max_tokens``, pusty ``content``,
  ``finish_reason`` kłamie „stop"). Detekcja ucięcia (:func:`~core.ai.gateway.is_truncated`)
  obowiązuje więc tak samo jak w streszczeniach.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mediaforge.core.ai.gateway import (
    Transport,
    default_transport,
    parse_chat_content,
    post_chat,
)
from mediaforge.core.ai.routing import ModelRoute

# MIME obrazu z rozszerzenia pliku (format data-URL). Nieznane rozszerzenie → PNG (bezpieczny
# domyślny dla zrzutów slajdów). ``.jpg`` i ``.jpeg`` mapują się na to samo ``image/jpeg``.
_MIME_BY_SUFFIX: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
_DEFAULT_MIME = "image/png"

# Prompt analizy slajdu (PL). Trzy sekcje w stałym porządku, by parser mógł je rozdzielić.
# Sufiks (``/no_think``) dokleja się w system-prompcie — patrz uwaga o budżecie w module docstring.
SLIDE_ANALYSIS_PROMPT = (
    "Przeanalizuj slajd wykładu. Zwróć: (1) TYTUŁ slajdu (krótki), (2) TEKST — wierny odczyt "
    "tekstu ze slajdu, (3) OPIS — 1-2 zdania co pokazuje (wykres/schemat/tabela). "
    "Format:\nTYTUŁ:\n...\nTEKST:\n...\nOPIS:\n..."
)


@dataclass(slots=True)
class VisionConfig:
    """Ustawienia transportu/formatu analizy VLM (model wybiera routing, nie ten config).

    ``base_url`` to endpoint gatewaya LiteLLM. ``api_key`` to OPCJONALNY master key gatewaya
    (keyring). ``timeout`` per wywołanie = ``summary_timeout`` (analiza slajdu na lokalnym VLM
    bywa kilkusekundowa, ale gęsty slajd + zimny model potrafi trwać). ``prompt_suffix``
    (``/no_think``) jest konieczny dla qwen3-vl (patrz module docstring).
    """

    base_url: str
    # 2048: gęste slajdy tabelaryczne potrzebują zapasu na pełny odczyt sekcji TEKST (pre-flight).
    max_tokens: int = 2048
    timeout: float = 600.0
    api_key: str | None = None
    prompt_suffix: str = "/no_think"


@dataclass(frozen=True, slots=True)
class SlideAnalysis:
    """Wynik analizy slajdu przez VLM: trzy pola z odpowiedzi (odporne na braki → puste)."""

    title: str
    text: str
    description: str


def _mime_for(image_path: Path) -> str:
    """MIME obrazu z rozszerzenia (data-URL); nieznane → PNG."""
    return _MIME_BY_SUFFIX.get(image_path.suffix.lower(), _DEFAULT_MIME)


def _data_url(image_path: Path) -> str:
    """Data-URL ``data:<mime>;base64,<b64>`` z zawartości pliku obrazu."""
    b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return f"data:{_mime_for(image_path)};base64,{b64}"


def _system_prompt(suffix: str) -> str:
    """Instrukcja systemowa analizy slajdu + opcjonalny sufiks (``/no_think`` dla qwen3-vl)."""
    base = "Jesteś asystentem analizującym slajdy wykładu. Odpowiadaj rzeczowo, po polsku."
    return f"{base} {suffix}" if suffix else base


def build_vision_request(
    image_path: Path,
    prompt: str,
    route: ModelRoute,
    config: VisionConfig,
) -> dict[str, Any]:
    """Składa payload chat-completions z obrazem (format OpenAI vision).

    Wiadomość usera to content-lista: część tekstowa (``prompt``) + część ``image_url`` z obrazem
    zakodowanym jako data-URL base64 (MIME z rozszerzenia). ``model`` bierze się z trasy,
    system-prompt niesie ``config.prompt_suffix``. LiteLLM przekazuje ten kształt do Ollamy.
    """
    return {
        "model": route.model,
        "messages": [
            {"role": "system", "content": _system_prompt(config.prompt_suffix)},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": _data_url(image_path)}},
                ],
            },
        ],
        "max_tokens": config.max_tokens,
    }


def parse_slide_analysis(raw: str) -> SlideAnalysis:
    """Rozbija surową odpowiedź VLM na trzy pola (TYTUŁ/TEKST/OPIS) — czysta funkcja.

    Odporny na braki: nagłówki sekcji rozpoznajemy po prefiksie linii (``TYTUŁ:``/``TEKST:``/
    ``OPIS:``, wielkość liter i dwukropek elastycznie); tekst po nagłówku (i w kolejnych liniach
    do następnego nagłówka) trafia do danego pola. Brak sekcji → puste pole (nie crash), tekst
    przed pierwszym nagłówkiem jest ignorowany (np. rozgrzewka modelu).
    """
    fields: dict[str, list[str]] = {"title": [], "text": [], "description": []}
    header_by_key = {"title": "tytuł", "text": "tekst", "description": "opis"}
    current: str | None = None
    for line in raw.splitlines():
        matched = _match_header(line, header_by_key)
        if matched is not None:
            key, rest = matched
            current = key
            if rest:
                fields[key].append(rest)
            continue
        if current is not None:
            fields[current].append(line)
    return SlideAnalysis(
        title=_join(fields["title"]),
        text=_join(fields["text"]),
        description=_join(fields["description"]),
    )


def _match_header(line: str, header_by_key: dict[str, str]) -> tuple[str, str] | None:
    """Czy linia zaczyna sekcję (TYTUŁ/TEKST/OPIS)? → (klucz, reszta po dwukropku) albo ``None``."""
    stripped = line.strip()
    lowered = stripped.lower()
    for key, header in header_by_key.items():
        prefix = f"{header}:"
        if lowered.startswith(prefix):
            return key, stripped[len(prefix) :].strip()
    return None


def _join(lines: list[str]) -> str:
    """Skleja linie sekcji i przycina puste brzegi (spójny wynik dla pustych/wielolinijkowych)."""
    return "\n".join(lines).strip()


@dataclass(slots=True)
class VisionClient:
    """Klient gatewaya VLM: obraz slajdu + trasa → surowa analiza (TYTUŁ/TEKST/OPIS). Qt-free."""

    config: VisionConfig
    transport: Transport = field(default=default_transport)

    def analyze(
        self, image_path: Path, route: ModelRoute, *, prompt: str = SLIDE_ANALYSIS_PROMPT
    ) -> str:
        """Prymityw NISKOPOZIOMOWY: obraz + (dowolny) ``prompt`` → SUROWY tekst modelu (bez pól).

        To warstwa transportu VLM: dowolna instrukcja przez ``prompt``, zwrot niesparsowanej treści.
        Użyj, gdy potrzebujesz innej instrukcji niż analiza slajdu albo surowej odpowiedzi.
        Dla standardowej analizy slajdu (TYTUŁ/TEKST/OPIS → pola) wołaj :meth:`analyze_slide`.
        Reużywa wspólny rdzeń transportu (:func:`~core.ai.gateway.post_chat`) i ekstraktor treści
        (:func:`~core.ai.gateway.parse_chat_content`) — ta sama detekcja pustej treści/ucięcia co
        w streszczeniach (qwen3-vl bez ``/no_think`` zjada budżet na rozumowanie).
        """
        payload = build_vision_request(image_path, prompt, route, self.config)
        data = post_chat(
            payload,
            base_url=self.config.base_url,
            transport=self.transport,
            api_key=self.config.api_key,
            timeout=self.config.timeout,
        )
        return parse_chat_content(data, max_tokens=self.config.max_tokens)

    def analyze_slide(self, image_path: Path, route: ModelRoute) -> SlideAnalysis:
        """Prymityw WYSOKOPOZIOMOWY (używany przez handler notatek): stały prompt analizy slajdu →
        parsowanie do pól :class:`SlideAnalysis` (TYTUŁ/TEKST/OPIS). Różni się od :meth:`analyze`
        tym, że NIE przyjmuje własnego promptu i zwraca STRUKTURĘ, nie surowy tekst.
        """
        return parse_slide_analysis(self.analyze(image_path, route))
