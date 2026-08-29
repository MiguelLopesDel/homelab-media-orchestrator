#!/usr/bin/env python3
"""Observe media requests and maintain their durable reconciliation plans.

The observer records state and may blocklist only downloads proven dead under
the narrow policy below. Cache grabs, imports and bounded searches are executed
by the separate dispatcher.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import http.cookiejar
import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener

from media_orchestrator_core import (
    PreviousPlan,
    ScopeFacts,
    dead_near_empty_candidate,
    reconcile_scope,
    satisfied_queue_ids,
    scope_is_available,
)


SENSITIVE = {"apikey", "api_key", "authorization", "password", "cookie", "token"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
  source TEXT NOT NULL,
  entity_key TEXT NOT NULL,
  first_seen_at INTEGER NOT NULL,
  last_seen_at INTEGER NOT NULL,
  state_hash TEXT NOT NULL,
  state_json TEXT NOT NULL,
  PRIMARY KEY (source, entity_key)
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  occurred_at INTEGER NOT NULL,
  source TEXT NOT NULL,
  entity_key TEXT NOT NULL,
  event_type TEXT NOT NULL,
  before_json TEXT,
  after_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_source_time ON events(source, occurred_at DESC);
CREATE TABLE IF NOT EXISTS policies (
  title_key TEXT PRIMARY KEY,
  urgency TEXT NOT NULL CHECK (urgency IN ('normal', 'urgent')),
  updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
  source TEXT NOT NULL,
  entity_key TEXT NOT NULL,
  evaluated_at INTEGER NOT NULL,
  urgency TEXT NOT NULL,
  decision TEXT NOT NULL,
  reason TEXT NOT NULL,
  eta_seconds INTEGER,
  candidate_hint TEXT,
  PRIMARY KEY (source, entity_key)
);
CREATE TABLE IF NOT EXISTS decision_events (
  id INTEGER PRIMARY KEY,
  occurred_at INTEGER NOT NULL,
  source TEXT NOT NULL,
  entity_key TEXT NOT NULL,
  decision TEXT NOT NULL,
  reason TEXT NOT NULL,
  eta_seconds INTEGER,
  candidate_hint TEXT
);
CREATE TABLE IF NOT EXISTS download_observations (
  infohash TEXT PRIMARY KEY,
  last_progress REAL NOT NULL,
  zero_speed_samples INTEGER NOT NULL,
  first_zero_speed_at INTEGER,
  last_seen_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS replacement_actions (
  source TEXT NOT NULL,
  owner_id INTEGER NOT NULL,
  acted_at INTEGER NOT NULL,
  queue_ids_json TEXT NOT NULL,
  PRIMARY KEY (source, owner_id)
);
CREATE TABLE IF NOT EXISTS replacement_action_scopes (
  source TEXT NOT NULL,
  scope_key TEXT NOT NULL,
  acted_at INTEGER NOT NULL,
  queue_ids_json TEXT NOT NULL,
  PRIMARY KEY (source, scope_key)
);
CREATE TABLE IF NOT EXISTS requested_scope_status (
  request_id INTEGER NOT NULL,
  series_id INTEGER NOT NULL,
  season_number INTEGER NOT NULL,
  state TEXT NOT NULL,
  missing_episodes INTEGER NOT NULL,
  queue_episodes INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (request_id, season_number)
);
CREATE TABLE IF NOT EXISTS scope_reconciliation (
  request_id INTEGER NOT NULL,
  series_id INTEGER NOT NULL,
  season_number INTEGER NOT NULL,
  lifecycle_state TEXT NOT NULL,
  action TEXT,
  next_action_at INTEGER,
  reason TEXT NOT NULL,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  last_action_at INTEGER,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (request_id, season_number)
);
CREATE TABLE IF NOT EXISTS scope_verification (
  request_id INTEGER NOT NULL,
  series_id INTEGER NOT NULL,
  season_number INTEGER NOT NULL,
  imported_episodes INTEGER NOT NULL,
  missing_episodes INTEGER NOT NULL,
  language_pending_episodes INTEGER NOT NULL,
  language_unscanned_episodes INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (request_id, season_number)
);
CREATE TABLE IF NOT EXISTS orchestrator_actions (
  action_key TEXT PRIMARY KEY,
  action_type TEXT NOT NULL,
  scope_json TEXT NOT NULL,
  status TEXT NOT NULL,
  command_id INTEGER,
  reason TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
"""


