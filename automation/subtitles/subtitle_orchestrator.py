#!/usr/bin/env python3
"""Per-item Portuguese audio/subtitle orchestrator for the anime library.

Sonarr and Radarr remain the sole acquisition/import owners. This service
observes files after import. Episode remediation can ask Bazarr for Portuguese
subtitles with a strict budget, then queue server-side text translation or
notebook-side bitmap OCR. Movies are audited and alerted without acquisition
actions. It never deletes, renames, replaces, or edits a video file.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

from subtitle_policy import EpisodeLanguageFacts, LanguagePolicy, evaluate_episode


HOME = Path(os.environ.get("HOMELAB_ROOT", "/srv/homelab"))
CONFIG_ROOT = Path(os.environ.get("CONFIG", str(HOME / "config")))
DATA_ROOT = Path(os.environ.get("DATA", "/srv/media"))
ENV_FILE = HOME / ".env"
DB_PATH = CONFIG_ROOT / "torrent-registry/subtitle-orchestrator.sqlite3"
DUB_CATALOG = Path(os.environ.get(
    "DUB_CATALOG", str(CONFIG_ROOT / "dub-availability/dublagem.json")
))
DUB_SOURCE_DIR = CONFIG_ROOT / "dub-availability"
ANIBRIDGE_CACHE = DUB_SOURCE_DIR / "anibridge-mappings.json"
MYDUBLIST_CACHE = DUB_SOURCE_DIR / "mydublist-high-portuguese.json"
MYDUBLIST_NORMAL_CACHE = DUB_SOURCE_DIR / "mydublist-normal-portuguese.json"
CRUNCHYROLL_EVIDENCE = DUB_SOURCE_DIR / "crunchyroll-ptbr-episodes.json"
BAZARR_CONFIG = HOME / "config/bazarr/config/config.yaml"
TRANSLATOR = CONFIG_ROOT / "translate_series.py"
WORK_DIR = Path("/tmp/subtitle-orchestrator")
TORRENT_ANIME_ROOT = DATA_ROOT / "torrents/anime"
SEVEN_Z = HOME / "tools/7zz"
SONARR_BASE = "http://127.0.0.1:8989/api/v3"
RADARR_BASE = "http://127.0.0.1:7878/api/v3"
BAZARR_BASE = "http://127.0.0.1:6767/api"
SONARR_MEDIA_ROOT = "/data/library"
HOST_MEDIA_ROOT = str(DATA_ROOT / "library")

PT_CODES = {"pt", "por", "pob", "pb", "ptbr", "pt-br", "pt_br"}
EN_CODES = {"en", "eng", "english"}
TEXT_SUBTITLE_CODECS = {
    "ass", "ssa", "subrip", "srt", "webvtt", "mov_text", "text", "ttml"
}
BITMAP_SUBTITLE_CODECS = {
    "hdmv_pgs_subtitle", "pgs", "dvd_subtitle", "dvb_subtitle", "xsub"
}
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".m4v"}
SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".sup"}
AUDIO_SIDECAR_EXTENSIONS = {
    ".aac", ".ac3", ".dts", ".eac3", ".flac", ".m4a", ".mka",
    ".mp3", ".ogg", ".opus", ".wav",
}
IMPORTABLE_SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".vtt", ".sup"}
ARCHIVE_EXTENSIONS = {".7z", ".zip", ".rar"}
MAX_BAZARR_SEARCHES_PER_DAY = 6
MAX_TEXT_TRANSLATIONS_PER_DAY = 2
MAX_OCR_CLAIMS_PER_DAY = 1
JOB_LEASE_SECONDS = 12 * 3600
# Three small bulk datasets; a daily refresh tracks newly dubbed episodes
# without turning the per-episode scanner into a network poller.
DUB_SOURCE_REFRESH_SECONDS = 24 * 3600
CRUNCHYROLL_REFRESH_SECONDS = 20 * 3600
ANIBRIDGE_URL = (
    "https://github.com/anibridge/anibridge-mappings/releases/download/"
    "v3/mappings.min.json"
)
MYDUBLIST_URL = (
    "https://raw.githubusercontent.com/Joelis57/MyDubList/main/"
    "dubs/confidence/high/dubbed_portuguese.json"
)
MYDUBLIST_NORMAL_URL = (
    "https://raw.githubusercontent.com/Joelis57/MyDubList/main/"
    "dubs/confidence/normal/dubbed_portuguese.json"
)


def utc_now() -> int:
    return int(time.time())


def load_env(path: Path = ENV_FILE) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not raw or raw.lstrip().startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"").strip("'"))


def api_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    method: str = "GET",
    fields: dict[str, Any] | None = None,
    timeout: int = 30,
) -> Any:
    data = None
    request_headers = dict(headers or {})
    if fields is not None:
        data = urllib.parse.urlencode(fields).encode()
        request_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    request = urllib.request.Request(
        url, data=data, headers=request_headers, method=method
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
    return json.loads(body) if body else None


def sonarr_get(path: str) -> Any:
    key = os.environ.get("SONARR_API_KEY")
    if not key:
        raise RuntimeError("SONARR_API_KEY is not configured")
    return api_json(SONARR_BASE + path, headers={"X-Api-Key": key})


def radarr_get(path: str) -> Any:
    key = os.environ.get("RADARR_API_KEY")
    if not key:
        raise RuntimeError("RADARR_API_KEY is not configured")
    return api_json(RADARR_BASE + path, headers={"X-Api-Key": key})


def bazarr_key() -> str:
    text = BAZARR_CONFIG.read_text(encoding="utf-8", errors="ignore")
    match = re.search(r"(?m)^\s*apikey:\s*['\"]?([^'\"\s]+)", text)
    if not match:
        raise RuntimeError("Bazarr API key was not found")
    return match.group(1)


def bazarr_language_for_series(series_id: int) -> str:
    """Use the language configured for that exact Bazarr series profile."""
    headers = {"X-API-KEY": bazarr_key()}
    profiles = api_json(
        BAZARR_BASE + "/system/languages/profiles", headers=headers
    ) or []
    profile_languages = {
        int(profile["profileId"]): profile.get("items", [{}])[0].get("language", "pt")
        for profile in profiles
        if profile.get("items")
    }
    response = api_json(
        BAZARR_BASE + "/series?start=0&length=-1", headers=headers
    ) or {}
    for series in response.get("data", []):
        if int(series.get("sonarrSeriesId", -1)) == series_id:
            return profile_languages.get(int(series.get("profileId") or 0), "pt")
    return "pt"


def bazarr_search(series_id: int, episode_id: int) -> None:
    language = bazarr_language_for_series(series_id)
    api_json(
        BAZARR_BASE + "/episodes/subtitles",
        headers={"X-API-KEY": bazarr_key()},
        method="PATCH",
        fields={
            "seriesid": series_id,
            "episodeid": episode_id,
            "language": language,
            "forced": "false",
            "hi": "false",
        },
        timeout=120,
    )


def connect_db(path: Path | None = None) -> sqlite3.Connection:
    path = path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS episode_state (
            episode_id INTEGER PRIMARY KEY,
            series_id INTEGER NOT NULL,
            series_title TEXT NOT NULL,
            season_number INTEGER NOT NULL,
            episode_number INTEGER NOT NULL,
            episode_title TEXT NOT NULL,
            file_id INTEGER NOT NULL,
            file_path TEXT NOT NULL,
            video_signature TEXT NOT NULL,
            date_added INTEGER NOT NULL,
            has_pt_audio INTEGER NOT NULL,
            has_pt_subtitle INTEGER NOT NULL,
            has_english_text_subtitle INTEGER NOT NULL,
            has_english_bitmap_subtitle INTEGER NOT NULL,
            dub_available INTEGER NOT NULL,
            bazarr_attempts INTEGER NOT NULL DEFAULT 0,
            last_bazarr_attempt INTEGER,
            decision_state TEXT NOT NULL,
            last_error TEXT,
            updated_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS probe_cache (
            signature TEXT PRIMARY KEY,
            facts_json TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS archive_cache (
            signature TEXT PRIMARY KEY,
            members_json TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS dub_observations (
            episode_id INTEGER PRIMARY KEY,
            series_title TEXT NOT NULL,
            season_number INTEGER NOT NULL,
            episode_number INTEGER NOT NULL,
            first_seen INTEGER NOT NULL,
            last_seen INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS movie_state (
            movie_id INTEGER PRIMARY KEY,
            movie_title TEXT NOT NULL,
            year INTEGER,
            tmdb_id INTEGER NOT NULL,
            file_id INTEGER NOT NULL,
            file_path TEXT NOT NULL,
            video_signature TEXT NOT NULL,
            date_added INTEGER NOT NULL,
            has_pt_audio INTEGER NOT NULL,
            has_pt_subtitle INTEGER NOT NULL,
            has_english_text_subtitle INTEGER NOT NULL,
            has_english_bitmap_subtitle INTEGER NOT NULL,
            dub_available INTEGER NOT NULL,
            decision_state TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS movie_dub_observations (
            movie_id INTEGER PRIMARY KEY,
            movie_title TEXT NOT NULL,
            tmdb_id INTEGER NOT NULL,
            first_seen INTEGER NOT NULL,
            last_seen INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS movie_alerts (
            movie_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            active INTEGER NOT NULL,
            first_seen INTEGER NOT NULL,
            last_seen INTEGER NOT NULL,
            last_sent INTEGER,
            PRIMARY KEY (movie_id, kind)
        );

        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            episode_id INTEGER NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('text', 'ocr')),
            status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed','cancelled')),
            file_path TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            worker TEXT,
            error TEXT,
            result_json TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_open_job_per_file
            ON jobs(file_path) WHERE status IN ('queued','running');

        CREATE TABLE IF NOT EXISTS alerts (
            episode_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            active INTEGER NOT NULL,
            first_seen INTEGER NOT NULL,
            last_seen INTEGER NOT NULL,
            last_sent INTEGER,
            PRIMARY KEY (episode_id, kind)
        );

        CREATE TABLE IF NOT EXISTS actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            episode_id INTEGER,
            kind TEXT NOT NULL,
            happened_at INTEGER NOT NULL,
            success INTEGER NOT NULL,
            detail TEXT
        );
        """
    )
    db.commit()
    return db


