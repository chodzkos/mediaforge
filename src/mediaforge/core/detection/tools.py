"""Detekcja narzędzi CLI — `probe_tool` (forward-looking) + sondy mediaforge.

KONTRAKT `probe_tool` = NADZBIÓR pakietowego `chodzkos-detection.probe_tool` (v0.1.1):
pakiet zwraca ``{available, version}``; my dodajemy ``path`` (Path | None), którego pakiet
jeszcze nie ma, a którego potrzebujemy do override/subprocess. Te same nazwy pól tam, gdzie
pakiet sięga → zgodność nazewnicza; ``path`` jako rozszerzenie. Gdy pakiet dostanie wzbogacony
`probe_tool` (`_make_tool` z EpubForge: path + fallback na katalogi instalacji), PODMIEŃ ciało
`probe_tool` na import z pakietu — reszta sond się nie zmienia.

Rozbieżność do uzgodnienia przy wzbogacaniu pakietu: pakietowy `probe_tool` nie ma `path`
ani fallbacku na katalogi; EpubForge `_make_tool` ma oba. Po upstreamie `_make_tool` wszyscy
(pdf2md/EpubForge/mediaforge) zejdą się na jednym bogatym kontrakcie.

`probe_tool`/`command_in_path`/`api_key_present` = generyczne → docelowo do chodzkos-detection.
`check_ffmpeg`/`check_whispercpp`/`check_ytdlp`/`check_litellm`/`check_providers` = mediaforge.
Wszystko odporne na brak narzędzia. `detect_version` (flaga EpubForge) pominięta — ffmpeg/
whisper.cpp/yt-dlp mają nieszkodliwe `--version`.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from mediaforge.core.winutil import NO_WINDOW_FLAGS

_TIMEOUT = 5
# Sonda enkodera koduje 1 klatkę — zwykle ~0,5 s, ale zimny NVENC/ładowanie sterownika bywa
# wolniejsze; 15 s to bezpieczny sufit (i tak wołane w tle / w doctorze, nie na wątku UI).
_ENCODER_PROBE_TIMEOUT = 15
# Rozmiar wejścia sondy. NIE zmniejszać: 64x64 dawało FAŁSZYWY NEGATYW NVENC — h264_nvenc
# wymaga wejścia rzędu ≥145 px szerokości, poniżej którego inicjalizacja pada na ZDROWYM
# enkoderze (returncode≠0 → mylące „✗"). 640x360 ma bezpieczny margines ponad minima wszystkich
# rodzin (nvenc/amf/qsv). Zmierzone: 64x64 → rc≠0, 640x360 → rc 0 (RTX 5090, sterownik 610.62).
_ENCODER_PROBE_SIZE = "640x360"


def _run(cmd: list[str], timeout: int = _TIMEOUT) -> str:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=NO_WINDOW_FLAGS,
        )
        return proc.stdout or proc.stderr or ""
    except Exception:
        return ""


def _run_capture(cmd: list[str], timeout: int) -> tuple[int, str]:
    """Uruchamia komendę → ``(returncode, stderr)``. ``(127, "")`` gdy nie ruszyła / timeout."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=NO_WINDOW_FLAGS,
        )
        return proc.returncode, proc.stderr or ""
    except Exception:
        return 127, ""


def _stderr_tail(stderr: str, lines: int = 2) -> str:
    """Ostatnie ``lines`` niepustych linii stderr (realny powód porażki do doctora)."""
    tail = [ln.strip() for ln in stderr.strip().splitlines() if ln.strip()]
    return " | ".join(tail[-lines:])


def _encoder_probe_cmd(name: str, ffmpeg: str) -> list[str]:
    """Komenda kodująca 1 klatkę ``testsrc`` do ``null`` — próba REALNEJ inicjalizacji enkodera."""
    return [
        ffmpeg,
        "-hide_banner",
        "-f",
        "lavfi",
        "-i",
        f"testsrc=duration=0.1:size={_ENCODER_PROBE_SIZE}:rate=30",
        "-frames:v",
        "1",
        "-c:v",
        name,
        "-f",
        "null",
        "-",
    ]


@dataclass(frozen=True)
class ProbeResult:
    """Wynik sondy enkodera: czy działa + ogon stderr przy porażce (diagnostyka, nie zgadywanie)."""

    available: bool
    stderr_tail: str = ""  # ostatnie linie stderr, tylko gdy available=False


