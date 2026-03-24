import unittest
from unittest import mock

from app.web_video_extractor import ResolvedMediaSource, VideoPageExtractionError, VideoPageExtractionUnavailable, resolve_media_source


class FakeYoutubeDL:
    def __init__(self, options):
        self.options = options

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def extract_info(self, url, download=False):
        return {
            "title": "Demo Title",
            "webpage_url": url,
            "formats": [
                {"url": "https://cdn.example.com/video/demo.m3u8", "ext": "m3u8", "protocol": "m3u8_native", "height": 720, "acodec": "aac", "vcodec": "h264"},
                {"url": "https://cdn.example.com/video/demo.mp4", "ext": "mp4", "protocol": "https", "height": 1080, "acodec": "aac", "vcodec": "h264"},
            ],
            "http_headers": {"User-Agent": "TestAgent/1.0", "Referer": url},
        }


class FakeYtDlpModule:
    YoutubeDL = FakeYoutubeDL


class WebVideoExtractorTests(unittest.TestCase):
    def test_resolve_media_source_keeps_direct_url(self) -> None:
        resolved = resolve_media_source("https://cdn.example.com/demo.mp4")
        self.assertIsInstance(resolved, ResolvedMediaSource)
        self.assertEqual(resolved.media_url, "https://cdn.example.com/demo.mp4")
        self.assertFalse(resolved.resolved_from_page)

    @mock.patch("app.web_video_extractor._load_yt_dlp_module", return_value=FakeYtDlpModule())
    def test_resolve_media_source_extracts_play_page(self, load_module: mock.Mock) -> None:
        resolved = resolve_media_source("https://example.com/vod/play/100")

        load_module.assert_called_once()
        self.assertEqual(resolved.media_url, "https://cdn.example.com/video/demo.mp4")
        self.assertEqual(resolved.display_name, "Demo Title")
        self.assertTrue(resolved.resolved_from_page)
        self.assertEqual(resolved.headers["User-Agent"], "TestAgent/1.0")

    @mock.patch("app.web_video_extractor._load_yt_dlp_module", side_effect=VideoPageExtractionUnavailable("missing"))
    def test_resolve_media_source_requires_extractor_for_play_page(self, load_module: mock.Mock) -> None:
        with self.assertRaises(VideoPageExtractionUnavailable):
            resolve_media_source("https://example.com/vod/play/100")
        load_module.assert_called_once()

    @mock.patch("app.web_video_extractor._load_yt_dlp_module", return_value=FakeYtDlpModule())
    def test_resolve_media_source_raises_when_no_media_found(self, load_module: mock.Mock) -> None:
        class EmptyYoutubeDL(FakeYoutubeDL):
            def extract_info(self, url, download=False):
                return {"title": "Empty"}

        class EmptyModule:
            YoutubeDL = EmptyYoutubeDL

        load_module.return_value = EmptyModule()
        with self.assertRaises(VideoPageExtractionError):
            resolve_media_source("https://example.com/vod/play/100")


if __name__ == "__main__":
    unittest.main()
