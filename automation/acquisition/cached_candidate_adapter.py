#!/usr/bin/env python3
"""Reuse one exact cached candidate through Sonarr without a new indexer search."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.request import Request, urlopen

from media_orchestrator_core import release_title_starts_with_series


def sonarr(path: str, *, method: str = "GET", body: object | None = None):
    data = json.dumps(body).encode() if body is not None else None
    request = Request(
        os.environ.get("SONARR_URL", "http://127.0.0.1:8989").rstrip("/") + path,
        data=data,
        method=method,
        headers={"X-Api-Key": os.environ["SONARR_API_KEY"], "Content-Type": "application/json"},
    )
    with urlopen(request, timeout=90) as response:
        payload = response.read()
    return json.loads(payload) if payload else None


def normalized(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def iso_date(value: str | None) -> str:
    if value:
        try:
            return parsedate_to_datetime(value).astimezone(timezone.utc).isoformat()
        except (TypeError, ValueError):
            try:
                return datetime.fromisoformat(value).astimezone(timezone.utc).isoformat()
            except (TypeError, ValueError):
                pass
    return datetime.now(timezone.utc).isoformat()


def query_matches(query: dict, aliases: list[str], season: int, episode: int) -> bool:
    query_season = query.get("season")
    query_episode = query.get("ep")
    if query_season is not None and str(query_season) != str(season):
        return False
    if query_episode is not None and str(query_episode).lstrip("0") != str(episode):
        return False
    q = normalized(str(query.get("q", "")))
    alias_match = any(normalized(alias) in q or q.startswith(normalized(alias)) for alias in aliases if len(normalized(alias)) >= 6)
    if not alias_match:
        return False
    if query_episode is not None:
        return True
    return bool(re.search(rf"(?:^| )0*{episode}$", q))


def title_can_cover_episode(title: str, season: int, episode: int) -> bool:
    """Reject releases that explicitly name a different episode.

    Titles without an explicit episode remain eligible because they may be
    season packs; Sonarr remains the final parser and policy authority.
    """
    text = title.casefold().replace("_", " ").replace(".", " ")
    # A theatrical film can share a series title but can never supply a TV
    # episode. Keep the exclusion at this cache seam so every caller inherits it.
    if re.search(r"\b(?:movie|films?|le film|the film|gekijouban)\b", text):
        return False
    # Named recap/compilation specials share a series title but are not a
    # substitute for a requested TV episode (including normal S00 specials).
    if re.search(r"\b(?:recollections|recap|compilation)\b", text):
        return False
    declared_seasons = {
        int(value) for value in re.findall(r"\bs0*(\d{1,3})e\d", text)
    }
    declared_seasons.update(
        int(value) for value in re.findall(r"\b0*(\d{1,3})x\d", text)
    )
    # A release that explicitly names another season must never become a probe
    # merely because both seasons have an episode with the same number.
    if declared_seasons and declared_seasons != {season}:
        return False
    explicit: set[int] = set()
    for pattern in (
        rf"\bs0*{season}e0*(\d{{1,3}})\b",
        rf"\b0*{season}x0*(\d{{1,3}})\b",
        r"(?:\bepisode\s*|\bep\s*|\s-\s)0*(\d{1,3})\b",
    ):
        explicit.update(int(value) for value in re.findall(pattern, text))
    return not explicit or episode in explicit


def candidates(db: sqlite3.Connection, aliases: list[str], season: int, episode: int) -> list[dict]:
    rows = db.execute("""SELECT r.result_id,r.title,r.seeders,r.size_bytes,r.published,
                                r.infohash,r.last_seen_at,s.indexer_id,s.query_json
                         FROM results r
                         JOIN search_results sr ON sr.result_id=r.result_id
                         JOIN searches s ON s.id=sr.search_id
                         WHERE r.download_reference_encrypted IS NOT NULL
                         ORDER BY COALESCE(r.seeders,0) DESC,r.last_seen_at DESC""").fetchall()
    output: dict[str, dict] = {}
    for result_id, title, seeders, size, published, infohash, seen, indexer, query_json in rows:
        try:
            query = json.loads(query_json)
        except json.JSONDecodeError:
            continue
        if (not query_matches(query, aliases, season, episode)
                or not title_can_cover_episode(title, season, episode)
                or not release_title_starts_with_series(title, aliases)):
            continue
        output.setdefault(result_id, {
            "result_id": result_id, "title": title, "seeders": seeders or 0,
            "size": size or 0, "published": published, "infohash": infohash,
            "last_seen_at": seen, "indexer": indexer,
        })
    return sorted(output.values(), key=lambda item: (item["seeders"], item["last_seen_at"]), reverse=True)


def ensure_actions(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS orchestrator_actions (
      action_key TEXT PRIMARY KEY, action_type TEXT NOT NULL, scope_json TEXT NOT NULL,
      status TEXT NOT NULL, command_id INTEGER, reason TEXT,
      created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)""")


