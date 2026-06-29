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
from pathlib import Path
from typing import Any

_TIMEOUT = 5


def _run(cmd: list[str], timeout: int = _TIMEOUT) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.stdout or proc.stderr or ""
    except Exception:
        return ""


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


def check_ffmpeg() -> dict[str, Any]:
    """ffmpeg: kontrakt probe_tool + warstwa mediaforge (enkodery NVENC/x264/x265 OBOK)."""
    tool = probe_tool("ffmpeg", ["-hide_banner", "-version"], _ffmpeg_version)
    encoders: dict[str, bool] = {}
    if tool["available"]:
        enc = _run(["ffmpeg", "-hide_banner", "-encoders"])
        for name in ("h264_nvenc", "hevc_nvenc", "av1_nvenc", "libx264", "libx265"):
            encoders[name] = name in enc
    return {**tool, "encoders": encoders}


def check_whispercpp(override_path: str | None = None, binary: str = "whisper-cli") -> dict[str, Any]:
    """whisper.cpp: override z configu (`whispercpp_path`) → fallback `shutil.which`.

    Override zaprojektowany OD RAZU: binarka bywa self-compiled poza PATH (np. build/bin/).
    Override przeżyje przyszłą migrację na wzbogacony `probe_tool` pakietu (jak override
    java/epubcheck w EpubForge żyje obok detekcji). Kontrakt: {available, version, path}.
    """
    if override_path:
        p = Path(override_path)
        if p.exists():
            return {"available": True, "version": _whisper_version(p), "path": p}
    for cand in (binary, "whisper-cpp", "whisper", "main"):
        found = shutil.which(cand)
        if found:
            return {"available": True, "version": _whisper_version(Path(found)), "path": Path(found)}
    return {"available": False, "version": "", "path": None}


def _whisper_version(path: Path) -> str:
    out = _run([str(path), "--version"])
    return out.splitlines()[0].strip() if out.strip() else ""


def check_ytdlp() -> dict[str, Any]:
    """yt-dlp: pakiet Python (preferowane) lub binarka w PATH. Kontrakt {available, version, path}."""
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
    """Które klucze dostawców są w keyring. SAME BOOLEANY — nazwy spójne z core/secrets."""
    providers = ("anthropic", "openai", "gemini", "deepseek")
    return {p: api_key_present("mediaforge", f"api_key_{p}") for p in providers}
