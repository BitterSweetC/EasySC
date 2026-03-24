from __future__ import annotations

import hashlib
import html
import json
import os
import re
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
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
QUALITY_ALIASES = {
    "": "max",
    "max": "max",
    "best": "max",
    "highest": "max",
    "source": "max",
    "original": "max",
    "1080": "1080p",
    "1080p": "1080p",
    "720": "720p",
    "720p": "720p",
}
QUALITY_MAX_HEIGHTS = {
    "max": None,
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
    "smooth60": "smooth",
    "60fps": "smooth",
    "fps60": "smooth",
}


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


class CastStartError(RuntimeError):
    def __init__(self, message: str, *, served_media_url: str = "", player_url: str = "") -> None:
        super().__init__(message)
        self.served_media_url = served_media_url
        self.player_url = player_url


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
        raise ValueError("目标设备不支持 AVTransport")
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
            "当前环境未安装 yt-dlp，无法把视频播放页自动解析成直投地址。请先在项目虚拟环境中安装 yt-dlp。"
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


def _profile_label(value: Any) -> str:
    profile = normalize_transcode_profile(value)
    if profile == "quality":
        return "quality"
    if profile == "smooth":
        return "smooth 60fps"
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
            TranscodeAttempt(effective_profile="smooth", encoder_name=hardware_encoder, smooth_fallback=True, hardware=True),
            TranscodeAttempt(effective_profile="quality", encoder_name="libx264", downgraded=True),
        ]
    return [TranscodeAttempt(effective_profile="quality", encoder_name="libx264", downgraded=True)]


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
    }
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(source_url, download=False)
    except Exception as exc:
        raise VideoPageExtractionError(f"解析视频播放页失败：{exc}") from exc

    if not isinstance(info, dict):
        raise VideoPageExtractionError("解析结果无效，未找到可投送的视频信息。")

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
        raise VideoPageExtractionError("未能从当前播放页解析出可直接投送的单链接视频，且当前页面只暴露了分离音视频流。")

    direct_url, direct_headers = direct_candidate
    merged_headers.update(direct_headers)
    return BridgeResolvedSource(
        source_url=source_url,
        media_url=direct_url,
        display_name=title,
        headers=merged_headers,
        resolved_from_page=True,
    )


def _format_ffmpeg_headers(values: Mapping[str, Any] | None) -> str:
    headers = filter_forward_headers(values)
    if not headers:
        return ""
    return "".join(f"{key}: {value}\r\n" for key, value in headers.items())


def _build_video_filter_chain(transcode_profile: str, *, encoder_name: str = "libx264", smooth_fallback: bool = False) -> str:
    profile = normalize_transcode_profile(transcode_profile)
    filters = ["scale=trunc(iw/2)*2:trunc(ih/2)*2"]
    if profile == "smooth":
        if smooth_fallback:
            filters.append("fps=60000/1001")
        else:
            filters.append("minterpolate=fps=60000/1001:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1")
    if encoder_name in {"h264_qsv", "h264_amf"}:
        filters.append("format=nv12")
    return ",".join(filters)


