"""Hermetic media-flow regression tests.

These tests generate a tiny local MKV with ffmpeg.  They use no network,
Docker daemon, Sonarr, qBittorrent, indexer, API token, or server path.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dub_orchestrator import connect_db, step_episode
from external_audio_builder import probe


def create_dubbed_fixture(path: Path) -> None:
    """Create a one-second 720p MKV with Japanese and Brazilian Portuguese audio."""
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=24",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
            "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000",
            "-t", "1", "-map", "0:v:0", "-map", "1:a:0", "-map", "2:a:0",
            "-c:v", "mpeg4", "-q:v", "5", "-c:a", "aac", "-threads", "1",
            "-metadata:s:a:0", "language=jpn",
            "-metadata:s:a:1", "language=por",
            "-metadata:s:a:1", "title=Brazilian",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )


class OfflineDubFlowTests(unittest.TestCase):
    def test_verified_720p_ptbr_torrent_is_hardlinked_not_transcoded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "source.mkv", root / "library.mkv"
            create_dubbed_fixture(source)
            create_dubbed_fixture(target)
            target.with_suffix(".por.default.m4a").write_bytes(b"obsolete sidecar")

            source_probe = probe(source)
            video = next(stream for stream in source_probe["streams"] if stream["codec_type"] == "video")
            audio_languages = [
                stream.get("tags", {}).get("language") for stream in source_probe["streams"]
                if stream["codec_type"] == "audio"
            ]
            self.assertEqual(720, video["height"])
            self.assertIn("por", audio_languages)

            db = connect_db(root / "state.sqlite3")
            db.execute(
                """INSERT INTO dub_jobs(episode_id,series_id,series_title,season_number,
                   episode_number,target_path,state,candidate_json,infohash,source_path,
                   source_owned,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    1, 1, "Offline fixture", 1, 1, str(target), "analysing",
                    json.dumps({"selected": {"title": "Offline fixture S01E01 Dublado PT-BR 720p"}}),
                    "offline", str(source), 0, 1, 1,
                ),
            )
            db.commit()
            # The direct-import path has no qBittorrent side effect when the
            # source is externally adopted; the fake makes that invariant explicit.
            with mock.patch("dub_orchestrator.QBit"):
                result = step_episode(db, allow_search=False, episode_id=1)
            self.assertEqual("fulfilled", result["state"])
            self.assertEqual(os.stat(source).st_ino, os.stat(target).st_ino)
            self.assertFalse(target.with_suffix(".por.default.m4a").exists())
            self.assertEqual("fulfilled", db.execute(
                "SELECT state FROM dub_jobs WHERE episode_id=1"
            ).fetchone()[0])
            db.close()


if __name__ == "__main__":
    unittest.main()