def parse_datetime(value: str | None) -> int:
    if not value:
        return utc_now()
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return int(parsed.timestamp())


def host_path(sonarr_path: str) -> Path:
    if sonarr_path.startswith(SONARR_MEDIA_ROOT + "/"):
        return Path(HOST_MEDIA_ROOT + sonarr_path[len(SONARR_MEDIA_ROOT):])
    return Path(sonarr_path)


def compact_language(value: str) -> str:
    return value.strip().casefold().replace("_", "-")


def is_pt(value: str) -> bool:
    lang = compact_language(value)
    return lang in PT_CODES or "portugu" in lang or "brazil" in lang


def is_en(value: str) -> bool:
    return compact_language(value) in EN_CODES


def trusted_sonarr_pt_audio(episode_file: dict[str, Any]) -> bool:
    """Accept explicit PT-BR metadata only from our reviewed direct-import path.

    Some direct-download MP4 files carry a single ``und`` audio stream, so
    ffprobe cannot identify the spoken language.  Manual imports from that
    reviewed source use the release group ``DirectPTBR`` and explicitly set
    Portuguese (Brazil) in Sonarr.  Requiring both facts prevents an ordinary
    release-name guess from overriding file-level probing.
    """
    if episode_file.get("releaseGroup") != "DirectPTBR":
        return False
    return any(
        is_pt(str(language.get("name", "")))
        or int(language.get("id", -1)) == 33
        for language in episode_file.get("languages") or []
    )


def stream_labels(stream: dict[str, Any]) -> list[str]:
    tags = stream.get("tags") or {}
    return [str(tags.get("language", "")), str(tags.get("title", ""))]


def matching_sidecars(video: Path) -> list[Path]:
    if not video.parent.exists():
        return []
    prefix = video.stem + "."
    return sorted(
        p for p in video.parent.iterdir()
        if p.is_file()
        and p.name.startswith(prefix)
        and p.suffix.casefold() in SUBTITLE_EXTENSIONS
        and p.stat().st_size > 0
    )


def matching_audio_sidecars(video: Path) -> list[Path]:
    """Return external audio tracks belonging to this exact video basename."""
    if not video.parent.exists():
        return []
    prefix = video.stem + "."
    return sorted(
        path for path in video.parent.iterdir()
        if path.is_file()
        and path.name.startswith(prefix)
        and path.suffix.casefold() in AUDIO_SIDECAR_EXTENSIONS
        and path.stat().st_size > 0
    )


def sidecar_language(path: Path, video: Path) -> str:
    middle = path.name[len(video.stem) + 1 : -len(path.suffix)]
    tokens = re.split(r"[._ -]+", middle.casefold())
    joined = "-".join(tokens)
    for token in tokens + [joined]:
        if is_pt(token):
            return "pt"
        if is_en(token):
            return "en"
    return "unknown"


def has_pt_marker(value: str) -> bool:
    normalized = normalize_title(value).replace("_", " ")
    if re.search(r"\bpt\s*pt\b|portuguese\s+portugal|portugues\s+portugal", normalized):
        return False
    return bool(
        re.search(r"\bpt\s*br\b|\bptbr\b|brazilian\s+portuguese|portugues\s+brasileiro", normalized)
        or re.search(r"(?:^|[._ -])por(?:[._ -]|$)", normalized)
    )


def has_en_marker(value: str) -> bool:
    normalized = normalize_title(value).replace("_", " ")
    return bool(
        re.search(r"\ben\s*us\b|\beng\b|\benglish\b", normalized)
        and not re.search(r"\bpt\s*br\b|\bptbr\b|brazilian", normalized)
    )


def member_matches_episode(value: str, episode_number: int) -> bool:
    name = Path(value).name.casefold()
    for match in re.finditer(r"s\d{1,2}e(\d{1,3})", name):
        if int(match.group(1)) == episode_number:
            return True
    for match in re.finditer(r"\b(?:ep(?:isode)?[ ._-]*)?(\d{1,3})\b", name):
        if int(match.group(1)) == episode_number:
            prefix = name[max(0, match.start() - 3):match.start()]
            suffix = name[match.end():match.end() + 5]
            if "-" in prefix or "ep" in prefix or suffix.startswith((" ", ".", "_", "[")):
                return True
    return False


def choose_source_member(
    members: list[str], episode_number: int, language: str
) -> str | None:
    candidates = []
    for member in members:
        suffix = Path(member).suffix.casefold()
        if suffix not in IMPORTABLE_SUBTITLE_EXTENSIONS:
            continue
        language_match = has_pt_marker(member) if language == "pt" else has_en_marker(member)
        if not language_match or not member_matches_episode(member, episode_number):
            continue
        normalized = normalize_title(member)
        score = 0
        if language == "pt":
            score += 80 if re.search(r"\bpt\s*br\b|\bptbr\b", normalized) else 0
            score += 30 if "brazilian" in normalized or "brasileiro" in normalized else 0
        else:
            score -= 60 if re.search(r"sign|song|karaoke|typeset", normalized) else 0
        score += 20 if suffix == ".srt" else 0
        score += 15 if "netflix" in normalized else 0
        score += 10 if "crunchyroll" in normalized else 0
        candidates.append((score, -len(member), member))
    return max(candidates)[2] if candidates else None


def choose_pt_source_member(members: list[str], episode_number: int) -> str | None:
    return choose_source_member(members, episode_number, "pt")


def build_torrent_inode_index() -> dict[tuple[int, int], Path]:
    index: dict[tuple[int, int], Path] = {}
    if not TORRENT_ANIME_ROOT.exists():
        return index
    for path in TORRENT_ANIME_ROOT.rglob("*"):
        try:
            if path.is_file() and path.suffix.casefold() in VIDEO_EXTENSIONS:
                stat = path.stat()
                index[(stat.st_dev, stat.st_ino)] = path
        except OSError:
            continue
    return index


