#!/usr/bin/env python3
"""
Web-friendly helper utilities for recording and downloading YouTube videos.

This module keeps a small surface area so it can be reused from Flask views
and remains compatible with the lightweight tests that check the download
fallback logic.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Iterable, Tuple

BASE_DIR = Path(__file__).parent
DEFAULT_CONFIG_PATH = BASE_DIR / "config" / "defaults.yaml"
USER_CONFIG_PATH = BASE_DIR / "config" / "user_settings.yaml"


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------
def _load_config(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def load_settings() -> dict:
    """Load merged defaults + user overrides."""
    defaults = _load_config(DEFAULT_CONFIG_PATH)
    overrides = _load_config(USER_CONFIG_PATH)
    merged = {**defaults, **overrides}
    for section in ("paths", "auth"):
        merged[section] = {**defaults.get(section, {}), **overrides.get(section, {})}
    merged.setdefault("ui", defaults.get("ui", {}))
    return merged


def save_settings(data: dict) -> None:
    USER_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    USER_CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Command helpers
# ---------------------------------------------------------------------------
def run_cmd(cmd: Iterable[str], **kwargs) -> Tuple[int, str, str]:
    """Run a command and return (returncode, stdout, stderr)."""
    process = subprocess.run(list(cmd), capture_output=True, text=True, **kwargs)
    return process.returncode, process.stdout or "", process.stderr or ""


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


# ---------------------------------------------------------------------------
# YouTube download helpers
# ---------------------------------------------------------------------------
DEFAULT_OUTPUT_TEMPLATE = "%(title).80B.mp4"


def _yt_common_opts(*, allow_ffmpeg: bool = True, download_dir: Path | None = None) -> list[str]:
    """Common yt-dlp options for both recording and downloads.

    allow_ffmpeg=False removes post-processing flags so downloads succeed even
    when ffmpeg is missing.
    """
    download_dir = Path(download_dir or BASE_DIR / "recordings")
    download_dir.mkdir(parents=True, exist_ok=True)

    opts: list[str] = [
        "-o",
        str(download_dir / DEFAULT_OUTPUT_TEMPLATE),
        "--no-playlist",
        "--no-progress",
        "-f",
        "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
    ]
    if allow_ffmpeg:
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


def yt_download(url: str, download_dir: Path, *, allow_ffmpeg: bool = True) -> Path | None:
    """Download a YouTube video with a best-effort ffmpeg fallback."""
    download_dir = Path(download_dir)
    download_dir.mkdir(parents=True, exist_ok=True)

    attempts = []
    if allow_ffmpeg and _ffmpeg_available():
        attempts.append(True)
    attempts.append(False)  # always keep a non-ffmpeg fallback

    for use_ffmpeg in attempts:
        opts = _yt_common_opts(allow_ffmpeg=use_ffmpeg, download_dir=download_dir)
        rc, _, _ = run_cmd(["yt-dlp", url, *opts])
        if rc == 0:
            path = _expected_download_path(download_dir)
            if path and path.exists():
                return path
    return None


if __name__ == "__main__":
    settings = load_settings()
    print("Current settings:\n", json.dumps(settings, ensure_ascii=False, indent=2))
