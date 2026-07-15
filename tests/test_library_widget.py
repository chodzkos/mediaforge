"""Widok biblioteki (pytest-qt, offscreen): lista, filtr, edycja metadanych → trwałość."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest
from chodzkos_gui_kit.config import Config
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from mediaforge.core import config as cfg_mod
from mediaforge.core.ai.gateway import Transport
from mediaforge.core.ai.routing import ModelRoute, RouteKind
from mediaforge.core.jobs import JobStore
from mediaforge.core.jobs.handlers import JOB_TRANSCRIBE
from mediaforge.core.library.db import Database
from mediaforge.core.library.material import MaterialMetadata, read_metadata, write_metadata
from mediaforge.core.library.recordings import RecordingStore
from mediaforge.gui import library_widget as library_widget_mod
from mediaforge.gui.import_dialog import ImportDialog
from mediaforge.gui.library_widget import LibraryWidget


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    db = tmp_path / "library.sqlite3"
    monkeypatch.setattr(cfg_mod, "library_db_path", lambda: db)
    monkeypatch.setattr(cfg_mod, "default_recordings_dir", lambda: tmp_path / "lib")
    Database(db).migrate()
    return db


def _seed(db: Path, tmp_path: Path, title: str, *, category: str, tags: list[str]) -> Path:
    folder = tmp_path / "lib" / title
    meta = MaterialMetadata(
        title=title,
        created_at="2026-06-30T10:00:00+00:00",
        category=category,
        tags=tags,
        duration=61.0,
    )
    write_metadata(folder, meta)
    RecordingStore(db).upsert_material(folder, meta)
    return folder


def test_library_lists_and_edits_persist(
    qtbot: QtBot, qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _isolate(monkeypatch, tmp_path)
    folder = _seed(db, tmp_path, "Wykład", category="Sieci", tags=["tcp"])

    widget = LibraryWidget()
    qtbot.addWidget(widget)

    assert widget._list.count() == 1
    widget._list.setCurrentRow(0)
    assert widget._details.title.text() == "Wykład"

    # Edycja metadanych → zapis do metadata.json (źródło prawdy) + SQLite.
    widget._details.title.setText("Wykład o BGP")
    widget._details.presenter.setText("dr Nowak")
    widget._details.tags.setText("tcp, bgp")
    widget._on_save()

    saved = read_metadata(folder)
    assert saved.title == "Wykład o BGP"
    assert saved.presenter == "dr Nowak"
    assert saved.tags == ["bgp", "tcp"]
    # Indeks SQLite zsynchronizowany.
    assert RecordingStore(db).list_materials()[0][2] == saved


def test_library_filters_by_category(
    qtbot: QtBot, qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _isolate(monkeypatch, tmp_path)
    _seed(db, tmp_path, "A", category="Sieci", tags=["tcp"])
    _seed(db, tmp_path, "B", category="AI", tags=["llm"])

    widget = LibraryWidget()
    qtbot.addWidget(widget)
    assert widget._list.count() == 2

    idx = widget._cat_filter.findText("AI")
    widget._cat_filter.setCurrentIndex(idx)
    assert widget._list.count() == 1


def test_notes_refusal_points_to_settings_not_config(
    qtbot: QtBot, qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Odmowa notatek bez modelu VLM wskazuje Ustawienia (regresja: koniec „w configu")."""
    db = _isolate(monkeypatch, tmp_path)
    _seed(db, tmp_path, "Wyklad", category="Sieci", tags=["tcp"])

    widget = LibraryWidget()
    qtbot.addWidget(widget)
    widget._list.setCurrentRow(0)

    widget._on_notes()  # brak vlm_model → odmowa (log błędu)
    log = widget._log.toPlainText()
    assert "Ustawieni" in log  # „w Ustawieniach (ikona zębatki)"
    assert "w configu" not in log  # antywzorzec usunięty
    assert JobStore(db).list_jobs() == []


