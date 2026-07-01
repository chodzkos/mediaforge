"""Dialog nagrywania ekranu/audio — cienki adapter Qt nad :mod:`core.engines.recorder`.

Używa widgetów z chodzkos-gui-kit (``PathEntry`` na katalog wyjściowy, ``LogView`` ze
statusami nagrywania przez ``level_colors``) — bez własnych widgetów ścieżki/logu. Cała
logika nagrywania (FFmpeg, segmentacja, odzysk) jest w ``core`` (Qt-free); tu zostaje
zbieranie opcji, timer/rozmiar przez ``QTimer`` i sterowanie sesją (Start/Pauza/Stop).

Wybór monitora trafia jako ``output_idx`` do ``ddagrab`` (capture per-output). Geometria
``QScreen`` w pikselach fizycznych (x ``devicePixelRatio``) daje rozdzielczość monitora —
do walidacji regionu (crop) i szacowania rozmiaru. Tryb „Okno (po tytule)" usunięty:
ddagrab nie zna okien.
"""

from __future__ import annotations

import math
from enum import StrEnum
from pathlib import Path

from chodzkos_gui_kit.qt.theme import current_palette
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
from mediaforge.core.engines.dshow_devices import DshowAudioDevice, list_dshow_audio_devices
from mediaforge.core.engines.ffmpeg_cmd import (
    PRESETS,
    AudioConfig,
    CaptureMode,
    CaptureSource,
)
from mediaforge.core.engines.recorder import (
    RecorderEngine,
    RecorderSession,
    RecorderState,
    discard_material_dir,
    material_exists,
    next_free_title,
    safe_filename,
)
from mediaforge.core.library.db import Database
from mediaforge.core.library.recordings import RecordingStore

# Statusy nagrywania dla LogView (klucze ról palety — przeżywają zmianę motywu).
RECORD_LEVEL_COLORS = {"recording": "red", "paused": "accent2", "saved": "accent"}

# ddagrab łapie wybrany monitor (output_idx); region = crop wewnątrz niego. Brak trybu okna.
_MODE_LABELS: list[tuple[str, CaptureMode]] = [
    ("Cały monitor", CaptureMode.FULLSCREEN),
    ("Region (x, y, szer, wys)", CaptureMode.REGION),
]


def _physical_geometry(screen: QScreen) -> tuple[int, int, int, int]:
    """Geometria monitora w pikselach fizycznych (DPI-aware) — podgląd rozmiaru/tryb Region."""
    geo = screen.geometry()
    dpr = screen.devicePixelRatio()
    return (
        int(geo.x() * dpr),
        int(geo.y() * dpr),
        int(geo.width() * dpr),
        int(geo.height() * dpr),
    )


class CollisionChoice(StrEnum):
    """Decyzja użytkownika przy kolizji nazwy nagrania."""

    OVERWRITE = "overwrite"
    RENAME = "rename"
    CANCEL = "cancel"


