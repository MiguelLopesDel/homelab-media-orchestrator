import unittest

from subtitle_policy import EpisodeLanguageFacts, LanguagePolicy, evaluate_episode


class SubtitlePolicyTests(unittest.TestCase):
    def facts(self, **changes):
        values = dict(
            age_seconds=3 * 24 * 3600,
            has_pt_audio=False,
            has_pt_subtitle=False,
            has_pt_external_subtitle=False,
            has_english_text_subtitle=True,
            has_english_bitmap_subtitle=False,
            has_english_external_subtitle=False,
            dub_available=False,
            bazarr_attempts=2,
            seconds_since_bazarr_attempt=24 * 3600,
            translation_status=None,
        )
        values.update(changes)
        return EpisodeLanguageFacts(**values)

    def test_portuguese_audio_finishes_without_subtitle(self):
        decision = evaluate_episode(self.facts(has_pt_audio=True), LanguagePolicy())
        self.assertEqual("dubbed", decision.state)
        self.assertIsNone(decision.action)
        self.assertEqual((), decision.alerts)

    def test_confirmed_dub_skips_all_subtitle_work_and_reports_dub_gap(self):
        decision = evaluate_episode(
            self.facts(has_pt_subtitle=True, dub_available=True), LanguagePolicy()
        )
        self.assertEqual("missing_dub", decision.state)
        self.assertIsNone(decision.action)
        self.assertEqual(("dub_available_but_missing",), decision.alerts)

    def test_fresh_confirmed_dub_waits_before_alerting(self):
        decision = evaluate_episode(
            self.facts(
                age_seconds=2 * 3600,
                dub_available=True,
                has_pt_subtitle=False,
            ),
            LanguagePolicy(),
        )
        self.assertEqual("missing_dub", decision.state)
        self.assertIsNone(decision.action)
        self.assertEqual((), decision.alerts)

    def test_separate_portuguese_subtitle_is_materialized_before_bazarr(self):
        decision = evaluate_episode(
            self.facts(has_pt_external_subtitle=True), LanguagePolicy()
        )
        self.assertEqual("portuguese_subtitle_source", decision.state)
        self.assertEqual("materialize_portuguese_subtitle", decision.action)
        self.assertNotIn("missing_portuguese", decision.alerts)

    def test_separate_english_subtitle_is_preserved_before_provider_search(self):
        decision = evaluate_episode(
            self.facts(
                has_english_text_subtitle=False,
                has_english_external_subtitle=True,
                bazarr_attempts=0,
            ),
            LanguagePolicy(),
        )
        self.assertEqual("english_subtitle_source", decision.state)
        self.assertEqual("materialize_english_subtitle", decision.action)

    def test_bazarr_gets_two_spaced_attempts_before_translation(self):
        policy = LanguagePolicy()
        first = evaluate_episode(
            self.facts(age_seconds=24 * 3600, bazarr_attempts=0), policy
        )
        self.assertEqual("search_bazarr", first.action)

        wait = evaluate_episode(
            self.facts(
                age_seconds=36 * 3600,
                bazarr_attempts=1,
                seconds_since_bazarr_attempt=60,
            ),
            policy,
        )
        self.assertEqual("wait_bazarr", wait.state)

        second = evaluate_episode(
            self.facts(
                age_seconds=40 * 3600,
                bazarr_attempts=1,
                seconds_since_bazarr_attempt=13 * 3600,
            ),
            policy,
        )
        self.assertEqual("search_bazarr", second.action)

    def test_text_translation_only_after_bazarr_and_delay(self):
        decision = evaluate_episode(self.facts(), LanguagePolicy())
        self.assertEqual("queue_text_translation", decision.action)

    def test_bitmap_translation_is_queued_for_notebook(self):
        decision = evaluate_episode(
            self.facts(
                has_english_text_subtitle=False,
                has_english_bitmap_subtitle=True,
            ),
            LanguagePolicy(),
        )
        self.assertEqual("queue_ocr_translation", decision.action)

    def test_queued_translation_is_not_duplicated(self):
        decision = evaluate_episode(
            self.facts(translation_status="queued"), LanguagePolicy()
        )
        self.assertEqual("translation_queued", decision.state)
        self.assertIsNone(decision.action)

    def test_failed_translation_is_not_retried_automatically(self):
        decision = evaluate_episode(
            self.facts(translation_status="failed"), LanguagePolicy()
        )
        self.assertEqual("translation_failed", decision.state)
        self.assertIsNone(decision.action)

    def test_no_english_source_is_reported_without_spending_api_quota(self):
        decision = evaluate_episode(
            self.facts(
                has_english_text_subtitle=False,
                has_english_bitmap_subtitle=False,
            ),
            LanguagePolicy(),
        )
        self.assertEqual("blocked_no_source", decision.state)
        self.assertIsNone(decision.action)
        self.assertIn("missing_portuguese", decision.alerts)


if __name__ == "__main__":
    unittest.main()
