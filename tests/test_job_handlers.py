"""Handlery kolejki: transkrypcja, import i streszczenie jako joby (executor + atrapy)."""

from __future__ import annotations

import json
import logging
import urllib.error
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

from mediaforge.core.ai.summarize import SummaryClient, SummaryConfig
from mediaforge.core.ai.transcribe import (
    TranscribeOptions,
    Transcript,
    TranscriptionError,
    TranscriptionResult,
)
from mediaforge.core.engines.import_engine import ImporterEngine
from mediaforge.core.jobs import JobQueue, JobStatus, JobStore
from mediaforge.core.jobs.handlers import (
    DEFAULT_LANES,
    DEFAULT_ROUTES,
    JOB_IMPORT,
    JOB_SUMMARIZE,
    JOB_TRANSCRIBE,
    enqueue_summarize,
    make_import_handler,
    make_summarize_handler,
    make_transcribe_handler,
)
from mediaforge.core.library.db import Database
from mediaforge.core.library.material import MaterialMetadata, read_metadata, write_metadata
from mediaforge.core.library.recordings import RecordingStore

_LOCAL = "ollama/qwen3:27b"
_CLOUD = "anthropic/claude-3"


def _db(tmp_path: Path) -> Path:
    db = tmp_path / "library.sqlite3"
    Database(db).migrate()
    return db


class _FakeBackend:
    """Backend transkrypcji-atrapa: pisze .json/.srt do folderu, zwraca runtime cuda."""

    name = "fake"

    def transcribe(
        self,
        source: Path,
        out_dir: Path,
        opts: TranscribeOptions,
        *,
        on_progress: Callable[[int], None] | None = None,
    ) -> TranscriptionResult:
        if on_progress is not None:
            on_progress(50)  # symulacja postępu w połowie
        json_path = out_dir / f"{source.stem}.json"
        srt_path = out_dir / f"{source.stem}.srt"
        json_path.write_text("{}", encoding="utf-8")
        srt_path.write_text("1\n", encoding="utf-8")
        return TranscriptionResult(
            transcript=Transcript(language="pl", model="fake"),
            runtime="cuda",
            json_path=json_path,
            srt_path=srt_path,
        )


def _seed_material(store: RecordingStore, lib: Path, title: str) -> tuple[int, Path]:
    folder = lib / title
    meta = MaterialMetadata(title=title, created_at="t", audio_path=f"{title}.wav")
    write_metadata(folder, meta)
    (folder / f"{title}.wav").write_bytes(b"AUDIO")
    rec_id = store.upsert_material(folder, read_metadata(folder))
    return rec_id, folder


def test_transcribe_job_writes_status_and_files(tmp_path: Path) -> None:
    db = _db(tmp_path)
    store = RecordingStore(db)
    rec_id, folder = _seed_material(store, tmp_path / "lib", "Wyklad")

    queue = JobQueue(JobStore(db), lanes={JOB_TRANSCRIBE: 1})
    queue.register(JOB_TRANSCRIBE, make_transcribe_handler(store, _FakeBackend()))
    job_id = JobStore(db).enqueue(JOB_TRANSCRIBE, recording_id=rec_id)

    assert queue.process_pending() == 1
    assert JobStore(db).get(job_id).status is JobStatus.DONE  # type: ignore[union-attr]

    # metadata.json (źródło prawdy) zaktualizowane.
    meta = read_metadata(folder)
    assert meta.transcript_status == "done"
    assert meta.transcript_json == "Wyklad.json" and meta.transcript_srt == "Wyklad.srt"
    # SQLite zsynchronizowane.
    material = store.get_material(rec_id)
    assert material is not None and material[1].transcript_status == "done"
    # rescan zachowuje status (nie zeruje).
    store.rescan(tmp_path / "lib")
    again = store.get_material(rec_id)
    assert again is not None and again[1].transcript_status == "done"


