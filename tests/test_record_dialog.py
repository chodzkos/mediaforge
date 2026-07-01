"""Testy dialogu nagrywania (pytest-qt, offscreen) — UI + sterowanie sesją z atrapą FFmpeg."""

from __future__ import annotations

from pathlib import Path

import pytest
from chodzkos_gui_kit.qt.widgets import LogView, PathEntry
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from mediaforge.core import config as cfg_mod
from mediaforge.core.engines.ffmpeg_cmd import CaptureMode, CaptureSource
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


def test_full_monitor_has_no_region(dialog: rd.RecordDialog) -> None:
    dialog._mode_combo.setCurrentIndex(0)
    src = dialog._build_capture_source()
    assert src.mode is CaptureMode.FULLSCREEN
    assert src.region is None  # cały monitor → bez crop


def test_region_mode_parses_subrect(dialog: rd.RecordDialog) -> None:
    # Region względem monitora — bierzemy podprostokąt mieszczący się w realnej rozdzielczości.
    _x, _y, mon_w, mon_h = rd._physical_geometry(QGuiApplication.screens()[0])
    rw, rh = mon_w // 2, mon_h // 2
    dialog._mode_combo.setCurrentIndex(1)
    dialog._region_edit.setText(f"0,0,{rw},{rh}")
    src = dialog._build_capture_source()
    assert src.mode is CaptureMode.REGION
    assert src.region == (0, 0, rw, rh)


def test_region_outside_monitor_is_rejected(dialog: rd.RecordDialog) -> None:
    dialog._mode_combo.setCurrentIndex(1)
    dialog._region_edit.setText("0,0,99999,99999")  # poza każdym realnym monitorem
    with pytest.raises(ValueError, match="wykracza poza monitor"):
        dialog._build_capture_source()


def test_start_aborts_on_invalid_region(dialog: rd.RecordDialog) -> None:
    dialog._mode_combo.setCurrentIndex(1)
    dialog._region_edit.setText("oops")  # niepoprawny format
    dialog._on_start()
    assert dialog._session is None  # nie wystartowało
    assert "region" in dialog._log.toPlainText().lower()


def test_window_capture_removed(dialog: rd.RecordDialog) -> None:
    # ddagrab nie zna okien — tryb i pole usunięte z modelu i GUI (brak funkcji-widma).
    assert not hasattr(CaptureMode, "WINDOW")
    assert not hasattr(CaptureSource(), "window_title")
    assert not hasattr(dialog, "_window_edit")
    assert dialog._mode_combo.count() == 2  # tylko: cały monitor + region


def test_recorded_seconds_counts_from_recording_signal(dialog: rd.RecordDialog) -> None:
    # Licznik nagrania startuje od „Nagrywam" (po pre-rollu), nie od uruchomienia ffmpeg.
    dialog._preroll_sec = 3
    assert dialog._recorded_seconds(1.5) is None  # wciąż pre-roll → brak licznika
    assert dialog._recorded_seconds(3.0) == 0.0  # granica: start nagrania
    assert dialog._recorded_seconds(5.0) == 2.0  # 5 s ffmpeg - 3 s pre-roll


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
    assert "przygotowuję" in dialog._log.toPlainText().lower()  # faza pre-roll przed „Nagrywam"

    dialog._on_stop()
    assert dialog._session is None
    assert "Zapisano" in dialog._log.toPlainText()

    rows = RecordingStore(db_path).list_recordings(RecordingStatus.RECORDED)
    assert len(rows) == 1
    assert rows[0].title == "Test nagranie"