def redact(value: Any, key: str = "") -> Any:
    if key.lower() in SENSITIVE:
        return "[redacted]"
    if isinstance(value, dict):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def canonical(value: dict[str, Any]) -> str:
    return json.dumps(redact(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class Http:
    def __init__(self) -> None:
        self.cookies = http.cookiejar.CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.cookies))

    def request(self, url: str, *, headers: dict[str, str] | None = None,
                form: dict[str, str] | None = None, json_response: bool = True,
                method: str | None = None, body: bytes | None = None) -> Any:
        if form is not None and body is not None:
            raise ValueError("form and body are mutually exclusive")
        payload = urlencode(form).encode() if form is not None else body
        request = Request(url, data=payload, headers=headers or {}, method=method)
        if form is not None:
            request.add_header("Content-Type", "application/x-www-form-urlencoded")
        with self.opener.open(request, timeout=20) as response:
            payload = response.read()
        return json.loads(payload) if json_response and payload else payload


def url(base: str, path: str) -> str:
    return base.rstrip("/") + path


def enabled(name: str) -> bool:
    return bool(os.environ.get(name))


def arr_queue(client: Http, service: str, base: str, api_key: str) -> list[tuple[str, dict[str, Any]]]:
    data = client.request(url(base, "/api/v3/queue?page=1&pageSize=1000&includeUnknownSeriesItems=true"),
                          headers={"X-Api-Key": api_key})
    records = data.get("records", []) if isinstance(data, dict) else []
    output = []
    for item in records:
        episode = item.get("episode") if isinstance(item.get("episode"), dict) else None
        if episode is None and isinstance(item.get("episodes"), list):
            episode = next((value for value in item["episodes"] if isinstance(value, dict)), None)
        episode_ids = set()
        if isinstance(item.get("episodeId"), int):
            episode_ids.add(item["episodeId"])
        if isinstance(episode, dict) and isinstance(episode.get("id"), int):
            episode_ids.add(episode["id"])
        if isinstance(item.get("episodes"), list):
            episode_ids.update(
                value["id"] for value in item["episodes"]
                if isinstance(value, dict) and isinstance(value.get("id"), int)
            )
        identity = str(item.get("id") or item.get("downloadId") or item.get("title"))
        output.append((identity, {
            "id": item.get("id"), "download_id": item.get("downloadId"),
            "title": item.get("title"), "status": item.get("status"),
            "tracked_download_state": item.get("trackedDownloadState"),
            "tracked_download_status": item.get("trackedDownloadStatus"),
            "timeleft": item.get("timeleft"), "size": item.get("size"),
            "sizeleft": item.get("sizeleft"), "error_message": item.get("errorMessage"),
            "status_messages": item.get("statusMessages"),
            "series": item.get("series", {}).get("title") if isinstance(item.get("series"), dict) else None,
            "movie": item.get("movie", {}).get("title") if isinstance(item.get("movie"), dict) else None,
            "series_id": item.get("seriesId"), "movie_id": item.get("movieId"),
            "episode_ids": sorted(episode_ids),
            "season_number": item.get("seasonNumber") if item.get("seasonNumber") is not None else (episode or {}).get("seasonNumber"),
        }))
    return output


def qbit_torrents(client: Http, base: str, username: str, password: str) -> list[tuple[str, dict[str, Any]]]:
    client.request(url(base, "/api/v2/auth/login"), form={"username": username, "password": password}, json_response=False)
    data = client.request(url(base, "/api/v2/torrents/info"))
    output = []
    for item in data if isinstance(data, list) else []:
        identity = str(item.get("hash") or item.get("name"))
        output.append((identity, {
            "hash": item.get("hash"), "name": item.get("name"), "state": item.get("state"),
            "progress": item.get("progress"), "dlspeed": item.get("dlspeed"), "upspeed": item.get("upspeed"),
            "size": item.get("size"), "downloaded": item.get("downloaded"),
            "eta": item.get("eta"), "num_seeds": item.get("num_seeds"), "num_leechs": item.get("num_leechs"),
            "availability": item.get("availability"), "added_on": item.get("added_on"),
            "completion_on": item.get("completion_on"), "category": item.get("category"),
            "tags": item.get("tags"), "content_path": item.get("content_path"),
        }))
    return output


