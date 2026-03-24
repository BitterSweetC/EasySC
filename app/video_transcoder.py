from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = PROJECT_ROOT / "cache" / "compat_media"
BILILIVE_ROOT = Path(r"C:\Program Files (x86)\bililive\ugc_assistant")
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


class FfmpegNotFoundError(RuntimeError):
    pass


def _candidate_ffmpeg_paths() -> list[Path]:
    candidates: list[Path] = []
    env_value = os.environ.get("SCREEN_CASTING_FFMPEG")
    if env_value:
        candidates.append(Path(env_value))

    which_value = shutil.which("ffmpeg")
    if which_value:
        candidates.append(Path(which_value))

    if BILILIVE_ROOT.exists():
        candidates.extend(sorted(BILILIVE_ROOT.glob("*\\ffmpeg.exe"), reverse=True))

    candidates.extend(
        [
            Path(r"C:\ffmpeg\bin\ffmpeg.exe"),
            Path(r"C:\Program Files\ffmpeg\bin\ffmpeg.exe"),
        ]
    )
    return candidates


def find_ffmpeg() -> Path:
    for candidate in _candidate_ffmpeg_paths():
        if candidate.exists():
            return candidate
    raise FfmpegNotFoundError("未找到 ffmpeg，可设置环境变量 SCREEN_CASTING_FFMPEG 指向 ffmpeg.exe。")


def _safe_stem(file_path: Path) -> str:
    stem = SAFE_NAME_RE.sub("_", file_path.stem).strip("._")
    return stem or "video"


def build_compatible_output_path(source_file: Path) -> Path:
    source = source_file.resolve()
    stat = source.stat()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{_safe_stem(source)}_{stat.st_size}_{stat.st_mtime_ns}.mp4"


def prepare_compatible_mp4(source_file: Path) -> Path:
    source = source_file.resolve()
    if not source.exists():
        raise FileNotFoundError(str(source))

    output_file = build_compatible_output_path(source)
    if output_file.exists() and output_file.stat().st_size > 0:
        return output_file

    ffmpeg_path = find_ffmpeg()
    output_file.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(ffmpeg_path),
        "-y",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-map_metadata",
        "-1",
        "-sn",
        "-dn",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-vf",
        "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-ac",
        "2",
        str(output_file),
    ]
    result = subprocess.run(command, capture_output=True, text=True, errors="ignore")
    if result.returncode != 0 or not output_file.exists() or output_file.stat().st_size == 0:
        stderr = (result.stderr or result.stdout or "未知错误").strip()
        tail = "\n".join(stderr.splitlines()[-12:])
        raise RuntimeError(f"ffmpeg 转码失败。\n{tail}")
    return output_file
