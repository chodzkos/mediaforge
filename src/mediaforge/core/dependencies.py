"""Wykrywanie dostępnych narzędzi i zasobów — komenda `mediaforge-cli doctor`.

Zbudowane kit-ready, w trzech warstwach (czego pdf2md nie ma — jego doctor() splata
budowanie tabel z sondami):

  Warstwa 2 — sondy UNIWERSALNE (kandydaci do gui-kit): command_in_path, api_key_present,
              check_gpu. UWAGA: check_gpu zwraca SUROWE fakty (nazwa/VRAM/compute_cap) —
              bez interpretacji. To granica ekstrakcji.
  Most/Warstwa 3 — SPECYFICZNE dla mediaforge: mapowanie arch (detect_arch/arch_from_name/
              resolved_arch) + polityka tierów (resolved_profile → compute.classify), oraz
              sondy ffmpeg/whisper.cpp/LiteLLM/dostawcy. To ZOSTAJE w mediaforge.
  Warstwa 1 — PREZENTACJA (render_report): oddzielona od sond, operuje tylko na danych.

GRANICA EKSTRAKCJI (Uwaga 2): do kitu idzie check_gpu (surowe dane GPU). Mapowanie arch
i classify() to mediaforge-specyficzna polityka tierów — NIE wciągać ich do kitu.

Wszystkie sondy odporne na brak narzędzia (False/pusty dict, nigdy nie rzucają). Qt-free.
Detekcja GPU bez torcha (nvidia-smi); sonda torcha tylko jako dodatek dla toru HF.
"""

from __future__ import annotations

import contextlib
import importlib.util
import platform
import re
import shutil
import subprocess
import urllib.request
from functools import lru_cache
from typing import Any

from mediaforge.core.compute import ComputeProfile, GPUArch, classify

_TIMEOUT = 5