def seerr_requests(client: Http, base: str, api_key: str) -> list[tuple[str, dict[str, Any]]]:
    # Seerr accepts pagination here but rejects the former `sort=updatedAt`
    # parameter with HTTP 400 on current releases.  Keep the default ordering:
    # this is an observer, not a request-management UI.
    data = client.request(url(base, "/api/v1/request?take=1000&skip=0"),
                          headers={"X-Api-Key": api_key})
    records = data.get("results", []) if isinstance(data, dict) else []
    output = []
    for item in records:
        media = item.get("media") if isinstance(item.get("media"), dict) else {}
        identity = str(item.get("id"))
        output.append((identity, {
            "id": item.get("id"), "status": item.get("status"), "type": item.get("type"),
            "created_at": item.get("createdAt"), "updated_at": item.get("updatedAt"),
            "is_4k": item.get("is4k"), "media_id": media.get("id"),
            "tmdb_id": media.get("tmdbId"), "tvdb_id": media.get("tvdbId"),
            "sonarr_series_id": media.get("externalServiceId") if item.get("type") == "tv" else None,
            "media_status": media.get("status"),
            "requested_seasons": sorted({season.get("seasonNumber") for season in item.get("seasons", []) if isinstance(season, dict) and isinstance(season.get("seasonNumber"), int)}),
            "requested_season_statuses": {
                season["seasonNumber"]: season.get("status")
                for season in item.get("seasons", [])
                if isinstance(season, dict) and isinstance(season.get("seasonNumber"), int)
            },
            "title": media.get("title") or media.get("name"),
        }))
    return output


