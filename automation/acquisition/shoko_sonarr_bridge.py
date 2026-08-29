#!/usr/bin/env python3
"""Import downloaded anime into Sonarr using Shoko's hash-based identification.

Sonarr's own importer derives season/episode from the release filename, which
fansub groups do not follow reliably.  Shoko identifies each file by its ED2K
hash against AniDB, so identity never depends on the filename.  This bridge
reads Shoko's identification and drives Sonarr's manual-import endpoint with
explicit episode IDs, bypassing Sonarr's parser entirely.

Nothing here moves or renames a file: Sonarr performs its normal import from
the torrent directory, exactly as it does for a release it parses on its own.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

# Shoko sees the torrent tree at this path; Sonarr sees the same files here.
SHOKO_TORRENT_MOUNT = os.environ.get("SHOKO_TORRENT_MOUNT", "/mnt/torrents")
SONARR_TORRENT_MOUNT = os.environ.get("SONARR_TORRENT_MOUNT", "/data/torrents")


def shoko(path: str):
    request = Request(
        os.environ.get("SHOKO_URL", "http://127.0.0.1:8111").rstrip("/") + path,
        headers={"apikey": os.environ["SHOKO_API_KEY"]},
    )
    with urlopen(request, timeout=120) as response:
        data = response.read()
    return json.loads(data) if data else None


def sonarr(path: str, *, method: str = "GET", body=None):
    payload = json.dumps(body).encode() if body is not None else None
    request = Request(
        os.environ.get("SONARR_URL", "http://127.0.0.1:8989").rstrip("/") + path,
        data=payload, method=method,
        headers={"X-Api-Key": os.environ["SONARR_API_KEY"],
                 "Content-Type": "application/json"},
    )
    with urlopen(request, timeout=120) as response:
        data = response.read()
    return json.loads(data) if data else None


def shoko_paged(path: str, page_size: int = 100):
    """Yield every entry of a paged Shoko collection."""
    page = 1
    while True:
        joiner = "&" if "?" in path else "?"
        chunk = shoko(f"{path}{joiner}page={page}&pageSize={page_size}")
        items = chunk.get("List", []) if isinstance(chunk, dict) else []
        yield from items
        if len(items) < page_size:
            return
        page += 1


def torrent_import_folders() -> list[int]:
    """Shoko import folders that hold the torrent tree."""
    return [
        folder["ID"] for folder in shoko("/api/v3/ImportFolder")
        if str(folder.get("Path", "")).rstrip("/").startswith(SHOKO_TORRENT_MOUNT)
    ]


def shoko_identified_files() -> dict[str, dict]:
    """Map absolute Sonarr-visible path -> Shoko cross-reference for one file.

    Files Shoko could not identify, or that resolve to more than one episode,
    are skipped: an ambiguous mapping must never drive an automatic import.
    """
    folders = {folder["ID"]: str(folder["Path"]).rstrip("/")
               for folder in shoko("/api/v3/ImportFolder")}
    output: dict[str, dict] = {}
    for folder_id in torrent_import_folders():
        base = folders[folder_id]
        for entry in shoko_paged(f"/api/v3/ImportFolder/{folder_id}/File?include=XRefs"):
            series_refs = entry.get("SeriesIDs") or []
            if len(series_refs) != 1:
                continue
            episode_refs = series_refs[0].get("EpisodeIDs") or []
            if len(episode_refs) != 1:
                continue
            # Sonarr hardlinks imports into the library, so one hash can carry
            # a location in several import folders.  Only the torrent-folder
            # location is a candidate for import.
            for location in entry.get("Locations") or []:
                if location.get("ImportFolderID") != folder_id:
                    continue
                relative = str(location.get("RelativePath") or "").lstrip("/")
                if not relative:
                    continue
                shoko_path = f"{base}/{relative}"
                sonarr_path = SONARR_TORRENT_MOUNT + shoko_path[len(SHOKO_TORRENT_MOUNT):]
                output[sonarr_path] = {
                    "shoko_file_id": entry["ID"],
                    "series": series_refs[0].get("SeriesID") or {},
                    "episode": episode_refs[0],
                }
    return output


def tmdb_episode_detail(episode_ref: dict) -> dict | None:
    """Resolve the single TMDB episode a Shoko cross-reference points at."""
    episodes = (episode_ref.get("TMDB") or {}).get("Episode") or []
    if len(episodes) != 1:
        return None
    detail = shoko(f"/api/v3/Tmdb/Episode/{episodes[0]}")
    return detail if isinstance(detail, dict) else None


def locate_slot(episode_ref: dict, sonarr_episodes: list[dict]) -> dict | None:
    """Find the Sonarr episode a file belongs to.

    Matching runs on the TVDB episode id, which is what Sonarr keys episodes
    by.  Season/episode numbers are only a fallback for regular seasons: the
    two databases number specials independently, so season 0 positions do not
    correspond at all (one database may number a recap where the other numbers
    a movie).
    """
    detail = tmdb_episode_detail(episode_ref)
    if detail is None:
        return None

    tvdb_id = detail.get("TvdbEpisodeID")
    if tvdb_id:
        return next((episode for episode in sonarr_episodes
                     if episode.get("tvdbId") == tvdb_id), None)

    season = detail.get("SeasonNumber")
    number = detail.get("EpisodeNumber")
    if season is None or number is None or int(season) == 0:
        return None
    return next((episode for episode in sonarr_episodes
                 if (episode["seasonNumber"], episode["episodeNumber"])
                 == (int(season), int(number))), None)


def sonarr_series_index() -> tuple[dict[int, dict], dict[int, dict]]:
    series = sonarr("/api/v3/series")
    return ({item["tmdbId"]: item for item in series if item.get("tmdbId")},
            {item["tvdbId"]: item for item in series if item.get("tvdbId")})


def resolve_target(reference: dict, by_tmdb: dict, by_tvdb: dict) -> dict | None:
    """Find the Sonarr series a Shoko cross-reference belongs to."""
    shows = (reference.get("series", {}).get("TMDB") or {}).get("Show") or []
    if len(shows) == 1 and shows[0] in by_tmdb:
        return by_tmdb[shows[0]]
    tvdb = reference.get("series", {}).get("TvDB") or []
    if len(tvdb) == 1 and tvdb[0] in by_tvdb:
        return by_tvdb[tvdb[0]]
    return None


def plan_imports(claimed: set[int] | None = None) -> list[dict]:
    """Build one unambiguous file -> Sonarr episode import per candidate.

    ``claimed`` carries episode IDs already submitted in an earlier run: a
    submitted import stays invisible to Sonarr's ``hasFile`` until its command
    finishes, so without it a slow import would be queued twice.
    """
    by_tmdb, by_tvdb = sonarr_series_index()
    episodes_cache: dict[int, list[dict]] = {}
    plan: list[dict] = []
    claimed = set(claimed or ())

    for path, reference in sorted(shoko_identified_files().items()):
        target = resolve_target(reference, by_tmdb, by_tvdb)
        if target is None:
            continue
        if target["id"] not in episodes_cache:
            episodes_cache[target["id"]] = sonarr(f"/api/v3/episode?seriesId={target['id']}")
        slot = locate_slot(reference["episode"], episodes_cache[target["id"]])
        if slot is None or slot.get("hasFile") or not slot.get("monitored"):
            continue

        # Shoko's own TvDB episode id, when it still carries one, must agree.
        tvdb_episode = reference["episode"].get("TvDB") or []
        if len(tvdb_episode) == 1 and tvdb_episode[0] != slot.get("tvdbId"):
            continue

        if slot["id"] in claimed:
            continue
        claimed.add(slot["id"])
        plan.append({
            "path": path,
            "series_id": target["id"],
            "series_title": target["title"],
            "season": slot["seasonNumber"],
            "episode_number": slot["episodeNumber"],
            "episode_id": slot["id"],
            "shoko_file_id": reference["shoko_file_id"],
        })
    return plan


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS orchestrator_actions (
      action_key TEXT PRIMARY KEY, action_type TEXT NOT NULL, scope_json TEXT NOT NULL,
      status TEXT NOT NULL, command_id INTEGER, reason TEXT,
      created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)""")


