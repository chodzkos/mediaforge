"""Powłoka głównego okna mediaforge — wpina chodzkos-gui-kit (S0).

Górny pasek wg GUI_STANDARD §6 (logo + przełącznik motywu + About), pusta
biblioteka (placeholder), strumień statusu przez kitowy ``LogView`` i dolny pasek
z wykrytymi narzędziami (ffmpeg/whisper/CUDA + tier). Motyw i belkę DWM liczy
``ThemeManager`` kitu; geometria okna persystowana przez ``Config`` (nie QSettings).

Zero hardcodowanych hexów i zero globalnego QSS — kolory z palety kitu, a stylowanie
generycznych widgetów (``QToolButton``/``QLineEdit``) pominięte (przeciekłoby do
dialogów kitu). Re-render historii logu po zmianie motywu robi ``LogView.set_theme``.
"""

from __future__ import annotations

from typing import Any

from chodzkos_gui_kit.config import Config
from chodzkos_gui_kit.qt.theme import ThemeManager, ThemeSetting, current_palette
from chodzkos_gui_kit.qt.widgets import LogView
from PySide6.QtCore import QByteArray, QObject, QRunnable, QThreadPool, Signal
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from mediaforge import __version__
from mediaforge.core import config as cfg_mod
from mediaforge.core import detection
from mediaforge.gui.about import open_about
from mediaforge.gui.help_window import open_help
from mediaforge.gui.library_widget import LibraryWidget
from mediaforge.gui.record_dialog import RecordDialog

# Etykiety cyklu motywu (kolejność: auto → jasny → ciemny → auto).
_THEME_CYCLE: tuple[ThemeSetting, ...] = ("auto", "light", "dark")
_THEME_LABELS = {"auto": "Motyw: Auto", "light": "Motyw: Jasny", "dark": "Motyw: Ciemny"}

# Kolory statusów dla LogView (nazwy ról palety — przeżywają zmianę motywu).
_LOG_LEVEL_COLORS = {"recording": "red", "transcribing": "accent2"}


class _EnvSignals(QObject):
    """Most sygnałowy dla :class:`_EnvironmentProbe` (QRunnable nie jest QObject)."""

    report_ready = Signal(dict)


class _EnvironmentProbe(QRunnable):
    """Uruchamia ``detection.check_all`` w puli wątków — sondy (nvidia-smi/ffmpeg/…) poza UI.

    Wynik wraca sygnałem ``report_ready(dict)`` na wątek UI. Most sygnałowy (``signals``) jest
    WŁASNOŚCIĄ okna (żyje niezależnie od auto-usuwanego runnable), więc emisja jest bezpieczna.
    """

    def __init__(
        self, signals: _EnvSignals, *, whispercpp_path: str | None, litellm_base_url: str | None
    ) -> None:
        super().__init__()
        self._signals = signals
        self._whispercpp_path = whispercpp_path
        self._litellm_base_url = litellm_base_url

    def run(self) -> None:
        # probe_encoders=True: wybór enkodera nagrania (RecordDialog) musi znać realną
        # używalność, nie tylko obecność w buildzie (NVENC-widmo → cichy zgon nagrania).
        # Bezpiecznie tu, bo cała sonda i tak biegnie w puli wątków, poza wątkiem UI.
        report = detection.check_all(
            whispercpp_path=self._whispercpp_path,
            litellm_base_url=self._litellm_base_url,
            probe_encoders=True,
        )
        self._signals.report_ready.emit(report)


