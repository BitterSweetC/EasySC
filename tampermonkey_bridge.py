from __future__ import annotations

from collections import deque
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
import urllib.request
from urllib.parse import parse_qs, urlsplit

from app.discovery import discover_devices
from app.dlna import DlnaController
from app.http_server import MediaHttpServer
from app.media_source import display_name_from_source, explain_generated_media_url, guess_content_type_from_source, is_http_url, is_probable_direct_media_url
from app.models import DlnaDevice
from app.video_transcoder import FfmpegNotFoundError, find_ffmpeg
from app.web_video_extractor import VideoPageExtractionError, VideoPageExtractionUnavailable, has_web_video_extractor, resolve_media_source

FORWARD_HEADER_ALLOWLIST = {"User-Agent", "Referer", "Origin", "Cookie", "Authorization", "Accept", "Accept-Language"}
FORWARD_HEADER_NAME_MAP = {name.lower(): name for name in FORWARD_HEADER_ALLOWLIST}
DEFAULT_DISCOVERY_TIMEOUT = 5.5
PROJECT_ROOT = Path(__file__).resolve().parent
REMOTE_MUX_CACHE_DIR = PROJECT_ROOT / "cache" / "compat_media" / "remote_mux"
REMOTE_TRANSCODE_CACHE_DIR = PROJECT_ROOT / "cache" / "compat_media" / "remote_transcode"
LIVE_TRANSCODE_CACHE_DIR = PROJECT_ROOT / "cache" / "compat_media" / "live_transcode"
LIVE_TRANSCODE_SEGMENT_SECONDS = 2
LIVE_TRANSCODE_STARTUP_TIMEOUT = 30.0
LIVE_TRANSCODE_STARTUP_BUFFER_SECONDS = 15.0
LIVE_HLS_FLAGS = "append_list+omit_endlist"
SMOOTH_TARGET_FPS = "30000/1001"
SMOOTH_MAX_WIDTH = 1280
SMOOTH_MAX_HEIGHT = 720
SMOOTH_SOFTWARE_MAX_WIDTH = 960
SMOOTH_SOFTWARE_MAX_HEIGHT = 540
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
QUALITY_ALIASES = {
    "": "max",
    "max": "max",
    "best": "max",
    "highest": "max",
    "source": "max",
    "original": "max",
    "2160": "2160p",
    "2160p": "2160p",
    "4k": "2160p",
    "1440": "1440p",
    "1440p": "1440p",
    "2k": "1440p",
    "1080": "1080p",
    "1080p": "1080p",
    "720": "720p",
    "720p": "720p",
}
QUALITY_MAX_HEIGHTS = {
    "max": None,
    "2160p": 2160,
    "1440p": 1440,
    "1080p": 1080,
    "720p": 720,
}
TRANSCODE_PROFILE_ALIASES = {
    "": "standard",
    "standard": "standard",
    "direct": "standard",
    "copy": "standard",
    "quality": "quality",
    "hq": "quality",
    "smooth": "smooth",
    "smooth30": "smooth",
    "30fps": "smooth",
    "smooth60": "smooth",
    "60fps": "smooth",
    "fps60": "smooth",
}
MUX_AWARE_PAGE_HOST_SUFFIXES = (
    "bilibili.com",
    "qq.com",
)


@dataclass(slots=True)
class BridgeResolvedSource:
    source_url: str
    media_url: str
    display_name: str
    headers: dict[str, str]
    resolved_from_page: bool
    requires_local_mux: bool = False
    video_url: str = ""
    audio_url: str = ""
    video_headers: dict[str, str] = field(default_factory=dict)
    audio_headers: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class BridgeTask:
    task_id: str
    status: str
    message: str
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    result: dict[str, Any] | None = None
    error: str = ""


@dataclass(slots=True, frozen=True)
class TranscodeAttempt:
    effective_profile: str
    encoder_name: str
    smooth_fallback: bool = False
    downgraded: bool = False
    hardware: bool = False


def _format_transcode_attempt_label(attempt: TranscodeAttempt) -> str:
    label = f"{attempt.effective_profile}/{attempt.encoder_name}"
    if attempt.smooth_fallback:
        label = f"{label} fallback"
    if attempt.downgraded:
        label = f"{label} downgraded"
    return label


class CastStartError(RuntimeError):
    def __init__(self, message: str, *, served_media_url: str = "", player_url: str = "") -> None:
        super().__init__(message)
        self.served_media_url = served_media_url
        self.player_url = player_url


class LiveHlsTranscodeSession:
    def __init__(
        self,
        *,
        output_dir: Path,
        playlist_path: Path,
        process: subprocess.Popen[Any],
        proxy_servers: list[MediaHttpServer],
    ) -> None:
        self.output_dir = output_dir
        self.playlist_path = playlist_path
        self.process = process
        self.proxy_servers = proxy_servers
        self._stderr_lines: deque[str] = deque(maxlen=40)
        self._stopped = False
        self._stderr_thread = threading.Thread(target=self._capture_stderr, daemon=True)
        self._stderr_thread.start()

    def _capture_stderr(self) -> None:
        stderr = self.process.stderr
        if stderr is None:
            return
        try:
            for line in stderr:
                text = str(line).strip()
                if text:
                    self._stderr_lines.append(text)
        finally:
            try:
                stderr.close()
            except OSError:
                pass

    def _tail_stderr(self) -> str:
        tail = "\n".join(self._stderr_lines).strip()
        return tail or "unknown ffmpeg error"

    def buffered_duration_seconds(self) -> float:
        if not self.playlist_path.exists():
            return 0.0
        try:
            text = self.playlist_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return 0.0

        total = 0.0
        for line in text.splitlines():
            if not line.startswith("#EXTINF:"):
                continue
            value = line.partition(":")[2].partition(",")[0].strip()
            try:
                total += float(value)
            except ValueError:
                continue
        if total > 0:
            return total
        return float(len(list(self.output_dir.glob("segment_*.ts"))) * LIVE_TRANSCODE_SEGMENT_SECONDS)

    def wait_until_ready(
        self,
        timeout: float = LIVE_TRANSCODE_STARTUP_TIMEOUT,
        *,
        startup_buffer_seconds: float = LIVE_TRANSCODE_STARTUP_BUFFER_SECONDS,
    ) -> None:
        deadline = time.time() + max(timeout, 1.0)
        required_buffer = max(0.0, float(startup_buffer_seconds))
        while time.time() < deadline:
            buffered_seconds = self.buffered_duration_seconds()
            if self.playlist_path.exists() and buffered_seconds >= required_buffer:
                return
            exit_code = self.process.poll()
            if exit_code is not None:
                if self.playlist_path.exists() and buffered_seconds > 0:
                    return
                raise RuntimeError(f"ffmpeg exited with code {exit_code}.\n{self._tail_stderr()}")
            time.sleep(0.25)
        raise RuntimeError(
            f"Timed out waiting for live HLS startup buffer ({buffered_seconds:.1f}s/{required_buffer:.1f}s).\n{self._tail_stderr()}"
        )

    def stop(self, cleanup: bool = True) -> None:
        if self._stopped:
            return
        self._stopped = True

        if self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
            except OSError:
                pass

        stderr = self.process.stderr
        if stderr is not None and not stderr.closed:
            try:
                stderr.close()
            except OSError:
                pass

        if self._stderr_thread.is_alive():
            self._stderr_thread.join(timeout=1)

        for proxy_server in self.proxy_servers:
            proxy_server.stop()

        if cleanup:
            shutil.rmtree(self.output_dir, ignore_errors=True)


_FFMPEG_ENCODER_LIST_CACHE: dict[str, set[str]] = {}
_FFMPEG_HW_ENCODER_CACHE: dict[str, str] = {}