def claimed_episode_ids(connection: sqlite3.Connection) -> set[int]:
    """Episode IDs this bridge has already handed to Sonarr."""
    ensure_schema(connection)
    rows = connection.execute(
        "SELECT scope_json FROM orchestrator_actions "
        "WHERE action_type='shoko_bridge_import' AND status IN ('submitting','submitted')"
    ).fetchall()
    output: set[int] = set()
    for (scope_json,) in rows:
        try:
            episode_id = json.loads(scope_json).get("episode_id")
        except json.JSONDecodeError:
            continue
        if isinstance(episode_id, int):
            output.add(episode_id)
    return output


def folder_preview(folder: str, cache: dict[str, list]) -> list:
    """Sonarr's manual-import listing for one folder, fetched at most once.

    A torrent category folder can hold hundreds of files, so this must not be
    re-queried per candidate.
    """
    if folder not in cache:
        cache[folder] = sonarr("/api/v3/manualimport?" + "&".join([
            "folder=" + quote(folder), "filterExistingFiles=false",
        ])) or []
    return cache[folder]


def submit(connection: sqlite3.Connection, item: dict, cache: dict[str, list]) -> str:
    scope = {"episode_id": item["episode_id"], "path": item["path"]}
    action_key = "shoko-bridge:" + hashlib.sha256(
        json.dumps(scope, sort_keys=True).encode()).hexdigest()
    ensure_schema(connection)
    if connection.execute("SELECT 1 FROM orchestrator_actions WHERE action_key=?",
                          (action_key,)).fetchone():
        return "duplicate"

    listing = folder_preview(str(Path(item["path"]).parent), cache)
    preview = next((entry for entry in listing
                    if entry.get("path") == item["path"]), None)
    if preview is None:
        return "no_preview"

    now = int(time.time())
    connection.execute("INSERT INTO orchestrator_actions VALUES(?,?,?,?,?,?,?,?)",
                       (action_key, "shoko_bridge_import", json.dumps(scope),
                        "submitting", None,
                        "Shoko identified this file by hash; episode id supplied explicitly.",
                        now, now))
    connection.commit()
    try:
        response = sonarr("/api/v3/command", method="POST", body={
            "name": "ManualImport",
            "importMode": "auto",
            "files": [{
                "path": item["path"],
                "folderName": Path(item["path"]).parent.name,
                "seriesId": item["series_id"],
                "episodeIds": [item["episode_id"]],
                "seasonNumber": item["season"],
                "quality": preview["quality"],
                "languages": preview.get("languages") or [],
                "releaseGroup": preview.get("releaseGroup"),
                "indexerFlags": preview.get("indexerFlags") or 0,
            }],
        })
    except HTTPError as error:
        connection.execute(
            "UPDATE orchestrator_actions SET status='failed',reason=?,updated_at=? WHERE action_key=?",
            (f"HTTP {error.code}", int(time.time()), action_key))
        connection.commit()
        return "failed"
    connection.execute(
        "UPDATE orchestrator_actions SET status='submitted',command_id=?,updated_at=? WHERE action_key=?",
        (response.get("id"), int(time.time()), action_key))
    connection.commit()
    return "submitted"


