from __future__ import annotations

import ipaddress
import mimetypes
from pathlib import Path
from urllib.parse import unquote, urlsplit

DIRECT_MEDIA_SUFFIXES = {
    ".mp4",
    ".m4v",
    ".webm",
    ".mov",
    ".m3u8",
    ".mpd",
    ".flv",
    ".ts",
    ".mkv",
    ".avi",
    ".wmv",
}
DIRECT_MEDIA_HINTS = (".m3u8", ".mp4", ".m4v", ".webm", ".mov", ".mpd", ".flv", ".ts")


def is_http_url(value: str) -> bool:
    lowered = value.strip().lower()
    return lowered.startswith("http://") or lowered.startswith("https://")


def extract_url_path(value: str) -> str:
    return unquote(urlsplit(value).path)


def is_probable_direct_media_url(value: str) -> bool:
    if not is_http_url(value):
        return False
    split = urlsplit(value)
    path = extract_url_path(value).lower()
    if any(path.endswith(suffix) for suffix in DIRECT_MEDIA_SUFFIXES):
        return True
    query = split.query.lower()
    fragment = split.fragment.lower()
    combined = f"{query}&{fragment}"
    return any(hint in combined for hint in DIRECT_MEDIA_HINTS)




def _is_probable_local_host(hostname: str) -> bool:
    lowered = hostname.strip().lower()
    if lowered in {"localhost", "127.0.0.1"}:
        return True
    try:
        ip = ipaddress.ip_address(lowered)
    except ValueError:
        return False
    return bool(ip.is_private or ip.is_loopback)


def explain_generated_media_url(value: str) -> str | None:
    if not is_http_url(value):
        return None
    split = urlsplit(value)
    hostname = split.hostname or ""
    if not _is_probable_local_host(hostname):
        return None

    path = (split.path or "/").lower()
    if path in {"", "/"}:
        return "当前输入的是本程序生成的局域网接收地址根地址，不是原始视频播放页。它不能再拿来解析视频。若你要在电视浏览器打开，请使用完整的兼容播放页地址。"
    if path == "/player":
        return "当前输入的是本程序生成的兼容播放页地址。这个地址应直接在电视浏览器打开，不要再粘贴回“输入视频网址”。"
    if path.startswith("/media/") or path.startswith("/proxy"):
        return "当前输入的是本程序生成的兼容视频地址。它是给电视播放器直接访问的，不需要再重新解析。"
    if path.startswith("/snapshot") or path.startswith("/stream") or path.startswith("/health"):
        return "当前输入的是兼容投屏接收地址，不是原始视频播放页。它应直接在电视浏览器打开，而不是重新放回视频解析框。"
    return None

def guess_content_type_from_source(source: str | Path) -> str:
    if isinstance(source, Path):
        guess_target = source.name
    else:
        guess_target = extract_url_path(source) or source
    content_type, _ = mimetypes.guess_type(guess_target)
    return content_type or "application/octet-stream"


def display_name_from_source(source: str | Path) -> str:
    if isinstance(source, Path):
        return source.name
    path = extract_url_path(source)
    name = Path(path).name if path else ""
    if name:
        return name
    host = urlsplit(source).netloc
    return host or source
