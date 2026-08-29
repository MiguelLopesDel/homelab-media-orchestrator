import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dub_orchestrator import (
    DATA_ROOT,
    connect_db,
    dub_probe_score,
    dub_title_score,
    internal_download_url,
    next_episode_job,
    candidate_plan,
    candidate_rank,
    cache_candidates,
    complete_pack_mapping,
    next_candidate_plan,
    search_due,
    selected_candidate,
    qbit_host_path,
    qbit_member_host_path,
    probe_is_stalled,
    scan_movie_jobs,
    select_movie_member,
    select_video_member,
)
from cached_candidate_adapter import title_can_cover_episode


class DubOrchestratorTests(unittest.TestCase):
    def test_candidate_plan_preserves_fallback_order(self):
        plan = candidate_plan([{"title": "first"}, {"title": "second"}])
        self.assertEqual("first", selected_candidate(__import__("json").dumps(plan))["title"])
        self.assertEqual("second", plan["remaining"][0]["title"])
        fallback = next_candidate_plan(__import__("json").dumps(plan))
        self.assertEqual("second", fallback["selected"]["title"])
        self.assertIsNone(next_candidate_plan(__import__("json").dumps(fallback)))

    def test_scheduler_uses_one_probe_per_season_before_second_episode(self):
        with tempfile.TemporaryDirectory() as directory:
            db = connect_db(Path(directory) / "state.sqlite3")
            rows = [
                (101, 1, "First", 1, 1, "/one", "queued", 10, 10),
                (102, 1, "First", 1, 2, "/two", "queued", 1, 1),
                (201, 2, "Second", 1, 1, "/three", "queued", 2, 2),
            ]
            db.executemany(
                """INSERT INTO dub_jobs(episode_id,series_id,series_title,season_number,
                   episode_number,target_path,state,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""", rows,
            )
            self.assertEqual(201, next_episode_job(db)["episode_id"])
            db.close()

    def test_scheduler_continues_active_probe_before_new_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            db = connect_db(Path(directory) / "state.sqlite3")
            rows = [
                (101, 1, "First", 1, 1, "/one", "metadata_wait", 10, 100),
                (201, 2, "Second", 1, 1, "/two", "queued", 1, 1),
            ]
            db.executemany(
                """INSERT INTO dub_jobs(episode_id,series_id,series_title,season_number,
                   episode_number,target_path,state,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""", rows,
            )
            self.assertEqual(101, next_episode_job(db)["episode_id"])
            db.close()

    def test_waiting_scope_in_cooldown_does_not_block_another_proved_dub(self):
        with tempfile.TemporaryDirectory() as directory:
            db = connect_db(Path(directory) / "state.sqlite3")
            future = __import__("time").time_ns() // 1_000_000_000 + 3600
            rows = [
                (101, 1, "Blocked", 1, 1, "/one", "waiting_candidate", 1, 1),
                (201, 2, "Ready", 1, 1, "/two", "queued", 2, 2),
            ]
            db.executemany(
                """INSERT INTO dub_jobs(episode_id,series_id,series_title,season_number,
                   episode_number,target_path,state,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""", rows,
            )
            db.execute("UPDATE dub_jobs SET next_attempt_at=? WHERE episode_id=101", (future,))
            self.assertEqual(201, next_episode_job(db)["episode_id"])
            db.close()

    def test_search_budget_is_per_season_not_global(self):
        with tempfile.TemporaryDirectory() as directory:
            db = connect_db(Path(directory) / "state.sqlite3")
            rows = [
                (101, 1, "First", 1, 1, "/one", "queued", 1, 1),
                (201, 2, "Second", 1, 1, "/two", "queued", 1, 1),
            ]
            db.executemany(
                """INSERT INTO dub_jobs(episode_id,series_id,series_title,season_number,
                   episode_number,target_path,state,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""", rows,
            )
            first = db.execute("SELECT * FROM dub_jobs WHERE episode_id=101").fetchone()
            second = db.execute("SELECT * FROM dub_jobs WHERE episode_id=201").fetchone()
            db.execute("INSERT INTO dub_budgets VALUES(?,?)", ("episode-search:1:1", __import__("time").time_ns() // 1_000_000_000))
            self.assertFalse(search_due(db, first))
            self.assertTrue(search_due(db, second))
            db.close()

    def test_pack_is_rejected_when_it_cannot_map_every_missing_episode(self):
        with tempfile.TemporaryDirectory() as directory:
            db = connect_db(Path(directory) / "state.sqlite3")
            rows = [
                (101, 1, "First", 1, 1, "/one", "queued", 1, 1),
                (102, 1, "First", 1, 2, "/two", "queued", 1, 1),
            ]
            db.executemany(
                """INSERT INTO dub_jobs(episode_id,series_id,series_title,season_number,
                   episode_number,target_path,state,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""", rows,
            )
            leader = next_episode_job(db)
            one_file = [{"index": 0, "name": "First S01E01.mkv", "size": 100_000_000}]
            self.assertEqual([], complete_pack_mapping(db, leader, one_file))
            full_pack = one_file + [{"index": 1, "name": "First S01E02.mkv", "size": 100_000_000}]
            self.assertEqual(2, len(complete_pack_mapping(db, leader, full_pack)))
            db.close()

    def test_only_strong_audio_markers_are_trusted_without_probe(self):
        self.assertGreater(dub_title_score("Anime S01E01 DUBLADO PT-BR 1080p"), 0)
        self.assertGreater(dub_title_score("[Anipakku] Anime S01 [1080p]"), 0)
        self.assertLess(dub_title_score("Anime S01E01 Multi-Audio 1080p"), 0)
        self.assertLess(dub_title_score("Anime S01E01 Multi-Subs PT-BR"), 0)
        self.assertLess(dub_title_score("Anime S01E01 English Dub"), 0)

    def test_ambiguous_multi_is_an_audio_probe_only_for_proved_dub_jobs(self):
        self.assertGreater(dub_probe_score("Anime S01E01 MULTi 1080p WEB"), 0)
        self.assertGreater(dub_probe_score("Anime S01E01 Dual-Audio 1080p"), 0)
        self.assertLess(dub_probe_score("Anime S01E01 Multi-Subs PT-BR"), 0)
        self.assertLess(dub_probe_score("Anime S01E01 English Dub"), 0)
        self.assertLess(dub_probe_score("Anime S01E01 Multi-Audio German Dub"), 0)

    def test_provider_multi_rip_precedes_plain_dual_probe(self):
        provider_multi = {"title": "Example S01E01 1080p CR WEB-DL MULTi AAC", "seeders": 1}
        plain_dual = {"title": "Example S01E01 1080p WEB DUAL AAC", "seeders": 99}
        self.assertGreater(candidate_rank(provider_multi), candidate_rank(plain_dual))

    def test_hinting_season_pack_precedes_single_episode_multi(self):
        pack = {"title": "Example Season 01 Batch 1080p CR WEB-DL MULTi AAC", "seeders": 1}
        single = {"title": "Example S01E01 1080p CR WEB-DL MULTi AAC", "seeders": 99}
        self.assertGreater(candidate_rank(pack), candidate_rank(single))

    def test_plain_dual_pack_does_not_precede_single_episode_multi(self):
        dual_pack = {"title": "Example Season 01 Batch 1080p WEB Dual-Audio", "seeders": 999}
        single = {"title": "Example S01E01 1080p CR WEB-DL MULTi AAC", "seeders": 1}
        self.assertGreater(candidate_rank(single), candidate_rank(dual_pack))

    def test_completed_probe_is_never_rejected_for_lacking_peers(self):
        old = __import__("time").time_ns() // 1_000_000_000 - 3600
        stalled = {"progress": 0.5, "availability": 0, "dlspeed": 0}
        completed = {"progress": 1, "availability": 0, "dlspeed": 0}
        self.assertTrue(probe_is_stalled(stalled, old))
        self.assertFalse(probe_is_stalled(completed, old))

    def test_cache_candidates_receives_the_state_database_for_rejections(self):
        with tempfile.TemporaryDirectory() as directory:
            db = connect_db(Path(directory) / "state.sqlite3")
            candidate_db = Path(directory) / "candidates.sqlite3"
            candidate_db.touch()
            job = {"series_id": 1, "season_number": 1, "episode_number": 1}
            release = {
                "title": "Example S01E01 Multi-Audio 1080p",
                "infohash": "a" * 40,
                "result_id": "result-1",
            }
            with mock.patch("dub_orchestrator.CANDIDATE_DB", candidate_db), \
                 mock.patch("dub_orchestrator.sonarr", return_value={"title": "Example"}), \
                 mock.patch("dub_orchestrator.cached_candidates", return_value=[release]):
                found = cache_candidates(db, job)
            self.assertEqual("result-1", found[0]["result_id"])
            db.close()

    def test_exact_season_episode_wins_inside_pack(self):
        files = [
            {"index": 0, "name": "Anime S01E01.mkv", "size": 100_000_000},
            {"index": 1, "name": "Anime S01E02.mkv", "size": 100_000_000},
        ]
        self.assertEqual(1, select_video_member(files, 1, 2, 2)["index"])

    def test_ambiguous_absolute_number_stops_for_review(self):
        files = [
            {"index": 0, "name": "Anime - 14.mkv", "size": 100_000_000},
            {"index": 1, "name": "Anime Episode 14 v2.mkv", "size": 100_000_000},
        ]
        self.assertIsNone(select_video_member(files, 2, 1, 14))

    def test_cached_candidate_never_crosses_an_explicit_season(self):
        self.assertFalse(title_can_cover_episode("Example S02E01 Multi-Audio", 1, 1))
        self.assertTrue(title_can_cover_episode("Example S01E01 Multi-Audio", 1, 1))

    def test_single_video_release_is_safe_to_select(self):
        files = [
            {"index": 0, "name": "release.nfo", "size": 100},
            {"index": 1, "name": "opaque-name.mkv", "size": 100_000_000},
        ]
        self.assertEqual(1, select_video_member(files, 3, 7, 31)["index"])

    def test_schema_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            first = connect_db(path)
            first.close()
            second = connect_db(path)
            tables = {
                row[0] for row in second.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertTrue({
                "dub_jobs", "dub_budgets", "dub_events",
                "movie_dub_jobs", "movie_dub_events",
            } <= tables)
            second.close()

    def test_missing_dub_movie_creates_a_durable_job(self):
        with tempfile.TemporaryDirectory() as directory:
            db = connect_db(Path(directory) / "state.sqlite3")
            db.execute(
                """CREATE TABLE movie_state(
                       movie_id INTEGER PRIMARY KEY,movie_title TEXT,year INTEGER,
                       file_path TEXT,decision_state TEXT,updated_at INTEGER)"""
            )
            db.execute(
                "INSERT INTO movie_state VALUES(?,?,?,?,?,?)",
                (21, "Example Anime: The Movie", 2019,
                 "/library/example-movie.mkv", "missing_dub", 100),
            )
            self.assertEqual(1, scan_movie_jobs(db)["created"])
            job = db.execute(
                "SELECT * FROM movie_dub_jobs WHERE movie_id=21"
            ).fetchone()
            self.assertEqual("queued", job["state"])
            self.assertEqual("/library/example-movie.mkv", job["target_path"])
            self.assertEqual(0, scan_movie_jobs(db)["created"])
            db.close()

    def test_movie_job_is_fulfilled_when_a_dub_appears(self):
        with tempfile.TemporaryDirectory() as directory:
            db = connect_db(Path(directory) / "state.sqlite3")
            db.execute(
                """CREATE TABLE movie_state(
                       movie_id INTEGER PRIMARY KEY,movie_title TEXT,year INTEGER,
                       file_path TEXT,decision_state TEXT,updated_at INTEGER)"""
            )
            db.execute(
                "INSERT INTO movie_state VALUES(?,?,?,?,?,?)",
                (21, "Movie", 2019, "/library/movie.mkv", "missing_dub", 100),
            )
            scan_movie_jobs(db)
            db.execute("UPDATE movie_state SET decision_state='complete' WHERE movie_id=21")
            self.assertEqual(1, scan_movie_jobs(db)["fulfilled"])
            state = db.execute(
                "SELECT state FROM movie_dub_jobs WHERE movie_id=21"
            ).fetchone()[0]
            self.assertEqual("fulfilled", state)
            db.close()

    def test_movie_member_is_not_confused_with_episodes_in_a_pack(self):
        files = [
            {"index": 0, "name": "Youjo Senki S01E01.mkv", "size": 500_000_000},
            {"index": 1, "name": "Gekijouban Youjo Senki Movie.mkv", "size": 2_000_000_000},
            {"index": 2, "name": "Youjo Senki S01E02.mkv", "size": 500_000_000},
        ]
        self.assertEqual(1, select_movie_member(files)["index"])

    def test_ambiguous_movie_pack_stops_for_review(self):
        files = [
            {"index": 0, "name": "Movie.mkv", "size": 2_000_000_000},
            {"index": 1, "name": "Film Bonus.mkv", "size": 200_000_000},
        ]
        self.assertIsNone(select_movie_member(files))

    def test_qbit_container_path_maps_to_host_without_guessing(self):
        self.assertEqual(
            DATA_ROOT / "torrents/anime/pack/episode.mkv",
            qbit_host_path("/data/torrents/anime", "pack/episode.mkv"),
        )
        with self.assertRaisesRegex(ValueError, "fora"):
            qbit_host_path("/downloads", "episode.mkv")

    def test_qbit_member_uses_actual_incomplete_content_path(self):
        torrent = {
            "save_path": "/data/torrents/dub-staging",
            "content_path": "/data/torrents/incompletos/dub-source/pack",
        }
        self.assertEqual(
            DATA_ROOT / "torrents/incompletos/dub-source/pack/episode.mkv",
            qbit_member_host_path(torrent, "pack/episode.mkv"),
        )

    def test_relative_prowlarr_download_url_is_made_container_reachable(self):
        self.assertEqual(
            "http://prowlarr:9696/api/v1/download?id=123",
            internal_download_url("/api/v1/download?id=123"),
        )
        self.assertEqual(
            "http://prowlarr:9696/api/v1/download?id=123",
            internal_download_url("http://127.0.0.1:9696/api/v1/download?id=123"),
        )


if __name__ == "__main__":
    unittest.main()