def test_transcribe_handler_forwards_progress(tmp_path: Path) -> None:
    """Handler przekazuje on_progress(pct) → callback postępu kolejki jako frakcję (pct/100)."""
    from mediaforge.core.jobs.store import Job

    db = _db(tmp_path)
    store = RecordingStore(db)
    rec_id, _ = _seed_material(store, tmp_path / "lib", "M")
    handler = make_transcribe_handler(store, _FakeBackend())

    seen: list[float] = []
    job = Job(
        id=1,
        recording_id=rec_id,
        job_type=JOB_TRANSCRIBE,
        status=JobStatus.RUNNING,
        progress=0.0,
        error_message=None,
        retry_count=0,
        max_retries=0,
        payload={},
        created_at="t",
        updated_at="t",
    )
    handler(job, seen.append)
    assert 0.5 in seen  # on_progress(50) → progress(0.5)


def test_transcribe_does_not_reset_other_material(tmp_path: Path) -> None:
    db = _db(tmp_path)
    store = RecordingStore(db)
    a_id, _ = _seed_material(store, tmp_path / "lib", "A")
    b_id, _ = _seed_material(store, tmp_path / "lib", "B")

    queue = JobQueue(JobStore(db), lanes={JOB_TRANSCRIBE: 1})
    queue.register(JOB_TRANSCRIBE, make_transcribe_handler(store, _FakeBackend()))
    JobStore(db).enqueue(JOB_TRANSCRIBE, recording_id=a_id)
    queue.process_pending()

    other = store.get_material(b_id)
    assert other is not None and other[1].transcript_status == "none"  # nietknięty


class _FailingBackend:
    """Backend-atrapa: symuluje nieudaną transkrypcję (ffmpeg/whisper-cli) rzucając wyjątek."""

    name = "failing"

    def transcribe(
        self,
        source: Path,
        out_dir: Path,
        opts: TranscribeOptions,
        *,
        on_progress: Callable[[int], None] | None = None,
    ) -> TranscriptionResult:
        raise TranscriptionError(f"whisper-cli nie wytworzył transkryptu ({source}): boom")


def test_transcribe_job_failed_when_backend_raises(tmp_path: Path) -> None:
    """Backend rzuca TranscriptionError → job FAILED z komunikatem; status materiału bez zmian."""
    db = _db(tmp_path)
    store = RecordingStore(db)
    rec_id, folder = _seed_material(store, tmp_path / "lib", "Wyklad")

    queue = JobQueue(JobStore(db), lanes={JOB_TRANSCRIBE: 1})
    queue.register(JOB_TRANSCRIBE, make_transcribe_handler(store, _FailingBackend()))
    job_id = JobStore(db).enqueue(JOB_TRANSCRIBE, recording_id=rec_id, max_retries=0)

    queue.process_pending()
    failed = JobStore(db).get(job_id)
    assert failed is not None and failed.status is JobStatus.FAILED
    assert failed.error_message and "whisper-cli nie wytworzył transkryptu" in failed.error_message
    # transcript_status materiału pozostaje „none" (nie zapisano „done").
    material = store.get_material(rec_id)
    assert material is not None and material[1].transcript_status == "none"
    assert read_metadata(folder).transcript_status == "none"


def test_transcribe_job_fails_for_material_without_folder(tmp_path: Path) -> None:
    db = _db(tmp_path)
    store = RecordingStore(db)
    # Wiersz bez folderu (np. sprzed S2) → get_material zwraca None → job błędny, nie crash.
    rec_id = store.create("bez folderu")

    queue = JobQueue(JobStore(db), lanes={JOB_TRANSCRIBE: 1})
    queue.register(JOB_TRANSCRIBE, make_transcribe_handler(store, _FakeBackend()))
    job_id = JobStore(db).enqueue(JOB_TRANSCRIBE, recording_id=rec_id, max_retries=0)

    queue.process_pending()
    failed = JobStore(db).get(job_id)
    assert failed is not None and failed.status is JobStatus.FAILED
    assert failed.error_message and str(rec_id) in failed.error_message


def _fake_copy(src: Path, dest: Path) -> object:
    dest.write_bytes(b"FAKE")
    return dest


