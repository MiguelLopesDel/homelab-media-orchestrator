#!/usr/bin/env python3
"""Execute at most one due reconciliation action per invocation."""

from __future__ import annotations

import json
import argparse
import os
import sqlite3
import subprocess
import sys
import time
import re
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from media_orchestrator_core import release_title_starts_with_series


DB = Path(os.environ.get("ACQUISITIONS_DB", "/registry/acquisitions.sqlite3"))
GLOBAL_SEARCH_INTERVAL = int(os.environ.get("GLOBAL_SEARCH_INTERVAL_SECONDS", "1800"))


def sonarr(path: str, *, method: str = "GET", body=None):
    payload = json.dumps(body).encode() if body is not None else None
    request = Request(
        os.environ.get("SONARR_URL", "http://127.0.0.1:8989").rstrip("/") + path,
        data=payload, method=method,
        headers={"X-Api-Key": os.environ["SONARR_API_KEY"], "Content-Type": "application/json"},
    )
    with urlopen(request, timeout=120) as response:
        data = response.read()
    return json.loads(data) if data else None


def ensure_schema(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS orchestrator_budgets (
      budget_key TEXT PRIMARY KEY, last_at INTEGER NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS orchestrator_actions (
      action_key TEXT PRIMARY KEY, action_type TEXT NOT NULL, scope_json TEXT NOT NULL,
      status TEXT NOT NULL, command_id INTEGER, reason TEXT,
      created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)""")


def update_plan(db: sqlite3.Connection, request_id: int, season: int, *,
                lifecycle: str, action: str | None, next_at: int | None,
                reason: str, increment: bool = False) -> None:
    db.execute("""UPDATE scope_reconciliation SET lifecycle_state=?,action=?,next_action_at=?,
                  reason=?,attempt_count=attempt_count+?,last_action_at=?,updated_at=?
                  WHERE request_id=? AND season_number=?""",
               (lifecycle, action, next_at, reason, int(increment), int(time.time()),
                int(time.time()), request_id, season))


def due_scope(db: sqlite3.Connection):
    now = int(time.time())
    return db.execute("""SELECT request_id,series_id,season_number,attempt_count
                         FROM scope_reconciliation
                         WHERE action IN ('reconcile_scope','try_cached_candidate','search_one_episode')
                           AND next_action_at IS NOT NULL AND next_action_at<=?
                         ORDER BY CASE WHEN season_number=0 THEN 1 ELSE 0 END,
                                  COALESCE(last_action_at,0),attempt_count,request_id DESC
                         LIMIT 1""", (now,)).fetchone()


def missing_episode(series_id: int, season: int):
    episodes = sonarr(f"/api/v3/episode?seriesId={series_id}")
    missing = [episode for episode in episodes
               if episode.get("seasonNumber") == season
               and episode.get("monitored") and not episode.get("hasFile")]
    return sorted(missing, key=lambda item: item.get("episodeNumber", 0))[0] if missing else None


def active_search() -> bool:
    return any(command.get("status") in {"queued", "started"} and "Search" in str(command.get("name"))
               for command in sonarr("/api/v3/command"))


def normalized(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def title_starts_with_series(title: str, aliases: list[str]) -> bool:
    return release_title_starts_with_series(title, aliases)


def blocked_download_hashes(queue: list[dict], series_id: int, season: int) -> set[str]:
    """Return exact completed import-blocked hashes reported by Sonarr."""
    return {
        str(item.get("downloadId")).casefold()
        for item in queue
        if item.get("seriesId") == series_id
        and item.get("seasonNumber") == season
        and str(item.get("status") or "").casefold() == "completed"
        and str(item.get("trackedDownloadState") or "").casefold() == "importblocked"
        and item.get("downloadId")
    }


def try_local_artifact(series_id: int, season: int) -> tuple[str, str]:
    """Ask the conservative importer about one matching completed artifact."""
    try:
        from sonarr_import_reconciler import qbit_torrents
        series = sonarr(f"/api/v3/series/{series_id}")
        queue_data = sonarr("/api/v3/queue?page=1&pageSize=1000&includeUnknownSeriesItems=true")
        queue = queue_data.get("records", []) if isinstance(queue_data, dict) else []
        exact_hashes = blocked_download_hashes(queue, series_id, season)
        aliases = [series["title"]] + [item["title"] for item in series.get("alternateTitles", []) if item.get("title")]
        aliases = [normalized(alias) for alias in aliases if len(normalized(alias)) >= 6]
        matches = [torrent for torrent in qbit_torrents()
                   if float(torrent.get("progress") or 0) == 1
                   and (str(torrent.get("hash") or "").casefold() in exact_hashes
                        or any(alias in normalized(str(torrent.get("name", ""))) for alias in aliases))]
        matches.sort(key=lambda torrent: str(torrent.get("hash") or "").casefold() not in exact_hashes)
    except Exception as error:
        return "failed", f"Falha ao inspecionar artefatos locais: {error}"
    for torrent in matches:
        command = [sys.executable, "/app/sonarr_import_reconciler.py",
                   "--download-hash", torrent["hash"], "--apply"]
        completed = subprocess.run(command, text=True, capture_output=True, timeout=240)
        lines = [line for line in completed.stdout.splitlines() if line.startswith("{")]
        if not lines:
            continue
        preview = json.loads(lines[0])
        if int(preview.get("safe_files") or 0) > 0:
            return "submitted", f"Importação segura submetida para {preview['safe_files']} arquivo(s) locais."
    return "empty", "Nenhum artefato local concluído possui mapeamento seguro para este escopo."


def try_cache(series_id: int, season: int, episode: int) -> tuple[str, str]:
    command = [sys.executable, "/app/cached_candidate_adapter.py",
               "--series-id", str(series_id), "--season", str(season),
               "--episode", str(episode), "--apply"]
    completed = subprocess.run(command, text=True, capture_output=True, timeout=180)
    lines = [line for line in completed.stdout.splitlines() if line.startswith("{")]
    if not lines:
        return "failed", (completed.stderr or completed.stdout)[-1000:]
    preview = json.loads(lines[0])
    if not preview.get("candidates"):
        return "empty", "Nenhum candidato exato e não tentado no cache."
    result = json.loads(lines[-1]) if len(lines) > 1 else {}
    return result.get("result", "failed"), result.get("reason", "Resultado ausente.")


def controlled_search(db: sqlite3.Connection, request_id: int, series_id: int,
                      season: int, episode: dict, attempts: int) -> tuple[str, str]:
    now = int(time.time())
    row = db.execute("SELECT last_at FROM orchestrator_budgets WHERE budget_key='external_search'").fetchone()
    if row and now - row[0] < GLOBAL_SEARCH_INTERVAL:
        next_at = row[0] + GLOBAL_SEARCH_INTERVAL
        update_plan(db, request_id, season, lifecycle="search_cooldown", action="search_one_episode",
                    next_at=next_at, reason="Orçamento global de busca ainda em cooldown.")
        return "cooldown", f"Busca adiada até {next_at}."
    if active_search():
        update_plan(db, request_id, season, lifecycle="search_cooldown", action="search_one_episode",
                    next_at=now + 10 * 60, reason="O Sonarr já possui uma busca em execução.")
        return "busy", "O Sonarr já possui uma busca em execução."

    db.execute("""INSERT INTO orchestrator_budgets VALUES('external_search',?)
                  ON CONFLICT(budget_key) DO UPDATE SET last_at=excluded.last_at""", (now,))
    action_key = f"episode-search:{series_id}:{season}:{episode['id']}:{now}"
    scope = {"request_id": request_id, "series_id": series_id, "season": season,
             "episode_id": episode["id"]}
    db.execute("INSERT INTO orchestrator_actions VALUES(?,?,?,?,?,?,?,?)",
               (action_key, "episode_search", json.dumps(scope), "searching", None,
                "One interactive episode search.", now, now))
    db.commit()
    releases = sonarr("/api/v3/release?" + urlencode({"episodeId": episode["id"]}))
    series = sonarr(f"/api/v3/series/{series_id}")
    aliases = [series["title"]] + [item["title"] for item in series.get("alternateTitles", []) if item.get("title")]
    viable = [release for release in releases
              if release.get("approved") and release.get("downloadAllowed")
              and (release.get("seeders") is None or release.get("seeders") > 0)]
    viable = [release for release in viable
              if release.get("mappedSeriesId") == series_id
              and any(item.get("id") == episode["id"] for item in release.get("mappedEpisodeInfo") or [])
              and title_starts_with_series(str(release.get("title") or ""), aliases)]
    if viable:
        sonarr("/api/v3/release", method="POST", body=viable[0])
        status, reason = "grabbed", f"Sonarr aprovou 1 de {len(releases)} releases retornados."
        update_plan(db, request_id, season, lifecycle="downloading", action=None,
                    next_at=None, reason=reason, increment=True)
    else:
        delays = (1800, 7200, 21600, 86400)
        delay = delays[min(attempts, len(delays) - 1)]
        status, reason = "no_candidate", f"Nenhum dos {len(releases)} releases foi aprovado pelo Sonarr."
        update_plan(db, request_id, season, lifecycle="search_cooldown", action="search_one_episode",
                    next_at=now + delay, reason=reason, increment=True)
    db.execute("UPDATE orchestrator_actions SET status=?,reason=?,updated_at=? WHERE action_key=?",
               (status, reason, int(time.time()), action_key))
    return status, reason


def run_once() -> int:
    with sqlite3.connect(DB) as db:
        ensure_schema(db)
        scope = due_scope(db)
        if scope is None:
            print(json.dumps({"result": "idle"}))
            return 0
        request_id, series_id, season, attempts = scope
        episode = missing_episode(series_id, season)
        if episode is None:
            update_plan(db, request_id, season, lifecycle="complete", action=None,
                        next_at=None, reason="Nenhum episódio monitorado permanece ausente.")
            db.commit()
            print(json.dumps({"result": "complete", "request": request_id, "season": season}))
            return 0

        artifact_status, artifact_reason = try_local_artifact(series_id, season)
        if artifact_status == "submitted":
            update_plan(db, request_id, season, lifecycle="importing", action=None,
                        next_at=None, reason=artifact_reason, increment=True)
            result, reason = artifact_status, artifact_reason
        else:
            cache_status, reason = try_cache(series_id, season, episode["episodeNumber"])
            if cache_status == "grabbed":
                update_plan(db, request_id, season, lifecycle="downloading", action=None,
                            next_at=None, reason=reason, increment=True)
                result = cache_status
            elif cache_status in {"rejected", "failed"}:
                update_plan(db, request_id, season, lifecycle="retry_scheduled", action="try_cached_candidate",
                            next_at=int(time.time()) + 5 * 60, reason=reason, increment=True)
                result = cache_status
            else:
                result, reason = controlled_search(db, request_id, series_id, season, episode, attempts)
        db.commit()
        print(json.dumps({"result": result, "request": request_id, "series": series_id,
                          "season": season, "episode": episode["episodeNumber"], "reason": reason}, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=int, default=0)
    args = parser.parse_args()
    while True:
        result = run_once()
        if not args.interval:
            return result
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
