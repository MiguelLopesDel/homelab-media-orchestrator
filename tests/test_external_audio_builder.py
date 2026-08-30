import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from external_audio_builder import probe, selected_audio_stream
from dub_pipeline import can_direct_import_dub


class ExternalAudioBuilderTests(unittest.TestCase):
    def test_probe_retains_video_height_for_direct_dub_import(self):
        response = SimpleNamespace(stdout='{"streams":[{"codec_type":"video","height":1080},{"codec_type":"audio","tags":{"language":"por"}}]}')
        with mock.patch("external_audio_builder.subprocess.run", return_value=response) as run:
            data = probe(Path("dubbed.mkv"))
        self.assertIn("height", run.call_args.args[0][4])
        self.assertTrue(can_direct_import_dub(data))

    def test_relative_audio_index_ignores_video_and_subtitle_streams(self):
        probe = {
            "streams": [
                {"codec_type": "video", "index": 0},
                {"codec_type": "audio", "index": 1, "tags": {"language": "jpn"}},
                {"codec_type": "subtitle", "index": 2},
                {"codec_type": "audio", "index": 3, "tags": {"language": "por"}},
            ]
        }
        self.assertEqual(3, selected_audio_stream(probe, 1)["index"])

    def test_invalid_audio_index_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "índice"):
            selected_audio_stream({"streams": []}, 0)


if __name__ == "__main__":
    unittest.main()
