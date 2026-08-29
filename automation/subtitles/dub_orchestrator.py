#!/usr/bin/env python3
"""Durable, rate-limited acquisition of PT-BR audio sidecars.

The existing language auditor owns the question "which episode needs a dub?".
This worker owns one narrow continuation: obtain a proven dubbed release in a
qBittorrent staging category, align it against the existing library video and
publish external audio.  Sonarr never imports the staging release.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import asdict
from pathlib import Path
from typing import Any

HOME = Path(os.environ.get("HOMELAB_ROOT", "/srv/homelab"))
CONFIG_ROOT = Path(os.environ.get("CONFIG", str(HOME / "config")))
DATA_ROOT = Path(os.environ.get("DATA", "/srv/media"))
_PLUGIN_MODULES = os.environ.get("HOMELAB_PLUGIN_DIR")
if _PLUGIN_MODULES and Path(_PLUGIN_MODULES).is_dir():
    sys.path.insert(0, _PLUGIN_MODULES)

from cached_candidate_adapter import candidates as cached_candidates
from dub_pipeline import AlignmentReport, analyse_episode, episode_spec
from external_audio_builder import render_episode, verify_episode
from timeline_alignment import SystemBusyError


ENV_FILE = HOME / ".env"
STATE_DB = CONFIG_ROOT / "torrent-registry/subtitle-orchestrator.sqlite3"
CANDIDATE_DB = CONFIG_ROOT / "torrent-registry/candidates.sqlite3"
STAGING_CONTAINER = "/data/torrents/dub-staging"
STAGING_HOST = DATA_ROOT / "torrents/dub-staging"
SONARR_URL = "http://127.0.0.1:8989/api/v3"
RADARR_URL = "http://127.0.0.1:7878/api/v3"
QBITTORRENT_URL = "http://127.0.0.1:8080"
# A live indexer query is expensive, but one dead season must not block every
# other proved dub.  The budget is therefore per series/season, not global.
SEARCH_INTERVAL_SECONDS = 6 * 3600
PROBE_STALL_SECONDS = 10 * 60
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".m4v"}


def now() -> int:
    return int(time.time())


def load_env(path: Path = ENV_FILE) -> None:
    if not path.exists():
        return
    for line in path.read_text(errors="ignore").splitlines():
        if line and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"").strip("'"))


def connect_db(path: Path = STATE_DB) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS dub_jobs (
            episode_id INTEGER PRIMARY KEY,
            series_id INTEGER NOT NULL,
            series_title TEXT NOT NULL,
            season_number INTEGER NOT NULL,
            episode_number INTEGER NOT NULL,
            absolute_episode_number INTEGER,
            target_path TEXT NOT NULL,
            state TEXT NOT NULL,
            candidate_json TEXT,
            infohash TEXT,
            source_path TEXT,
            alignment_json TEXT,
            source_owned INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at INTEGER,
            last_error TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS dub_budgets (
            budget_key TEXT PRIMARY KEY,
            last_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS dub_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            episode_id INTEGER NOT NULL,
            happened_at INTEGER NOT NULL,
            event TEXT NOT NULL,
            detail TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS dub_rejected_candidates (
            infohash TEXT PRIMARY KEY,
            rejected_at INTEGER NOT NULL,
            reason TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS movie_dub_jobs (
            movie_id INTEGER PRIMARY KEY,
            movie_title TEXT NOT NULL,
            year INTEGER,
            target_path TEXT NOT NULL,
            state TEXT NOT NULL,
            candidate_json TEXT,
            infohash TEXT,
            source_path TEXT,
            alignment_json TEXT,
            source_owned INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS movie_dub_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            movie_id INTEGER NOT NULL,
            happened_at INTEGER NOT NULL,
            event TEXT NOT NULL,
            detail TEXT NOT NULL
        );
        """
    )
    columns = {row[1] for row in db.execute("PRAGMA table_info(dub_jobs)")}
    if "source_owned" not in columns:
        db.execute(
            "ALTER TABLE dub_jobs ADD COLUMN source_owned INTEGER NOT NULL DEFAULT 0"
        )
    if "next_attempt_at" not in columns:
        db.execute("ALTER TABLE dub_jobs ADD COLUMN next_attempt_at INTEGER")
    db.commit()
    return db


