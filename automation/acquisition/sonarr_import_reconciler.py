#!/usr/bin/env python3
"""Recover safe, already-downloaded Sonarr imports without overwriting files."""

from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

from media_orchestrator_core import select_safe_imports


MEDIA_EXTENSIONS = {
    ".3gp", ".asf", ".avi", ".divx", ".flv", ".m2ts", ".m4v", ".mkv",
    ".mov", ".mp4", ".mpeg", ".mpg", ".mts", ".ogm", ".ogv", ".ts",
    ".vob", ".webm", ".wmv",
}


def artifact_folder(content_path: Path) -> Path:
    """Return the folder Sonarr should preview for a qBittorrent artifact."""
    return content_path.parent if content_path.suffix.casefold() in MEDIA_EXTENSIONS else content_path


def strict_episode_key(path: str) -> tuple[int, int] | None:
    """Parse one, and only one, SxxExx marker from a release filename."""
    name = Path(path).name
    matches = re.findall(r"(?i)(?:^|[ ._-])S(\d{1,2})E(\d{1,3})(?=$|[ ._-])", name)
    if len(matches) != 1:
        return None
    remainder = re.sub(r"(?i)(?:^|[ ._-])S\d{1,2}E\d{1,3}(?=$|[ ._-])", " ", name)
    if re.search(r"(?i)(?:^|[ ._-])E\d{1,3}(?=$|[ ._-])", remainder):
        return None
    season, episode = matches[0]
    return int(season), int(episode)


def sonarr(path: str, *, method: str = "GET", body: object | None = None):
    payload = json.dumps(body).encode() if body is not None else None
    request = Request(
        os.environ.get("SONARR_URL", "http://127.0.0.1:8989").rstrip("/") + path,
        data=payload,
        method=method,
        headers={"X-Api-Key": os.environ["SONARR_API_KEY"], "Content-Type": "application/json"},
    )
    with urlopen(request, timeout=90) as response:
        data = response.read()
    return json.loads(data) if data else None


def qbit_torrents() -> list[dict]:
    base = os.environ.get("QBITTORRENT_URL", "http://127.0.0.1:8080").rstrip("/")
    jar = http.cookiejar.CookieJar()
    opener = build_opener(HTTPCookieProcessor(jar))
    login = urlencode({
        "username": os.environ["QBITTORRENT_USER"],
        "password": os.environ["QBITTORRENT_PASSWORD"],
    }).encode()
    opener.open(Request(base + "/api/v2/auth/login", data=login), timeout=20).read()
    with opener.open(base + "/api/v2/torrents/info", timeout=30) as response:
        return json.load(response)


