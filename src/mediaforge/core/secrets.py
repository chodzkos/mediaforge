"""Sekrety przez systemowy magazyn poświadczeń (keyring).

Tu trafiają: klucze API dostawców chmury (per provider), token HF (pyannote),
token bota Telegram. NIGDY nie loguj wartości i nie zapisuj ich w configu/repo.
gui-kit nie obejmuje sekretów — to warstwa wyłącznie mediaforge.

Nazewnictwo kluczy jest ujednolicone (jeden namespace ``_SERVICE``), żeby
„wyczyść sesję i dane logowania" (LEGAL_BOUNDARIES) mógł je hurtowo skasować:

* API dostawcy chmury → ``provider_api_key_name(provider)`` → ``"api_key:<provider>"``;
* token Hugging Face (pyannote, gated) → :data:`HF_TOKEN_KEY`;
* token bota Telegram → :data:`TELEGRAM_TOKEN_KEY`.
"""

from __future__ import annotations

import contextlib

import keyring
from keyring.errors import PasswordDeleteError

_SERVICE = "mediaforge"

# Prefiks kluczy API dostawców — wspólny, by dało się je wylistować/wyczyścić.
_API_KEY_PREFIX = "api_key:"

# Stałe nazwy pojedynczych sekretów (poza rejestrem dostawców).
HF_TOKEN_KEY = "hf_token"  # gated pyannote — token Hugging Face
TELEGRAM_TOKEN_KEY = "telegram_bot_token"
# Master key gatewaya LiteLLM — JEDYNY klucz mieszkający w mediaforge (opcjonalny; klucze
# dostawców trzyma config gatewaya, nie aplikacja). W keyring, nigdy w configu/plaintext.
GATEWAY_MASTER_KEY = "litellm_master_key"


def provider_api_key_name(provider: str) -> str:
    """Zwraca nazwę klucza keyring dla API danego dostawcy (np. ``api_key:openai``)."""
    return f"{_API_KEY_PREFIX}{provider}"


def get_secret(key: str) -> str | None:
    """Czyta sekret z keyring (``None``, gdy brak)."""
    return keyring.get_password(_SERVICE, key)


def set_secret(key: str, value: str) -> None:
    """Zapisuje sekret do keyring."""
    keyring.set_password(_SERVICE, key, value)


def delete_secret(key: str) -> None:
    """Usuwa sekret (idempotentnie — brak klucza nie jest błędem)."""
    with contextlib.suppress(PasswordDeleteError):
        keyring.delete_password(_SERVICE, key)


def get_provider_api_key(provider: str) -> str | None:
    """Czyta klucz API dostawcy chmury z keyring."""
    return get_secret(provider_api_key_name(provider))


def set_provider_api_key(provider: str, value: str) -> None:
    """Zapisuje klucz API dostawcy chmury do keyring."""
    set_secret(provider_api_key_name(provider), value)


def delete_provider_api_key(provider: str) -> None:
    """Usuwa klucz API dostawcy chmury z keyring (idempotentnie)."""
    delete_secret(provider_api_key_name(provider))
