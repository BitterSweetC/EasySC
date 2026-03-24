import io
import json
import unittest
import urllib.request

from PIL import Image

from app.mirror_server import ScreenMirrorServer
from app.screen_capture import QUALITY_PRESETS, ScreenCaptureConfig


def make_dummy_jpeg() -> bytes:
    image = Image.new("RGB", (32, 24), color=(12, 34, 56))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    return buffer.getvalue()


class MirrorServerTests(unittest.TestCase):
    def test_preset_config(self) -> None:
        config = ScreenCaptureConfig.from_preset("HD")
        self.assertEqual(config.scale, QUALITY_PRESETS["HD"]["scale"])
        self.assertEqual(config.fps, QUALITY_PRESETS["HD"]["fps"])

    def test_mirror_server_serves_index_snapshot_and_health(self) -> None:
        server = ScreenMirrorServer(capture_func=make_dummy_jpeg)
        url = server.start(ScreenCaptureConfig.from_preset("Smooth"))
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                html = response.read().decode("utf-8")
                self.assertEqual(response.status, 200)
                self.assertIn("/stream.mjpg", html)

            with urllib.request.urlopen(f"{url}snapshot.jpg", timeout=5) as response:
                payload = response.read()
                self.assertEqual(response.status, 200)
                self.assertGreater(len(payload), 10)
                self.assertEqual(payload[:2], b"\xff\xd8")

            with urllib.request.urlopen(f"{url}health", timeout=5) as response:
                info = json.loads(response.read().decode("utf-8"))
                self.assertTrue(info["ready"])
        finally:
            server.stop()


if __name__ == "__main__":
    unittest.main()