def test_import_job_creates_material(tmp_path: Path) -> None:
    db = _db(tmp_path)
    store = RecordingStore(db)
    src = tmp_path / "clip.mp3"
    src.write_bytes(b"SRC")
    engine = ImporterEngine(
        store=store, runner=lambda c: 0, probe_runner=lambda c: "12\n", copy_fn=_fake_copy
    )

    queue = JobQueue(JobStore(db), lanes={JOB_IMPORT: 2})
    queue.register(JOB_IMPORT, make_import_handler(engine))
    JobStore(db).enqueue(
        JOB_IMPORT,
        payload={
            "src": str(src),
            "library_root": str(tmp_path / "lib"),
            "category": "Podcast",
            "tags": ["audio"],
        },
    )

    assert queue.process_pending() == 1
    materials = store.list_materials()
    assert len(materials) == 1
    assert materials[0][2].title == "clip" and materials[0][2].category == "Podcast"


# ── Streszczenie (atrapa transportu gatewaya, bez sieci) ──────────────────────


def _seed_transcribed(
    store: RecordingStore, lib: Path, title: str, *, cloud_ok: bool = False
) -> tuple[int, Path]:
    """Materiał z ukończoną transkrypcją (whisper.cpp json w folderze) — wejście streszczenia."""
    folder = lib / title
    meta = MaterialMetadata(
        title=title,
        created_at="t",
        audio_path=f"{title}.wav",
        transcript_status="done",
        transcript_json=f"{title}.json",
        cloud_ok=cloud_ok,
    )
    write_metadata(folder, meta)
    (folder / f"{title}.json").write_text(
        json.dumps({"transcription": [{"text": "Zdanie pierwsze."}, {"text": "Zdanie drugie."}]}),
        encoding="utf-8",
    )
    return store.upsert_material(folder, read_metadata(folder)), folder