def test_transcribe_button_enqueues_job(
    qtbot: QtBot, qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _isolate(monkeypatch, tmp_path)
    _seed(db, tmp_path, "Wyklad", category="Sieci", tags=["tcp"])

    widget = LibraryWidget()  # start_jobs() NIE wołane → brak wątku roboczego
    qtbot.addWidget(widget)
    widget._list.setCurrentRow(0)

    # Bez whisper_model → nie kolejkuje (log błędu).
    widget._on_transcribe()
    assert JobStore(db).list_jobs() == []

    # Z modelem → job transkrypcji w kolejce (recording_id materiału).
    cfg_mod.set_whisper_model(widget._config, "/m/model.bin")
    widget._on_transcribe()
    jobs = JobStore(db).list_jobs()
    assert len(jobs) == 1 and jobs[0].job_type == "transcribe"
    assert jobs[0].recording_id is not None


def test_transcribe_refusal_points_to_settings_not_config(
    qtbot: QtBot, qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Odmowa transkrypcji bez modelu wskazuje Ustawienia (regresja: koniec „w configu")."""
    db = _isolate(monkeypatch, tmp_path)
    _seed(db, tmp_path, "Wyklad", category="Sieci", tags=["tcp"])

    widget = LibraryWidget()
    qtbot.addWidget(widget)
    widget._list.setCurrentRow(0)

    widget._on_transcribe()  # brak whisper_model → odmowa (log błędu)
    log = widget._log.toPlainText()
    assert "Ustawieni" in log  # „w Ustawieniach (ikona zębatki → Transkrypcja)"
    assert "w configu" not in log  # antywzorzec usunięty
    assert JobStore(db).list_jobs() == []


def test_reload_ai_handlers_reregisters_transcribe(
    qtbot: QtBot, qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Transkrypcja czyta config przy rejestracji → reload (po zapisie Ustawień) ją obejmuje.

    Bez tego zmiana whisper_model/whispercpp_path działałaby dopiero po restarcie aplikacji.
    """
    _isolate(monkeypatch, tmp_path)
    widget = LibraryWidget()
    qtbot.addWidget(widget)

    registered: list[str] = []
    monkeypatch.setattr(
        widget._queue, "register", lambda job_type, handler: registered.append(job_type)
    )
    widget.reload_ai_handlers()
    assert JOB_TRANSCRIBE in registered  # nie tylko summarize/notes


def test_library_widget_refusals_avoid_w_configu() -> None:
    """Strażnik: żadna odmowa w library_widget nie odsyła „w configu" (user idzie do Ustawień)."""
    src = Path(library_widget_mod.__file__).read_text(encoding="utf-8")
    assert "w configu" not in src


# ── Szczelina config→klient: /no_think przeżywa CZYSTY config (None ≠ skasowany sufiks) ─────
#
# Buildery (build_vision_request/build_summary_request) mają własne testy z ręcznym Config.
# Te testy pilnują WIRINGU (_vision_client/_summary_client czyta config aplikacji) — gdyby
# None z configu trafił WPROST do *Config, nadpisałby default dataclassy i qwen3(-vl) zjadałby
# cały budżet na rozumowanie (pusta treść → „Model zużył cały limit tokenów"). Atrapy builderowe
# tej szczeliny NIE łapią, bo nie czytają configu aplikacji.


def _clean_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``cfg_mod.load`` → świeży Config bez żadnych kluczy (same domyślne = getter zwraca None)."""
    clean = Config(cfg_mod.APP_NAME, path=tmp_path / "clean_config.json")
    monkeypatch.setattr(cfg_mod, "load", lambda: clean)


def _config_with(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **keys: object) -> None:
    """``cfg_mod.load`` → Config z podanymi kluczami ustawionymi (reszta domyślna)."""
    cfg = Config(cfg_mod.APP_NAME, path=tmp_path / "preset_config.json")
    for key, value in keys.items():
        cfg[key] = value
    monkeypatch.setattr(cfg_mod, "load", lambda: cfg)


def test_vlm_suffix_independent_of_summary_suffix(
    qtbot: QtBot, qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Footgun #76 zamknięty: ``summary_prompt_suffix=""`` (streszczenia bez ``/no_think``) NIE
    kasuje ``/no_think`` VLM. Osobne klucze — VLM (qwen3-vl) dostaje własny default niezależnie."""
    _isolate(monkeypatch, tmp_path)
    _config_with(
        monkeypatch,
        tmp_path,
        summary_prompt_suffix="",  # streszczenia modelem nie-rozumującym → jawnie bez sufiksu
        # vlm_prompt_suffix celowo NIEustawiony → VLM od własnego defaultu /no_think
    )
    widget = LibraryWidget()
    qtbot.addWidget(widget)

    summary_cap: dict[str, object] = {}
    summary = widget._summary_client()
    summary.transport = _capturing_transport(summary_cap, "Streszczenie.")
    summary.summarize("Transkrypt.", ModelRoute(RouteKind.LOCAL, "ollama/qwen3:27b"))

    vlm_cap: dict[str, object] = {}
    vlm = widget._vision_client()
    vlm.transport = _capturing_transport(vlm_cap, "TYTUŁ:\nA")
    img = tmp_path / "slajd.png"
    img.write_bytes(b"\x89PNG\r\n")
    vlm.analyze_slide(img, ModelRoute(RouteKind.LOCAL, "ollama/qwen-vl-local"))

    summary_payload, vlm_payload = summary_cap["payload"], vlm_cap["payload"]
    assert isinstance(summary_payload, dict) and isinstance(vlm_payload, dict)
    assert "/no_think" not in summary_payload["messages"][0]["content"]  # streszczenia: wyłączony
    assert vlm_payload["messages"][0]["content"].endswith("/no_think")  # VLM: nietknięty


def _capturing_transport(captured: dict[str, object], content: str) -> Transport:
    """Atrapa transportu: zapamiętuje payload żądania, zwraca minimalną poprawną odpowiedź."""

    def transport(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> bytes:
        captured["payload"] = json.loads(body)
        return json.dumps({"choices": [{"message": {"content": content}}]}).encode("utf-8")

    return transport


def test_notes_vlm_payload_carries_no_think_on_clean_config(
    qtbot: QtBot, qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Przy CZYSTYM configu payload VLM (z ``_vision_client``) niesie ``/no_think`` + 2048."""
    _isolate(monkeypatch, tmp_path)
    _clean_config(monkeypatch, tmp_path)
    widget = LibraryWidget()
    qtbot.addWidget(widget)

    captured: dict[str, object] = {}
    client = widget._vision_client()
    client.transport = _capturing_transport(captured, "TYTUŁ:\nA")
    img = tmp_path / "slajd.png"
    img.write_bytes(b"\x89PNG\r\n")
    client.analyze_slide(img, ModelRoute(RouteKind.LOCAL, "ollama/qwen-vl-local"))

    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["messages"][0]["content"].endswith("/no_think")  # sufiks NIE skasowany
    assert payload["max_tokens"] == 2048  # domyślny budżet VLM (default dataclassy)


def test_vlm_prompt_suffix_override_reaches_payload(
    qtbot: QtBot, qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nowy klucz ``vlm_prompt_suffix`` (nie-None) faktycznie steruje payloadem VLM (wiring)."""
    _isolate(monkeypatch, tmp_path)
    _config_with(monkeypatch, tmp_path, vlm_prompt_suffix="/custom_vlm")
    widget = LibraryWidget()
    qtbot.addWidget(widget)

    captured: dict[str, object] = {}
    client = widget._vision_client()
    client.transport = _capturing_transport(captured, "TYTUŁ:\nA")
    img = tmp_path / "slajd.png"
    img.write_bytes(b"\x89PNG\r\n")
    client.analyze_slide(img, ModelRoute(RouteKind.LOCAL, "ollama/qwen-vl-local"))

    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["messages"][0]["content"].endswith("/custom_vlm")  # override z nowego klucza


def test_summary_payload_carries_no_think_on_clean_config(
    qtbot: QtBot, qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ta sama szczelina dla streszczeń: ``_summary_client`` niesie ``/no_think`` + 4096."""
    _isolate(monkeypatch, tmp_path)
    _clean_config(monkeypatch, tmp_path)
    widget = LibraryWidget()
    qtbot.addWidget(widget)

    captured: dict[str, object] = {}
    client = widget._summary_client()
    client.transport = _capturing_transport(captured, "Streszczenie.")
    client.summarize("Transkrypt.", ModelRoute(RouteKind.LOCAL, "ollama/qwen3:27b"))

    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["messages"][0]["content"].endswith("/no_think")  # sufiks NIE skasowany
    assert payload["max_tokens"] == 4096  # domyślny budżet streszczenia (default dataclassy)


def test_attach_slides_button_copies_and_shows_gallery(
    qtbot: QtBot, qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """„Podłącz slajdy" kopiuje obrazy do slides/, licznik w Info i galeria z timestampem."""
    db = _isolate(monkeypatch, tmp_path)
    folder = _seed(db, tmp_path, "Wyklad", category="Sieci", tags=["tcp"])
    src = tmp_path / "src"
    src.mkdir()
    for name in ("s_0s.png", "s_154s.png", "notatki.txt"):
        (src / name).write_bytes(b"X")

    widget = LibraryWidget()
    qtbot.addWidget(widget)
    widget._list.setCurrentRow(0)
    # Podmieniamy natywny dialog wyboru plików (pytest-qt go nie kliknie).
    monkeypatch.setattr(
        widget,
        "_pick_slide_sources",
        lambda: [src / "s_0s.png", src / "s_154s.png", src / "notatki.txt"],
    )
    widget._on_attach_slides()

    copied = sorted(p.name for p in (folder / "slides").iterdir())
    assert copied == ["s_0s.png", "s_154s.png"]  # nie-obraz pominięty
    assert read_metadata(folder).slides[1].timestamp_s == 154
    # Panel: licznik slajdów w Info + galeria z podpisem timestampu (2:34).
    assert "Slajdy: 2" in widget._details._info.text()
    assert widget._details._slides_gallery.count() == 2
    labels = {widget._details._slides_gallery.item(i).text() for i in range(2)}
    assert "2:34" in labels  # 154 s → 2:34 (widoczny sygnał mapy czasowej)


def test_import_dialog_constructs(
    qtbot: QtBot, qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate(monkeypatch, tmp_path)
    dialog = ImportDialog()
    qtbot.addWidget(dialog)
    assert dialog.enqueued_count == 0
    # Bez plików import nie kolejkuje (loguje ostrzeżenie).
    dialog._on_import()
    assert dialog.enqueued_count == 0


# ── Usuwanie materiału z biblioteki ───────────────────────────────────────────


def test_delete_confirmed_removes_material(
    qtbot: QtBot, qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _isolate(monkeypatch, tmp_path)
    folder = _seed(db, tmp_path, "Do usunięcia", category="X", tags=[])
    widget = LibraryWidget()
    qtbot.addWidget(widget)
    widget._list.setCurrentRow(0)

    monkeypatch.setattr(widget, "_confirm_delete", lambda title: True)
    widget._on_delete()

    assert widget._list.count() == 0  # zniknął z listy
    assert not folder.exists()  # folder usunięty z dysku
    assert widget._current is None
    assert "Usunięto" in widget._log.toPlainText()


def test_delete_cancelled_keeps_material(
    qtbot: QtBot, qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _isolate(monkeypatch, tmp_path)
    folder = _seed(db, tmp_path, "Zostaje", category="X", tags=[])
    widget = LibraryWidget()
    qtbot.addWidget(widget)
    widget._list.setCurrentRow(0)

    monkeypatch.setattr(widget, "_confirm_delete", lambda title: False)  # Anuluj
    widget._on_delete()

    assert widget._list.count() == 1 and folder.exists()  # nic nie ruszone


def test_delete_with_active_job_shows_error(
    qtbot: QtBot, qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _isolate(monkeypatch, tmp_path)
    folder = _seed(db, tmp_path, "W transkrypcji", category="X", tags=[])
    rec_id = RecordingStore(db).list_materials()[0][0]
    JobStore(db).enqueue("transcribe", recording_id=rec_id)  # aktywny job (pending)

    widget = LibraryWidget()
    qtbot.addWidget(widget)
    widget._list.setCurrentRow(0)

    monkeypatch.setattr(widget, "_confirm_delete", lambda title: True)
    widget._on_delete()

    assert widget._list.count() == 1 and folder.exists()  # guard: nic nie usunięte
    assert "aktywne zadanie" in widget._log.toPlainText()