class Store:
    def __init__(self, database: Path) -> None:
        database.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(database)
        self.connection.executescript(SCHEMA)

    def record(self, source: str, entity_key: str, state: dict[str, Any]) -> str:
        now = int(time.time())
        after = canonical(state)
        state_hash = hashlib.sha256(after.encode()).hexdigest()
        old = self.connection.execute("SELECT state_hash,state_json FROM entities WHERE source=? AND entity_key=?",
                                      (source, entity_key)).fetchone()
        if old is None:
            kind, before = "observed", None
            self.connection.execute("INSERT INTO entities VALUES(?,?,?,?,?,?)",
                                    (source, entity_key, now, now, state_hash, after))
        elif old[0] != state_hash:
            kind, before = "changed", old[1]
            self.connection.execute("UPDATE entities SET last_seen_at=?,state_hash=?,state_json=? WHERE source=? AND entity_key=?",
                                    (now, state_hash, after, source, entity_key))
        else:
            self.connection.execute("UPDATE entities SET last_seen_at=? WHERE source=? AND entity_key=?",
                                    (now, source, entity_key))
            return "unchanged"
        self.connection.execute("INSERT INTO events(occurred_at,source,entity_key,event_type,before_json,after_json) VALUES(?,?,?,?,?,?)",
                                (now, source, entity_key, kind, before, after))
        return kind

    def close(self) -> None:
        self.connection.commit()
        self.connection.close()

    def urgency_for(self, title: str | None) -> str:
        if not title:
            return "normal"
        key = title.casefold().strip()
        row = self.connection.execute("SELECT urgency FROM policies WHERE title_key=?", (key,)).fetchone()
        return row[0] if row else "normal"

    def observe_download(self, infohash: str, torrent: dict[str, Any]) -> tuple[int, int]:
        """Measure consecutive no-progress samples and their real duration."""
        now = int(time.time())
        progress = float(torrent.get("progress") or 0)
        speed = int(torrent.get("dlspeed") or 0)
        row = self.connection.execute(
            "SELECT last_progress,zero_speed_samples,first_zero_speed_at FROM download_observations WHERE infohash=?",
            (infohash,),
        ).fetchone()
        stalled = progress < 1 and speed == 0
        if not stalled:
            samples, first = 0, None
        elif row is None or progress > row[0]:
            samples, first = 1, now
        else:
            samples, first = row[1] + 1, row[2]
        self.connection.execute(
            """INSERT INTO download_observations VALUES(?,?,?,?,?)
            ON CONFLICT(infohash) DO UPDATE SET last_progress=excluded.last_progress,
              zero_speed_samples=excluded.zero_speed_samples,first_zero_speed_at=excluded.first_zero_speed_at,
              last_seen_at=excluded.last_seen_at""",
            (infohash, progress, samples, first, now),
        )
        stalled_seconds = max(0, now - int(first)) if first is not None else 0
        return samples, stalled_seconds

    def candidate_hint(self, title: str | None) -> str | None:
        """Return a local registry hint, never a download instruction."""
        if not title:
            return None
        try:
            registry = Path(self.connection.execute("PRAGMA database_list").fetchone()[2]).parent / "candidates.sqlite3"
            if not registry.exists():
                return None
            with sqlite3.connect(registry) as candidates:
                row = candidates.execute(
                    "SELECT title,seeders FROM results WHERE lower(title) LIKE ? ORDER BY COALESCE(seeders,0) DESC,last_seen_at DESC LIMIT 1",
                    (f"%{title.casefold()}%",),
                ).fetchone()
            return f"{row[0]} ({row[1] or 0} seeders)" if row else None
        except sqlite3.Error:
            return None

    def decide(self, source: str, entity_key: str, state: dict[str, Any], torrent: dict[str, Any] | None,
               stalled_samples: int = 0, stalled_seconds: int = 0,
               minimum_stall_seconds: int = 10 * 60) -> str:
        """Persist a non-destructive policy decision for a queued acquisition."""
        title = state.get("series") or state.get("movie") or state.get("title")
        urgency = self.urgency_for(title)
        hour = datetime.now().astimezone().hour
        is_night = hour >= 22 or hour < 6
        eta = torrent.get("eta") if torrent else None
        eta = eta if isinstance(eta, int) and 0 <= eta < 8_640_000 else None
        progress = torrent.get("progress") if torrent else None
        speed = torrent.get("dlspeed") if torrent else None
        hint = self.candidate_hint(title)

        if not torrent:
            decision, reason = "awaiting_download", "A fila ainda não está associada a um torrent observável."
        elif progress == 1:
            decision, reason = "awaiting_import", "O torrent concluiu; aguardar importação pelo Library owner."
        elif urgency == "urgent" and eta is not None and eta <= 2 * 3600:
            decision, reason = "continue", "Urgente com ETA de até duas horas."
        elif urgency == "urgent":
            decision, reason = "seek_replacement", "Urgente sem ETA de até duas horas."
        elif eta is not None and eta <= 2 * 3600:
            decision, reason = "continue", "ETA de até duas horas."
        elif is_night and eta is not None and eta <= 10 * 3600:
            decision, reason = "continue_overnight", "Janela noturna e ETA de até dez horas."
        elif eta is not None and eta > 10 * 3600:
            decision, reason = "seek_replacement", "ETA acima de dez horas."
        elif not is_night and eta is not None and eta > 2 * 3600:
            decision, reason = "seek_replacement", "Fora da janela noturna e ETA acima de duas horas."
        elif speed == 0 and stalled_samples >= 2 and stalled_seconds >= minimum_stall_seconds:
            minutes = stalled_seconds // 60
            decision, reason = "seek_replacement", f"Sem velocidade e sem progresso por pelo menos {minutes} minutos ({stalled_samples} amostras)."
        elif speed == 0:
            decision, reason = "observe_stalled", "Sem velocidade; aguardar mais uma amostra antes de procurar substituto."
        else:
            decision, reason = "observe", "ETA ainda não é confiável para uma decisão."

        now = int(time.time())
        previous = self.connection.execute("SELECT decision,reason,eta_seconds,candidate_hint FROM decisions WHERE source=? AND entity_key=?",
                                           (source, entity_key)).fetchone()
        current = (decision, reason, eta, hint)
        self.connection.execute(
            """INSERT INTO decisions VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(source,entity_key) DO UPDATE SET evaluated_at=excluded.evaluated_at,
              urgency=excluded.urgency,decision=excluded.decision,reason=excluded.reason,
              eta_seconds=excluded.eta_seconds,candidate_hint=excluded.candidate_hint""",
            (source, entity_key, now, urgency, decision, reason, eta, hint),
        )
        if previous != current:
            self.connection.execute("INSERT INTO decision_events(occurred_at,source,entity_key,decision,reason,eta_seconds,candidate_hint) VALUES(?,?,?,?,?,?,?)",
                                    (now, source, entity_key, decision, reason, eta, hint))
        return decision

    def can_replace(self, source: str, owner_id: int, cooldown_seconds: int) -> bool:
        row = self.connection.execute("SELECT acted_at FROM replacement_actions WHERE source=? AND owner_id=?",
                                      (source, owner_id)).fetchone()
        return row is None or int(time.time()) - row[0] >= cooldown_seconds

    def replacement_done(self, source: str, owner_id: int, queue_ids: list[int]) -> None:
        self.connection.execute(
            """INSERT INTO replacement_actions VALUES(?,?,?,?)
            ON CONFLICT(source,owner_id) DO UPDATE SET acted_at=excluded.acted_at,queue_ids_json=excluded.queue_ids_json""",
            (source, owner_id, int(time.time()), json.dumps(queue_ids)),
        )

    def can_replace_scope(self, source: str, scope_key: str, cooldown_seconds: int) -> bool:
        row = self.connection.execute("SELECT acted_at FROM replacement_action_scopes WHERE source=? AND scope_key=?",
                                      (source, scope_key)).fetchone()
        return row is None or int(time.time()) - row[0] >= cooldown_seconds

    def replacement_scope_done(self, source: str, scope_key: str, queue_ids: list[int]) -> None:
        self.connection.execute(
            """INSERT INTO replacement_action_scopes VALUES(?,?,?,?)
            ON CONFLICT(source,scope_key) DO UPDATE SET acted_at=excluded.acted_at,queue_ids_json=excluded.queue_ids_json""",
            (source, scope_key, int(time.time()), json.dumps(queue_ids)),
        )

    def scope_status(self, request_id: int, series_id: int, season: int, state: str, missing: int, queued: int) -> None:
        self.connection.execute(
            """INSERT INTO requested_scope_status VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(request_id,season_number) DO UPDATE SET series_id=excluded.series_id,state=excluded.state,
              missing_episodes=excluded.missing_episodes,queue_episodes=excluded.queue_episodes,updated_at=excluded.updated_at""",
            (request_id, series_id, season, state, missing, queued, int(time.time())),
        )

    def previous_plan(self, request_id: int, season: int) -> PreviousPlan | None:
        row = self.connection.execute(
            """SELECT lifecycle_state,action,next_action_at,attempt_count,last_action_at
               FROM scope_reconciliation WHERE request_id=? AND season_number=?""",
            (request_id, season),
        ).fetchone()
        return PreviousPlan(*row) if row else None

    def reconciliation_plan(self, facts: ScopeFacts, plan) -> None:
        self.connection.execute(
            """INSERT INTO scope_reconciliation
               (request_id,series_id,season_number,lifecycle_state,action,next_action_at,
                reason,attempt_count,last_action_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(request_id,season_number) DO UPDATE SET
                 series_id=excluded.series_id,lifecycle_state=excluded.lifecycle_state,
                 action=excluded.action,next_action_at=excluded.next_action_at,
                 reason=excluded.reason,attempt_count=excluded.attempt_count,
                 updated_at=excluded.updated_at""",
            (facts.request_id, facts.series_id, facts.season_number,
             plan.lifecycle_state, plan.action, plan.next_action_at, plan.reason,
             plan.attempt_count,
             self.previous_plan(facts.request_id, facts.season_number).last_action_at
             if self.previous_plan(facts.request_id, facts.season_number) else None,
             int(time.time())),
        )

    def scope_verification(self, facts: ScopeFacts, imported: int) -> None:
        """Persist the import/language gates that justified the current plan."""
        self.connection.execute(
            """INSERT INTO scope_verification VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(request_id,season_number) DO UPDATE SET
              series_id=excluded.series_id,imported_episodes=excluded.imported_episodes,
              missing_episodes=excluded.missing_episodes,
              language_pending_episodes=excluded.language_pending_episodes,
              language_unscanned_episodes=excluded.language_unscanned_episodes,
              updated_at=excluded.updated_at""",
            (facts.request_id, facts.series_id, facts.season_number, imported,
             facts.missing_episodes, facts.language_pending_episodes,
             facts.language_unscanned_episodes, int(time.time())),
        )


