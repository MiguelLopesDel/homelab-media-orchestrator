import tempfile
import unittest
import json
from pathlib import Path
from unittest import mock

from subtitle_orchestrator import (
    choose_pt_source_member,
    choose_source_member,
    connect_db,
    compact_episode_ranges,
    crunchyroll_dub_is_available,
    crunchyroll_season_number,
    dub_is_available,
    episode_in_mapping_range,
    external_dub_is_available,
    completed_season_dub_is_available,
    external_movie_dub_is_available,
    matching_audio_sidecars,
    parse_crunchyroll_calendar,
    matching_sidecars,
    send_pending_alerts,
    sidecar_language,
    trusted_sonarr_pt_audio,
)


class SubtitleOrchestratorTests(unittest.TestCase):
    def test_trusted_direct_import_can_supply_missing_mp4_language_tag(self):
        self.assertTrue(trusted_sonarr_pt_audio({
            "releaseGroup": "DirectPTBR",
            "languages": [{"id": 33, "name": "Portuguese (Brazil)"}],
        }))

    def test_unreviewed_sonarr_language_guess_is_not_trusted(self):
        self.assertFalse(trusted_sonarr_pt_audio({
            "releaseGroup": "UnknownRelease",
            "languages": [{"id": 33, "name": "Portuguese (Brazil)"}],
        }))

    def test_sidecars_belong_to_exact_video_and_identify_language(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "Anime - S01E01.mkv"
            video.touch()
            pt = root / "Anime - S01E01.pt-BR.srt"
            en = root / "Anime - S01E01.eng.ass"
            other = root / "Anime - S01E02.pt.srt"
            for path in (pt, en, other):
                path.write_text("subtitle")
            self.assertEqual([en, pt], matching_sidecars(video))
            self.assertEqual("pt", sidecar_language(pt, video))
            self.assertEqual("en", sidecar_language(en, video))

    def test_external_audio_belongs_to_exact_video(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "Anime - S01E01.mkv"
            video.touch()
            pt = root / "Anime - S01E01.Portuguese (Brazil).default.por.m4a"
            other = root / "Anime - S01E02.default.por.m4a"
            pt.write_bytes(b"audio")
            other.write_bytes(b"audio")
            self.assertEqual([pt], matching_audio_sidecars(video))
            self.assertEqual("pt", sidecar_language(pt, video))

    def test_dub_catalog_is_series_and_season_specific(self):
        catalog = {
            "Sample Show": {"dublado": [1, 2]},
            "Complete Dub Show": {"dublado": "tudo"},
        }
        self.assertTrue(dub_is_available(catalog, "Sample Show", "/x", 2))
        self.assertFalse(dub_is_available(catalog, "Sample Show", "/x", 3))
        self.assertTrue(dub_is_available(catalog, "Complete Dub Show: Sequel", "/x", 0))
        self.assertFalse(dub_is_available(catalog, "Example Series", "/x", 1))

    def test_dub_catalog_can_limit_an_airing_season_by_episode(self):
        catalog = {
            "Airing Anime": {
                "temporadas": {
                    "2": {
                        "episodios": {"ate": 7},
                        "fonte": "official-provider",
                    }
                }
            }
        }
        self.assertTrue(dub_is_available(catalog, "Airing Anime", "/x", 2, 7))
        self.assertFalse(dub_is_available(catalog, "Airing Anime", "/x", 2, 8))

    def test_exact_episode_list_does_not_fill_gaps(self):
        catalog = {
            "Anime": {"temporadas": {"1": {"episodios": [1, 2, 4]}}}
        }
        self.assertTrue(dub_is_available(catalog, "Anime", "/x", 1, 4))
        self.assertFalse(dub_is_available(catalog, "Anime", "/x", 1, 3))

    def test_external_high_confidence_mapping_is_per_episode(self):
        index = (
            {
                "tvdb_show:123:s2": {
                    "mal:456": {"1-7": "1-7"},
                }
            },
            {456}, set(),
        )
        self.assertTrue(external_dub_is_available(index, 123, 2, 7))
        self.assertFalse(external_dub_is_available(index, 123, 2, 8))
        self.assertFalse(external_dub_is_available(index, 123, 1, 7))

    def test_external_normal_confidence_is_still_exactly_mapped(self):
        index = (
            {"tvdb_show:123:s1": {"mal:456": {"1-12": "1-12"}}},
            set(), {456},
        )
        self.assertTrue(external_dub_is_available(index, 123, 1, 12))
        self.assertFalse(external_dub_is_available(index, 123, 1, 13))

    def test_external_mapping_requires_high_confidence_mal_id(self):
        index = (
            {"tvdb_show:123:s1": {"mal:456": {"1-12": "1-12"}}},
            set(), set(),
        )
        self.assertFalse(external_dub_is_available(index, 123, 1, 1))

    def test_completed_regular_season_inherits_mapped_dub(self):
        index = (
            {"tvdb_show:123:s1": {"mal:456": {"1-12": "1-12"}}},
            {456}, set(),
        )
        self.assertTrue(completed_season_dub_is_available(
            index, 123, 1, set(range(1, 13)), series_ended=True,
        ))

    def test_airing_or_partially_mapped_season_does_not_inherit(self):
        index = (
            {"tvdb_show:123:s1": {"mal:456": {"1-7": "1-7"}}},
            {456}, set(),
        )
        self.assertFalse(completed_season_dub_is_available(
            index, 123, 1, set(range(1, 9)), series_ended=True,
        ))
        self.assertFalse(completed_season_dub_is_available(
            index, 123, 1, set(range(1, 8)), series_ended=False,
        ))
        self.assertFalse(completed_season_dub_is_available(
            index, 123, 0, {1}, series_ended=True,
        ))

    def test_external_movie_dub_uses_exact_tmdb_mapping(self):
        index = (
            {
                "tmdb_movie:283984": {
                    "mal:25537": {"1": "1"},
                    "imdb_movie:tt4054952": {"1": "1"},
                }
            },
            {25537}, set(),
        )
        self.assertTrue(external_movie_dub_is_available(index, 283984))
        self.assertFalse(external_movie_dub_is_available(index, 390634))

    def test_external_movie_mapping_requires_high_confidence_mal_id(self):
        index = (
            {"tmdb_movie:283984": {"mal:25537": {"1": "1"}}},
            set(), set(),
        )
        self.assertFalse(external_movie_dub_is_available(index, 283984))

    def test_episode_mapping_range_does_not_guess(self):
        self.assertTrue(episode_in_mapping_range(4, "1-7"))
        self.assertTrue(episode_in_mapping_range(4, "4"))
        self.assertFalse(episode_in_mapping_range(8, "1-7"))
        self.assertFalse(episode_in_mapping_range(4, "1-"))

    def test_crunchyroll_calendar_is_exact_ptbr_evidence(self):
        html = '''
        <article class="release js-release" data-episode-num="7"
          data-popover-url="/simulcastcalendar/popover/ABCPTBR">
          <h1 class="season-name"><a href="https://www.crunchyroll.com/pt-br/series/CR123/anime">
          <cite itemprop="name">Anime 2ª Temporada (Português (Brasil))</cite></a></h1>
          <meta content="2026-08-19T05:00:00-07:00" itemprop="datePublished">
        </article>
        <article class="release js-release" data-episode-num="8"
          data-popover-url="/simulcastcalendar/popover/ABCJAJP">
          <h1 class="season-name"><a href="https://www.crunchyroll.com/pt-br/series/CR123/anime">
          <cite itemprop="name">Anime 2ª Temporada</cite></a></h1>
        </article>
        '''
        self.assertEqual(
            [{
                "crunchyroll_id": "CR123", "season": 2, "episode": 7,
                "season_title": "Anime 2ª Temporada (Português (Brasil))",
                "published_at": "2026-08-19T05:00:00-07:00",
                "source": "Crunchyroll simulcast calendar PTBR",
            }],
            parse_crunchyroll_calendar(html),
        )

    def test_crunchyroll_evidence_requires_reviewed_series_id(self):
        catalog = {"Anime": {"crunchyroll_id": "CR123"}}
        evidence = {("CR123", 2, 7)}
        self.assertTrue(crunchyroll_dub_is_available(
            catalog, evidence, "Anime", "/anime", 2, 7
        ))
        self.assertFalse(crunchyroll_dub_is_available(
            catalog, evidence, "Anime", "/anime", 2, 8
        ))
        self.assertFalse(crunchyroll_dub_is_available(
            catalog, evidence, "Other", "/other", 2, 7
        ))

    def test_crunchyroll_season_parser_defaults_without_guessing_episode(self):
        self.assertEqual(2, crunchyroll_season_number(
            "Anime 2ª Temporada (Português (Brasil))"
        ))
        self.assertEqual(3, crunchyroll_season_number("Anime Season 3 (Portuguese Dub)"))
        self.assertEqual(1, crunchyroll_season_number("Anime (Português (Brasil))"))

    def test_discord_episode_ranges_are_compact_and_exact(self):
        self.assertEqual("E01–E03, E05, E08–E09", compact_episode_ranges(
            [9, 1, 2, 3, 5, 8, 8]
        ))

    def test_database_schema_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            first = connect_db(path)
            first.close()
            second = connect_db(path)
            tables = {
                row[0]
                for row in second.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertTrue({
                "episode_state", "jobs", "alerts", "actions", "movie_state",
                "movie_dub_observations", "movie_alerts",
            } <= tables)
            second.close()

    @mock.patch("subtitle_orchestrator.post_discord_lines")
    def test_movie_alert_is_formatted_and_marked_sent(self, post):
        with tempfile.TemporaryDirectory() as directory:
            db = connect_db(Path(directory) / "state.sqlite3")
            db.execute(
                "INSERT INTO movie_state VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    7, "Anime Movie", 2024, 123, 8, "/movie.mkv", "sig",
                    1, 0, 1, 1, 0, 1, "missing_dub", 2,
                ),
            )
            db.execute(
                "INSERT INTO movie_alerts VALUES(?,?,?,?,?,NULL)",
                (7, "dub_available_but_missing", 1, 1, 2),
            )
            self.assertEqual(1, send_pending_alerts(db))
            lines = post.call_args.args[0]
            self.assertIn("**Auditoria de idioma por filme**", lines)
            self.assertIn("• Anime Movie (2024)", lines)
            self.assertIsNotNone(db.execute(
                "SELECT last_sent FROM movie_alerts WHERE movie_id=7"
            ).fetchone()[0])
            db.close()

    def test_archive_member_prefers_ptbr_srt_for_exact_episode(self):
        members = [
            "Legendas ptPT/Anime - 02.POR.ass",
            "Legendas ptBR/Fansub Anime - 01.POR.ass",
            "Legendas ptBR/Netflix Anime.S01E02.Brazilian Portuguese.POR.srt",
            "Legendas enUS/Anime.S01E02.ENG.ass",
        ]
        self.assertEqual(
            "Legendas ptBR/Netflix Anime.S01E02.Brazilian Portuguese.POR.srt",
            choose_pt_source_member(members, 2),
        )

    def test_archive_member_can_preserve_separate_english_dialogue(self):
        members = [
            "English Signs/Anime.S01E03.ENG.ass",
            "Legendas enUS/Anime.S01E03.English.srt",
            "Legendas ptBR/Anime.S01E03.POR.srt",
        ]
        self.assertEqual(
            "Legendas enUS/Anime.S01E03.English.srt",
            choose_source_member(members, 3, "en"),
        )

if __name__ == "__main__":
    unittest.main()
