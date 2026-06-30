"""Doctor: agregacja sond + prezentacja. APP-SIDE — NIE do `chodzkos-detection`.

`check_all()` składa sondy hardware + tools w jeden raport-DANE; `render_report()` to warstwa
prezentacji (plain-text), oddzielona od sond. Render zostaje w aplikacji, bo prezentacja jest
app-specyficzna (pdf2md: Rich, mediaforge: Qt). Override (whisper.cpp, LiteLLM) przekazywane
parametrami — wiring (CLI/GUI) czyta je z `core/config` i podaje tutaj, dzięki czemu warstwa
detekcji pozostaje odsprzężona od configu.
"""

from __future__ import annotations

import platform
from typing import Any

from mediaforge.core.compute import classify

from . import hardware, tools


def whisper_cuda_ok(whispercpp_path: str | None = None) -> bool:
    """PLACEHOLDER — w S3 zastąpione REALNĄ sondą whisper.cpp CUDA z WŁASNYM progiem.

    Docelowo (S3): wywołanie binarki whisper.cpp z testem backendu / próbny krótki przebieg —
    NIE próg sm_75/cu130 (to próg torcha pdf2md; ggml schodzi niżej, prawdopodobnie do Pascala,
    więc GTX 1070 może działać). Do S3 decyzję o tierze podejmuje heurystyka arch+VRAM w
    compute.classify; tu zgrubne: GPU obecny i binarka whisper.cpp znaleziona (override/PATH).
    """
    gpu_ok = bool(hardware.check_gpu()["available"])
    whisper_ok = bool(tools.check_whispercpp(whispercpp_path)["available"])
    return gpu_ok and whisper_ok


def check_all(
    whispercpp_path: str | None = None,
    litellm_base_url: str | None = None,
) -> dict[str, Any]:
    """Zbiorczy raport jako DANE — komenda `doctor` (render_report) i status bar GUI.

    Override przekazywane z wiringu (z core/config): `whispercpp_path`, `litellm_base_url`.
    """
    gpu_raw = hardware.check_gpu()
    arch = hardware.resolved_arch(gpu_raw)
    profile = classify(
        has_cuda=bool(gpu_raw["available"]),
        vram_gb=float(gpu_raw["vram_gb"]),
        arch=arch,
    )
    wh = tools.check_whispercpp(whispercpp_path)
    litellm = tools.check_litellm(litellm_base_url) if litellm_base_url else tools.check_litellm()
    return {
        "system": {
            "os": platform.system(),
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "ffmpeg": tools.check_ffmpeg(),
        "whispercpp": wh,
        "ytdlp": tools.check_ytdlp(),
        "gpu": {**gpu_raw, "arch": arch.value},  # wzbogacenie o arch dla wyświetlania
        "compute": {
            "tier": profile.tier.value,
            "transcription_local": profile.transcription_local,
            # PLACEHOLDER (S3): zastąpione realną sondą whisper.cpp CUDA
            # z własnym progiem (nie sm_75/cu130):
            "whisper_cuda_ok": bool(gpu_raw["available"]) and wh["available"],
            "llm_local": profile.llm_local,
            "vlm_local": profile.vlm_local,
            "note": profile.note,
        },
        "litellm": litellm,
        "providers": tools.check_providers(),
    }


# ───────────── Warstwa prezentacji (oddzielona od sond; app-side) ─────────────

_HINTS: dict[str, str] = {
    "ffmpeg": "zainstaluj ffmpeg i dodaj do PATH",
    "whispercpp": "zbuduj whisper.cpp (CUDA) i ustaw whispercpp_path w configu (bywa poza PATH)",
    "gpu": "brak GPU CUDA — transkrypcja/LLM pójdą w chmurę (LiteLLM)",
    "litellm": "uruchom gateway LiteLLM albo ustaw endpoint w konfiguracji",
}


def _mark(ok: bool) -> str:
    return "✓" if ok else "✗"


def status_line(report: dict[str, Any]) -> str:
    """Zwięzły jednowierszowy status do paska GUI — z tych samych DANYCH co `doctor`.

    Jedno źródło detekcji: pasek statusu i `doctor` czytają `check_all()`, tu tylko inna
    (krótsza) prezentacja niż `render_report`.
    """
    ff = "OK" if report.get("ffmpeg", {}).get("available") else "brak"
    wh = "OK" if report.get("whispercpp", {}).get("available") else "brak"
    gpu = report.get("gpu", {})
    cuda = f"{gpu.get('name', '')} {gpu.get('vram_gb', 0):g} GB" if gpu.get("available") else "brak"
    tier = report.get("compute", {}).get("tier", "?")
    return f"FFmpeg: {ff}  |  whisper.cpp: {wh}  |  CUDA: {cuda}  |  Tier: {tier}"


def render_report(report: dict[str, Any]) -> str:
    """Czytelny render raportu (warstwa prezentacji, oddzielona od sond). Plain-text."""
    lines: list[str] = []

    sys_info = report.get("system", {})
    lines.append(f"System:      {sys_info.get('os', '?')} · Python {sys_info.get('python', '?')}")

    ff = report.get("ffmpeg", {})
    enc = ff.get("encoders", {})
    enc_str = ", ".join(f"{n} {_mark(v)}" for n, v in enc.items()) if enc else "-"
    ff_av = _mark(ff.get("available", False))
    lines.append(f"FFmpeg:      {ff_av} {ff.get('version', '')}".rstrip())
    lines.append(f"             enkodery: {enc_str}")
    if not ff.get("available", False):
        lines.append(f"             → {_HINTS['ffmpeg']}")

    wh = report.get("whispercpp", {})
    wh_path = wh.get("path") or ""
    lines.append(f"whisper.cpp: {_mark(wh.get('available', False))} {wh_path}".rstrip())
    if not wh.get("available", False):
        lines.append(f"             → {_HINTS['whispercpp']}")

    yt = report.get("ytdlp", {})
    yt_av = _mark(yt.get("available", False))
    lines.append(f"yt-dlp:      {yt_av} {yt.get('version', '')}".rstrip())

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