def language_gate_counts(database: Path, episode_ids: set[int]) -> tuple[int, int]:
    """Return (pending, unscanned) language-policy counts for imported files.

    The subtitle/dub worker owns remediation.  This observer only reads its
    durable per-episode state, making the acquisition plan wait for the same
    policy without starting unscoped subtitle searches from this container.
    """
    if not episode_ids:
        return 0, 0
    audit_db = database.parent / "subtitle-orchestrator.sqlite3"
    if not audit_db.exists():
        return 0, len(episode_ids)
    try:
        with sqlite3.connect(f"file:{audit_db}?mode=ro", uri=True) as audit:
            rows = audit.execute(
                "SELECT episode_id, decision_state FROM episode_state WHERE episode_id IN (%s)"
                % ",".join("?" for _ in episode_ids),
                tuple(sorted(episode_ids)),
            ).fetchall()
    except sqlite3.Error as error:
        logging.warning("language audit unavailable: %s", error)
        return 0, len(episode_ids)
    states = {int(episode_id): str(state) for episode_id, state in rows}
    unscanned = len(episode_ids - states.keys())
    ready = {"dubbed", "pt_subtitled"}
    pending = sum(1 for state in states.values() if state not in ready)
    return pending, unscanned


def probe(name: str, fetch: Callable[[], list[tuple[str, dict[str, Any]]]], store: Store) -> tuple[int, int]:
    try:
        records = fetch()
    except (HTTPError, URLError, OSError, ValueError) as error:
        logging.warning("%s unavailable: %s", name, error)
        return 0, 0
    changes = sum(store.record(name, key, state) != "unchanged" for key, state in records)
    return len(records), changes


