from dataclasses import dataclass, field
from typing import Any

from app.media_source import display_name_from_source, explain_generated_media_url, is_http_url, is_probable_direct_media_url

PREFERRED_EXTENSIONS = {"mp4", "m4v", "mov", "m3u8", "mpd", "webm", "ts"}
HEADER_ALLOWLIST = {"User-Agent", "Referer", "Origin", "Cookie", "Authorization", "Accept", "Accept-Language"}


class VideoPageExtractionUnavailable(RuntimeError):
    pass


class VideoPageExtractionError(RuntimeError):
    pass


class _SilentYtdlpLogger:
    def debug(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        pass

    def error(self, message: str) -> None:
        pass


@dataclass(slots=True)
class ResolvedMediaSource:
    media_url: str
    display_name: str
    original_url: str
    headers: dict[str, str] = field(default_factory=dict)
    resolved_from_page: bool = False


def has_web_video_extractor() -> bool:
    try:
        _load_yt_dlp_module()
        return True
    except VideoPageExtractionUnavailable:
        return False


def _load_yt_dlp_module() -> Any:
    try:
        import yt_dlp  # type: ignore
    except ImportError as exc:
        raise VideoPageExtractionUnavailable(
            "yt-dlp is not installed in the current environment, so video pages cannot be resolved automatically."
        ) from exc
    return yt_dlp


def _iter_info_nodes(info: Any):
    if not isinstance(info, dict):
        return
    yield info
    entries = info.get("entries") or []
    for entry in entries:
        if isinstance(entry, dict):
            yield from _iter_info_nodes(entry)


def _looks_like_media_url(url: str, node: dict[str, Any]) -> bool:
    if not url or not is_http_url(url):
        return False
    if is_probable_direct_media_url(url):
        return True
    protocol = str(node.get("protocol") or "").lower()
    ext = str(node.get("ext") or "").lower()
    return any(token in protocol for token in ("http", "m3u8", "dash")) or ext in PREFERRED_EXTENSIONS


def _score_candidate(node: dict[str, Any]) -> int:
    score = 0
    url = str(node.get("url") or "")
    ext = str(node.get("ext") or "").lower()
    protocol = str(node.get("protocol") or "").lower()
    acodec = str(node.get("acodec") or "")
    vcodec = str(node.get("vcodec") or "")
    if is_http_url(url):
        score += 200
    if ext == "mp4":
        score += 80
    elif ext == "m3u8":
        score += 70
    elif ext in PREFERRED_EXTENSIONS:
        score += 50
    if protocol in {"https", "http"}:
        score += 40
    elif "m3u8" in protocol:
        score += 35
    elif "dash" in protocol:
        score += 20
    if vcodec and vcodec != "none":
        score += 30
    if acodec and acodec != "none":
        score += 30
    if node.get("has_drm"):
        score -= 500
    try:
        score += min(int(node.get("height") or 0), 1080)
    except (TypeError, ValueError):
        pass
    return score


def _pick_best_media_node(info: dict[str, Any]) -> dict[str, Any] | None:
    candidates: list[tuple[int, dict[str, Any]]] = []
    for node in _iter_info_nodes(info):
        direct_url = str(node.get("url") or "").strip()
        if _looks_like_media_url(direct_url, node):
            candidates.append((_score_candidate(node), node))
        for fmt in node.get("formats") or []:
            if not isinstance(fmt, dict):
                continue
            fmt_url = str(fmt.get("url") or "").strip()
            if _looks_like_media_url(fmt_url, fmt):
                merged = dict(node)
                merged.update(fmt)
                if "title" not in merged and node.get("title"):
                    merged["title"] = node.get("title")
                candidates.append((_score_candidate(merged), merged))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _extract_headers(*nodes: dict[str, Any]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for node in nodes:
        for key, value in dict(node.get("http_headers") or {}).items():
            if key in HEADER_ALLOWLIST and value:
                headers[str(key)] = str(value)
    return headers


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


def resolve_media_source(source_url: str) -> ResolvedMediaSource:
    if not is_http_url(source_url):
        raise ValueError("Only http:// and https:// URLs are supported.")

    generated_url_hint = explain_generated_media_url(source_url)
    if generated_url_hint:
        raise ValueError(generated_url_hint)

    if is_probable_direct_media_url(source_url):
        return ResolvedMediaSource(
            media_url=source_url,
            display_name=display_name_from_source(source_url),
            original_url=source_url,
            resolved_from_page=False,
        )

    yt_dlp = _load_yt_dlp_module()
    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "extract_flat": False,
        "format": "best[acodec!=none][vcodec!=none]/best",
        "logger": _SilentYtdlpLogger(),
    }
    try:
        info = _extract_info_with_format_retry(yt_dlp, source_url, options)
    except Exception as exc:
        raise VideoPageExtractionError(f"Failed to resolve video page: {exc}") from exc

    if not isinstance(info, dict):
        raise VideoPageExtractionError("The extractor result is invalid.")

    media_node = _pick_best_media_node(info)
    if media_node is None:
        raise VideoPageExtractionError(
            "No directly playable media URL was found on the current page. The site may require DRM or only expose protected streams."
        )

    media_url = str(media_node.get("url") or "").strip()
    if not is_http_url(media_url):
        raise VideoPageExtractionError("The extractor returned no usable network media URL.")

    display_name = str(media_node.get("title") or info.get("title") or display_name_from_source(source_url)).strip() or display_name_from_source(source_url)
    headers = _extract_headers(info, media_node)
    return ResolvedMediaSource(
        media_url=media_url,
        display_name=display_name,
        original_url=source_url,
        headers=headers,
        resolved_from_page=True,
    )