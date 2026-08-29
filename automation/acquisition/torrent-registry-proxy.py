#!/usr/bin/env python3
"""Passive Torznab response recorder for a Prowlarr-backed *arr stack.

The proxy is intentionally transparent to callers.  It records every item in
successful Torznab search responses, but never follows download URLs or asks an
indexer a second question.  Secrets in request/download URLs are never stored.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import logging
import re
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from xml.etree import ElementTree as ET

from cryptography.fernet import Fernet


TORZNAB = "http://torznab.com/schemas/2015/feed"
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade"}
SECRET_KEYS = {"apikey", "api_key", "passkey", "token", "auth", "authorization"}


SCHEMA = """
CREATE TABLE IF NOT EXISTS searches (
  id INTEGER PRIMARY KEY,
  seen_at INTEGER NOT NULL,
  indexer_id TEXT NOT NULL,
  query_json TEXT NOT NULL,
  result_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS results (
  result_id TEXT PRIMARY KEY,
  infohash TEXT,
  title TEXT NOT NULL,
  download_reference TEXT,
  download_reference_encrypted BLOB,
  size_bytes INTEGER,
  seeders INTEGER,
  peers INTEGER,
  published TEXT,
  category TEXT,
  first_seen_at INTEGER NOT NULL,
  last_seen_at INTEGER NOT NULL,
  seen_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS search_results (
  search_id INTEGER NOT NULL REFERENCES searches(id),
  result_id TEXT NOT NULL REFERENCES results(result_id),
  PRIMARY KEY(search_id, result_id)
);
CREATE INDEX IF NOT EXISTS results_title_idx ON results(title);
CREATE INDEX IF NOT EXISTS results_infohash_idx ON results(infohash);
"""


def clean_query(query: str) -> dict[str, str]:
    return {key: value for key, value in parse_qsl(query, keep_blank_values=True) if key.lower() not in SECRET_KEYS}


def clean_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme == "magnet":
        # Magnet links are identifiers, not credentials; retain only the BTIH.
        bits = [part for part in parsed.query.split("&") if part.lower().startswith("xt=urn:btih:")]
        return "magnet:?" + "&".join(bits)
    query = [(key, item) for key, item in parse_qsl(parsed.query, keep_blank_values=True) if key.lower() not in SECRET_KEYS]
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def as_int(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except ValueError:
        return None


def infohash_from(*values: str) -> str | None:
    for value in values:
        match = re.search(r"(?:btih:|infohash=)([A-Fa-f0-9]{40}|[A-Za-z2-7]{32})", value, re.I)
        if match:
            return match.group(1).lower()
    return None


class Catalog:
    def __init__(self, database: Path, vault_key: Path) -> None:
        database.parent.mkdir(parents=True, exist_ok=True)
        self.database = database
        self.lock = threading.Lock()
        raw_key = vault_key.read_bytes().strip()
        self.vault = Fernet(raw_key)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(results)")}
            if "download_reference" not in columns:
                connection.execute("ALTER TABLE results ADD COLUMN download_reference TEXT")
            if "download_reference_encrypted" not in columns:
                connection.execute("ALTER TABLE results ADD COLUMN download_reference_encrypted BLOB")

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.database, timeout=20)

    def raw_download_reference(self, result_id: str) -> str | None:
        """Resolve an opaque local id without exposing the stored credential."""
        if not re.fullmatch(r"[0-9a-f]{64}", result_id):
            return None
        with self.lock, self.connect() as connection:
            row = connection.execute(
                "SELECT download_reference_encrypted FROM results WHERE result_id=?",
                (result_id,),
            ).fetchone()
        if row is None or row[0] is None:
            return None
        return self.vault.decrypt(row[0]).decode("utf-8")

    def capture(self, indexer_id: str, query: dict[str, str], response: bytes) -> int:
        try:
            root = ET.fromstring(response)
        except ET.ParseError:
            return 0
        items = root.findall(".//item")
        now = int(time.time())
        records: list[dict[str, Any]] = []
        for item in items:
            title = item.findtext("title") or "[sem título]"
            guid = item.findtext("guid") or ""
            link = item.findtext("link") or ""
            enclosure = item.find("enclosure")
            download_url = (enclosure.get("url") if enclosure is not None else None) or link or guid
            attributes = {entry.get("name"): entry.get("value") for entry in item.findall(f"{{{TORZNAB}}}attr")}
            infohash = infohash_from(download_url, guid, attributes.get("infohash") or "")
            download_reference = clean_url(download_url)
            encrypted_reference = self.vault.encrypt(download_url.encode("utf-8")) if download_url else None
            stable_value = infohash or download_reference or title
            result_id = hashlib.sha256(f"{indexer_id}\0{stable_value}".encode()).hexdigest()
            records.append({
                "result_id": result_id,
                "infohash": infohash,
                "title": title,
                    "download_reference": download_reference,
                    "encrypted_reference": encrypted_reference,
                "size": as_int(attributes.get("size")),
                "seeders": as_int(attributes.get("seeders")),
                "peers": as_int(attributes.get("peers")),
                "published": item.findtext("pubDate"),
                "category": attributes.get("category"),
            })
        with self.lock, self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO searches(seen_at,indexer_id,query_json,result_count) VALUES(?,?,?,?)",
                (now, indexer_id, json.dumps(query, ensure_ascii=False, sort_keys=True), len(records)),
            )
            search_id = cursor.lastrowid
            for item in records:
                connection.execute(
                    """INSERT INTO results(result_id,infohash,title,download_reference,download_reference_encrypted,size_bytes,seeders,peers,published,category,first_seen_at,last_seen_at,seen_count)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(result_id) DO UPDATE SET
                      last_seen_at=excluded.last_seen_at, seen_count=results.seen_count+1,
                      seeders=excluded.seeders, peers=excluded.peers,
                      download_reference=excluded.download_reference,
                      download_reference_encrypted=excluded.download_reference_encrypted""",
                    (item["result_id"], item["infohash"], item["title"], item["download_reference"], item["encrypted_reference"], item["size"], item["seeders"], item["peers"],
                     item["published"], item["category"], now, now, 1),
                )
                connection.execute("INSERT OR IGNORE INTO search_results VALUES(?,?)", (search_id, item["result_id"]))
        return len(records)


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    catalog: Catalog
    upstream_host: str
    upstream_port: int

    def log_message(self, format: str, *args: object) -> None:
        # BaseHTTPRequestHandler normally logs the raw request target, which
        # may contain a Torznab API key.  The recorder never needs that value
        # for diagnostics, so apply the same sanitisation used for the catalog.
        parsed = urlsplit(self.path)
        safe_path = urlunsplit(("", "", parsed.path, urlencode(clean_query(parsed.query)), ""))
        values = list(args)
        if values:
            values[0] = safe_path
        logging.info("%s - %s", self.address_string(), format % tuple(values))

    def do_GET(self) -> None:
        if self.path.startswith("/__registry__/download/"):
            self.serve_cached_download()
            return
        self.forward()

    def do_POST(self) -> None:
        self.forward()

    def serve_cached_download(self) -> None:
        """Redeem an opaque candidate through Prowlarr without a new search."""
        result_id = urlsplit(self.path).path.rsplit("/", 1)[-1].lower()
        raw = self.catalog.raw_download_reference(result_id)
        if raw is None:
            self.send_error(404, "Cached candidate not found")
            return
        parsed = urlsplit(raw)
        # Captured references must point back to a Prowlarr download route. Do
        # not turn the registry into a generic credentialed HTTP/SSRF proxy.
        if not re.fullmatch(r"/\d+/download", parsed.path):
            self.send_error(422, "Cached reference is not a Prowlarr download")
            return
        target = parsed.path + (("?" + parsed.query) if parsed.query else "")
        connection = http.client.HTTPConnection(self.upstream_host, self.upstream_port, timeout=90)
        try:
            connection.request("GET", target, headers={"User-Agent": self.headers.get("User-Agent", "Sonarr")})
            response = connection.getresponse()
            body = response.read()
            self.send_response(response.status, response.reason)
            for key, value in response.getheaders():
                if key.lower() not in HOP_BY_HOP | {"content-length", "content-encoding"}:
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            logging.info("redeemed cached candidate result=%s status=%s bytes=%s", result_id[:12], response.status, len(body))
        except Exception as error:
            logging.error("cached candidate redemption failed result=%s: %s", result_id[:12], error)
            self.send_error(502, "Prowlarr download failed")
        finally:
            connection.close()

    def forward(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = self.rfile.read(length) if length else None
        headers = {key: value for key, value in self.headers.items() if key.lower() not in HOP_BY_HOP | {"host", "accept-encoding", "content-length"}}
        if payload is not None:
            headers["Content-Length"] = str(len(payload))
        connection = http.client.HTTPConnection(self.upstream_host, self.upstream_port, timeout=90)
        try:
            connection.request(self.command, self.path, body=payload, headers=headers)
            response = connection.getresponse()
            body = response.read()
            response_headers = response.getheaders()
            self.send_response(response.status, response.reason)
            for key, value in response_headers:
                if key.lower() not in HOP_BY_HOP | {"content-length", "content-encoding"}:
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            match = re.match(r"^/(\d+)/api(?:\?|$)", self.path)
            content_type = next((value for key, value in response_headers if key.lower() == "content-type"), "")
            if response.status == 200 and match and ("xml" in content_type.lower() or body.lstrip().startswith(b"<?xml")):
                count = self.catalog.capture(match.group(1), clean_query(urlsplit(self.path).query), body)
                logging.info("captured indexer=%s results=%s", match.group(1), count)
        except Exception as error:
            logging.exception("upstream request failed: %s", error)
            self.send_error(502, "Prowlarr upstream unavailable")
        finally:
            connection.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9696)
    parser.add_argument("--upstream-host", default="prowlarr-upstream")
    parser.add_argument("--upstream-port", type=int, default=9696)
    parser.add_argument("--database", type=Path, default=Path("/registry/candidates.sqlite3"))
    parser.add_argument("--vault-key", type=Path, default=Path("/run/secrets/candidate-vault.key"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ProxyHandler.catalog = Catalog(args.database, args.vault_key)
    ProxyHandler.upstream_host = args.upstream_host
    ProxyHandler.upstream_port = args.upstream_port
    server = ThreadingHTTPServer((args.listen, args.port), ProxyHandler)
    logging.info("recorder listening on %s:%s -> %s:%s", args.listen, args.port, args.upstream_host, args.upstream_port)
    server.serve_forever()


if __name__ == "__main__":
    main()