def archive_members(db: sqlite3.Connection, archive: Path) -> list[str]:
    if not SEVEN_Z.exists():
        return []
    stat = archive.stat()
    signature = hashlib.sha256(
        f"{archive}:{stat.st_size}:{stat.st_mtime_ns}".encode()
    ).hexdigest()
    row = db.execute(
        "SELECT members_json FROM archive_cache WHERE signature=?", (signature,)
    ).fetchone()
    if row:
        return json.loads(row["members_json"])
    result = subprocess.run(
        [str(SEVEN_Z), "l", "-slt", str(archive)],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    members = [
        line[7:] for line in result.stdout.splitlines()
        if line.startswith("Path = ")
        and Path(line[7:]).suffix.casefold() in IMPORTABLE_SUBTITLE_EXTENSIONS
    ]
    db.execute(
        "INSERT OR REPLACE INTO archive_cache VALUES(?,?,?)",
        (signature, json.dumps(members, ensure_ascii=False), utc_now()),
    )
    return members


def find_external_source(
    db: sqlite3.Connection,
    torrent_index: dict[tuple[int, int], Path],
    video: Path,
    episode_number: int,
    language: str,
) -> dict[str, str] | None:
    stat = video.stat()
    original = torrent_index.get((stat.st_dev, stat.st_ino))
    if original is None:
        return None

    loose_members: list[str] = []
    loose_paths: dict[str, Path] = {}
    for candidate in original.parent.rglob("*"):
        try:
            if candidate.is_file() and candidate.suffix.casefold() in IMPORTABLE_SUBTITLE_EXTENSIONS:
                relative = str(candidate.relative_to(original.parent))
                loose_members.append(relative)
                loose_paths[relative] = candidate
        except OSError:
            continue
    chosen = choose_source_member(loose_members, episode_number, language)
    if chosen:
        return {
            "kind": "file", "path": str(loose_paths[chosen]),
            "extension": loose_paths[chosen].suffix.casefold(), "language": language,
        }

    for archive in sorted(original.parent.iterdir()):
        if not archive.is_file() or archive.suffix.casefold() not in ARCHIVE_EXTENSIONS:
            continue
        try:
            chosen = choose_source_member(
                archive_members(db, archive), episode_number, language
            )
        except Exception:
            continue
        if chosen:
            return {
                "kind": "archive", "path": str(archive), "member": chosen,
                "extension": Path(chosen).suffix.casefold(), "language": language,
            }
    return None


def materialize_subtitle(source: dict[str, str], video_path: str) -> Path:
    video = Path(video_path)
    extension = source["extension"]
    language_tag = "pt-BR" if source["language"] == "pt" else "en"
    target = video.with_name(video.stem + "." + language_tag + extension)
    if target.exists() and target.stat().st_size > 0:
        return target
    if target.exists():
        target = video.with_name(video.stem + "." + language_tag + ".recovered" + extension)
        if target.exists() and target.stat().st_size > 0:
            return target
    created = False
    try:
        with target.open("xb") as output:
            created = True
            if source["kind"] == "file":
                with Path(source["path"]).open("rb") as input_file:
                    shutil.copyfileobj(input_file, output)
            else:
                result = subprocess.run(
                    [str(SEVEN_Z), "e", "-so", source["path"], source["member"]],
                    stdout=output,
                    stderr=subprocess.PIPE,
                    timeout=300,
                )
                if result.returncode != 0:
                    raise RuntimeError(result.stderr.decode(errors="replace")[-1000:])
        if target.stat().st_size == 0:
            raise RuntimeError("subtitle source produced an empty file")
        return target
    except Exception:
        if created and target.exists():
            target.unlink()
        raise


def signatures(video: Path) -> tuple[str, str]:
    stat = video.stat()
    video_sig = f"{video}:{stat.st_size}:{stat.st_mtime_ns}"
    sidecars = matching_sidecars(video) + matching_audio_sidecars(video)
    full = [video_sig]
    for sidecar in sidecars:
        side_stat = sidecar.stat()
        full.append(f"{sidecar.name}:{side_stat.st_size}:{side_stat.st_mtime_ns}")
    return (
        hashlib.sha256(video_sig.encode()).hexdigest(),
        hashlib.sha256("|".join(full).encode()).hexdigest(),
    )


def probe_language_facts(db: sqlite3.Connection, video: Path) -> tuple[str, dict[str, bool]]:
    video_sig, probe_sig = signatures(video)
    cached = db.execute(
        "SELECT facts_json FROM probe_cache WHERE signature=?", (probe_sig,)
    ).fetchone()
    if cached:
        return video_sig, json.loads(cached["facts_json"])

    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries",
            "stream=codec_type,codec_name:stream_tags=language,title",
            "-of", "json", str(video),
        ],
        capture_output=True,
        text=True,
        timeout=90,
        check=True,
    )
    streams = json.loads(result.stdout or "{}").get("streams", [])
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    subtitles = [s for s in streams if s.get("codec_type") == "subtitle"]
    has_pt_audio = any(any(is_pt(v) for v in stream_labels(s)) for s in audio)
    has_pt_subtitle = any(
        any(is_pt(v) for v in stream_labels(s)) for s in subtitles
    )
    en_subtitles = [
        s for s in subtitles if any(is_en(v) for v in stream_labels(s))
    ]
    has_en_text = any(
        str(s.get("codec_name", "")).casefold() in TEXT_SUBTITLE_CODECS
        for s in en_subtitles
    )
    has_en_bitmap = any(
        str(s.get("codec_name", "")).casefold() in BITMAP_SUBTITLE_CODECS
        for s in en_subtitles
    )

    for sidecar in matching_sidecars(video):
        language = sidecar_language(sidecar, video)
        has_pt_subtitle = has_pt_subtitle or language == "pt"
        has_en_text = has_en_text or language == "en"

    for sidecar in matching_audio_sidecars(video):
        has_pt_audio = has_pt_audio or sidecar_language(sidecar, video) == "pt"

    facts = {
        "has_pt_audio": has_pt_audio,
        "has_pt_subtitle": has_pt_subtitle,
        "has_english_text_subtitle": has_en_text,
        "has_english_bitmap_subtitle": has_en_bitmap,
    }
    db.execute(
        "INSERT OR REPLACE INTO probe_cache(signature,facts_json,created_at) VALUES(?,?,?)",
        (probe_sig, json.dumps(facts, sort_keys=True), utc_now()),
    )
    return video_sig, facts