@lru_cache(maxsize=32)
def probe_encoder_result(name: str, ffmpeg: str = "ffmpeg") -> ProbeResult:
    """Empiryczna sonda: czy enkoder REALNIE inicjalizuje się w runtime (nie tylko jest w buildzie).

    Listing ``ffmpeg -encoders`` mówi tylko, że enkoder wkompilowano — inicjalizacja może paść:
    za stary sterownik (FFmpeg 8.x wymaga NVIDIA ≥610) albo brak wsparcia w krzemie (``av1_nvenc``
    na Pascalu). Kodujemy 1 klatkę ``testsrc`` do ``null`` i patrzymy na kod wyjścia — ta sama
    filozofia co :func:`core.ai.transcribe.detect_whisper_runtime`: EMPIRYKA zamiast progów.
    Werdykt WYŁĄCZNIE po ``returncode`` — warning na stderr przy rc=0 to nadal sukces.

    Wynik cache'owany (``lru_cache``): sonda ~0,5 s/enkoder, a odpowiedź jest stała w obrębie
    procesu (sprzęt/sterownik się nie zmienia). Testy czyszczą cache przez ``cache_clear()``.
    """
    rc, stderr = _run_capture(_encoder_probe_cmd(name, ffmpeg), _ENCODER_PROBE_TIMEOUT)
    if rc == 0:
        return ProbeResult(True)
    return ProbeResult(False, _stderr_tail(stderr))


def probe_encoder(name: str, ffmpeg: str = "ffmpeg") -> bool:
    """Bool-owe podsumowanie sondy (zgodność wsteczna). Szczegóły: :func:`probe_encoder_result`."""
    return probe_encoder_result(name, ffmpeg).available


# ───────────── Prymitywy generyczne (→ chodzkos-detection) ─────────────


def command_in_path(cmd: str) -> bool:
    """Czy narzędzie wiersza poleceń jest dostępne w PATH."""
    return shutil.which(cmd) is not None


def api_key_present(service: str, key: str) -> bool:
    """Czy klucz jest w keyring. Zwraca TYLKO bool — nigdy wartości sekretu."""
    try:
        import keyring

        return keyring.get_password(service, key) is not None
    except Exception:
        return False


