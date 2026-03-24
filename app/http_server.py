import html
from collections import OrderedDict
from dataclasses import dataclass
import mimetypes
import re
import socket
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Mapping, Optional, Tuple
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit

BUFFER_SIZE = 64 * 1024
RANGE_PATTERN = re.compile(r"bytes=(\d*)-(\d*)")
MANIFEST_URI_PATTERN = re.compile(r'URI="([^"]+)"')
MANIFEST_CONTENT_TYPES = (
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "audio/mpegurl",
)
CLIENT_DISCONNECT_ERRORS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)
REMOTE_PREFETCH_SEGMENTS = 3
REMOTE_PREFETCH_MAX_DEPTH = 2
REMOTE_PREFETCH_MAX_CHILD_MANIFESTS = 4
REMOTE_CACHE_MAX_ITEM_BYTES = 8 * 1024 * 1024
REMOTE_CACHE_MAX_TOTAL_BYTES = 24 * 1024 * 1024


@dataclass(slots=True)
class RemoteCacheEntry:
    payload: bytes
    content_type: str
    headers: dict[str, str]


def build_proxy_url(base_url: str, proxy_path: str, source_url: str) -> str:
    return f"{base_url}{proxy_path}?url={quote(source_url, safe='')}"


def quote_path_for_url(path: str | Path) -> str:
    parts = [quote(part) for part in Path(path).parts if part not in {"", "."}]
    return "/".join(parts)


def rewrite_manifest_payload(payload: bytes, source_url: str, base_url: str, proxy_path: str) -> bytes:
    text = payload.decode("utf-8-sig", errors="ignore")
    trailing_newline = text.endswith("\n")
    rewritten_lines: list[str] = []

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            rewritten_lines.append(line)
            continue
        if stripped.startswith("#"):
            rewritten_lines.append(
                MANIFEST_URI_PATTERN.sub(
                    lambda match: f'URI="{build_proxy_url(base_url, proxy_path, urljoin(source_url, match.group(1)))}"',
                    line,
                )
            )
            continue
        rewritten_lines.append(build_proxy_url(base_url, proxy_path, urljoin(source_url, stripped)))

    rewritten = "\n".join(rewritten_lines)
    if trailing_newline:
        rewritten += "\n"
    return rewritten.encode("utf-8")


def extract_manifest_targets(payload: bytes, source_url: str) -> list[tuple[str, str]]:
    text = payload.decode("utf-8-sig", errors="ignore")
    targets: list[tuple[str, str]] = []

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            for match in MANIFEST_URI_PATTERN.finditer(line):
                targets.append(("attribute", urljoin(source_url, match.group(1))))
            continue
        targets.append(("line", urljoin(source_url, stripped)))

    return targets


def classify_manifest(payload: bytes) -> str:
    text = payload.decode("utf-8-sig", errors="ignore")
    if "#EXT-X-STREAM-INF" in text:
        return "master"
    if "#EXTINF" in text or "#EXT-X-TARGETDURATION" in text or "#EXT-X-MEDIA-SEQUENCE" in text:
        return "media"
    return "unknown"


def looks_like_manifest_url(source_url: str) -> bool:
    lowered = source_url.strip().lower()
    if ".m3u8" in lowered or lowered.endswith(".m3u"):
        return True
    split = urlsplit(source_url)
    combined = f"{split.query}&{split.fragment}".lower()
    return ".m3u8" in combined


def is_manifest_response(source_url: str, content_type: str) -> bool:
    lowered_type = content_type.lower()
    lowered_path = urlsplit(source_url).path.lower()
    return lowered_path.endswith(".m3u8") or any(token in lowered_type for token in MANIFEST_CONTENT_TYPES)


def _cache_response_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    if not headers:
        return {}
    values: dict[str, str] = {}
    for name in ("ETag", "Last-Modified"):
        value = headers.get(name)
        if value:
            values[name] = str(value)
    return values


def lookup_remote_cache_entry(server: object, source_url: str) -> RemoteCacheEntry | None:
    cache = getattr(server, "remote_cache", None)
    lock = getattr(server, "remote_cache_lock", None)
    if cache is None or lock is None:
        return None
    with lock:
        entry = cache.get(source_url)
        if entry is None:
            return None
        cache.move_to_end(source_url)
        return entry