def api_json(
    url: str,
    *,
    method: str = "GET",
    body: object | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 120,
) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    request_headers = {"Content-Type": "application/json", **(headers or {})}
    request = urllib.request.Request(url, data=data, method=method, headers=request_headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read()
    return json.loads(payload) if payload else None


def sonarr(path: str) -> Any:
    key = os.environ.get("SONARR_API_KEY")
    if not key:
        raise RuntimeError("SONARR_API_KEY não configurada")
    return api_json(SONARR_URL + path, headers={"X-Api-Key": key})


def radarr(path: str) -> Any:
    key = os.environ.get("RADARR_API_KEY")
    if not key:
        raise RuntimeError("RADARR_API_KEY não configurada")
    return api_json(RADARR_URL + path, headers={"X-Api-Key": key})


class QBit:
    def __init__(self) -> None:
        jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        data = urllib.parse.urlencode({
            "username": os.environ["QBITTORRENT_USER"],
            "password": os.environ["QBITTORRENT_PASSWORD"],
        }).encode()
        self.opener.open(
            urllib.request.Request(QBITTORRENT_URL + "/api/v2/auth/login", data=data),
            timeout=30,
        ).read()

    def get(self, path: str) -> Any:
        with self.opener.open(QBITTORRENT_URL + path, timeout=60) as response:
            payload = response.read()
        return json.loads(payload) if payload else None

    def post(self, path: str, fields: dict[str, Any]) -> None:
        data = urllib.parse.urlencode(fields).encode()
        self.opener.open(
            urllib.request.Request(QBITTORRENT_URL + path, data=data), timeout=90
        ).read()

    def torrent(self, infohash: str) -> dict[str, Any] | None:
        for item in self.get("/api/v2/torrents/info") or []:
            if str(item.get("hash", "")).casefold() == infohash.casefold():
                return item
        return None

    def ensure_category(self, name: str, save_path: str) -> None:
        categories = self.get("/api/v2/torrents/categories") or {}
        if name in categories:
            return
        self.post("/api/v2/torrents/createCategory", {
            "category": name,
            "savePath": save_path,
        })

    def discard_owned_source(self, infohash: str) -> None:
        """Delete only a failed staging probe owned by this module."""
        self.post("/api/v2/torrents/delete", {
            "hashes": infohash,
            "deleteFiles": "true",
        })


def event(db: sqlite3.Connection, episode_id: int, name: str, detail: str) -> None:
    db.execute(
        "INSERT INTO dub_events(episode_id,happened_at,event,detail) VALUES(?,?,?,?)",
        (episode_id, now(), name, detail[:1000]),
    )


def reject_candidate(db: sqlite3.Connection, infohash: str, reason: str) -> None:
    db.execute(
        """INSERT INTO dub_rejected_candidates(infohash,rejected_at,reason) VALUES(?,?,?)
        ON CONFLICT(infohash) DO UPDATE SET rejected_at=excluded.rejected_at,reason=excluded.reason""",
        (infohash.casefold(), now(), reason[:1000]),
    )


def candidate_is_rejected(db: sqlite3.Connection, infohash: str | None) -> bool:
    if not infohash:
        return False
    return db.execute(
        "SELECT 1 FROM dub_rejected_candidates WHERE infohash=?", (str(infohash).casefold(),)
    ).fetchone() is not None


def movie_event(db: sqlite3.Connection, movie_id: int, name: str, detail: str) -> None:
    db.execute(
        "INSERT INTO movie_dub_events(movie_id,happened_at,event,detail) VALUES(?,?,?,?)",
        (movie_id, now(), name, detail[:1000]),
    )


def sonarr_episode(episode_id: int) -> dict[str, Any]:
    return sonarr(f"/episode/{episode_id}")


def scan_jobs(db: sqlite3.Connection) -> dict[str, int]:
    created = fulfilled = 0
    timestamp = now()
    missing = list(db.execute(
        "SELECT * FROM episode_state WHERE decision_state='missing_dub' ORDER BY updated_at"
    ))
    for row in missing:
        episode = sonarr_episode(row["episode_id"])
        absolute = episode.get("absoluteEpisodeNumber")
        cursor = db.execute(
            """INSERT OR IGNORE INTO dub_jobs(
                   episode_id,series_id,series_title,season_number,episode_number,
                   absolute_episode_number,target_path,state,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,'queued',?,?)""",
            (
                row["episode_id"], row["series_id"], row["series_title"],
                row["season_number"], row["episode_number"], absolute,
                row["file_path"], timestamp, timestamp,
            ),
        )
        created += cursor.rowcount
    missing_ids = {row["episode_id"] for row in missing}
    for row in db.execute(
        "SELECT episode_id FROM dub_jobs WHERE state NOT IN ('published','fulfilled')"
    ):
        if row["episode_id"] not in missing_ids:
            db.execute(
                "UPDATE dub_jobs SET state='fulfilled',updated_at=? WHERE episode_id=?",
                (timestamp, row["episode_id"]),
            )
            fulfilled += 1
    movie_result = scan_movie_jobs(db)
    db.commit()
    return {
        "created": created,
        "fulfilled": fulfilled,
        "missing_dub": len(missing),
        "movie_created": movie_result["created"],
        "movie_fulfilled": movie_result["fulfilled"],
        "movies_missing_dub": movie_result["missing_dub"],
    }


def scan_movie_jobs(db: sqlite3.Connection) -> dict[str, int]:
    """Create durable jobs for Radarr movies proven to have a PT-BR dub."""
    created = fulfilled = 0
    timestamp = now()
    missing = list(db.execute(
        "SELECT * FROM movie_state WHERE decision_state='missing_dub' ORDER BY updated_at"
    ))
    for row in missing:
        cursor = db.execute(
            """INSERT OR IGNORE INTO movie_dub_jobs(
                   movie_id,movie_title,year,target_path,state,created_at,updated_at
               ) VALUES(?,?,?,?,'queued',?,?)""",
            (
                row["movie_id"], row["movie_title"], row["year"],
                row["file_path"], timestamp, timestamp,
            ),
        )
        created += cursor.rowcount
    missing_ids = {row["movie_id"] for row in missing}
    for row in db.execute(
        "SELECT movie_id FROM movie_dub_jobs WHERE state NOT IN ('published','fulfilled')"
    ):
        if row["movie_id"] not in missing_ids:
            db.execute(
                "UPDATE movie_dub_jobs SET state='fulfilled',updated_at=? WHERE movie_id=?",
                (timestamp, row["movie_id"]),
            )
            fulfilled += 1
    return {"created": created, "fulfilled": fulfilled, "missing_dub": len(missing)}


def dub_title_score(title: str) -> int:
    text = " ".join(re.findall(r"[a-z0-9]+", title.casefold()))
    if re.search(r"english dub|dub eng|vostfr|french|truefrench", text):
        return -1
    score = 0
    if re.search(r"dublad[oa]s?|portuguese dub|dub pt br", text):
        score += 120
    if re.search(
        r"(?:multi audio|multi áudio|dual).{0,20}pt br|pt br.{0,20}(?:multi audio|multi áudio|dual)",
        text,
    ):
        score += 100
    if "anipakku" in text or "iceblue" in text:
        score += 90
    if re.search(r"\bmulti\b", text) and "multi subs" not in text:
        score += 35
    if re.search(r"\bpt br\b|\bptbr\b|\bpor\b", text):
        score += 20
    return score if score >= 50 else -1


def dub_probe_score(title: str) -> int:
    """Return whether a proved-dub job may probe this release.

    Availability is established before a job exists, from the deterministic
    episode catalogue.  A release title is therefore not the source of truth:
    it decides only whether the downloader may fetch one selected episode to
    inspect its actual audio tracks.  Explicitly foreign releases and
    subtitle-only ``MULTi`` releases remain ineligible.  Ambiguous
    ``MULTi``/``DUAL`` releases are eligible as low-priority probes, while
    explicit PT-BR evidence stays preferred.
    """
    text = " ".join(re.findall(r"[a-z0-9]+", title.casefold()))
    if re.search(
        r"english dub|dub eng|vostfr|french|truefrench|german|deutsch",
        text,
    ):
        return -1
    has_multi_audio = "multi audio" in text or "multiaudio" in text
    if ("multi subs" in text or "multisubs" in text) and not has_multi_audio:
        return -1
    strong = dub_title_score(title)
    if strong > 0:
        return strong + 100
    if re.search(r"\b(?:multi|dual)\b", text):
        return 1
    return -1


def candidate_rank(candidate: dict[str, Any]) -> tuple[int, int, int, int, int]:
    """Rank probes by likely PT-BR yield, never by title alone as proof.

    A complete provider rip marked ``MULTi`` is more likely to retain every
    provider audio language than a plain ``DUAL`` release, which commonly means
    Japanese plus English.  The selected episode is still always inspected
    before the source is accepted.
    """
    title = str(candidate.get("title", ""))
    text = " ".join(re.findall(r"[a-z0-9]+", title.casefold()))
    resolution_match = re.search(r"\b(720|1080|2160)p\b", title.casefold())
    resolution = int(resolution_match.group(1)) if resolution_match else 0
    strong = dub_title_score(title)
    has_multi = "multi" in text
    has_dual = "dual" in text
    provider_rip = bool(re.search(r"\b(?:cr|crunchyroll)\b", text)) and bool(
        re.search(r"\bweb (?:dl|rip)\b", text)
    )
    if strong > 0:
        probe_class = 4
    elif has_multi and not has_dual and provider_rip:
        probe_class = 3
    elif has_multi and not has_dual:
        probe_class = 2
    elif has_multi:
        probe_class = 1
    else:  # plain DUAL: eligible only as the final low-confidence probe.
        probe_class = 0
    return (probe_class, strong, resolution, int(candidate.get("seeders") or 0), len(title))


def candidate_plan(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Persist an ordered probe plan, so a rejected probe advances safely."""
    if not candidates:
        raise ValueError("candidate plan requires at least one candidate")
    return {"selected": candidates[0], "remaining": candidates[1:]}


def selected_candidate(raw: str | None) -> dict[str, Any]:
    """Read both the new candidate-plan shape and historical single entries."""
    value = json.loads(raw or "{}")
    return value.get("selected", value)


def next_candidate_plan(raw: str | None) -> dict[str, Any] | None:
    """Drop the probe just disproved and return its next durable candidate."""
    value = json.loads(raw or "{}")
    remaining = list(value.get("remaining") or [])
    return candidate_plan(remaining) if remaining else None


def scope_jobs(db: sqlite3.Connection, row: sqlite3.Row) -> list[sqlite3.Row]:
    """Jobs for exactly one requested season; specials remain their own scope."""
    return list(db.execute(
        """SELECT * FROM dub_jobs WHERE series_id=? AND season_number=?
           AND state NOT IN ('published','fulfilled','needs_review','failed')
           ORDER BY episode_number,episode_id""",
        (row["series_id"], row["season_number"]),
    ))


def next_episode_job(
    db: sqlite3.Connection,
    *,
    excluded_scopes: set[tuple[int, int]] | None = None,
) -> sqlite3.Row | None:
    """Fairly pick a scope leader, never twelve independent episodes at once.

    A non-terminal probe/download in a season is always advanced before a new
    probe from that same season.  Across seasons, the oldest scope leader wins.
    This is the scheduler seam: callers only ask for one safe continuation.
    """
    rows = list(db.execute(
        """SELECT * FROM dub_jobs
           WHERE state NOT IN ('published','fulfilled','needs_review','failed')
             AND (state != 'waiting_candidate' OR next_attempt_at IS NULL OR next_attempt_at<=?)
           ORDER BY series_id, season_number, episode_number, episode_id"""
    , (now(),)))
    grouped: dict[tuple[int, int], list[sqlite3.Row]] = {}
    for row in rows:
        key = (int(row["series_id"]), int(row["season_number"]))
        grouped.setdefault(key, []).append(row)
    choices: list[tuple[int, int, int, int, sqlite3.Row]] = []
    active_states = {
        "candidate_selected", "metadata_wait", "downloading", "analysing", "ready",
    }
    excluded_scopes = excluded_scopes or set()
    for (series_id, season), members in grouped.items():
        if (series_id, season) in excluded_scopes:
            continue
        active = [member for member in members if member["state"] in {
            *active_states,
        }]
        if active:
            chosen = min(active, key=lambda item: (item["updated_at"], item["episode_number"], item["episode_id"]))
            choices.append((0, int(chosen["updated_at"]), series_id, season, chosen))
        else:
            # No active source: the first episode is the cheap probe for this scope.
            chosen = members[0]
            choices.append((1, int(chosen["updated_at"]), series_id, season, chosen))
    return min(choices, default=(0, 0, 0, 0, None))[-1]


def cache_candidates(db: sqlite3.Connection, job: sqlite3.Row) -> list[dict[str, Any]]:
    if not CANDIDATE_DB.exists():
        return []
    series = sonarr(f"/series/{job['series_id']}")
    aliases = [series["title"]] + [
        item["title"] for item in series.get("alternateTitles", []) if item.get("title")
    ]
    with sqlite3.connect(CANDIDATE_DB) as registry:
        found = cached_candidates(
            registry, aliases, job["season_number"], job["episode_number"]
        )
    output = []
    for item in found:
        if dub_probe_score(item["title"]) < 0 or not item.get("infohash") or candidate_is_rejected(db, item.get("infohash")):
            continue
        output.append({
            **item,
            "download_url": f"http://prowlarr:9696/__registry__/download/{item['result_id']}",
            "source": "cache",
        })
    return sorted(output, key=candidate_rank, reverse=True)


def normalized_title(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def movie_aliases(movie: dict[str, Any]) -> list[str]:
    aliases = [movie.get("title", ""), movie.get("originalTitle", "")]
    aliases.extend(
        item.get("title", "") for item in movie.get("alternateTitles", [])
        if isinstance(item, dict)
    )
    return [alias for alias in aliases if alias]


def movie_cache_candidates(db: sqlite3.Connection, job: sqlite3.Row) -> list[dict[str, Any]]:
    """Reuse only cached, downloadable releases with strong PT-BR audio evidence."""
    if not CANDIDATE_DB.exists():
        return []
    movie = radarr(f"/movie/{job['movie_id']}")
    aliases = [normalized_title(alias) for alias in movie_aliases(movie)]
    aliases = [alias for alias in aliases if len(alias) >= 6]
    year = str(job["year"] or "")
    with sqlite3.connect(CANDIDATE_DB) as registry:
        registry.row_factory = sqlite3.Row
        rows = registry.execute(
            """SELECT result_id,title,seeders,size_bytes,infohash,last_seen_at
               FROM results
               WHERE download_reference_encrypted IS NOT NULL
                 AND infohash IS NOT NULL
               ORDER BY COALESCE(seeders,0) DESC,last_seen_at DESC"""
        )
        output = []
        for item in rows:
            title = str(item["title"] or "")
            normalized = normalized_title(title)
            if dub_title_score(title) < 0:
                continue
            if not any(alias in normalized or normalized.startswith(alias) for alias in aliases):
                continue
            if year and re.search(r"\b(?:19|20)\d{2}\b", normalized) and year not in normalized:
                continue
            output.append({
                "result_id": item["result_id"],
                "title": title,
                "seeders": int(item["seeders"] or 0),
                "size": int(item["size_bytes"] or 0),
                "infohash": str(item["infohash"]).casefold(),
                "download_url": (
                    "http://prowlarr:9696/__registry__/download/"
                    f"{item['result_id']}"
                ),
                "source": "cache",
            })
    return sorted(output, key=candidate_rank, reverse=True)


def episode_search_budget_key(job: sqlite3.Row) -> str:
    return f"episode-search:{job['series_id']}:{job['season_number']}"


def search_due(db: sqlite3.Connection, job: sqlite3.Row | None = None) -> bool:
    budget_key = episode_search_budget_key(job) if job is not None else "global_indexer_search"
    row = db.execute(
        "SELECT last_at FROM dub_budgets WHERE budget_key=?", (budget_key,)
    ).fetchone()
    return row is None or now() - row["last_at"] >= SEARCH_INTERVAL_SECONDS


def next_search_at(db: sqlite3.Connection, job: sqlite3.Row) -> int:
    row = db.execute(
        "SELECT last_at FROM dub_budgets WHERE budget_key=?", (episode_search_budget_key(job),)
    ).fetchone()
    return now() if row is None else int(row["last_at"]) + SEARCH_INTERVAL_SECONDS


def search_candidates(db: sqlite3.Connection, job: sqlite3.Row) -> list[dict[str, Any]]:
    if not search_due(db, job):
        return []
    timestamp = now()
    db.execute(
        "INSERT INTO dub_budgets VALUES(?,?) "
        "ON CONFLICT(budget_key) DO UPDATE SET last_at=excluded.last_at",
        (episode_search_budget_key(job), timestamp),
    )
    db.commit()
    response = sonarr(f"/release?episodeId={job['episode_id']}") or []
    output = []
    for item in response:
        title = str(item.get("title", ""))
        protocol = str(item.get("protocol", "")).casefold()
        infohash = item.get("infoHash") or item.get("infohash")
        download_url = item.get("downloadUrl")
        if protocol != "torrent" or not infohash or not download_url or candidate_is_rejected(db, str(infohash)):
            continue
        if dub_probe_score(title) < 0:
            continue
        download_url = internal_download_url(str(download_url))
        output.append({
            "result_id": str(item.get("guid") or infohash),
            "title": title,
            "seeders": int(item.get("seeders") or 0),
            "size": int(item.get("size") or 0),
            "infohash": str(infohash).casefold(),
            "download_url": download_url,
            "source": "exact_sonarr_search",
        })
    return sorted(output, key=candidate_rank, reverse=True)


def search_movie_candidates(
    db: sqlite3.Connection, job: sqlite3.Row,
) -> list[dict[str, Any]]:
    if not search_due(db):
        return []
    timestamp = now()
    db.execute(
        "INSERT INTO dub_budgets VALUES('global_indexer_search',?) "
        "ON CONFLICT(budget_key) DO UPDATE SET last_at=excluded.last_at",
        (timestamp,),
    )
    db.commit()
    response = radarr(f"/release?movieId={job['movie_id']}") or []
    output = []
    for item in response:
        title = str(item.get("title", ""))
        protocol = str(item.get("protocol", "")).casefold()
        infohash = item.get("infoHash") or item.get("infohash")
        download_url = item.get("downloadUrl")
        if protocol != "torrent" or not infohash or not download_url:
            continue
        if dub_title_score(title) < 0:
            continue
        output.append({
            "result_id": str(item.get("guid") or infohash),
            "title": title,
            "seeders": int(item.get("seeders") or 0),
            "size": int(item.get("size") or 0),
            "infohash": str(infohash).casefold(),
            "download_url": internal_download_url(str(download_url)),
            "source": "exact_radarr_search",
        })
    return sorted(output, key=candidate_rank, reverse=True)


def internal_download_url(download_url: str) -> str:
    if download_url.startswith("/"):
        return "http://prowlarr:9696" + download_url
    return re.sub(
        r"^https?://(?:127\.0\.0\.1|localhost):9696",
        "http://prowlarr:9696", download_url, flags=re.IGNORECASE,
    )


def select_video_member(
    names: list[dict[str, Any]], season: int, episode: int, absolute: int | None
) -> dict[str, Any] | None:
    videos = [
        item for item in names
        if Path(item.get("name", "")).suffix.casefold() in VIDEO_EXTENSIONS
        and int(item.get("size") or 0) >= 50 * 1024 * 1024
    ]
    if len(videos) == 1:
        return videos[0]
    patterns = [
        rf"\bs0*{season}e0*{episode}\b",
        rf"\b0*{season}x0*{episode}\b",
    ]
    explicit = [item for item in videos if any(
        re.search(pattern, item["name"].casefold().replace("_", " "))
        for pattern in patterns
    )]
    if len(explicit) == 1:
        return explicit[0]
    numbers = [absolute, episode] if absolute is not None else [episode]
    fallback = []
    for item in videos:
        normalized = item["name"].casefold().replace("_", " ").replace(".", " ")
        if any(re.search(rf"(?:episode|ep|\s-\s)\s*0*{number}\b", normalized) for number in numbers):
            fallback.append(item)
    return fallback[0] if len(fallback) == 1 else None


def select_movie_member(names: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Select a movie without confusing it with episodes inside a mixed pack."""
    videos = [
        item for item in names
        if Path(item.get("name", "")).suffix.casefold() in VIDEO_EXTENSIONS
        and int(item.get("size") or 0) >= 100 * 1024 * 1024
    ]
    if len(videos) == 1:
        return videos[0]
    movie_marked = [
        item for item in videos
        if re.search(r"\b(movie|film|filme|gekijouban)\b", normalized_title(item["name"]))
        and not re.search(r"\bs\d{1,2}e\d{1,3}\b", normalized_title(item["name"]))
    ]
    return movie_marked[0] if len(movie_marked) == 1 else None


def update_job(
    db: sqlite3.Connection,
    episode_id: int,
    state: str,
    *,
    error: str | None = None,
    **values: Any,
) -> None:
    columns = ["state=?", "updated_at=?", "last_error=?"]
    params: list[Any] = [state, now(), error]
    for key, value in values.items():
        columns.append(f"{key}=?")
        params.append(value)
    params.append(episode_id)
    db.execute(f"UPDATE dub_jobs SET {','.join(columns)} WHERE episode_id=?", params)


def update_movie_job(
    db: sqlite3.Connection,
    movie_id: int,
    state: str,
    *,
    error: str | None = None,
    **values: Any,
) -> None:
    columns = ["state=?", "updated_at=?", "last_error=?"]
    params: list[Any] = [state, now(), error]
    for key, value in values.items():
        columns.append(f"{key}=?")
        params.append(value)
    params.append(movie_id)
    db.execute(
        f"UPDATE movie_dub_jobs SET {','.join(columns)} WHERE movie_id=?", params
    )


def qbit_host_path(save_path: str, member: str) -> Path:
    if save_path == "/data":
        return DATA_ROOT / member
    if save_path.startswith("/data/"):
        return DATA_ROOT / save_path[len("/data/"):] / member
    raise ValueError(f"save path fora de /data: {save_path}")


def qbit_member_host_path(torrent: dict[str, Any], member: str) -> Path:
    """Resolve a qBittorrent file member through its actual content path.

    Category rules may place active downloads under an incomplete directory
    even when the requested save path points elsewhere.  ``content_path`` is
    qBittorrent's authoritative location and must win over ``save_path``.
    """
    content_path = str(torrent.get("content_path") or "")
    member_path = Path(member)
    if content_path.startswith("/data"):
        host_content = qbit_host_path(content_path, "")
        if host_content.name == member_path.name:
            return host_content
        if member_path.parts and host_content.name == member_path.parts[0]:
            return host_content.joinpath(*member_path.parts[1:])
    return qbit_host_path(str(torrent["save_path"]), member)


def complete_pack_mapping(
    db: sqlite3.Connection, row: sqlite3.Row, files: list[dict[str, Any]],
) -> list[tuple[sqlite3.Row, dict[str, Any]]]:
    """Map every still-missing episode before treating a probe as a season pack.

    Returning an empty list means the release is an individual/ambiguous pack:
    it remains useful only for the probe episode and is never guessed across a
    season.
    """
    members = scope_jobs(db, row)
    videos = [
        item for item in files
        if Path(item.get("name", "")).suffix.casefold() in VIDEO_EXTENSIONS
        and int(item.get("size") or 0) >= 50 * 1024 * 1024
    ]
    if len(videos) < len(members):
        return []
    mapped: list[tuple[sqlite3.Row, dict[str, Any]]] = []
    used: set[int] = set()
    for member in members:
        video = select_video_member(
            files, member["season_number"], member["episode_number"],
            member["absolute_episode_number"],
        )
        if video is None or int(video["index"]) in used:
            return []
        used.add(int(video["index"]))
        mapped.append((member, video))
    return mapped if len(mapped) > 1 else []


def promote_verified_pack(
    db: sqlite3.Connection, qbit: QBit, row: sqlite3.Row,
) -> int:
    """Release all exactly mapped members after one probe has proved the dub.

    The caller has already verified PT-BR audio and timeline for ``row``.  Each
    other episode still goes through its own timeline verification before a
    sidecar is published; the shared probe only avoids rediscovering/downloading
    the same season pack twelve times.
    """
    if not row["source_owned"]:
        return 0
    torrent = qbit.torrent(row["infohash"])
    if torrent is None:
        raise RuntimeError("torrent de staging desapareceu durante a promoção do pack")
    files = qbit.get(f"/api/v2/torrents/files?hash={row['infohash']}") or []
    mappings = complete_pack_mapping(db, row, files)
    if not mappings:
        return 0
    ids = "|".join(str(video["index"]) for _, video in mappings)
    qbit.post("/api/v2/torrents/filePrio", {
        "hash": row["infohash"], "id": ids, "priority": 1,
    })
    qbit.post("/api/v2/torrents/start", {"hashes": row["infohash"]})
    plan_json = row["candidate_json"]
    for member, video in mappings:
        if member["episode_id"] == row["episode_id"]:
            continue
        update_job(
            db, member["episode_id"], "downloading", candidate_json=plan_json,
            infohash=row["infohash"], source_path=str(qbit_member_host_path(torrent, video["name"])),
            source_owned=1, error=None,
        )
        event(db, member["episode_id"], "pack_released", video["name"])
    return len(mappings) - 1


def preview(db: sqlite3.Connection) -> dict[str, Any]:
    episode = db.execute(
        "SELECT * FROM dub_jobs WHERE state NOT IN ('published','fulfilled') "
        "ORDER BY updated_at,episode_id LIMIT 1"
    ).fetchone()
    movie = db.execute(
        "SELECT * FROM movie_dub_jobs WHERE state NOT IN ('published','fulfilled') "
        "ORDER BY updated_at,movie_id LIMIT 1"
    ).fetchone()
    if episode is None and movie is None:
        return {"status": "idle"}
    if movie is not None and (
        episode is None or movie["updated_at"] < episode["updated_at"]
    ):
        return {
            "movie_id": movie["movie_id"],
            "label": f"{movie['movie_title']} ({movie['year'] or '?'})",
            "state": movie["state"],
        }
    return {
        "episode_id": episode["episode_id"],
        "label": f"{episode['series_title']} S{episode['season_number']:02d}E{episode['episode_number']:02d}",
        "state": episode["state"],
    }


def step_episode(
    db: sqlite3.Connection,
    *,
    allow_search: bool,
    excluded_scopes: set[tuple[int, int]] | None = None,
    episode_id: int | None = None,
) -> dict[str, Any]:
    if episode_id is None:
        row = next_episode_job(db, excluded_scopes=excluded_scopes)
    else:
        row = db.execute(
            "SELECT * FROM dub_jobs WHERE episode_id=? "
            "AND state NOT IN ('published','fulfilled')",
            (episode_id,),
        ).fetchone()
    if row is None:
        return {"status": "idle"}
    episode_id, state = row["episode_id"], row["state"]
    label = f"{row['series_title']} S{row['season_number']:02d}E{row['episode_number']:02d}"
    try:
        if state in {"queued", "waiting_candidate"}:
            found = cache_candidates(db, row)
            if not found and allow_search:
                found = search_candidates(db, row)
            if not found:
                update_job(
                    db, episode_id, "waiting_candidate",
                    next_attempt_at=next_search_at(db, row),
                    error="nenhum candidato elegível para sonda no cache; próxima consulta ao Sonarr respeita o cooldown deste escopo",
                )
                event(db, episode_id, "candidate_wait", "no eligible cached probe candidate")
                return {"episode": label, "state": "waiting_candidate"}
            candidate = found[0]
            update_job(
                db, episode_id, "candidate_selected",
                candidate_json=json.dumps(candidate_plan(found), ensure_ascii=False),
                infohash=candidate["infohash"], attempts=row["attempts"] + 1,
                next_attempt_at=None,
            )
            event(db, episode_id, "candidate_selected", candidate["title"])
            return {"episode": label, "state": "candidate_selected", "title": candidate["title"]}

        candidate = selected_candidate(row["candidate_json"])
        qbit = QBit()
        if state == "candidate_selected":
            existing = qbit.torrent(row["infohash"])
            if existing is None:
                qbit.ensure_category("dub-source", STAGING_CONTAINER)
                qbit.post("/api/v2/torrents/add", {
                    "urls": candidate["download_url"],
                    "savepath": STAGING_CONTAINER,
                    "category": "dub-source",
                    "stopped": "true",
                })
                owned = 1
            elif existing.get("category") == "dub-source":
                owned = 1
            elif float(existing.get("progress") or 0) >= 1:
                owned = 0
            else:
                update_job(
                    db, episode_id, "needs_review",
                    error="a fonte já existe como torrent incompleto fora do staging; não será modificada",
                )
                return {"episode": label, "state": "needs_review"}
            update_job(db, episode_id, "metadata_wait", source_owned=owned)
            event(db, episode_id, "torrent_added", row["infohash"])
            return {"episode": label, "state": "metadata_wait"}

        torrent = qbit.torrent(row["infohash"])
        if torrent is None:
            raise RuntimeError("torrent de staging ainda não apareceu no qBittorrent")
        if state == "metadata_wait":
            files = qbit.get(f"/api/v2/torrents/files?hash={row['infohash']}") or []
            if not files and row["source_owned"]:
                # Magnet metadata is only available after contacting peers.  Do
                # that briefly, then stop before any unselected file can run.
                qbit.post("/api/v2/torrents/start", {"hashes": row["infohash"]})
                for _ in range(30):
                    time.sleep(2)
                    files = qbit.get(f"/api/v2/torrents/files?hash={row['infohash']}") or []
                    if files:
                        break
                qbit.post("/api/v2/torrents/stop", {"hashes": row["infohash"]})
            if not files:
                update_job(db, episode_id, "metadata_wait", error="metadata ainda indisponível; manter staging parado e tentar novamente")
                return {"episode": label, "state": "metadata_wait"}
            selected = select_video_member(
                files, row["season_number"], row["episode_number"],
                row["absolute_episode_number"],
            )
            if selected is None:
                update_job(db, episode_id, "needs_review", error="não foi possível identificar exatamente um episódio dentro do torrent")
                return {"episode": label, "state": "needs_review"}
            if row["source_owned"]:
                all_ids = "|".join(str(item["index"]) for item in files)
                qbit.post("/api/v2/torrents/filePrio", {
                    "hash": row["infohash"], "id": all_ids, "priority": 0,
                })
                qbit.post("/api/v2/torrents/filePrio", {
                    "hash": row["infohash"], "id": selected["index"], "priority": 1,
                })
                qbit.post("/api/v2/torrents/start", {"hashes": row["infohash"]})
                source_path = qbit_member_host_path(torrent, selected["name"])
                next_state = "downloading"
                event(db, episode_id, "download_started", selected["name"])
            else:
                source_path = qbit_host_path(torrent["save_path"], selected["name"])
                if not source_path.is_file():
                    raise RuntimeError(f"torrent completo não possui arquivo acessível: {source_path}")
                next_state = "analysing"
                event(db, episode_id, "existing_source_adopted", selected["name"])
            update_job(db, episode_id, next_state, source_path=str(source_path))
            return {"episode": label, "state": next_state, "member": selected["name"]}

        if state == "downloading":
            stalled = (
                float(torrent.get("availability") or 0) <= 0
                and int(torrent.get("dlspeed") or 0) == 0
                and now() - int(row["updated_at"]) >= PROBE_STALL_SECONDS
            )
            if stalled:
                fallback = next_candidate_plan(row["candidate_json"])
                if row["source_owned"]:
                    qbit.discard_owned_source(row["infohash"])
                reject_candidate(db, row["infohash"], "sem peers/disponibilidade por dez minutos")
                if fallback is not None:
                    next_candidate = fallback["selected"]
                    update_job(db, episode_id, "candidate_selected", candidate_json=json.dumps(fallback, ensure_ascii=False), infohash=next_candidate["infohash"], source_path=None, source_owned=0, error="sonda sem peers por dez minutos; tentando próximo candidato")
                    event(db, episode_id, "probe_stalled", "no peers; next candidate")
                    return {"episode": label, "state": "candidate_selected", "reason": "sonda sem peers"}
                update_job(db, episode_id, "waiting_candidate", source_path=None, source_owned=0, next_attempt_at=next_search_at(db, row), error="sonda sem peers por dez minutos; aguardando nova consulta ao Sonarr")
                event(db, episode_id, "probe_stalled", "no peers; search cooldown")
                return {"episode": label, "state": "waiting_candidate", "reason": "sonda sem peers"}
            if float(torrent.get("progress") or 0) < 1:
                return {
                    "episode": label, "state": "downloading",
                    "progress": round(float(torrent.get("progress") or 0), 4),
                }
            source = Path(row["source_path"])
            if row["source_owned"]:
                files = qbit.get(f"/api/v2/torrents/files?hash={row['infohash']}") or []
                selected = select_video_member(
                    files, row["season_number"], row["episode_number"],
                    row["absolute_episode_number"],
                )
                if selected is None:
                    raise RuntimeError("torrent completo perdeu o mapeamento do episódio")
                source = qbit_member_host_path(torrent, selected["name"])
            if not source.is_file():
                raise RuntimeError(f"download terminou, mas fonte não existe: {source}")
            update_job(db, episode_id, "analysing", source_path=str(source))
            return {"episode": label, "state": "analysing"}

        if state == "analysing":
            report = analyse_episode(
                Path(row["source_path"]), Path(row["target_path"]),
                trusted_single_audio_portuguese=dub_title_score(candidate["title"]) > 0,
            )
            if report.confidence != "high":
                # A failed probe proves only that *this* candidate is wrong.
                # Do not leave the season stuck and do not touch a torrent that
                # pre-existed outside our private staging category.
                fallback = next_candidate_plan(row["candidate_json"])
                if row["source_owned"]:
                    qbit.discard_owned_source(row["infohash"])
                reject_candidate(db, row["infohash"], report.reason)
                if fallback is not None:
                    next_candidate = fallback["selected"]
                    update_job(
                        db, episode_id, "candidate_selected",
                        candidate_json=json.dumps(fallback, ensure_ascii=False),
                        infohash=next_candidate["infohash"], source_path=None,
                        alignment_json=None, source_owned=0,
                        error=f"sonda recusada: {report.reason}; tentando próximo candidato",
                    )
                    event(db, episode_id, "candidate_rejected", report.reason)
                    return {"episode": label, "state": "candidate_selected", "reason": report.reason}
                update_job(
                    db, episode_id, "waiting_candidate", source_path=None,
                    alignment_json=None, source_owned=0,
                    error=f"sonda recusada: {report.reason}; sem próximo candidato no plano",
                )
                event(db, episode_id, "candidate_rejected", report.reason)
                return {"episode": label, "state": "waiting_candidate", "reason": report.reason}
            next_state = "ready"
            released = promote_verified_pack(db, qbit, row)
            update_job(
                db, episode_id, next_state,
                alignment_json=json.dumps(asdict(report), ensure_ascii=False),
                error=None,
            )
            event(db, episode_id, "alignment", f"{report.confidence}: {report.reason}")
            return {
                "episode": label, "state": next_state,
                "alignment": asdict(report), "pack_members_released": released,
            }

        if state == "ready":
            report = AlignmentReport(**json.loads(row["alignment_json"]))
            spec = episode_spec(report)
            render_episode(spec, replace=False)
            verify_episode(spec)
            update_job(db, episode_id, "published")
            event(db, episode_id, "published", spec["output"])
            return {"episode": label, "state": "published", "output": spec["output"]}

        raise RuntimeError(f"estado não suportado: {state}")
    except SystemBusyError as exc:
        error = str(exc)
        update_job(db, episode_id, "analysing", error=error)
        event(db, episode_id, "alignment_deferred", error)
        return {"episode": label, "state": "analysing", "deferred": error}
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        update_job(db, episode_id, "failed", error=error)
        event(db, episode_id, "failed", error)
        return {"episode": label, "state": "failed", "error": error}
    finally:
        db.commit()


def step_movie(
    db: sqlite3.Connection, *, allow_search: bool, movie_id: int | None = None,
) -> dict[str, Any]:
    query = (
        "SELECT * FROM movie_dub_jobs WHERE state NOT IN "
        "('published','fulfilled','needs_review','failed')"
    )
    params: tuple[int, ...] = ()
    if movie_id is not None:
        query += " AND movie_id=?"
        params = (movie_id,)
    query += " ORDER BY updated_at,movie_id LIMIT 1"
    row = db.execute(query, params).fetchone()
    if row is None:
        return {"status": "idle"}
    movie_id, state = row["movie_id"], row["state"]
    label = f"{row['movie_title']} ({row['year'] or '?'})"
    try:
        if state in {"queued", "waiting_candidate"}:
            found = movie_cache_candidates(db, row)
            if not found and allow_search:
                found = search_movie_candidates(db, row)
            if not found:
                update_movie_job(
                    db, movie_id, "waiting_candidate",
                    error="nenhuma fonte dublada PT-BR forte no cache/budget atual",
                )
                movie_event(db, movie_id, "candidate_wait", "no strong cached candidate")
                return {"movie": label, "state": "waiting_candidate"}
            candidate = found[0]
            update_movie_job(
                db, movie_id, "candidate_selected",
                candidate_json=json.dumps(candidate, ensure_ascii=False),
                infohash=candidate["infohash"], attempts=row["attempts"] + 1,
            )
            movie_event(db, movie_id, "candidate_selected", candidate["title"])
            return {
                "movie": label, "state": "candidate_selected",
                "title": candidate["title"],
            }

        candidate = json.loads(row["candidate_json"] or "{}")
        qbit = QBit()
        if state == "candidate_selected":
            existing = qbit.torrent(row["infohash"])
            if existing is None:
                qbit.ensure_category("dub-source", STAGING_CONTAINER)
                qbit.post("/api/v2/torrents/add", {
                    "urls": candidate["download_url"],
                    "savepath": STAGING_CONTAINER,
                    "category": "dub-source",
                    "stopped": "true",
                })
                owned = 1
            elif existing.get("category") == "dub-source":
                owned = 1
            elif float(existing.get("progress") or 0) >= 1:
                owned = 0
            else:
                update_movie_job(
                    db, movie_id, "needs_review",
                    error=(
                        "a fonte já existe como torrent incompleto fora do staging; "
                        "não será modificada"
                    ),
                )
                return {"movie": label, "state": "needs_review"}
            update_movie_job(db, movie_id, "metadata_wait", source_owned=owned)
            movie_event(db, movie_id, "torrent_added", row["infohash"])
            return {"movie": label, "state": "metadata_wait"}

        torrent = qbit.torrent(row["infohash"])
        if torrent is None:
            raise RuntimeError("torrent de staging ainda não apareceu no qBittorrent")
        if state == "metadata_wait":
            files = qbit.get(f"/api/v2/torrents/files?hash={row['infohash']}") or []
            selected = select_movie_member(files)
            if selected is None:
                update_movie_job(
                    db, movie_id, "needs_review",
                    error="não foi possível identificar exatamente um filme no torrent",
                )
                return {"movie": label, "state": "needs_review"}
            if row["source_owned"]:
                all_ids = "|".join(str(item["index"]) for item in files)
                qbit.post("/api/v2/torrents/filePrio", {
                    "hash": row["infohash"], "id": all_ids, "priority": 0,
                })
                qbit.post("/api/v2/torrents/filePrio", {
                    "hash": row["infohash"], "id": selected["index"], "priority": 1,
                })
                qbit.post("/api/v2/torrents/start", {"hashes": row["infohash"]})
                source_path = qbit_member_host_path(torrent, selected["name"])
                next_state = "downloading"
                movie_event(db, movie_id, "download_started", selected["name"])
            else:
                source_path = qbit_host_path(torrent["save_path"], selected["name"])
                if not source_path.is_file():
                    raise RuntimeError(
                        f"torrent completo não possui arquivo acessível: {source_path}"
                    )
                next_state = "analysing"
                movie_event(db, movie_id, "existing_source_adopted", selected["name"])
            update_movie_job(db, movie_id, next_state, source_path=str(source_path))
            return {"movie": label, "state": next_state, "member": selected["name"]}

        if state == "downloading":
            if float(torrent.get("progress") or 0) < 1:
                return {
                    "movie": label, "state": "downloading",
                    "progress": round(float(torrent.get("progress") or 0), 4),
                }
            source = Path(row["source_path"])
            if row["source_owned"]:
                files = qbit.get(f"/api/v2/torrents/files?hash={row['infohash']}") or []
                selected = select_movie_member(files)
                if selected is None:
                    raise RuntimeError("torrent completo perdeu o mapeamento do filme")
                source = qbit_member_host_path(torrent, selected["name"])
            if not source.is_file():
                raise RuntimeError(f"download terminou, mas fonte não existe: {source}")
            update_movie_job(db, movie_id, "analysing", source_path=str(source))
            return {"movie": label, "state": "analysing"}

        if state == "analysing":
            report = analyse_episode(
                Path(row["source_path"]), Path(row["target_path"]),
                trusted_single_audio_portuguese=dub_title_score(candidate["title"]) > 0,
            )
            next_state = "ready" if report.confidence == "high" else "needs_review"
            update_movie_job(
                db, movie_id, next_state,
                alignment_json=json.dumps(asdict(report), ensure_ascii=False),
                error=None if next_state == "ready" else report.reason,
            )
            movie_event(db, movie_id, "alignment", f"{report.confidence}: {report.reason}")
            return {"movie": label, "state": next_state, "alignment": asdict(report)}

        if state == "ready":
            report = AlignmentReport(**json.loads(row["alignment_json"]))
            spec = episode_spec(report)
            render_episode(spec, replace=False)
            verify_episode(spec)
            update_movie_job(db, movie_id, "published")
            movie_event(db, movie_id, "published", spec["output"])
            return {"movie": label, "state": "published", "output": spec["output"]}

        raise RuntimeError(f"estado de filme não suportado: {state}")
    except SystemBusyError as exc:
        error = str(exc)
        update_movie_job(db, movie_id, "analysing", error=error)
        movie_event(db, movie_id, "alignment_deferred", error)
        return {"movie": label, "state": "analysing", "deferred": error}
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        update_movie_job(db, movie_id, "failed", error=error)
        movie_event(db, movie_id, "failed", error)
        return {"movie": label, "state": "failed", "error": error}
    finally:
        db.commit()


def step(
    db: sqlite3.Connection,
    *,
    allow_search: bool,
    excluded_scopes: set[tuple[int, int]] | None = None,
) -> dict[str, Any]:
    episode = next_episode_job(db, excluded_scopes=excluded_scopes)
    movie = db.execute(
        "SELECT updated_at FROM movie_dub_jobs WHERE state NOT IN "
        "('published','fulfilled','needs_review','failed') "
        "ORDER BY updated_at,movie_id LIMIT 1"
    ).fetchone()
    if episode is None and movie is None:
        return {"status": "idle"}
    # A proved episodic dub is never blocked by an unrelated movie queue.
    if episode is None and movie is not None:
        return step_movie(db, allow_search=allow_search)
    return step_episode(
        db, allow_search=allow_search, excluded_scopes=excluded_scopes,
    )


def step_batch(
    db: sqlite3.Connection, *, allow_search: bool, limit: int,
) -> list[dict[str, Any]]:
    """Advance distinct season scopes in one scheduler cycle.

    One unseeded probe must not make every other proved-dub season wait for a
    later cron tick.  A scope appears at most once here; its next continuation
    remains available to the following cycle.
    """
    results: list[dict[str, Any]] = []
    excluded_scopes: set[tuple[int, int]] = set()
    for _ in range(limit):
        chosen = next_episode_job(db, excluded_scopes=excluded_scopes)
        if chosen is None:
            break
        scope = (int(chosen["series_id"]), int(chosen["season_number"]))
        result = step(
            db, allow_search=allow_search, excluded_scopes=excluded_scopes,
        )
        results.append(result)
        excluded_scopes.add(scope)
    return results


def status(db: sqlite3.Connection) -> dict[str, Any]:
    states = dict(db.execute("SELECT state,COUNT(*) FROM dub_jobs GROUP BY state"))
    rows = list(db.execute(
        "SELECT series_title,season_number,episode_number,state,last_error "
        "FROM dub_jobs WHERE state NOT IN ('fulfilled') ORDER BY series_title,season_number,episode_number"
    ))
    movie_states = dict(db.execute(
        "SELECT state,COUNT(*) FROM movie_dub_jobs GROUP BY state"
    ))
    movie_rows = list(db.execute(
        "SELECT movie_title,year,state,last_error FROM movie_dub_jobs "
        "WHERE state NOT IN ('fulfilled') ORDER BY movie_title,year"
    ))
    return {
        "states": states,
        "jobs": [dict(row) for row in rows],
        "movie_states": movie_states,
        "movie_jobs": [dict(row) for row in movie_rows],
        "search_interval_seconds": SEARCH_INTERVAL_SECONDS,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("scan")
    step_parser = commands.add_parser("step")
    step_parser.add_argument("--apply", action="store_true")
    step_parser.add_argument("--allow-search", action="store_true")
    step_parser.add_argument("--movie-id", type=int)
    step_parser.add_argument("--episode-id", type=int)
    step_parser.add_argument("--batch", type=int, default=1)
    commands.add_parser("status")
    args = parser.parse_args()
    load_env()
    db = connect_db()
    try:
        if args.command == "scan":
            output = scan_jobs(db)
        elif args.command == "status":
            output = status(db)
        elif not args.apply:
            output = {"mode": "preview", **preview(db)}
        elif args.movie_id is not None and args.episode_id is not None:
            parser.error("--movie-id e --episode-id não podem ser usados juntos")
        elif args.movie_id is not None:
            output = {
                "mode": "apply",
                **step_movie(db, allow_search=args.allow_search, movie_id=args.movie_id),
            }
        else:
            if args.batch < 1:
                parser.error("--batch deve ser pelo menos 1")
            if args.batch == 1:
                if args.episode_id is not None:
                    output = {
                        "mode": "apply",
                        **step_episode(
                            db, allow_search=args.allow_search,
                            episode_id=args.episode_id,
                        ),
                    }
                else:
                    output = {"mode": "apply", **step(db, allow_search=args.allow_search)}
            else:
                output = {
                    "mode": "apply",
                    "batch": step_batch(
                        db, allow_search=args.allow_search, limit=args.batch,
                    ),
                }
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
