"""Sekrety przez systemowy magazyn poświadczeń (keyring).

Tu trafiają: klucze API dostawców chmury (per provider), token HF (pyannote),
token bota Telegram. NIGDY nie loguj wartości i nie zapisuj ich w configu/repo.
gui-kit nie obejmuje sekretów — to warstwa wyłącznie mediaforge.
"""

from __future__ import annotations

import keyring
from keyring.errors import PasswordDeleteError

_SERVICE = "mediaforge"


def get_secret(key: str) -> str | None:
    return keyring.get_password(_SERVICE, key)


def set_secret(key: str, value: str) -> None:
    keyring.set_password(_SERVICE, key, value)


def delete_secret(key: str) -> None:
    try:
        keyring.delete_password(_SERVICE, key)
    except PasswordDeleteError:
        pass  # już nie istnieje — idempotentnie OK