def _capturing_client(*, fail: bool = False) -> tuple[SummaryClient, dict[str, object]]:
    """Klient gatewaya z transportem-atrapą: zapisuje payload albo udaje padnięty gateway."""
    captured: dict[str, object] = {}

    def transport(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> bytes:
        if fail:
            raise urllib.error.URLError("connection refused")
        captured["payload"] = json.loads(body)
        return json.dumps(
            {"choices": [{"message": {"content": "# Streszczenie\nTreść."}}]}
        ).encode()

    client = SummaryClient(SummaryConfig(base_url="http://gw:4000"), transport=transport)
    return client, captured


def test_summarize_job_local_happy_path(tmp_path: Path) -> None:
    """Lokalne streszczenie: summary.md powstaje, statusy set, model lokalny, przeżywa rescan."""
    db = _db(tmp_path)
    store = RecordingStore(db)
    rec_id, folder = _seed_transcribed(store, tmp_path / "lib", "Wyklad", cloud_ok=False)
    client, captured = _capturing_client()

    queue = JobQueue(JobStore(db), lanes=DEFAULT_LANES, routes=DEFAULT_ROUTES)
    queue.register(
        JOB_SUMMARIZE,
        make_summarize_handler(store, client, local_model=_LOCAL, cloud_model=_CLOUD),
    )
    enqueue_summarize(store, JobStore(db), rec_id, local_model=_LOCAL, cloud_model=_CLOUD)

    assert queue.process_pending() == 1
    # Model lokalny mimo skonfigurowanej chmury (cloud_ok=False → fail-safe).
    assert captured["payload"]["model"] == _LOCAL  # type: ignore[index]
    # summary.md w folderze materiału (źródło prawdy) + statusy.
    # BOM (utf-8-sig) TYLKO dla summary.md — czytniki windowsowe bez BOM zgadują cp1250.
    assert (folder / "summary.md").read_bytes().startswith(b"\xef\xbb\xbf")
    assert (folder / "summary.md").read_text(encoding="utf-8-sig").startswith("# Streszczenie")
    # metadata.json BEZ BOM — json.loads dalej przechodzi (BOM łamałby parser, RFC 8259).
    assert json.loads((folder / "metadata.json").read_text(encoding="utf-8"))["summary_status"]
    meta = read_metadata(folder)
    assert meta.summary_status == "done" and meta.summary_path == "summary.md"
    material = store.get_material(rec_id)
    assert material is not None and material[1].summary_status == "done"
    # Round-trip: summary_status przeżywa rescan (nie zerowany).
    store.rescan(tmp_path / "lib")
    again = store.get_material(rec_id)
    assert again is not None and again[1].summary_status == "done"


def test_summarize_cloud_ok_routes_cloud_on_io_lane(tmp_path: Path) -> None:
    """cloud_ok=True + model chmurowy → trasa chmurowa (model cloud), linia IO (nie blokuje GPU)."""
    db = _db(tmp_path)
    store = RecordingStore(db)
    rec_id, _folder = _seed_transcribed(store, tmp_path / "lib", "M", cloud_ok=True)
    client, captured = _capturing_client()

    job_id = enqueue_summarize(store, JobStore(db), rec_id, local_model=_LOCAL, cloud_model=_CLOUD)
    job = JobStore(db).get(job_id)
    assert job is not None and job.payload["lane"] == "io"  # chmura → linia IO

    queue = JobQueue(JobStore(db), lanes=DEFAULT_LANES, routes=DEFAULT_ROUTES)
    queue.register(
        JOB_SUMMARIZE,
        make_summarize_handler(store, client, local_model=_LOCAL, cloud_model=_CLOUD),
    )
    queue.process_pending()
    assert captured["payload"]["model"] == _CLOUD  # type: ignore[index]


def test_summarize_cloud_ok_false_uses_gpu_lane_and_local_model(tmp_path: Path) -> None:
    """cloud_ok=False → linia GPU i model lokalny, nawet gdy model chmurowy jest skonfigurowany."""
    db = _db(tmp_path)
    store = RecordingStore(db)
    rec_id, _folder = _seed_transcribed(store, tmp_path / "lib", "M", cloud_ok=False)

    job_id = enqueue_summarize(store, JobStore(db), rec_id, local_model=_LOCAL, cloud_model=_CLOUD)
    job = JobStore(db).get(job_id)
    assert job is not None and job.payload["lane"] == "gpu"  # lokalnie → linia GPU


def test_summarize_gateway_down_job_error_with_base_url(tmp_path: Path) -> None:
    """Padnięty gateway → job błędny, komunikat zawiera base_url (to gateway, nie apka)."""
    db = _db(tmp_path)
    store = RecordingStore(db)
    rec_id, _folder = _seed_transcribed(store, tmp_path / "lib", "M", cloud_ok=False)
    client, _ = _capturing_client(fail=True)

    queue = JobQueue(JobStore(db), lanes=DEFAULT_LANES, routes=DEFAULT_ROUTES)
    queue.register(
        JOB_SUMMARIZE,
        make_summarize_handler(store, client, local_model=_LOCAL, cloud_model=_CLOUD),
    )
    job_id = enqueue_summarize(store, JobStore(db), rec_id, local_model=_LOCAL, cloud_model=_CLOUD)
    # max_retries domyślnie 3 — dobijamy do failed powtórzeniami.
    for _ in range(4):
        queue.process_pending()

    failed = JobStore(db).get(job_id)
    assert failed is not None and failed.status is JobStatus.FAILED
    assert failed.error_message and "http://gw:4000" in failed.error_message


def test_summarize_enqueue_refused_without_transcript(tmp_path: Path) -> None:
    """Brak transkryptu → odmowa enqueue (ValueError „najpierw transkrypcja"), nic w kolejce."""
    db = _db(tmp_path)
    store = RecordingStore(db)
    rec_id, _folder = _seed_material(store, tmp_path / "lib", "BezTranskryptu")  # transcript none

    with pytest.raises(ValueError, match=r"[Tt]ranskryp"):
        enqueue_summarize(store, JobStore(db), rec_id, local_model=_LOCAL, cloud_model=_CLOUD)
    assert JobStore(db).list_jobs() == []


# ── Streszczenie map-reduce (długi materiał, atrapa transportu liczy wywołania) ───


def _seed_multi(
    store: RecordingStore, lib: Path, title: str, texts: list[str], *, cloud_ok: bool = False
) -> tuple[int, Path]:
    """Materiał z transkryptem o wielu segmentach (z offsetami czasu) — wejście map-reduce."""
    folder = lib / title
    meta = MaterialMetadata(
        title=title,
        created_at="t",
        audio_path=f"{title}.wav",
        transcript_status="done",
        transcript_json=f"{title}.json",
        cloud_ok=cloud_ok,
    )
    write_metadata(folder, meta)
    transcription = [
        {"text": t, "offsets": {"from": i * 60_000, "to": (i * 60 + 59) * 1000}}
        for i, t in enumerate(texts)
    ]
    (folder / f"{title}.json").write_text(
        json.dumps({"transcription": transcription}), encoding="utf-8"
    )
    return store.upsert_material(folder, read_metadata(folder)), folder


def _mr_client(
    *,
    max_tokens: int = 4096,
    fail_fragment: int | None = None,
    truncate_fragments: tuple[int, ...] = (),
) -> tuple[SummaryClient, list[dict[str, object]]]:
    """Klient map-reduce: liczy wywołania, opcjonalnie wywala/ucina wybrany fragment.

    Odpowiedź „S{n}" (n = numer wywołania) pozwala testom rozróżnić mapę (S1..SN) od reduce
    (ostatnie wywołanie). ``fail_fragment``/``truncate_fragments`` celują w konkretny fragment po
    treści promptu (a nie po numerze wywołania), więc są odporne na retry kolejki.
    """
    payloads: list[dict[str, object]] = []

    def transport(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> bytes:
        data = json.loads(body)
        payloads.append(data)
        user = data["messages"][1]["content"]
        if fail_fragment is not None and f"Fragment {fail_fragment}/" in user:
            raise urllib.error.URLError("boom")
        truncated = any(f"Fragment {f}/" in user for f in truncate_fragments)
        usage = {"completion_tokens": max_tokens if truncated else 10}
        return json.dumps(
            {"choices": [{"message": {"content": f"S{len(payloads)}"}}], "usage": usage}
        ).encode("utf-8")

    client = SummaryClient(
        SummaryConfig(base_url="http://gw:4000", max_tokens=max_tokens), transport
    )
    return client, payloads


def _register_summary(queue: JobQueue, store: RecordingStore, client: SummaryClient) -> None:
    queue.register(
        JOB_SUMMARIZE,
        make_summarize_handler(
            store, client, local_model=_LOCAL, cloud_model=_CLOUD, chunk_chars=120
        ),
    )


def test_summarize_single_chunk_one_call_no_parts(tmp_path: Path) -> None:
    """Krótki materiał (jeden kawałek) → DOKŁADNIE 1 wywołanie, brak summary_parts.md (zgodność)."""
    db = _db(tmp_path)
    store = RecordingStore(db)
    rec_id, folder = _seed_multi(store, tmp_path / "lib", "Short", ["krótki materiał"])
    client, payloads = _mr_client()

    queue = JobQueue(JobStore(db), lanes=DEFAULT_LANES, routes=DEFAULT_ROUTES)
    _register_summary(queue, store, client)
    enqueue_summarize(store, JobStore(db), rec_id, local_model=_LOCAL, cloud_model=_CLOUD)
    queue.process_pending()

    assert len(payloads) == 1  # jedna ścieżka, jeden request, zero reduce
    assert not (folder / "summary_parts.md").exists()
    assert (folder / "summary.md").read_text(encoding="utf-8-sig").strip() == "S1"
    assert read_metadata(folder).summary_parts_path is None


def test_summarize_map_reduce_writes_parts_and_summary(tmp_path: Path) -> None:
    """3 kawałki → 4 wywołania (3 map + 1 reduce); parts ma 3 sekcje; summary.md = reduce."""
    db = _db(tmp_path)
    store = RecordingStore(db)
    rec_id, folder = _seed_multi(store, tmp_path / "lib", "Long", ["a" * 100, "b" * 100, "c" * 100])
    client, payloads = _mr_client()

    queue = JobQueue(JobStore(db), lanes=DEFAULT_LANES, routes=DEFAULT_ROUTES)
    _register_summary(queue, store, client)
    enqueue_summarize(store, JobStore(db), rec_id, local_model=_LOCAL, cloud_model=_CLOUD)
    queue.process_pending()

    assert len(payloads) == 4  # 3 map + 1 reduce
    # Sufiks /no_think obecny w KAŻDYM wywołaniu (map i reduce).
    assert all(p["messages"][0]["content"].endswith("/no_think") for p in payloads)  # type: ignore[index]
    parts = (folder / "summary_parts.md").read_text(encoding="utf-8-sig")
    assert "## Część 1/3" in parts and "## Część 3/3" in parts
    assert "S1" in parts and "S3" in parts  # treści cząstkowe
    assert (folder / "summary.md").read_text(encoding="utf-8-sig").strip() == "S4"  # reduce
    meta = read_metadata(folder)
    assert meta.summary_status == "done" and meta.summary_parts_path == "summary_parts.md"
    # Round-trip: ścieżka parts przeżywa rescan (jak summary_path).
    store.rescan(tmp_path / "lib")
    again = store.get_material(rec_id)
    assert again is not None and again[1].summary_parts_path == "summary_parts.md"


def test_summarize_map_error_keeps_prior_parts(tmp_path: Path) -> None:
    """Błąd w kawałku 2 → job error, summary_parts.md zawiera część 1, komunikat wskazuje plik."""
    db = _db(tmp_path)
    store = RecordingStore(db)
    rec_id, folder = _seed_multi(store, tmp_path / "lib", "Boom", ["a" * 100, "b" * 100, "c" * 100])
    client, _ = _mr_client(fail_fragment=2)

    queue = JobQueue(JobStore(db), lanes=DEFAULT_LANES, routes=DEFAULT_ROUTES)
    _register_summary(queue, store, client)
    job_id = enqueue_summarize(store, JobStore(db), rec_id, local_model=_LOCAL, cloud_model=_CLOUD)
    for _ in range(4):  # wyczerpujemy retry aż do failed
        queue.process_pending()

    failed = JobStore(db).get(job_id)
    assert failed is not None and failed.status is JobStatus.FAILED
    assert failed.error_message and "summary_parts.md" in failed.error_message
    parts = (folder / "summary_parts.md").read_text(encoding="utf-8-sig")
    assert "## Część 1/3" in parts and "## Część 2/3" not in parts  # praca 1..1 ocalona


def test_summarize_progress_monotonic(tmp_path: Path) -> None:
    """Progress rośnie monotonicznie 0→1 z krokiem po każdym wywołaniu (map + reduce)."""
    db = _db(tmp_path)
    store = RecordingStore(db)
    rec_id, _folder = _seed_multi(
        store, tmp_path / "lib", "Prog", ["a" * 100, "b" * 100, "c" * 100]
    )
    client, _ = _mr_client()
    handler = make_summarize_handler(
        store, client, local_model=_LOCAL, cloud_model=_CLOUD, chunk_chars=120
    )
    job_id = enqueue_summarize(store, JobStore(db), rec_id, local_model=_LOCAL, cloud_model=_CLOUD)
    job = JobStore(db).get(job_id)
    assert job is not None

    seen: list[float] = []
    handler(job, seen.append)
    assert seen == sorted(seen)  # monotoniczny (nie maleje)
    assert seen[0] == 0.1 and seen[-1] == 1.0
    assert len(seen) >= 5  # 0.1 start + 3 map + reduce + 1.0


def test_summarize_map_truncation_warns_and_annotates(tmp_path: Path, caplog) -> None:  # type: ignore[no-untyped-def]
    """Ucięcie w kawałku (completion_tokens == max) → warning w logu + adnotacja w parts."""
    db = _db(tmp_path)
    store = RecordingStore(db)
    rec_id, folder = _seed_multi(
        store, tmp_path / "lib", "Trunc", ["a" * 100, "b" * 100, "c" * 100]
    )
    client, _ = _mr_client(truncate_fragments=(2,))
    handler = make_summarize_handler(
        store, client, local_model=_LOCAL, cloud_model=_CLOUD, chunk_chars=120
    )
    job_id = enqueue_summarize(store, JobStore(db), rec_id, local_model=_LOCAL, cloud_model=_CLOUD)
    job = JobStore(db).get(job_id)
    assert job is not None

    with caplog.at_level(logging.WARNING, logger="mediaforge"):
        handler(job, lambda _p: None)
    assert "części 2/3" in caplog.text and "ucięte" in caplog.text
    parts = (folder / "summary_parts.md").read_text(encoding="utf-8-sig")
    # Adnotacja tylko przy uciętej sekcji (2), nie przy pełnych (1, 3).
    assert parts.count("mogła zostać ucięta") == 1
    section_2 = parts.split("## Część 2/3")[1].split("## Część 3/3")[0]
    assert "mogła zostać ucięta" in section_2