def probe_tool(
    name: str,
    version_args: list[str] | None = None,
    version_parser: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """Generyczna sonda narzędzia CLI. Kontrakt: {available, version, path}.

    Na teraz: `shutil.which` (narzędzia CLI mediaforge zwykle w PATH). Bez fallbacku na katalogi
    — ten przyjdzie z wzbogaconym `probe_tool` pakietu (`_make_tool`), wtedy podmienimy ciało.
    """
    found = shutil.which(name)
    result: dict[str, Any] = {
        "available": found is not None,
        "version": "",
        "path": Path(found) if found else None,
    }
    if found and version_args:
        out = _run([name, *version_args])
        if out:
            line = out.splitlines()[0].strip()
            result["version"] = version_parser(out) if version_parser else line
    return result


# ───────────── Sondy mediaforge (ZOSTAJĄ; zbudowane na prymitywach) ─────────────


def _ffmpeg_version(out: str) -> str:
    parts = out.splitlines()[0].split()
    return parts[2] if len(parts) > 2 else ""


def check_ffmpeg(
    *, probe_encoders: bool = False, probe: Callable[[str], bool] | None = None
) -> dict[str, Any]:
    """ffmpeg: kontrakt probe_tool + warstwa mediaforge (enkodery NVENC/x264/x265 OBOK).

    ``encoders`` = obecność w BUILDZIE (listing ``-encoders``). ``encoders_usable`` = REALNA
    inicjalizacja w runtime (:func:`probe_encoder`) — enkoder w buildzie może paść (za stary
    sterownik NVIDIA, brak wsparcia w krzemie); wybór enkodera nagrania musi patrzeć na usable,
    nie na build (inaczej NVENC-widmo zabija nagranie zamiast zejść na libx264).

    Sonda runtime (``probe_encoders=True``) jest kosztowna (~0,5 s/enkoder), więc leniwa: doctor
    ją włącza, GUI woła ją w tle (sonda środowiska poza wątkiem UI). Bez niej ``encoders_usable``
    jest puste, a wołający spada na build-presence. ``probe`` = wstrzykiwana sonda (seam testów).
    """
    tool = probe_tool("ffmpeg", ["-hide_banner", "-version"], _ffmpeg_version)
    encoders: dict[str, bool] = {}
    encoders_usable: dict[str, bool] = {}
    encoder_probe_errors: dict[str, str] = {}  # nazwa → ogon stderr, tylko dla martwych w runtime
    if tool["available"]:
        enc = _run(["ffmpeg", "-hide_banner", "-encoders"])
        # NVENC (NVIDIA) + AMF (Radeon/APU AMD, np. 780M → h264_amf) + QSV (Intel iGPU); ta sama
        # sonda runtime co NVENC (obecność w buildzie ≠ realna inicjalizacja). libx264/libx265 obok.
        for name in (
            "h264_nvenc",
            "hevc_nvenc",
            "av1_nvenc",
            "h264_amf",
            "hevc_amf",
            "h264_qsv",
            "libx264",
            "libx265",
        ):
            encoders[name] = name in enc
        if probe_encoders:
            # Wstrzyknięty bool-``probe`` (seam testów) → ProbeResult bez stderr; realna sonda →
            # z ogonem stderr. Sonda TYLKO dla enkoderów obecnych w buildzie.
            def _result(name: str) -> ProbeResult:
                return ProbeResult(probe(name)) if probe is not None else probe_encoder_result(name)

            results = {n: _result(n) for n, present in encoders.items() if present}
            encoders_usable = {n: r.available for n, r in results.items()}
            encoder_probe_errors = {
                n: r.stderr_tail for n, r in results.items() if not r.available and r.stderr_tail
            }
    return {
        **tool,
        "encoders": encoders,
        "encoders_usable": encoders_usable,
        "encoder_probe_errors": encoder_probe_errors,
    }


def check_whispercpp(
    override_path: str | None = None, binary: str = "whisper-cli"
) -> dict[str, Any]:
    """whisper.cpp: override z configu (`whispercpp_path`) → fallback `shutil.which`.

    Override zaprojektowany OD RAZU: binarka bywa self-compiled poza PATH (np. build/bin/).
    Override przeżyje przyszłą migrację na wzbogacony `probe_tool` pakietu (jak override
    java/epubcheck w EpubForge żyje obok detekcji). Kontrakt: {available, version, path}.
    Fallback PATH celowo wąski (whisper-cli/whisper-cpp) — nazwy generyczne (main/whisper) dają
    false-positive; dla nietypowych lokalizacji służy override, nie zgadywanie nazw.
    """
    if override_path:
        p = Path(override_path)
        if p.exists():
            return {"available": True, "version": _whisper_version(p), "path": p}
    # Fallback PATH — TYLKO nazwy specyficzne dla whisper.cpp. CELOWO bez "whisper" (koliduje
    # z CLI openai-whisper — inne narzędzie) i bez "main" (zbyt generyczne: trafia przypadkowy
    # main/main.exe na PATH → false-positive; to wywróciło CI). Stary build z binarką "main"
    # albo binarka poza PATH → użyj override whispercpp_path.
    for cand in (binary, "whisper-cpp"):
        found = shutil.which(cand)
        if found:
            p = Path(found)
            return {"available": True, "version": _whisper_version(p), "path": p}
    return {"available": False, "version": "", "path": None}


def _whisper_version(path: Path) -> str:
    out = _run([str(path), "--version"])
    return out.splitlines()[0].strip() if out.strip() else ""


def check_ytdlp() -> dict[str, Any]:
    """yt-dlp: pakiet Python lub binarka w PATH. Kontrakt {available, version, path}."""
    try:
        import yt_dlp

        return {
            "available": True,
            "version": str(getattr(yt_dlp.version, "__version__", "") or ""),
            "path": None,  # zainstalowany jako pakiet — brak ścieżki binarki
        }
    except Exception:
        return probe_tool("yt-dlp", ["--version"])


def check_litellm(base_url: str = "http://localhost:4000") -> dict[str, Any]:
    """Gateway LiteLLM: osiągalność endpointu + lista modeli. base_url z config (override)."""
    result: dict[str, Any] = {"available": False, "base_url": base_url, "models": []}
    try:
        import json
        import urllib.request

        with urllib.request.urlopen(f"{base_url}/v1/models", timeout=2) as resp:
            data = json.loads(resp.read())
            result["models"] = [m.get("id", "") for m in data.get("data", [])]
            result["available"] = True
    except Exception:
        pass
    return result


def check_providers() -> dict[str, bool]:
    """Które klucze dostawców są w keyring. SAME BOOLEANY — nazwy spójne z core/secrets.

    Serwis i nazwa klucza pochodzą z ``core.secrets`` (ten sam name-builder co strona zapisu),
    więc odczyt i zapis nie mogą się rozjechać (dawniej: ``api_key_<p>`` vs ``api_key:<p>``).
    """
    from mediaforge.core.secrets import SERVICE_NAME, provider_api_key_name

    providers = ("anthropic", "openai", "gemini", "deepseek")
    return {p: api_key_present(SERVICE_NAME, provider_api_key_name(p)) for p in providers}
