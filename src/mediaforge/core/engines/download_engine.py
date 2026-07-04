"""DownloaderEngine — pobieranie URL-i (yt-dlp) do biblioteki (implementacja AcquisitionEngine).

Trzeci tor akwizycji obok Recorder/Importer. yt-dlp uruchamiany jako **subprocess** (spójnie
z ffmpeg/whisper-cli: izolacja crashy, luźny pin, aktualizacja niezależna od aplikacji).
Budowa komendy to czysta, testowalna funkcja; uruchamianie (Popen ze strumieniem stdout) jest
wstrzykiwane, więc orkiestracja jest testowalna bez sieci i bez yt-dlp.

**Granica prawna (LEGAL_BOUNDARIES / CLAUDE.md).** Zalogowany dostęp WYŁĄCZNIE przez
``--cookies-from-browser`` (opt-in, per pobranie) — aplikacja nie widzi ani nie zapisuje
poświadczeń. Builder NIGDY nie emituje ``--username``/``--password`` (jest na to test-kontrakt).
ZERO obchodzenia DRM (yt-dlp i tak nie umie — nie dodajemy obejść).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from mediaforge.core.engines.base import (
    AcquireOptions,
    MediaArtifact,
    QualityOption,
    Source,
    SourceKind,
)
from mediaforge.core.engines.import_engine import (
    AUDIO_EXTS,
    build_probe_duration_command,
    parse_duration,
)
from mediaforge.core.engines.recorder import safe_filename
from mediaforge.core.library.material import MaterialMetadata, write_metadata
from mediaforge.core.library.recordings import RecordingStore

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# Przeglądarki dozwolone jako źródło sesji (opt-in). Zamknięta lista — nie przyjmujemy
# dowolnego stringa do komendy zalogowanego pobierania.
COOKIE_BROWSERS: tuple[str, ...] = ("chrome", "edge", "firefox", "brave", "vivaldi", "opera")


# ── Czysty builder komendy yt-dlp (testowalny, egzekwuje granicę prawną) ──────


def build_download_command(
    url: str,
    out_dir: Path,
    *,
    ytdlp: str = "yt-dlp",
    audio_only: bool = False,
    cookies_browser: str | None = None,
    format_selector: str | None = None,
) -> list[str]:
    """Buduje komendę yt-dlp: URL → plik + miniatura + info.json w folderze materiału.

    ``cookies_browser`` (opt-in) dokłada ``--cookies-from-browser`` — JEDYNY tor zalogowany.
    Ta funkcja **nigdy** nie emituje ``--username``/``--password`` (granica prawna: zero haseł
    w aplikacji). ``audio_only`` → ``-x`` (podcasty/audio; oryginalny kodek, bez rekompresji).
    """
    cmd = [
        ytdlp,
        url,
        "-o",
        str(out_dir / "%(title)s.%(ext)s"),
        "--no-playlist",
        "--write-thumbnail",
        "--convert-thumbnails",
        "jpg",
        "--write-info-json",
        "--newline",  # postęp jako osobne linie (nie \r) — parsowalne
        "--progress",
    ]
    if audio_only:
        cmd += ["-x", "--audio-format", "best"]  # zostaw oryginalny kodek gdy się da
    else:
        cmd += ["-f", format_selector or "bv*+ba/b"]
    if cookies_browser:
        if cookies_browser not in COOKIE_BROWSERS:
            raise ValueError(f"Nieobsługiwana przeglądarka dla cookies: {cookies_browser}")
        cmd += ["--cookies-from-browser", cookies_browser]
    return cmd


def build_update_command(ytdlp: str = "yt-dlp") -> list[str]:
    """Komenda samo-aktualizacji standalone yt-dlp (``-U``). Dla modułu pythonowego nie działa."""
    return [ytdlp, "-U"]


# Wynik decyzji o aktualizacji: (komenda do uruchomienia albo None, komunikat dla użytkownika).
UpdateRunner = Callable[[list[str]], "tuple[int, str]"]


def ytdlp_update_plan(*, available: bool, path: str | None) -> tuple[list[str] | None, str]:
    """Decyduje wariant aktualizacji yt-dlp z detekcji (standalone ``-U`` vs moduł pythonowy).

    Standalone (binarka w PATH, ``path`` ustawione) → ``yt-dlp -U``. Moduł pythonowy
    (``path`` None) NIE aktualizuje się przez ``-U`` — zwracamy instrukcję „przez uv".
    """
    if not available:
        return None, "yt-dlp niedostępny — zainstaluj (uv add yt-dlp) albo dodaj binarkę do PATH."
    if path:
        return build_update_command(str(path)), f"Aktualizuję yt-dlp ({path})…"
    return (
        None,
        "yt-dlp działa jako moduł pythonowy — zaktualizuj pakiet: uv sync --upgrade-package yt-dlp",
    )


def _default_update_runner(command: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=120,
            creationflags=_NO_WINDOW,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def run_ytdlp_update(
    *, available: bool, path: str | None, runner: UpdateRunner = _default_update_runner
) -> str:
    """Wykonuje aktualizację yt-dlp wg wariantu z detekcji; zwraca komunikat dla użytkownika."""
    command, message = ytdlp_update_plan(available=available, path=path)
    if command is None:
        return message
    code, output = runner(command)
    tail = output.strip().splitlines()[-3:]
    detail = "\n".join(tail)
    return f"{message}\n{detail}" if code == 0 else f"yt-dlp -U nie powiodło się:\n{detail}"


# ── Parser postępu (czysta funkcja; ten sam wzorzec co transcribe-progress) ───

_PROGRESS_RE = re.compile(r"\[download\]\s+(\d+(?:\.\d+)?)%")


def parse_download_progress(line: str) -> int | None:
    """Wyciąga procent (0-100) z linii postępu yt-dlp (``[download]  42.3% of …``) albo None."""
    match = _PROGRESS_RE.search(line)
    if not match:
        return None
    return max(0, min(100, int(float(match.group(1)))))


# ── Metadane z .info.json (pewniejsze niż parsowanie stdout) ──────────────────


@dataclass(slots=True)
class InfoMeta:
    """Metadane odczytane z ``*.info.json`` yt-dlp (--write-info-json)."""

    title: str
    uploader: str | None
    upload_date: str | None  # surowe YYYYMMDD z yt-dlp (parsowanie po stronie konsumenta)
    duration: float | None
    ext: str | None


def parse_info_json(data: dict[str, Any]) -> InfoMeta:
    """Parsuje słownik ``*.info.json`` → tytuł/kanał/datę/czas trwania/rozszerzenie."""
    raw_duration = data.get("duration")
    try:
        duration = float(raw_duration) if raw_duration is not None else None
    except (TypeError, ValueError):
        duration = None
    return InfoMeta(
        title=str(data.get("title") or "").strip(),
        uploader=(str(data.get("uploader") or data.get("channel") or "").strip() or None),
        upload_date=(str(data.get("upload_date") or "").strip() or None),
        duration=duration,
        ext=(str(data.get("ext") or "").strip() or None),
    )


# ── Wstrzykiwany wykonawca (Popen ze strumieniem stdout do parsera postępu) ───

LineCb = Callable[[str], None]


@dataclass(slots=True)
class RunResult:
    """Wynik uruchomienia yt-dlp: kod wyjścia + ogon logu (do komunikatu błędu)."""

    returncode: int
    tail: str  # ostatnie linie (stdout+stderr scalone) — źródło POWODU błędu


class DownloadRunner(Protocol):
    """Uruchamia yt-dlp; strumieniuje linie stdout do ``on_line`` (postęp) na bieżąco."""

    def __call__(self, command: list[str], on_line: LineCb | None = None, /) -> RunResult: ...


def _default_runner(command: list[str], on_line: LineCb | None = None) -> RunResult:
    """Popen ze scalonym stderr→stdout: woła ``on_line`` na bieżąco i trzyma ogon logu.

    stderr scalone do stdout (jeden strumień, bez ryzyka zakleszczenia) — postęp i błędy
    yt-dlp lecą tym samym kanałem; ``tail`` (ostatnie linie) niesie POWÓD błędu (ERROR: …).
    """
    try:
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            creationflags=_NO_WINDOW,
        )
    except (OSError, ValueError) as exc:
        return RunResult(returncode=1, tail=str(exc))
    tail: deque[str] = deque(maxlen=25)
    if proc.stdout is not None:
        for line in proc.stdout:
            stripped = line.rstrip("\n")
            if stripped:
                tail.append(stripped)
            if on_line is not None:
                on_line(line)
    proc.wait()
    return RunResult(returncode=proc.returncode, tail="\n".join(tail))


def _error_message(tail: str) -> str:
    """Wyciąga czytelny POWÓD z ogona logu yt-dlp — linie ERROR albo ostatnie linie."""
    lines = [line for line in tail.splitlines() if line.strip()]
    errors = [line for line in lines if line.lstrip().startswith("ERROR")]
    chosen = errors or lines[-3:]
    return " | ".join(chosen)[:500] or "yt-dlp zakończył się błędem (brak szczegółów)."


def _now() -> str:
    return datetime.now(UTC).isoformat()


def domain_of(url: str) -> str:
    """Hostname URL-a (bez ``www.``), małymi literami — klucz profilu źródła."""
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


@dataclass(slots=True)
class DownloaderEngine:
    """Silnik pobierania URL-i przez yt-dlp (Protocol :class:`AcquisitionEngine`).

    GUI używa :meth:`download` (z metadanymi/opcjami); :meth:`acquire` to wariant z interfejsu
    silnika. Runner wstrzykiwany, więc orkiestracja jest testowalna bez sieci i yt-dlp.
    """

    store: RecordingStore | None = None
    ytdlp: str = "yt-dlp"
    runner: DownloadRunner = _default_runner
    probe_runner: Callable[[list[str]], str] | None = None
    name: str = "downloader"

    def can_handle(self, source: Source) -> bool:
        """Obsługuje źródła typu URL."""
        return source.kind is SourceKind.URL

    def probe(self, source: Source) -> list[QualityOption]:
        """Bez wcześniejszego ``-F`` podajemy jedną domyślną opcję (bv*+ba/b albo audio)."""
        return [QualityOption(label="Najlepsza dostępna (wideo+audio)")]

    def acquire(
        self,
        source: Source,
        opts: AcquireOptions,
        progress: Callable[[float, str], None],
    ) -> MediaArtifact:
        """Pobiera URL z ``source.target`` do ``opts.output_dir`` (audio-only wg opcji)."""
        return self.download(source.target, opts.output_dir, progress, audio_only=opts.audio_only)

    def download(
        self,
        url: str,
        library_root: Path,
        progress: Callable[[float, str], None],
        *,
        audio_only: bool = False,
        cookies_browser: str | None = None,
        title: str | None = None,
        category: str | None = None,
        tags: list[str] | None = None,
        presenter: str | None = None,
        organizer: str | None = None,
        cloud_ok: bool = False,
        description: str | None = None,
    ) -> MediaArtifact:
        """Pobiera URL do folderu materiału: yt-dlp → plik + miniatura + info.json → metadata.json.

        Folder = ``library_root/<slug tytułu-podpowiedzi>``; tytuł/kanał/datę/czas trwania czyta
        z ``*.info.json`` (pewniejsze niż stdout). ``cloud_ok`` domyślnie ``False`` (fail-safe) —
        profil źródła może go podnieść tylko dlatego, że użytkownik świadomie tak ustawił.
        Exit != 0 → :class:`RuntimeError` z ostatnimi liniami logu (geo-block/404/prywatne wideo).
        """
        title_hint = title or _title_hint_from_url(url)
        material_dir = library_root / safe_filename(title_hint)
        material_dir.mkdir(parents=True, exist_ok=True)

        command = build_download_command(
            url,
            material_dir,
            ytdlp=self.ytdlp,
            audio_only=audio_only,
            cookies_browser=cookies_browser,
        )
        last_pct = -1

        def on_line(line: str) -> None:
            nonlocal last_pct
            pct = parse_download_progress(line)
            if pct is not None and pct != last_pct:  # throttle: tylko przy zmianie %
                last_pct = pct
                progress(pct / 100.0, f"Pobieranie… {pct}%")

        result = self.runner(command, on_line)
        if result.returncode != 0:
            raise RuntimeError(f"yt-dlp: {_error_message(result.tail)}")

        info = _read_info_json(material_dir)
        media = _find_media_file(material_dir, info)
        thumb = _find_thumbnail(material_dir)
        final_title = (info.title if info and info.title else None) or title_hint
        is_audio = bool(audio_only) or (media is not None and media.suffix.lower() in AUDIO_EXTS)

        duration = info.duration if info and info.duration is not None else None
        if duration is None and media is not None:
            duration = parse_duration(self._probe(build_probe_duration_command(media)))

        meta = MaterialMetadata(
            title=final_title,
            created_at=_now(),
            source_type="download",
            source_url=url,
            presenter=presenter,
            organizer=organizer or (info.uploader if info else None),
            category=category,
            tags=sorted(tags or []),
            duration=duration,
            video_path=media.name if (media is not None and not is_audio) else None,
            audio_path=media.name if (media is not None and is_audio) else None,
            thumbnail_path=thumb.name if thumb is not None else None,
            cloud_ok=cloud_ok,
        )
        write_metadata(material_dir, meta)
        if self.store is not None:
            self.store.upsert_material(material_dir, meta)
        progress(1.0, "Pobrano")

        return MediaArtifact(
            video_path=media if (media is not None and not is_audio) else None,
            audio_path=media if (media is not None and is_audio) else None,
            metadata={"folder": str(material_dir), "url": url},
        )

    def _probe(self, command: list[str]) -> str:
        if self.probe_runner is not None:
            return self.probe_runner(command)
        try:
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=30,
                creationflags=_NO_WINDOW,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return proc.stdout or ""


def _title_hint_from_url(url: str) -> str:
    """Podpowiedź tytułu (nazwa folderu) z URL-a, gdy nie znamy tytułu przed pobraniem."""
    parsed = urlparse(url)
    stem = Path(parsed.path).stem or parsed.hostname or "material"
    return stem


def _read_info_json(material_dir: Path) -> InfoMeta | None:
    """Wczytuje pierwszy ``*.info.json`` z folderu materiału (metadane od yt-dlp)."""
    for path in sorted(material_dir.glob("*.info.json")):
        try:
            return parse_info_json(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return None


def _find_media_file(material_dir: Path, info: InfoMeta | None) -> Path | None:
    """Znajduje pobrany plik mediów (pomija .info.json/.jpg/.webp/miniatury)."""
    skip = {".json", ".jpg", ".jpeg", ".webp", ".png"}
    candidates = [
        p
        for p in sorted(material_dir.iterdir())
        if p.is_file() and p.suffix.lower() not in skip and not p.name.endswith(".info.json")
    ]
    if info and info.ext:
        for p in candidates:
            if p.suffix.lower() == f".{info.ext.lower()}":
                return p
    return candidates[0] if candidates else None


def _find_thumbnail(material_dir: Path) -> Path | None:
    """Znajduje miniaturę (jpg z ``--convert-thumbnails jpg``)."""
    thumbs = sorted(material_dir.glob("*.jpg"))
    return thumbs[0] if thumbs else None
