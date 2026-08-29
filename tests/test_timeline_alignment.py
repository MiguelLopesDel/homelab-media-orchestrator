import unittest

from timeline_alignment import SystemBusyError, ensure_safe_load, plan_from_fingerprints


def splitmix64(value: int) -> int:
    value = (value + 0x9E3779B97F4A7C15) & ((1 << 64) - 1)
    value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & ((1 << 64) - 1)
    value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & ((1 << 64) - 1)
    return value ^ (value >> 31)


def edited_sequences():
    source = [splitmix64(index) for index in range(120)]
    first_gap = [splitmix64(10_000 + index) for index in range(4)]
    second_gap = [splitmix64(20_000 + index) for index in range(6)]
    target = source[:40] + first_gap + source[48:90] + second_gap + source[94:]
    return source, target


class TimelineAlignmentTests(unittest.TestCase):
    def test_busy_host_defers_heavy_alignment(self):
        with self.assertRaises(SystemBusyError):
            ensure_safe_load(load_average=4.0, cpu_count=2)
        ensure_safe_load(load_average=1.0, cpu_count=2)

    def test_piecewise_offsets_and_silent_gaps_create_high_confidence_plan(self):
        source, target = edited_sequences()
        plan = plan_from_fingerprints(
            source, target, len(source) / 2, len(target) / 2,
            dialogue_intervals=[], subtitle_evidence="text-subtitle:por",
            sample_rate=2,
        )
        self.assertEqual("high", plan.confidence)
        self.assertGreater(plan.aligned_coverage, 0.90)
        self.assertTrue(any(segment["input"] == "target" for segment in plan.segments))
        self.assertTrue(any(abs(offset - 2.0) < 0.01 for offset in plan.candidate_offsets))

    def test_dialogue_inside_unmatched_target_gap_requires_review(self):
        source, target = edited_sequences()
        plan = plan_from_fingerprints(
            source, target, len(source) / 2, len(target) / 2,
            dialogue_intervals=[(20.2, 21.8)], subtitle_evidence="text-subtitle:eng",
            sample_rate=2,
        )
        self.assertEqual("review-required", plan.confidence)
        self.assertIn("diálogo", plan.reason)

    def test_gap_without_text_subtitle_requires_review(self):
        source, target = edited_sequences()
        plan = plan_from_fingerprints(
            source, target, len(source) / 2, len(target) / 2,
            dialogue_intervals=None, subtitle_evidence="no-text-subtitle",
            sample_rate=2,
        )
        self.assertEqual("review-required", plan.confidence)
        self.assertIn("legenda textual", plan.reason)

    def test_unrelated_editions_do_not_produce_a_plan(self):
        source = [splitmix64(index) for index in range(100)]
        target = [splitmix64(50_000 + index) for index in range(100)]
        plan = plan_from_fingerprints(
            source, target, 50.0, 50.0,
            dialogue_intervals=[], subtitle_evidence="text-subtitle:por",
            sample_rate=2,
        )
        self.assertEqual("review-required", plan.confidence)
        self.assertFalse(plan.segments)


if __name__ == "__main__":
    unittest.main()