def run_once(arguments) -> int:
    with sqlite3.connect(arguments.database) as connection:
        claimed = claimed_episode_ids(connection)
    plan = plan_imports(claimed)
    if arguments.limit:
        plan = plan[:arguments.limit]
    print(json.dumps({"planned": len(plan)}, ensure_ascii=False), flush=True)
    for item in plan:
        print(json.dumps({
            "series": item["series_title"],
            "season": item["season"],
            "episode": item["episode_number"],
            "file": Path(item["path"]).name,
        }, ensure_ascii=False), flush=True)
    if not arguments.apply:
        return 0

    cache: dict[str, list] = {}
    with sqlite3.connect(arguments.database) as connection:
        for item in plan:
            print(json.dumps({"result": submit(connection, item, cache),
                              "episode_id": item["episode_id"]},
                             ensure_ascii=False), flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path,
                        default=Path("/registry/acquisitions.sqlite3"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--interval", type=int, default=0)
    arguments = parser.parse_args()

    while True:
        try:
            result = run_once(arguments)
        except Exception as error:  # a transient API failure must not end the loop
            print(json.dumps({"result": "error", "reason": str(error)[:300]},
                             ensure_ascii=False), flush=True)
            result = 1
        if not arguments.interval:
            return result
        time.sleep(arguments.interval)


if __name__ == "__main__":
    sys.exit(main())
