"""Handler notatki per slajd (S6): kolejność faz VLM→LLM, wznowienie, odmowy, składanie notes.md."""

from __future__ import annotations

import json
import urllib.error
from collections.abc import Mapping
from pathlib import Path

import pytest

from mediaforge.core.ai.routing import (
    ModelRoute,
    RouteKind,
    SensitivityViolation,
    assert_route_allowed,
)
from mediaforge.core.ai.summarize import SummaryClient, SummaryConfig
from mediaforge.core.ai.vision import VisionClient, VisionConfig
from mediaforge.core.jobs import JobStore
from mediaforge.core.jobs.handlers import (
    SLIDES_ANALYSIS_FILENAME,
    enqueue_notes,
    make_notes_handler,
)
from mediaforge.core.jobs.store import Job, JobStatus
from mediaforge.core.library.db import Database
from mediaforge.core.library.material import MaterialMetadata, read_metadata, write_metadata
from mediaforge.core.library.recordings import RecordingStore
from mediaforge.core.library.slides import Slide

_VLM_LOCAL = "ollama/qwen-vl-local"
_VLM_CLOUD = "gemini/gemini-vision"
_LLM_LOCAL = "ollama/qwen3:27b"
_LLM_CLOUD = "anthropic/claude-3"

# Slajdy: 1 i 2 z timestampem (30 s, 90 s), 3 bez timestampu → sekcja z adnotacją.
_SLIDES = (
    Slide(filename="s1.png", index=1, timestamp_s=30),
    Slide(filename="s2.png", index=2, timestamp_s=90),
    Slide(filename="s3.png", index=3, timestamp_s=None),
)


def _db(tmp_path: Path) -> Path:
    db = tmp_path / "library.sqlite3"
    Database(db).migrate()
    return db


def _seed(
    store: RecordingStore,
    lib: Path,
    title: str = "Wyklad",
    *,
    slides: tuple[Slide, ...] = _SLIDES,
    transcript: bool = True,
    cloud_ok: bool = False,
) -> tuple[int, Path]:
    """Materiał ze slajdami (obrazy na dysku) + transkrypt z segmentami w oknach slajdów."""
    folder = lib / title
    meta = MaterialMetadata(
        title=title,
        created_at="2026-07-15T10:00:00",
        presenter="Dr Kowalski",
        audio_path=f"{title}.wav",
        transcript_status="done" if transcript else "none",
        transcript_json=f"{title}.json" if transcript else None,
        cloud_ok=cloud_ok,
        slides=slides,
    )
    write_metadata(folder, meta)
    slides_dir = folder / "slides"
    slides_dir.mkdir(parents=True, exist_ok=True)
    for s in slides:
        (slides_dir / s.filename).write_bytes(b"\x89PNG_fake")
    if transcript:
        # Segment 40-50 (środek 45 → okno slajdu 1); 100-110 (środek 105 → okno slajdu 2).
        transcription = [
            {"text": "Mówię o pierwszym slajdzie.", "offsets": {"from": 40_000, "to": 50_000}},
            {"text": "Teraz drugi slajd.", "offsets": {"from": 100_000, "to": 110_000}},
        ]
        (folder / f"{title}.json").write_text(
            json.dumps({"transcription": transcription}), encoding="utf-8"
        )
    return store.upsert_material(folder, read_metadata(folder)), folder


