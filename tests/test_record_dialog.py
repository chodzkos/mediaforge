"""Testy dialogu nagrywania (pytest-qt, offscreen) — UI + sterowanie sesją z atrapą FFmpeg."""

from __future__ import annotations

from pathlib import Path

import pytest
from chodzkos_gui_kit.qt.widgets import LogView, PathEntry
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from mediaforge.core import config as cfg_mod
from mediaforge.core.engines.ffmpeg_cmd import CaptureMode
from mediaforge.core.engines.recorder import RecorderEngine
from mediaforge.core.library.recordings import RecordingStatus, RecordingStore
from mediaforge.gui import record_dialog as rd


class _FakeProc:
    def stop_gracefully(self, timeout: float = 8.0) -> None:
        return None

    def is_running(self) -> bool:
        return False


def _fake_factory(command: list[str]) -> _FakeProc:
    pattern = command[-1]
    start = int(command[command.index("-segment_start_number") + 1])
    Path(pattern % start).write_bytes(b"SEG")
    return _FakeProc()


def _fake_concat(command: list[str]) -> int:
    Path(command[-1]).write_bytes(b"FINAL")
    return 0


@pytest.fixture
def dialog(
    qtbot: QtBot, qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> rd.RecordDialog:
    db_path = tmp_path / "library.sqlite3"
    monkeypatch.setattr(cfg_mod, "library_db_path", lambda: db_path)
    monkeypatch.setattr(cfg_mod, "default_recordings_dir", lambda: tmp_path / "out")
    monkeypatch.setattr(rd, "check_ffmpeg", lambda: {"encoders": {"hevc_nvenc": True}})
    dlg = rd.RecordDialog()
    qtbot.addWidget(dlg)
    return dlg


def test_dialog_uses_kit_widgets_and_presets(dialog: rd.RecordDialog) -> None:
    assert isinstance(dialog._out_dir, PathEntry)  # katalog z kitu, nie własny widget
    assert isinstance(dialog._log, LogView)  # log z kitu
    assert dialog._preset_combo.count() == 5  # 5 presetów jakości
    assert "recording" in rd.RECORD_LEVEL_COLORS  # status nagrywania ma kolor


def test_build_capture_source_modes(dialog: rd.RecordDialog) -> None:
    dialog._mode_combo.setCurrentIndex(0)
    assert dialog._build_capture_source().mode is CaptureMode.FULLSCREEN

    dialog._mode_combo.setCurrentIndex(2)
    dialog._window_edit.setText("Wykład")
    src = dialog._build_capture_source()
    assert src.mode is CaptureMode.WINDOW
    assert src.window_title == "Wykład"

    dialog._mode_combo.setCurrentIndex(3)
    dialog._region_edit.setText("10,20,800,600")
    src = dialog._build_capture_source()
    assert src.region == (10, 20, 800, 600)


def test_audio_config_reflects_checkboxes(dialog: rd.RecordDialog) -> None:
    dialog._sys_audio.setChecked(True)
    dialog._mic_audio.setChecked(True)
    dialog._mix_audio.setChecked(True)
    cfg = dialog._build_audio_config()
    assert cfg.system_audio is True
    assert cfg.microphone is True
    assert cfg.mix is True


def test_start_stop_lifecycle_writes_library(dialog: rd.RecordDialog, tmp_path: Path) -> None:
    db_path = tmp_path / "library.sqlite3"
    # Podmieniamy silnik na wersję z atrapą procesu/concat (bez realnego FFmpeg).
    dialog._engine = RecorderEngine(
        encoders={"hevc_nvenc": True},
        store=RecordingStore(db_path),
        process_factory=_fake_factory,
        concat_runner=_fake_concat,
    )
    dialog._title_edit.setText("Test nagranie")
    dialog._sys_audio.setChecked(False)
    dialog._out_dir.set(str(tmp_path / "out"))

    dialog._on_start()
    assert dialog._session is not None
    assert "rozpoczęte" in dialog._log.toPlainText().lower()

    dialog._on_stop()
    assert dialog._session is None
    assert "Zapisano" in dialog._log.toPlainText()

    rows = RecordingStore(db_path).list_recordings(RecordingStatus.RECORDED)
    assert len(rows) == 1
    assert rows[0].title == "Test nagranie"