def known_dead_hashes(db: sqlite3.Connection) -> set[str]:
    output: set[str] = set()
    for state_json, in db.execute("SELECT state_json FROM entities WHERE source='qbittorrent'"):
        try:
            state = json.loads(state_json)
        except json.JSONDecodeError:
            continue
        if (float(state.get("progress") or 0) < 1
                and int(state.get("dlspeed") or 0) == 0
                and float(state.get("availability") or 0) <= 0):
            output.add(str(state.get("hash", "")).casefold())
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=Path("/registry/candidates.sqlite3"))
    parser.add_argument("--acquisitions", type=Path, default=Path("/registry/acquisitions.sqlite3"))
    parser.add_argument("--series-id", type=int, required=True)
    parser.add_argument("--season", type=int, required=True)
    parser.add_argument("--episode", type=int, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    series = sonarr(f"/api/v3/series/{args.series_id}")
    aliases = [series["title"]] + [item["title"] for item in series.get("alternateTitles", []) if item.get("title")]
    with sqlite3.connect(args.registry) as registry:
        found = candidates(registry, aliases, args.season, args.episode)
    with sqlite3.connect(args.acquisitions) as actions:
        ensure_actions(actions)
        dead_hashes = known_dead_hashes(actions)
        attempted = {row[0] for row in actions.execute(
            "SELECT json_extract(scope_json,'$.result_id') FROM orchestrator_actions WHERE action_type='cached_release_push'"
        ) if row[0]}
        found = [item for item in found
                 if item["result_id"] not in attempted
                 and str(item.get("infohash") or "").casefold() not in dead_hashes]
        preview = [{"id": item["result_id"][:12], "title": item["title"], "seeders": item["seeders"]} for item in found[:5]]
        print(json.dumps({"series": series["title"], "season": args.season, "episode": args.episode,
                          "candidates": preview, "mode": "apply" if args.apply else "preview"}, ensure_ascii=False))
        if not found or not args.apply:
            return 0
        item = found[0]
        scope = {"series_id": args.series_id, "season": args.season, "episode": args.episode,
                 "result_id": item["result_id"]}
        action_key = "cache:" + item["result_id"]
        now = int(time.time())
        actions.execute("INSERT INTO orchestrator_actions VALUES(?,?,?,?,?,?,?,?)",
                        (action_key, "cached_release_push", json.dumps(scope), "submitting", None,
                         "Exact cached search context; no new indexer query.", now, now))
        actions.commit()
        payload = {
            "title": item["title"],
            "downloadUrl": f"http://prowlarr:9696/__registry__/download/{item['result_id']}",
            "protocol": "torrent",
            "publishDate": iso_date(item["published"]),
            "size": item["size"],
            "infoHash": item["infohash"],
            "seeders": item["seeders"],
            "tvdbId": series.get("tvdbId") or 0,
        }
        try:
            response = sonarr("/api/v3/release/push", method="POST", body=payload)
            approved = any(item.get("approved") for item in response or [])
            rejections = sorted({reason for decision in response or [] for reason in decision.get("rejections", [])})
            status = "grabbed" if approved else "rejected"
            reason = "Sonarr accepted cached release." if approved else "; ".join(rejections)[:1000]
        except Exception as error:
            status, reason = "failed", str(error)[:1000]
        actions.execute("UPDATE orchestrator_actions SET status=?,reason=?,updated_at=? WHERE action_key=?",
                        (status, reason, int(time.time()), action_key))
        actions.commit()
        print(json.dumps({"result": status, "reason": reason}, ensure_ascii=False))
        return 0 if status in {"grabbed", "rejected"} else 1


if __name__ == "__main__":
    sys.exit(main())
