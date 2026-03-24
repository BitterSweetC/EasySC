import unittest

from app.dlna import build_action_envelope, build_didl_metadata, guess_content_type


class DlnaTests(unittest.TestCase):
    def test_guess_content_type(self) -> None:
        self.assertEqual(guess_content_type("movie.mp4"), "video/mp4")

    def test_guess_content_type_from_url(self) -> None:
        self.assertEqual(guess_content_type("https://cdn.example.com/video/demo.mp4?token=1"), "video/mp4")

    def test_build_metadata(self) -> None:
        metadata = build_didl_metadata("demo & clip.mp4", "http://127.0.0.1/media/demo.mp4")
        self.assertIn("demo &amp; clip.mp4", metadata)
        self.assertIn("object.item.videoItem", metadata)
        self.assertIn("video/mp4", metadata)

    def test_build_metadata_from_url(self) -> None:
        metadata = build_didl_metadata("https://cdn.example.com/path/demo%20clip.mp4?token=1", "https://cdn.example.com/path/demo%20clip.mp4?token=1")
        self.assertIn("demo clip.mp4", metadata)
        self.assertIn("video/mp4", metadata)

    def test_build_action_envelope(self) -> None:
        payload = build_action_envelope(
            "urn:schemas-upnp-org:service:AVTransport:1",
            "Play",
            [("InstanceID", "0"), ("Speed", "1.5")],
        ).decode("utf-8")
        self.assertIn("AVTransport:1", payload)
        self.assertIn("<Speed>1.5</Speed>", payload)
        self.assertIn("<u:Play", payload)


if __name__ == "__main__":
    unittest.main()