def filter_forward_headers(values: Mapping[str, Any] | None) -> dict[str, str]:
    headers: dict[str, str] = {}
    if not values:
        return headers
    for key, value in values.items():
        normalized = FORWARD_HEADER_NAME_MAP.get(str(key).strip().lower())
        if normalized and value:
            headers[normalized] = str(value)
    return headers


def build_didl_metadata_with_title(source: str, media_url: str, title: str) -> str:
    safe_title = html.escape(title.strip() or display_name_from_source(source))
    protocol_info = f"http-get:*:{guess_content_type_from_source(source)}:*"
    resource = html.escape(media_url)
    return (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        '<item id="0" parentID="-1" restricted="1">'
        f"<dc:title>{safe_title}</dc:title>"
        "<upnp:class>object.item.videoItem</upnp:class>"
        f'<res protocolInfo="{protocol_info}">{resource}</res>'
        "</item>"
        "</DIDL-Lite>"
    )


def set_media_with_title(controller: DlnaController, media_url: str, metadata_source: str, title: str) -> bytes:
    if controller.device.av_transport is None:
        raise ValueError("The target device does not expose AVTransport.")
    metadata = build_didl_metadata_with_title(metadata_source, media_url, title)
    return controller._post_action(  # type: ignore[attr-defined]
        controller.device.av_transport,
        "SetAVTransportURI",
        [
            ("InstanceID", "0"),
            ("CurrentURI", media_url),
            ("CurrentURIMetaData", metadata),
        ],
    )


def _load_yt_dlp_module() -> Any:
    try:
        import yt_dlp  # type: ignore
    except ImportError as exc:
        raise VideoPageExtractionUnavailable(
            "yt-dlp is not installed in the current environment, so page extraction is unavailable."
        ) from exc
    return yt_dlp


def _safe_output_stem(display_name: str) -> str:
    stem = SAFE_NAME_RE.sub("_", Path(display_name).stem or display_name).strip("._")
    return stem or "remote_video"


def _hash_remote_streams(video_url: str, audio_url: str) -> str:
    digest = hashlib.sha1(f"{video_url}\n{audio_url}".encode("utf-8")).hexdigest()
    return digest[:16]


def _hash_remote_media(media_url: str, profile: str) -> str:
    digest = hashlib.sha1(f"{profile}\n{media_url}".encode("utf-8")).hexdigest()
    return digest[:16]


def normalize_quality_preference(value: Any) -> str:
    text = str(value or "").strip().lower()
    return QUALITY_ALIASES.get(text, "max")


def normalize_transcode_profile(value: Any) -> str:
    text = str(value or "").strip().lower()
    return TRANSCODE_PROFILE_ALIASES.get(text, "standard")


def _quality_max_height(value: Any) -> int | None:
    return QUALITY_MAX_HEIGHTS[normalize_quality_preference(value)]


def _prefers_mux_aware_page_resolution(source_url: str) -> bool:
    if not is_http_url(source_url) or is_probable_direct_media_url(source_url):
        return False
    hostname = (urlsplit(source_url).hostname or "").strip().lower()
    if not hostname:
        return False
    return any(hostname == suffix or hostname.endswith(f".{suffix}") for suffix in MUX_AWARE_PAGE_HOST_SUFFIXES)


def _profile_label(value: Any) -> str:
    profile = normalize_transcode_profile(value)
    if profile == "quality":
        return "quality"
    if profile == "smooth":
        return "smooth 30fps"
    return "standard"


def _ffmpeg_cache_key(ffmpeg_path: Path) -> str:
    return str(ffmpeg_path.resolve()).lower()


def _list_ffmpeg_encoders(ffmpeg_path: Path) -> set[str]:
    cache_key = _ffmpeg_cache_key(ffmpeg_path)
    cached = _FFMPEG_ENCODER_LIST_CACHE.get(cache_key)
    if cached is not None:
        return cached

    result = subprocess.run(
        [str(ffmpeg_path), "-hide_banner", "-encoders"],
        capture_output=True,
        text=True,
        errors="ignore",
        timeout=15,
    )
    encoders: set[str] = set()
    output = f"{result.stdout}\n{result.stderr}"
    for line in output.splitlines():
        match = re.match(r"^\s*[VAS\.]{6}\s+([A-Za-z0-9_]+)\s+", line)
        if match:
            encoders.add(match.group(1))
    _FFMPEG_ENCODER_LIST_CACHE[cache_key] = encoders
    return encoders


