from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from app.web_video_extractor import has_web_video_extractor
from tampermonkey_bridge import run_bridge_server

FFMPEG_DOWNLOAD_URLS = [
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
    "https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-gpl.zip",
]


def log_step(message: str) -> None:
    print(f"[Screen casting] {message}", flush=True)


def resolve_launch_root(
    *,
    frozen: bool | None = None,
    script_path: str | Path | None = None,
    executable_path: str | Path | None = None,
) -> Path:
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    if frozen:
        target = Path(executable_path or sys.executable)
    else:
        target = Path(script_path or __file__)
    return target.resolve().parent


def runtime_root(project_root: Path) -> Path:
    return project_root / ".runtime"


def find_local_ffmpeg(project_root: Path) -> Path | None:
    candidates: list[Path] = []
    env_value = os.environ.get("SCREEN_CASTING_FFMPEG")
    if env_value:
        candidates.append(Path(env_value))
    candidates.extend(
        [
            runtime_root(project_root) / "ffmpeg" / "ffmpeg.exe",
            runtime_root(project_root) / "ffmpeg.exe",
            project_root / "ffmpeg.exe",
        ]
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return None


def _download_file(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "ScreenCastingBridge/1.0"})
    with urllib.request.urlopen(request) as response, destination.open("wb") as file_obj:
        shutil.copyfileobj(response, file_obj)


def ensure_local_ffmpeg(project_root: Path) -> Path | None:
    existing = find_local_ffmpeg(project_root)
    if existing is not None:
        return existing

    root = runtime_root(project_root)
    downloads_root = root / "downloads"
    target_dir = root / "ffmpeg"
    target_path = target_dir / "ffmpeg.exe"
    downloads_root.mkdir(parents=True, exist_ok=True)
    target_dir.mkdir(parents=True, exist_ok=True)

    downloaded_archive: Path | None = None
    for url in FFMPEG_DOWNLOAD_URLS:
        archive_path = downloads_root / Path(url).name
        try:
            log_step(f"正在下载 ffmpeg: {url}")
            _download_file(url, archive_path)
            downloaded_archive = archive_path
            break
        except Exception:
            archive_path.unlink(missing_ok=True)

    if downloaded_archive is None:
        log_step("ffmpeg 自动下载失败，将继续尝试使用系统里已有的 ffmpeg。")
        return None

    with tempfile.TemporaryDirectory(prefix="screen-casting-ffmpeg-") as temp_dir:
        extract_root = Path(temp_dir)
        with zipfile.ZipFile(downloaded_archive) as archive:
            archive.extractall(extract_root)
        found = next(extract_root.rglob("ffmpeg.exe"), None)
        if found is None:
            raise RuntimeError("ffmpeg 压缩包已下载，但未找到 ffmpeg.exe。")
        shutil.copy2(found, target_path)
    return target_path.resolve()


def run_self_check(project_root: Path) -> int:
    ffmpeg_path = find_local_ffmpeg(project_root)
    yt_dlp_ready = has_web_video_extractor()
    print(f"launch_root={project_root}")
    print(f"yt_dlp={'ok' if yt_dlp_ready else 'missing'}")
    print(f"ffmpeg={ffmpeg_path if ffmpeg_path is not None else 'missing'}")
    return 0 if yt_dlp_ready else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Start the local Tampermonkey casting bridge.")
    parser.add_argument("--host", default="127.0.0.1", help="Bridge bind host, default is 127.0.0.1")
    parser.add_argument("--port", default=9527, type=int, help="Bridge bind port, default is 9527")
    parser.add_argument("--self-check", action="store_true", help="Check bundled dependencies and exit")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    project_root = resolve_launch_root()
    if args.self_check:
        raise SystemExit(run_self_check(project_root))

    ffmpeg_path = ensure_local_ffmpeg(project_root)
    if ffmpeg_path is not None:
        os.environ["SCREEN_CASTING_FFMPEG"] = str(ffmpeg_path)
    log_step(f"Bridge 即将启动。油猴里保持默认地址 http://{args.host}:{args.port} 即可。")
    run_bridge_server(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
