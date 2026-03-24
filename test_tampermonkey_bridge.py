import json
import io
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from app.models import DlnaDevice, DlnaService
import app.web_video_extractor as web_video_extractor
from app.web_video_extractor import ResolvedMediaSource
import tampermonkey_bridge
from tampermonkey_bridge import (
    BridgeTask,
    LiveHlsTranscodeSession,
    TampermonkeyBridgeService,
    _build_video_filter_chain,
    _build_transcode_attempts,
    create_bridge_server,
    detect_preferred_h264_hardware_encoder,
    filter_forward_headers,
    mux_remote_streams_to_compatible_mp4,
    resolve_source_with_yt_dlp_fallback,
    transcode_remote_media_to_compatible_mp4,
)


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
    def setUp(self) -> None:
        tampermonkey_bridge._FFMPEG_ENCODER_LIST_CACHE.clear()
        tampermonkey_bridge._FFMPEG_HW_ENCODER_CACHE.clear()

    @staticmethod
    def _make_binary_response(payload: bytes) -> mock.MagicMock:
        response = mock.MagicMock()
        stream = io.BytesIO(payload)
        response.read.side_effect = stream.read
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        return response

    def test_build_live_hls_output_command_uses_compatible_hls_flags(self) -> None:
        command, playlist_path = tampermonkey_bridge._build_live_hls_output_command(
            Path(r"C:\ffmpeg\bin\ffmpeg.exe"),
            ["-i", "http://127.0.0.1:4123/media/demo.m3u8"],
            ["-map", "0:v:0", "-map", "0:a:0?"],
            Path(r"C:\temp\live"),
            "quality",
        )

        self.assertEqual(playlist_path, Path(r"C:\temp\live\index.m3u8"))
        flag_index = command.index("-hls_flags")
        self.assertEqual(command[flag_index + 1], tampermonkey_bridge.LIVE_HLS_FLAGS)
        self.assertNotIn("temp_file", command[flag_index + 1])

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
            display_name="椤甸潰鏍囬",
            headers={"referer": "https://example.com/watch/1", "cookie": "sid=1"},
        )

        self.assertEqual(resolved.display_name, "椤甸潰鏍囬")
        self.assertEqual(resolved.media_url, "https://cdn.example.com/video/demo.m3u8")
        self.assertEqual(resolved.headers["Referer"], "https://example.com/watch/1")
        self.assertEqual(resolved.headers["Cookie"], "sid=1")
        self.assertEqual(resolved.headers["User-Agent"], "BridgeUA/1.0")

    @mock.patch("app.web_video_extractor._load_yt_dlp_module")
    def test_web_video_extractor_retries_without_format_selector_when_selector_is_unavailable(
        self,
        load_yt_dlp_module: mock.Mock,
    ) -> None:
        option_calls: list[dict[str, object]] = []

        class FakeYoutubeDL:
            def __init__(self, options):
                self.options = dict(options)
                option_calls.append(self.options)

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def extract_info(self, url, download=False):
                if self.options.get("format"):
                    raise Exception("Requested format is not available. Use --list-formats for a list of available formats")
                return {
                    "title": "Bili Demo",
                    "formats": [
                        {
                            "format_id": "muxed",
                            "url": "https://cdn.example.com/video_1080.mp4",
                            "ext": "mp4",
                            "height": 1080,
                            "vcodec": "avc1.640028",
                            "acodec": "mp4a.40.2",
                        }
                    ],
                }

        class FakeModule:
            YoutubeDL = FakeYoutubeDL

        load_yt_dlp_module.return_value = FakeModule()

        resolved = web_video_extractor.resolve_media_source("https://www.bilibili.com/video/BVdemo")

        self.assertEqual(len(option_calls), 2)
        self.assertIn("format", option_calls[0])
        self.assertNotIn("format", option_calls[1])
        self.assertIn("logger", option_calls[0])
        self.assertEqual(resolved.media_url, "https://cdn.example.com/video_1080.mp4")
        self.assertEqual(resolved.display_name, "Bili Demo")

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

    @mock.patch("tampermonkey_bridge._load_yt_dlp_module")
    def test_fallback_resolver_uses_best_available_below_1440p_cap(self, load_yt_dlp_module: mock.Mock) -> None:
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
            quality="1440p",
        )

        self.assertTrue(resolved.requires_local_mux)
        self.assertEqual(resolved.video_url, "https://cdn.example.com/video_1080.m4s")

    @mock.patch("tampermonkey_bridge._load_yt_dlp_module")
    def test_fallback_resolver_retries_without_format_selector_when_selector_is_unavailable(
        self,
        load_yt_dlp_module: mock.Mock,
    ) -> None:
        option_calls: list[dict[str, object]] = []

        class FakeYoutubeDL:
            def __init__(self, options):
                self.options = dict(options)
                option_calls.append(self.options)

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def extract_info(self, url, download=False):
                if self.options.get("format"):
                    raise Exception("Requested format is not available. Use --list-formats for a list of available formats")
                return {
                    "title": "Bili Demo",
                    "http_headers": {"Referer": url},
                    "formats": [
                        {
                            "format_id": "video",
                            "url": "https://cdn.example.com/video_only.m4s",
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
        )

        self.assertEqual(len(option_calls), 2)
        self.assertIn("format", option_calls[0])
        self.assertNotIn("format", option_calls[1])
        self.assertTrue(resolved.requires_local_mux)
        self.assertEqual(resolved.video_url, "https://cdn.example.com/video_only.m4s")
        self.assertEqual(resolved.audio_url, "https://cdn.example.com/audio_only.m4s")

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

    @mock.patch("tampermonkey_bridge.resolve_source_with_yt_dlp_fallback")
    @mock.patch("tampermonkey_bridge.resolve_media_source")
    def test_service_resolve_source_prefers_mux_aware_fallback_for_bilibili_pages(
        self,
        resolve_media_source: mock.Mock,
        resolve_source_with_yt_dlp_fallback: mock.Mock,
    ) -> None:
        resolve_source_with_yt_dlp_fallback.return_value = mock.Mock(
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

        service = TampermonkeyBridgeService()
        resolved = service.resolve_source("https://www.bilibili.com/video/BVdemo", display_name="Bili Demo")

        resolve_media_source.assert_not_called()
        resolve_source_with_yt_dlp_fallback.assert_called_once()
        self.assertEqual(resolve_source_with_yt_dlp_fallback.call_args.kwargs["quality"], "max")
        self.assertTrue(resolved.requires_local_mux)
        self.assertEqual(resolved.video_url, "https://cdn.example.com/video_only.m4s")
        self.assertEqual(resolved.audio_url, "https://cdn.example.com/audio_only.m4s")

    @mock.patch("tampermonkey_bridge.resolve_source_with_yt_dlp_fallback")
    @mock.patch("tampermonkey_bridge.resolve_media_source")
    def test_service_resolve_source_falls_back_to_direct_extractor_when_mux_aware_fallback_fails(
        self,
        resolve_media_source: mock.Mock,
        resolve_source_with_yt_dlp_fallback: mock.Mock,
    ) -> None:
        resolve_source_with_yt_dlp_fallback.side_effect = web_video_extractor.VideoPageExtractionError("fallback failed")
        resolve_media_source.return_value = ResolvedMediaSource(
            media_url="https://cdn.example.com/video/demo.m3u8",
            display_name="Bili Demo",
            original_url="https://www.bilibili.com/video/BVdemo",
            headers={"User-Agent": "BridgeUA/1.0"},
            resolved_from_page=True,
        )

        service = TampermonkeyBridgeService()
        resolved = service.resolve_source(
            "https://www.bilibili.com/video/BVdemo",
            display_name="Bili Demo",
            headers={"referer": "https://www.bilibili.com/video/BVdemo"},
        )

        resolve_source_with_yt_dlp_fallback.assert_called_once()
        resolve_media_source.assert_called_once_with("https://www.bilibili.com/video/BVdemo")
        self.assertEqual(resolved.media_url, "https://cdn.example.com/video/demo.m3u8")
        self.assertEqual(resolved.headers["Referer"], "https://www.bilibili.com/video/BVdemo")
        self.assertEqual(resolved.headers["User-Agent"], "BridgeUA/1.0")
        self.assertFalse(resolved.requires_local_mux)

    def test_build_transcode_attempts_keep_smooth_without_hardware(self) -> None:
        attempts = _build_transcode_attempts("smooth", hardware_encoder="")
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].effective_profile, "smooth")
        self.assertEqual(attempts[0].encoder_name, "libx264")
        self.assertTrue(attempts[0].smooth_fallback)
        self.assertFalse(attempts[0].downgraded)

    def test_build_video_filter_chain_targets_stable_30fps_for_smooth(self) -> None:
        chain = _build_video_filter_chain("smooth", encoder_name="libx264")
        self.assertEqual(
            chain,
            "scale=w='min(960,iw)':h='min(540,ih)':force_original_aspect_ratio=decrease:flags=fast_bilinear,"
            "scale=trunc(iw/2)*2:trunc(ih/2)*2,fps=30000/1001",
        )

    def test_build_video_filter_chain_caps_smooth_resolution_for_hardware(self) -> None:
        chain = _build_video_filter_chain("smooth", encoder_name="h264_qsv")
        self.assertEqual(
            chain,
            "scale=w='min(1280,iw)':h='min(720,ih)':force_original_aspect_ratio=decrease:flags=fast_bilinear,"
            "scale=trunc(iw/2)*2:trunc(ih/2)*2,fps=30000/1001,format=nv12",
        )

    def test_build_live_hls_output_command_uses_realtime_friendly_smooth_settings(self) -> None:
        command, _ = tampermonkey_bridge._build_live_hls_output_command(
            Path(r"C:\ffmpeg\bin\ffmpeg.exe"),
            ["-i", "http://127.0.0.1:4123/media/demo.m3u8"],
            ["-map", "0:v:0", "-map", "0:a:0?"],
            Path(r"C:\temp\live"),
            "smooth",
            encoder_name="libx264",
            smooth_fallback=True,
        )

        self.assertIn("-preset", command)
        self.assertEqual(command[command.index("-preset") + 1], "superfast")
        self.assertEqual(command[command.index("-crf") + 1], "25")
        self.assertEqual(command[command.index("-b:a") + 1], "96k")
        self.assertEqual(command[command.index("-tune") + 1], "zerolatency")
        self.assertEqual(command[command.index("-g") + 1], "60")
        self.assertEqual(command[command.index("-keyint_min") + 1], "60")
        self.assertEqual(command[command.index("-sc_threshold") + 1], "0")

    def test_live_hls_session_reports_buffered_duration_from_playlist(self) -> None:
        process = mock.Mock()
        process.stderr = None
        with tempfile.TemporaryDirectory() as temp_dir:
            playlist_path = Path(temp_dir) / "index.m3u8"
            playlist_path.write_text(
                "#EXTM3U\n#EXTINF:2.0,\nsegment_00001.ts\n#EXTINF:2.0,\nsegment_00002.ts\n#EXTINF:2.0,\nsegment_00003.ts\n",
                encoding="utf-8",
            )
            session = LiveHlsTranscodeSession(
                output_dir=Path(temp_dir),
                playlist_path=playlist_path,
                process=process,
                proxy_servers=[],
            )
            try:
                self.assertAlmostEqual(session.buffered_duration_seconds(), 6.0)
            finally:
                session.stop(cleanup=False)

    @mock.patch("tampermonkey_bridge.start_live_hls_transcode_session")
    @mock.patch("tampermonkey_bridge.set_media_with_title")
    @mock.patch("tampermonkey_bridge.resolve_media_source")
    @mock.patch("tampermonkey_bridge.DlnaController")
    def test_service_uses_live_hls_transcode_for_smooth_hls(
        self,
        dlna_controller_cls: mock.Mock,
        resolve_media_source: mock.Mock,
        set_media_with_title: mock.Mock,
        start_live_hls_transcode_session: mock.Mock,
    ) -> None:
        device = sample_device()
        service = TampermonkeyBridgeService()
        service._devices_by_location = {device.location: device}

        resolve_media_source.return_value = ResolvedMediaSource(
            media_url="https://cdn.example.com/video/demo.m3u8",
            display_name="Demo Stream",
            original_url="https://example.com/watch/1",
            headers={"User-Agent": "BridgeUA/1.0"},
            resolved_from_page=True,
        )

        controller = mock.Mock()
        dlna_controller_cls.return_value = controller

        live_session = mock.Mock()
        live_session.playlist_path = Path(r"C:\temp\live\index.m3u8")
        start_live_hls_transcode_session.return_value = live_session

        def fake_start_hls(playlist_path: str, display_name: str) -> str:
            service.http_server._player_url = "http://192.168.1.88:4123/player"
            return "http://192.168.1.88:4123/media/live/index.m3u8"

        service.http_server.start_hls = mock.Mock(side_effect=fake_start_hls)

        payload = service.cast(
            device_location=device.location,
            source_url="https://example.com/watch/1",
            display_name="Page Title",
            headers={"referer": "https://example.com/watch/1"},
            transcode_profile="smooth",
        )

        start_live_hls_transcode_session.assert_called_once_with(
            media_url="https://cdn.example.com/video/demo.m3u8",
            display_name="Page Title",
            headers={
                "Referer": "https://example.com/watch/1",
                "User-Agent": "BridgeUA/1.0",
            },
            transcode_profile="smooth",
            startup_buffer_seconds=15.0,
        )
        service.http_server.start_hls.assert_called_once_with(str(live_session.playlist_path), "Page Title")
        set_media_with_title.assert_called_once_with(
            controller,
            "http://192.168.1.88:4123/media/live/index.m3u8",
            str(live_session.playlist_path),
            "Page Title",
        )
        controller.play.assert_called_once_with("1")
        self.assertEqual(payload["player_url"], "http://192.168.1.88:4123/player")
        self.assertIs(service.current_live_transcode_session, live_session)

        service.stop()
        live_session.stop.assert_called_once_with()

    @mock.patch("tampermonkey_bridge.set_media_with_title")
    @mock.patch("tampermonkey_bridge.transcode_remote_media_to_compatible_mp4")
    @mock.patch("tampermonkey_bridge.start_live_hls_transcode_session")
    @mock.patch("tampermonkey_bridge.resolve_media_source")
    @mock.patch("tampermonkey_bridge.DlnaController")
    def test_service_uses_full_transcode_for_quality_hls(
        self,
        dlna_controller_cls: mock.Mock,
        resolve_media_source: mock.Mock,
        start_live_hls_transcode_session: mock.Mock,
        transcode_remote_media_to_compatible_mp4: mock.Mock,
        set_media_with_title: mock.Mock,
    ) -> None:
        device = sample_device()
        service = TampermonkeyBridgeService()
        service._devices_by_location = {device.location: device}

        resolve_media_source.return_value = ResolvedMediaSource(
            media_url="https://cdn.example.com/video/demo.m3u8",
            display_name="Demo Stream",
            original_url="https://example.com/watch/1",
            headers={"User-Agent": "BridgeUA/1.0"},
            resolved_from_page=True,
        )

        controller = mock.Mock()
        dlna_controller_cls.return_value = controller

        transcoded_file = Path(r"C:\temp\demo_quality.mp4")
        transcode_remote_media_to_compatible_mp4.return_value = transcoded_file
        service.http_server.start = mock.Mock(return_value="http://192.168.1.88:4123/media/demo_quality.mp4")
        service.http_server.start_hls = mock.Mock()
        service.http_server._player_url = "http://192.168.1.88:4123/player"

        payload = service.cast(
            device_location=device.location,
            source_url="https://example.com/watch/1",
            display_name="Page Title",
            headers={"referer": "https://example.com/watch/1"},
            transcode_profile="quality",
        )

        start_live_hls_transcode_session.assert_not_called()
        transcode_remote_media_to_compatible_mp4.assert_called_once_with(
            media_url="https://cdn.example.com/video/demo.m3u8",
            display_name="Page Title",
            headers={
                "Referer": "https://example.com/watch/1",
                "User-Agent": "BridgeUA/1.0",
            },
            transcode_profile="quality",
        )
        service.http_server.start.assert_called_once_with(str(transcoded_file))
        service.http_server.start_hls.assert_not_called()
        set_media_with_title.assert_called_once_with(
            controller,
            "http://192.168.1.88:4123/media/demo_quality.mp4",
            str(transcoded_file),
            "Page Title",
        )
        controller.play.assert_called_once_with("1")
        self.assertEqual(payload["player_url"], "http://192.168.1.88:4123/player")
        self.assertIsNone(service.current_live_transcode_session)


    @mock.patch("tampermonkey_bridge._try_fast_compatible_hls_remux")
    @mock.patch("tampermonkey_bridge._run_transcode_command")
    @mock.patch("tampermonkey_bridge.find_ffmpeg")
    @mock.patch("tampermonkey_bridge.MediaHttpServer")
    def test_remote_transcode_uses_local_proxy_for_ffmpeg_input(
        self,
        media_http_server_cls: mock.Mock,
        find_ffmpeg: mock.Mock,
        run_transcode_command: mock.Mock,
        try_fast_compatible_hls_remux: mock.Mock,
    ) -> None:
        ffmpeg_path = Path(r"C:\ffmpeg\bin\ffmpeg.exe")
        proxied_media_url = "http://127.0.0.1:4123/media/demo.m3u8"
        transcoded_file = Path(r"C:\temp\demo_quality.mp4")
        proxy_server = mock.Mock()

        media_http_server_cls.return_value = proxy_server
        proxy_server.start_remote.return_value = proxied_media_url
        find_ffmpeg.return_value = ffmpeg_path
        run_transcode_command.return_value = transcoded_file
        try_fast_compatible_hls_remux.return_value = None

        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.object(tampermonkey_bridge, "REMOTE_TRANSCODE_CACHE_DIR", Path(temp_dir)):
                result = transcode_remote_media_to_compatible_mp4(
                    media_url="https://cdn.example.com/video/demo.m3u8?token=abc",
                    display_name="Demo Stream",
                    headers={
                        "referer": "https://example.com/watch/1",
                        "cookie": "sid=1",
                        "x-ignored": "ignored",
                    },
                    transcode_profile="quality",
                )

        self.assertEqual(result, transcoded_file)
        media_http_server_cls.assert_called_once_with()
        proxy_server.start_remote.assert_called_once_with(
            "https://cdn.example.com/video/demo.m3u8?token=abc",
            "Demo Stream",
            headers={
                "Referer": "https://example.com/watch/1",
                "Cookie": "sid=1",
            },
        )
        run_transcode_command.assert_called_once()
        try_fast_compatible_hls_remux.assert_called_once()
        self.assertEqual(run_transcode_command.call_args.args[0], ffmpeg_path)
        self.assertEqual(run_transcode_command.call_args.args[1], ["-i", proxied_media_url])
        self.assertEqual(run_transcode_command.call_args.args[2], ["-map", "0:v:0", "-map", "0:a:0?"])
        self.assertEqual(run_transcode_command.call_args.args[4], "quality")
        self.assertEqual(run_transcode_command.call_args.args[3].parent, Path(temp_dir))
        proxy_server.stop.assert_called_once_with()

    @mock.patch("tampermonkey_bridge._try_fast_compatible_hls_remux")
    @mock.patch("tampermonkey_bridge._run_transcode_command")
    @mock.patch("tampermonkey_bridge.find_ffmpeg")
    @mock.patch("tampermonkey_bridge.MediaHttpServer")
    def test_remote_transcode_prefers_fast_hls_remux_when_possible(
        self,
        media_http_server_cls: mock.Mock,
        find_ffmpeg: mock.Mock,
        run_transcode_command: mock.Mock,
        try_fast_compatible_hls_remux: mock.Mock,
    ) -> None:
        ffmpeg_path = Path(r"C:\ffmpeg\bin\ffmpeg.exe")
        proxied_media_url = "http://127.0.0.1:4123/media/demo.m3u8"
        fast_file = Path(r"C:\temp\demo_quality_fast.mp4")
        proxy_server = mock.Mock()

        media_http_server_cls.return_value = proxy_server
        proxy_server.start_remote.return_value = proxied_media_url
        find_ffmpeg.return_value = ffmpeg_path
        try_fast_compatible_hls_remux.return_value = fast_file

        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.object(tampermonkey_bridge, "REMOTE_TRANSCODE_CACHE_DIR", Path(temp_dir)):
                result = transcode_remote_media_to_compatible_mp4(
                    media_url="https://cdn.example.com/video/demo.m3u8?token=abc",
                    display_name="Demo Stream",
                    headers={"referer": "https://example.com/watch/1"},
                    transcode_profile="quality",
                )

        self.assertEqual(result, fast_file)
        try_fast_compatible_hls_remux.assert_called_once()
        run_transcode_command.assert_not_called()
        proxy_server.stop.assert_called_once_with()

    @mock.patch("tampermonkey_bridge.urllib.request.urlopen")
    @mock.patch("tampermonkey_bridge._run_transcode_command")
    @mock.patch("tampermonkey_bridge._try_fast_compatible_local_stream_mux")
    @mock.patch("tampermonkey_bridge.find_ffmpeg")
    def test_quality_mux_prefers_fast_local_stream_mux_when_streams_are_already_compatible(
        self,
        find_ffmpeg: mock.Mock,
        try_fast_compatible_local_stream_mux: mock.Mock,
        run_transcode_command: mock.Mock,
        urlopen: mock.Mock,
    ) -> None:
        ffmpeg_path = Path(r"C:\ffmpeg\bin\ffmpeg.exe")
        fast_file = Path(r"C:\temp\demo_quality_fast.mp4")

        find_ffmpeg.return_value = ffmpeg_path
        try_fast_compatible_local_stream_mux.return_value = fast_file
        urlopen.side_effect = [
            self._make_binary_response(b"video-bytes"),
            self._make_binary_response(b"audio-bytes"),
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.object(tampermonkey_bridge, "REMOTE_MUX_CACHE_DIR", Path(temp_dir)):
                result = mux_remote_streams_to_compatible_mp4(
                    video_url="https://cdn.example.com/video_1080.m4s",
                    audio_url="https://cdn.example.com/audio_only.m4s",
                    display_name="Bili Demo",
                    video_headers={"referer": "https://www.bilibili.com/video/BVdemo"},
                    audio_headers={"referer": "https://www.bilibili.com/video/BVdemo"},
                    transcode_profile="quality",
                )

        self.assertEqual(result, fast_file)
        try_fast_compatible_local_stream_mux.assert_called_once()
        self.assertEqual(try_fast_compatible_local_stream_mux.call_args.args[0], ffmpeg_path)
        self.assertEqual(try_fast_compatible_local_stream_mux.call_args.args[3].suffix, ".mp4")
        run_transcode_command.assert_not_called()
        self.assertEqual(urlopen.call_count, 2)

    @mock.patch("tampermonkey_bridge.urllib.request.urlopen")
    @mock.patch("tampermonkey_bridge._run_transcode_command")
    @mock.patch("tampermonkey_bridge._try_fast_compatible_local_stream_mux")
    @mock.patch("tampermonkey_bridge.find_ffmpeg")
    def test_quality_mux_falls_back_to_transcode_when_fast_local_stream_mux_is_not_possible(
        self,
        find_ffmpeg: mock.Mock,
        try_fast_compatible_local_stream_mux: mock.Mock,
        run_transcode_command: mock.Mock,
        urlopen: mock.Mock,
    ) -> None:
        ffmpeg_path = Path(r"C:\ffmpeg\bin\ffmpeg.exe")
        transcoded_file = Path(r"C:\temp\demo_quality.mp4")

        find_ffmpeg.return_value = ffmpeg_path
        try_fast_compatible_local_stream_mux.return_value = None
        run_transcode_command.return_value = transcoded_file
        urlopen.side_effect = [
            self._make_binary_response(b"video-bytes"),
            self._make_binary_response(b"audio-bytes"),
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.object(tampermonkey_bridge, "REMOTE_MUX_CACHE_DIR", Path(temp_dir)):
                result = mux_remote_streams_to_compatible_mp4(
                    video_url="https://cdn.example.com/video_av1.m4s",
                    audio_url="https://cdn.example.com/audio_only.m4s",
                    display_name="Bili Demo",
                    video_headers={"referer": "https://www.bilibili.com/video/BVdemo"},
                    audio_headers={"referer": "https://www.bilibili.com/video/BVdemo"},
                    transcode_profile="quality",
                )

        self.assertEqual(result, transcoded_file)
        try_fast_compatible_local_stream_mux.assert_called_once()
        run_transcode_command.assert_called_once()
        self.assertEqual(run_transcode_command.call_args.args[0], ffmpeg_path)
        self.assertEqual(run_transcode_command.call_args.args[1][0], "-i")
        self.assertEqual(run_transcode_command.call_args.args[2], ["-map", "0:v:0", "-map", "1:a:0"])
        self.assertEqual(run_transcode_command.call_args.args[4], "quality")
        self.assertEqual(urlopen.call_count, 2)

    def test_get_task_is_not_blocked_by_playback_lock(self) -> None:
        service = TampermonkeyBridgeService()
        with service._lock:
            service._tasks["task-1"] = BridgeTask(
                task_id="task-1",
                status="transcoding",
                message="Rendering smoother 60fps playback with hardware encoding...",
            )

        payload: dict[str, object] = {}

        service._playback_lock.acquire()
        try:
            thread = threading.Thread(target=lambda: payload.update(result=service.get_task("task-1")))
            thread.start()
            thread.join(timeout=0.5)
            self.assertFalse(thread.is_alive(), "get_task should not wait for playback work")
        finally:
            service._playback_lock.release()
            if "thread" in locals() and thread.is_alive():
                thread.join(timeout=1)

        self.assertIsNotNone(payload.get("result"))
        self.assertEqual(payload["result"]["task_id"], "task-1")  # type: ignore[index]

    @mock.patch("tampermonkey_bridge.subprocess.run")
    def test_detect_preferred_hardware_encoder_skips_unusable_nvenc_and_uses_qsv(self, subprocess_run: mock.Mock) -> None:
        ffmpeg_path = Path(r"C:\ffmpeg\bin\ffmpeg.exe")

        subprocess_run.side_effect = [
            mock.Mock(
                returncode=0,
                stdout="\n".join(
                    [
                        " V..... h264_nvenc           NVIDIA NVENC H.264 encoder (codec h264)",
                        " V..... h264_qsv             H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10 (Intel Quick Sync Video acceleration) (codec h264)",
                    ]
                ),
                stderr="",
            ),
            mock.Mock(returncode=1, stdout="", stderr="nvenc unavailable"),
            mock.Mock(returncode=0, stdout="", stderr=""),
        ]

        encoder = detect_preferred_h264_hardware_encoder(ffmpeg_path)

        self.assertEqual(encoder, "h264_qsv")
        self.assertEqual(subprocess_run.call_count, 3)


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
                "display_name": "椤甸潰鏍囬",
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
        self.assertEqual(payload["resolved_source"]["display_name"], "椤甸潰鏍囬")
        self.service.http_server.start_remote.assert_called_once()
        set_media_with_title.assert_called_once_with(
            controller,
            "http://192.168.1.88:4123/media/demo.m3u8",
            "https://cdn.example.com/video/demo.m3u8",
            "椤甸潰鏍囬",
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
                media_url="https://cdn.example.com/video/demo.mp4",
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
            media_url="https://cdn.example.com/video/demo.mp4",
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