class MainWindow(QMainWindow):
    """Główne okno: górny pasek, pusta biblioteka, log statusu, pasek narzędzi."""

    def __init__(self, theme_manager: ThemeManager, config: Config) -> None:
        super().__init__()
        self._theme_manager = theme_manager
        self._config = config
        self.setWindowTitle("mediaforge")
        # Minimum dobrane tak, by panel metadanych (rząd przycisków) mieścił się bez ściskania;
        # resize to sensowny start (geometria z configu i tak nadpisze przy ponownym otwarciu).
        self.setMinimumSize(900, 600)
        self.resize(1100, 760)

        # Cache raportu środowiska (z sondy w tle) — m.in. wynik ffmpeg dla RecordDialog, żeby
        # nie sondować drugi raz. Most sygnałowy jest polem okna (przeżywa auto-usuwany runnable).
        self._env_report: dict[str, Any] | None = None
        self._env_signals = _EnvSignals()
        self._env_signals.report_ready.connect(self._on_report_ready)

        self._build_ui()
        self._restore_geometry()
        self._theme_manager.theme_changed.connect(self._on_theme_changed)
        self._report_environment()

    # ── Budowa UI ─────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        self._build_menu()
        root.addLayout(self._build_topbar())

        self._library = LibraryWidget()
        root.addWidget(self._library, stretch=1)

        self._log = LogView(timestamps=True, level_colors=_LOG_LEVEL_COLORS)
        self._log.setMinimumHeight(140)
        self._log.setToolTip("Status operacji (nagrywanie, transkrypcja, zadania)")
        root.addWidget(self._log)

        self._status_label = QLabel("")
        self.statusBar().addPermanentWidget(self._status_label)

    def _build_topbar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setSpacing(8)

        logo = QLabel("mediaforge")
        logo_font = logo.font()
        logo_font.setPointSize(logo_font.pointSize() + 4)
        logo_font.setBold(True)
        logo.setFont(logo_font)
        bar.addWidget(logo)

        version = QLabel(f"v{__version__}")
        version.setEnabled(False)
        bar.addWidget(version)

        record = QToolButton()
        record.setText("● Nagrywaj")
        record.setToolTip("Nagraj ekran / dźwięk (FFmpeg + NVENC)")
        record.clicked.connect(self._open_recorder)
        bar.addWidget(record)

        bar.addStretch(1)

        self._theme_button = QToolButton()
        self._theme_button.setToolTip("Przełącz motyw (auto / jasny / ciemny)")
        self._theme_button.clicked.connect(self._cycle_theme)
        self._sync_theme_button()
        bar.addWidget(self._theme_button)

        return bar

    def _build_menu(self) -> None:
        """Menu „Pomoc": Pomoc (F1) → okno pomocy z zakładkami; O programie → dialog kitowy."""
        help_menu = self.menuBar().addMenu("&Pomoc")

        help_action = QAction("&Pomoc", self)
        help_action.setShortcut(QKeySequence.StandardKey.HelpContents)  # F1
        help_action.triggered.connect(lambda: open_help(self))
        help_menu.addAction(help_action)

        help_menu.addSeparator()

        about_action = QAction("&O programie", self)
        about_action.triggered.connect(lambda: open_about(self))
        help_menu.addAction(about_action)

    # ── Nagrywanie ──────────────────────────────────────────────────────────--

    def _open_recorder(self) -> None:
        """Otwiera dialog nagrywania ekranu/audio (S1); po zamknięciu odświeża bibliotekę.

        Wynik ffmpeg podajemy z cache raportu startowego — dialog nie sonduje ffmpeg drugi raz.
        Gdy sonda startowa jeszcze nie wróciła (``None``), dialog sam zrobi fallbackową sondę.
        """
        ffmpeg_probe = self._env_report.get("ffmpeg") if self._env_report else None
        dialog = RecordDialog(self, ffmpeg_probe=ffmpeg_probe)
        dialog.exec()
        self._library.refresh_all()

    # ── Motyw ───────────────────────────────────────────────────────────────--

    def _cycle_theme(self) -> None:
        """Przełącza ustawienie motywu na kolejne w cyklu i stosuje je."""
        current = self._theme_manager.setting
        idx = _THEME_CYCLE.index(current) if current in _THEME_CYCLE else 0
        nxt = _THEME_CYCLE[(idx + 1) % len(_THEME_CYCLE)]
        self._theme_manager.apply(nxt)  # zapisuje do configu i przemalowuje

    def _sync_theme_button(self) -> None:
        self._theme_button.setText(_THEME_LABELS.get(self._theme_manager.setting, "Motyw"))

    def _on_theme_changed(self, _palette: object) -> None:
        """Po zmianie motywu: zaktualizuj etykietę i przemaluj historię logu."""
        self._sync_theme_button()
        self._log.set_theme(current_palette())

    # ── Środowisko / status ─────────────────────────────────────────────────--

    def _report_environment(self) -> None:
        """Startuje sondę środowiska w tle; status uzupełni :meth:`_on_report_ready`.

        Sondy (nvidia-smi, ffmpeg, yt-dlp, LiteLLM) to subprocessy/sieć — na wątku UI zamroziłyby
        start okna. Pasek statusu startuje z „wykrywanie środowiska…" i wypełnia się po sygnale.
        """
        self._status_label.setText("Wykrywanie środowiska…")
        probe = _EnvironmentProbe(
            self._env_signals,
            whispercpp_path=cfg_mod.get_whispercpp_path(self._config),
            litellm_base_url=cfg_mod.get_litellm_base_url(self._config),
        )
        QThreadPool.globalInstance().start(probe)

    def _on_report_ready(self, report: dict[str, Any]) -> None:
        """Uzupełnia pasek statusu i log startowy po powrocie sondy środowiska (wątek UI)."""
        self._env_report = report  # cache (m.in. ffmpeg dla RecordDialog — bez drugiej sondy)
        self._status_label.setText(detection.status_line(report))
        self._log.log_info("mediaforge gotowy.")
        ff = "OK" if report["ffmpeg"]["available"] else "brak"
        wh = "OK" if report["whispercpp"]["available"] else "brak"
        self._log.append_line(f"FFmpeg: {ff} · whisper.cpp: {wh}", "info")
        gpu_info = report["gpu"]
        has_cuda = bool(gpu_info["available"])
        gpu = f"{gpu_info['name']} ({gpu_info['vram_gb']:g} GB)" if has_cuda else "brak CUDA"
        self._log.append_line(
            f"GPU: {gpu} · profil obliczeniowy: Tier {report['compute']['tier']}",
            "info" if has_cuda else "warn",
        )

    # ── Geometria okna (persystencja przez Config) ─────────────────────────────

    def _restore_geometry(self) -> None:
        saved = cfg_mod.get_window_geometry(self._config)
        if saved:
            self.restoreGeometry(QByteArray.fromBase64(saved.encode("ascii")))

    def start_jobs(self) -> None:
        """Uruchamia kolejkę zadań biblioteki (woła entry point po pokazaniu okna)."""
        self._library.start_jobs()

    def closeEvent(self, event: QCloseEvent) -> None:
        """Zapisuje geometrię okna, domyka kolejkę i config przed zamknięciem.

        Nie blokujemy zamknięcia dłużej niż ``stop()`` (do 30 s na domknięcie bieżących zadań);
        gdy nie zdążą, wpisujemy ostrzeżenie — zadania ``running`` odzyskają się przy następnym
        starcie (:meth:`JobStore.recover_stale`), więc nic nie ginie.
        """
        drained = self._library.shutdown()  # zatrzymaj polling + wątek roboczy kolejki
        if not drained:
            self._log.append_line(
                "Zadania w toku zostaną przywrócone przy następnym starcie", "warn"
            )
        geometry = bytes(self.saveGeometry().toBase64().data()).decode("ascii")
        cfg_mod.set_window_geometry(self._config, geometry)
        self._config.save_now()  # zamknięcie = zapis bezwarunkowy (omija debounce)
        super().closeEvent(event)
