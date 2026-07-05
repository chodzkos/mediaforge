"""Panel szczegółów materiału — wydzielony i odsprzężony od logiki biblioteki.

Kandydat do ekstrakcji do ``chodzkos-gui-kit``: NIE zna store/kolejki/configu. Dostaje
``(folder, MaterialMetadata)`` w :meth:`load`, wystawia edytowalne pola i przyciski akcji;
zapis metadanych i kolejkowanie zadań robi właściciel (``LibraryWidget``), podłączając się do
sygnałów ``clicked`` przycisków. Odczyt edycji przez :meth:`apply_edits` (czysta transformacja
na danych rdzenia — bez IO).

**Layout naprawiony U ŹRÓDŁA (nie łatanie kolejnego widgetu).** Jawny podział pionowy na trzy
rodzaje sekcji:

* STAŁE — miniatura, formularz metadanych, rząd akcji: naturalna wysokość, nie rosną;
* ROSNĄCE — „Info" z ``wordWrap``: pionowo ``Minimum``, rośnie w dół z długą treścią;
* jedna ROZCIĄGLIWA — podgląd streszczenia: pionowo ``Expanding`` + ``stretch=1`` w layoucie,
  z własnym scrollem wewnętrznym (rośnie ON, nie odpycha przycisków).

Żaden element o zmiennej treści nie ma stałej/maksymalnej wysokości
(``setFixedHeight``/``setMaximumHeight``) — długi tekst rośnie, zamiast nachodzić na sąsiada.
Całość opakowana w ``QScrollArea`` (``widgetResizable``): gdy suma minimalnych wysokości
przerośnie okno, pojawia się scroll. Dzięki temu dodanie KOLEJNEGO elementu do panelu
(S5/S6) nie może już wywołać nakładania — nie będzie wymagać kolejnego fixu layoutu.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from mediaforge.core.library.material import MaterialMetadata
from mediaforge.core.library.slides import SLIDES_DIRNAME

_SLIDE_ROLE = int(Qt.ItemDataRole.UserRole)


def _fmt_duration(seconds: float | None) -> str:
    if not seconds:
        return "—"
    total = int(seconds)
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def _fmt_timestamp(seconds: int) -> str:
    """Sekunda → ``m:ss`` (albo ``h:mm:ss``) — etykieta timestampu przy miniaturze slajdu."""
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class MaterialDetailsPanel(QScrollArea):
    """Miniatura + metadane + akcje + podgląd streszczenia dla jednego materiału.

    Przyciski (``save_btn``/``transcribe_btn``/``summarize_btn``/``open_btn``/``delete_btn``)
    i pola edycji (``title``/``presenter``/``organizer``/``category``/``tags``/``cloud_ok``)
    są publiczne — właściciel wiąże je i czyta. Panel sam NIE wykonuje żadnej akcji na danych.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # widgetResizable: zawartość dopasowuje się do viewportu; scroll gdy za mało miejsca.
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.Shape.NoFrame)

        content = QWidget()
        self.setWidget(content)
        col = QVBoxLayout(content)

        # ── STAŁE: miniatura (minimum, nie stała — może pokazać obraz lub tekst) ──
        self._thumb = QLabel("(brak podglądu)")
        self._thumb.setMinimumHeight(150)
        self._thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._thumb.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        col.addWidget(self._thumb)

        # ── STAŁE: formularz metadanych (jednowierszowe pola) ────────────────────
        form = QFormLayout()
        self.title = QLineEdit()
        form.addRow("Tytuł:", self.title)
        self.presenter = QLineEdit()
        form.addRow("Prowadzący:", self.presenter)
        self.organizer = QLineEdit()
        form.addRow("Organizator:", self.organizer)
        self.category = QLineEdit()
        form.addRow("Kategoria:", self.category)
        self.tags = QLineEdit()
        self.tags.setPlaceholderText("tagi po przecinku")
        form.addRow("Tagi:", self.tags)
        # Zgoda na chmurę (fail-safe): bez niej streszczenie idzie WYŁĄCZNIE lokalnie.
        self.cloud_ok = QCheckBox("Zezwól na przetwarzanie w chmurze")
        self.cloud_ok.setToolTip("Bez zgody materiał jest przetwarzany wyłącznie lokalnie")
        form.addRow("Prywatność:", self.cloud_ok)
        col.addLayout(form)

        # ── ROSNĄCE: „Info" (wordWrap) — pionowo Minimum, rośnie w dół z treścią ─
        self._info = QLabel("")
        self._info.setWordWrap(True)
        self._info.setEnabled(False)
        self._info.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        col.addWidget(self._info)

        # ── STAŁE: rząd akcji ────────────────────────────────────────────────────
        actions = QHBoxLayout()
        self.save_btn = QPushButton("Zapisz metadane")
        actions.addWidget(self.save_btn)
        self.transcribe_btn = QPushButton("Transkrybuj")
        self.transcribe_btn.setToolTip("Dodaj transkrypcję (whisper.cpp) do kolejki")
        actions.addWidget(self.transcribe_btn)
        self.summarize_btn = QPushButton("Streść")
        self.summarize_btn.setToolTip("Streść transkrypt przez gateway (aktywne po transkrypcji)")
        actions.addWidget(self.summarize_btn)
        self.attach_slides_btn = QPushButton("Podłącz slajdy")
        self.attach_slides_btn.setToolTip(
            "Skopiuj obrazy slajdów (z przeglądarki: prawy→zapisz albo rozszerzenie Image "
            "Downloader) do folderu materiału. Nazwy z czasem (mp.pl _450s) mapują się na moment"
        )
        actions.addWidget(self.attach_slides_btn)
        self.open_btn = QPushButton("Otwórz folder")
        actions.addWidget(self.open_btn)
        actions.addStretch(1)
        self.delete_btn = QPushButton("Usuń")
        self.delete_btn.setToolTip("Trwale usuń materiał (nagranie + transkrypt + metadane)")
        actions.addWidget(self.delete_btn)
        col.addLayout(actions)

        # ── ROZCIĄGLIWA: podgląd streszczenia (Markdown, własny scroll) ──────────
        col.addWidget(QLabel("Streszczenie:"))
        self._summary_view = QTextBrowser()
        self._summary_view.setOpenExternalLinks(True)
        self._summary_view.setPlaceholderText("(brak streszczenia — użyj „Streść”)")
        self._summary_view.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        col.addWidget(self._summary_view, stretch=1)

        # ── ROZCIĄGLIWA: galeria slajdów (IconMode, własny scroll wewnętrzny) ────
        # Kolejna sekcja rozciągliwa PO fixie layoutu z fix/s4-polish — panel jest w QScrollArea,
        # więc długie Info + streszczenie + galeria naraz przy wąskim oknie nie nachodzą (scroll).
        col.addWidget(QLabel("Slajdy:"))
        self._slides_gallery = QListWidget()
        self._slides_gallery.setViewMode(QListWidget.ViewMode.IconMode)
        self._slides_gallery.setIconSize(QSize(160, 120))
        self._slides_gallery.setResizeMode(QListWidget.ResizeMode.Adjust)
        self._slides_gallery.setMovement(QListWidget.Movement.Static)
        self._slides_gallery.setSpacing(6)
        self._slides_gallery.setMinimumHeight(140)
        self._slides_gallery.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding
        )
        self._slides_gallery.itemActivated.connect(self._open_slide)
        self._slides_gallery.itemClicked.connect(self._open_slide)
        col.addWidget(self._slides_gallery, stretch=1)

        self._folder: Path | None = None
        self.set_editing_enabled(False)

    # ── API dla właściciela ───────────────────────────────────────────────────

    def load(self, folder: Path, meta: MaterialMetadata) -> None:
        """Wypełnia panel danymi materiału (pola, Info, miniatura, streszczenie, galeria)."""
        self._folder = folder
        self.title.setText(meta.title)
        self.presenter.setText(meta.presenter or "")
        self.organizer.setText(meta.organizer or "")
        self.category.setText(meta.category or "")
        self.tags.setText(", ".join(meta.tags))
        self.cloud_ok.setChecked(meta.cloud_ok)
        self._info.setText(
            f"Data: {meta.created_at[:19] or '—'}  ·  Długość: {_fmt_duration(meta.duration)}  ·  "
            f"Źródło: {meta.source_type}  ·  Transkrypcja: {meta.transcript_status}  ·  "
            f"Streszczenie: {meta.summary_status}  ·  Slajdy: {len(meta.slides)}"
        )
        self.set_editing_enabled(True)
        # „Streść" tylko po transkrypcji (handler i tak odmówi bez transkryptu — to UX).
        self.summarize_btn.setEnabled(meta.transcript_status == "done")
        self._show_thumbnail(folder, meta)
        self._show_summary(folder, meta)
        self._show_slides(folder, meta)

    def clear(self) -> None:
        """Czyści pola i podgląd, wyłącza edycję (brak zaznaczonego materiału)."""
        self._folder = None
        for edit in (self.title, self.presenter, self.organizer, self.category, self.tags):
            edit.clear()
        self.cloud_ok.setChecked(False)
        self._info.clear()
        self._thumb.setText("(brak podglądu)")
        self._summary_view.clear()
        self._slides_gallery.clear()
        self.set_editing_enabled(False)

    def set_editing_enabled(self, enabled: bool) -> None:
        """Włącza/wyłącza pola i przyciski (edycja tylko przy zaznaczonym materiale)."""
        for widget in (
            self.title,
            self.presenter,
            self.organizer,
            self.category,
            self.tags,
            self.cloud_ok,
            self.save_btn,
            self.transcribe_btn,
            self.summarize_btn,
            self.attach_slides_btn,
            self.open_btn,
            self.delete_btn,
        ):
            widget.setEnabled(enabled)

    def apply_edits(self, meta: MaterialMetadata) -> MaterialMetadata:
        """Zwraca metadane z naniesionymi wartościami z pól (czysta transformacja, bez IO)."""
        return replace(
            meta,
            title=self.title.text().strip() or meta.title,
            presenter=self.presenter.text().strip() or None,
            organizer=self.organizer.text().strip() or None,
            category=self.category.text().strip() or None,
            tags=[t.strip() for t in self.tags.text().split(",") if t.strip()],
            cloud_ok=self.cloud_ok.isChecked(),
        )

    # ── Podgląd (miniatura / streszczenie) ────────────────────────────────────

    def _show_thumbnail(self, folder: Path, meta: MaterialMetadata) -> None:
        if meta.thumbnail_path:
            pixmap = QPixmap(str(folder / meta.thumbnail_path))
            if not pixmap.isNull():
                self._thumb.setPixmap(
                    pixmap.scaledToWidth(320, Qt.TransformationMode.SmoothTransformation)
                )
                return
        self._thumb.setText("(brak podglądu)")

    def _show_summary(self, folder: Path, meta: MaterialMetadata) -> None:
        """Renderuje summary.md (źródło prawdy) jako Markdown; brak pliku → czyści podgląd.

        Odczyt przez ``utf-8-sig`` — summary.md zapisywany jest z BOM (dla czytników
        zewnętrznych), więc tu strippujemy go, żeby nie renderował się jako niewidoczny
        znak na początku podglądu.
        """
        if meta.summary_path:
            path = folder / meta.summary_path
            try:
                self._summary_view.setMarkdown(path.read_text(encoding="utf-8-sig"))
                return
            except OSError:
                pass
        self._summary_view.clear()

    def _show_slides(self, folder: Path, meta: MaterialMetadata) -> None:
        """Renderuje galerię miniatur z ``slides/``; timestamp (m:ss) jako podpis, gdy jest."""
        self._slides_gallery.clear()
        slides_dir = folder / SLIDES_DIRNAME
        for slide in meta.slides:
            path = slides_dir / slide.filename
            label = _fmt_timestamp(slide.timestamp_s) if slide.timestamp_s is not None else ""
            item = QListWidgetItem(label)
            item.setToolTip(f"{slide.index}. {slide.filename}")
            item.setData(_SLIDE_ROLE, str(path))
            pixmap = QPixmap(str(path))
            if not pixmap.isNull():
                item.setIcon(QIcon(pixmap))
            self._slides_gallery.addItem(item)

    def _open_slide(self, item: QListWidgetItem) -> None:
        """Powiększa klik­nięty slajd w osobnym oknie (pełny obraz, skalowany do ekranu)."""
        path = str(item.data(_SLIDE_ROLE) or "")
        pixmap = QPixmap(path)
        if pixmap.isNull():
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(item.toolTip() or "Slajd")
        layout = QVBoxLayout(dialog)
        label = QLabel()
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setPixmap(pixmap.scaledToWidth(900, Qt.TransformationMode.SmoothTransformation))
        layout.addWidget(label)
        dialog.exec()
