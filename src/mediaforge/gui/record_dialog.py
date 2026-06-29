"""Dialog nagrywania ekranu/audio — cienki adapter Qt nad :mod:`core.engines.recorder`.

Używa widgetów z chodzkos-gui-kit (``PathEntry`` na katalog wyjściowy, ``LogView`` ze
statusami nagrywania przez ``level_colors``) — bez własnych widgetów ścieżki/logu. Cała
logika nagrywania (FFmpeg, segmentacja, odzysk) jest w ``core`` (Qt-free); tu zostaje
zbieranie opcji, timer/rozmiar przez ``QTimer`` i sterowanie sesją (Start/Pauza/Stop).

Wybór monitora jest DPI-aware: geometria ``QScreen`` przeliczana na piksele fizyczne
(x ``devicePixelRatio``), bo ``gdigrab`` operuje na pikselach fizycznych pulpitu.
"""

from __future__ import annotations

from pathlib import Path

from chodzkos_gui_kit.qt.widgets import LogView, PathEntry
from PySide6.QtCore import QTimer
from PySide6.QtGui import QGuiApplication, QScreen
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from mediaforge.core import config as cfg_mod
from mediaforge.core.detection import check_ffmpeg
from mediaforge.core.engines.ffmpeg_cmd import (
    PRESETS,
    AudioConfig,
    CaptureMode,
    CaptureSource,
)
from mediaforge.core.engines.recorder import RecorderEngine, RecorderSession, RecorderState
from mediaforge.core.library.db import Database
from mediaforge.core.library.recordings import RecordingStore

# Statusy nagrywania dla LogView (klucze ról palety — przeżywają zmianę motywu).
RECORD_LEVEL_COLORS = {"recording": "red", "paused": "accent2", "saved": "accent"}

_MODE_LABELS: list[tuple[str, CaptureMode]] = [
    ("Cały pulpit", CaptureMode.FULLSCREEN),
    ("Wybrany monitor", CaptureMode.REGION),
    ("Okno (po tytule)", CaptureMode.WINDOW),
    ("Region (x, y, szer, wys)", CaptureMode.REGION),
]


def _physical_geometry(screen: QScreen) -> tuple[int, int, int, int]:
    """Geometria monitora w pikselach fizycznych (DPI-aware) dla gdigrab."""
    geo = screen.geometry()
    dpr = screen.devicePixelRatio()
    return (
        int(geo.x() * dpr),
        int(geo.y() * dpr),
        int(geo.width() * dpr),
        int(geo.height() * dpr),
    )


