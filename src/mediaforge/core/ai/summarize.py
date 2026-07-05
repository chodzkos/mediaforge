"""Klient streszczeń przez gateway LiteLLM (Qt-free, transport ``urllib`` ze stdlib).

TWARDA GRANICA: aplikacja rozmawia WYŁĄCZNIE z gatewayem LiteLLM (jedno
OpenAI-kompatybilne API). ZERO SDK dostawców, ZERO kluczy API dostawców w aplikacji —
routing wrażliwości i klucze mieszkają w konfiguracji gatewaya. Jedyny dopuszczalny
sekret po stronie mediaforge to opcjonalny master key gatewaya (keyring, nie plaintext).

Podział odpowiedzialności (obie funkcje są czyste i testowalne bez sieci):

* :func:`build_summary_request` — składa payload chat-completions (model z :class:`ModelRoute`);
* :func:`parse_summary_response` — wyciąga treść albo rzuca :class:`GatewayError`
  (błąd gatewaya / śmieci / pusta treść).

Transport (:meth:`SummaryClient.summarize`) używa ``urllib.request`` ze stdlib; błąd
HTTP/połączenia zamienia na :class:`GatewayError` z URL-em gatewaya — użytkownik ma wiedzieć,
że zawiódł gateway, nie aplikacja.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mediaforge.core.ai.routing import ModelRoute
from mediaforge.core.ai.transcribe import parse_whisper_json

# Transport wstrzykiwalny (seam do testów bez sieci): URL + body + nagłówki + timeout → bajty.
Transport = Callable[[str, bytes, Mapping[str, str], float], bytes]

_CHAT_PATH = "/v1/chat/completions"


class GatewayError(Exception):
    """Gateway niedostępny lub zwrócił nieużyteczną odpowiedź (czytelny komunikat do jobs)."""


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
    timeout: float = 120.0  # lokalny model na dużym transkrypcie bywa wolny
    api_key: str | None = None
    # Sufiks system-promptu — dla qwen3 „/no_think" (soft-switch wyłączający tryb rozumowania,
    # żeby całość budżetu szła w treść). Konfigurowalny: przy zmianie modelu można wyczyścić ("").
    prompt_suffix: str = "/no_think"


def _endpoint(base_url: str) -> str:
    """URL chat-completions gatewaya, odporny na trailing slash w ``base_url``."""
    return f"{base_url.rstrip('/')}{_CHAT_PATH}"


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


def build_summary_request(text: str, route: ModelRoute, config: SummaryConfig) -> dict[str, Any]:
    """Składa payload chat-completions: model z ``route``, transkrypt jako wiadomość usera.

    Kształt zgodny z OpenAI/LiteLLM: ``model`` bierze się z trasy (lokalna/chmurowa),
    system-prompt niesie język streszczenia + ``config.prompt_suffix``, ``max_tokens`` z configu.
    """
    return {
        "model": route.model,
        "messages": [
            {"role": "system", "content": _system_prompt(config.language, config.prompt_suffix)},
            {"role": "user", "content": text},
        ],
        "max_tokens": config.max_tokens,
    }


def parse_summary_response(data: Any, *, max_tokens: int | None = None) -> str:
    """Wyciąga treść streszczenia z odpowiedzi gatewaya albo rzuca :class:`GatewayError`.

    Obsługiwane przypadki błędne: odpowiedź z polem ``error`` (gateway zgłosił problem),
    śmieci/niepoprawny kształt (brak ``choices``/``message``) oraz pusta treść.

    Diagnostyka pustej treści (potrzebuje pełnej odpowiedzi, nie tylko ``choices``): gdy
    ``content`` jest pusty ORAZ ``usage.completion_tokens >= max_tokens`` (budżet wyczerpany),
    rzucamy komunikat o modelu rozumującym (qwen3 zjadł limit na rozumowanie zanim zaczął
    treść — reasoning_content, nie content). **Nie** opieramy się na ``finish_reason``: przy
    tym ucięciu raportuje ``stop`` mimo osiągnięcia limitu (zmierzone na żywo).
    """
    if not isinstance(data, Mapping):
        raise GatewayError(f"Gateway zwrócił nieoczekiwany kształt: {type(data).__name__}")
    if "error" in data:
        raise GatewayError(f"Gateway zgłosił błąd: {data['error']}")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise GatewayError("Gateway nie zwrócił żadnej odpowiedzi (brak 'choices').")
    message = choices[0].get("message") if isinstance(choices[0], Mapping) else None
    content = message.get("content") if isinstance(message, Mapping) else None
    if not isinstance(content, str) or not content.strip():
        if max_tokens is not None and _budget_exhausted(data, max_tokens):
            raise GatewayError(
                f"Model zużył cały limit tokenów ({max_tokens}) na rozumowanie zanim zaczął "
                "streszczenie — zwiększ summary_max_tokens albo zostaw summary_prompt_suffix"
                "=/no_think."
            )
        raise GatewayError("Gateway zwrócił pustą treść streszczenia.")
    return content.strip()


def _budget_exhausted(data: Mapping[str, Any], max_tokens: int) -> bool:
    """Czy ``usage.completion_tokens`` osiągnął/przekroczył budżet (limit tokenów wyczerpany)."""
    usage = data.get("usage")
    if not isinstance(usage, Mapping):
        return False
    completion = usage.get("completion_tokens")
    return isinstance(completion, int) and completion >= max_tokens


def read_transcript_text(transcript_json: Path) -> str:
    """Wyciąga pełny tekst z transkryptu whisper.cpp (``--output-json``) — wejście streszczenia.

    Współdzieli parser z :mod:`core.ai.transcribe` (jedno źródło formatu whisper.cpp),
    łączy tekst wszystkich segmentów. Pusty/zepsuty transkrypt daje pusty string —
    handler odmawia streszczenia zawczasu (status transkryptu), więc tu nie dublujemy walidacji.
    """
    data = json.loads(transcript_json.read_text(encoding="utf-8"))
    return parse_whisper_json(data).text


def _default_transport(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> bytes:
    """Domyślny transport POST przez ``urllib.request`` (stdlib, bez zewnętrznych SDK)."""
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        raw = resp.read()
    return bytes(raw)


@dataclass(slots=True)
class SummaryClient:
    """Klient gatewaya: transkrypt + trasa → streszczenie (Markdown). Transport wstrzykiwalny."""

    config: SummaryConfig
    transport: Transport = field(default=_default_transport)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    def summarize(self, text: str, route: ModelRoute) -> str:
        """POST do gatewaya i zwrot treści; błąd HTTP/połączenia → :class:`GatewayError` z URL-em.

        Komunikat błędu NAZYWA gateway (``Gateway niedostępny (http://…): …``), żeby użytkownik
        wiedział, że zawiódł gateway (nie uruchomiony / zły endpoint), a nie aplikacja.
        """
        payload = build_summary_request(text, route, self.config)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        url = _endpoint(self.config.base_url)
        try:
            raw = self.transport(url, body, self._headers(), self.config.timeout)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise GatewayError(f"Gateway niedostępny ({self.config.base_url}): {exc}") from exc
        try:
            data = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise GatewayError(
                f"Gateway zwrócił niepoprawny JSON ({self.config.base_url}): {exc}"
            ) from exc
        return parse_summary_response(data, max_tokens=self.config.max_tokens)
