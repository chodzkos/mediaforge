"""Detekcja GPU — surowe fakty z nvidia-smi (warstwa A) + most arch dla mediaforge.

GRANICA EKSTRAKCJI: `check_gpu` (+ helpery nvidia-smi) to część generyczna → docelowo do
`chodzkos-detection` (Qt-free, stdlib). Mapowanie arch (`detect_arch`/`arch_from_name`/
`resolved_arch`) i `resolved_profile` używają `compute.GPUArch`/`classify` — to mediaforge-
specyficzna polityka tierów, ZOSTAJE w aplikacji przy wydzieleniu.

Detekcja bez torcha (nvidia-smi); sonda torcha tylko jako dodatek dla toru HF. Odporne na
brak narzędzia (puste/UNKNOWN, nigdy nie rzuca).
"""

from __future__ import annotations

import contextlib
import importlib.util
import re
import shutil
import subprocess
from functools import lru_cache
from typing import Any

from mediaforge.core.compute import ComputeProfile, GPUArch, classify

_TIMEOUT = 5


# ───────────── Część generyczna (→ chodzkos-detection): surowe fakty GPU ─────────────


def _nvidia_smi(query: str) -> str:
    """Best-effort zapytanie nvidia-smi → surowy stdout (lub ""). Odporne na wszystko.

    Gate przez shutil.which (brak sterowników/inny GPU → ""), sprawdzenie returncode
    (sterownik w złym stanie → ""), brak parsowania stderr (śmieci nie idą dalej).
    """
    if shutil.which("nvidia-smi") is None:
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
    """GPU — SUROWE fakty z nvidia-smi (bez interpretacji arch/tier). Sonda generyczna.

    Pola: available, name, vram_gb, driver, compute_cap (może być "" na starszych
    sterownikach — wtedy arch mapuje się z nazwy po stronie mediaforge), torch_cuda_usable.
    """
    result: dict[str, Any] = {
        "available": False,
        "name": "",
        "vram_gb": 0.0,
        "driver": "",
        "compute_cap": "",
        "torch_cuda_usable": False,
    }
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
            cc = _nvidia_smi("compute_cap")  # osobne zapytanie — starsze sterowniki nie mają pola
            cc_line = cc.splitlines()[0].strip() if cc.strip() else ""
            if cc_line and not cc_line.startswith("["):  # odfiltruj [Not Supported]/[N/A]
                result["compute_cap"] = cc_line
    with contextlib.suppress(Exception):
        if importlib.util.find_spec("torch") is not None:
            result["torch_cuda_usable"] = _torch_cuda_usable()
    return result


# ───────────── Most mediaforge (ZOSTAJE): mapowanie arch + polityka tierów ─────────────


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


def resolved_profile(gpu: dict[str, Any] | None = None) -> ComputeProfile:
    """Wykryty GPU → profil obliczeniowy (tier) przez compute.classify. Polityka mediaforge."""
    gpu = gpu if gpu is not None else check_gpu()
    return classify(
        has_cuda=bool(gpu["available"]),
        vram_gb=float(gpu["vram_gb"]),
        arch=resolved_arch(gpu),
    )
