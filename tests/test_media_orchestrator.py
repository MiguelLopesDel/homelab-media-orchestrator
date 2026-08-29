import unittest
from pathlib import Path

import media_orchestrator_core as core
from cached_candidate_adapter import candidates, known_dead_hashes, query_matches, title_can_cover_episode
from media_acquisition_dispatcher import blocked_download_hashes, due_scope, title_starts_with_series
from shoko_sonarr_bridge import resolve_target
from media_orchestrator_core import (
    PreviousPlan,
    ScopeFacts,
    reconcile_scope,
    satisfied_queue_ids,
    scope_is_available,
    select_safe_imports,
)
from sonarr_import_reconciler import artifact_folder, strict_episode_key


class ReconciliationTests(unittest.TestCase):
    def facts(self, **changes):
        values = dict(request_id=12, series_id=30, season_number=2,
                      missing_episodes=11, queue_episodes=0)
        values.update(changes)
        return ScopeFacts(**values)

    def test_pending_scope_has_a_continuation(self):
        plan = reconcile_scope(self.facts(), None, 1000)
        self.assertEqual("reconcile_scope", plan.action)
        self.assertEqual(1000, plan.next_action_at)

    def test_cache_is_used_before_search(self):
        plan = reconcile_scope(self.facts(cached_candidate_count=3), None, 1000)
        self.assertEqual("try_cached_candidate", plan.action)

    def test_safe_artifact_is_used_before_cache(self):
        plan = reconcile_scope(self.facts(safe_import_count=11, cached_candidate_count=3), None, 1000)
        self.assertEqual("import_safe_files", plan.action)

    def test_import_block_does_not_freeze_the_remaining_season(self):
        plan = reconcile_scope(self.facts(import_blocked=True), None, 1000)
        self.assertEqual("resolving_import", plan.lifecycle_state)
        self.assertEqual("reconcile_scope", plan.action)
        self.assertEqual(1000, plan.next_action_at)

    def test_seerr_availability_does_not_close_scope_with_missing_files(self):
        plan = reconcile_scope(self.facts(satisfied_by_seerr=True), None, 1000)
        self.assertEqual("retry_scheduled", plan.lifecycle_state)
        self.assertEqual("reconcile_scope", plan.action)

    def test_imported_scope_waits_for_language_audit(self):
        plan = reconcile_scope(
            self.facts(missing_episodes=0, language_unscanned_episodes=2), None, 1000,
        )
        self.assertEqual("verifying_language", plan.lifecycle_state)
        self.assertEqual("refresh_language_audit", plan.action)

    def test_imported_scope_waits_for_language_remediation(self):
        plan = reconcile_scope(
            self.facts(missing_episodes=0, language_pending_episodes=3), None, 1000,
        )
        self.assertEqual("remediating_language", plan.lifecycle_state)
        self.assertEqual("await_language_pipeline", plan.action)

    def test_scope_only_completes_after_import_and_language_policy(self):
        plan = reconcile_scope(self.facts(missing_episodes=0), None, 1000)
        self.assertEqual("complete", plan.lifecycle_state)

    def test_overall_available_does_not_close_processing_specials(self):
        request = {
            "media_status": 5,
            "requested_season_statuses": {0: 2, 1: 5},
        }
        self.assertFalse(scope_is_available(request, 0))
        self.assertTrue(scope_is_available(request, 1))

    def test_completed_import_block_is_detached_after_scope_is_satisfied(self):
        queued = [{
            "id": 91,
            "download_id": "ABC",
            "status": "completed",
            "tracked_download_state": "importBlocked",
        }]
        torrents = {"abc": {"progress": 1}}
        self.assertEqual([91], satisfied_queue_ids(0, queued, torrents))

    def test_incomplete_scope_never_detaches_import_blocked_queue(self):
        queued = [{
            "id": 91,
            "download_id": "ABC",
            "status": "completed",
            "tracked_download_state": "importBlocked",
        }]
        self.assertEqual([], satisfied_queue_ids(1, queued, {"abc": {"progress": 1}}))

    def test_imported_episode_detaches_its_stale_warning_before_season_completes(self):
        queued = [{
            "id": 91,
            "download_id": "ABC",
            "episode_ids": [1156],
            "status": "completed",
            "tracked_download_state": "importBlocked",
        }]
        self.assertEqual(
            [91],
            satisfied_queue_ids(
                21, queued, {"abc": {"progress": 1}}, available_episode_ids={1156},
            ),
        )

    def test_existing_cooldown_is_preserved(self):
        previous = PreviousPlan("search_cooldown", "search_one_episode", 2000, 2, 900)
        plan = reconcile_scope(self.facts(), previous, 1000)
        self.assertEqual(2000, plan.next_action_at)
        self.assertEqual("search_cooldown", plan.lifecycle_state)


