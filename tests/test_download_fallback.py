from pathlib import Path
import importlib

import pytest


@pytest.fixture(scope="module")
def bot_module():
    return importlib.import_module("youtube_recorder_bot")


def _extract_output_path(cmd):
    if "-o" not in cmd:
        return None
    idx = cmd.index("-o")
    template = Path(cmd[idx + 1])
    sample = template.name.replace("%(title).80B", "SampleTitle").replace("%(ext)s", "webm")
    return template.parent / sample


def test_common_opts_remove_ffmpeg(bot_module):
    opts = bot_module._yt_common_opts(allow_ffmpeg=True)
    assert "--remux-video" in opts

    opts_no_ffmpeg = bot_module._yt_common_opts(allow_ffmpeg=False)
    assert "--remux-video" not in opts_no_ffmpeg
    assert "--postprocessor-args" not in opts_no_ffmpeg
    assert "best[ext=mp4][acodec!=none][vcodec!=none]/best[acodec!=none]" in opts_no_ffmpeg


def test_yt_download_fallback_without_ffmpeg(monkeypatch, tmp_path, bot_module):
    attempts = []

    def fake_run_cmd(cmd, **kwargs):
        attempts.append(cmd)
        out_path = _extract_output_path(cmd)
        if "--remux-video" in cmd:
            return 1, "", "ffmpeg not found"
        if out_path:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(b"data")
        return 0, "", ""

    monkeypatch.setattr(bot_module, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(bot_module, "_ffmpeg_available", lambda: True)

    result, error = bot_module.yt_download("https://www.youtube.com/watch?v=abcdefghijk", tmp_path)

    assert error is None
    assert result is not None
    assert result.exists()
    assert any("--remux-video" in cmd for cmd in attempts)
    assert any("--remux-video" not in cmd for cmd in attempts)
