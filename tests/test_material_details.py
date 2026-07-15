"""Panel szczegółów (pytest-qt): galeria slajdów jako kolejna sekcja NIE psuje layoutu.

Sanity-test wymagany przy feat/attach-slides: długie Info + streszczenie + galeria naraz
przy wąskim oknie nie nachodzą — QScrollArea daje scroll zamiast nakładania (regresja fixu
layoutu z fix/s4-polish).
"""

from __future__ import annotations

from pathlib import Path

from chodzkos_gui_kit.qt.widgets import make_scrollable
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from mediaforge.core.library.material import MaterialMetadata, write_metadata
from mediaforge.core.library.slides import SLIDES_DIRNAME, Slide
from mediaforge.gui.material_details import MaterialDetailsPanel


def _material_with_everything(tmp_path: Path) -> tuple[Path, MaterialMetadata]:
    folder = tmp_path / "mat"
    slides_dir = folder / SLIDES_DIRNAME
    slides_dir.mkdir(parents=True)
    slides = []
    for i in range(6):
        (slides_dir / f"s_{i * 30}s.png").write_bytes(b"X")
        slides.append(Slide(f"s_{i * 30}s.png", i + 1, i * 30))
    meta = MaterialMetadata(
        title="Materiał z długim Info " * 3,
        created_at="2026-07-05T10:00:00+00:00",
        presenter="dr " + "X" * 40,
        duration=5400.0,
        summary_status="done",
        summary_path="summary.md",
        slides=tuple(slides),
    )
    write_metadata(folder, meta)
    (folder / "summary.md").write_text("# Streszczenie\n\n" + "akapit " * 400, encoding="utf-8-sig")
    return folder, meta


def test_panel_scrolls_instead_of_overlapping(
    qtbot: QtBot, qapp: QApplication, tmp_path: Path
) -> None:
    folder, meta = _material_with_everything(tmp_path)
    panel = MaterialDetailsPanel()
    panel.load(folder, meta)
    # Panel to czysta treść; scroll daje owijający kitowy make_scrollable (jak w GUI).
    scroll = make_scrollable(panel)
    qtbot.addWidget(scroll)
    scroll.setFixedSize(240, 240)  # WĄSKIE i NISKIE — najgorszy przypadek
    scroll.show()
    qapp.processEvents()
    qapp.processEvents()

    content = scroll.widget()
    assert content is panel
    # Treść wyższa niż viewport → aktywny pionowy scroll (nie clip/overlap).
    assert content.minimumSizeHint().height() > scroll.viewport().height()
    assert scroll.verticalScrollBar().maximum() > 0

    # Sekcje w pionie NIE nachodzą: bottom(i) <= top(i+1) w kolejności ułożenia.
    rows = [panel._thumb, panel.title, panel._info, panel.save_btn, panel._summary_view]
    tops = [w.mapTo(content, w.rect().topLeft()).y() for w in rows]
    bots = [w.mapTo(content, w.rect().bottomLeft()).y() for w in rows]
    assert all(bots[i] <= tops[i + 1] for i in range(len(rows) - 1))
    # Galeria slajdów jest pod streszczeniem i ma własny scroll wewnętrzny (nie odpycha reszty).
    gallery_top = panel._slides_gallery.mapTo(content, panel._slides_gallery.rect().topLeft()).y()
    assert gallery_top >= bots[-1]
    assert panel._slides_gallery.count() == 6


def test_notes_button_enabled_only_with_slides_and_transcript(qtbot: QtBot, tmp_path: Path) -> None:
    """„Notatka" aktywna tylko, gdy są slajdy ORAZ transkrypt done; notes.md w podglądzie."""
    folder = tmp_path / "mat"
    slides_dir = folder / SLIDES_DIRNAME
    slides_dir.mkdir(parents=True)
    (slides_dir / "s1.png").write_bytes(b"X")
    slides = (Slide("s1.png", 1, 30),)
    base = MaterialMetadata(title="M", created_at="2026-07-05T10:00:00+00:00", slides=slides)
    write_metadata(folder, base)

    panel = MaterialDetailsPanel()
    qtbot.addWidget(panel)

    # Slajdy są, ale brak transkryptu → „Notatka" nieaktywna.
    panel.load(folder, base)
    assert panel.notes_btn.isEnabled() is False

    # Slajdy + transkrypt done → aktywna.
    with_tr = MaterialMetadata(
        title="M",
        created_at="2026-07-05T10:00:00+00:00",
        transcript_status="done",
        transcript_json="M.json",
        slides=slides,
    )
    panel.load(folder, with_tr)
    assert panel.notes_btn.isEnabled() is True

    # Transkrypt done, ale BEZ slajdów → nieaktywna.
    no_slides = MaterialMetadata(
        title="M",
        created_at="2026-07-05T10:00:00+00:00",
        transcript_status="done",
        transcript_json="M.json",
    )
    panel.load(folder, no_slides)
    assert panel.notes_btn.isEnabled() is False

    # notes.md renderuje się w podglądzie (utf-8-sig, jak streszczenie).
    (folder / "notes.md").write_text("# Notatka\n\n## Slajd 1", encoding="utf-8-sig")
    done = MaterialMetadata(
        title="M",
        created_at="2026-07-05T10:00:00+00:00",
        transcript_status="done",
        transcript_json="M.json",
        notes_status="done",
        notes_path="notes.md",
        slides=slides,
    )
    panel.load(folder, done)
    assert "Notatka" in panel._notes_view.toMarkdown()
