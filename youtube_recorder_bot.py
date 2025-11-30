#!/usr/bin/env python3
"""
Web-friendly helper utilities for recording and downloading YouTube videos.

This module keeps a small surface area so it can be reused from Flask views
and remains compatible with the lightweight tests that check the download
fallback logic.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Iterable, Tuple

BASE_DIR = Path(__file__).parent
DEFAULT_CONFIG_PATH = BASE_DIR / "config" / "defaults.yaml"
USER_CONFIG_PATH = BASE_DIR / "config" / "user_settings.yaml"
YOUTUBE_EXTRACTOR_ARGS = "youtube:player_client=android"


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------
def _load_config(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def _ensure_local_paths(settings: dict) -> None:
    """Create configured local directories when they are missing."""

    for key in ("recordings", "downloads", "captures", "transcripts", "summaries"):
        path_value = settings.get("paths", {}).get(key)
        if not path_value:
            continue

        Path(path_value).expanduser().mkdir(parents=True, exist_ok=True)


def load_settings() -> dict:
    """Load merged defaults + user overrides."""
    defaults = _load_config(DEFAULT_CONFIG_PATH)
    overrides = _load_config(USER_CONFIG_PATH)
    merged = {**defaults, **overrides}
    for section in ("paths", "auth"):
        merged[section] = {**defaults.get(section, {}), **overrides.get(section, {})}
    merged.setdefault("ui", defaults.get("ui", {}))
    _ensure_local_paths(merged)
    return merged


def save_settings(data: dict) -> None:
    USER_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    USER_CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Command helpers
# ---------------------------------------------------------------------------
def run_cmd(cmd: Iterable[str], **kwargs) -> Tuple[int, str, str]:
    """Run a command and return (returncode, stdout, stderr).

    A missing binary is reported as a standard return code (127) so callers can
    surface a helpful error message instead of crashing.
    """

    try:
        process = subprocess.run(list(cmd), capture_output=True, text=True, **kwargs)
        return process.returncode, process.stdout or "", process.stderr or ""
    except FileNotFoundError as exc:  # pragma: no cover - exercised via higher level
        missing = Path(list(cmd)[0]).name
        return 127, "", f"{missing} executable not found: {exc}"


def _ffmpeg_path() -> Path | None:
    """Return an ffmpeg path when it is available or configured via env."""

    env_path = Path(shutil.which("ffmpeg") or "")
    if env_path.exists():
        return env_path

    fallback = os.getenv("FFMPEG_PATH")
    if fallback:
        candidate = Path(fallback).expanduser()
        if candidate.exists():
            return candidate
    return None


def _ffmpeg_available() -> bool:
    return _ffmpeg_path() is not None


def capture_live_frame(url: str, dest_dir: Path | None = None) -> tuple[Path | None, str | None]:
    """Capture a single frame from a YouTube live stream.

    Returns (output_path, error_message). When the capture succeeds,
    error_message is None.
    """

    if not _ffmpeg_available():
        return None, "ffmpeg가 설치되어 있지 않아 캡처를 진행할 수 없습니다."

    dest_dir = Path(dest_dir or BASE_DIR / "static" / "captures")
    dest_dir.mkdir(parents=True, exist_ok=True)

    rc, stdout, _ = run_cmd(["yt-dlp", "-g", "-f", "best", "--extractor-args", YOUTUBE_EXTRACTOR_ARGS, url])
    if rc != 0 or not stdout.strip():
        return None, "스트리밍 URL을 확인하지 못했습니다. 링크가 올바른지 확인하세요."

    stream_url = stdout.splitlines()[0].strip()
    timestamp = datetime.now().strftime("%y%m%d_%H:%M:%S")
    output_path = dest_dir / f"{timestamp}.png"
    suffix = 1
    while output_path.exists():
        output_path = dest_dir / f"{timestamp}_{suffix}.png"
        suffix += 1

    rc, _, _ = run_cmd(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-ss",
            "00:00:01",
            "-i",
            stream_url,
            "-frames:v",
            "1",
            str(output_path),
        ]
    )

    if rc != 0 or not output_path.exists():
        return None, "캡처 중 오류가 발생했습니다. 스트림 접근 권한 또는 네트워크 상태를 확인하세요."

    return output_path, None


# ---------------------------------------------------------------------------
# YouTube download helpers
# ---------------------------------------------------------------------------
DEFAULT_OUTPUT_TEMPLATE = "%(title).80B.mp4"


def _yt_common_opts(
    *, allow_ffmpeg: bool = True, download_dir: Path | None = None, ffmpeg_path: Path | None = None
) -> list[str]:
    """Common yt-dlp options for both recording and downloads.

    allow_ffmpeg=False removes post-processing flags so downloads succeed even
    when ffmpeg is missing.
    """
    download_dir = Path(download_dir or BASE_DIR / "recordings")
    download_dir.mkdir(parents=True, exist_ok=True)

    format_selector = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
    if not allow_ffmpeg:
        # Avoid formats that require muxing when ffmpeg is missing. Restrict to
        # progressive streams with both audio/video so yt-dlp can save without
        # additional tools.
        format_selector = "best[ext=mp4][acodec!=none][vcodec!=none]/best[acodec!=none]"

    opts: list[str] = [
        "-o",
        str(download_dir / DEFAULT_OUTPUT_TEMPLATE),
        "--no-playlist",
        "--no-progress",
        "--extractor-args",
        YOUTUBE_EXTRACTOR_ARGS,
        "-f",
        format_selector,
    ]
    if allow_ffmpeg:
        if ffmpeg_path:
            opts.extend(["--ffmpeg-location", str(ffmpeg_path)])
        opts.extend([
            "--remux-video",
            "mp4",
            "--postprocessor-args",
            "-c:v copy -c:a copy",
        ])
    return opts


def _expected_download_path(download_dir: Path) -> Path | None:
    """Return the most recent file in the directory if any exists."""
    files = sorted(
        download_dir.glob("*"),
        key=lambda p: p.stat().st_mtime if p.exists() else 0,
        reverse=True,
    )
    return files[0] if files else None


def yt_download(url: str, download_dir: Path, *, allow_ffmpeg: bool = True) -> tuple[Path | None, str | None]:
    """Download a YouTube video with a best-effort ffmpeg fallback."""

    download_dir = Path(download_dir)
    download_dir.mkdir(parents=True, exist_ok=True)

    ffmpeg_path = _ffmpeg_path()
    last_error = None

    attempts = []
    if allow_ffmpeg and ffmpeg_path:
        attempts.append(True)
    attempts.append(False)  # always keep a non-ffmpeg fallback

    for use_ffmpeg in attempts:
        opts = _yt_common_opts(
            allow_ffmpeg=use_ffmpeg, download_dir=download_dir, ffmpeg_path=ffmpeg_path
        )
        rc, stdout, stderr = run_cmd(["yt-dlp", url, *opts])
        if rc == 0:
            path = _expected_download_path(download_dir)
            if path and path.exists():
                return path, None
        last_error = stderr or stdout or "다운로드 중 알 수 없는 오류가 발생했습니다."

    return None, last_error


if __name__ == "__main__":
    settings = load_settings()
    print("Current settings:\n", json.dumps(settings, ensure_ascii=False, indent=2))
