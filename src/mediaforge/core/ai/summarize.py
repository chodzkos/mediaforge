"""Klient streszczeń przez gateway LiteLLM (Qt-free).

TWARDA GRANICA: aplikacja rozmawia WYŁĄCZNIE z gatewayem LiteLLM (jedno
OpenAI-kompatybilne API). ZERO SDK dostawców, ZERO kluczy API dostawców w aplikacji —
routing wrażliwości i klucze mieszkają w konfiguracji gatewaya. Jedyny dopuszczalny
sekret po stronie mediaforge to opcjonalny master key gatewaya (keyring, nie plaintext).

Podział odpowiedzialności (obie funkcje są czyste i testowalne bez sieci):

* :func:`build_summary_request` — składa payload chat-completions (model z :class:`ModelRoute`);
* :func:`parse_summary_response` — wyciąga treść albo rzuca :class:`GatewayError`
  (błąd gatewaya / śmieci / pusta treść). To alias wspólnego ekstraktora z :mod:`core.ai.gateway`.

Rdzeń transportu (POST, podział błędów **timeout** vs **połączenie**, parsowanie JSON, detekcja
ucięcia) jest wydzielony do :mod:`core.ai.gateway` — współdzielony z klientem VLM
(:mod:`core.ai.vision`). Tu zostaje logika specyficzna dla streszczeń: prompty, map-reduce,
:class:`SummaryClient`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mediaforge.core.ai.chunking import Chunk, split_segments
from mediaforge.core.ai.gateway import (
    GatewayError,
    Transport,
    default_transport,
    is_truncated,
    parse_chat_content,
    post_chat,
)
from mediaforge.core.ai.routing import ModelRoute
from mediaforge.core.ai.transcribe import Segment, parse_whisper_json

# Re-eksport rdzenia transportu (wydzielony do :mod:`core.ai.gateway`): stabilne API dla
# handlerów i testów, które importują te nazwy z ``summarize``. ``parse_summary_response`` to
# ekstraktor treści chat-completions — dla streszczeń zachowana dotychczasowa nazwa.
parse_summary_response = parse_chat_content
__all__ = [
    "GatewayError",
    "Transport",
    "is_truncated",
    "parse_summary_response",
]

# Adnotacja doklejana przy prawdopodobnym ucięciu treści (limit summary_max_tokens osiągnięty).
# Ucięte streszczenie jest wciąż użyteczne — NIE wywalamy joba, tylko oznaczamy je w pliku.
TRUNCATED_MARK = "⚠ [treść mogła zostać ucięta]"


@dataclass(frozen=True, slots=True)
class SummaryResult:
    """Wynik jednego wywołania gatewaya: treść + flaga prawdopodobnego ucięcia (do adnotacji)."""

    text: str
    truncated: bool


@dataclass(slots=True)
class SummaryConfig:
    """Ustawienia transportu/formatu streszczenia (modele wybiera routing, nie ten config).

    ``base_url`` to endpoint gatewaya LiteLLM. ``api_key`` to OPCJONALNY master key gatewaya
    (z keyring) — jedyny sekret w aplikacji; ``None`` = gateway bez autoryzacji. Modele
    (lokalny/chmurowy) NIE są tu — rozstrzyga je :func:`~mediaforge.core.ai.routing.resolve_route`.
    """

    base_url: str
    language: str = "pl"
    # 4096: modele rozumujące (qwen3) zjadają część budżetu na wewnętrzne rozumowanie zanim
    # zaczną treść — za mały limit kończy się pustym content (patrz parse_summary_response).
    max_tokens: int = 4096
    # 600 s: 2-godzinny transkrypt to kilkuminutowy prefill+generacja na lokalnym 27b — krótszy
    # timeout padał w połowie pracy (patrz podział błędów transportu w summarize).
    timeout: float = 600.0
    api_key: str | None = None
    # Sufiks system-promptu — dla qwen3 „/no_think" (soft-switch wyłączający tryb rozumowania,
    # żeby całość budżetu szła w treść). Konfigurowalny: przy zmianie modelu można wyczyścić ("").
    prompt_suffix: str = "/no_think"


def _system_prompt(language: str, suffix: str = "") -> str:
    """Instrukcja systemowa: rzeczowe streszczenie w zadanym języku, w Markdown.

    ``suffix`` (np. ``/no_think`` dla qwen3) doklejamy na końcu — soft-switch wyłączający
    tryb rozumowania modelu. Pusty ``suffix`` = brak dopisku (przy modelu nierozumującym).
    """
    base = (
        f"Jesteś asystentem, który tworzy zwięzłe, rzeczowe streszczenia w języku {language}. "
        "Zachowaj najważniejsze tezy, definicje i wnioski. Zwróć wynik w formacie Markdown."
    )
    return f"{base} {suffix}" if suffix else base


def build_summary_request(
    text: str, route: ModelRoute, config: SummaryConfig, *, max_tokens: int | None = None
) -> dict[str, Any]:
    """Składa payload chat-completions: model z ``route``, transkrypt jako wiadomość usera.

    Kształt zgodny z OpenAI/LiteLLM: ``model`` bierze się z trasy (lokalna/chmurowa),
    system-prompt niesie język streszczenia + ``config.prompt_suffix``. ``max_tokens`` z configu,
    chyba że jawnie nadpisany — faza reduce ma osobny, większy budżet (patrz :meth:`run`).
    """
    return {
        "model": route.model,
        "messages": [
            {"role": "system", "content": _system_prompt(config.language, config.prompt_suffix)},
            {"role": "user", "content": text},
        ],
        "max_tokens": config.max_tokens if max_tokens is None else max_tokens,
    }


def read_transcript_text(transcript_json: Path) -> str:
    """Wyciąga pełny tekst z transkryptu whisper.cpp (``--output-json``) — wejście streszczenia.

    Współdzieli parser z :mod:`core.ai.transcribe` (jedno źródło formatu whisper.cpp),
    łączy tekst wszystkich segmentów. Pusty/zepsuty transkrypt daje pusty string —
    handler odmawia streszczenia zawczasu (status transkryptu), więc tu nie dublujemy walidacji.
    """
    data = json.loads(transcript_json.read_text(encoding="utf-8"))
    return parse_whisper_json(data).text


def read_transcript_segments(transcript_json: Path) -> list[Segment]:
    """Wczytuje SEGMENTY transkryptu whisper.cpp (start/end/text) — wejście dzielnika map-reduce.

    W przeciwieństwie do :func:`read_transcript_text` (sklejony tekst) zwraca segmenty z
    granicami czasu, których potrzebuje :func:`~mediaforge.core.ai.chunking.split_segments`,
    by ciąć materiał wyłącznie na granicach zdań i znać zakres czasu każdego kawałka.
    """
    data = json.loads(transcript_json.read_text(encoding="utf-8"))
    return list(parse_whisper_json(data).segments)


# ── Map-reduce: prompty per kawałek + hierarchiczny reduce (Qt-free, testowalne) ──────


def _hhmm(seconds: float) -> str:
    """Znacznik czasu ``HH:MM`` z sekund (na nagłówki czasowe kawałków; ujemne -> 00:00)."""
    total = max(0, int(seconds))
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}"


def _range(start: float, end: float) -> str:
    """Zakres czasu kawałka ``HH:MM-HH:MM`` (zwykły łącznik ASCII — bez confusables w RUF)."""
    return f"{_hhmm(start)}-{_hhmm(end)}"


def map_prompt(index: int, total: int, chunk: Chunk) -> str:
    """Wiadomość usera dla fazy MAP: streszczenie pojedynczego fragmentu (z jego zakresem czasu).

    System-prompt (język + ``prompt_suffix``) jest ten sam co w ścieżce pojedynczej — tu tylko
    treść usera niesie kontekst „który to fragment i z jakiego czasu", by model nie mylił porządku.
    """
    return (
        f"Fragment {index}/{total} wykładu (czas {_range(chunk.start, chunk.end)}):\n\n"
        f"{chunk.text}\n\n"
        "Streść ten fragment po polsku: kluczowe tezy, 1-3 akapity."
    )


def reduce_prompt(body: str) -> str:
    """Wiadomość usera dla fazy REDUCE: sklej streszczenia cząstkowe w jedno spójne.

    Prompt niesie CEL DŁUGOŚCI — bez niego model próbuje zachować wszystko z fragmentów
    (konkatenacja) i z definicji dobija każdego capa. „Syntetyzuj, nie sklejaj" celuje w
    streszczenie, nie sumę streszczeń.
    """
    return (
        "Poniżej streszczenia kolejnych fragmentów wykładu. Połącz je w jedno spójne "
        "streszczenie po polsku (bez powtórzeń, zachowaj chronologię). "
        "Docelowa długość: 6-12 akapitów — syntetyzuj, nie sklejaj; pomijaj powtórzenia "
        "między fragmentami.\n\n"
        f"{body}"
    )


# Bezpieczny sufit okna kontekstu (num_ctx 32k z marginesem na system-prompt + narzut czatu).
# Guard reduce trzyma prompt + wyjście poniżej tej granicy, żeby długi prompt nie wypadł za okno.
SAFE_CONTEXT_TOKENS = 30_000


def estimate_tokens(text: str) -> int:
    """Zgrubna estymata tokenów promptu (~3 znaki/token) — do guardu okna, nie do rozliczeń."""
    return len(text) // 3


def fit_reduce_budget(
    prompt: str, reduce_max_tokens: int, *, safe_ctx: int = SAFE_CONTEXT_TOKENS
) -> int:
    """Przycina budżet wyjścia reduce, by ``prompt + wyjście`` zmieściło się w oknie.

    Zwraca ``min(reduce_max_tokens, okno - tokeny_promptu)``, nie mniej niż 1. Lepiej krótsze
    wyjście niż ciche wypadnięcie promptu za ``num_ctx`` (model urwałby wtedy WEJŚCIE, nie tylko
    wyjście). Wołający porównuje wynik z ``reduce_max_tokens`` i loguje warning, jeśli przyciął.
    """
    room = safe_ctx - estimate_tokens(prompt)
    return min(reduce_max_tokens, max(1, room))


def part_section(index: int, total: int, chunk: Chunk, text: str, *, truncated: bool) -> str:
    """Sekcja pliku ``summary_parts.md`` dla jednego kawałka (nagłówek czasowy + treść).

    Praca częściowa jest zapisywana po każdym kawałku — przy ``truncated`` doklejamy adnotację,
    żeby czytelnik wiedział, że ta akurat sekcja mogła się urwać na limicie tokenów.
    """
    header = f"## Część {index}/{total} ({_range(chunk.start, chunk.end)})"
    body = f"{text}\n\n{TRUNCATED_MARK}" if truncated else text
    return f"{header}\n\n{body}\n"


def _labeled(seg: Segment) -> Segment:
    """Segment z tekstem poprzedzonym znacznikiem czasu — nagłówek przetrwa sklejenie w reduce."""
    return Segment(start=seg.start, end=seg.end, text=f"[{_range(seg.start, seg.end)}] {seg.text}")


def reduce_parts(
    parts: list[Segment],
    *,
    chunk_chars: int,
    call: Callable[[str], SummaryResult],
) -> tuple[SummaryResult, int]:
    """Hierarchiczny reduce: łączy streszczenia cząstkowe aż zostanie jeden wynik.

    Każdy ``Segment`` w ``parts`` niesie streszczenie fragmentu (``text``) i jego zakres czasu
    (``start``/``end``). Grupowanie robi TEN SAM :func:`split_segments` co przy mapie (zero nowej
    logiki podziału): gdy sklejone streszczenia mieszczą się w ``chunk_chars`` -> jeden finalny
    reduce; gdy nie -> runda pośrednia (reduce każdej grupy) i powtórka nad krótszymi wynikami.

    ``call`` dostaje gotową wiadomość usera (przez :func:`reduce_prompt`) i zwraca wynik z flagą
    ucięcia. Zwraca finalny :class:`SummaryResult` oraz liczbę wykonanych wywołań reduce (do
    rozliczenia postępu). Pętla jest ograniczona liczbą części + zapas — streszczenia z każdą
    rundą krótsze, więc zbiega; limit tylko zabezpiecza przed patologicznym brakiem zbieżności.
    """
    labeled = [_labeled(p) for p in parts]
    calls = 0
    for _ in range(len(parts) + 2):
        groups = split_segments(labeled, chunk_chars)
        if len(groups) <= 1:
            body = groups[0].text if groups else ""
            return call(reduce_prompt(body)), calls + 1
        next_level: list[Segment] = []
        for group in groups:
            res = call(reduce_prompt(group.text))
            calls += 1
            next_level.append(Segment(start=group.start, end=group.end, text=res.text))
        labeled = [_labeled(s) for s in next_level]
    # Brak zbieżności (patologiczne wejście) -> jeden finalny reduce nad wszystkim, co zostało.
    body = " ".join(s.text for s in labeled)
    return call(reduce_prompt(body)), calls + 1


def summary_start_line(
    char_count: int, model: str, timeout: float, *, chunks: int | None = None
) -> str:
    """Linia diagnostyczna startu streszczenia (do LogView): rozmiar wejścia, model, timeout.

    Rozmiar w tysiącach znaków (``~N tys.``) — następna diagnoza długiego materiału ma liczby
    od ręki, bez ręcznego liczenia transkryptu. ``chunks`` (gdy > 1) sygnalizuje ścieżkę
    map-reduce: dokłada „X części", a timeout jest liczony NA WYWOŁANIE (długi materiał idzie
    wieloma requestami). Brak ``chunks`` = ścieżka pojedyncza (dawny format zachowany 1:1).
    """
    if chunks is not None and chunks > 1:
        return (
            f"Streszczanie: ~{char_count // 1000} tys. znaków transkryptu, {chunks} części, "
            f"model {model}, timeout {int(timeout)} s/wywołanie"
        )
    return (
        f"Streszczanie: ~{char_count // 1000} tys. znaków transkryptu, "
        f"model {model}, timeout {int(timeout)} s"
    )


@dataclass(slots=True)
class SummaryClient:
    """Klient gatewaya: transkrypt + trasa → streszczenie (Markdown). Transport wstrzykiwalny."""

    config: SummaryConfig
    transport: Transport = field(default=default_transport)

    def _post(self, user_content: str, route: ModelRoute, *, max_tokens: int | None = None) -> Any:
        """POST wiadomości usera przez wspólny rdzeń :func:`~core.ai.gateway.post_chat`.

        Wspólny rdzeń ścieżki pojedynczej (:meth:`summarize`) i map-reduce (:meth:`run`).
        ``max_tokens`` (gdy podany) nadpisuje budżet wyjścia z configu — faza reduce ma osobny.
        Podział błędów transportu (timeout vs połączenie) i parsowanie JSON są w ``post_chat``.
        """
        payload = build_summary_request(user_content, route, self.config, max_tokens=max_tokens)
        return post_chat(
            payload,
            base_url=self.config.base_url,
            transport=self.transport,
            api_key=self.config.api_key,
            timeout=self.config.timeout,
        )

    def summarize(self, text: str, route: ModelRoute) -> str:
        """POST transkryptu i zwrot treści — ścieżka POJEDYNCZA (jeden request, bez reduce).

        Zachowanie identyczne jak dotąd: krótki materiał (jeden kawałek) idzie tędy 1:1, więc
        stary format ``summary.md`` i błędy transportu nie zmieniają się.
        """
        return parse_summary_response(self._post(text, route), max_tokens=self.config.max_tokens)

    def run(
        self, user_content: str, route: ModelRoute, *, max_tokens: int | None = None
    ) -> SummaryResult:
        """POST dowolnej wiadomości (map/reduce) → treść + flaga ucięcia (do adnotacji plików).

        Jak :meth:`summarize`, ale zwraca też informację o prawdopodobnym ucięciu treści na
        limicie budżetu (:func:`is_truncated`) — map-reduce oznacza takie sekcje w
        ``summary_parts.md`` / ``summary.md`` zamiast wywalać job (ucięte streszczenie jest wciąż
        użyteczne). ``max_tokens`` nadpisuje budżet z configu (faza reduce ma osobny, większy) —
        detekcja ucięcia liczona jest wobec REALNIE użytego budżetu, nie domyślnego z configu.
        """
        budget = self.config.max_tokens if max_tokens is None else max_tokens
        data = self._post(user_content, route, max_tokens=max_tokens)
        text = parse_summary_response(data, max_tokens=budget)
        return SummaryResult(text=text, truncated=is_truncated(data, budget))
