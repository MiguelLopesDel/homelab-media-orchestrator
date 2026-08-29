#!/usr/bin/env python3
"""Archive qBittorrent metainfo files and maintain a local SQLite catalog.

The qBittorrent BT_backup directory is the source of truth.  This command is
idempotent: it only copies an artifact once, keyed by its infohash filename.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any


def parse_bencode(blob: bytes, offset: int = 0) -> tuple[Any, int]:
    marker = blob[offset:offset + 1]
    if marker == b"i":
        end = blob.index(b"e", offset)
        return int(blob[offset + 1:end]), end + 1
    if marker == b"l":
        result = []
        offset += 1
        while blob[offset:offset + 1] != b"e":
            item, offset = parse_bencode(blob, offset)
            result.append(item)
        return result, offset + 1
    if marker == b"d":
        result = {}
        offset += 1
        while blob[offset:offset + 1] != b"e":
            key, offset = parse_bencode(blob, offset)
            value, offset = parse_bencode(blob, offset)
            result[key] = value
        return result, offset + 1
    if b"0" <= marker <= b"9":
        colon = blob.index(b":", offset)
        size = int(blob[offset:colon])
        start = colon + 1
        return blob[start:start + size], start + size
    raise ValueError(f"invalid bencode marker at byte {offset}")


def text(value: Any) -> str | None:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value if isinstance(value, str) else None


def torrent_metadata(path: Path, infohash: str) -> dict[str, Any]:
    root, consumed = parse_bencode(path.read_bytes())
    if consumed != path.stat().st_size or not isinstance(root, dict):
        raise ValueError("the file is not one complete torrent dictionary")
    info = root.get(b"info", {})
    if not isinstance(info, dict):
        raise ValueError("torrent has no info dictionary")
    name = text(info.get(b"name.utf-8")) or text(info.get(b"name")) or infohash
    files = info.get(b"files")
    if isinstance(files, list):
        file_count = len(files)
        total_size = sum(item.get(b"length", 0) for item in files if isinstance(item, dict))
    else:
        file_count = 1
        total_size = info.get(b"length", 0)
    trackers: list[str] = []
    announce = text(root.get(b"announce"))
    if announce:
        trackers.append(announce)
    tiers = root.get(b"announce-list", [])
    if isinstance(tiers, list):
        for tier in tiers:
            values = tier if isinstance(tier, list) else [tier]
            for value in values:
                value_text = text(value)
                if value_text and value_text not in trackers:
                    trackers.append(value_text)
    return {
        "infohash": infohash.lower(),
        "name": name,
        "total_size": int(total_size),
        "file_count": int(file_count),
        "trackers": trackers,
        "comment": text(root.get(b"comment.utf-8")) or text(root.get(b"comment")),
        "created_by": text(root.get(b"created by")),
        "creation_date": root.get(b"creation date"),
        "metainfo_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


SCHEMA = """
CREATE TABLE IF NOT EXISTS torrents (
  infohash TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  total_size INTEGER NOT NULL,
  file_count INTEGER NOT NULL,
  trackers_json TEXT NOT NULL,
  comment TEXT,
  created_by TEXT,
  creation_date INTEGER,
  metainfo_sha256 TEXT NOT NULL,
  source_path TEXT NOT NULL,
  archive_path TEXT NOT NULL,
  first_seen_at INTEGER NOT NULL,
  last_seen_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS torrents_name_idx ON torrents(name);
"""


def archive(source: Path, destination: Path, dry_run: bool) -> tuple[int, int, list[str]]:
    artifacts = destination / "torrents"
    database_path = destination / "registry.sqlite3"
    if not dry_run:
        artifacts.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(":memory:" if dry_run else database_path)
    connection.executescript(SCHEMA)
    added = skipped = 0
    failures: list[str] = []
    now = int(time.time())
    for metainfo in sorted(source.glob("*.torrent")):
        infohash = metainfo.stem.lower()
        if len(infohash) not in (40, 64) or any(char not in "0123456789abcdef" for char in infohash):
            failures.append(f"{metainfo.name}: invalid infohash filename")
            continue
        target = artifacts / f"{infohash}.torrent"
        row = connection.execute("SELECT infohash FROM torrents WHERE infohash = ?", (infohash,)).fetchone()
        try:
            metadata = torrent_metadata(metainfo, infohash)
        except (OSError, ValueError) as error:
            failures.append(f"{metainfo.name}: {error}")
            continue
        if row is None:
            if not dry_run:
                temporary = target.with_suffix(".torrent.tmp")
                shutil.copy2(metainfo, temporary)
                os.replace(temporary, target)
            connection.execute(
                """INSERT INTO torrents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (metadata["infohash"], metadata["name"], metadata["total_size"], metadata["file_count"],
                 json.dumps(metadata["trackers"]), metadata["comment"], metadata["created_by"],
                 metadata["creation_date"], metadata["metainfo_sha256"], str(metainfo), str(target), now, now),
            )
            added += 1
        else:
            connection.execute("UPDATE torrents SET last_seen_at = ? WHERE infohash = ?", (now, infohash))
            skipped += 1
    connection.commit()
    connection.close()
    return added, skipped, failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="qBittorrent BT_backup directory")
    parser.add_argument("--archive", type=Path, required=True, help="registry directory")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not args.source.is_dir():
        parser.error(f"source directory does not exist: {args.source}")
    added, skipped, failures = archive(args.source, args.archive, args.dry_run)
    print(json.dumps({"added": added, "known": skipped, "failures": failures}, ensure_ascii=False))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
