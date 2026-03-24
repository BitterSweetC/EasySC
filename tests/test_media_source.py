import unittest

from app.media_source import display_name_from_source, explain_generated_media_url, guess_content_type_from_source, is_http_url, is_probable_direct_media_url


class MediaSourceTests(unittest.TestCase):
    def test_is_http_url(self) -> None:
        self.assertTrue(is_http_url("https://example.com/video.mp4"))
        self.assertTrue(is_http_url("http://example.com/video.mp4"))
        self.assertFalse(is_http_url("ftp://example.com/video.mp4"))

    def test_is_probable_direct_media_url(self) -> None:
        self.assertTrue(is_probable_direct_media_url("https://cdn.example.com/demo.mp4"))
        self.assertTrue(is_probable_direct_media_url("https://cdn.example.com/play?source=movie.m3u8"))
        self.assertFalse(is_probable_direct_media_url("https://example.com/vod/play/142114/1/1261601"))

    def test_guess_content_type_from_source(self) -> None:
        self.assertEqual(guess_content_type_from_source("https://cdn.example.com/demo.mp4?token=1"), "video/mp4")

    def test_display_name_from_source(self) -> None:
        self.assertEqual(display_name_from_source("https://cdn.example.com/path/demo%20clip.mp4"), "demo clip.mp4")
        self.assertEqual(display_name_from_source("https://example.com"), "example.com")


    def test_explain_generated_media_url(self) -> None:
        self.assertIn("兼容播放页地址", explain_generated_media_url("http://192.168.1.6:51094/player"))
        self.assertIn("局域网接收地址根地址", explain_generated_media_url("http://192.168.1.6:51094"))
        self.assertIsNone(explain_generated_media_url("https://example.com/vod/play/142114/1/1261601"))


if __name__ == "__main__":
    unittest.main()
