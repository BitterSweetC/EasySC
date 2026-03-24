import json
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from app.models import DlnaDevice, DlnaService
from app.web_video_extractor import ResolvedMediaSource
from tampermonkey_bridge import TampermonkeyBridgeService, create_bridge_server, filter_forward_headers, resolve_source_with_yt_dlp_fallback


def sample_device() -> DlnaDevice:
    return DlnaDevice(
        location="http://192.168.1.20:8895/description.xml",
        usn="uuid:test-tv::urn:schemas-upnp-org:device:MediaRenderer:1",
        friendly_name="Living Room TV",
        manufacturer="OpenAI",
        model_name="Demo TV",
        av_transport=DlnaService(
            service_type="urn:schemas-upnp-org:service:AVTransport:1",
            control_url="http://192.168.1.20:8895/MediaRenderer/AVTransport/Control",
        ),
        rendering_control=DlnaService(
            service_type="urn:schemas-upnp-org:service:RenderingControl:1",
            control_url="http://192.168.1.20:8895/MediaRenderer/RenderingControl/Control",
        ),
    )


class TampermonkeyBridgeServiceTests(unittest.TestCase):
    def test_filter_forward_headers_is_case_insensitive(self) -> None:
        headers = filter_forward_headers(
            {
                "referer": "https://example.com/watch/1",
                "COOKIE": "sid=1",
                "X-Ignored": "ignored",
            }
        )
        self.assertEqual(
            headers,
            {
                "Referer": "https://example.com/watch/1",
                "Cookie": "sid=1",
            },
        )

    @mock.patch("tampermonkey_bridge.resolve_media_source")
    def test_resolve_source_merges_page_headers(self, resolve_media_source: mock.Mock) -> None:
        resolve_media_source.return_value = ResolvedMediaSource(
            media_url="https://cdn.example.com/video/demo.m3u8",
            display_name="Demo Stream",
            original_url="https://example.com/watch/1",
            headers={"User-Agent": "BridgeUA/1.0"},
            resolved_from_page=True,
        )

        service = TampermonkeyBridgeService()
        resolved = service.resolve_source(
            "https://example.com/watch/1",
            display_name="页面标题",
            headers={"referer": "https://example.com/watch/1", "cookie": "sid=1"},
        )

        self.assertEqual(resolved.display_name, "页面标题")
        self.assertEqual(resolved.media_url, "https://cdn.example.com/video/demo.m3u8")
        self.assertEqual(resolved.headers["Referer"], "https://example.com/watch/1")
        self.assertEqual(resolved.headers["Cookie"], "sid=1")
        self.assertEqual(resolved.headers["User-Agent"], "BridgeUA/1.0")

    @mock.patch("tampermonkey_bridge._load_yt_dlp_module")
    def test_fallback_resolver_marks_separate_streams_for_local_mux(self, load_yt_dlp_module: mock.Mock) -> None:
        class FakeYoutubeDL:
            def __init__(self, options):
                self.options = options

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def extract_info(self, url, download=False):
                return {
                    "title": "Bili Demo",
                    "http_headers": {"Referer": url},
                    "requested_formats": [
                        {
                            "format_id": "video",
                            "url": "https://cdn.example.com/video_only.m4s",
                            "ext": "mp4",
                            "height": 1080,
                            "vcodec": "av01.0.00M.10.0.110.01.01.01.0",
                            "acodec": "none",
                        },
                        {
                            "format_id": "audio",
                            "url": "https://cdn.example.com/audio_only.m4s",
                            "ext": "m4a",
                            "vcodec": "none",
                            "acodec": "mp4a.40.2",
                            "abr": 192,
                        },
                    ],
                    "formats": [],
                }

        class FakeModule:
            YoutubeDL = FakeYoutubeDL

        load_yt_dlp_module.return_value = FakeModule()

        resolved = resolve_source_with_yt_dlp_fallback(
            "https://www.bilibili.com/video/BVdemo",
            display_name="Bili Demo",
            headers={"cookie": "sid=1"},
        )

        self.assertTrue(resolved.requires_local_mux)
        self.assertEqual(resolved.video_url, "https://cdn.example.com/video_only.m4s")
        self.assertEqual(resolved.audio_url, "https://cdn.example.com/audio_only.m4s")
        self.assertEqual(resolved.video_headers["Cookie"], "sid=1")

    @mock.patch("tampermonkey_bridge._load_yt_dlp_module")
    def test_fallback_resolver_respects_quality_cap_for_separate_streams(self, load_yt_dlp_module: mock.Mock) -> None:
        class FakeYoutubeDL:
            def __init__(self, options):
                self.options = options

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def extract_info(self, url, download=False):
                return {
                    "title": "Bili Demo",
                    "http_headers": {"Referer": url},
                    "requested_formats": [
                        {
                            "format_id": "video-1080",
                            "url": "https://cdn.example.com/video_1080.m4s",
                            "ext": "mp4",
                            "height": 1080,
                            "vcodec": "avc1.640028",
                            "acodec": "none",
                        },
                        {
                            "format_id": "audio",
                            "url": "https://cdn.example.com/audio_only.m4s",
                            "ext": "m4a",
                            "vcodec": "none",
                            "acodec": "mp4a.40.2",
                            "abr": 192,
                        },
                    ],
                    "formats": [
                        {
                            "format_id": "video-720",
                            "url": "https://cdn.example.com/video_720.m4s",
                            "ext": "mp4",
                            "height": 720,
                            "vcodec": "avc1.64001f",
                            "acodec": "none",
                        },
                        {
                            "format_id": "video-1080",
                            "url": "https://cdn.example.com/video_1080.m4s",
                            "ext": "mp4",
                            "height": 1080,
                            "vcodec": "avc1.640028",
                            "acodec": "none",
                        },
                        {
                            "format_id": "audio",
                            "url": "https://cdn.example.com/audio_only.m4s",
                            "ext": "m4a",
                            "vcodec": "none",
                            "acodec": "mp4a.40.2",
                            "abr": 192,
                        },
                    ],
                }

        class FakeModule:
            YoutubeDL = FakeYoutubeDL

        load_yt_dlp_module.return_value = FakeModule()

        resolved = resolve_source_with_yt_dlp_fallback(
            "https://www.bilibili.com/video/BVdemo",
            display_name="Bili Demo",
            quality="720p",
        )

        self.assertTrue(resolved.requires_local_mux)
        self.assertEqual(resolved.video_url, "https://cdn.example.com/video_720.m4s")

    @mock.patch("tampermonkey_bridge.resolve_source_with_yt_dlp_fallback")
    @mock.patch("tampermonkey_bridge.resolve_media_source")
    def test_service_resolve_source_uses_fallback_for_non_max_quality(
        self,
        resolve_media_source: mock.Mock,
        resolve_source_with_yt_dlp_fallback: mock.Mock,
    ) -> None:
        resolve_source_with_yt_dlp_fallback.return_value = mock.Mock(
            source_url="https://example.com/watch/1",
            media_url="https://cdn.example.com/video_720.m3u8",
            display_name="Demo",
            headers={},
            resolved_from_page=True,
            requires_local_mux=False,
            video_url="",
            audio_url="",
        )

        service = TampermonkeyBridgeService()
        service.resolve_source("https://example.com/watch/1", quality="720p")

        resolve_media_source.assert_not_called()
        resolve_source_with_yt_dlp_fallback.assert_called_once()
        self.assertEqual(resolve_source_with_yt_dlp_fallback.call_args.kwargs["quality"], "720p")


class BridgeHttpApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = TampermonkeyBridgeService()
        self.server = create_bridge_server(host="127.0.0.1", port=0, service=self.service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        self.service.close()

    def request_json(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers={"Content-Type": "application/json"} if payload is not None else {},
            method=method,
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            body = response.read().decode("utf-8")
            return response.status, json.loads(body)

    def test_health_endpoint_returns_json(self) -> None:
        status, payload = self.request_json("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["service"], "screen-casting-tampermonkey-bridge")

    def test_async_cast_endpoint_returns_task_id(self) -> None:
        self.service.start_cast_task = mock.Mock(
            return_value={
                "ok": True,
                "accepted": True,
                "task_id": "task-123",
                "status": "queued",
                "message": "Cast task accepted.",
            }
        )

        status, payload = self.request_json(
            "POST",
            "/api/cast",
            {
                "device_location": "http://192.168.1.20:8895/description.xml",
                "source_url": "https://example.com/watch/1",
                "quality": "720p",
                "transcode_profile": "smooth",
                "async": True,
            },
        )

        self.assertEqual(status, 202)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["task_id"], "task-123")
        self.service.start_cast_task.assert_called_once()
        self.assertEqual(self.service.start_cast_task.call_args.kwargs["quality"], "720p")
        self.assertEqual(self.service.start_cast_task.call_args.kwargs["transcode_profile"], "smooth")

    @mock.patch("tampermonkey_bridge.set_media_with_title")
    @mock.patch("tampermonkey_bridge.resolve_media_source")
    @mock.patch("tampermonkey_bridge.DlnaController")
    def test_cast_endpoint_uses_bridge_pipeline(
        self,
        dlna_controller_cls: mock.Mock,
        resolve_media_source: mock.Mock,
        set_media_with_title: mock.Mock,
    ) -> None:
        device = sample_device()
        self.service._devices_by_location = {device.location: device}

        resolve_media_source.return_value = ResolvedMediaSource(
            media_url="https://cdn.example.com/video/demo.m3u8",
            display_name="Demo Stream",
            original_url="https://example.com/watch/1",
            headers={"User-Agent": "BridgeUA/1.0"},
            resolved_from_page=True,
        )

        controller = mock.Mock()
        dlna_controller_cls.return_value = controller

        def fake_start_remote(media_url: str, display_name: str, headers: dict | None = None) -> str:
            self.service.http_server._player_url = "http://192.168.1.88:4123/player"
            return "http://192.168.1.88:4123/media/demo.m3u8"

        self.service.http_server.start_remote = mock.Mock(side_effect=fake_start_remote)

        status, payload = self.request_json(
            "POST",
            "/api/cast",
            {
                "device_location": device.location,
                "source_url": "https://example.com/watch/1",
                "display_name": "页面标题",
                "headers": {
                    "referer": "https://example.com/watch/1",
                    "cookie": "sid=1",
                },
            },
        )

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["device"]["display_name"], "Living Room TV (OpenAI Demo TV)")
        self.assertEqual(payload["player_url"], "http://192.168.1.88:4123/player")
        self.assertEqual(payload["resolved_source"]["display_name"], "页面标题")
        self.service.http_server.start_remote.assert_called_once()
        set_media_with_title.assert_called_once_with(
            controller,
            "http://192.168.1.88:4123/media/demo.m3u8",
            "https://cdn.example.com/video/demo.m3u8",
            "页面标题",
        )
        controller.play.assert_called_once_with("1")

    @mock.patch("tampermonkey_bridge.set_media_with_title")
    @mock.patch("tampermonkey_bridge.mux_remote_streams_to_compatible_mp4")
    @mock.patch("tampermonkey_bridge.DlnaController")
    def test_cast_uses_local_muxed_file_when_separate_streams_are_detected(
        self,
        dlna_controller_cls: mock.Mock,
        mux_remote_streams_to_compatible_mp4: mock.Mock,
        set_media_with_title: mock.Mock,
    ) -> None:
        device = sample_device()
        self.service._devices_by_location = {device.location: device}

        controller = mock.Mock()
        dlna_controller_cls.return_value = controller

        muxed_file = Path("cache/compat_media/remote_mux/demo_muxed.mp4")
        mux_remote_streams_to_compatible_mp4.return_value = muxed_file
        self.service.http_server.start = mock.Mock(return_value="http://192.168.1.88:4123/media/demo_muxed.mp4")
        self.service.http_server._player_url = "http://192.168.1.88:4123/player"
        self.service.resolve_source = mock.Mock(
            return_value=mock.Mock(
                source_url="https://www.bilibili.com/video/BVdemo",
                media_url="https://cdn.example.com/video_only.m4s",
                display_name="Bili Demo",
                headers={"Referer": "https://www.bilibili.com/video/BVdemo"},
                resolved_from_page=True,
                requires_local_mux=True,
                video_url="https://cdn.example.com/video_only.m4s",
                audio_url="https://cdn.example.com/audio_only.m4s",
                video_headers={"Referer": "https://www.bilibili.com/video/BVdemo"},
                audio_headers={"Referer": "https://www.bilibili.com/video/BVdemo"},
            )
        )

        payload = self.service.cast(
            device_location=device.location,
            source_url="https://www.bilibili.com/video/BVdemo",
            display_name="Bili Demo",
        )

        self.assertTrue(payload["ok"])
        mux_remote_streams_to_compatible_mp4.assert_called_once()
        self.service.http_server.start.assert_called_once_with(str(muxed_file))
        set_media_with_title.assert_called_once_with(
            controller,
            "http://192.168.1.88:4123/media/demo_muxed.mp4",
            str(muxed_file),
            "Bili Demo",
        )

    @mock.patch("tampermonkey_bridge.set_media_with_title")
    @mock.patch("tampermonkey_bridge.transcode_remote_media_to_compatible_mp4")
    @mock.patch("tampermonkey_bridge.DlnaController")
    def test_cast_uses_local_transcode_when_profile_requests_optimization(
        self,
        dlna_controller_cls: mock.Mock,
        transcode_remote_media_to_compatible_mp4: mock.Mock,
        set_media_with_title: mock.Mock,
    ) -> None:
        device = sample_device()
        self.service._devices_by_location = {device.location: device}

        controller = mock.Mock()
        dlna_controller_cls.return_value = controller

        transcoded_file = Path("cache/compat_media/remote_transcode/demo_quality.mp4")
        transcode_remote_media_to_compatible_mp4.return_value = transcoded_file
        self.service.http_server.start = mock.Mock(return_value="http://192.168.1.88:4123/media/demo_quality.mp4")
        self.service.http_server.start_remote = mock.Mock()
        self.service.http_server._player_url = "http://192.168.1.88:4123/player"
        self.service.resolve_source = mock.Mock(
            return_value=mock.Mock(
                source_url="https://example.com/watch/1",
                media_url="https://cdn.example.com/video/demo.m3u8",
                display_name="Demo Stream",
                headers={"Referer": "https://example.com/watch/1"},
                resolved_from_page=True,
                requires_local_mux=False,
                video_url="",
                audio_url="",
            )
        )

        payload = self.service.cast(
            device_location=device.location,
            source_url="https://example.com/watch/1",
            display_name="Demo Stream",
            transcode_profile="quality",
        )

        self.assertTrue(payload["ok"])
        transcode_remote_media_to_compatible_mp4.assert_called_once_with(
            media_url="https://cdn.example.com/video/demo.m3u8",
            display_name="Demo Stream",
            headers={"Referer": "https://example.com/watch/1"},
            transcode_profile="quality",
        )
        self.service.http_server.start.assert_called_once_with(str(transcoded_file))
        self.service.http_server.start_remote.assert_not_called()
        set_media_with_title.assert_called_once_with(
            controller,
            "http://192.168.1.88:4123/media/demo_quality.mp4",
            str(transcoded_file),
            "Demo Stream",
        )


if __name__ == "__main__":
    unittest.main()
