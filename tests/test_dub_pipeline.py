import tempfile
import unittest
from pathlib import Path

from dub_pipeline import (
    AlignmentReport,
    analyse_episode,
    compare_fingerprints,
    dhash,
    episode_spec,
    portuguese_audio_index,
)
from timeline_alignment import TimelinePlan


def media(duration, audios):
    streams = [
        {"codec_type": "audio", "codec_name": "aac", "tags": {"language": language}}
        for language in audios
    ]
    return {"format": {"duration": str(duration)}, "streams": streams}


def rejected_timeline(*_):
    return TimelinePlan(
        "review-required", "cortes não comprovados", (), 4, 0, 0, (),
        0.0, 0.0, None, 0.0, "not-checked",
    )


class DubPipelineTests(unittest.TestCase):
    def test_portuguese_audio_index_is_relative_to_audio_streams(self):
        probe = {
            "streams": [
                {"codec_type": "video"},
                {"codec_type": "audio", "tags": {"language": "jpn"}},
                {"codec_type": "subtitle", "tags": {"language": "por"}},
                {"codec_type": "audio", "tags": {"language": "por"}},
            ]
        }
        self.assertEqual(1, portuguese_audio_index(probe))

    def test_ambiguous_portuguese_audio_requires_review(self):
        self.assertIsNone(portuguese_audio_index(media(10, ["por", "pt-BR"])))

    def test_trusted_dub_release_can_supply_missing_single_audio_tag(self):
        source, target = Path("source.mp4"), Path("target.mkv")
        probes = {source: media(100.0, ["und"]), target: media(100.0, ["jpn"])}
        report = analyse_episode(
            source, target,
            probe_media=probes.__getitem__,
            fingerprints=lambda _: [0] * 16,
            trusted_single_audio_portuguese=True,
        )
        self.assertEqual("high", report.confidence)
        self.assertEqual("trusted-dub-release-with-single-audio", report.source_audio_evidence)

    def test_untagged_audio_without_trusted_release_is_blocked(self):
        source, target = Path("source.mp4"), Path("target.mkv")
        probes = {source: media(100.0, ["und"]), target: media(100.0, ["jpn"])}
        report = analyse_episode(
            source, target,
            probe_media=probes.__getitem__,
            fingerprints=lambda _: self.fail("blocked source must not be decoded"),
        )
        self.assertEqual("blocked", report.confidence)

    def test_dhash_is_stable_and_sensitive_to_horizontal_order(self):
        increasing = bytes(range(9)) * 8
        decreasing = bytes(reversed(range(9))) * 8
        self.assertEqual(0, dhash(increasing))
        self.assertEqual((1 << 64) - 1, dhash(decreasing))

    def test_fingerprint_comparison_reports_matches(self):
        samples, ratio, median = compare_fingerprints([0, 1, 3], [0, 1, 7])
        self.assertEqual(3, samples)
        self.assertEqual(1.0, ratio)
        self.assertEqual(0.0, median)

    def test_high_confidence_requires_duration_audio_and_video_match(self):
        source, target = Path("source.mkv"), Path("target.mkv")
        probes = {source: media(100.0, ["jpn", "por"]), target: media(100.05, ["jpn"])}
        report = analyse_episode(
            source, target,
            probe_media=probes.__getitem__,
            fingerprints=lambda _: [0] * 16,
        )
        self.assertEqual("high", report.confidence)
        self.assertEqual(1, report.source_audio_index)

    def test_duration_divergence_stops_before_expensive_fingerprint(self):
        source, target = Path("source.mkv"), Path("target.mkv")
        probes = {source: media(100.0, ["por"]), target: media(102.0, ["jpn"])}
        report = analyse_episode(
            source, target,
            probe_media=probes.__getitem__,
            fingerprints=lambda _: self.fail("fingerprint should not run"),
            timeline_planner=rejected_timeline,
        )
        self.assertEqual("review-required", report.confidence)

    def test_episode_spec_refuses_non_high_confidence(self):
        source, target = Path("source.mkv"), Path("target.mkv")
        probes = {source: media(100.0, ["por"]), target: media(102.0, ["jpn"])}
        report = analyse_episode(
            source, target,
            probe_media=probes.__getitem__,
            fingerprints=lambda _: [],
            timeline_planner=rejected_timeline,
        )
        with self.assertRaisesRegex(ValueError, "não permite"):
            episode_spec(report)

    def test_piecewise_plan_is_forwarded_to_audio_builder(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "episode.mkv"
            target.write_bytes(b"video-remains-untouched")
            segments = (
                {"input": "source", "start": 0.0, "end": 40.0},
                {"input": "target", "start": 40.0, "end": 42.0},
                {"input": "source", "start": 48.0, "end": 106.0},
            )
            report = AlignmentReport(
                "source.mp4", str(target), 106.0, 100.0, -6.0, 0,
                "trusted-dub-release-with-single-audio", 400, 0.99, 2.0,
                "high", "piecewise proven", "piecewise-visual", segments,
            )
            spec = episode_spec(report)
            self.assertEqual(list(segments), spec["segments"])
            self.assertEqual(target.stat().st_size, spec["target_size"])


if __name__ == "__main__":
    unittest.main()