def store_remote_cache_entry(
    server: object,
    source_url: str,
    payload: bytes,
    content_type: str,
    headers: Mapping[str, str] | None = None,
) -> bool:
    cache = getattr(server, "remote_cache", None)
    lock = getattr(server, "remote_cache_lock", None)
    cache_bytes = getattr(server, "remote_cache_bytes", None)
    if cache is None or lock is None or cache_bytes is None:
        return False

    payload_size = len(payload)
    if payload_size <= 0 or payload_size > REMOTE_CACHE_MAX_ITEM_BYTES:
        return False

    with lock:
        existing = cache.pop(source_url, None)
        if existing is not None:
            server.remote_cache_bytes = max(0, int(server.remote_cache_bytes) - len(existing.payload))  # type: ignore[attr-defined]

        while cache and int(server.remote_cache_bytes) + payload_size > REMOTE_CACHE_MAX_TOTAL_BYTES:  # type: ignore[attr-defined]
            _, evicted = cache.popitem(last=False)
            server.remote_cache_bytes = max(0, int(server.remote_cache_bytes) - len(evicted.payload))  # type: ignore[attr-defined]

        if int(server.remote_cache_bytes) + payload_size > REMOTE_CACHE_MAX_TOTAL_BYTES:  # type: ignore[attr-defined]
            return False

        cache[source_url] = RemoteCacheEntry(
            payload=payload,
            content_type=content_type,
            headers=_cache_response_headers(headers),
        )
        server.remote_cache_bytes = int(server.remote_cache_bytes) + payload_size  # type: ignore[attr-defined]
    return True


def get_local_ip() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("8.8.8.8", 80))
            candidate = probe.getsockname()[0]
            if candidate and not candidate.startswith("127."):
                return candidate
        except OSError:
            pass
    try:
        candidate = socket.gethostbyname(socket.gethostname())
        if candidate and not candidate.startswith("127."):
            return candidate
    except OSError:
        pass
    return "127.0.0.1"


def parse_range_header(header_value: str, file_size: int) -> Optional[Tuple[int, int]]:
    match = RANGE_PATTERN.fullmatch(header_value.strip())
    if not match:
        return None

    start_text, end_text = match.groups()
    if not start_text and not end_text:
        return None

    if start_text:
        start = int(start_text)
        end = int(end_text) if end_text else file_size - 1
    else:
        suffix_length = int(end_text)
        if suffix_length <= 0:
            return None
        start = max(0, file_size - suffix_length)
        end = file_size - 1

    if start > end or start >= file_size:
        return None
    return start, min(end, file_size - 1)