class RecordDialog(QDialog):
    """Okno konfiguracji i sterowania nagrywaniem (źródło, jakość, audio, katalog)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Nagrywanie ekranu")
        self.setMinimumWidth(520)

        Database(cfg_mod.library_db_path()).migrate()
        self._engine = RecorderEngine(
            encoders=check_ffmpeg().get("encoders", {}),
            store=RecordingStore(cfg_mod.library_db_path()),
        )
        self._session: RecorderSession | None = None

        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._tick)

        self._build_ui()
        self._sync_controls()

    # ── Budowa UI ─────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        form = QFormLayout()

        self._title_edit = QLineEdit("Nagranie")
        self._title_edit.setToolTip("Nazwa materiału (folder + wpis w bibliotece)")
        form.addRow("Tytuł:", self._title_edit)

        self._mode_combo = QComboBox()
        for label, _mode in _MODE_LABELS:
            self._mode_combo.addItem(label)
        self._mode_combo.currentIndexChanged.connect(self._sync_controls)
        self._mode_combo.setToolTip("Co nagrywać: pulpit, monitor, okno albo region")
        form.addRow("Źródło:", self._mode_combo)

        self._monitor_combo = QComboBox()
        for i, screen in enumerate(QGuiApplication.screens()):
            _x, _y, w, h = _physical_geometry(screen)
            self._monitor_combo.addItem(f"Monitor {i + 1} — {w}x{h}")
        form.addRow("Monitor:", self._monitor_combo)

        self._window_edit = QLineEdit()
        self._window_edit.setPlaceholderText("Dokładny tytuł okna")
        form.addRow("Okno:", self._window_edit)

        self._region_edit = QLineEdit()
        self._region_edit.setPlaceholderText("x,y,szerokość,wysokość — np. 0,0,1920,1080")
        form.addRow("Region:", self._region_edit)

        self._preset_combo = QComboBox()
        self._preset_keys = list(PRESETS.keys())
        for key in self._preset_keys:
            self._preset_combo.addItem(PRESETS[key].label)
        self._preset_combo.setCurrentIndex(self._preset_keys.index("standard"))
        self._preset_combo.currentIndexChanged.connect(self._sync_controls)
        self._preset_combo.setToolTip("Preset jakości (kodek, FPS, bitrate)")
        form.addRow("Jakość:", self._preset_combo)

        self._sys_audio = QCheckBox("Dźwięk systemowy (WASAPI loopback)")
        self._sys_audio.setChecked(True)
        self._mic_audio = QCheckBox("Mikrofon")
        self._mix_audio = QCheckBox("Zmiksuj w jeden ślad")
        for box in (self._sys_audio, self._mic_audio):
            box.toggled.connect(self._sync_controls)
        form.addRow("Audio:", self._sys_audio)
        form.addRow("", self._mic_audio)
        form.addRow("", self._mix_audio)

        self._sys_device = QLineEdit()
        self._sys_device.setPlaceholderText("Urządzenie loopback (ffmpeg -list_devices)")
        form.addRow("Urz. systemowe:", self._sys_device)
        self._mic_device = QLineEdit()
        self._mic_device.setPlaceholderText("Urządzenie mikrofonu (ffmpeg -list_devices)")
        form.addRow("Urz. mikrofonu:", self._mic_device)

        self._out_dir = PathEntry(mode="dir", placeholder="Katalog wyjściowy nagrań")
        self._out_dir.set(str(cfg_mod.default_recordings_dir()))
        form.addRow("Zapis do:", self._out_dir)

        root.addLayout(form)

        status = QHBoxLayout()
        self._timer_label = QLabel("Czas: 00:00:00")
        self._size_label = QLabel("Szac. rozmiar: 0,0 MB")
        status.addWidget(self._timer_label)
        status.addStretch(1)
        status.addWidget(self._size_label)
        root.addLayout(status)

        buttons = QHBoxLayout()
        self._start_btn = QPushButton("● Nagrywaj")
        self._start_btn.setToolTip("Rozpocznij nagrywanie")
        self._start_btn.clicked.connect(self._on_start)
        self._pause_btn = QPushButton("⏸ Pauza")
        self._pause_btn.setToolTip("Wstrzymaj / wznów nagrywanie")
        self._pause_btn.clicked.connect(self._on_pause)
        self._stop_btn = QPushButton("⏹ Zatrzymaj")
        self._stop_btn.setToolTip("Zakończ i zapisz nagranie")
        self._stop_btn.clicked.connect(self._on_stop)
        buttons.addWidget(self._start_btn)
        buttons.addWidget(self._pause_btn)
        buttons.addWidget(self._stop_btn)
        root.addLayout(buttons)

        self._log = LogView(timestamps=True, level_colors=RECORD_LEVEL_COLORS)
        self._log.setMinimumHeight(120)
        self._log.setToolTip("Status nagrywania")
        root.addWidget(self._log)

        close_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_box.rejected.connect(self.reject)
        root.addWidget(close_box)

    # ── Stan kontrolek ─────────────────────────────────────────────────────────

    def _current_mode_index(self) -> int:
        return self._mode_combo.currentIndex()

    def _sync_controls(self) -> None:
        """Włącza/wyłącza pola zależne od trybu źródła, audio i stanu sesji."""
        idx = self._current_mode_index()
        self._monitor_combo.setEnabled(idx == 1)
        self._window_edit.setEnabled(idx == 2)
        self._region_edit.setEnabled(idx == 3)
        self._sys_device.setEnabled(self._sys_audio.isChecked())
        self._mic_device.setEnabled(self._mic_audio.isChecked())
        self._mix_audio.setEnabled(self._sys_audio.isChecked() and self._mic_audio.isChecked())

        recording = self._session is not None and self._session.state in (
            RecorderState.RECORDING,
            RecorderState.PAUSED,
        )
        self._start_btn.setEnabled(not recording)
        self._pause_btn.setEnabled(recording)
        self._stop_btn.setEnabled(recording)
        for w in (self._mode_combo, self._preset_combo, self._title_edit, self._out_dir):
            w.setEnabled(not recording)

    # ── Budowanie opcji z UI ────────────────────────────────────────────────────

    def _build_capture_source(self) -> CaptureSource:
        idx = self._current_mode_index()
        if idx == 0:
            return CaptureSource(mode=CaptureMode.FULLSCREEN)
        if idx == 1:
            screens = QGuiApplication.screens()
            mon = self._monitor_combo.currentIndex()
            region = _physical_geometry(screens[mon]) if 0 <= mon < len(screens) else None
            return CaptureSource(mode=CaptureMode.REGION, monitor=mon, region=region)
        if idx == 2:
            return CaptureSource(mode=CaptureMode.WINDOW, window_title=self._window_edit.text())
        return CaptureSource(mode=CaptureMode.REGION, region=self._parse_region())

    def _parse_region(self) -> tuple[int, int, int, int] | None:
        parts = [p.strip() for p in self._region_edit.text().split(",")]
        if len(parts) != 4:
            return None
        try:
            x, y, w, h = (int(p) for p in parts)
        except ValueError:
            return None
        return (x, y, w, h)

    def _build_audio_config(self) -> AudioConfig:
        return AudioConfig(
            system_audio=self._sys_audio.isChecked(),
            microphone=self._mic_audio.isChecked(),
            system_device=self._sys_device.text() or None,
            mic_device=self._mic_device.text() or None,
            mix=self._mix_audio.isChecked() and self._mix_audio.isEnabled(),
        )

    # ── Akcje ───────────────────────────────────────────────────────────────────

    def _on_start(self) -> None:
        quality = PRESETS[self._preset_keys[self._preset_combo.currentIndex()]]
        out_dir = Path(self._out_dir.get() or str(cfg_mod.default_recordings_dir()))
        from mediaforge.core.engines.recorder import safe_filename

        work_dir = out_dir / safe_filename(self._title_edit.text()) / "_work"
        self._session = self._engine.new_session(
            source=self._build_capture_source(),
            audio=self._build_audio_config(),
            quality=quality,
            work_dir=work_dir,
        )
        try:
            self._session.start()
        except Exception as exc:  # pokazujemy błąd w logu, nie wywalamy GUI
            self._session = None
            self._log.append_line(f"Nie udało się rozpocząć: {exc}", "error")
            self._sync_controls()
            return
        self._log.append_line("Nagrywanie rozpoczęte", "recording")
        self._timer.start()
        self._sync_controls()

    def _on_pause(self) -> None:
        if self._session is None:
            return
        if self._session.state is RecorderState.RECORDING:
            self._session.pause()
            self._pause_btn.setText("▶ Wznów")
            self._log.append_line("Wstrzymano", "paused")
        elif self._session.state is RecorderState.PAUSED:
            self._session.resume()
            self._pause_btn.setText("⏸ Pauza")
            self._log.append_line("Wznowiono", "recording")
        self._sync_controls()

    def _on_stop(self) -> None:
        if self._session is None:
            return
        self._timer.stop()
        self._session.stop()
        self._log.append_line("Składanie segmentów…", "info")
        try:
            artifact = self._engine.finalize_to_library(
                self._session,
                title=self._title_edit.text(),
                output_dir=Path(self._out_dir.get() or str(cfg_mod.default_recordings_dir())),
            )
        except Exception as exc:  # błąd składania pokazujemy w logu, nie wywalamy GUI
            self._log.append_line(f"Błąd finalizacji: {exc}", "error")
            self._session = None
            self._sync_controls()
            return
        target = artifact.video_path or artifact.audio_path
        self._log.append_line(f"Zapisano: {target}", "saved")
        cfg_parent = self.parent()
        self._session = None
        self._pause_btn.setText("⏸ Pauza")
        self._sync_controls()
        _ = cfg_parent  # rezerwacja: odświeżenie biblioteki w głównym oknie (S2)

    # ── Timer / telemetria ───────────────────────────────────────────────────────

    def _tick(self) -> None:
        if self._session is None:
            return
        st = self._session.status()
        secs = int(st.elapsed_seconds)
        self._timer_label.setText(
            f"Czas: {secs // 3600:02d}:{secs % 3600 // 60:02d}:{secs % 60:02d}"
        )
        self._size_label.setText(f"Szac. rozmiar: {st.estimated_mb:.1f} MB".replace(".", ","))
