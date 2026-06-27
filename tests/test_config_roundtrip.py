"""Round-trip konfiguracji przez ``Config`` z gui-kit + typowane akcesory mediaforge."""

from __future__ import annotations

from pathlib import Path

from chodzkos_gui_kit.config import Config

from mediaforge.core import config as cfg_mod
from mediaforge.core.ai.providers import ModelSpec, Provider, Task
from mediaforge.core.compute import ComputeTier


def _fresh(path: Path) -> Config:
    return Config(cfg_mod.APP_NAME, path=path)


def test_last_dir_and_geometry_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    cfg = _fresh(path)

    cfg_mod.set_last_dir(cfg, "import", "/data/in")
    cfg_mod.set_window_geometry(cfg, "Z2VvbWV0cnk=")
    cfg.save_now()

    reloaded = _fresh(path)
    assert cfg_mod.get_last_dir(reloaded, "import") == "/data/in"
    assert cfg_mod.get_last_dir(reloaded, "export") is None
    assert cfg_mod.get_window_geometry(reloaded) == "Z2VvbWV0cnk="


def test_compute_override_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    cfg = _fresh(path)
    fp = "machine-x/amd64"

    assert cfg_mod.get_compute_override(cfg, fp) is None
    cfg_mod.set_compute_override(cfg, ComputeTier.B, fp)
    cfg.save_now()

    assert cfg_mod.get_compute_override(_fresh(path), fp) is ComputeTier.B


def test_provider_assignments_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    cfg = _fresh(path)
    cfg_mod.set_provider_assignments(
        cfg,
        {Task.SLIDES_VLM: ModelSpec(Provider.GEMINI, "gemini-2.0", supports_vision=True)},
    )
    cfg.save_now()

    loaded = cfg_mod.get_provider_assignments(_fresh(path))
    spec = loaded[Task.SLIDES_VLM]
    assert spec.provider is Provider.GEMINI
    assert spec.model == "gemini-2.0"
    assert spec.supports_vision is True


def test_on_dirty_fires(tmp_path: Path) -> None:
    """Hak ``on_dirty`` (cel debounce GUI) odpala się przy każdej zmianie."""
    calls: list[int] = []
    cfg = Config(cfg_mod.APP_NAME, path=tmp_path / "config.json", on_dirty=lambda: calls.append(1))
    cfg_mod.set_last_dir(cfg, "import", "/x")
    assert calls  # zmiana oznaczyła config jako brudny
