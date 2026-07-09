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
from mediaforge.core.engines.recorder import RecorderEngine, RecorderState, material_dir_for
from mediaforge.core.library.recordings import RecordingStatus, RecordingStore
from mediaforge.gui import record_dialog as rd


class _FakeProc:
    def stop_gracefully(self, timeout: float = 8.0) -> None:
        return None

    def is_running(self) -> bool:
        return False


def _fake_factory(command: list[str], log_path: Path | None = None) -> _FakeProc:
    pattern = command[-1]
    start = int(command[command.index("-segment_start_number") + 1])
    Path(pattern % start).write_bytes(b"SEG")
    return _FakeProc()


def _fake_concat(command: list[str]) -> int:
    Path(command[-1]).write_bytes(b"FINAL")
    return 0


def _stop_and_wait(dialog: rd.RecordDialog, qtbot: QtBot) -> None:
    """Woła _on_stop() (stop+concat na wątku roboczym) i czeka aż wątek się domknie."""
    dialog._on_stop()
    qtbot.waitUntil(
        lambda: not dialog._finalizing and dialog._finalize_thread is None, timeout=5000
    )


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


def test_engine_uses_usable_encoders_not_build(
    qtbot: QtBot, qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M21: silnik dostaje encoders_usable (runtime), nie build — NVENC-widmo poza wyborem."""
    monkeypatch.setattr(cfg_mod, "library_db_path", lambda: tmp_path / "library.sqlite3")
    monkeypatch.setattr(cfg_mod, "default_recordings_dir", lambda: tmp_path / "out")
    probe = {
        "available": True,
        "encoders": {"hevc_nvenc": True, "libx264": True},  # build: NVENC obecny
        "encoders_usable": {"hevc_nvenc": False, "libx264": True},  # runtime: NVENC martwy
    }
    dlg = rd.RecordDialog(ffmpeg_probe=probe)
    qtbot.addWidget(dlg)
    assert dlg._engine.encoders == {"hevc_nvenc": False, "libx264": True}


def test_engine_falls_back_to_build_encoders_without_probe(
    qtbot: QtBot, qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stary raport bez encoders_usable → wybór spada na build-presence (zgodność wsteczna)."""
    monkeypatch.setattr(cfg_mod, "library_db_path", lambda: tmp_path / "library.sqlite3")
    monkeypatch.setattr(cfg_mod, "default_recordings_dir", lambda: tmp_path / "out")
    dlg = rd.RecordDialog(ffmpeg_probe={"available": True, "encoders": {"hevc_nvenc": True}})
    qtbot.addWidget(dlg)
    assert dlg._engine.encoders == {"hevc_nvenc": True}


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


def test_start_stop_lifecycle_writes_library(
    dialog: rd.RecordDialog, tmp_path: Path, qtbot: QtBot
) -> None:
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

    _stop_and_wait(dialog, qtbot)  # stop+concat na wątku roboczym → czekamy na wynik
    assert dialog._session is None
    assert "Zapisano" in dialog._log.toPlainText()

    rows = RecordingStore(db_path).list_recordings(RecordingStatus.RECORDED)
    assert len(rows) == 1
    assert rows[0].title == "Test nagranie"


# ── Śmierć procesu FFmpeg w trakcie / na starcie ──────────────────────────────


class _DeadProc:
    """Atrapa procesu, który natychmiast jest martwy (is_running() False)."""

    def stop_gracefully(self, timeout: float = 8.0) -> None:
        return None

    def is_running(self) -> bool:
        return False


def _dead_factory(command: list[str], log_path: Path | None = None) -> _DeadProc:
    pattern = command[-1]
    start = int(command[command.index("-segment_start_number") + 1])
    Path(pattern % start).write_bytes(b"SEG")
    if log_path is not None:  # zasymuluj ślad stderr FFmpeg do diagnostyki
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("Device or resource busy\n", encoding="utf-8")
    return _DeadProc()


def _dead_engine(dialog: rd.RecordDialog, db_path: Path) -> None:
    dialog._engine = RecorderEngine(
        encoders={"hevc_nvenc": True},
        store=RecordingStore(db_path),
        process_factory=_dead_factory,
        concat_runner=_fake_concat,
    )


def test_tick_detects_process_death_offers_resume_or_stop(
    dialog: rd.RecordDialog, tmp_path: Path
) -> None:
    """_tick przy martwym procesie: log error + ogon, sesja w PAUSED, przyciski Wznów/Zatrzymaj."""
    _dead_engine(dialog, tmp_path / "library.sqlite3")
    dialog._title_edit.setText("Nagranie")
    dialog._sys_audio.setChecked(False)
    dialog._out_dir.set(str(tmp_path / "out"))
    dialog._preroll_sec = 0

    dialog._on_start()
    assert dialog._session is not None
    dialog._awaiting_start_verify = False  # po oknie sondy startu — _tick przejmuje wykrywanie

    dialog._tick()
    log = dialog._log.toPlainText()
    assert "FFmpeg przerwał nagrywanie" in log
    assert "Device or resource busy" in log  # ogon loga w diagnostyce
    assert dialog._session is not None and dialog._session.state is RecorderState.PAUSED
    # Dwa wyjścia odblokowane: Wznów (pauza) i Zatrzymaj; Start zablokowany.
    assert dialog._pause_btn.isEnabled() and dialog._stop_btn.isEnabled()
    assert not dialog._start_btn.isEnabled()
    assert dialog._pause_btn.text() == "▶ Wznów"
    dialog._session = None  # sprzątanie: bramka zamknięcia nie prosi o modal w teardown qtbot


def test_verify_started_resets_ui_on_immediate_death(
    dialog: rd.RecordDialog, tmp_path: Path
) -> None:
    """Śmierć w 1,5 s: _verify_started pokazuje błąd i resetuje UI, BEZ wpisu do biblioteki."""
    db_path = tmp_path / "library.sqlite3"
    _dead_engine(dialog, db_path)
    dialog._title_edit.setText("Nagranie")
    dialog._sys_audio.setChecked(False)
    dialog._out_dir.set(str(tmp_path / "out"))
    dialog._preroll_sec = 0

    dialog._on_start()
    assert dialog._session is not None
    dialog._verify_started()  # ręcznie zamiast czekać 1,5 s

    assert dialog._session is None  # UI zresetowane
    assert "nie wystartował" in dialog._log.toPlainText().lower()
    assert dialog._start_btn.isEnabled() and not dialog._stop_btn.isEnabled()
    # Brak wpisu do biblioteki — nic sensownego się nie nagrało.
    assert RecordingStore(db_path).list_recordings(RecordingStatus.RECORDED) == []


def test_on_stop_finalize_failure_keeps_segments_and_logs_workdir(
    dialog: rd.RecordDialog, tmp_path: Path, qtbot: QtBot
) -> None:
    """Porażka finalizacji: log błędu + ścieżka _work; segmenty zostają, brak wpisu w bibliotece."""
    db_path = tmp_path / "library.sqlite3"

    def failing_concat(command: list[str]) -> int:
        return 1  # kod ≠ 0 i brak pliku wynikowego → RuntimeError w finalize

    dialog._engine = RecorderEngine(
        encoders={"hevc_nvenc": True},
        store=RecordingStore(db_path),
        process_factory=_fake_factory,  # tworzy segment (jest co sklejać)
        concat_runner=failing_concat,
    )
    dialog._title_edit.setText("Nagranie")
    dialog._sys_audio.setChecked(False)
    dialog._out_dir.set(str(tmp_path / "out"))
    dialog._preroll_sec = 0

    dialog._on_start()
    assert dialog._session is not None
    work_dir = dialog._session.work_dir

    _stop_and_wait(dialog, qtbot)
    log = dialog._log.toPlainText()
    assert "Błąd finalizacji" in log
    assert str(work_dir) in log  # ścieżka _work do ręcznego odzysku
    assert dialog._session is None
    # Segmenty NIE ruszone + brak wpisu w bibliotece.
    assert work_dir.exists() and any(work_dir.iterdir())
    assert RecordingStore(db_path).list_recordings(RecordingStatus.RECORDED) == []


# ── Składanie (stop+concat) poza wątkiem UI ────────────────────────────────────


def _prime_finalize(dialog: rd.RecordDialog, tmp_path: Path, concat: object) -> None:
    """Podpina silnik z atrapą, startuje nagranie (bez pre-rollu) — gotowe do _on_stop()."""
    dialog._engine = RecorderEngine(
        encoders={"hevc_nvenc": True},
        store=RecordingStore(tmp_path / "library.sqlite3"),
        process_factory=_fake_factory,
        concat_runner=concat,  # type: ignore[arg-type]
    )
    dialog._sys_audio.setChecked(False)
    dialog._out_dir.set(str(tmp_path / "out"))
    dialog._preroll_sec = 0
    dialog._on_start()


def test_finalize_off_ui_thread_unlocks_on_success(
    dialog: rd.RecordDialog, tmp_path: Path, qtbot: QtBot
) -> None:
    """Sukces składania na wątku roboczym: UI zablokowane w trakcie, sygnał finished odblokowuje."""
    _prime_finalize(dialog, tmp_path, _fake_concat)

    dialog._on_stop()
    # Synchronicznie po _on_stop: składanie ruszyło na osobnym wątku → UI zablokowane.
    assert dialog._finalizing is True
    assert dialog._finalize_thread is not None  # praca poza wątkiem UI
    assert not dialog._start_btn.isEnabled()
    assert not dialog._stop_btn.isEnabled()

    qtbot.waitUntil(lambda: not dialog._finalizing, timeout=5000)  # sygnał finished dotarł
    assert dialog._session is None
    assert "Zapisano" in dialog._log.toPlainText()
    assert dialog._start_btn.isEnabled()  # UI odblokowany po sukcesie
    qtbot.waitUntil(lambda: dialog._finalize_thread is None, timeout=5000)  # wątek posprzątany


def test_finalize_off_ui_thread_unlocks_on_failure(
    dialog: rd.RecordDialog, tmp_path: Path, qtbot: QtBot
) -> None:
    """Porażka składania na wątku roboczym: sygnał failed loguje błąd i odblokowuje UI."""

    def failing_concat(command: list[str]) -> int:
        return 1  # brak pliku wynikowego → RuntimeError w finalize → sygnał failed

    _prime_finalize(dialog, tmp_path, failing_concat)

    dialog._on_stop()
    assert dialog._finalizing is True  # składanie w toku (wątek roboczy)

    qtbot.waitUntil(lambda: not dialog._finalizing, timeout=5000)  # sygnał failed dotarł
    assert "Błąd finalizacji" in dialog._log.toPlainText()
    assert dialog._session is None
    assert dialog._start_btn.isEnabled()  # UI odblokowany także po porażce
    qtbot.waitUntil(lambda: dialog._finalize_thread is None, timeout=5000)


# ── Zamknięcie okna w trakcie nagrania (ochrona przed osieroceniem FFmpeg) ────


class _LiveProc:
    """Atrapa żywego procesu FFmpeg — śledzi, czy zawołano stop_gracefully."""

    def __init__(self) -> None:
        self.stopped = False

    def stop_gracefully(self, timeout: float = 8.0) -> None:
        self.stopped = True

    def is_running(self) -> bool:
        return not self.stopped


def _start_recording_with(dialog: rd.RecordDialog, tmp_path: Path, proc: _LiveProc) -> None:
    """Startuje sesję nagrywania z atrapą żywego procesu (bez pre-rollu, bez realnego FFmpeg)."""

    def factory(command: list[str], log_path: Path | None = None) -> _LiveProc:
        pattern = command[-1]
        start = int(command[command.index("-segment_start_number") + 1])
        Path(pattern % start).write_bytes(b"SEG")
        return proc

    dialog._engine = RecorderEngine(
        encoders={"hevc_nvenc": True},
        store=RecordingStore(tmp_path / "library.sqlite3"),
        process_factory=factory,
        concat_runner=_fake_concat,
    )
    dialog._sys_audio.setChecked(False)
    dialog._out_dir.set(str(tmp_path / "out"))
    dialog._preroll_sec = 0
    dialog._on_start()


def test_close_during_recording_back_keeps_session_and_process(
    dialog: rd.RecordDialog, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """„Wróć": zamknięcie zablokowane — sesja i proces FFmpeg żyją dalej."""
    proc = _LiveProc()
    _start_recording_with(dialog, tmp_path, proc)
    assert dialog._session is not None and dialog._session.state is RecorderState.RECORDING

    monkeypatch.setattr(dialog, "_ask_close_action", lambda: rd.CloseChoice.CANCEL)
    dialog.close()

    assert dialog._session is not None  # okno nie zamknęło sesji
    assert dialog._session.state is RecorderState.RECORDING
    assert proc.stopped is False  # proces FFmpeg nietknięty
    dialog._session = None  # sprzątanie: bramka zamknięcia nie prosi o modal w teardown qtbot


def test_close_during_recording_discard_stops_process(
    dialog: rd.RecordDialog, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """„Porzuć": proces FFmpeg domknięty (stop_gracefully), bez finalizacji i wpisu w bibliotece."""
    db_path = tmp_path / "library.sqlite3"
    proc = _LiveProc()
    _start_recording_with(dialog, tmp_path, proc)
    assert dialog._session is not None

    monkeypatch.setattr(dialog, "_ask_close_action", lambda: rd.CloseChoice.DISCARD)
    dialog.close()

    assert proc.stopped is True  # stop_gracefully zawołane na procesie (proces nie osierocony)
    assert dialog._session is None
    assert "porzucone" in dialog._log.toPlainText().lower()
    assert RecordingStore(db_path).list_recordings(RecordingStatus.RECORDED) == []  # brak wpisu


# ── Kolizja nazwy: nadpisz / nowa nazwa / anuluj (dialog zamokowany przez _resolve_collision) ──


def _seed_material(out_dir: Path, title: str) -> Path:
    d = material_dir_for(out_dir, title)
    d.mkdir(parents=True, exist_ok=True)
    (d / "metadata.json").write_text("{}", encoding="utf-8")
    return d


def _fake_engine(dialog: rd.RecordDialog, db_path: Path) -> None:
    dialog._engine = RecorderEngine(
        encoders={"hevc_nvenc": True},
        store=RecordingStore(db_path),
        process_factory=_fake_factory,
        concat_runner=_fake_concat,
    )


def test_collision_cancel_changes_nothing(
    dialog: rd.RecordDialog, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out"
    mat = _seed_material(out, "Nagranie")
    dialog._out_dir.set(str(out))
    dialog._title_edit.setText("Nagranie")
    monkeypatch.setattr(dialog, "_resolve_collision", lambda o, t: (rd.CollisionChoice.CANCEL, ""))
    dialog._on_start()
    assert dialog._session is None  # nie wystartowało
    assert (mat / "metadata.json").exists()  # stary materiał nietknięty


def test_collision_overwrite_replaces_material(
    dialog: rd.RecordDialog, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, qtbot: QtBot
) -> None:
    db_path = tmp_path / "library.sqlite3"
    _fake_engine(dialog, db_path)
    out = tmp_path / "out"
    mat = _seed_material(out, "Nagranie")
    (mat / "marker.txt").write_text("old", encoding="utf-8")
    dialog._out_dir.set(str(out))
    dialog._title_edit.setText("Nagranie")
    dialog._sys_audio.setChecked(False)
    monkeypatch.setattr(
        dialog, "_resolve_collision", lambda o, t: (rd.CollisionChoice.OVERWRITE, "")
    )
    dialog._on_start()
    assert not (mat / "marker.txt").exists()  # stary folder usunięty (nadpisanie)
    assert dialog._session is not None

    _stop_and_wait(dialog, qtbot)
    rows = RecordingStore(db_path).list_recordings(RecordingStatus.RECORDED)
    assert len(rows) == 1  # jeden materiał, bez duplikatu
    assert rows[0].title == "Nagranie"


def test_collision_rename_uses_new_name(
    dialog: rd.RecordDialog, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, qtbot: QtBot
) -> None:
    db_path = tmp_path / "library.sqlite3"
    _fake_engine(dialog, db_path)
    out = tmp_path / "out"
    _seed_material(out, "Nagranie")
    dialog._out_dir.set(str(out))
    dialog._title_edit.setText("Nagranie")
    dialog._sys_audio.setChecked(False)
    monkeypatch.setattr(
        dialog, "_resolve_collision", lambda o, t: (rd.CollisionChoice.RENAME, "Nagranie (2)")
    )
    dialog._on_start()
    assert dialog._title_edit.text() == "Nagranie (2)"  # tytuł zmieniony na wolny

    _stop_and_wait(dialog, qtbot)
    rows = RecordingStore(db_path).list_recordings(RecordingStatus.RECORDED)
    titles = {r.title for r in rows}
    assert "Nagranie (2)" in titles  # zapisane pod nową nazwą
    assert (out / "Nagranie" / "metadata.json").exists()  # stary materiał nietknięty


def test_collision_dialog_disables_rename_for_taken_name(qtbot: QtBot, qapp: QApplication) -> None:
    """M14: walidacja na żywo — wpisanie zajętej/pustej nazwy wyszarza „Zapisz pod nową nazwą"."""
    taken = {"zajeta"}
    dlg = rd._CollisionDialog("Nagranie", "Nagranie (2)", name_taken=lambda n: n in taken)
    qtbot.addWidget(dlg)

    # Stan startowy: proponowana wolna nazwa → przycisk aktywny, notka ukryta.
    assert dlg._rename_btn.isEnabled()
    assert not dlg._name_warn.isVisibleTo(dlg)

    dlg._name_edit.setText("zajeta")  # zajęta → wyłączony + notka
    assert not dlg._rename_btn.isEnabled()
    assert dlg._name_warn.isVisibleTo(dlg)

    dlg._name_edit.setText("wolna")  # znów wolna → aktywny, notka znika
    assert dlg._rename_btn.isEnabled()
    assert not dlg._name_warn.isVisibleTo(dlg)

    dlg._name_edit.setText("")  # pusta → wyłączony (Enter nie zaakceptuje), bez notki „zajęta"
    assert not dlg._rename_btn.isEnabled()
    assert not dlg._name_warn.isVisibleTo(dlg)


def test_collision_rename_revalidates_taken_name(
    dialog: rd.RecordDialog, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, qtbot: QtBot
) -> None:
    """M14: guard w _on_start — gdyby dialog puścił zajętą nazwę, tytuł spada na wolną.

    Nowe segmenty NIE wjeżdżają w istniejący (cudzy) materiał.
    """
    db_path = tmp_path / "library.sqlite3"
    _fake_engine(dialog, db_path)
    out = tmp_path / "out"
    _seed_material(out, "Nagranie")  # kolizja startowa
    other = _seed_material(out, "Zajeta")  # nazwa zwrócona z RENAME też jest zajęta
    (other / "marker.txt").write_text("cudze", encoding="utf-8")
    dialog._out_dir.set(str(out))
    dialog._title_edit.setText("Nagranie")
    dialog._sys_audio.setChecked(False)
    # Dialog (hipotetycznie) zwraca zajętą nazwę „Zajeta" — guard musi ją odrzucić.
    monkeypatch.setattr(
        dialog, "_resolve_collision", lambda o, t: (rd.CollisionChoice.RENAME, "Zajeta")
    )
    dialog._on_start()

    assert dialog._title_edit.text() == "Zajeta (2)"  # spadek na pierwszą wolną
    assert dialog._session is not None
    # Cudzy materiał „Zajeta" nietknięty (segmenty nowej sesji nie wjechały w jego folder).
    assert (other / "marker.txt").read_text(encoding="utf-8") == "cudze"

    _stop_and_wait(dialog, qtbot)
    rows = RecordingStore(db_path).list_recordings(RecordingStatus.RECORDED)
    titles = {r.title for r in rows}
    assert "Zajeta (2)" in titles  # zapisane pod wolną nazwą, nie wjechało w „Zajeta"


def test_out_of_library_warning_toggles(dialog: rd.RecordDialog, tmp_path: Path) -> None:
    # Fixture: default_recordings_dir = tmp_path/"out". Wewnątrz → notka ukryta, poza → widoczna.
    dialog._out_dir.set(str(tmp_path / "out" / "sesja"))
    dialog._update_out_of_lib_warn()
    assert dialog._out_of_lib_warn.isHidden()

    dialog._out_dir.set(str(tmp_path / "gdzie_indziej"))
    dialog._update_out_of_lib_warn()
    assert not dialog._out_of_lib_warn.isHidden()  # katalog poza biblioteką → ostrzeżenie