def cycle(database: Path) -> None:
    client = Http()
    store = Store(database)
    checks: list[tuple[str, Callable[[], list[tuple[str, dict[str, Any]]]]]] = []
    if enabled("JELLYSEERR_API_KEY"):
        checks.append(("seerr", lambda: seerr_requests(client, os.environ.get("SEERR_URL", "http://jellyseerr:5055"), os.environ["JELLYSEERR_API_KEY"])))
    if enabled("SONARR_API_KEY"):
        checks.append(("sonarr_queue", lambda: arr_queue(client, "sonarr", os.environ.get("SONARR_URL", "http://sonarr:8989"), os.environ["SONARR_API_KEY"])))
    if os.environ.get("RADARR_ENABLED", "false").lower() == "true" and enabled("RADARR_API_KEY"):
        checks.append(("radarr_queue", lambda: arr_queue(client, "radarr", os.environ.get("RADARR_URL", "http://radarr:7878"), os.environ["RADARR_API_KEY"])))
    if enabled("QBITTORRENT_USER") and enabled("QBITTORRENT_PASSWORD"):
        checks.append(("qbittorrent", lambda: qbit_torrents(client, os.environ.get("QBITTORRENT_URL", "http://qbittorrent:8080"), os.environ["QBITTORRENT_USER"], os.environ["QBITTORRENT_PASSWORD"])))
    observed: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    summary: dict[str, dict[str, int]] = {}
    for name, fetch in checks:
        try:
            records = fetch()
        except (HTTPError, URLError, OSError, ValueError) as error:
            logging.warning("%s unavailable: %s", name, error)
            summary[name] = {"records": 0, "changes": 0}
            continue
        observed[name] = records
        summary[name] = {"records": len(records), "changes": sum(store.record(name, key, state) != "unchanged" for key, state in records)}
    torrents = {key.lower(): state for key, state in observed.get("qbittorrent", [])}
    stalled = {infohash: store.observe_download(infohash, state) for infohash, state in torrents.items()}
    minimum_stall_seconds = int(os.environ.get("DEAD_TORRENT_MIN_STALL_SECONDS", "600"))
    requested_scopes: dict[int, set[int]] = {}
    for _, request in observed.get("seerr", []):
        series_id = request.get("sonarr_series_id")
        seasons = request.get("requested_seasons")
        if isinstance(series_id, int) and isinstance(seasons, list):
            requested_scopes.setdefault(series_id, set()).update(
                season for season in seasons
                if isinstance(season, int) and not scope_is_available(request, season)
            )
    scope_summary: dict[str, int] = {}
    scope_facts: list[ScopeFacts] = []
    stale_satisfied_queue_ids: set[int] = set()
    if os.environ.get("SONARR_API_KEY"):
        queue_by_scope: dict[tuple[int, int], list[dict[str, Any]]] = {}
        for _, item in observed.get("sonarr_queue", []):
            if isinstance(item.get("series_id"), int) and isinstance(item.get("season_number"), int):
                queue_by_scope.setdefault((item["series_id"], item["season_number"]), []).append(item)
        for request_id, request in observed.get("seerr", []):
            series_id = request.get("sonarr_series_id")
            seasons = request.get("requested_seasons")
            if not isinstance(series_id, int) or not isinstance(seasons, list):
                continue
            try:
                episodes = client.request(url(os.environ.get("SONARR_URL", "http://sonarr:8989"), f"/api/v3/episode?seriesId={series_id}"), headers={"X-Api-Key": os.environ["SONARR_API_KEY"]})
            except (HTTPError, URLError, OSError, ValueError):
                continue
            for season in seasons:
                if not isinstance(season, int): continue
                missing = sum(1 for e in episodes if e.get("seasonNumber") == season and e.get("monitored") and not e.get("hasFile"))
                available_episode_ids = {
                    e["id"] for e in episodes
                    if e.get("seasonNumber") == season and e.get("hasFile") and isinstance(e.get("id"), int)
                }
                language_pending, language_unscanned = language_gate_counts(
                    database, available_episode_ids,
                )
                queued_items = queue_by_scope.get((series_id, season), [])
                stale_satisfied_queue_ids.update(satisfied_queue_ids(
                    missing, queued_items, torrents,
                    available_episode_ids=available_episode_ids,
                ))
                satisfied_by_seerr = scope_is_available(request, season)
                if missing == 0 and language_unscanned: state = "language_verifying"
                elif missing == 0 and language_pending: state = "language_remediating"
                elif missing == 0: state = "complete"
                elif any(i.get("tracked_download_state") == "importBlocked" for i in queued_items): state = "import_blocked"
                elif queued_items: state = "downloading"
                else: state = "pending"
                store.scope_status(int(request_id), series_id, season, state, missing, len(queued_items))
                scope_facts.append(ScopeFacts(
                    request_id=int(request_id), series_id=series_id, season_number=season,
                    missing_episodes=missing, queue_episodes=len(queued_items),
                    import_blocked=state == "import_blocked",
                    satisfied_by_seerr=satisfied_by_seerr,
                    language_pending_episodes=language_pending,
                    language_unscanned_episodes=language_unscanned,
                ))
                store.scope_verification(scope_facts[-1], len(available_episode_ids))
                scope_summary[state] = scope_summary.get(state, 0) + 1
    if scope_summary: summary["requested_scopes"] = scope_summary
    plan_summary: dict[str, int] = {}
    now = int(time.time())
    for facts in scope_facts:
        previous = store.previous_plan(facts.request_id, facts.season_number)
        plan = reconcile_scope(facts, previous, now)
        store.reconciliation_plan(facts, plan)
        plan_summary[plan.lifecycle_state] = plan_summary.get(plan.lifecycle_state, 0) + 1
    if plan_summary:
        summary["reconciliation_plans"] = plan_summary
    if os.environ.get("SONARR_API_KEY"):
        command_updates: dict[str, int] = {}
        for action_key, command_id in store.connection.execute(
            "SELECT action_key,command_id FROM orchestrator_actions WHERE status='submitted' AND command_id IS NOT NULL"
        ).fetchall():
            try:
                command = client.request(
                    url(os.environ.get("SONARR_URL", "http://sonarr:8989"), f"/api/v3/command/{command_id}"),
                    headers={"X-Api-Key": os.environ["SONARR_API_KEY"]},
                )
            except (HTTPError, URLError, OSError, ValueError):
                continue
            status = str(command.get("status") or "").casefold()
            if status not in {"completed", "failed", "aborted", "cancelled"}:
                continue
            reason = command.get("exception") or command.get("message")
            store.connection.execute(
                "UPDATE orchestrator_actions SET status=?,reason=?,updated_at=? WHERE action_key=?",
                (status, reason, int(time.time()), action_key),
            )
            command_updates[status] = command_updates.get(status, 0) + 1
        if command_updates:
            summary["command_updates"] = command_updates
    decisions: dict[str, int] = {}
    replacements: dict[str, dict[tuple[int, int], list[int]]] = {}
    for source in ("sonarr_queue", "radarr_queue"):
        for key, state in observed.get(source, []):
            download_id = state.get("download_id")
            infohash = str(download_id).lower() if download_id else None
            current_torrent = torrents.get(infohash) if infohash else None
            stalled_samples, stalled_seconds = stalled.get(infohash, (0, 0)) if infohash else (0, 0)
            outcome = store.decide(source, key, state, current_torrent,
                                   stalled_samples, stalled_seconds, minimum_stall_seconds)
            decisions[outcome] = decisions.get(outcome, 0) + 1
            owner = state.get("series_id") if source == "sonarr_queue" else state.get("movie_id")
            # qBittorrent may fetch the torrent metadata/first piece and report
            # a tiny non-zero progress (e.g. 16 KiB of a 365 MiB episode). That
            # is not user media worth preserving, so treat up to 1 MiB as empty.
            is_dead_near_empty = current_torrent is not None and dead_near_empty_candidate(
                current_torrent,
                stalled_samples=stalled_samples,
                stalled_seconds=stalled_seconds,
                minimum_stall_seconds=minimum_stall_seconds,
            )
            season = state.get("season_number")
            is_requested_scope = source != "sonarr_queue" or (isinstance(owner, int) and isinstance(season, int) and season in requested_scopes.get(owner, set()))
            if outcome == "seek_replacement" and is_dead_near_empty and is_requested_scope and isinstance(owner, int) and isinstance(season, int) and isinstance(state.get("id"), int):
                replacements.setdefault(source, {}).setdefault((owner, season), []).append(state["id"])
    if decisions:
        summary["decisions"] = decisions
    if os.environ.get("MEDIA_ORCHESTRATOR_APPLY", "false").lower() == "true":
        cooldown = int(os.environ.get("REPLACEMENT_COOLDOWN_SECONDS", "21600"))
        bases = {"sonarr_queue": os.environ.get("SONARR_URL", "http://sonarr:8989"),
                 "radarr_queue": os.environ.get("RADARR_URL", "http://radarr:7878")}
        keys = {"sonarr_queue": os.environ.get("SONARR_API_KEY"), "radarr_queue": os.environ.get("RADARR_API_KEY")}
        applied = 0
        if stale_satisfied_queue_ids and keys["sonarr_queue"]:
            ids = sorted(stale_satisfied_queue_ids)
            action_key = "detach-satisfied:" + hashlib.sha256(json.dumps(ids).encode()).hexdigest()
            now = int(time.time())
            store.connection.execute(
                "INSERT OR IGNORE INTO orchestrator_actions VALUES(?,?,?,?,?,?,?,?)",
                (action_key, "detach_satisfied_queue", json.dumps({"queue_ids": ids}),
                 "submitting", None,
                 "Escopo já importado; destacar aviso residual sem remover o torrent.", now, now),
            )
            endpoint = url(
                bases["sonarr_queue"],
                "/api/v3/queue/bulk?removeFromClient=false&blocklist=false&skipRedownload=true&changeCategory=false",
            )
            try:
                client.request(
                    endpoint, method="DELETE", json_response=False,
                    body=json.dumps({"ids": ids}).encode(),
                    headers={"X-Api-Key": keys["sonarr_queue"], "Content-Type": "application/json"},
                )
            except (HTTPError, URLError, OSError, ValueError) as error:
                store.connection.execute(
                    "UPDATE orchestrator_actions SET status='failed',reason=?,updated_at=? WHERE action_key=?",
                    (str(error), int(time.time()), action_key),
                )
                logging.error("detach satisfied Sonarr queue failed: %s", error)
            else:
                store.connection.execute(
                    "UPDATE orchestrator_actions SET status='completed',updated_at=? WHERE action_key=?",
                    (int(time.time()), action_key),
                )
                summary["satisfied_queue"] = {"detached": len(ids)}
        for source, groups in replacements.items():
            api_key = keys[source]
            if not api_key:
                continue
            for (owner_id, season), ids in groups.items():
                # Radarr is deliberately not automated until its movie-search
                # command and library policy have been validated separately.
                if source != "sonarr_queue":
                    continue
                scope_key = f"series:{owner_id}:season:{season}"
                endpoint = url(bases[source], "/api/v3/queue/bulk?removeFromClient=true&blocklist=true&skipRedownload=true&changeCategory=false")
                try:
                    client.request(endpoint, method="DELETE", json_response=False,
                                   body=json.dumps({"ids": ids}).encode(),
                                   headers={"X-Api-Key": api_key, "Content-Type": "application/json"})
                except (HTTPError, URLError, OSError, ValueError) as error:
                    logging.error("replacement for %s/%s failed: %s", source, scope_key, error)
                    continue
                # A dead candidate is always blocklisted. Search fan-out is
                # deliberately not triggered here: the durable reconciliation
                # plan will try local artifacts and cache before one episode.
                applied += 1
        if applied:
            summary["replacement_actions"] = {"applied": applied}
    store.close()
    logging.info("cycle %s", json.dumps(summary, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("/registry/acquisitions.sqlite3"))
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    while True:
        cycle(args.database)
        if args.once:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
