"""Enumeracja urządzeń audio DirectShow (dshow) dla nagrywania — parser + wrapper.

ffmpeg na Windows nie ma natywnego wejścia WASAPI: dźwięk systemowy idzie przez dshow i
wymaga urządzenia zdolnego do loopbacku (Stereo Mix / wirtualny kabel typu VB-Cable).
Sama enumeracja takiego urządzenia NIE tworzy — GUI ostrzega, gdy żadnego nie ma.

Parser jest czystą funkcją (testowalną na każdym OS); wrapper woła ffmpeg (Windows-only).
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

# Wzorce nazw loopback (lowercase, substring). Uwaga na lokalizacje Windows.
_LOOPBACK_HINTS: tuple[str, ...] = (
    "stereo mix",
    "miks stereo",
    "stereomix",
    "stereomischung",
    "mixage stéréo",
    "what u hear",
    "wave out",
    "loopback",
    "cable output",
    "vb-audio",
    "voicemeeter",
    "virtual-audio-capturer",
)
_DEVICE_RE = re.compile(r'"(?P<name>[^"]+)"\s+\((?P<kind>audio|video)\)')
_ALT_RE = re.compile(r'Alternative name\s+"(?P<alt>[^"]+)"')


@dataclass(frozen=True)
class DshowAudioDevice:
    """Urządzenie audio dshow z metadanymi do wyboru w GUI."""

    name: str  # przyjazna nazwa (do wyświetlenia)
    alt_name: str  # "@device_cm_..." — UŻYWAJ w -i audio= (jednoznaczna); może być ""
    is_loopback: bool


def _is_loopback(name: str) -> bool:
    low = name.lower()
    return any(hint in low for hint in _LOOPBACK_HINTS)


def parse_dshow_audio_devices(ffmpeg_stderr: str) -> list[DshowAudioDevice]:
    """Parsuje wyjście `ffmpeg -list_devices true -f dshow -i dummy` → urządzenia AUDIO."""
    devices: list[DshowAudioDevice] = []
    pending: str | None = None
    for line in ffmpeg_stderr.splitlines():
        dev = _DEVICE_RE.search(line)
        if dev:
            if pending is not None:
                devices.append(DshowAudioDevice(pending, "", _is_loopback(pending)))
                pending = None
            if dev.group("kind") == "audio":
                pending = dev.group("name")
            continue
        alt = _ALT_RE.search(line)
        if alt and pending is not None:
            devices.append(DshowAudioDevice(pending, alt.group("alt"), _is_loopback(pending)))
            pending = None
    if pending is not None:
        devices.append(DshowAudioDevice(pending, "", _is_loopback(pending)))
    return devices


def list_dshow_audio_devices(ffmpeg: str = "ffmpeg") -> list[DshowAudioDevice]:
    """Uruchamia ffmpeg → urządzenia audio dshow. Tylko Windows. Odporne na brak ffmpeg.

    ffmpeg wypisuje listę na STDERR i kończy się != 0 ('dummy' to nie urządzenie) — OK.
    """
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return []
    return parse_dshow_audio_devices(proc.stderr or "")