def _probe_hardware_encoder(ffmpeg_path: Path, encoder_name: str) -> bool:
    command = [
        str(ffmpeg_path),
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=size=128x72:rate=1:duration=1",
        "-frames:v",
        "1",
        "-an",
    ]
    if encoder_name == "h264_nvenc":
        command.extend(["-c:v", encoder_name, "-preset", "p5", "-cq", "28", "-b:v", "0"])
    elif encoder_name == "h264_qsv":
        command.extend(["-vf", "format=nv12", "-c:v", encoder_name, "-global_quality", "28"])
    elif encoder_name == "h264_amf":
        command.extend(["-vf", "format=nv12", "-c:v", encoder_name, "-quality", "speed"])
    else:
        return False
    command.extend(["-f", "null", "-"])
    try:
        result = subprocess.run(command, capture_output=True, text=True, errors="ignore", timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def detect_preferred_h264_hardware_encoder(ffmpeg_path: Path) -> str:
    cache_key = _ffmpeg_cache_key(ffmpeg_path)
    cached = _FFMPEG_HW_ENCODER_CACHE.get(cache_key)
    if cached is not None:
        return cached

    encoders = _list_ffmpeg_encoders(ffmpeg_path)
    for candidate in ("h264_nvenc", "h264_qsv", "h264_amf"):
        if candidate not in encoders:
            continue
        if _probe_hardware_encoder(ffmpeg_path, candidate):
            _FFMPEG_HW_ENCODER_CACHE[cache_key] = candidate
            return candidate

    _FFMPEG_HW_ENCODER_CACHE[cache_key] = ""
    return ""


def _build_transcode_attempts(transcode_profile: str, hardware_encoder: str = "") -> list[TranscodeAttempt]:
    profile = normalize_transcode_profile(transcode_profile)
    if profile == "standard":
        return [TranscodeAttempt(effective_profile="standard", encoder_name="libx264")]
    if profile == "quality":
        return [TranscodeAttempt(effective_profile="quality", encoder_name="libx264")]
    if hardware_encoder:
        return [
            TranscodeAttempt(effective_profile="smooth", encoder_name=hardware_encoder, hardware=True),
            TranscodeAttempt(effective_profile="smooth", encoder_name="libx264", smooth_fallback=True),
        ]
    return [TranscodeAttempt(effective_profile="smooth", encoder_name="libx264", smooth_fallback=True)]


def _build_quality_format_selector(value: Any) -> str:
    max_height = _quality_max_height(value)
    if max_height is None:
        return "bestvideo+bestaudio/best[acodec!=none][vcodec!=none]/best"
    return (
        f"bestvideo[height<={max_height}]+bestaudio/"
        f"best[height<={max_height}][acodec!=none][vcodec!=none]/"
        f"best[height<={max_height}]/"
        "bestvideo+bestaudio/best"
    )


class _SilentYtdlpLogger:
    def debug(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        pass

    def error(self, message: str) -> None:
        pass


def _is_requested_format_unavailable(exc: Exception) -> bool:
    message = str(exc or "").lower()
    return "requested format is not available" in message or "requested format not available" in message


def _extract_info_with_format_retry(yt_dlp: Any, source_url: str, options: dict[str, Any]) -> Any:
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            return downloader.extract_info(source_url, download=False)
    except Exception as exc:
        if "format" not in options or not _is_requested_format_unavailable(exc):
            raise

    retry_options = dict(options)
    retry_options.pop("format", None)
    with yt_dlp.YoutubeDL(retry_options) as downloader:
        return downloader.extract_info(source_url, download=False)


def _candidate_height(item: Mapping[str, Any]) -> int | None:
    try:
        height = int(item.get("height") or 0)
    except (TypeError, ValueError):
        return None
    return height if height > 0 else None


def _prefer_height_candidates(candidates: list[dict[str, Any]], max_height: int | None) -> list[dict[str, Any]]:
    if max_height is None:
        return candidates

    within: list[dict[str, Any]] = []
    above: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []

    for item in candidates:
        height = _candidate_height(item)
        if height is None:
            unknown.append(item)
        elif height <= max_height:
            within.append(item)
        else:
            above.append(item)

    if within:
        return within + unknown
    if above:
        closest_height = min(_candidate_height(item) or max_height for item in above)
        closest = [item for item in above if _candidate_height(item) == closest_height]
        return closest + unknown
    return candidates


def _pick_direct_media_candidate(info: dict[str, Any], quality: str = "max") -> tuple[str, dict[str, str]] | None:
    max_height = _quality_max_height(quality)
    candidates: list[dict[str, Any]] = []
    direct_url = str(info.get("url") or "").strip()
    if direct_url and is_http_url(direct_url):
        candidates.append(info)

    for item in info.get("formats") or []:
        if isinstance(item, dict):
            candidates.append(item)

    candidates = _prefer_height_candidates(candidates, max_height)

    best_score = -10**9
    best_url = ""
    best_headers: dict[str, str] = {}
    for item in candidates:
        url = str(item.get("url") or "").strip()
        if not url or not is_http_url(url):
            continue
        score = 0
        protocol = str(item.get("protocol") or "").lower()
        ext = str(item.get("ext") or "").lower()
        acodec = str(item.get("acodec") or "")
        vcodec = str(item.get("vcodec") or "")
        if vcodec and vcodec != "none":
            score += 200
        if acodec and acodec != "none":
            score += 200
        if "m3u8" in protocol:
            score += 80
        elif protocol in {"http", "https"}:
            score += 60
        if ext == "mp4":
            score += 50
        elif is_probable_direct_media_url(url):
            score += 40
        height = _candidate_height(item)
        if height is not None:
            score += min(height, max_height or 4320)
        if score > best_score:
            best_score = score
            best_url = url
            best_headers = filter_forward_headers(item.get("http_headers") or info.get("http_headers") or {})

    if best_url:
        return best_url, best_headers
    return None


def _extract_best_requested_streams(info: dict[str, Any], quality: str = "max") -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    max_height = _quality_max_height(quality)
    requested_formats = [item for item in (info.get("requested_formats") or []) if isinstance(item, dict)]
    all_formats = [item for item in (info.get("formats") or []) if isinstance(item, dict)]
    candidates = requested_formats + [item for item in all_formats if item not in requested_formats]

    video_candidates: list[dict[str, Any]] = []
    audio_candidates: list[dict[str, Any]] = []
    for item in candidates:
        url = str(item.get("url") or "").strip()
        if not url or not is_http_url(url):
            continue
        vcodec = str(item.get("vcodec") or "")
        acodec = str(item.get("acodec") or "")
        if vcodec and vcodec != "none":
            video_candidates.append(item)
        if acodec and acodec != "none":
            audio_candidates.append(item)

    video_candidates = _prefer_height_candidates(video_candidates, max_height)

    best_video = None
    best_audio = None
    best_video_score = -10**9
    best_audio_score = -10**9

    for item in video_candidates:
        vcodec = str(item.get("vcodec") or "")
        score = 0
        height = _candidate_height(item)
        if height is not None:
            score += min(height, max_height or 4320)
        lowered_vcodec = vcodec.lower()
        if "avc" in lowered_vcodec or "h264" in lowered_vcodec:
            score += 500
        elif "hev1" in lowered_vcodec or "h265" in lowered_vcodec or "hevc" in lowered_vcodec:
            score += 120
        elif "av01" in lowered_vcodec or "av1" in lowered_vcodec:
            score -= 260
        score += 100 if str(item.get("ext") or "").lower() == "mp4" else 0
        if score > best_video_score:
            best_video_score = score
            best_video = item

    for item in audio_candidates:
        score = 0
        try:
            score += min(int(item.get("abr") or 0), 320)
        except (TypeError, ValueError):
            pass
        lowered_acodec = str(item.get("acodec") or "").lower()
        if "mp4a" in lowered_acodec or "aac" in lowered_acodec:
            score += 220
        score += 50 if str(item.get("ext") or "").lower() in {"m4a", "mp4"} else 0
        if score > best_audio_score:
            best_audio_score = score
            best_audio = item

    return best_video, best_audio


def resolve_source_with_yt_dlp_fallback(
    source_url: str,
    *,
    display_name: str = "",
    headers: Mapping[str, Any] | None = None,
    quality: str = "max",
) -> BridgeResolvedSource:
    if not is_http_url(source_url):
        raise ValueError("Current source must start with http:// or https://")

    quality = normalize_quality_preference(quality)
    generated_url_hint = explain_generated_media_url(source_url)
    if generated_url_hint:
        raise ValueError(generated_url_hint)

    if is_probable_direct_media_url(source_url):
        merged_headers = filter_forward_headers(headers)
        return BridgeResolvedSource(
            source_url=source_url,
            media_url=source_url,
            display_name=display_name.strip() or display_name_from_source(source_url),
            headers=merged_headers,
            resolved_from_page=False,
        )

    yt_dlp = _load_yt_dlp_module()
    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "extract_flat": False,
        "format": _build_quality_format_selector(quality),
        "logger": _SilentYtdlpLogger(),
    }
    try:
        info = _extract_info_with_format_retry(yt_dlp, source_url, options)
    except Exception as exc:
        raise VideoPageExtractionError(f"Failed to resolve video page: {exc}") from exc

    if not isinstance(info, dict):
        raise VideoPageExtractionError("The extractor result is invalid.")

    title = str(display_name or info.get("title") or display_name_from_source(source_url)).strip() or display_name_from_source(source_url)
    merged_headers = filter_forward_headers(headers)
    merged_headers.update(filter_forward_headers(info.get("http_headers") or {}))

    video_stream, audio_stream = _extract_best_requested_streams(info, quality=quality)
    if video_stream is not None and audio_stream is not None:
        video_headers = dict(merged_headers)
        video_headers.update(filter_forward_headers(video_stream.get("http_headers") or {}))
        audio_headers = dict(merged_headers)
        audio_headers.update(filter_forward_headers(audio_stream.get("http_headers") or {}))
        return BridgeResolvedSource(
            source_url=source_url,
            media_url=str(video_stream.get("url") or "").strip(),
            display_name=title,
            headers=merged_headers,
            resolved_from_page=True,
            requires_local_mux=True,
            video_url=str(video_stream.get("url") or "").strip(),
            audio_url=str(audio_stream.get("url") or "").strip(),
            video_headers=video_headers,
            audio_headers=audio_headers,
        )

    direct_candidate = _pick_direct_media_candidate(info, quality=quality)
    if direct_candidate is None:
        raise VideoPageExtractionError("No directly playable single-URL media was found on the current page, and the page only exposed separated audio/video streams.")

    direct_url, direct_headers = direct_candidate
    merged_headers.update(direct_headers)
    return BridgeResolvedSource(
        source_url=source_url,
        media_url=direct_url,
        display_name=title,
        headers=merged_headers,
        resolved_from_page=True,
    )

def _smooth_target_dimensions(*, encoder_name: str, smooth_fallback: bool) -> tuple[int, int]:
    if encoder_name == "libx264" or smooth_fallback:
        return SMOOTH_SOFTWARE_MAX_WIDTH, SMOOTH_SOFTWARE_MAX_HEIGHT
    return SMOOTH_MAX_WIDTH, SMOOTH_MAX_HEIGHT


def _build_video_filter_chain(transcode_profile: str, *, encoder_name: str = "libx264", smooth_fallback: bool = False) -> str:
    profile = normalize_transcode_profile(transcode_profile)
    if profile == "smooth":
        max_width, max_height = _smooth_target_dimensions(
            encoder_name=encoder_name,
            smooth_fallback=smooth_fallback,
        )
        filters = [
            (
                f"scale=w='min({max_width},iw)':h='min({max_height},ih)':"
                "force_original_aspect_ratio=decrease:flags=fast_bilinear"
            ),
            "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        ]
        filters.append(f"fps={SMOOTH_TARGET_FPS}")
    else:
        filters = ["scale=trunc(iw/2)*2:trunc(ih/2)*2"]
    if encoder_name in {"h264_qsv", "h264_amf"}:
        filters.append("format=nv12")
    return ",".join(filters)


def _transcode_profile_defaults(profile: str) -> tuple[str, str, str]:
    if profile == "quality":
        return "medium", "18", "192k"
    if profile == "smooth":
        return "superfast", "25", "96k"
    return "veryfast", "23", "128k"


def _build_video_output_args(
    transcode_profile: str,
    *,
    encoder_name: str = "libx264",
    smooth_fallback: bool = False,
    live_hls: bool = False,
) -> tuple[list[str], str]:
    profile = normalize_transcode_profile(transcode_profile)
    preset, crf, audio_bitrate = _transcode_profile_defaults(profile)

    if encoder_name == "libx264":
        video_args = [
            "-c:v",
            "libx264",
            "-preset",
            preset,
            "-crf",
            crf,
            "-vf",
            _build_video_filter_chain(profile, encoder_name=encoder_name, smooth_fallback=smooth_fallback),
            "-pix_fmt",
            "yuv420p",
        ]
        if live_hls and profile == "smooth":
            video_args.extend(
                [
                    "-tune",
                    "zerolatency",
                    "-g",
                    "60",
                    "-keyint_min",
                    "60",
                    "-sc_threshold",
                    "0",
                ]
            )
        return video_args, audio_bitrate

    if encoder_name == "h264_nvenc":
        cq = "21" if profile == "quality" else "23"
        return (
            [
                "-c:v",
                "h264_nvenc",
                "-preset",
                "p5",
                "-rc",
                "vbr",
                "-cq",
                cq,
                "-b:v",
                "0",
                "-vf",
                _build_video_filter_chain(profile, encoder_name=encoder_name, smooth_fallback=smooth_fallback),
                "-pix_fmt",
                "yuv420p",
            ],
            audio_bitrate,
        )

    if encoder_name == "h264_qsv":
        global_quality = "20" if profile == "quality" else "23"
        return (
            [
                "-c:v",
                "h264_qsv",
                "-global_quality",
                global_quality,
                "-preset",
                "medium",
                "-vf",
                _build_video_filter_chain(profile, encoder_name=encoder_name, smooth_fallback=smooth_fallback),
            ],
            audio_bitrate,
        )

    if encoder_name == "h264_amf":
        qp_p = "20" if profile == "quality" else "23"
        qp_i = "18" if profile == "quality" else "21"
        return (
            [
                "-c:v",
                "h264_amf",
                "-usage",
                "transcoding",
                "-quality",
                "quality",
                "-rc",
                "cqp",
                "-qp_i",
                qp_i,
                "-qp_p",
                qp_p,
                "-vf",
                _build_video_filter_chain(profile, encoder_name=encoder_name, smooth_fallback=smooth_fallback),
            ],
            audio_bitrate,
        )

    raise RuntimeError(f"Unsupported encoder: {encoder_name}")


def _append_transcode_output_options(
    command: list[str],
    output_file: Path,
    transcode_profile: str,
    *,
    encoder_name: str = "libx264",
    smooth_fallback: bool = False,
) -> list[str]:
    profile = normalize_transcode_profile(transcode_profile)
    video_args, audio_bitrate = _build_video_output_args(
        profile,
        encoder_name=encoder_name,
        smooth_fallback=smooth_fallback,
    )

    command.extend(
        [
            "-map_metadata",
            "-1",
            "-sn",
            "-dn",
            *video_args,
            "-movflags",
            "+faststart",
            "-c:a",
            "aac",
            "-b:a",
            audio_bitrate,
            "-ac",
            "2",
            str(output_file),
        ]
    )
    return command


def _build_live_hls_output_command(
    ffmpeg_path: Path,
    input_args: list[str],
    map_args: list[str],
    output_dir: Path,
    transcode_profile: str,
    *,
    encoder_name: str = "libx264",
    smooth_fallback: bool = False,
) -> tuple[list[str], Path]:
    profile = normalize_transcode_profile(transcode_profile)
    playlist_path = output_dir / "index.m3u8"
    segment_pattern = output_dir / "segment_%05d.ts"
    video_args, audio_bitrate = _build_video_output_args(
        profile,
        encoder_name=encoder_name,
        smooth_fallback=smooth_fallback,
        live_hls=True,
    )

    command = [
        str(ffmpeg_path),
        "-y",
        *input_args,
        *map_args,
        "-map_metadata",
        "-1",
        "-sn",
        "-dn",
        *video_args,
        "-c:a",
        "aac",
        "-b:a",
        audio_bitrate,
        "-ac",
        "2",
        "-f",
        "hls",
        "-hls_time",
        str(LIVE_TRANSCODE_SEGMENT_SECONDS),
        "-hls_list_size",
        "0",
        "-hls_playlist_type",
        "event",
        "-hls_flags",
        LIVE_HLS_FLAGS,
        "-hls_segment_filename",
        str(segment_pattern),
        str(playlist_path),
    ]
    return command, playlist_path


def _run_transcode_command(
    ffmpeg_path: Path,
    input_args: list[str],
    map_args: list[str],
    output_file: Path,
    transcode_profile: str,
) -> Path:
    profile = normalize_transcode_profile(transcode_profile)
    hardware_encoder = detect_preferred_h264_hardware_encoder(ffmpeg_path) if profile == "smooth" else ""
    attempts = _build_transcode_attempts(profile, hardware_encoder=hardware_encoder)
    failure_sections: list[str] = []

    for attempt in attempts:
        command = [str(ffmpeg_path), "-y", *input_args, *map_args]
        _append_transcode_output_options(
            command,
            output_file,
            attempt.effective_profile,
            encoder_name=attempt.encoder_name,
            smooth_fallback=attempt.smooth_fallback,
        )
        result = subprocess.run(command, capture_output=True, text=True, errors="ignore")
        if result.returncode == 0 and output_file.exists() and output_file.stat().st_size > 0:
            return output_file

        stderr = (result.stderr or result.stdout or "unknown ffmpeg error").strip()
        label = _format_transcode_attempt_label(attempt)
        failure_sections.extend([f"{label} failed:"])
        failure_sections.extend(stderr.splitlines()[-8:])

        if output_file.exists():
            try:
                output_file.unlink()
            except OSError:
                pass

    tail = "\n".join(failure_sections[-20:])
    raise RuntimeError(f"ffmpeg failed to transcode remote media.\n{tail}")


def _find_ffprobe_for_ffmpeg(ffmpeg_path: Path) -> Path | None:
    candidates = [ffmpeg_path.with_name("ffprobe.exe" if ffmpeg_path.suffix.lower() == ".exe" else "ffprobe")]
    which_value = shutil.which("ffprobe")
    if which_value:
        candidates.append(Path(which_value))
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _probe_media_codecs(ffmpeg_path: Path, media_source: str) -> tuple[set[str], set[str]] | None:
    ffprobe_path = _find_ffprobe_for_ffmpeg(ffmpeg_path)
    if ffprobe_path is None:
        return None

    command = [
        str(ffprobe_path),
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type,codec_name",
        "-of",
        "json",
        media_source,
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            errors="ignore",
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    if result.returncode != 0:
        return None

    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return None

    streams = payload.get("streams")
    if not isinstance(streams, list):
        return None

    video_codecs: set[str] = set()
    audio_codecs: set[str] = set()
    for item in streams:
        if not isinstance(item, dict):
            continue
        codec_type = str(item.get("codec_type") or "").strip().lower()
        codec_name = str(item.get("codec_name") or "").strip().lower()
        if not codec_name:
            continue
        if codec_type == "video":
            video_codecs.add(codec_name)
        elif codec_type == "audio":
            audio_codecs.add(codec_name)

    if not video_codecs:
        return None
    return video_codecs, audio_codecs


def _probe_remote_media_codecs(ffmpeg_path: Path, media_url: str) -> tuple[set[str], set[str]] | None:
    return _probe_media_codecs(ffmpeg_path, media_url)


def _is_fast_copy_mp4_compatible(video_codecs: set[str], audio_codecs: set[str]) -> bool:
    return video_codecs == {"h264"} and audio_codecs.issubset({"aac"})


def _run_fast_copy_mux(
    ffmpeg_path: Path,
    input_args: list[str],
    map_args: list[str],
    output_file: Path,
    *,
    attempt_arg_sets: list[list[str]],
) -> Path | None:
    for extra_args in attempt_arg_sets:
        command = [
            str(ffmpeg_path),
            "-y",
            *input_args,
            *map_args,
            "-map_metadata",
            "-1",
            "-sn",
            "-dn",
            *extra_args,
            str(output_file),
        ]
        result = subprocess.run(command, capture_output=True, text=True, errors="ignore")
        if result.returncode == 0 and output_file.exists() and output_file.stat().st_size > 0:
            return output_file
        if output_file.exists():
            try:
                output_file.unlink()
            except OSError:
                pass
    return None


def _try_fast_compatible_hls_remux(ffmpeg_path: Path, media_url: str, output_file: Path) -> Path | None:
    codecs = _probe_remote_media_codecs(ffmpeg_path, media_url)
    if codecs is None:
        return None

    video_codecs, audio_codecs = codecs
    if not _is_fast_copy_mp4_compatible(video_codecs, audio_codecs):
        return None

    return _run_fast_copy_mux(
        ffmpeg_path,
        ["-i", media_url],
        ["-map", "0:v:0", "-map", "0:a:0?"],
        output_file,
        attempt_arg_sets=[
            ["-c:v", "copy", "-c:a", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart"],
            ["-c:v", "copy", "-c:a", "copy", "-movflags", "+faststart"],
        ],
    )


def _try_fast_compatible_local_stream_mux(
    ffmpeg_path: Path,
    video_input: Path,
    audio_input: Path,
    output_file: Path,
) -> Path | None:
    video_probe = _probe_media_codecs(ffmpeg_path, str(video_input))
    audio_probe = _probe_media_codecs(ffmpeg_path, str(audio_input))
    if video_probe is None or audio_probe is None:
        return None

    video_codecs, _ = video_probe
    _, audio_codecs = audio_probe
    if not _is_fast_copy_mp4_compatible(video_codecs, audio_codecs):
        return None

    return _run_fast_copy_mux(
        ffmpeg_path,
        ["-i", str(video_input), "-i", str(audio_input)],
        ["-map", "0:v:0", "-map", "1:a:0"],
        output_file,
        attempt_arg_sets=[
            ["-c:v", "copy", "-c:a", "copy", "-movflags", "+faststart"],
            ["-c:v", "copy", "-c:a", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart"],
        ],
    )


def mux_remote_streams_to_compatible_mp4(
    *,
    video_url: str,
    audio_url: str,
    display_name: str,
    video_headers: Mapping[str, Any] | None = None,
    audio_headers: Mapping[str, Any] | None = None,
    transcode_profile: str = "standard",
) -> Path:
    ffmpeg_path = find_ffmpeg()
    REMOTE_MUX_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    profile = normalize_transcode_profile(transcode_profile)
    output_file = REMOTE_MUX_CACHE_DIR / (
        f"{_safe_output_stem(display_name)}_{profile}_{_hash_remote_streams(video_url, audio_url)}.mp4"
    )
    if output_file.exists() and output_file.stat().st_size > 0:
        return output_file
    video_input = output_file.with_suffix(".video.m4s")
    audio_input = output_file.with_suffix(".audio.m4s")

    def download_remote_stream(url: str, values: Mapping[str, Any] | None, destination: Path) -> None:
        if destination.exists() and destination.stat().st_size > 0:
            return
        request = urllib.request.Request(url, headers=filter_forward_headers(values))
        with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as handle:
            while True:
                chunk = response.read(1024 * 256)
                if not chunk:
                    break
                handle.write(chunk)

    download_remote_stream(video_url, video_headers, video_input)
    download_remote_stream(audio_url, audio_headers, audio_input)

    if profile in {"standard", "quality"}:
        fast_muxed = _try_fast_compatible_local_stream_mux(ffmpeg_path, video_input, audio_input, output_file)
        if fast_muxed is not None:
            return fast_muxed

    return _run_transcode_command(
        ffmpeg_path,
        ["-i", str(video_input), "-i", str(audio_input)],
        ["-map", "0:v:0", "-map", "1:a:0"],
        output_file,
        profile,
    )


def _looks_like_hls_media_url(media_url: str) -> bool:
    return ".m3u8" in str(media_url or "").lower()


def start_live_hls_transcode_session(
    *,
    media_url: str,
    display_name: str,
    headers: Mapping[str, Any] | None = None,
    transcode_profile: str = "quality",
    startup_buffer_seconds: float = LIVE_TRANSCODE_STARTUP_BUFFER_SECONDS,
) -> LiveHlsTranscodeSession:
    ffmpeg_path = find_ffmpeg()
    LIVE_TRANSCODE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    profile = normalize_transcode_profile(transcode_profile)
    hardware_encoder = detect_preferred_h264_hardware_encoder(ffmpeg_path) if profile == "smooth" else ""
    attempts = _build_transcode_attempts(profile, hardware_encoder=hardware_encoder)
    failure_sections: list[str] = []

    for attempt in attempts:
        session_dir = LIVE_TRANSCODE_CACHE_DIR / f"{_safe_output_stem(display_name)}_{attempt.effective_profile}_{uuid.uuid4().hex[:10]}"
        session_dir.mkdir(parents=True, exist_ok=True)

        proxy_server = MediaHttpServer()
        proxied_media_url = proxy_server.start_remote(media_url, display_name, headers=filter_forward_headers(headers))
        command, playlist_path = _build_live_hls_output_command(
            ffmpeg_path,
            ["-i", proxied_media_url],
            ["-map", "0:v:0", "-map", "0:a:0?"],
            session_dir,
            attempt.effective_profile,
            encoder_name=attempt.encoder_name,
            smooth_fallback=attempt.smooth_fallback,
        )
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            errors="ignore",
        )
        session = LiveHlsTranscodeSession(
            output_dir=session_dir,
            playlist_path=playlist_path,
            process=process,
            proxy_servers=[proxy_server],
        )
        try:
            session.wait_until_ready(startup_buffer_seconds=startup_buffer_seconds)
            return session
        except RuntimeError as exc:
            label = _format_transcode_attempt_label(attempt)
            failure_sections.extend([f"{label} failed:"])
            failure_sections.extend(str(exc).splitlines()[-8:])
            session.stop()

    tail = "\n".join(failure_sections[-20:])
    raise RuntimeError(f"ffmpeg failed to start live HLS transcode.\n{tail}")


def transcode_remote_media_to_compatible_mp4(
    *,
    media_url: str,
    display_name: str,
    headers: Mapping[str, Any] | None = None,
    transcode_profile: str = "quality",
) -> Path:
    ffmpeg_path = find_ffmpeg()
    REMOTE_TRANSCODE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    profile = normalize_transcode_profile(transcode_profile)
    output_file = REMOTE_TRANSCODE_CACHE_DIR / (
        f"{_safe_output_stem(display_name)}_{profile}_{_hash_remote_media(media_url, profile)}.mp4"
    )
    if output_file.exists() and output_file.stat().st_size > 0:
        return output_file

    proxy_server = MediaHttpServer()
    proxied_media_url = proxy_server.start_remote(media_url, display_name, headers=filter_forward_headers(headers))
    try:
        if profile == "quality" and _looks_like_hls_media_url(media_url):
            fast_remuxed = _try_fast_compatible_hls_remux(ffmpeg_path, proxied_media_url, output_file)
            if fast_remuxed is not None:
                return fast_remuxed
        return _run_transcode_command(
            ffmpeg_path,
            ["-i", proxied_media_url],
            ["-map", "0:v:0", "-map", "0:a:0?"],
            output_file,
            profile,
        )
    finally:
        proxy_server.stop()


class TampermonkeyBridgeService:
    def __init__(self) -> None:
        self.http_server = MediaHttpServer()
        self._lock = threading.RLock()
        self._playback_lock = threading.RLock()
        self._devices_by_location: dict[str, DlnaDevice] = {}
        self._tasks: dict[str, BridgeTask] = {}
        self.current_controller: DlnaController | None = None
        self.current_device: DlnaDevice | None = None
        self.current_live_transcode_session: LiveHlsTranscodeSession | None = None

    def health(self) -> dict[str, Any]:
        with self._lock:
            device_count = len(self._devices_by_location)
            current_device = self.current_device.display_name if self.current_device else ""
        return {
            "ok": True,
            "service": "screen-casting-tampermonkey-bridge",
            "extractor_available": has_web_video_extractor(),
            "cached_device_count": device_count,
            "current_device": current_device,
            "task_count": len(self._tasks),
        }

    def list_devices(self, timeout: float = DEFAULT_DISCOVERY_TIMEOUT) -> list[dict[str, Any]]:
        devices = discover_devices(timeout=timeout)
        with self._lock:
            self._devices_by_location = {device.location: device for device in devices}
        return [self._serialize_device(device) for device in devices]

    def resolve_source(
        self,
        source_url: str,
        *,
        display_name: str = "",
        headers: Mapping[str, Any] | None = None,
        quality: str = "max",
    ) -> BridgeResolvedSource:
        quality = normalize_quality_preference(quality)
        if quality != "max":
            return resolve_source_with_yt_dlp_fallback(
                source_url,
                display_name=display_name,
                headers=headers,
                quality=quality,
            )

        if _prefers_mux_aware_page_resolution(source_url):
            try:
                return resolve_source_with_yt_dlp_fallback(
                    source_url,
                    display_name=display_name,
                    headers=headers,
                    quality=quality,
                )
            except VideoPageExtractionError:
                pass

        try:
            resolved = resolve_media_source(source_url)
        except VideoPageExtractionError:
            return resolve_source_with_yt_dlp_fallback(
                source_url,
                display_name=display_name,
                headers=headers,
                quality=quality,
            )

        merged_headers = filter_forward_headers(headers)
        merged_headers.update(filter_forward_headers(resolved.headers))
        effective_name = display_name.strip() or resolved.display_name
        return BridgeResolvedSource(
            source_url=source_url,
            media_url=resolved.media_url,
            display_name=effective_name,
            headers=merged_headers,
            resolved_from_page=resolved.resolved_from_page,
        )

    def cast(
        self,
        *,
        device_location: str,
        source_url: str,
        display_name: str = "",
        headers: Mapping[str, Any] | None = None,
        quality: str = "max",
        transcode_profile: str = "standard",
        speed: str = "1",
        volume: int | None = None,
        discovery_timeout: float = DEFAULT_DISCOVERY_TIMEOUT,
    ) -> dict[str, Any]:
        return self._cast_sync(
            device_location=device_location,
            source_url=source_url,
            display_name=display_name,
            headers=headers,
            quality=quality,
            transcode_profile=transcode_profile,
            speed=speed,
            volume=volume,
            discovery_timeout=discovery_timeout,
            progress_callback=None,
        )

    def start_cast_task(
        self,
        *,
        device_location: str,
        source_url: str,
        display_name: str = "",
        headers: Mapping[str, Any] | None = None,
        quality: str = "max",
        transcode_profile: str = "standard",
        speed: str = "1",
        volume: int | None = None,
        discovery_timeout: float = DEFAULT_DISCOVERY_TIMEOUT,
    ) -> dict[str, Any]:
        task_id = uuid.uuid4().hex
        task = BridgeTask(
            task_id=task_id,
            status="queued",
            message="Cast task queued.",
        )
        with self._lock:
            self._tasks[task_id] = task

        thread = threading.Thread(
            target=self._run_cast_task,
            kwargs={
                "task_id": task_id,
                "device_location": device_location,
                "source_url": source_url,
                "display_name": display_name,
                "headers": headers,
                "quality": quality,
                "transcode_profile": transcode_profile,
                "speed": speed,
                "volume": volume,
                "discovery_timeout": discovery_timeout,
            },
            daemon=True,
        )
        thread.start()
        return {
            "ok": True,
            "accepted": True,
            "task_id": task_id,
            "status": "queued",
            "message": "Cast task accepted.",
        }

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            return self._serialize_task(task)

    def _cast_sync(
        self,
        *,
        device_location: str,
        source_url: str,
        display_name: str = "",
        headers: Mapping[str, Any] | None = None,
        quality: str = "max",
        transcode_profile: str = "standard",
        speed: str = "1",
        volume: int | None = None,
        discovery_timeout: float = DEFAULT_DISCOVERY_TIMEOUT,
        progress_callback: Any = None,
    ) -> dict[str, Any]:
        profile = normalize_transcode_profile(transcode_profile)
        smooth_hardware_encoder = ""
        if profile == "smooth":
            try:
                smooth_hardware_encoder = detect_preferred_h264_hardware_encoder(find_ffmpeg())
            except FfmpegNotFoundError:
                smooth_hardware_encoder = ""
        self._notify_progress(progress_callback, "resolving", "Resolving page source...")
        resolved = self.resolve_source(
            source_url,
            display_name=display_name,
            headers=headers,
            quality=quality,
        )
        self._notify_progress(progress_callback, "scanning", "Locating target TV...")
        device = self._find_device(device_location, discovery_timeout=discovery_timeout)
        served_media_url = ""
        player_url = ""
        metadata_source = resolved.media_url
        live_transcode_session: LiveHlsTranscodeSession | None = None

        with self._lock:
            previous_controller = self.current_controller
            previous_live_transcode_session = self.current_live_transcode_session
            self.current_controller = None
            self.current_device = None
            self.current_live_transcode_session = None

        with self._playback_lock:
            if previous_controller is not None:
                try:
                    previous_controller.stop()
                except (ValueError, HTTPError, URLError, OSError):
                    pass
            if previous_live_transcode_session is not None:
                previous_live_transcode_session.stop()

            if resolved.requires_local_mux:
                if profile == "standard":
                    progress_message = "Downloading streams and preparing a compatible MP4..."
                elif profile == "quality":
                    progress_message = "Downloading streams and preparing a high-quality compatible MP4..."
                elif smooth_hardware_encoder:
                    progress_message = "Downloading streams and preparing stable 30fps playback with hardware encoding..."
                else:
                    progress_message = "Downloading streams and preparing stable 30fps playback with software encoding..."
                self._notify_progress(progress_callback, "muxing", progress_message)
                muxed_file = mux_remote_streams_to_compatible_mp4(
                    video_url=resolved.video_url,
                    audio_url=resolved.audio_url,
                    display_name=resolved.display_name,
                    video_headers=resolved.video_headers or resolved.headers,
                    audio_headers=resolved.audio_headers or resolved.headers,
                    transcode_profile=profile,
                )
                served_media_url = self.http_server.start(str(muxed_file))
                metadata_source = str(muxed_file)
            elif profile != "standard":
                if profile == "quality":
                    progress_message = "Applying local quality optimization..."
                elif smooth_hardware_encoder:
                    progress_message = "Preparing stable 30fps playback with hardware encoding..."
                else:
                    progress_message = "Preparing stable 30fps playback with software encoding..."
                self._notify_progress(progress_callback, "transcoding", progress_message)
                if profile == "smooth" and _looks_like_hls_media_url(resolved.media_url):
                    self._notify_progress(
                        progress_callback,
                        "preloading",
                        f"Preloading about {int(LIVE_TRANSCODE_STARTUP_BUFFER_SECONDS)} seconds of transcoded video before casting...",
                    )
                    live_transcode_session = start_live_hls_transcode_session(
                        media_url=resolved.media_url,
                        display_name=resolved.display_name,
                        headers=resolved.headers,
                        transcode_profile=profile,
                        startup_buffer_seconds=LIVE_TRANSCODE_STARTUP_BUFFER_SECONDS,
                    )
                    served_media_url = self.http_server.start_hls(str(live_transcode_session.playlist_path), resolved.display_name)
                    metadata_source = str(live_transcode_session.playlist_path)
                    with self._lock:
                        self.current_live_transcode_session = live_transcode_session
                else:
                    transcoded_file = transcode_remote_media_to_compatible_mp4(
                        media_url=resolved.media_url,
                        display_name=resolved.display_name,
                        headers=resolved.headers,
                        transcode_profile=profile,
                    )
                    served_media_url = self.http_server.start(str(transcoded_file))
                    metadata_source = str(transcoded_file)
            else:
                self._notify_progress(progress_callback, "serving", "Preparing media URL for the TV...")
                if ".m3u8" in str(resolved.media_url or "").lower():
                    self._notify_progress(progress_callback, "preloading", "Preloading a few HLS segments for smoother startup...")
                served_media_url = self.http_server.start_remote(
                    resolved.media_url,
                    resolved.display_name,
                    headers=resolved.headers,
                )
                metadata_source = resolved.media_url
            player_url = self.http_server.player_url or ""

            controller = DlnaController(device)
            try:
                self._notify_progress(progress_callback, "casting", "Sending playback command to the TV...")
                set_media_with_title(controller, served_media_url, metadata_source, resolved.display_name)
                controller.play(str(speed or "1").strip() or "1")
                if volume is not None:
                    try:
                        controller.set_volume(int(volume))
                    except (ValueError, HTTPError, URLError, OSError):
                        pass
            except (ValueError, HTTPError, URLError, OSError, RuntimeError) as exc:
                raise CastStartError(str(exc), served_media_url=served_media_url, player_url=player_url) from exc

            with self._lock:
                self.current_controller = controller
                self.current_device = device

        return {
            "ok": True,
            "device": self._serialize_device(device),
            "resolved_source": self._serialize_resolved_source(resolved),
            "served_media_url": served_media_url,
            "player_url": player_url,
        }

    def _run_cast_task(
        self,
        *,
        task_id: str,
        device_location: str,
        source_url: str,
        display_name: str,
        headers: Mapping[str, Any] | None,
        quality: str,
        transcode_profile: str,
        speed: str,
        volume: int | None,
        discovery_timeout: float,
    ) -> None:
        self._update_task(task_id, status="running", message="Cast task started.")
        try:
            result = self._cast_sync(
                device_location=device_location,
                source_url=source_url,
                display_name=display_name,
                headers=headers,
                quality=quality,
                transcode_profile=transcode_profile,
                speed=speed,
                volume=volume,
                discovery_timeout=discovery_timeout,
                progress_callback=lambda status, message: self._update_task(task_id, status=status, message=message),
            )
        except Exception as exc:
            self._update_task(task_id, status="failed", message=str(exc), error=str(exc))
            return
        self._update_task(task_id, status="completed", message="Cast task completed.", result=result)

    def stop(self) -> dict[str, Any]:
        with self._playback_lock:
            with self._lock:
                controller = self.current_controller
                live_transcode_session = self.current_live_transcode_session
                device_name = self.current_device.display_name if self.current_device else ""
                self.current_controller = None
                self.current_device = None
                self.current_live_transcode_session = None

            if controller is not None:
                try:
                    controller.stop()
                except (ValueError, HTTPError, URLError, OSError):
                    pass

            if live_transcode_session is not None:
                live_transcode_session.stop()

            self.http_server.stop()
        return {
            "ok": True,
            "stopped": True,
            "device": device_name,
        }

    def close(self) -> None:
        self.stop()

    def _find_device(self, device_location: str, *, discovery_timeout: float) -> DlnaDevice:
        location = device_location.strip()
        if not location:
            raise ValueError("Device location is required. Scan devices and select a TV first.")

        with self._lock:
            cached = self._devices_by_location.get(location)
        if cached is not None:
            return cached

        devices = discover_devices(timeout=discovery_timeout)
        with self._lock:
            self._devices_by_location = {device.location: device for device in devices}
            cached = self._devices_by_location.get(location)
        if cached is None:
            raise ValueError("The selected TV was not found. Rescan devices in the Tampermonkey panel and try again.")
        return cached

    def _serialize_device(self, device: DlnaDevice) -> dict[str, Any]:
        return {
            "location": device.location,
            "usn": device.usn,
            "friendly_name": device.friendly_name,
            "manufacturer": device.manufacturer,
            "model_name": device.model_name,
            "display_name": device.display_name,
            "supports_media_cast": device.supports_media_cast,
            "supports_volume": device.rendering_control is not None,
        }

    def _serialize_resolved_source(self, source: BridgeResolvedSource) -> dict[str, Any]:
        return {
            "source_url": source.source_url,
            "media_url": source.media_url,
            "display_name": source.display_name,
            "headers": dict(source.headers),
            "resolved_from_page": source.resolved_from_page,
            "requires_local_mux": source.requires_local_mux,
            "video_url": source.video_url,
            "audio_url": source.audio_url,
        }

    def _notify_progress(self, callback: Any, status: str, message: str) -> None:
        if callable(callback):
            callback(status, message)

    def _update_task(
        self,
        task_id: str,
        *,
        status: str,
        message: str,
        result: dict[str, Any] | None = None,
        error: str = "",
    ) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task.status = status
            task.message = message
            task.updated_at = time.time()
            if result is not None:
                task.result = result
            if error:
                task.error = error

    def _serialize_task(self, task: BridgeTask) -> dict[str, Any]:
        return {
            "ok": True,
            "task_id": task.task_id,
            "status": task.status,
            "message": task.message,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
            "result": task.result,
            "error": task.error,
        }


class BridgeRequestHandler(BaseHTTPRequestHandler):
    server_version = "TampermonkeyBridge/0.1"

    def do_OPTIONS(self) -> None:
        self._send_headers(204, "text/plain; charset=utf-8", 0)

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/":
            body = self._build_index_html().encode("utf-8")
            self._send_headers(200, "text/html; charset=utf-8", len(body))
            self.wfile.write(body)
            return
        if parsed.path == "/api/health":
            self._send_json(200, self._service.health())
            return
        if parsed.path == "/api/devices":
            timeout = self._float_query_value(parsed.query, "timeout", DEFAULT_DISCOVERY_TIMEOUT)
            try:
                payload = {
                    "ok": True,
                    "devices": self._service.list_devices(timeout=timeout),
                }
            except OSError as exc:
                self._send_json(502, {"ok": False, "error": str(exc)})
                return
            self._send_json(200, payload)
            return
        if parsed.path.startswith("/api/tasks/"):
            task_id = parsed.path.rsplit("/", 1)[-1].strip()
            payload = self._service.get_task(task_id)
            if payload is None:
                self._send_json(404, {"ok": False, "error": "Task not found"})
                return
            self._send_json(200, payload)
            return
        self._send_json(404, {"ok": False, "error": "Not found"})

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        try:
            body = self._read_json_body()
        except ValueError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
            return

        if parsed.path == "/api/resolve":
            self._handle_resolve(body)
            return
        if parsed.path == "/api/cast":
            self._handle_cast(body)
            return
        if parsed.path == "/api/stop":
            self._send_json(200, self._service.stop())
            return
        self._send_json(404, {"ok": False, "error": "Not found"})

    def log_message(self, format: str, *args) -> None:
        return

    @property
    def _service(self) -> TampermonkeyBridgeService:
        return self.server.bridge_service  # type: ignore[attr-defined]

    def _handle_resolve(self, body: dict[str, Any]) -> None:
        source_url = str(body.get("source_url") or body.get("url") or "").strip()
        display_name = str(body.get("display_name") or "").strip()
        headers = body.get("headers")
        quality = normalize_quality_preference(body.get("quality"))
        try:
            resolved = self._service.resolve_source(
                source_url,
                display_name=display_name,
                headers=headers,
                quality=quality,
            )
        except VideoPageExtractionUnavailable as exc:
            self._send_json(503, {"ok": False, "error": str(exc)})
            return
        except (ValueError, VideoPageExtractionError) as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
            return

        self._send_json(
            200,
            {
                "ok": True,
                "resolved_source": self._service._serialize_resolved_source(resolved),
            },
        )

    def _handle_cast(self, body: dict[str, Any]) -> None:
        source_url = str(body.get("source_url") or body.get("url") or "").strip()
        device_location = str(body.get("device_location") or body.get("location") or "").strip()
        display_name = str(body.get("display_name") or "").strip()
        headers = body.get("headers")
        quality = normalize_quality_preference(body.get("quality"))
        transcode_profile = normalize_transcode_profile(body.get("transcode_profile"))
        speed = str(body.get("speed") or "1")
        volume = body.get("volume")
        async_mode = bool(body.get("async"))
        discovery_timeout = self._safe_float(body.get("discovery_timeout"), DEFAULT_DISCOVERY_TIMEOUT)

        try:
            if async_mode:
                payload = self._service.start_cast_task(
                    device_location=device_location,
                    source_url=source_url,
                    display_name=display_name,
                    headers=headers,
                    quality=quality,
                    transcode_profile=transcode_profile,
                    speed=speed,
                    volume=int(volume) if volume not in (None, "") else None,
                    discovery_timeout=discovery_timeout,
                )
                self._send_json(202, payload)
                return

            payload = self._service.cast(
                device_location=device_location,
                source_url=source_url,
                display_name=display_name,
                headers=headers,
                quality=quality,
                transcode_profile=transcode_profile,
                speed=speed,
                volume=int(volume) if volume not in (None, "") else None,
                discovery_timeout=discovery_timeout,
            )
        except VideoPageExtractionUnavailable as exc:
            self._send_json(503, {"ok": False, "error": str(exc)})
            return
        except (ValueError, VideoPageExtractionError) as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
            return
        except CastStartError as exc:
            self._send_json(
                502,
                {
                    "ok": False,
                    "error": str(exc),
                    "served_media_url": exc.served_media_url,
                    "player_url": exc.player_url,
                },
            )
            return
        except OSError as exc:
            self._send_json(502, {"ok": False, "error": str(exc)})
            return

        self._send_json(200, payload)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSON parse failed: {exc.msg}") from exc
        if not isinstance(payload, dict):
            raise ValueError("Request body must be a JSON object.")
        return payload

    def _float_query_value(self, query: str, name: str, default: float) -> float:
        value = parse_qs(query).get(name, [""])[0]
        return self._safe_float(value, default)

    def _safe_float(self, value: Any, default: float) -> float:
        if value in (None, ""):
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _build_index_html(self) -> str:
        return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>婵炲苯婀辩亸銊╁箮閺囩偟娼屾俊妞煎劜鐢挳寮靛鍛潳</title>
  <style>
    body {
      margin: 0;
      padding: 32px 20px;
      background: #0b1220;
      color: #e2e8f0;
      font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
    }
    .shell {
      max-width: 860px;
      margin: 0 auto;
    }
    .card {
      background: #111b30;
      border: 1px solid #334155;
      border-radius: 16px;
      padding: 20px;
      margin-top: 16px;
    }
    code {
      background: #18253f;
      border-radius: 8px;
      padding: 2px 6px;
    }
    pre {
      background: #09101d;
      border-radius: 12px;
      padding: 14px;
      overflow: auto;
    }
  </style>
</head>
<body>
  <main class="shell">
    <h1>婵炲苯婀辩亸銊╁箮閺囩偟娼屾俊妞煎劜鐢挳寮靛鍛潳鐎瑰憡褰冮幆搴ㄥ礉?/h1>
    <div class="card">
      <p>閺夆晜鐟ら柌婊堝嫉椤掆偓濠€鎾嫉瀹ュ懎顫ゅ〒姘⊕鐞涖儵鎮壕瀣闁哄牜鍓濋惃鐔兼偨椤帞绀夐柣顫妽濞肩敻骞嶉锝呬紟闁活澀绲婚～瀣Υ娴ｅ彨鎺楀几閹邦喚绉瑰銈勭祷椤锛愰幋婵囧嬀闁秆€鍋撴鐐茬埣閳ь剚淇虹换?DLNA 闁硅埖娲熼埀顑跨閸╁矂鎮芥担鍐炬綊闁?/p>
      <p>闁稿鍎遍幃宥呂涢埀顒勫蓟閵夘垳绐?code>/api/health</code></p>
      <p>闁规鍋呭璺ㄦ媼閹屾У闁?code>/api/devices</code></p>
      <p>閻熸瑱绲鹃悗鐣屾喆閸℃侗鏆ラ柨?code>POST /api/resolve</code></p>
      <p>鐎殿喒鍋撳┑顔碱儐婵洨浠﹁箛銉х獥<code>POST /api/cast</code></p>
      <p>闁稿绮嶉娑㈠箮閺囩偟娼岄柨?code>POST /api/stop</code></p>
    </div>
    <div class="card">
      <p>濠碘€冲€归悘澶娾柦閸︻厼鐨戦柤瀛樼濠€鐗堟交閻愭壆鐟濆☉鎾愁煭缁辨繄鎷犳搴樷偓妯兼媼閵堝牆澹栭柡鍫墴閸ｇ兘鏌婂鍥╂瀭闁汇劌瀚棢闁规亽鍎卞﹢鎾锤閳ь剟宕仦鐣岀Ъ闁告挸绉堕顒勫矗閿濆嫮顏遍柤閿嬬暘閳?/p>
      <pre>python run_bridge.py --host 127.0.0.1 --port 9527</pre>
    </div>
  </main>
</body>
</html>"""

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_headers(status, "application/json; charset=utf-8", len(body))
        self.wfile.write(body)

    def _send_headers(self, status: int, content_type: str, content_length: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(content_length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.end_headers()


def create_bridge_server(
    host: str = "127.0.0.1",
    port: int = 9527,
    *,
    service: TampermonkeyBridgeService | None = None,
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), BridgeRequestHandler)
    server.bridge_service = service or TampermonkeyBridgeService()  # type: ignore[attr-defined]
    return server


def run_bridge_server(host: str = "127.0.0.1", port: int = 9527) -> None:
    server = create_bridge_server(host=host, port=port)
    service = server.bridge_service  # type: ignore[attr-defined]
    try:
        print(f"Tampermonkey bridge listening on http://{host}:{port}")
        server.serve_forever()
    except KeyboardInterrupt:
        print("Tampermonkey bridge stopped.")
    finally:
        service.close()
        server.server_close()
