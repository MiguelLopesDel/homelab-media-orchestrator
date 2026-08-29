import unittest

from external_audio_builder import selected_audio_stream


class ExternalAudioBuilderTests(unittest.TestCase):
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