class SafeImportTests(unittest.TestCase):
    def preview(self, episode_id=1112, rejection=None, has_file=False):
        return {
            "path": "/downloads/Spice and Wolf II - 01.mkv",
            "episodes": [{"id": episode_id, "hasFile": has_file}],
            "quality": {"quality": {"id": 7}},
            "languages": [{"id": 1, "name": "English"}],
            "rejections": [] if rejection is None else [{"reason": rejection}],
        }

    def test_pack_folder_mismatch_is_safe_to_override(self):
        item = self.preview(rejection="Episode 2x01 was unexpected considering the pack folder name")
        self.assertEqual([item], select_safe_imports([item], {1112}))

    def test_grab_history_id_rejection_is_safe_to_override(self):
        item = self.preview(rejection=(
            "Found matching series via grab history, but release was matched "
            "to series by ID. Automatic import is not possible."
        ))
        self.assertEqual([item], select_safe_imports([item], {1112}))

    def test_invalid_episode_is_never_overridden(self):
        item = self.preview(rejection="Invalid season or episode")
        self.assertEqual([], select_safe_imports([item], {1112}))

    def test_existing_episode_is_never_overwritten(self):
        item = self.preview(has_file=True)
        self.assertEqual([], select_safe_imports([item], {1112}))

    def test_ambiguous_duplicate_mapping_is_rejected(self):
        first = self.preview()
        second = {**self.preview(), "path": "/downloads/duplicate.mkv"}
        self.assertEqual([], select_safe_imports([first, second], {1112}))


class ArtifactFolderTests(unittest.TestCase):
    def test_dotted_release_directory_is_not_treated_as_media_file(self):
        path = Path("/data/torrents/anime/WataMote.S01.1080p.x265-smol")
        self.assertEqual(path, artifact_folder(path))

    def test_media_file_uses_parent_folder(self):
        path = Path("/data/torrents/anime/WataMote.S01E01.mkv")
        self.assertEqual(path.parent, artifact_folder(path))

    def test_strict_episode_key_accepts_one_episode(self):
        self.assertEqual((1, 9), strict_episode_key("WataMote.S01E09.1080p.mkv"))

    def test_strict_episode_key_rejects_multi_episode_file(self):
        self.assertIsNone(strict_episode_key("WataMote.S01E09-E10.1080p.mkv"))


class DeadCandidateTests(unittest.TestCase):
    def torrent(self, **changes):
        value = {"downloaded": 0, "dlspeed": 0, "availability": 0}
        value.update(changes)
        return value

    def test_two_samples_do_not_replace_a_recent_torrent(self):
        self.assertFalse(core.dead_near_empty_candidate(
            self.torrent(), stalled_samples=2, stalled_seconds=300,
        ))

    def test_two_samples_and_ten_real_minutes_can_replace_a_dead_torrent(self):
        self.assertTrue(core.dead_near_empty_candidate(
            self.torrent(), stalled_samples=2, stalled_seconds=600,
        ))

    def test_available_candidate_is_never_declared_dead(self):
        self.assertFalse(core.dead_near_empty_candidate(
            self.torrent(availability=1), stalled_samples=3, stalled_seconds=900,
        ))


class CachedCandidateTests(unittest.TestCase):
    def test_cached_search_context_does_not_admit_another_series_title(self):
        import sqlite3
        db = sqlite3.connect(":memory:")
        db.execute("""CREATE TABLE results(
          result_id TEXT,title TEXT,seeders INTEGER,size_bytes INTEGER,published TEXT,
          infohash TEXT,last_seen_at INTEGER,download_reference_encrypted TEXT)""")
        db.execute("CREATE TABLE searches(id INTEGER,indexer_id INTEGER,query_json TEXT)")
        db.execute("CREATE TABLE search_results(search_id INTEGER,result_id TEXT)")
        db.execute(
            "INSERT INTO results VALUES(?,?,?,?,?,?,?,?)",
            ("wrong", "[Anipakku] Tonikaku Kawaii High School Days ONA", 20, 1000,
             None, "ABC", 10, "encrypted"),
        )
        db.execute(
            "INSERT INTO searches VALUES(?,?,?)",
            (1, 4, '{"q":"School Days","season":"0","ep":"1"}'),
        )
        db.execute("INSERT INTO search_results VALUES(?,?)", (1, "wrong"))
        self.assertEqual([], candidates(db, ["School Days"], 0, 1))

    def test_exact_torznab_episode_context_matches(self):
        query = {"q": "Watashi ga Motenai", "season": "1", "ep": "10"}
        self.assertTrue(query_matches(query, ["Watashi ga Motenai"], 1, 10))

    def test_other_episode_does_not_match(self):
        query = {"q": "Watashi ga Motenai", "season": "1", "ep": "11"}
        self.assertFalse(query_matches(query, ["Watashi ga Motenai"], 1, 10))

    def test_plain_search_suffix_matches_exact_episode(self):
        query = {"q": "Spice and Wolf II 08"}
        self.assertTrue(query_matches(query, ["Spice and Wolf II"], 2, 8))

    def test_result_explicitly_naming_other_episode_is_rejected(self):
        title = "[FTW]_Watashi_ga_Motenai_-_03_[720p].mkv"
        self.assertFalse(title_can_cover_episode(title, 1, 9))

    def test_season_pack_without_explicit_episode_remains_eligible(self):
        title = "[smol] WataMote (Season 1) (BD 1080p HEVC) [Dual Audio]"
        self.assertTrue(title_can_cover_episode(title, 1, 10))

    def test_known_zero_availability_hash_is_excluded(self):
        import sqlite3
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE entities(source TEXT,state_json TEXT)")
        db.execute("INSERT INTO entities VALUES(?,?)", ("qbittorrent", '{"hash":"ABC","progress":0,"dlspeed":0,"availability":0}'))
        self.assertEqual({"abc"}, known_dead_hashes(db))