class _CollisionDialog(QDialog):
    """Wybór przy zajętej nazwie: Nadpisz / Zapisz pod nową nazwą / Anuluj.

    Kit nie ma generycznego dialogu wyboru (tylko file-dialogi), więc budujemy własny
    QDialog — motyw dziedziczy z ``ThemeManager`` (świadomie NIE natywny ``QMessageBox``).
    Domyślny (Enter) = „Zapisz pod nową nazwą" — bezpieczny, nie traci danych.
    """

    def __init__(self, name: str, proposed: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Nazwa już zajęta")
        self._choice = CollisionChoice.CANCEL

        root = QVBoxLayout(self)
        root.addWidget(QLabel(f"Materiał «{name}» już istnieje."))
        self._name_edit = QLineEdit(proposed)
        self._name_edit.setToolTip("Nazwa dla nowego nagrania (przy zapisie pod nową nazwą)")
        root.addWidget(self._name_edit)

        row = QHBoxLayout()
        overwrite_btn = QPushButton("Nadpisz")
        overwrite_btn.setToolTip("Usuń stary materiał i zapisz nowy pod tą nazwą")
        overwrite_btn.clicked.connect(self._choose_overwrite)
        rename_btn = QPushButton("Zapisz pod nową nazwą")
        rename_btn.setDefault(True)
        rename_btn.clicked.connect(self._choose_rename)
        cancel_btn = QPushButton("Anuluj")
        cancel_btn.clicked.connect(self.reject)
        row.addWidget(overwrite_btn)
        row.addStretch(1)
        row.addWidget(cancel_btn)
        row.addWidget(rename_btn)
        root.addLayout(row)

    def _choose_overwrite(self) -> None:
        self._choice = CollisionChoice.OVERWRITE
        self.accept()

    def _choose_rename(self) -> None:
        self._choice = CollisionChoice.RENAME
        self.accept()

    def choice(self) -> CollisionChoice:
        return self._choice

    def chosen_name(self) -> str:
        return self._name_edit.text().strip()


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
        # Pre-roll (UX): tyle sekund „Przygotowuję…" przed „Nagrywam", by przeczekać zimny
        # start ddagrab. FFmpeg nic nie tnie — user zaczyna treść po sygnale (głowa przed nią).
        self._preroll_sec = cfg_mod.get_record_preroll_sec(cfg_mod.load())
        self._recording_announced = False

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
        self._mode_combo.setToolTip("Co nagrywać: cały monitor albo region wewnątrz niego")
        form.addRow("Źródło:", self._mode_combo)

        self._monitor_combo = QComboBox()
        for i, screen in enumerate(QGuiApplication.screens()):
            _x, _y, w, h = _physical_geometry(screen)
            self._monitor_combo.addItem(f"Monitor {i + 1} — {w}x{h}")
        self._monitor_combo.setToolTip("Monitor do nagrania (output_idx ddagrab)")
        form.addRow("Monitor:", self._monitor_combo)

        self._region_edit = QLineEdit()
        self._region_edit.setPlaceholderText(
            "x,y,szerokość,wysokość względem monitora — np. 0,0,1920,1080"
        )
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

        # Enumeracja urządzeń dshow (Windows-only; inny OS → []), rozdzielona na loopback
        # (dźwięk systemowy) i mikrofony. Combo edytowalne — zachowuje ręczne wpisanie.
        devices = list_dshow_audio_devices()
        loopback = [d for d in devices if d.is_loopback]
        mics = [d for d in devices if not d.is_loopback]
        self._has_loopback = bool(loopback)

        self._sys_device = QComboBox()
        self._sys_device.setEditable(True)
        self._sys_device.setToolTip(
            "Urządzenie loopback dźwięku systemowego (Stereo Mix / VB-Cable)"
        )
        self._fill_device_combo(self._sys_device, loopback)
        form.addRow("Urz. systemowe:", self._sys_device)
        self._mic_device = QComboBox()
        self._mic_device.setEditable(True)
        self._mic_device.setToolTip("Urządzenie mikrofonu")
        self._fill_device_combo(self._mic_device, mics)
        form.addRow("Urz. mikrofonu:", self._mic_device)

        # Bez urządzenia loopback nie nagra się dźwięk systemowy — enumeracja go nie tworzy.
        self._loopback_warn = QLabel(
            "Brak urządzenia do nagrania dźwięku systemowego. Włącz „Miks stereo” "
            "(Panel sterowania → Dźwięk → Nagrywanie → ppm → Pokaż wyłączone urządzenia) "
            "albo zainstaluj wirtualny kabel (VB-Cable). Bez tego nagra się tylko obraz/mikrofon."
        )
        self._loopback_warn.setWordWrap(True)
        self._loopback_warn.setStyleSheet(f"color: {current_palette().amber};")
        form.addRow("", self._loopback_warn)

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
        self._monitor_combo.setEnabled(True)  # zawsze: wybór monitora (output_idx) dla obu trybów
        self._region_edit.setEnabled(idx == 1)
        self._sys_device.setEnabled(self._sys_audio.isChecked())
        self._mic_device.setEnabled(self._mic_audio.isChecked())
        self._mix_audio.setEnabled(self._sys_audio.isChecked() and self._mic_audio.isChecked())
        # Ostrzeżenie o braku loopbacku tylko, gdy dźwięk systemowy jest włączony.
        self._loopback_warn.setVisible(self._sys_audio.isChecked() and not self._has_loopback)

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
        """Buduje źródło z UI. Region (gdy wybrany) jest walidowany — może rzucić ValueError."""
        mon = max(0, self._monitor_combo.currentIndex())
        if self._current_mode_index() == 0:
            return CaptureSource(mode=CaptureMode.FULLSCREEN, monitor=mon, region=None)
        return CaptureSource(
            mode=CaptureMode.REGION, monitor=mon, region=self._validated_region(mon)
        )

    def _validated_region(self, monitor: int) -> tuple[int, int, int, int]:
        """Region z UI sprawdzony, że mieści się w monitorze (crop poza zakres = błąd ffmpeg).

        Współrzędne są WZGLĘDEM wybranego monitora (0,0 = jego lewy-górny róg).
        """
        region = self._parse_region()
        if region is None:
            raise ValueError("Region: podaj x,y,szerokość,wysokość (np. 0,0,1920,1080)")
        x, y, w, h = region
        if w <= 0 or h <= 0:
            raise ValueError("Region: szerokość i wysokość muszą być dodatnie")
        screens = QGuiApplication.screens()
        if 0 <= monitor < len(screens):
            _mx, _my, mon_w, mon_h = _physical_geometry(screens[monitor])
            if x < 0 or y < 0 or x + w > mon_w or y + h > mon_h:
                raise ValueError(
                    f"Region {x},{y},{w},{h} wykracza poza monitor {mon_w}x{mon_h} "
                    "(współrzędne są względem wybranego monitora)"
                )
        return region

    def _parse_region(self) -> tuple[int, int, int, int] | None:
        parts = [p.strip() for p in self._region_edit.text().split(",")]
        if len(parts) != 4:
            return None
        try:
            x, y, w, h = (int(p) for p in parts)
        except ValueError:
            return None
        return (x, y, w, h)

    def _fill_device_combo(self, combo: QComboBox, devices: list[DshowAudioDevice]) -> None:
        """Wypełnia combo: tekst = przyjazna nazwa, data = alt_name (jednoznaczna do -i audio=)."""
        combo.clear()
        for dev in devices:
            combo.addItem(dev.name, dev.alt_name)
        combo.addItem("", "")  # opcja: brak / wpisz ręcznie
        combo.setCurrentIndex(0 if devices else combo.count() - 1)

    @staticmethod
    def _device_value(combo: QComboBox) -> str | None:
        """alt_name (itemData) gdy tekst odpowiada urządzeniu z listy; inaczej wpisany tekst."""
        text = combo.currentText().strip()
        if not text:
            return None
        idx = combo.findText(text)
        if idx >= 0:
            data = combo.itemData(idx)
            if isinstance(data, str) and data:
                return data  # alt_name — odporna na kolizje/znaki w nazwie
        return text

    def _build_audio_config(self) -> AudioConfig:
        return AudioConfig(
            system_audio=self._sys_audio.isChecked(),
            microphone=self._mic_audio.isChecked(),
            system_device=self._device_value(self._sys_device),
            mic_device=self._device_value(self._mic_device),
            mix=self._mix_audio.isChecked() and self._mix_audio.isEnabled(),
        )

    # ── Akcje ───────────────────────────────────────────────────────────────────

    def _resolve_collision(self, out_dir: Path, title: str) -> tuple[CollisionChoice, str]:
        """Pyta użytkownika przy zajętej nazwie; zwraca (wybór, nazwa). Seam do testów."""
        dlg = _CollisionDialog(title, next_free_title(out_dir, title), self)
        dlg.exec()
        return dlg.choice(), dlg.chosen_name()

    def _on_start(self) -> None:
        quality = PRESETS[self._preset_keys[self._preset_combo.currentIndex()]]
        out_dir = Path(self._out_dir.get() or str(cfg_mod.default_recordings_dir()))
        title = self._title_edit.text()

        # Kolizja nazwy: stare nagranie o tej nazwie zmieszałoby się z nowym (segmenty +
        # metadata/transkrypt). Pytamy: nadpisz / nowa nazwa / anuluj (bez cichej utraty danych).
        if material_exists(out_dir, title):
            choice, new_name = self._resolve_collision(out_dir, title)
            if choice is CollisionChoice.CANCEL:
                self._log.append_line("Nagrywanie anulowane — nazwa zajęta", "paused")
                return
            if choice is CollisionChoice.OVERWRITE:
                discard_material_dir(out_dir, title)
                self._log.append_line(f"Nadpisuję materiał «{title}»", "paused")
            else:  # RENAME
                title = new_name or next_free_title(out_dir, title)
                self._title_edit.setText(title)

        work_dir = out_dir / safe_filename(title) / "_work"
        try:
            # Walidacja regionu w GUI — nie puszczamy poza-zakresowego crop do ffmpeg.
            source = self._build_capture_source()
        except ValueError as exc:
            self._log.append_line(str(exc), "error")
            return
        self._session = self._engine.new_session(
            source=source,
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
        self._recording_announced = False
        if self._preroll_sec > 0:
            self._log.append_line(
                f"Przygotowuję nagranie… (pre-roll {self._preroll_sec}s)", "paused"
            )
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

    def _recorded_seconds(self, elapsed: float) -> float | None:
        """Czas realnego nagrania = elapsed FFmpeg - pre-roll (odcięta głowa).

        ``None`` gdy wciąż trwa pre-roll (głowa jeszcze odcinana) — licznik startuje dopiero
        od sygnału „Nagrywam", nie od uruchomienia ffmpeg.
        """
        recorded = elapsed - self._preroll_sec
        return recorded if recorded >= 0 else None

    def _tick(self) -> None:
        if self._session is None:
            return
        st = self._session.status()
        recorded = self._recorded_seconds(st.elapsed_seconds)
        if recorded is None:  # faza pre-roll: pokaż odliczanie „Przygotowuję…"
            remaining = max(1, math.ceil(self._preroll_sec - st.elapsed_seconds))
            self._timer_label.setText(f"Przygotowuję… {remaining}s")
            return
        if not self._recording_announced:
            self._recording_announced = True
            self._log.append_line("● Nagrywam", "recording")
        secs = int(recorded)
        self._timer_label.setText(
            f"Czas: {secs // 3600:02d}:{secs % 3600 // 60:02d}:{secs % 60:02d}"
        )
        self._size_label.setText(f"Szac. rozmiar: {st.estimated_mb:.1f} MB".replace(".", ","))