def normalize_title(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def load_dub_catalog() -> dict[str, Any]:
    if not DUB_CATALOG.exists():
        return {}
    return json.loads(DUB_CATALOG.read_text(encoding="utf-8")).get("series", {})


def download_atomic(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    request = urllib.request.Request(
        url, headers={"User-Agent": "homelab-dub-availability/1.0"}
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response, temporary.open("xb") as output:
            shutil.copyfileobj(response, output)
        # Parse before replacing the last known-good copy.
        json.loads(temporary.read_text(encoding="utf-8"))
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def refresh_dub_sources(*, force: bool = False) -> dict[str, Any]:
    """Refresh two bulk datasets, never one request per library episode."""
    now = utc_now()
    refreshed: list[str] = []
    skipped: list[str] = []
    for name, url, destination in (
        ("AnimeBridge", ANIBRIDGE_URL, ANIBRIDGE_CACHE),
        ("MyDubList high", MYDUBLIST_URL, MYDUBLIST_CACHE),
        ("MyDubList normal", MYDUBLIST_NORMAL_URL, MYDUBLIST_NORMAL_CACHE),
    ):
        fresh = (
            destination.exists()
            and now - int(destination.stat().st_mtime) < DUB_SOURCE_REFRESH_SECONDS
        )
        if fresh and not force:
            skipped.append(name)
            continue
        download_atomic(url, destination)
        refreshed.append(name)
    calendar = refresh_crunchyroll_calendar(force=force)
    return {"refreshed": refreshed, "cached": skipped, "crunchyroll": calendar}


def crunchyroll_calendar_html(day: dt.date) -> str:
    url = (
        "https://www.crunchyroll.com/pt-br/simulcastcalendar?"
        + urllib.parse.urlencode({"date": day.isoformat(), "filter": "premium"})
    )
    result = subprocess.run(
        [
            "curl", "-fsSL", url,
            "-H", "User-Agent: Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 Chrome/140.0.0.0 Safari/537.36",
            "-H", "Accept-Language: pt-BR,pt;q=0.9,en;q=0.8",
        ],
        check=True, capture_output=True, text=True, timeout=120,
    )
    return result.stdout


def crunchyroll_season_number(title: str) -> int:
    without_language = re.sub(
        r"\s*\((?:Portugu[eê]s \(Brasil\)|Portuguese Dub)\)\s*$", "", title,
        flags=re.IGNORECASE,
    )
    patterns = (
        r"(?:^|\s)(\d+)[ªº]?\s+Temporada\b",
        r"\bSeason\s+(\d+)\b",
        r"\b(\d+)(?:st|nd|rd|th)\s+Season\b",
    )
    for pattern in patterns:
        match = re.search(pattern, without_language, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return 1


def parse_crunchyroll_calendar(html_text: str) -> list[dict[str, Any]]:
    """Extract only exact Brazilian-Portuguese episode releases."""
    starts = [m.start() for m in re.finditer(r'<article class="release\b', html_text)]
    records: list[dict[str, Any]] = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(html_text)
        block = html_text[start:end]
        if not re.search(r'data-popover-url="[^"]*PTBR"', block, re.IGNORECASE):
            continue
        episode_match = re.search(r'data-episode-num="([0-9]+(?:\.[0-9]+)?)"', block)
        series_match = re.search(
            r'href="https://www\.crunchyroll\.com/pt-br/series/([^/"?]+)/[^"?]*"',
            block, re.IGNORECASE,
        )
        title_match = re.search(
            r'<h1 class="season-name">.*?<cite itemprop="name">(.*?)</cite>',
            block, re.IGNORECASE | re.DOTALL,
        )
        published_match = re.search(
            r'<meta content="([^"]+)" itemprop="datePublished">', block,
            re.IGNORECASE,
        )
        if not (episode_match and series_match and title_match):
            continue
        episode_value = float(episode_match.group(1))
        if not episode_value.is_integer():
            continue
        title = re.sub(r"\s+", " ", title_match.group(1)).strip()
        records.append(
            {
                "crunchyroll_id": series_match.group(1),
                "season": crunchyroll_season_number(title),
                "episode": int(episode_value),
                "season_title": title,
                "published_at": published_match.group(1) if published_match else None,
                "source": "Crunchyroll simulcast calendar PTBR",
            }
        )
    return records


def refresh_crunchyroll_calendar(*, force: bool = False) -> dict[str, Any]:
    now = utc_now()
    if (
        CRUNCHYROLL_EVIDENCE.exists() and not force
        and now - int(CRUNCHYROLL_EVIDENCE.stat().st_mtime)
        < CRUNCHYROLL_REFRESH_SECONDS
    ):
        payload = json.loads(CRUNCHYROLL_EVIDENCE.read_text(encoding="utf-8"))
        return {"cached": True, "episodes": len(payload.get("episodes", {}))}

    existing: dict[str, Any] = {}
    first_run = not CRUNCHYROLL_EVIDENCE.exists()
    if not first_run:
        existing = json.loads(CRUNCHYROLL_EVIDENCE.read_text(encoding="utf-8")).get(
            "episodes", {}
        )
    today = dt.datetime.now(dt.timezone.utc).date()
    monday = today - dt.timedelta(days=today.weekday())
    # Four weeks only on bootstrap; afterwards current+previous week. This is
    # bounded and cumulative, so there is no per-title or per-episode polling.
    weeks = 4 if first_run else 2
    pages = 0
    added = 0
    for offset in range(weeks):
        records = parse_crunchyroll_calendar(
            crunchyroll_calendar_html(monday - dt.timedelta(days=7 * offset))
        )
        pages += 1
        for record in records:
            key = (
                f"{record['crunchyroll_id']}:s{record['season']}:"
                f"e{record['episode']}"
            )
            if key not in existing:
                added += 1
            existing[key] = record
    payload = {
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "episodes": dict(sorted(existing.items())),
    }
    CRUNCHYROLL_EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    temporary = CRUNCHYROLL_EVIDENCE.with_suffix(".json.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False, indent=2, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(CRUNCHYROLL_EVIDENCE)
    finally:
        temporary.unlink(missing_ok=True)
    return {"cached": False, "pages": pages, "added": added, "episodes": len(existing)}


def load_crunchyroll_evidence() -> set[tuple[str, int, int]]:
    if not CRUNCHYROLL_EVIDENCE.exists():
        return set()
    payload = json.loads(CRUNCHYROLL_EVIDENCE.read_text(encoding="utf-8"))
    return {
        (str(row["crunchyroll_id"]), int(row["season"]), int(row["episode"]))
        for row in payload.get("episodes", {}).values()
    }


def crunchyroll_dub_is_available(
    catalog: dict[str, Any], evidence: set[tuple[str, int, int]],
    series_title: str, series_path: str, season: int, episode: int,
) -> bool:
    haystack = normalize_title(series_title + " " + series_path)
    for name, declaration in catalog.items():
        if normalize_title(name) not in haystack:
            continue
        ids = declaration.get("crunchyroll_ids", declaration.get("crunchyroll_id", []))
        if isinstance(ids, str):
            ids = [ids]
        return any((str(series_id), season, episode) in evidence for series_id in ids)
    return False


def load_external_dub_index() -> tuple[dict[str, Any], set[int], set[int]]:
    """Load exact episode mappings plus conservative PT-BR availability.

    MyDubList's ``high`` and ``normal`` feeds are both curator-backed, daily
    bulk datasets.  They are only accepted after AnimeBridge maps the exact
    TVDB episode to the MAL title; no title-only match can create a dub job.
    """
    if not (ANIBRIDGE_CACHE.exists() and MYDUBLIST_CACHE.exists()
            and MYDUBLIST_NORMAL_CACHE.exists()):
        return {}, set(), set()
    mappings = json.loads(ANIBRIDGE_CACHE.read_text(encoding="utf-8"))
    high_payload = json.loads(MYDUBLIST_CACHE.read_text(encoding="utf-8"))
    normal_payload = json.loads(MYDUBLIST_NORMAL_CACHE.read_text(encoding="utf-8"))
    return (
        mappings,
        {int(value) for value in high_payload.get("dubbed", [])},
        {int(value) for value in normal_payload.get("dubbed", [])},
    )


def episode_in_mapping_range(episode: int, expression: str) -> bool:
    """Match the TVDB side of an AnimeBridge episode expression."""
    value = str(expression).strip()
    if value.isdigit():
        return episode == int(value)
    match = re.fullmatch(r"(\d+)-(\d+)", value)
    return bool(match and int(match.group(1)) <= episode <= int(match.group(2)))


def external_dub_is_available(
    index: tuple[dict[str, Any], set[int]], tvdb_id: int,
    season: int, episode: int,
) -> bool:
    """Resolve a high-confidence, full PT dub to an exact TVDB episode."""
    mappings, high_mal_ids, normal_mal_ids = index
    dubbed_mal_ids = high_mal_ids | normal_mal_ids
    targets = mappings.get(f"tvdb_show:{tvdb_id}:s{season}") or {}
    for target, ranges in targets.items():
        if not target.startswith("mal:"):
            continue
        try:
            mal_id = int(target.split(":", 1)[1])
        except ValueError:
            continue
        if mal_id not in dubbed_mal_ids or not isinstance(ranges, dict):
            continue
        if any(episode_in_mapping_range(episode, source) for source in ranges):
            return True
    return False


def external_dubbed_episode_numbers(
    index: tuple[dict[str, Any], set[int], set[int]], tvdb_id: int, season: int,
    episode_numbers: set[int],
) -> set[int]:
    """Return the regular episodes covered by a mapped PT-BR dub catalogue.

    A MyDubList entry describes a dubbed title, while AnimeBridge describes the
    TVDB-to-MAL episode mapping.  Together they are a season fact, not a
    release-name hint.  Keeping this calculation here makes the inheritance
    rule explicit and keeps specials out of the result.
    """
    mappings, high_mal_ids, normal_mal_ids = index
    targets = mappings.get(f"tvdb_show:{tvdb_id}:s{season}") or {}
    covered: set[int] = set()
    for target, ranges in targets.items():
        if not target.startswith("mal:") or not isinstance(ranges, dict):
            continue
        try:
            mal_id = int(target.split(":", 1)[1])
        except ValueError:
            continue
        if mal_id not in high_mal_ids | normal_mal_ids:
            continue
        covered.update(
            episode for episode in episode_numbers
            if any(episode_in_mapping_range(episode, source) for source in ranges)
        )
    return covered


def completed_season_dub_is_available(
    index: tuple[dict[str, Any], set[int], set[int]], tvdb_id: int,
    season: int, regular_episode_numbers: set[int], *, series_ended: bool,
) -> bool:
    """Deterministically inherit a PT-BR dub across a completed regular season.

    No season-0 item can inherit this status.  An airing show remains
    episode-specific because a provider can release a new episode before its
    dub.  For a concluded season, a dubbed mapped title that covers every
    regular TVDB episode establishes the property for the season as a whole.
    """
    if not series_ended or season <= 0 or not regular_episode_numbers:
        return False
    covered = external_dubbed_episode_numbers(
        index, tvdb_id, season, regular_episode_numbers,
    )
    return regular_episode_numbers <= covered


def external_movie_dub_is_available(
    index: tuple[dict[str, Any], set[int]], tmdb_id: int,
) -> bool:
    """Resolve a high-confidence, complete PT-BR movie dub from its TMDB id."""
    mappings, high_mal_ids, normal_mal_ids = index
    dubbed_mal_ids = high_mal_ids | normal_mal_ids
    targets = mappings.get(f"tmdb_movie:{tmdb_id}") or {}
    for target in targets:
        if not target.startswith("mal:"):
            continue
        try:
            mal_id = int(target.split(":", 1)[1])
        except ValueError:
            continue
        if mal_id in dubbed_mal_ids:
            return True
    return False


def dub_is_available(
    catalog: dict[str, Any], series_title: str, series_path: str, season: int,
    episode: int | None = None,
) -> bool:
    """Return only explicit per-episode dub availability.

    The preferred catalog shape is ``temporadas -> season -> episodios``.
    ``episodios`` accepts ``todos``, an exact list, or ``{"ate": N}``.  The
    legacy season-level shape remains readable for completed-series entries,
    but new/airing entries must use the exact shape.
    """
    haystack = normalize_title(series_title + " " + series_path)
    for name, declaration in catalog.items():
        if normalize_title(name) not in haystack:
            continue
        exact = declaration.get("temporadas") or {}
        season_rule = exact.get(str(season), exact.get(season))
        if season_rule is not None:
            episodes = (
                season_rule.get("episodios")
                if isinstance(season_rule, dict)
                else season_rule
            )
            if isinstance(episodes, str) and episodes in {"todos", "tudo"}:
                return True
            if episode is None:
                return False
            if isinstance(episodes, list):
                return episode in episodes
            if isinstance(episodes, dict) and "ate" in episodes:
                return 1 <= episode <= int(episodes["ate"])
            return False
        seasons = declaration.get("dublado")
        return seasons == "tudo" or season in (seasons or [])
    return False


def translation_status(db: sqlite3.Connection, file_path: str) -> str | None:
    row = db.execute(
        "SELECT status FROM jobs WHERE file_path=? ORDER BY id DESC LIMIT 1",
        (file_path,),
    ).fetchone()
    return row["status"] if row else None


def update_alerts(
    db: sqlite3.Connection, episode_id: int, current: tuple[str, ...], now: int
) -> None:
    current_set = set(current)
    previous = {
        row["kind"]: row
        for row in db.execute("SELECT * FROM alerts WHERE episode_id=?", (episode_id,))
    }
    for kind in current_set:
        row = previous.get(kind)
        if row is None:
            db.execute(
                "INSERT INTO alerts VALUES(?,?,?,?,?,NULL)",
                (episode_id, kind, 1, now, now),
            )
        elif not row["active"]:
            db.execute(
                "UPDATE alerts SET active=1,first_seen=?,last_seen=?,last_sent=NULL "
                "WHERE episode_id=? AND kind=?",
                (now, now, episode_id, kind),
            )
        else:
            db.execute(
                "UPDATE alerts SET last_seen=? WHERE episode_id=? AND kind=?",
                (now, episode_id, kind),
            )
    for kind, row in previous.items():
        if row["active"] and kind not in current_set:
            db.execute(
                "UPDATE alerts SET active=0,last_seen=? WHERE episode_id=? AND kind=?",
                (now, episode_id, kind),
            )


def update_movie_alerts(
    db: sqlite3.Connection, movie_id: int, current: tuple[str, ...], now: int
) -> None:
    current_set = set(current)
    previous = {
        row["kind"]: row
        for row in db.execute(
            "SELECT * FROM movie_alerts WHERE movie_id=?", (movie_id,)
        )
    }
    for kind in current_set:
        row = previous.get(kind)
        if row is None:
            db.execute(
                "INSERT INTO movie_alerts VALUES(?,?,?,?,?,NULL)",
                (movie_id, kind, 1, now, now),
            )
        elif not row["active"]:
            db.execute(
                "UPDATE movie_alerts SET active=1,first_seen=?,last_seen=?,"
                "last_sent=NULL WHERE movie_id=? AND kind=?",
                (now, now, movie_id, kind),
            )
        else:
            db.execute(
                "UPDATE movie_alerts SET last_seen=? WHERE movie_id=? AND kind=?",
                (now, movie_id, kind),
            )
    for kind, row in previous.items():
        if row["active"] and kind not in current_set:
            db.execute(
                "UPDATE movie_alerts SET active=0,last_seen=? "
                "WHERE movie_id=? AND kind=?",
                (now, movie_id, kind),
            )


def record_action(
    db: sqlite3.Connection,
    episode_id: int | None,
    kind: str,
    success: bool,
    detail: str = "",
) -> None:
    db.execute(
        "INSERT INTO actions(episode_id,kind,happened_at,success,detail) VALUES(?,?,?,?,?)",
        (episode_id, kind, utc_now(), int(success), detail[:1000]),
    )


def used_today(db: sqlite3.Connection, kind: str) -> int:
    now = dt.datetime.now(dt.timezone.utc)
    start = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    return db.execute(
        "SELECT COUNT(*) FROM actions WHERE kind=? AND happened_at>=?",
        (kind, start),
    ).fetchone()[0]


def queue_job(
    db: sqlite3.Connection, episode_id: int, kind: str, file_path: str
) -> bool:
    now = utc_now()
    try:
        db.execute(
            "INSERT INTO jobs(episode_id,kind,status,file_path,created_at,updated_at) "
            "VALUES(?,?, 'queued', ?, ?, ?)",
            (episode_id, kind, file_path, now, now),
        )
        record_action(db, episode_id, f"queue_{kind}_translation", True)
        return True
    except sqlite3.IntegrityError:
        return False


def execute_one_action(db: sqlite3.Connection, candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    priority = {
        "materialize_portuguese_subtitle": 0,
        "materialize_english_subtitle": 1,
        "search_bazarr": 2,
        "queue_text_translation": 3,
        "queue_ocr_translation": 4,
    }
    candidates.sort(
        key=lambda item: (priority[item["action"]], -item["date_added"])
    )
    for item in candidates:
        action = item["action"]
        episode_id = item["episode_id"]
        if action in {
            "materialize_portuguese_subtitle", "materialize_english_subtitle"
        }:
            error = ""
            success = False
            try:
                target = materialize_subtitle(item["subtitle_source"], item["file_path"])
                success = True
                detail = str(target)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                detail = error
            record_action(db, episode_id, action, success, detail)
            if not success:
                db.execute(
                    "UPDATE episode_state SET last_error=? WHERE episode_id=?",
                    (error, episode_id),
                )
            return {"action": action, "episode": item["label"], "success": success, "error": error}
        if action == "search_bazarr":
            if used_today(db, "bazarr_search") >= MAX_BAZARR_SEARCHES_PER_DAY:
                continue
            error = ""
            success = False
            try:
                bazarr_search(item["series_id"], episode_id)
                success = True
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
            now = utc_now()
            db.execute(
                "UPDATE episode_state SET bazarr_attempts=bazarr_attempts+1, "
                "last_bazarr_attempt=?,last_error=? WHERE episode_id=?",
                (now, error or None, episode_id),
            )
            record_action(db, episode_id, "bazarr_search", success, error)
            return {"action": action, "episode": item["label"], "success": success, "error": error}
        if action == "queue_text_translation":
            if queue_job(db, episode_id, "text", item["file_path"]):
                return {"action": action, "episode": item["label"], "success": True}
        if action == "queue_ocr_translation":
            if queue_job(db, episode_id, "ocr", item["file_path"]):
                return {"action": action, "episode": item["label"], "success": True}
    return None


def collect_episodes(db: sqlite3.Connection) -> tuple[list[dict[str, Any]], Counter]:
    series_list = sonarr_get("/series")
    catalog = load_dub_catalog()
    crunchyroll_evidence = load_crunchyroll_evidence()
    external_index = load_external_dub_index()
    policy = LanguagePolicy()
    now = utc_now()
    torrent_index = build_torrent_inode_index()
    candidates: list[dict[str, Any]] = []
    counts: Counter = Counter()
    seen_episode_ids: set[int] = set()

    for series in series_list:
        episodes = sonarr_get(
            f"/episode?seriesId={series['id']}&includeEpisodeFile=true"
        )
        # A concluded TV season with a mapped PT-BR dub is a deterministic
        # season-level fact.  We calculate it once from all regular TVDB
        # episodes, then inherit it only for that season.  Specials (S00) and
        # airing shows deliberately stay episode-specific.
        series_ended = bool(series.get("ended")) or str(
            series.get("status", "")
        ).casefold() in {"ended", "completed"}
        regular_numbers: dict[int, set[int]] = {}
        for candidate_episode in episodes:
            season_number = int(candidate_episode.get("seasonNumber") or 0)
            episode_number = int(candidate_episode.get("episodeNumber") or 0)
            if season_number > 0 and episode_number > 0:
                regular_numbers.setdefault(season_number, set()).add(episode_number)
        completed_dubbed_seasons = {
            season_number
            for season_number, numbers in regular_numbers.items()
            if completed_season_dub_is_available(
                external_index, int(series.get("tvdbId") or 0), season_number,
                numbers, series_ended=series_ended,
            )
        }
        for episode in episodes:
            if not episode.get("hasFile") or not episode.get("episodeFile"):
                continue
            episode_file = episode["episodeFile"]
            video = host_path(episode_file.get("path") or "")
            if video.suffix.casefold() not in VIDEO_EXTENSIONS or not video.exists():
                counts["missing_on_disk"] += 1
                continue
            try:
                video_signature, probe = probe_language_facts(db, video)
            except Exception as exc:
                counts["probe_error"] += 1
                continue
            if not probe["has_pt_audio"] and trusted_sonarr_pt_audio(episode_file):
                probe["has_pt_audio"] = True

            previous = db.execute(
                "SELECT * FROM episode_state WHERE episode_id=?", (episode["id"],)
            ).fetchone()
            file_changed = previous is not None and (
                previous["file_id"] != episode_file["id"]
                or previous["video_signature"] != video_signature
            )
            bazarr_attempts = 0 if file_changed or previous is None else previous["bazarr_attempts"]
            last_bazarr = None if file_changed or previous is None else previous["last_bazarr_attempt"]
            if file_changed:
                db.execute(
                    "UPDATE jobs SET status='cancelled',updated_at=? "
                    "WHERE episode_id=? AND status IN ('queued','running')",
                    (now, episode["id"]),
                )

            date_added = parse_datetime(episode_file.get("dateAdded"))
            if probe["has_pt_audio"]:
                db.execute(
                    """
                    INSERT INTO dub_observations(
                        episode_id,series_title,season_number,episode_number,first_seen,last_seen
                    ) VALUES(?,?,?,?,?,?)
                    ON CONFLICT(episode_id) DO UPDATE SET last_seen=excluded.last_seen
                    """,
                    (
                        episode["id"], series["title"], episode["seasonNumber"],
                        episode["episodeNumber"], now, now,
                    ),
                )
            observed_dub = db.execute(
                "SELECT 1 FROM dub_observations WHERE episode_id=?", (episode["id"],)
            ).fetchone() is not None
            is_dubbed_release = (
                episode["seasonNumber"] in completed_dubbed_seasons
            ) or dub_is_available(
                catalog, series["title"], series.get("path", ""),
                episode["seasonNumber"], episode["episodeNumber"],
            ) or crunchyroll_dub_is_available(
                catalog, crunchyroll_evidence, series["title"],
                series.get("path", ""), episode["seasonNumber"],
                episode["episodeNumber"],
            ) or external_dub_is_available(
                external_index, int(series.get("tvdbId") or 0),
                episode["seasonNumber"], episode["episodeNumber"],
            ) or observed_dub
            if is_dubbed_release and not probe["has_pt_audio"]:
                # A newly confirmed dub supersedes subtitle work that had not
                # started yet. Running work is left alone because it may
                # already be writing its result and cannot be interrupted
                # safely from the scanner.
                db.execute(
                    "UPDATE jobs SET status='cancelled',updated_at=?,"
                    "error='cancelled: PT-BR dub became the episode target' "
                    "WHERE episode_id=? AND status='queued'",
                    (now, episode["id"]),
                )
            external_source = None
            english_external_source = None
            if not probe["has_pt_audio"] and not probe["has_pt_subtitle"]:
                external_source = find_external_source(
                    db, torrent_index, video, episode["episodeNumber"], "pt"
                )
                if (
                    external_source is None
                    and not probe["has_english_text_subtitle"]
                    and not probe["has_english_bitmap_subtitle"]
                ):
                    english_external_source = find_external_source(
                        db, torrent_index, video, episode["episodeNumber"], "en"
                    )
            status = translation_status(db, str(video))
            facts = EpisodeLanguageFacts(
                age_seconds=max(0, now - date_added),
                has_pt_audio=probe["has_pt_audio"],
                has_pt_subtitle=probe["has_pt_subtitle"],
                has_pt_external_subtitle=external_source is not None,
                has_english_text_subtitle=probe["has_english_text_subtitle"],
                has_english_bitmap_subtitle=probe["has_english_bitmap_subtitle"],
                has_english_external_subtitle=english_external_source is not None,
                dub_available=is_dubbed_release,
                bazarr_attempts=bazarr_attempts,
                seconds_since_bazarr_attempt=(
                    None if last_bazarr is None else max(0, now - last_bazarr)
                ),
                translation_status=status,
            )
            decision = evaluate_episode(facts, policy)
            seen_episode_ids.add(episode["id"])
            label = (
                f"{series['title']} S{episode['seasonNumber']:02d}"
                f"E{episode['episodeNumber']:02d}"
            )
            db.execute(
                """
                INSERT INTO episode_state(
                    episode_id,series_id,series_title,season_number,episode_number,
                    episode_title,file_id,file_path,video_signature,date_added,
                    has_pt_audio,has_pt_subtitle,has_english_text_subtitle,
                    has_english_bitmap_subtitle,dub_available,bazarr_attempts,
                    last_bazarr_attempt,decision_state,last_error,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(episode_id) DO UPDATE SET
                    series_id=excluded.series_id,series_title=excluded.series_title,
                    season_number=excluded.season_number,episode_number=excluded.episode_number,
                    episode_title=excluded.episode_title,file_id=excluded.file_id,
                    file_path=excluded.file_path,video_signature=excluded.video_signature,
                    date_added=excluded.date_added,has_pt_audio=excluded.has_pt_audio,
                    has_pt_subtitle=excluded.has_pt_subtitle,
                    has_english_text_subtitle=excluded.has_english_text_subtitle,
                    has_english_bitmap_subtitle=excluded.has_english_bitmap_subtitle,
                    dub_available=excluded.dub_available,bazarr_attempts=excluded.bazarr_attempts,
                    last_bazarr_attempt=excluded.last_bazarr_attempt,
                    decision_state=excluded.decision_state,updated_at=excluded.updated_at
                """,
                (
                    episode["id"], series["id"], series["title"], episode["seasonNumber"],
                    episode["episodeNumber"], episode.get("title", ""), episode_file["id"],
                    str(video), video_signature, date_added, int(probe["has_pt_audio"]),
                    int(probe["has_pt_subtitle"]), int(probe["has_english_text_subtitle"]),
                    int(probe["has_english_bitmap_subtitle"]), int(is_dubbed_release),
                    bazarr_attempts, last_bazarr, decision.state,
                    previous["last_error"] if previous else None, now,
                ),
            )
            update_alerts(db, episode["id"], decision.alerts, now)
            counts[decision.state] += 1
            if decision.action:
                candidate = {
                        "action": decision.action,
                        "episode_id": episode["id"],
                        "series_id": series["id"],
                        "file_path": str(video),
                        "date_added": date_added,
                        "label": label,
                    }
                selected_source = external_source or english_external_source
                if selected_source is not None:
                    candidate["subtitle_source"] = selected_source
                candidates.append(candidate)

    known_ids = {
        row[0] for row in db.execute("SELECT episode_id FROM episode_state")
    }
    absent_ids = known_ids - seen_episode_ids
    if absent_ids:
        placeholders = ",".join("?" for _ in absent_ids)
        params = (now, *sorted(absent_ids))
        db.execute(
            f"UPDATE episode_state SET decision_state='absent',updated_at=? "
            f"WHERE episode_id IN ({placeholders})",
            params,
        )
        db.execute(
            f"UPDATE alerts SET active=0,last_seen=? "
            f"WHERE episode_id IN ({placeholders})",
            params,
        )
        counts["absent"] += len(absent_ids)
    return candidates, counts


def collect_movies(db: sqlite3.Connection) -> Counter:
    """Audit Radarr movie files without triggering searches or translation.

    Dub availability comes from the same cached AnimeBridge + MyDubList bulk
    index used for series, mapped by the movie's exact TMDB id. A PT-BR audio
    track observed locally is also durable evidence that the movie was dubbed.
    """
    movies = radarr_get("/movie")
    external_index = load_external_dub_index()
    policy = LanguagePolicy()
    now = utc_now()
    counts: Counter = Counter()
    seen_movie_ids: set[int] = set()

    for movie in movies:
        movie_file = movie.get("movieFile")
        if not movie.get("hasFile") or not movie_file:
            continue
        video = host_path(movie_file.get("path") or "")
        if video.suffix.casefold() not in VIDEO_EXTENSIONS or not video.exists():
            counts["missing_on_disk"] += 1
            continue
        try:
            video_signature, probe = probe_language_facts(db, video)
        except Exception:
            counts["probe_error"] += 1
            continue

        movie_id = int(movie["id"])
        tmdb_id = int(movie.get("tmdbId") or 0)
        date_added = parse_datetime(movie_file.get("dateAdded"))
        seen_movie_ids.add(movie_id)
        if probe["has_pt_audio"]:
            db.execute(
                """
                INSERT INTO movie_dub_observations(
                    movie_id,movie_title,tmdb_id,first_seen,last_seen
                ) VALUES(?,?,?,?,?)
                ON CONFLICT(movie_id) DO UPDATE SET
                    movie_title=excluded.movie_title,tmdb_id=excluded.tmdb_id,
                    last_seen=excluded.last_seen
                """,
                (movie_id, movie["title"], tmdb_id, now, now),
            )
        observed_dub = db.execute(
            "SELECT 1 FROM movie_dub_observations WHERE movie_id=?", (movie_id,)
        ).fetchone() is not None
        dub_available = (
            bool(tmdb_id and external_movie_dub_is_available(external_index, tmdb_id))
            or observed_dub
        )
        facts = EpisodeLanguageFacts(
            age_seconds=max(0, now - date_added),
            has_pt_audio=probe["has_pt_audio"],
            has_pt_subtitle=probe["has_pt_subtitle"],
            has_pt_external_subtitle=False,
            has_english_text_subtitle=probe["has_english_text_subtitle"],
            has_english_bitmap_subtitle=probe["has_english_bitmap_subtitle"],
            has_english_external_subtitle=False,
            dub_available=dub_available,
            bazarr_attempts=0,
            seconds_since_bazarr_attempt=None,
            translation_status=None,
        )
        decision = evaluate_episode(facts, policy)
        # Movie remediation remains with Radarr/Bazarr. This collector records
        # facts and alerts only; it deliberately ignores decision.action.
        db.execute(
            """
            INSERT INTO movie_state(
                movie_id,movie_title,year,tmdb_id,file_id,file_path,
                video_signature,date_added,has_pt_audio,has_pt_subtitle,
                has_english_text_subtitle,has_english_bitmap_subtitle,
                dub_available,decision_state,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(movie_id) DO UPDATE SET
                movie_title=excluded.movie_title,year=excluded.year,
                tmdb_id=excluded.tmdb_id,file_id=excluded.file_id,
                file_path=excluded.file_path,
                video_signature=excluded.video_signature,
                date_added=excluded.date_added,
                has_pt_audio=excluded.has_pt_audio,
                has_pt_subtitle=excluded.has_pt_subtitle,
                has_english_text_subtitle=excluded.has_english_text_subtitle,
                has_english_bitmap_subtitle=excluded.has_english_bitmap_subtitle,
                dub_available=excluded.dub_available,
                decision_state=excluded.decision_state,
                updated_at=excluded.updated_at
            """,
            (
                movie_id, movie["title"], movie.get("year"), tmdb_id,
                int(movie_file["id"]), str(video), video_signature, date_added,
                int(probe["has_pt_audio"]), int(probe["has_pt_subtitle"]),
                int(probe["has_english_text_subtitle"]),
                int(probe["has_english_bitmap_subtitle"]), int(dub_available),
                decision.state, now,
            ),
        )
        update_movie_alerts(db, movie_id, decision.alerts, now)
        counts[decision.state] += 1

    known_ids = {row[0] for row in db.execute("SELECT movie_id FROM movie_state")}
    absent_ids = known_ids - seen_movie_ids
    if absent_ids:
        placeholders = ",".join("?" for _ in absent_ids)
        params = (now, *sorted(absent_ids))
        db.execute(
            f"UPDATE movie_state SET decision_state='absent',updated_at=? "
            f"WHERE movie_id IN ({placeholders})",
            params,
        )
        db.execute(
            f"UPDATE movie_alerts SET active=0,last_seen=? "
            f"WHERE movie_id IN ({placeholders})",
            params,
        )
        counts["absent"] += len(absent_ids)
    return counts


def post_discord_lines(lines: list[str]) -> None:
    webhook = os.environ.get("DISCORD_WEBHOOK")
    if not webhook:
        raise RuntimeError("DISCORD_WEBHOOK is not configured")
    chunks: list[str] = []
    current = ""
    for line in lines:
        candidate = line if not current else current + "\n" + line
        if len(candidate) > 1950 and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    for content in chunks:
        payload = json.dumps({"content": content[:1950]}).encode()
        request = urllib.request.Request(
            webhook,
            data=payload,
            headers={
                "Content-Type": "application/json",
                # Discord rejects urllib's default agent with 403/1010.
                "User-Agent": "homelab-subtitle-orchestrator/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30):
            pass


def send_pending_alerts(db: sqlite3.Connection) -> int:
    episode_rows = db.execute(
        """
        SELECT a.episode_id,a.kind,e.series_title,e.season_number,e.episode_number
        FROM alerts a JOIN episode_state e ON e.episode_id=a.episode_id
        WHERE a.active=1 AND a.last_sent IS NULL
        ORDER BY e.series_title,e.season_number,e.episode_number
        """
    ).fetchall()
    movie_rows = db.execute(
        """
        SELECT a.movie_id,a.kind,m.movie_title,m.year
        FROM movie_alerts a JOIN movie_state m ON m.movie_id=a.movie_id
        WHERE a.active=1 AND a.last_sent IS NULL
        ORDER BY m.movie_title,m.year
        """
    ).fetchall()
    grouped: dict[str, dict[str, dict[int, list[int]]]] = {
        "dub_available_but_missing": {}, "missing_portuguese": {}
    }
    for row in episode_rows:
        grouped[row["kind"]].setdefault(row["series_title"], {}).setdefault(
            row["season_number"], []
        ).append(row["episode_number"])
    titles = {
        "dub_available_but_missing": "🔊 Dub PT-BR disponível, mas ausente na biblioteca",
        "missing_portuguese": "💬 Sem dub confirmado e sem legenda PT-BR",
    }
    if episode_rows:
        sections = ["**Auditoria de idioma por episódio**"]
        for kind in ("dub_available_but_missing", "missing_portuguese"):
            series_groups = grouped[kind]
            if not series_groups:
                continue
            condition_count = sum(
                len(episodes) for seasons in series_groups.values()
                for episodes in seasons.values()
            )
            sections.append(f"\n{titles[kind]}: **{condition_count}**")
            for series_title, seasons in series_groups.items():
                season_parts = []
                for season, episodes in sorted(seasons.items()):
                    ranges = compact_episode_ranges(episodes)
                    season_parts.append(f"S{season:02d}{ranges}")
                sections.append(f"• {series_title} — {'; '.join(season_parts)}")
        post_discord_lines(sections)

    if movie_rows:
        movie_sections = ["**Auditoria de idioma por filme**"]
        for kind in ("dub_available_but_missing", "missing_portuguese"):
            rows_for_kind = [row for row in movie_rows if row["kind"] == kind]
            if not rows_for_kind:
                continue
            movie_sections.append(f"\n{titles[kind]}: **{len(rows_for_kind)}**")
            for row in rows_for_kind:
                year = f" ({row['year']})" if row["year"] else ""
                movie_sections.append(f"• {row['movie_title']}{year}")
        post_discord_lines(movie_sections)

    now = utc_now()
    if episode_rows:
        db.executemany(
            "UPDATE alerts SET last_sent=? WHERE episode_id=? AND kind=?",
            [(now, row["episode_id"], row["kind"]) for row in episode_rows],
        )
    if movie_rows:
        db.executemany(
            "UPDATE movie_alerts SET last_sent=? WHERE movie_id=? AND kind=?",
            [(now, row["movie_id"], row["kind"]) for row in movie_rows],
        )
    total = len(episode_rows) + len(movie_rows)
    if total:
        record_action(db, None, "discord_alert", True, f"{total} conditions")
    return total


def compact_episode_ranges(episodes: list[int]) -> str:
    values = sorted(set(int(value) for value in episodes))
    ranges: list[str] = []
    start = end = values[0]
    for value in values[1:]:
        if value == end + 1:
            end = value
            continue
        ranges.append(
            f"E{start:02d}" if start == end else f"E{start:02d}–E{end:02d}"
        )
        start = end = value
    ranges.append(f"E{start:02d}" if start == end else f"E{start:02d}–E{end:02d}")
    return ", ".join(ranges)


def scan(*, apply: bool, notify: bool) -> dict[str, Any]:
    load_env()
    db = connect_db()
    candidates, episode_counts = collect_episodes(db)
    movie_counts = collect_movies(db)
    performed = execute_one_action(db, candidates) if apply else None
    notified = send_pending_alerts(db) if notify else 0
    db.commit()
    result = {
        # Compatibility aliases retained for existing log/status consumers.
        "states": dict(sorted(episode_counts.items())),
        "episode_states": dict(sorted(episode_counts.items())),
        "movie_states": dict(sorted(movie_counts.items())),
        "eligible_actions": Counter(x["action"] for x in candidates),
        "performed": performed,
        "alert_conditions_sent": notified,
    }
    result["eligible_actions"] = dict(result["eligible_actions"])
    return result


def status() -> dict[str, Any]:
    db = connect_db()
    episode_states = dict(db.execute(
        "SELECT decision_state,COUNT(*) FROM episode_state GROUP BY decision_state"
    ).fetchall())
    movie_states = dict(db.execute(
        "SELECT decision_state,COUNT(*) FROM movie_state GROUP BY decision_state"
    ).fetchall())
    jobs = dict(db.execute("SELECT status,COUNT(*) FROM jobs GROUP BY status").fetchall())
    episode_alerts = dict(db.execute(
        "SELECT kind,COUNT(*) FROM alerts WHERE active=1 GROUP BY kind"
    ).fetchall())
    movie_alerts = dict(db.execute(
        "SELECT kind,COUNT(*) FROM movie_alerts WHERE active=1 GROUP BY kind"
    ).fetchall())
    return {
        "episodes": sum(episode_states.values()),
        "states": episode_states,
        "episode_states": episode_states,
        "movies": sum(movie_states.values()),
        "movie_states": movie_states,
        "jobs": jobs,
        "active_alerts": episode_alerts,
        "active_episode_alerts": episode_alerts,
        "active_movie_alerts": movie_alerts,
        "usage_today": {
            "bazarr_searches": used_today(db, "bazarr_search"),
            "text_translation_starts": used_today(db, "text_translation_started"),
            "ocr_claims": used_today(db, "ocr_translation_claimed"),
        },
    }


def claim_job(kind: str, worker: str) -> dict[str, Any] | None:
    db = connect_db()
    budget_kind = "ocr_translation_claimed" if kind == "ocr" else "text_translation_started"
    limit = MAX_OCR_CLAIMS_PER_DAY if kind == "ocr" else MAX_TEXT_TRANSLATIONS_PER_DAY
    if used_today(db, budget_kind) >= limit:
        return None
    db.execute("BEGIN IMMEDIATE")
    now = utc_now()
    db.execute(
        "UPDATE jobs SET status='queued',worker=NULL,updated_at=?,"
        "error='worker lease expired; safely requeued' "
        "WHERE status='running' AND updated_at<?",
        (now, now - JOB_LEASE_SECONDS),
    )
    row = db.execute(
        "SELECT * FROM jobs WHERE kind=? AND status='queued' ORDER BY created_at,id LIMIT 1",
        (kind,),
    ).fetchone()
    if row is None:
        db.commit()
        return None
    db.execute(
        "UPDATE jobs SET status='running',worker=?,updated_at=? WHERE id=?",
        (worker, now, row["id"]),
    )
    record_action(db, row["episode_id"], budget_kind, True, f"job {row['id']}")
    db.commit()
    return {
        "id": row["id"], "kind": row["kind"], "file_path": row["file_path"],
        "episode_id": row["episode_id"],
    }


def finish_job(job_id: int, *, success: bool, result: dict[str, Any]) -> None:
    db = connect_db()
    status_value = "completed" if success else "failed"
    db.execute(
        "UPDATE jobs SET status=?,updated_at=?,error=?,result_json=? "
        "WHERE id=? AND status='running'",
        (
            status_value, utc_now(), None if success else str(result.get("error", "failed"))[:1000],
            json.dumps(result, ensure_ascii=False), job_id,
        ),
    )
    db.commit()


def work_text() -> dict[str, Any]:
    load_env()
    job = claim_job("text", "server")
    if job is None:
        return {"status": "idle_or_daily_budget_reached"}
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    try:
        spec = importlib.util.spec_from_file_location("translate_series", TRANSLATOR)
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load translator")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        result = module.process_episode(Path(job["file_path"]), WORK_DIR)
        success = result.get("status") in {"translated", "already_has_pt"}
        finish_job(job["id"], success=success, result=result)
        return {"job": job["id"], **result}
    except Exception as exc:
        result = {"error": f"{type(exc).__name__}: {exc}"}
        finish_job(job["id"], success=False, result=result)
        return {"job": job["id"], "status": "failed", **result}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    scan_parser = sub.add_parser("scan")
    scan_parser.add_argument("--apply", action="store_true")
    scan_parser.add_argument("--notify", action="store_true")
    sub.add_parser("status")
    refresh = sub.add_parser("refresh-dubs")
    refresh.add_argument("--force", action="store_true")
    sub.add_parser("work-text")
    claim = sub.add_parser("claim-ocr")
    claim.add_argument("--worker", default="notebook")
    complete = sub.add_parser("complete-job")
    complete.add_argument("job_id", type=int)
    complete.add_argument("--result", default="{}")
    fail = sub.add_parser("fail-job")
    fail.add_argument("job_id", type=int)
    fail.add_argument("--error", required=True)
    args = parser.parse_args()

    if args.command == "scan":
        output = scan(apply=args.apply, notify=args.notify)
    elif args.command == "status":
        output = status()
    elif args.command == "refresh-dubs":
        output = refresh_dub_sources(force=args.force)
    elif args.command == "work-text":
        output = work_text()
    elif args.command == "claim-ocr":
        output = claim_job("ocr", args.worker)
    elif args.command == "complete-job":
        finish_job(args.job_id, success=True, result=json.loads(args.result))
        output = {"status": "completed", "job": args.job_id}
    else:
        finish_job(args.job_id, success=False, result={"error": args.error})
        output = {"status": "failed", "job": args.job_id}
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
