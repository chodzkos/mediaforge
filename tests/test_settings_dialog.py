"""Dialog Ustawień AI: zapis przez Config, puste→default, walidacja „Sprawdź"."""

from __future__ import annotations

from pathlib import Path

from chodzkos_gui_kit.config import Config
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from mediaforge.core import config as cfg_mod
from mediaforge.gui.settings_dialog import (
    SettingsDialog,
    gateway_summary,
    validate_model,
    whisper_binary_summary,
    whisper_model_summary,
)


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


def test_whisper_summaries_are_pure() -> None:
    """Czyste funkcje sekcji Transkrypcja: binarka + model→runtime, hinty przy każdej porażce."""
    ok, msg = whisper_binary_summary(True, "1.5.0")
    assert ok and "1.5.0" in msg
    ok, msg = whisper_binary_summary(False, "")
    assert not ok and "whisper-cli" in msg

    assert whisper_model_summary(True, False, False, "unknown") == (
        False,
        "✗ wskaż plik modelu (.bin)",
    )
    assert "nie istnieje" in whisper_model_summary(True, True, False, "unknown")[1]  # brak pliku
    assert "binarkę" in whisper_model_summary(False, True, True, "cuda")[1]  # brak binarki
    ok, msg = whisper_model_summary(True, True, True, "cuda")
    assert ok and "CUDA" in msg
    ok, msg = whisper_model_summary(True, True, True, "cpu")
    assert ok and "CPU" in msg
    ok, msg = whisper_model_summary(True, True, True, "unknown")  # model jest, ale nie ruszył
    assert not ok and "nie ruszył" in msg


def test_dialog_saves_transcription_paths(qtbot: QtBot, qapp: QApplication, tmp_path: Path) -> None:
    """whispercpp_path i whisper_model zapisują się przez Config i przeżywają reload z dysku."""
    cfg = _cfg(tmp_path)
    dialog = SettingsDialog(config=cfg)
    qtbot.addWidget(dialog)
    dialog._whispercpp_path.setText("/opt/whisper/whisper-cli")
    dialog._whisper_model.setText("/models/ggml-large.bin")
    dialog._save()

    assert cfg_mod.get_whispercpp_path(cfg) == "/opt/whisper/whisper-cli"
    assert cfg_mod.get_whisper_model(cfg) == "/models/ggml-large.bin"
    assert cfg_mod.get_whisper_model(_cfg(tmp_path)) == "/models/ggml-large.bin"  # z dysku


def test_transcription_paths_empty_reset_to_none(
    qtbot: QtBot, qapp: QApplication, tmp_path: Path
) -> None:
    """Puste pola ścieżek → None (getter → autodetekcja / transkrypcja niedostępna)."""
    cfg = _cfg(tmp_path)
    cfg_mod.set_whisper_model(cfg, "/m/x.bin")
    cfg_mod.set_whispercpp_path(cfg, "/b/whisper-cli")

    dialog = SettingsDialog(config=cfg)
    qtbot.addWidget(dialog)
    assert dialog._whisper_model.text() == "/m/x.bin"  # override załadowany
    dialog._whisper_model.setText("")
    dialog._whispercpp_path.setText("")
    dialog._save()

    assert cfg_mod.get_whisper_model(cfg) is None
    assert cfg_mod.get_whispercpp_path(cfg) is None


def test_whisper_check_result_written_inline(
    qtbot: QtBot, qapp: QApplication, tmp_path: Path
) -> None:
    """Slot sondy whisper zapisuje wynik inline przy właściwym polu (bez subprocess)."""
    dialog = SettingsDialog(config=_cfg(tmp_path))
    qtbot.addWidget(dialog)

    dialog._whisper_pending = "whispercpp_path"
    dialog._on_whisper_probed(False, "", False, "unknown")  # binarki brak
    assert "✗" in dialog._result_labels["whispercpp_path"].text()

    dialog._whisper_model.setText("/m/x.bin")
    dialog._whisper_pending = "whisper_model"
    dialog._on_whisper_probed(True, "1.5", True, "cuda")  # runtime CUDA
    assert "CUDA" in dialog._result_labels["whisper_model"].text()


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
