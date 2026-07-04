"""Silnik pobierania yt-dlp: builder komendy (w tym granica prawna), postęp, orkiestracja."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mediaforge.core.engines.download_engine import (
    DownloaderEngine,
    DownloadRunner,
    LineCb,
    RunResult,
    build_download_command,
    build_update_command,
    domain_of,
    parse_download_progress,
    parse_info_json,
)
from mediaforge.core.jobs import JobQueue, JobStatus, JobStore
from mediaforge.core.jobs.handlers import (
    DEFAULT_LANES,
    DEFAULT_ROUTES,
    JOB_DOWNLOAD,
    make_download_handler,
)
from mediaforge.core.library.db import Database
from mediaforge.core.library.recordings import RecordingStore

_URL = "https://vid.example.com/watch?v=abc123"


# ── Builder komendy ───────────────────────────────────────────────────────────


def test_build_command_video_basic() -> None:
    cmd = build_download_command(_URL, Path("/lib/mat"))
    assert cmd[0] == "yt-dlp" and cmd[1] == _URL
    assert "--no-playlist" in cmd and "--write-info-json" in cmd
    assert "--write-thumbnail" in cmd and "--newline" in cmd
    # -o kieruje do folderu materiału z szablonem yt-dlp.
    assert cmd[cmd.index("-o") + 1].endswith("%(title)s.%(ext)s")
    # domyślnie wideo+audio.
    assert cmd[cmd.index("-f") + 1] == "bv*+ba/b"
    # bez opt-in: ZERO cookies.
    assert "--cookies-from-browser" not in cmd


def test_build_command_audio_only() -> None:
    cmd = build_download_command(_URL, Path("/lib/mat"), audio_only=True)
    assert "-x" in cmd and cmd[cmd.index("--audio-format") + 1] == "best"
    assert "-f" not in cmd  # audio: bez selektora wideo


def test_build_command_cookies_opt_in_only() -> None:
    """Cookies TYLKO przy opt-in; nieznana przeglądarka → ValueError (zamknięta lista)."""
    cmd = build_download_command(_URL, Path("/lib/mat"), cookies_browser="firefox")
    assert cmd[cmd.index("--cookies-from-browser") + 1] == "firefox"
    with pytest.raises(ValueError, match="przeglądarka"):
        build_download_command(_URL, Path("/lib/mat"), cookies_browser="netscape")


def test_build_command_never_emits_credentials() -> None:
    """GRANICA PRAWNA (kontrakt): builder NIGDY nie emituje --username/--password.

    Cookies-from-browser to jedyny tor zalogowany; haseł aplikacja nie wspiera i nie widzi.
    """
    for kwargs in (
        {},
        {"audio_only": True},
        {"cookies_browser": "chrome"},
        {"format_selector": "bv*+ba/b"},
    ):
        cmd = build_download_command(_URL, Path("/lib/mat"), **kwargs)
        joined = " ".join(cmd)
        assert "--username" not in cmd and "-u" not in cmd
        assert "--password" not in cmd and "--netrc" not in cmd
        assert "password" not in joined.lower()


def test_build_update_command() -> None:
    assert build_update_command("yt-dlp") == ["yt-dlp", "-U"]


# ── Parser postępu ────────────────────────────────────────────────────────────


def test_parse_download_progress() -> None:
    assert parse_download_progress("[download]  42.3% of 10.00MiB at 1MiB/s") == 42
    assert parse_download_progress("[download] 100% of 10.00MiB") == 100
    assert parse_download_progress("[download]   0.0% of ~5MiB") == 0
    # Nie-progres → None.
    assert parse_download_progress("[youtube] abc123: Downloading webpage") is None
    assert parse_download_progress("ERROR: Private video") is None


def test_parse_info_json_fields() -> None:
    info = parse_info_json(
        {
            "title": "Wykład",
            "channel": "Konf",
            "upload_date": "20260630",
            "duration": 3600,
            "ext": "mp4",
        }
    )
    assert info.title == "Wykład" and info.uploader == "Konf"
    assert info.upload_date == "20260630" and info.duration == 3600.0 and info.ext == "mp4"


def test_domain_of_strips_www() -> None:
    assert domain_of("https://www.Example.com/x") == "example.com"
    assert domain_of("https://cdn.example.com/ep.mp3") == "cdn.example.com"


# ── Silnik (atrapa runnera, bez sieci) ────────────────────────────────────────


def _db(tmp_path: Path) -> Path:
    db = tmp_path / "library.sqlite3"
    Database(db).migrate()
    return db


def _fake_runner(*, fail: bool = False) -> DownloadRunner:
    """Atrapa yt-dlp: streamuje postęp i pisze plik+info.json+miniaturę do folderu z -o."""

    def runner(command: list[str], on_line: LineCb | None = None) -> RunResult:
        if on_line is not None:
            for line in ("[download]   0.0% of 10MiB\n", "[download]  50.0% of 10MiB\n"):
                on_line(line)
        if fail:
            return RunResult(
                returncode=1, tail="[youtube] abc: Private video\nERROR: Private video, sign in"
            )
        out_dir = Path(command[command.index("-o") + 1]).parent
        base = "Wykład o sepsie"
        (out_dir / f"{base}.mp4").write_bytes(b"VIDEO")
        (out_dir / f"{base}.jpg").write_bytes(b"JPG")
        (out_dir / f"{base}.info.json").write_text(
            json.dumps({"title": base, "ext": "mp4", "duration": 3600, "uploader": "Konferencja"}),
            encoding="utf-8",
        )
        return RunResult(returncode=0, tail="")

    return runner


def test_engine_happy_path(tmp_path: Path) -> None:
    store = RecordingStore(_db(tmp_path))
    engine = DownloaderEngine(store=store, runner=_fake_runner())
    seen: list[float] = []

    engine.download(_URL, tmp_path / "lib", lambda frac, _m: seen.append(frac), title="hint")

    assert 0.5 in seen and 1.0 in seen  # postęp + zakończenie
    materials = store.list_materials()
    assert len(materials) == 1
    _id, folder, meta = materials[0]
    assert meta.title == "Wykład o sepsie"  # z .info.json, nie z podpowiedzi
    assert meta.source_type == "download" and meta.source_url == _URL
    assert meta.duration == 3600.0 and meta.organizer == "Konferencja"
    assert meta.video_path == "Wykład o sepsie.mp4"
    assert meta.thumbnail_path == "Wykład o sepsie.jpg"
    assert (folder / "metadata.json").exists()


def test_engine_cloud_ok_default_false(tmp_path: Path) -> None:
    """Fail-safe: bez jawnego cloud_ok nowy materiał jest lokalny (False)."""
    store = RecordingStore(_db(tmp_path))
    DownloaderEngine(store=store, runner=_fake_runner()).download(
        _URL, tmp_path / "lib", lambda _f, _m: None, title="hint"
    )
    assert store.list_materials()[0][2].cloud_ok is False


def test_engine_error_raises_with_reason(tmp_path: Path) -> None:
    engine = DownloaderEngine(runner=_fake_runner(fail=True))
    with pytest.raises(RuntimeError, match="Private video"):
        engine.download(_URL, tmp_path / "lib", lambda _f, _m: None, title="hint")


def test_download_handler_error_to_jobs_error(tmp_path: Path) -> None:
    """Błąd yt-dlp → job FAILED z POWODEM (stderr) w jobs.error."""
    db = _db(tmp_path)
    engine = DownloaderEngine(store=RecordingStore(db), runner=_fake_runner(fail=True))
    queue = JobQueue(JobStore(db), lanes=DEFAULT_LANES, routes=DEFAULT_ROUTES)
    queue.register(JOB_DOWNLOAD, make_download_handler(engine))
    job_id = JobStore(db).enqueue(
        JOB_DOWNLOAD,
        payload={"url": _URL, "library_root": str(tmp_path / "lib"), "title": "hint"},
        max_retries=0,
    )

    queue.process_pending()
    failed = JobStore(db).get(job_id)
    assert failed is not None and failed.status is JobStatus.FAILED
    assert failed.error_message and "Private video" in failed.error_message
