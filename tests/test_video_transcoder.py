import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.video_transcoder import build_compatible_output_path, find_ffmpeg, prepare_compatible_mp4


class VideoTranscoderTests(unittest.TestCase):
    def test_build_compatible_output_path_uses_cache_dir_and_mp4_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "demo clip.mkv"
            source.write_bytes(b"123")
            output = build_compatible_output_path(source)
            self.assertEqual(output.suffix, ".mp4")
            self.assertIn("cache", str(output))
            self.assertIn("demo_clip", output.name)

    def test_find_ffmpeg_prefers_env_var(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fake = Path(temp_dir) / "ffmpeg.exe"
            fake.write_bytes(b"binary")
            with mock.patch.dict(os.environ, {"SCREEN_CASTING_FFMPEG": str(fake)}):
                self.assertEqual(find_ffmpeg(), fake)

    def test_prepare_compatible_mp4_reuses_cached_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "cached source.avi"
            source.write_bytes(b"source-data")
            output = build_compatible_output_path(source)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"cached-output")
            with mock.patch("app.video_transcoder.find_ffmpeg", side_effect=AssertionError("ffmpeg should not be called")):
                self.assertEqual(prepare_compatible_mp4(source), output)

    def test_prepare_compatible_mp4_with_real_ffmpeg(self) -> None:
        try:
            ffmpeg = find_ffmpeg()
        except RuntimeError as exc:
            self.skipTest(str(exc))

        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source_demo.avi"
            generate = subprocess.run(
                [
                    str(ffmpeg),
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc=size=160x90:rate=15",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=1000:sample_rate=44100",
                    "-t",
                    "1",
                    "-shortest",
                    "-c:v",
                    "mpeg4",
                    "-q:v",
                    "5",
                    "-c:a",
                    "mp2",
                    str(source),
                ],
                capture_output=True,
                text=True,
                errors="ignore",
            )
            self.assertEqual(generate.returncode, 0, msg=generate.stderr or generate.stdout)
            output = prepare_compatible_mp4(source)
            self.assertTrue(output.exists())
            self.assertEqual(output.suffix, ".mp4")
            self.assertGreater(output.stat().st_size, 0)
            self.assertEqual(prepare_compatible_mp4(source), output)


if __name__ == "__main__":
    unittest.main()
