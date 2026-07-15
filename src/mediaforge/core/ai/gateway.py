"""Wspólny rdzeń transportu gatewaya LiteLLM (Qt-free, ``urllib`` ze stdlib).

TWARDA GRANICA: aplikacja rozmawia WYŁĄCZNIE z gatewayem LiteLLM (jedno OpenAI-kompatybilne
API). ZERO SDK dostawców, ZERO kluczy API dostawców w aplikacji — routing wrażliwości i klucze
mieszkają w konfiguracji gatewaya. Jedyny dopuszczalny sekret po stronie mediaforge to
opcjonalny master key gatewaya (keyring, nie plaintext).

Rdzeń wydzielony ze :mod:`core.ai.summarize`, gdy pojawił się DRUGI konsument gatewaya
(:mod:`core.ai.vision` — analiza slajdów VLM). Jeden transport, jeden podział błędów
(**timeout** „gateway wolno liczy" vs **połączenie** „gatewaya nie ma"), jedna detekcja
ucięcia (``completion_tokens >= max_tokens``) i jeden ekstraktor treści dla wszystkich klientów.
Obie funkcje ``post_chat``/``parse_chat_content`` są czyste i testowalne bez sieci (transport
wstrzykiwany).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

# Transport wstrzykiwalny (seam do testów bez sieci): URL + body + nagłówki + timeout → bajty.
Transport = Callable[[str, bytes, Mapping[str, str], float], bytes]

_CHAT_PATH = "/v1/chat/completions"


class GatewayError(Exception):
    """Gateway niedostępny lub zwrócił nieużyteczną odpowiedź (czytelny komunikat do jobs)."""


def endpoint(base_url: str) -> str:
    """URL chat-completions gatewaya, odporny na trailing slash w ``base_url``."""
    return f"{base_url.rstrip('/')}{_CHAT_PATH}"


def default_transport(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> bytes:
    """Domyślny transport POST przez ``urllib.request`` (stdlib, bez zewnętrznych SDK)."""
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        raw = resp.read()
    return bytes(raw)


def _is_timeout(exc: BaseException) -> bool:
    """Czy błąd transportu to timeout (a nie zerwane/odrzucone połączenie).

    ``socket.timeout`` jest aliasem ``TimeoutError`` (3.11+), więc timeout wystawia się albo
    bezpośrednio, albo opakowany w ``urllib.error.URLError`` (wtedy w ``reason``). Rozpoznajemy
    oba, by oddzielić „gateway wolno liczy" od „gatewaya nie ma" (connection refused).
    """
    if isinstance(exc, TimeoutError):
        return True
    return isinstance(getattr(exc, "reason", None), TimeoutError)


def _headers(api_key: str | None) -> dict[str, str]:
    """Nagłówki żądania: JSON + opcjonalny Bearer (master key gatewaya z keyring)."""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def post_chat(
    payload: Mapping[str, Any],
    *,
    base_url: str,
    transport: Transport,
    api_key: str | None = None,
    timeout: float,
) -> Any:
    """POST gotowego payloadu chat-completions; zwrot JSON-a albo :class:`GatewayError`.

    Wspólny rdzeń transportu WSZYSTKICH klientów gatewaya (streszczenia, VLM). Payload buduje
    wołający (kształt zależny od zadania: tekst vs obraz), tu tylko enkodujemy, wysyłamy i
    parsujemy JSON. Dwa powody błędu transportu rozdzielamy, bo prowadzą do różnych działań
    użytkownika: **timeout** (gateway ciężko liczy długi materiał) → komunikat o limicie czasu z
    podpowiedzią „zwiększ timeout"; **błąd połączenia** (gateway nie wstał / zły endpoint) →
    komunikat NAZYWAJĄCY gateway (``Gateway niedostępny``). Mylenie ich sugerowało padnięty
    gateway, gdy ten tylko wolno liczył.
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    url = endpoint(base_url)
    try:
        raw = transport(url, body, _headers(api_key), timeout)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        if _is_timeout(exc):
            raise GatewayError(
                f"Żądanie przekroczyło limit czasu ({int(timeout)} s) — długi materiał na "
                "lokalnym modelu może potrzebować kilku minut; zwiększ timeout."
            ) from exc
        raise GatewayError(f"Gateway niedostępny ({base_url}): {exc}") from exc
    try:
        return json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise GatewayError(f"Gateway zwrócił niepoprawny JSON ({base_url}): {exc}") from exc


def budget_exhausted(data: Mapping[str, Any], max_tokens: int) -> bool:
    """Czy ``usage.completion_tokens`` osiągnął/przekroczył budżet (limit tokenów wyczerpany)."""
    usage = data.get("usage")
    if not isinstance(usage, Mapping):
        return False
    completion = usage.get("completion_tokens")
    return isinstance(completion, int) and completion >= max_tokens


def is_truncated(data: Any, max_tokens: int) -> bool:
    """Czy NIEpusta treść jest prawdopodobnie ucięta (osiągnięto limit ``max_tokens``).

    Jedyny wiarygodny sygnał to ``usage.completion_tokens >= max_tokens`` — ``finish_reason``
    przy tym ucięciu raportuje ``stop`` (zmierzone na żywo), więc na nim NIE polegamy. Wołane
    dopiero po :func:`parse_chat_content` (treść już niepusta), więc równość budżetu = ucięcie
    w trakcie pisania treści (nie: cały budżet zjedzony na rozumowanie — to odrzuca parser wyżej).
    """
    return isinstance(data, Mapping) and budget_exhausted(data, max_tokens)


def parse_chat_content(data: Any, *, max_tokens: int | None = None) -> str:
    """Wyciąga treść z odpowiedzi chat-completions albo rzuca :class:`GatewayError`.

    Obsługiwane przypadki błędne: odpowiedź z polem ``error`` (gateway zgłosił problem),
    śmieci/niepoprawny kształt (brak ``choices``/``message``) oraz pusta treść.

    Diagnostyka pustej treści (potrzebuje pełnej odpowiedzi, nie tylko ``choices``): gdy
    ``content`` jest pusty ORAZ ``usage.completion_tokens >= max_tokens`` (budżet wyczerpany),
    rzucamy komunikat o modelu rozumującym (qwen3 zjadł limit na rozumowanie zanim zaczął treść —
    reasoning_content, nie content). **Nie** opieramy się na ``finish_reason``: przy tym ucięciu
    raportuje ``stop`` mimo osiągnięcia limitu (zmierzone na żywo).
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
        if max_tokens is not None and budget_exhausted(data, max_tokens):
            raise GatewayError(
                f"Model zużył cały limit tokenów ({max_tokens}) na rozumowanie zanim zaczął "
                "treść — zwiększ limit tokenów albo zostaw prompt_suffix=/no_think."
            )
        raise GatewayError("Gateway zwrócił pustą treść odpowiedzi.")
    return content.strip()