def _clients(
    fail_on_vlm_call: int | None = None,
) -> tuple[VisionClient, SummaryClient, list[tuple[str, dict[str, object]]]]:
    """Vision + Summary z JEDNYM transportem-atrapą; ``calls`` zapisuje kolejność (vlm/llm).

    VLM rozpoznajemy po content-liście (image_url), LLM po treści tekstowej. ``fail_on_vlm_call``
    (1-based) wywala N-te wywołanie VLM (test wznowienia — praca częściowa na dysku).
    """
    calls: list[tuple[str, dict[str, object]]] = []
    vlm_count = {"n": 0}

    def transport(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> bytes:
        data = json.loads(body)
        content = data["messages"][1]["content"]
        if isinstance(content, list):  # VLM (obraz)
            vlm_count["n"] += 1
            if fail_on_vlm_call is not None and vlm_count["n"] == fail_on_vlm_call:
                raise urllib.error.URLError("vlm boom")
            calls.append(("vlm", data))
            reply = "TYTUŁ:\nTytuł slajdu\nTEKST:\nTreść slajdu\nOPIS:\nOpis slajdu"
        else:  # LLM (tekst)
            calls.append(("llm", data))
            reply = (
                "### Komentarz prowadzącego\nProwadzący mówi o slajdzie.\n"
                "### Najważniejsze punkty\n- punkt"
            )
        return json.dumps({"choices": [{"message": {"content": reply}}]}).encode("utf-8")

    vision = VisionClient(VisionConfig(base_url="http://gw:4000"), transport=transport)
    summary = SummaryClient(SummaryConfig(base_url="http://gw:4000"), transport=transport)
    return vision, summary, calls


def _handler(store: RecordingStore, vision: VisionClient, summary: SummaryClient):  # type: ignore[no-untyped-def]
    return make_notes_handler(
        store,
        vision,
        summary,
        vlm_local=_VLM_LOCAL,
        vlm_cloud=None,
        llm_local=_LLM_LOCAL,
        llm_cloud=_LLM_CLOUD,
    )


def _job(rec_id: int) -> Job:
    return Job(
        id=1,
        recording_id=rec_id,
        job_type="notes",
        status=JobStatus.RUNNING,
        progress=0.0,
        error_message=None,
        retry_count=0,
        max_retries=0,
        payload={},
        created_at="t",
        updated_at="t",
    )


def test_notes_all_vlm_calls_before_first_llm(tmp_path: Path) -> None:
    """KRYTYCZNE: przepływ dwufazowy — WSZYSTKIE wywołania VLM przed pierwszym LLM (VRAM)."""
    store = RecordingStore(_db(tmp_path))
    rec_id, _folder = _seed(store, tmp_path / "lib")
    vision, summary, calls = _clients()

    _handler(store, vision, summary)(_job(rec_id), lambda _p: None)

    kinds = [k for k, _ in calls]
    assert kinds.count("vlm") == 3  # 3 slajdy analizowane przez VLM
    assert kinds.count("llm") == 2  # 2 slajdy z timestampem dostają komentarz
    first_llm = kinds.index("llm")
    assert set(kinds[:first_llm]) == {"vlm"}  # przed pierwszym LLM tylko VLM


def test_notes_slides_analysis_incremental_and_resume(tmp_path: Path) -> None:
    """slides_analysis.json przyrostowy; wznowienie po błędzie pomija już przeanalizowane slajdy."""
    store = RecordingStore(_db(tmp_path))
    rec_id, folder = _seed(store, tmp_path / "lib")

    # Bieg 1: VLM wywala się na 3. slajdzie → job rzuca, ale slajdy 1-2 są już na dysku.
    vision1, summary1, _ = _clients(fail_on_vlm_call=3)
    with pytest.raises(Exception):  # noqa: B017 (GatewayError z transportu)
        _handler(store, vision1, summary1)(_job(rec_id), lambda _p: None)
    saved = json.loads((folder / SLIDES_ANALYSIS_FILENAME).read_text(encoding="utf-8"))
    assert [item["index"] for item in saved] == [1, 2]  # praca częściowa 1..2 ocalona

    # Bieg 2: nowe klienty (działają) → VLM woła TYLKO slajd 3 (1 i 2 wczytane z pliku).
    vision2, summary2, calls2 = _clients()
    _handler(store, vision2, summary2)(_job(rec_id), lambda _p: None)
    assert [k for k, _ in calls2].count("vlm") == 1  # tylko brakujący slajd 3
    final = json.loads((folder / SLIDES_ANALYSIS_FILENAME).read_text(encoding="utf-8"))
    assert [item["index"] for item in final] == [1, 2, 3]
    assert (folder / "notes.md").exists()


def test_notes_md_relative_paths_and_untimed_section(tmp_path: Path) -> None:
    """notes.md: ścieżki obrazów WZGLĘDNE, format z CELU, slajd bez czasu → sekcja z adnotacją."""
    store = RecordingStore(_db(tmp_path))
    rec_id, folder = _seed(store, tmp_path / "lib")
    vision, summary, _ = _clients()

    _handler(store, vision, summary)(_job(rec_id), lambda _p: None)

    notes = (folder / "notes.md").read_text(encoding="utf-8-sig")
    # Nagłówek materiału (tytuł, data, prowadzący z metadanych).
    assert notes.startswith("# Wyklad")
    assert "Data: 2026-07-15" in notes and "Prowadzący: Dr Kowalski" in notes
    # Sekcja slajdu 1 wg formatu z CELU: nagłówek + czas + obraz WZGLĘDNY + komentarz.
    assert "## Slajd 1 — Tytuł slajdu" in notes
    assert "Czas: 00:00:30-00:01:30" in notes  # okno [30, 90)
    assert "![Slajd 1](slides/s1.png)" in notes  # ścieżka względna
    assert "### Komentarz prowadzącego" in notes
    # Ostatni slajd z timestampem „do końca".
    assert "Czas: 00:01:30 do końca" in notes
    # Slajd bez timestampu → adnotacja, bez komentarza prowadzącego.
    assert "brak mapy czasowej" in notes
    # Zero ścieżek absolutnych (notes.md ma działać po przeniesieniu folderu).
    assert str(folder) not in notes and "/home/" not in notes


def test_notes_persists_status_and_survives_rescan(tmp_path: Path) -> None:
    """notes_status/notes_path w metadata.json + SQLite; round-trip przez rescan (nie zerowany)."""
    lib = tmp_path / "lib"
    store = RecordingStore(_db(tmp_path))
    rec_id, folder = _seed(store, lib)
    vision, summary, _ = _clients()

    _handler(store, vision, summary)(_job(rec_id), lambda _p: None)

    meta = read_metadata(folder)
    assert meta.notes_status == "done" and meta.notes_path == "notes.md"
    # metadata.json BEZ BOM (json.loads przechodzi); notes.md Z BOM (utf-8-sig).
    assert (
        json.loads((folder / "metadata.json").read_text(encoding="utf-8"))["notes_status"] == "done"
    )
    assert (folder / "notes.md").read_bytes().startswith(b"\xef\xbb\xbf")
    material = store.get_material(rec_id)
    assert material is not None and material[1].notes_status == "done"
    store.rescan(lib)
    again = store.get_material(rec_id)
    assert again is not None and again[1].notes_status == "done"


def test_notes_progress_monotonic(tmp_path: Path) -> None:
    """Progress rośnie monotonicznie i domyka się na 1.0 (kroki: N_vlm + N_llm)."""
    store = RecordingStore(_db(tmp_path))
    rec_id, _folder = _seed(store, tmp_path / "lib")
    vision, summary, _ = _clients()

    seen: list[float] = []
    _handler(store, vision, summary)(_job(rec_id), seen.append)

    assert seen == sorted(seen)  # nie maleje
    assert seen[-1] == 1.0
    assert len(seen) == 6  # 3 VLM + 2 LLM (capped 0.99) + finalne 1.0


def test_notes_enqueue_refused_without_slides(tmp_path: Path) -> None:
    """Materiał bez slajdów → odmowa enqueue („najpierw podłącz slajdy"), nic w kolejce."""
    db = _db(tmp_path)
    store = RecordingStore(db)
    rec_id, _folder = _seed(store, tmp_path / "lib", "BezSlajdow", slides=())

    with pytest.raises(ValueError, match=r"[Ss]lajd"):
        enqueue_notes(
            store,
            JobStore(db),
            rec_id,
            vlm_local=_VLM_LOCAL,
            vlm_cloud=None,
            llm_local=_LLM_LOCAL,
            llm_cloud=_LLM_CLOUD,
        )
    assert JobStore(db).list_jobs() == []


def test_notes_enqueue_refused_without_transcript(tmp_path: Path) -> None:
    """Materiał ze slajdami, ale bez transkryptu → odmowa enqueue („najpierw transkrypcja")."""
    db = _db(tmp_path)
    store = RecordingStore(db)
    rec_id, _folder = _seed(store, tmp_path / "lib", "BezTranskryptu", transcript=False)

    with pytest.raises(ValueError, match=r"[Tt]ranskryp"):
        enqueue_notes(
            store,
            JobStore(db),
            rec_id,
            vlm_local=_VLM_LOCAL,
            vlm_cloud=None,
            llm_local=_LLM_LOCAL,
            llm_cloud=_LLM_CLOUD,
        )
    assert JobStore(db).list_jobs() == []


def test_notes_enqueue_local_uses_gpu_lane(tmp_path: Path) -> None:
    """Trasy lokalne (VLM+LLM) → linia GPU (jeden model w VRAM naraz, sequential VRAM)."""
    db = _db(tmp_path)
    store = RecordingStore(db)
    rec_id, _folder = _seed(store, tmp_path / "lib", cloud_ok=False)

    job_id = enqueue_notes(
        store,
        JobStore(db),
        rec_id,
        vlm_local=_VLM_LOCAL,
        vlm_cloud=_VLM_CLOUD,
        llm_local=_LLM_LOCAL,
        llm_cloud=_LLM_CLOUD,
    )
    job = JobStore(db).get(job_id)
    assert job is not None and job.payload["lane"] == "gpu"


def test_notes_vlm_route_respects_cloud_ok_stays_local(tmp_path: Path) -> None:
    """cloud_ok=False → VLM idzie LOKALNIE mimo skonfigurowanego modelu chmurowego (fail-safe)."""
    store = RecordingStore(_db(tmp_path))
    rec_id, _folder = _seed(store, tmp_path / "lib", cloud_ok=False)
    vision, summary, calls = _clients()

    handler = make_notes_handler(
        store,
        vision,
        summary,
        vlm_local=_VLM_LOCAL,
        vlm_cloud=_VLM_CLOUD,  # skonfigurowany, ale niedozwolony (cloud_ok=False)
        llm_local=_LLM_LOCAL,
        llm_cloud=_LLM_CLOUD,
    )
    handler(_job(rec_id), lambda _p: None)

    vlm_models = {d["model"] for k, d in calls if k == "vlm"}
    assert vlm_models == {_VLM_LOCAL}  # nigdy trasa chmurowa dla wrażliwego materiału


def test_assert_blocks_sensitive_vlm_route_to_cloud() -> None:
    """Ostatnia linia obrony: trasa VLM chmurowa na wrażliwym materiale → SensitivityViolation."""
    cloud_route = ModelRoute(RouteKind.CLOUD, _VLM_CLOUD)
    with pytest.raises(SensitivityViolation):
        assert_route_allowed(cloud_route, cloud_ok=False)
