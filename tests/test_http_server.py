import re
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app.http_server import MediaHttpServer, parse_range_header


class UpstreamHandler(BaseHTTPRequestHandler):
    routes: dict[str, tuple[bytes, str]] = {}

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path not in self.routes:
            self.send_error(404)
            return
        payload, content_type = self.routes[path]
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args) -> None:
        return


class HttpServerTests(unittest.TestCase):
    def test_parse_range_header(self) -> None:
        self.assertEqual(parse_range_header("bytes=0-9", 100), (0, 9))
        self.assertEqual(parse_range_header("bytes=10-", 100), (10, 99))
        self.assertEqual(parse_range_header("bytes=-10", 100), (90, 99))
        self.assertIsNone(parse_range_header("bytes=100-101", 100))

    def test_media_server_serves_partial_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            file_path = Path(temp_dir) / "demo.mp4"
            file_path.write_bytes(b"abcdefghijklmnopqrstuvwxyz")

            server = MediaHttpServer()
            url = server.start(str(file_path))
            try:
                request = urllib.request.Request(url, headers={"Range": "bytes=0-3"})
                with urllib.request.urlopen(request, timeout=5) as response:
                    self.assertEqual(response.status, 206)
                    self.assertEqual(response.read(), b"abcd")
            finally:
                server.stop()

    def test_media_server_serves_player_page(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            file_path = Path(temp_dir) / "demo & clip.mp4"
            file_path.write_bytes(b"abcdefghijklmnopqrstuvwxyz")

            server = MediaHttpServer()
            server.start(str(file_path))
            try:
                with urllib.request.urlopen(server.player_url, timeout=5) as response:  # type: ignore[arg-type]
                    body = response.read().decode("utf-8")
                    self.assertEqual(response.status, 200)
                    self.assertIn("<video", body)
                    self.assertIn("demo &amp; clip.mp4", body)
            finally:
                server.stop()

    def test_media_server_proxies_remote_media_and_player_page(self) -> None:
        UpstreamHandler.routes = {
            "/media/demo.mp4": (b"remote-demo", "video/mp4"),
        }
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        upstream_url = f"http://127.0.0.1:{upstream.server_address[1]}/media/demo.mp4"

        server = MediaHttpServer()
        media_url = server.start_remote(upstream_url, "远程演示.mp4")
        try:
            with urllib.request.urlopen(media_url, timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(), b"remote-demo")

            with urllib.request.urlopen(server.player_url, timeout=5) as response:  # type: ignore[arg-type]
                body = response.read().decode("utf-8")
                self.assertEqual(response.status, 200)
                self.assertIn(media_url, body)
                self.assertIn("远程演示.mp4", body)

            self.assertEqual(server.remote_source_url, upstream_url)
            self.assertEqual(server.media_url, media_url)
            self.assertIsNone(server.media_file)
        finally:
            server.stop()
            upstream.shutdown()
            upstream.server_close()
            thread.join(timeout=1)

    def test_media_server_rewrites_remote_m3u8(self) -> None:
        UpstreamHandler.routes = {
            "/playlist.m3u8": (b"#EXTM3U\n#EXTINF:4.0,\nsegment.ts\n", "application/vnd.apple.mpegurl"),
            "/segment.ts": (b"ts-segment", "video/mp2t"),
        }
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        upstream_url = f"http://127.0.0.1:{upstream.server_address[1]}/playlist.m3u8"

        server = MediaHttpServer()
        media_url = server.start_remote(upstream_url, "playlist.m3u8")
        try:
            with urllib.request.urlopen(media_url, timeout=5) as response:
                manifest = response.read().decode("utf-8")
                self.assertEqual(response.status, 200)
                self.assertIn("/proxy?url=", manifest)

            match = re.search(r"(http://[^\s]+/proxy\?url=[^\s]+)", manifest)
            self.assertIsNotNone(match)
            with urllib.request.urlopen(match.group(1), timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(), b"ts-segment")
        finally:
            server.stop()
            upstream.shutdown()
            upstream.server_close()
            thread.join(timeout=1)


if __name__ == "__main__":
    unittest.main()
