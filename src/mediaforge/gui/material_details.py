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

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from mediaforge.core.library.material import MaterialMetadata


def _fmt_duration(seconds: float | None) -> str:
    if not seconds:
        return "—"
    total = int(seconds)
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


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
        self.open_btn = QPushButton("Otwórz folder")
        actions.addWidget(self.open_btn)
        actions.addStretch(1)
        self.delete_btn = QPushButton("Usuń")
        self.delete_btn.setToolTip("Trwale usuń materiał (nagranie + transkrypt + metadane)")
        actions.addWidget(self.delete_btn)
        col.addLayout(actions)

        # ── ROZCIĄGLIWA (jedyna): podgląd streszczenia (Markdown, własny scroll) ──
        col.addWidget(QLabel("Streszczenie:"))
        self._summary_view = QTextBrowser()
        self._summary_view.setOpenExternalLinks(True)
        self._summary_view.setPlaceholderText("(brak streszczenia — użyj „Streść”)")
        self._summary_view.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        col.addWidget(self._summary_view, stretch=1)

        self.set_editing_enabled(False)

    # ── API dla właściciela ───────────────────────────────────────────────────

    def load(self, folder: Path, meta: MaterialMetadata) -> None:
        """Wypełnia panel danymi materiału (pola, Info, miniatura, podgląd streszczenia)."""
        self.title.setText(meta.title)
        self.presenter.setText(meta.presenter or "")
        self.organizer.setText(meta.organizer or "")
        self.category.setText(meta.category or "")
        self.tags.setText(", ".join(meta.tags))
        self.cloud_ok.setChecked(meta.cloud_ok)
        self._info.setText(
            f"Data: {meta.created_at[:19] or '—'}  ·  Długość: {_fmt_duration(meta.duration)}  ·  "
            f"Źródło: {meta.source_type}  ·  Transkrypcja: {meta.transcript_status}  ·  "
            f"Streszczenie: {meta.summary_status}"
        )
        self.set_editing_enabled(True)
        # „Streść" tylko po transkrypcji (handler i tak odmówi bez transkryptu — to UX).
        self.summarize_btn.setEnabled(meta.transcript_status == "done")
        self._show_thumbnail(folder, meta)
        self._show_summary(folder, meta)

    def clear(self) -> None:
        """Czyści pola i podgląd, wyłącza edycję (brak zaznaczonego materiału)."""
        for edit in (self.title, self.presenter, self.organizer, self.category, self.tags):
            edit.clear()
        self.cloud_ok.setChecked(False)
        self._info.clear()
        self._thumb.setText("(brak podglądu)")
        self._summary_view.clear()
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