class DispatcherTests(unittest.TestCase):
    def test_import_blocked_artifact_is_identified_by_sonarr_download_hash(self):
        queue = [{
            "seriesId": 32,
            "seasonNumber": 1,
            "downloadId": "F9860D1C8C84982E08606B84734862CC289C3D9A",
            "trackedDownloadState": "importBlocked",
            "status": "completed",
        }]
        self.assertEqual(
            {"f9860d1c8c84982e08606b84734862cc289c3d9a"},
            blocked_download_hashes(queue, 32, 1),
        )

    def test_due_scope_prefers_regular_season_over_specials(self):
        import sqlite3
        import time
        db = sqlite3.connect(":memory:")
        db.execute("""CREATE TABLE scope_reconciliation(
          request_id INTEGER,series_id INTEGER,season_number INTEGER,
          lifecycle_state TEXT,action TEXT,next_action_at INTEGER,reason TEXT,
          attempt_count INTEGER,last_action_at INTEGER,updated_at INTEGER)""")
        now = int(time.time())
        rows = [
            (10, 28, 0, "retry_scheduled", "search_one_episode", now, "special", 0, None, 1),
            (13, 31, 1, "retry_scheduled", "try_cached_candidate", now, "season", 0, None, 2),
        ]
        db.executemany("INSERT INTO scope_reconciliation VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
        self.assertEqual((13, 31, 1, 0), due_scope(db))

    def test_due_scope_does_not_starve_never_attempted_special(self):
        import sqlite3
        import time
        db = sqlite3.connect(":memory:")
        db.execute("""CREATE TABLE scope_reconciliation(
          request_id INTEGER,series_id INTEGER,season_number INTEGER,
          lifecycle_state TEXT,action TEXT,next_action_at INTEGER,reason TEXT,
          attempt_count INTEGER,last_action_at INTEGER,updated_at INTEGER)""")
        now = int(time.time())
        rows = [
            (10, 28, 0, "retry_scheduled", "reconcile_scope", now, "retried", 4, now, 1),
            (11, 29, 0, "retry_scheduled", "reconcile_scope", now, "never tried", 0, None, 2),
        ]
        db.executemany("INSERT INTO scope_reconciliation VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
        self.assertEqual((11, 29, 0, 0), due_scope(db))

    def test_release_must_start_with_series_after_group(self):
        self.assertTrue(title_starts_with_series("[Group] Example Series (Season 1-2)", ["Example Series"]))
        self.assertFalse(title_starts_with_series("[Anipakku] Tonikaku Kawaii High School Days", ["School Days"]))


class ShokoBridgeTests(unittest.TestCase):
    def test_exact_tmdb_show_reference_resolves_sonarr_series(self):
        expected = {"id": 42, "title": "Example Series"}
        reference = {"series": {"TMDB": {"Show": [123]}, "TvDB": [456]}}
        self.assertIs(expected, resolve_target(reference, {123: expected}, {}))

    def test_ambiguous_tmdb_reference_can_fall_back_to_exact_tvdb(self):
        expected = {"id": 42, "title": "Example Series"}
        reference = {"series": {"TMDB": {"Show": [123, 124]}, "TvDB": [456]}}
        self.assertIs(expected, resolve_target(reference, {}, {456: expected}))


if __name__ == "__main__":
    unittest.main()
