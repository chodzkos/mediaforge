"""Wspólna konfiguracja testów — wymusza platformę Qt ``offscreen`` (brak displaya)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
