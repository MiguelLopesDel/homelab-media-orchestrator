import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from subtitle_migration import (
    _format_ass_time,
    _validate_segments,
    parse_ass,
    retime_ass,
    shift_ass,
    summarise_ass,
    verify_sidecar,
)


ASS = """[Script Info]
Title: test

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:02.00,0:00:04.00,main,A,0,0,0,,Primeira
Dialogue: 0,0:01:00.00,0:01:05.00,main,B,0,0,0,,Segunda
Dialogue: 0,0:02:00.00,0:02:04.00,main,C,0,0,0,,Terceira
"""


class SubtitleMigrationTests(unittest.TestCase):
    def test_ass_parser_and_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "episode.ass"
            path.write_text(ASS)
            cues = parse_ass(ASS)
            self.assertEqual((2.0, 4.0), (cues[0].start, cues[0].end))
            self.assertEqual(3, summarise_ass(path).cue_count)

    def test_shift_applies_constant_offset_without_touching_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = root / "source.ass", root / "output.ass"
            source.write_text(ASS)
            shift_ass(source, output, 1.25)
            cues = parse_ass(output.read_text())
            self.assertEqual((3.25, 5.25), (cues[0].start, cues[0].end))
            self.assertIn("Primeira", output.read_text())

    def test_retime_manifest_removes_cut_and_shifts_later_cues(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "source.ass", root / "target.mkv"
            output, manifest = root / "output.ass", root / "edit.json"
            source.write_text(ASS)
            target.touch()
            manifest.write_text(json.dumps({
                "version": 1,
                "source_subtitle": str(source),
                "target_video": str(target),
                "output": str(output),
                "segments": [
                    {"source_start": 0, "source_end": 90, "target_start": 0},
                    {"source_start": 110, "source_end": 180, "target_start": 90},
                ],
            }))
            with mock.patch("subtitle_migration.probe_media", return_value={
                "format": {"duration": "160"}, "streams": []
            }):
                retime_ass(manifest)
            cues = parse_ass(output.read_text())
            self.assertEqual(3, len(cues))
            self.assertEqual((100.0, 104.0), (cues[2].start, cues[2].end))

    def test_overlapping_manifest_segments_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "sobrepõem"):
            _validate_segments([
                {"source_start": 0, "source_end": 10, "target_start": 0},
                {"source_start": 9, "source_end": 20, "target_start": 9},
            ])

    def test_verify_rejects_cue_after_video_end(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subtitle, target = root / "episode.pt-BR.ass", root / "episode.mkv"
            subtitle.write_text(ASS)
            target.touch()
            with mock.patch("subtitle_migration.probe_media", return_value={
                "format": {"duration": "100"}, "streams": []
            }):
                with self.assertRaisesRegex(ValueError, "última fala"):
                    verify_sidecar(subtitle, target)

    def test_ass_time_rounding_is_stable(self):
        self.assertEqual("1:02:03.46", _format_ass_time(3723.456))


if __name__ == "__main__":
    unittest.main()
