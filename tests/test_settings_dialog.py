"""Dialog Ustawień AI: zapis przez Config, puste→default, walidacja „Sprawdź"."""

from __future__ import annotations

from pathlib import Path

from chodzkos_gui_kit.config import Config
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from mediaforge.core import config as cfg_mod
from mediaforge.gui.settings_dialog import SettingsDialog, gateway_summary, validate_model


def _cfg(tmp_path: Path) -> Config:
    return Config(cfg_mod.APP_NAME, path=tmp_path / "config.json")


def test_validate_model_and_gateway_summary_are_pure() -> None:
    """Czyste funkcje walidacji: ✓ gdy model jest; ✗+lista gdy brak; martwy gateway czytelnie."""
    ok, msg = validate_model(True, ["qwen-local", "qwen-vl-local"], "qwen-local")
    assert ok and "✓" in msg
    ok, msg = validate_model(True, ["qwen-local"], "nie-ma-takiego")
    assert not ok and "qwen-local" in msg  # lista dostępnych w komunikacie
    ok, msg = validate_model(False, [], "cokolwiek")
    assert not ok and "niedostępny" in msg  # martwy gateway → czytelnie, nie crash
    ok, msg = validate_model(True, [], "")
    assert not ok and "wpisz" in msg  # puste pole modelu
    ok, msg = gateway_summary(True, ["a", "b"])
    assert ok and "2 modeli" in msg
    ok, msg = gateway_summary(False, [])
    assert not ok and "niedostępny" in msg


def test_dialog_saves_values_via_config(qtbot: QtBot, qapp: QApplication, tmp_path: Path) -> None:
    """Zapis przez Config: wartości wracają przez gettery i przeżywają reload z dysku (save_now)."""
    cfg = _cfg(tmp_path)
    dialog = SettingsDialog(config=cfg)
    qtbot.addWidget(dialog)
    dialog._base_url.setText("http://gw:4000")
    dialog._summary_local.setText("ollama/qwen3:27b")
    dialog._summary_cloud.setText("anthropic/claude-3")
    dialog._vlm_local.setText("ollama/qwen-vl-local")
    dialog._summary_timeout.setValue(900)
    dialog._vlm_max_tokens.setValue(3000)
    dialog._preroll.setValue(3)
    dialog._save()

    assert cfg_mod.get_litellm_base_url(cfg) == "http://gw:4000"
    assert cfg_mod.get_summary_model_local(cfg) == "ollama/qwen3:27b"
    assert cfg_mod.get_summary_model_cloud(cfg) == "anthropic/claude-3"
    assert cfg_mod.get_vlm_model_local(cfg) == "ollama/qwen-vl-local"
    assert cfg_mod.get_summary_timeout(cfg) == 900.0
    assert cfg_mod.get_vlm_max_tokens(cfg) == 3000
    assert cfg_mod.get_record_preroll_sec(cfg) == 3
    # Persystencja: reload z dysku widzi zapis.
    assert cfg_mod.get_summary_model_local(_cfg(tmp_path)) == "ollama/qwen3:27b"


def test_empty_or_zero_fields_reset_to_default(
    qtbot: QtBot, qapp: QApplication, tmp_path: Path
) -> None:
    """Puste pole tekstowe → None; spinbox 0 → None → getter zwraca default z kodu."""
    cfg = _cfg(tmp_path)
    cfg_mod.set_summary_model_local(cfg, "ollama/qwen3:27b")
    cfg_mod.set_summary_timeout(cfg, 999)

    dialog = SettingsDialog(config=cfg)
    qtbot.addWidget(dialog)
    # Załadowane wartości widoczne w polach (override, nie default).
    assert dialog._summary_local.text() == "ollama/qwen3:27b"
    assert dialog._summary_timeout.value() == 999

    dialog._summary_local.setText("")
    dialog._summary_timeout.setValue(0)
    dialog._save()

    assert cfg_mod.get_summary_model_local(cfg) is None  # puste → None
    assert cfg_mod.get_summary_timeout_override(cfg) is None  # 0 → None (jawnie nieustawione)
    assert (
        cfg_mod.get_summary_timeout(cfg) == cfg_mod.DEFAULT_SUMMARY_TIMEOUT_SEC
    )  # getter → default


def test_on_saved_callback_fires_on_save(qtbot: QtBot, qapp: QApplication, tmp_path: Path) -> None:
    """Po zapisie wołany jest ``on_saved`` (właściciel re-rejestruje handlery)."""
    calls: list[bool] = []
    dialog = SettingsDialog(config=_cfg(tmp_path), on_saved=lambda: calls.append(True))
    qtbot.addWidget(dialog)
    dialog._save()
    assert calls == [True]


def test_check_result_written_inline_to_field_label(
    qtbot: QtBot, qapp: QApplication, tmp_path: Path
) -> None:
    """Slot sondy zapisuje wynik inline przy właściwym polu (bez sieci — _on_probed wprost)."""
    dialog = SettingsDialog(config=_cfg(tmp_path))
    qtbot.addWidget(dialog)

    dialog._model_edits["vlm_local"].setText("qwen-vl-local")
    dialog._pending = "vlm_local"
    dialog._on_probed(True, ["qwen-vl-local"])
    assert "✓" in dialog._result_labels["vlm_local"].text()

    dialog._pending = "base_url"
    dialog._on_probed(False, [])
    assert "niedostępny" in dialog._result_labels["base_url"].text()
