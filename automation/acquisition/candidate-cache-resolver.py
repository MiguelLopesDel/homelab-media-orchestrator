#!/usr/bin/env python3
"""Resolve pending Seerr scopes against the local candidate cache only."""
import json, os, sqlite3, time
from urllib.request import Request, urlopen

DB = "/registry/acquisitions.sqlite3"
CANDIDATES = "/registry/candidates.sqlite3"

def sonarr_titles(series_id):
    req = Request(f"{os.getenv('SONARR_URL','http://127.0.0.1:8989')}/api/v3/series/{series_id}",
                  headers={"X-Api-Key": os.environ["SONARR_API_KEY"]})
    with urlopen(req, timeout=20) as response:
        series = json.load(response)
    titles = {series["title"]}
    for item in series.get("alternateTitles", []):
        if isinstance(item, dict) and item.get("title"):
            titles.add(item["title"])
    return sorted(titles, key=len, reverse=True)

def main():
    with sqlite3.connect(DB) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS scope_candidate_cache (
          request_id INTEGER NOT NULL, season_number INTEGER NOT NULL,
          result_id TEXT, title TEXT, seeders INTEGER, infohash TEXT,
          resolved_at INTEGER NOT NULL, PRIMARY KEY(request_id, season_number))""")
        pending = db.execute("SELECT request_id,series_id,season_number FROM requested_scope_status WHERE state='pending'").fetchall()
        with sqlite3.connect(CANDIDATES) as candidates:
            for request_id, series_id, season in pending:
                aliases = sonarr_titles(series_id)
                rows = []
                for alias in aliases:
                    # Ignore very short aliases: they are too ambiguous for an
                    # automatic cache match.
                    if len(alias) < 6: continue
                    rows.extend(candidates.execute("""SELECT result_id,title,seeders,infohash FROM results
                      WHERE lower(title) LIKE ? AND download_reference_encrypted IS NOT NULL
                      ORDER BY COALESCE(seeders,0) DESC,last_seen_at DESC LIMIT 10""",
                      (f"%{alias.casefold()}%",)).fetchall())
                row = max(rows, key=lambda item: item[2] or 0) if rows else None
                db.execute("""INSERT INTO scope_candidate_cache VALUES(?,?,?,?,?,?,?)
                  ON CONFLICT(request_id,season_number) DO UPDATE SET result_id=excluded.result_id,title=excluded.title,
                  seeders=excluded.seeders,infohash=excluded.infohash,resolved_at=excluded.resolved_at""",
                  (request_id, season, *(row or (None,None,None,None)), int(time.time())))
        print(json.dumps({"pending":len(pending)}))

if __name__ == '__main__': main()