class MediaRequestHandler(BaseHTTPRequestHandler):
    server_version = "ScreenCastingHTTP/0.6"

    def do_HEAD(self) -> None:
        self._dispatch(send_body=False)

    def do_GET(self) -> None:
        self._dispatch(send_body=True)

    def log_message(self, format: str, *args) -> None:
        return

    def _dispatch(self, send_body: bool) -> None:
        parsed = urlsplit(self.path)
        request_path = parsed.path
        media_path = self.server.media_path  # type: ignore[attr-defined]
        player_path = self.server.player_path  # type: ignore[attr-defined]
        proxy_path = getattr(self.server, "proxy_path", None)
        media_root_prefix = getattr(self.server, "media_root_prefix", None)
        if request_path == player_path:
            self._serve_player_page(send_body)
            return
        if media_root_prefix and request_path.startswith(media_root_prefix):
            relative_path = unquote(request_path[len(media_root_prefix) :]).lstrip("/")
            if not relative_path:
                relative_path = str(getattr(self.server, "media_root_default", "") or "").strip()
            if not relative_path:
                self.send_error(404, "File not found")
                return
            self._serve_root_file(send_body, relative_path)
            return
        if media_path and request_path == media_path:
            remote_media_url = getattr(self.server, "remote_media_url", None)
            if remote_media_url:
                self._serve_remote(send_body, remote_media_url)
            else:
                self._serve_file(send_body)
            return
        if proxy_path and request_path == proxy_path:
            source_url = parse_qs(parsed.query).get("url", [""])[0].strip()
            if not source_url:
                self.send_error(400, "Missing source url")
                return
            self._serve_remote(send_body, source_url)
            return
        self.send_error(404, "File not found")

    def _serve_file(self, send_body: bool) -> None:
        file_path = Path(self.server.media_file)  # type: ignore[attr-defined]
        self._serve_local_file(send_body, file_path)

    def _serve_root_file(self, send_body: bool, relative_path: str) -> None:
        root_dir = Path(self.server.media_root_dir).resolve()  # type: ignore[attr-defined]
        candidate = (root_dir / Path(relative_path)).resolve()
        try:
            candidate.relative_to(root_dir)
        except ValueError:
            self.send_error(403, "Access denied")
            return
        self._serve_local_file(send_body, candidate)

    def _serve_local_file(self, send_body: bool, file_path: Path) -> None:
        if not file_path.exists():
            self.send_error(404, "File not found")
            return

        file_size = file_path.stat().st_size
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"

        try:
            range_header = self.headers.get("Range")
            if range_header:
                byte_range = parse_range_header(range_header, file_size)
                if byte_range is None:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{file_size}")
                    self.end_headers()
                    return
                start, end = byte_range
                content_length = end - start + 1
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
            else:
                start = 0
                end = file_size - 1
                content_length = file_size
                self.send_response(200)

            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(content_length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()

            if not send_body:
                return

            with file_path.open("rb") as handle:
                handle.seek(start)
                remaining = content_length
                while remaining > 0:
                    chunk = handle.read(min(BUFFER_SIZE, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except CLIENT_DISCONNECT_ERRORS:
            return

    def _forward_remote_headers(self, source_url: str = "") -> dict[str, str]:
        remote_headers = getattr(self.server, "remote_headers", {})
        headers = {str(name): str(value) for name, value in dict(remote_headers).items() if name and value}
        headers.setdefault("Accept-Encoding", "identity")
        headers["Connection"] = "close"
        range_header = self.headers.get("Range")
        if range_header and not looks_like_manifest_url(source_url):
            headers["Range"] = range_header
        else:
            headers.pop("Range", None)
        return headers

    def _is_manifest_response(self, source_url: str, content_type: str) -> bool:
        return is_manifest_response(source_url, content_type)

    def _build_proxy_url(self, source_url: str) -> str:
        base_url = self.server.base_url  # type: ignore[attr-defined]
        proxy_path = self.server.proxy_path  # type: ignore[attr-defined]
        return build_proxy_url(base_url, proxy_path, source_url)

    def _rewrite_manifest(self, payload: bytes, source_url: str) -> bytes:
        base_url = self.server.base_url  # type: ignore[attr-defined]
        proxy_path = self.server.proxy_path  # type: ignore[attr-defined]
        return rewrite_manifest_payload(payload, source_url, base_url, proxy_path)

    def _serve_cached_remote(self, send_body: bool, source_url: str) -> bool:
        entry = lookup_remote_cache_entry(self.server, source_url)
        if entry is None:
            return False

        payload = entry.payload
        total_size = len(payload)
        try:
            range_header = self.headers.get("Range")
            if range_header:
                byte_range = parse_range_header(range_header, total_size)
                if byte_range is None:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{total_size}")
                    self.end_headers()
                    return True
                start, end = byte_range
                body = payload[start : end + 1]
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{total_size}")
            else:
                body = payload
                self.send_response(200)

            self.send_header("Content-Type", entry.content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Accept-Ranges", "bytes")
            for name, value in entry.headers.items():
                if value:
                    self.send_header(name, value)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()

            if send_body:
                self.wfile.write(body)
        except CLIENT_DISCONNECT_ERRORS:
            return True
        return True

    def _copy_upstream_headers(self, response: object, *, content_length: Optional[int] = None, content_type: Optional[str] = None) -> None:
        headers = response.headers  # type: ignore[attr-defined]
        values = {
            "Content-Type": content_type or headers.get("Content-Type"),
            "Content-Length": str(content_length) if content_length is not None else headers.get("Content-Length"),
            "Content-Range": headers.get("Content-Range"),
            "Accept-Ranges": headers.get("Accept-Ranges") or "bytes",
            "Last-Modified": headers.get("Last-Modified"),
            "ETag": headers.get("ETag"),
        }
        for key, value in values.items():
            if value:
                self.send_header(key, value)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")

    def _serve_remote(self, send_body: bool, source_url: str) -> None:
        if self._serve_cached_remote(send_body, source_url):
            return

        request = urllib.request.Request(
            source_url,
            headers=self._forward_remote_headers(source_url),
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                effective_url = response.geturl() or source_url
                content_type = response.headers.get("Content-Type") or (
                    mimetypes.guess_type(urlsplit(effective_url).path)[0] or "application/octet-stream"
                )
                if send_body and self._is_manifest_response(effective_url, content_type):
                    payload = self._rewrite_manifest(response.read(), effective_url)
                    store_remote_cache_entry(
                        self.server,
                        source_url,
                        payload,
                        "application/vnd.apple.mpegurl; charset=utf-8",
                        response.headers,
                    )
                    self.send_response(200)
                    self._copy_upstream_headers(response, content_length=len(payload), content_type="application/vnd.apple.mpegurl; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(payload)
                    return

                status = getattr(response, "status", 200)
                self.send_response(status)
                self._copy_upstream_headers(response, content_type=content_type)
                self.end_headers()
                if not send_body:
                    return

                while True:
                    chunk = response.read(BUFFER_SIZE)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except urllib.error.HTTPError as exc:
            self.send_error(exc.code, exc.reason)
        except CLIENT_DISCONNECT_ERRORS:
            return
        except (urllib.error.URLError, OSError) as exc:
            self.send_error(502, f"Bad gateway: {exc}")

    def _serve_player_page(self, send_body: bool) -> None:
        title = html.escape(self.server.player_title)  # type: ignore[attr-defined]
        media_url = html.escape(self.server.player_media_url)  # type: ignore[attr-defined]
        body = f"""<!doctype html>
<html lang=\"zh-CN\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>{title}</title>
  <style>
    body {{
      margin: 0;
      background: #0f172a;
      color: #e2e8f0;
      font-family: \"Microsoft YaHei UI\", \"Segoe UI\", sans-serif;
    }}
    .shell {{
      max-width: 960px;
      margin: 0 auto;
      padding: 20px 16px 28px;
    }}
    h1 {{
      margin: 0 0 10px;
      font-size: 24px;
      line-height: 1.4;
      word-break: break-all;
    }}
    p {{
      margin: 0 0 12px;
      color: #cbd5e1;
      font-size: 15px;
      line-height: 1.7;
    }}
    video {{
      width: 100%;
      max-height: 72vh;
      background: #000;
      border: 1px solid #334155;
      border-radius: 12px;
      display: block;
    }}
    button {{
      margin-top: 14px;
      padding: 12px 18px;
      border: 0;
      border-radius: 999px;
      background: #2f6bff;
      color: #fff;
      font-size: 16px;
    }}
  </style>
</head>
<body>
  <main class=\"shell\">
    <h1>{title}</h1>
    <p>这是电脑端生成的兼容播放页。若未自动播放，请按遥控器确认键或点击下方按钮。</p>
    <video id=\"player\" src=\"{media_url}\" controls autoplay playsinline webkit-playsinline x5-playsinline x5-video-player-type=\"h5-page\" preload=\"metadata\"></video>
    <button id=\"playButton\" type=\"button\">开始播放</button>
    <p id=\"status\">如果页面已打开但视频仍无法播放，请确认电脑没有休眠、电视和电脑仍在同一局域网，然后重新生成兼容播放页。</p>
  </main>
  <script>
    (function () {{
      var player = document.getElementById(\"player\");
      var playButton = document.getElementById(\"playButton\");
      var status = document.getElementById(\"status\");

      function tryPlay() {{
        var result;
        try {{
          result = player.play();
        }} catch (error) {{
          status.textContent = \"浏览器没有自动开始播放，请点击“开始播放”。\";
          playButton.style.display = \"inline-block\";
          return;
        }}
        if (result && typeof result.then === \"function\") {{
          result.then(function () {{
            status.textContent = \"视频已开始播放。\";
            playButton.style.display = \"none\";
          }}).catch(function () {{
            status.textContent = \"浏览器拦截了自动播放，请点击“开始播放”。\";
            playButton.style.display = \"inline-block\";
          }});
        }} else {{
          playButton.style.display = \"none\";
        }}
      }}

      playButton.addEventListener(\"click\", tryPlay);
      player.addEventListener(\"error\", function () {{
        status.textContent = \"视频加载失败。若电视刚请求后又立刻断开，通常是盒子内置播放器不接受当前流。请返回电脑端重新生成兼容播放页再试。\";
        playButton.style.display = \"inline-block\";
      }});
      window.addEventListener(\"load\", tryPlay);
    }})();
  </script>
</body>
</html>""".encode("utf-8")

        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()

            if send_body:
                self.wfile.write(body)
        except CLIENT_DISCONNECT_ERRORS:
            return


class MediaHttpServer:
    def __init__(self) -> None:
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._media_file: Optional[Path] = None
        self._media_url: Optional[str] = None
        self._player_url: Optional[str] = None
        self._remote_source_url: Optional[str] = None

    def start(self, file_path: str) -> str:
        self.stop()
        media_file = Path(file_path).resolve()
        url_path = f"/media/{quote(media_file.name)}"
        player_path = "/player"
        proxy_path = "/proxy"
        server = ThreadingHTTPServer(("0.0.0.0", 0), MediaRequestHandler)
        server.media_file = str(media_file)  # type: ignore[attr-defined]
        server.media_path = url_path  # type: ignore[attr-defined]
        server.player_path = player_path  # type: ignore[attr-defined]
        server.proxy_path = proxy_path  # type: ignore[attr-defined]
        server.remote_media_url = None  # type: ignore[attr-defined]
        server.remote_headers = {}  # type: ignore[attr-defined]
        server.remote_cache = OrderedDict()  # type: ignore[attr-defined]
        server.remote_cache_lock = threading.RLock()  # type: ignore[attr-defined]
        server.remote_cache_bytes = 0  # type: ignore[attr-defined]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        base_url = f"http://{get_local_ip()}:{server.server_address[1]}"
        server.base_url = base_url  # type: ignore[attr-defined]
        player_media_url = f"{base_url}{url_path}"
        server.player_media_url = player_media_url  # type: ignore[attr-defined]
        server.player_title = media_file.name  # type: ignore[attr-defined]

        self._media_file = media_file
        self._media_url = player_media_url
        self._player_url = f"{base_url}{player_path}"
        self._remote_source_url = None
        self._server = server
        self._thread = thread
        return self._media_url

    def start_hls(self, playlist_path: str, display_name: str) -> str:
        self.stop()
        playlist_file = Path(playlist_path).resolve()
        root_dir = playlist_file.parent
        relative_playlist = playlist_file.relative_to(root_dir)
        url_root = f"/media/{quote(root_dir.name)}/"
        url_path = f"{url_root}{quote_path_for_url(relative_playlist)}"
        player_path = "/player"
        proxy_path = "/proxy"
        server = ThreadingHTTPServer(("0.0.0.0", 0), MediaRequestHandler)
        server.media_file = str(playlist_file)  # type: ignore[attr-defined]
        server.media_path = url_path  # type: ignore[attr-defined]
        server.media_root_dir = str(root_dir)  # type: ignore[attr-defined]
        server.media_root_prefix = url_root  # type: ignore[attr-defined]
        server.media_root_default = str(relative_playlist).replace("\\", "/")  # type: ignore[attr-defined]
        server.player_path = player_path  # type: ignore[attr-defined]
        server.proxy_path = proxy_path  # type: ignore[attr-defined]
        server.remote_media_url = None  # type: ignore[attr-defined]
        server.remote_headers = {}  # type: ignore[attr-defined]
        server.remote_cache = OrderedDict()  # type: ignore[attr-defined]
        server.remote_cache_lock = threading.RLock()  # type: ignore[attr-defined]
        server.remote_cache_bytes = 0  # type: ignore[attr-defined]
        server.player_title = display_name  # type: ignore[attr-defined]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        base_url = f"http://{get_local_ip()}:{server.server_address[1]}"
        server.base_url = base_url  # type: ignore[attr-defined]
        local_media_url = f"{base_url}{url_path}"
        server.player_media_url = local_media_url  # type: ignore[attr-defined]

        self._media_file = playlist_file
        self._media_url = local_media_url
        self._player_url = f"{base_url}{player_path}"
        self._remote_source_url = None
        self._server = server
        self._thread = thread
        return self._media_url

    def start_remote(self, media_url: str, display_name: str, headers: Optional[dict[str, str]] = None) -> str:
        self.stop()
        media_name = Path(display_name).name.strip()
        source_suffix = Path(urlsplit(media_url).path).suffix
        if not source_suffix and looks_like_manifest_url(media_url):
            source_suffix = ".m3u8"
        if not media_name:
            media_name = f"remote-media{source_suffix}" if source_suffix else "remote-media"
        elif source_suffix and not Path(media_name).suffix:
            media_name = f"{media_name}{source_suffix}"
        url_path = f"/media/{quote(media_name)}"
        player_path = "/player"
        proxy_path = "/proxy"
        server = ThreadingHTTPServer(("0.0.0.0", 0), MediaRequestHandler)
        server.media_file = ""  # type: ignore[attr-defined]
        server.media_path = url_path  # type: ignore[attr-defined]
        server.player_path = player_path  # type: ignore[attr-defined]
        server.proxy_path = proxy_path  # type: ignore[attr-defined]
        server.remote_media_url = media_url  # type: ignore[attr-defined]
        server.remote_headers = dict(headers or {})  # type: ignore[attr-defined]
        server.remote_cache = OrderedDict()  # type: ignore[attr-defined]
        server.remote_cache_lock = threading.RLock()  # type: ignore[attr-defined]
        server.remote_cache_bytes = 0  # type: ignore[attr-defined]
        server.player_title = display_name  # type: ignore[attr-defined]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        base_url = f"http://{get_local_ip()}:{server.server_address[1]}"
        server.base_url = base_url  # type: ignore[attr-defined]
        local_media_url = f"{base_url}{url_path}"
        server.player_media_url = local_media_url  # type: ignore[attr-defined]
        self._warm_remote_cache(server, media_url, dict(headers or {}))

        self._media_file = None
        self._media_url = local_media_url
        self._player_url = f"{base_url}{player_path}"
        self._remote_source_url = media_url
        self._server = server
        self._thread = thread
        return self._media_url

    def _fetch_remote_payload(
        self,
        source_url: str,
        headers: Optional[dict[str, str]] = None,
    ) -> Tuple[bytes, str, Mapping[str, str], str]:
        request_headers = dict(headers or {})
        request_headers.setdefault("Accept-Encoding", "identity")
        request_headers["Connection"] = "close"
        request = urllib.request.Request(source_url, headers=request_headers, method="GET")
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = response.read()
            effective_url = response.geturl() or source_url
            content_type = response.headers.get("Content-Type") or (
                mimetypes.guess_type(urlsplit(effective_url).path)[0] or "application/octet-stream"
            )
            return payload, content_type, response.headers, effective_url

    def _prefetch_remote_url(
        self,
        server: ThreadingHTTPServer,
        source_url: str,
        headers: Optional[dict[str, str]],
        depth: int,
        visited: set[str],
    ) -> None:
        if depth < 0 or source_url in visited:
            return
        visited.add(source_url)
        if lookup_remote_cache_entry(server, source_url) is not None:
            return

        try:
            payload, content_type, response_headers, effective_url = self._fetch_remote_payload(source_url, headers=headers)
        except (urllib.error.HTTPError, urllib.error.URLError, OSError):
            return

        if not is_manifest_response(effective_url, content_type):
            store_remote_cache_entry(server, source_url, payload, content_type, response_headers)
            return

        rewritten = rewrite_manifest_payload(payload, effective_url, server.base_url, server.proxy_path)  # type: ignore[attr-defined]
        store_remote_cache_entry(
            server,
            source_url,
            rewritten,
            "application/vnd.apple.mpegurl; charset=utf-8",
            response_headers,
        )

        manifest_kind = classify_manifest(payload)
        targets = extract_manifest_targets(payload, effective_url)
        if manifest_kind == "master":
            for _, target_url in targets[:REMOTE_PREFETCH_MAX_CHILD_MANIFESTS]:
                self._prefetch_remote_url(server, target_url, headers, depth - 1, visited)
            return

        attribute_targets = [target_url for kind, target_url in targets if kind == "attribute"]
        line_targets = [target_url for kind, target_url in targets if kind == "line"]

        for target_url in attribute_targets[:REMOTE_PREFETCH_MAX_CHILD_MANIFESTS]:
            next_depth = depth - 1 if looks_like_manifest_url(target_url) else 0
            self._prefetch_remote_url(server, target_url, headers, next_depth, visited)

        prefetched_segments = 0
        for target_url in line_targets:
            if looks_like_manifest_url(target_url):
                self._prefetch_remote_url(server, target_url, headers, depth - 1, visited)
                continue
            self._prefetch_remote_url(server, target_url, headers, 0, visited)
            prefetched_segments += 1
            if prefetched_segments >= REMOTE_PREFETCH_SEGMENTS:
                break

    def _warm_remote_cache(
        self,
        server: ThreadingHTTPServer,
        media_url: str,
        headers: Optional[dict[str, str]] = None,
    ) -> None:
        if not looks_like_manifest_url(media_url):
            return
        self._prefetch_remote_url(server, media_url, headers, REMOTE_PREFETCH_MAX_DEPTH, set())

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=1)
        self._server = None
        self._thread = None
        self._media_file = None
        self._media_url = None
        self._player_url = None
        self._remote_source_url = None

    @property
    def media_file(self) -> Optional[Path]:
        return self._media_file

    @property
    def media_url(self) -> Optional[str]:
        return self._media_url

    @property
    def player_url(self) -> Optional[str]:
        return self._player_url

    @property
    def remote_source_url(self) -> Optional[str]:
        return self._remote_source_url