def _append_transcode_output_options(
    command: list[str],
    output_file: Path,
    transcode_profile: str,
    *,
    encoder_name: str = "libx264",
    smooth_fallback: bool = False,
) -> list[str]:
    profile = normalize_transcode_profile(transcode_profile)
    preset = "veryfast"
    crf = "23"
    audio_bitrate = "128k"
    video_args: list[str]

    if profile == "quality":
        preset = "medium"
        crf = "18"
        audio_bitrate = "192k"
    elif profile == "smooth":
        preset = "fast"
        crf = "20"
        audio_bitrate = "160k"

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
    elif encoder_name == "h264_nvenc":
        cq = "21" if profile == "quality" else "23"
        video_args = [
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
        ]
    elif encoder_name == "h264_qsv":
        global_quality = "20" if profile == "quality" else "23"
        video_args = [
            "-c:v",
            "h264_qsv",
            "-global_quality",
            global_quality,
            "-preset",
            "medium",
            "-vf",
            _build_video_filter_chain(profile, encoder_name=encoder_name, smooth_fallback=smooth_fallback),
        ]
    elif encoder_name == "h264_amf":
        qp_p = "20" if profile == "quality" else "23"
        qp_i = "18" if profile == "quality" else "21"
        video_args = [
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
        ]
    else:
        raise RuntimeError(f"Unsupported encoder: {encoder_name}")

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
        label = f"{attempt.effective_profile}/{attempt.encoder_name}"
        if attempt.smooth_fallback:
            label = f"{label} fallback"
        if attempt.downgraded:
            label = f"{label} downgraded"
        failure_sections.extend([f"{label} failed:"])
        failure_sections.extend(stderr.splitlines()[-8:])

        if output_file.exists():
            try:
                output_file.unlink()
            except OSError:
                pass

    tail = "\n".join(failure_sections[-20:])
    raise RuntimeError(f"ffmpeg failed to transcode remote media.\n{tail}")


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

    if profile == "standard":
        fast_mux_command = [
            str(ffmpeg_path),
            "-y",
            "-i",
            str(video_input),
            "-i",
            str(audio_input),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-map_metadata",
            "-1",
            "-sn",
            "-dn",
            "-c:v",
            "copy",
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            str(output_file),
        ]
        fast_result = subprocess.run(fast_mux_command, capture_output=True, text=True, errors="ignore")
        if fast_result.returncode == 0 and output_file.exists() and output_file.stat().st_size > 0:
            return output_file
        if output_file.exists():
            try:
                output_file.unlink()
            except OSError:
                pass

    return _run_transcode_command(
        ffmpeg_path,
        ["-i", str(video_input), "-i", str(audio_input)],
        ["-map", "0:v:0", "-map", "1:a:0"],
        output_file,
        profile,
    )


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

    input_args: list[str] = []
    header_blob = _format_ffmpeg_headers(headers)
    if header_blob:
        input_args.extend(["-headers", header_blob])
    input_args.extend(["-i", media_url])

    return _run_transcode_command(
        ffmpeg_path,
        input_args,
        ["-map", "0:v:0", "-map", "0:a:0?"],
        output_file,
        profile,
    )


class TampermonkeyBridgeService:
    def __init__(self) -> None:
        self.http_server = MediaHttpServer()
        self._lock = threading.RLock()
        self._playback_lock = threading.RLock()
        self._devices_by_location: dict[str, DlnaDevice] = {}
        self._tasks: dict[str, BridgeTask] = {}
        self.current_controller: DlnaController | None = None
        self.current_device: DlnaDevice | None = None

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

        with self._lock:
            previous_controller = self.current_controller
            self.current_controller = None
            self.current_device = None

        with self._playback_lock:
            if previous_controller is not None:
                try:
                    previous_controller.stop()
                except (ValueError, HTTPError, URLError, OSError):
                    pass

            if resolved.requires_local_mux:
                if profile == "standard":
                    progress_message = "Downloading streams and preparing a compatible MP4..."
                elif profile == "quality":
                    progress_message = "Downloading streams and applying local quality optimization..."
                elif smooth_hardware_encoder:
                    progress_message = "Downloading streams and rendering smoother 60fps playback with hardware encoding..."
                else:
                    progress_message = "60fps hardware encoding unavailable. Falling back to local quality optimization..."
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
                    progress_message = "Rendering smoother 60fps playback with hardware encoding..."
                else:
                    progress_message = "60fps hardware encoding unavailable. Falling back to local quality optimization..."
                self._notify_progress(progress_callback, "transcoding", progress_message)
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
                device_name = self.current_device.display_name if self.current_device else ""
                self.current_controller = None
                self.current_device = None

            if controller is not None:
                try:
                    controller.stop()
                except (ValueError, HTTPError, URLError, OSError):
                    pass

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
            raise ValueError("缺少设备地址，请先扫描并选择电视。")

        with self._lock:
            cached = self._devices_by_location.get(location)
        if cached is not None:
            return cached

        devices = discover_devices(timeout=discovery_timeout)
        with self._lock:
            self._devices_by_location = {device.location: device for device in devices}
            cached = self._devices_by_location.get(location)
        if cached is None:
            raise ValueError("未找到指定电视，请先在油猴面板里重新扫描设备。")
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
            raise ValueError(f"JSON 解析失败：{exc.msg}") from exc
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是 JSON 对象。")
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
  <title>油猴投屏桥接服务</title>
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
    <h1>油猴投屏桥接服务已启动</h1>
    <div class="card">
      <p>这个本地服务供油猴脚本调用，用来扫描电视、解析网页视频地址并通过 DLNA 投送到电视。</p>
      <p>健康检查：<code>/api/health</code></p>
      <p>扫描设备：<code>/api/devices</code></p>
      <p>解析视频：<code>POST /api/resolve</code></p>
      <p>开始投屏：<code>POST /api/cast</code></p>
      <p>停止投屏：<code>POST /api/stop</code></p>
    </div>
    <div class="card">
      <p>如果油猴脚本连不上，请确认脚本里配置的桥接地址和当前端口一致。</p>
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