def ensure_actions(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS orchestrator_actions (
      action_key TEXT PRIMARY KEY,
      action_type TEXT NOT NULL,
      scope_json TEXT NOT NULL,
      status TEXT NOT NULL,
      command_id INTEGER,
      reason TEXT,
      created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL
    )""")


def requested_missing_ids(connection: sqlite3.Connection) -> set[int]:
    scopes = connection.execute(
        "SELECT series_id,season_number FROM requested_scope_status WHERE state != 'complete'"
    ).fetchall()
    output: set[int] = set()
    for series_id, season in scopes:
        for episode in sonarr(f"/api/v3/episode?seriesId={series_id}"):
            if episode.get("seasonNumber") == season and episode.get("monitored") and not episode.get("hasFile"):
                output.add(int(episode["id"]))
    return output


def preview_for(torrent: dict) -> list[dict]:
    content_path = Path(torrent["content_path"])
    folder = artifact_folder(content_path)
    queue = sonarr("/api/v3/queue?page=1&pageSize=1000&includeUnknownSeriesItems=true")
    queue_records = queue.get("records", []) if isinstance(queue, dict) else []
    tracked = [
        item for item in queue_records
        if str(item.get("downloadId", "")).casefold() == torrent["hash"].casefold()
    ]
    parameters = {"folder": str(folder), "filterExistingFiles": "false"}
    if tracked:
        parameters["downloadId"] = torrent["hash"]
    query = urlencode(parameters)
    items = sonarr("/api/v3/manualimport?" + query)
    if not items and tracked:
        # Some completed multi-episode grabs are tracked by series ID while
        # their release folder cannot be parsed.  Ask Sonarr to validate a
        # strict filename-derived mapping instead of abandoning the artifact.
        raw = sonarr("/api/v3/manualimport?" + urlencode({
            "folder": str(folder), "filterExistingFiles": "false",
        }))
        series_ids = {int(item["seriesId"]) for item in tracked if item.get("seriesId")}
        if len(series_ids) == 1:
            series_id = series_ids.pop()
            episodes = sonarr(f"/api/v3/episode?seriesId={series_id}")
            missing_by_key = {
                (int(ep["seasonNumber"]), int(ep["episodeNumber"])): ep
                for ep in episodes if ep.get("monitored") and not ep.get("hasFile")
            }
            body = []
            for item in raw:
                key = strict_episode_key(item.get("path", ""))
                episode = missing_by_key.get(key)
                if episode is None:
                    continue
                body.append({
                    "path": item["path"],
                    "seriesId": series_id,
                    "seasonNumber": episode["seasonNumber"],
                    "episodeIds": [episode["id"]],
                    "quality": item["quality"],
                    "languages": item.get("languages") or [],
                    "releaseGroup": item.get("releaseGroup"),
                    "downloadId": torrent["hash"],
                    "indexerFlags": item.get("indexerFlags") or 0,
                    "releaseType": item.get("releaseType") or "singleEpisode",
                })
            if body:
                items = sonarr("/api/v3/manualimport", method="POST", body=body)
                for item in items:
                    item["series"] = {"id": series_id}
    if content_path.suffix.casefold() in MEDIA_EXTENSIONS:
        return [item for item in items if Path(item.get("path", "")) == content_path]
    prefix = str(content_path).rstrip("/") + "/"
    return [item for item in items if str(item.get("path", "")).startswith(prefix)]


def command_file(item: dict, download_hash: str) -> dict:
    episode = item["episodes"][0]
    return {
        "path": item["path"],
        "folderName": item.get("folderName"),
        "seriesId": item["series"]["id"],
        "episodeIds": [episode["id"]],
        "quality": item["quality"],
        "languages": item.get("languages") or [],
        "releaseGroup": item.get("releaseGroup"),
        "indexerFlags": item.get("indexerFlags") or 0,
        "releaseType": item.get("releaseType") or "singleEpisode",
        "downloadId": download_hash,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("/registry/acquisitions.sqlite3"))
    parser.add_argument("--download-hash", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    torrent = next((item for item in qbit_torrents() if item.get("hash", "").casefold() == args.download_hash.casefold()), None)
    if torrent is None:
        print("artifact not found")
        return 2
    if float(torrent.get("progress") or 0) != 1:
        print("artifact is not complete; no import planned")
        return 0

    with sqlite3.connect(args.database) as connection:
        ensure_actions(connection)
        missing = requested_missing_ids(connection)
        selected = select_safe_imports(preview_for(torrent), missing)
        episode_ids = sorted(item["episodes"][0]["id"] for item in selected)
        scope = {"download_hash": torrent["hash"].casefold(), "episode_ids": episode_ids}
        action_key = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()
        print(json.dumps({
            "artifact": torrent.get("name"),
            "safe_files": len(selected),
            "episode_ids": episode_ids,
            "mode": "apply" if args.apply else "preview",
        }, ensure_ascii=False))
        if not selected or not args.apply:
            return 0
        if connection.execute("SELECT 1 FROM orchestrator_actions WHERE action_key=?", (action_key,)).fetchone():
            print("action already recorded; refusing duplicate submission")
            return 0

        now = int(time.time())
        connection.execute(
            "INSERT INTO orchestrator_actions VALUES(?,?,?,?,?,?,?,?)",
            (action_key, "sonarr_manual_import", json.dumps(scope), "submitting", None,
             "One file maps to one requested missing episode; only pack-folder mismatch overridden.", now, now),
        )
        connection.commit()
        try:
            response = sonarr("/api/v3/command", method="POST", body={
                "name": "ManualImport",
                "files": [command_file(item, torrent["hash"]) for item in selected],
                "importMode": "copy",
            })
        except Exception as error:
            connection.execute(
                "UPDATE orchestrator_actions SET status='failed',reason=?,updated_at=? WHERE action_key=?",
                (str(error), int(time.time()), action_key),
            )
            connection.commit()
            raise
        connection.execute(
            "UPDATE orchestrator_actions SET status='submitted',command_id=?,updated_at=? WHERE action_key=?",
            (response.get("id"), int(time.time()), action_key),
        )
        connection.commit()
        print(f"submitted ManualImport command {response.get('id')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