def _run(cmd: list[str], timeout: int = _TIMEOUT) -> str:
    """Uruchom polecenie i zwróć stdout (lub stderr/""). Nigdy nie rzuca."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.stdout or proc.stderr or ""
    except Exception:
        return ""


# ──────────────────── Warstwa 2: sondy uniwersalne (kandydaci do kitu) ────────────────────


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


def _nvidia_smi(query: str) -> str:
    """Best-effort zapytanie nvidia-smi → surowy stdout (lub ""). Odporne na wszystko.

    Gate przez command_in_path (brak sterowników/inny GPU → ""), sprawdzenie returncode
    (sterownik w złym stanie → ""), brak parsowania stderr (śmieci nie trafiają dalej).
    """
    if not command_in_path("nvidia-smi"):
        return ""
    try:
        proc = subprocess.run(
            ["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
        )
        if proc.returncode != 0:
            return ""
        return proc.stdout or ""
    except Exception:
        return ""


@lru_cache(maxsize=1)
def _torch_cuda_usable() -> bool:
    """OPCJONALNE: realny test kernela — tylko gdy torch obecny (tor HF). Nie wymagane."""
    try:
        import torch

        if not torch.cuda.is_available():
            return False
        tensor = torch.zeros(1).cuda()
        torch.cuda.synchronize()
        del tensor
    except Exception:
        return False
    return True


def check_gpu() -> dict[str, Any]:
    """GPU — SUROWE fakty z nvidia-smi (bez interpretacji arch/tier). Sonda uniwersalna.

    Pola: available, name, vram_gb, driver, compute_cap (może być "" na starszych
    sterownikach — wtedy arch mapuje się z nazwy po stronie mediaforge), torch_cuda_usable.
    To jest granica ekstrakcji do kitu — mapowanie arch i tier są poza tą funkcją.
    """
    result: dict[str, Any] = {
        "available": False,
        "name": "",
        "vram_gb": 0.0,
        "driver": "",
        "compute_cap": "",
        "torch_cuda_usable": False,
    }
    # Pola zawsze wspierane (nazwa/VRAM/driver) — jedno zapytanie.
    base = _nvidia_smi("name,memory.total,driver_version")
    line = base.splitlines()[0] if base.strip() else ""
    if line:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3:
            result["available"] = True
            result["name"] = parts[0]
            with contextlib.suppress(ValueError):
                result["vram_gb"] = round(float(parts[1]) / 1024, 1)
            result["driver"] = parts[2]
            # compute_cap OSOBNYM zapytaniem — starsze sterowniki mogą nie mieć tego pola.
            cc = _nvidia_smi("compute_cap")
            cc_line = cc.splitlines()[0].strip() if cc.strip() else ""
            if cc_line and not cc_line.startswith("["):  # odfiltruj [Not Supported]/[N/A]
                result["compute_cap"] = cc_line
    try:
        if importlib.util.find_spec("torch") is not None:
            result["torch_cuda_usable"] = _torch_cuda_usable()
    except Exception:
        pass
    return result


# ───────────── Most: mapowanie arch + polityka tierów (mediaforge — NIE do kitu) ─────────────


def detect_arch(compute_cap: str) -> GPUArch:
    """compute capability → architektura. Preferowane źródło (dokładne)."""
    major = compute_cap.split(".")[0] if compute_cap else ""
    minor = compute_cap.split(".")[1] if "." in compute_cap else ""
    if major in ("12", "10"):
        return GPUArch.BLACKWELL  # 12.0 = RTX 50xx (sm_120); 10.0 = Blackwell DC
    if major == "8":
        return GPUArch.ADA if minor == "9" else GPUArch.AMPERE  # 8.9=Ada; 8.0/8.6=Ampere
    if major == "7":
        return GPUArch.TURING  # 7.5 → RTX 20xx / GTX 16xx
    if major == "6":
        return GPUArch.PASCAL  # 6.1 → GTX 1070
    if major in ("3", "5"):
        return GPUArch.OLDER
    return GPUArch.UNKNOWN


def arch_from_name(name: str) -> GPUArch:
    """Fallback: architektura z nazwy GPU, gdy compute_cap niedostępne (best-effort)."""
    n = name.upper()
    m = re.search(r"(RTX|GTX)\s*(\d{3,4})", n)
    if m:
        prefix, series = m.group(1), int(m.group(2))
        if prefix == "RTX":
            if 5000 <= series < 6000:
                return GPUArch.BLACKWELL
            if 4000 <= series < 5000:
                return GPUArch.ADA
            if 3000 <= series < 4000:
                return GPUArch.AMPERE
            if 2000 <= series < 3000:
                return GPUArch.TURING
        elif prefix == "GTX":
            if 1600 <= series < 1700:
                return GPUArch.TURING  # GTX 16xx = Turing
            if 1000 <= series < 1100:
                return GPUArch.PASCAL  # GTX 10xx = Pascal
    if any(x in n for x in ("B200", "B100", "GB20")):
        return GPUArch.BLACKWELL
    if any(x in n for x in ("A100", "A6000", "A40", "A30", "A10")):
        return GPUArch.AMPERE
    return GPUArch.UNKNOWN


def resolved_arch(gpu: dict[str, Any]) -> GPUArch:
    """Mediaforge: arch z compute_cap (preferowane), fallback z nazwy GPU."""
    arch = detect_arch(str(gpu.get("compute_cap", "")))
    if arch is GPUArch.UNKNOWN:
        arch = arch_from_name(str(gpu.get("name", "")))
    return arch


def resolved_profile() -> ComputeProfile:
    """Wykryty GPU → profil obliczeniowy (tier) przez compute.classify. Polityka mediaforge."""
    gpu = check_gpu()
    return classify(
        has_cuda=bool(gpu["available"]),
        vram_gb=float(gpu["vram_gb"]),
        arch=resolved_arch(gpu),
    )


# ───────────────────────── Warstwa 3: sondy mediaforge ─────────────────────────


def check_ffmpeg() -> dict[str, Any]:
    """ffmpeg: dostępność, wersja i które enkodery (NVENC/x264/x265) są w buildzie."""
    result: dict[str, Any] = {"available": False, "version": "", "encoders": {}}
    if not command_in_path("ffmpeg"):
        return result
    ver = _run(["ffmpeg", "-hide_banner", "-version"])
    if ver:
        result["available"] = True
        parts = ver.splitlines()[0].split()
        result["version"] = parts[2] if len(parts) > 2 else ""
    encoders = _run(["ffmpeg", "-hide_banner", "-encoders"])
    for name in ("h264_nvenc", "hevc_nvenc", "av1_nvenc", "libx264", "libx265"):
        result["encoders"][name] = name in encoders
    return result


def check_whispercpp(binary: str = "whisper-cli") -> dict[str, Any]:
    """whisper.cpp: obecność binarki (domyślny backend transkrypcji)."""
    result: dict[str, Any] = {"available": False, "path": "", "binary": binary}
    path = shutil.which(binary) or shutil.which("whisper-cpp") or shutil.which("main")
    if path:
        result["available"] = True
        result["path"] = path
    return result


def check_litellm(base_url: str = "http://localhost:4000") -> dict[str, Any]:
    """Gateway LiteLLM: osiągalność endpointu + lista modeli. base_url docelowo z config."""
    result: dict[str, Any] = {"available": False, "base_url": base_url, "models": []}
    try:
        with urllib.request.urlopen(f"{base_url}/v1/models", timeout=2) as resp:
            import json

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


def check_ytdlp() -> dict[str, Any]:
    """yt-dlp: dostępność (pakiet Python lub binarka w PATH) + wersja. Sonda mediaforge (S5)."""
    result: dict[str, Any] = {"available": False, "version": ""}
    try:
        import yt_dlp

        result["available"] = True
        result["version"] = str(getattr(yt_dlp.version, "__version__", "") or "")
    except Exception:
        if command_in_path("yt-dlp"):
            result["available"] = True
            out = _run(["yt-dlp", "--version"])
            result["version"] = out.splitlines()[0].strip() if out.strip() else ""
    return result


def whisper_cuda_ok() -> bool:
    """PLACEHOLDER — w S3 zastąpione REALNĄ sondą whisper.cpp CUDA.

    Docelowo (S3): wywołanie binarki whisper.cpp z testem backendu / próbny krótki przebieg,
    z WŁASNYM progiem architektury — NIE sm_75/cu130 (to próg torcha pdf2md; ggml schodzi
    niżej, prawdopodobnie do Pascala, więc GTX 1070 może działać). Do czasu S3 decyzję o tierze
    podejmuje heurystyka arch+VRAM w compute.classify; tu zwracamy zgrubne: GPU obecny
    i binarka whisper.cpp w PATH.
    """
    return bool(check_gpu()["available"]) and bool(check_whispercpp()["available"])


# ───────────────────────── Agregat danych (źródło dla CLI i GUI) ─────────────────────────


def check_all() -> dict[str, Any]:
    """Zbiorczy raport jako DANE. Sekcja gpu wzbogacona o arch (interpretacja mediaforge);
    surowe check_gpu pozostaje generyczne."""
    gpu_raw = check_gpu()
    arch = resolved_arch(gpu_raw)
    profile = classify(
        has_cuda=bool(gpu_raw["available"]),
        vram_gb=float(gpu_raw["vram_gb"]),
        arch=arch,
    )
    wh = check_whispercpp()
    return {
        "system": {
            "os": platform.system(),
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "ffmpeg": check_ffmpeg(),
        "whispercpp": wh,
        "ytdlp": check_ytdlp(),
        "gpu": {**gpu_raw, "arch": arch.value},  # wzbogacenie o arch dla wyświetlania
        "compute": {
            "tier": profile.tier.value,
            "transcription_local": profile.transcription_local,
            # PLACEHOLDER do S3 — realna sonda whisper.cpp CUDA (własny próg) zastąpi tę heurystykę:
            "whisper_cuda_ok": bool(gpu_raw["available"]) and wh["available"],
            "llm_local": profile.llm_local,
            "vlm_local": profile.vlm_local,
            "note": profile.note,
        },
        "litellm": check_litellm(),
        "providers": check_providers(),
    }


# ───────────────────────── Warstwa 1: prezentacja (oddzielona od sond) ─────────────────────────

_HINTS: dict[str, str] = {
    "ffmpeg": "zainstaluj ffmpeg i dodaj do PATH",
    "whispercpp": "zbuduj whisper.cpp (CUDA) lub wskaż ścieżkę binarki w ustawieniach",
    "gpu": "brak GPU CUDA — transkrypcja/LLM pójdą w chmurę (LiteLLM)",
    "litellm": "uruchom gateway LiteLLM albo ustaw endpoint w konfiguracji",
}


def _mark(ok: bool) -> str:
    return "✓" if ok else "✗"


def render_report(report: dict[str, Any]) -> str:
    """Czytelny render raportu (warstwa prezentacji, oddzielona od sond). Plain-text."""
    lines: list[str] = []

    sys_info = report.get("system", {})
    lines.append(f"System:      {sys_info.get('os', '?')} · Python {sys_info.get('python', '?')}")

    ff = report.get("ffmpeg", {})
    enc = ff.get("encoders", {})
    enc_str = ", ".join(f"{n} {_mark(v)}" for n, v in enc.items()) if enc else "-"
    lines.append(
        f"FFmpeg:      {_mark(ff.get('available', False))} {ff.get('version', '')}".rstrip()
    )
    lines.append(f"             enkodery: {enc_str}")
    if not ff.get("available", False):
        lines.append(f"             → {_HINTS['ffmpeg']}")

    wh = report.get("whispercpp", {})
    lines.append(f"whisper.cpp: {_mark(wh.get('available', False))} {wh.get('path', '')}".rstrip())
    if not wh.get("available", False):
        lines.append(f"             → {_HINTS['whispercpp']}")

    yt = report.get("ytdlp", {})
    lines.append(
        f"yt-dlp:      {_mark(yt.get('available', False))} {yt.get('version', '')}".rstrip()
    )

    gpu = report.get("gpu", {})
    comp = report.get("compute", {})
    if gpu.get("available", False):
        cc = gpu.get("compute_cap", "") or "cc n/d"
        lines.append(
            f"GPU:         ✓ {gpu.get('name', '')} · {gpu.get('vram_gb', 0)} GB · "
            f"{gpu.get('arch', '')} ({cc})"
        )
    else:
        lines.append(f"GPU:         ✗ — {_HINTS['gpu']}")
    lines.append(f"             → Tier {comp.get('tier', '?')}: {comp.get('note', '')}")

    ll = report.get("litellm", {})
    lines.append(f"LiteLLM:     {_mark(ll.get('available', False))} ({ll.get('base_url', '')})")
    if not ll.get("available", False):
        lines.append(f"             → {_HINTS['litellm']}")
    elif ll.get("models"):
        lines.append(f"             modele: {', '.join(ll['models'][:8])}")

    prov = report.get("providers", {})
    lines.append("Dostawcy:    " + ", ".join(f"{k} {_mark(v)}" for k, v in prov.items()))

    return "\n".join(lines)
