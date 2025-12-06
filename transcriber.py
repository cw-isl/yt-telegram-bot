from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Tuple

from faster_whisper import WhisperModel


logger = logging.getLogger(__name__)


def _bool_env(key: str, default: bool = False) -> bool:
    value = os.getenv(key)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _format_timestamp(seconds: float | None) -> str:
    if seconds is None:
        return "--:--"

    total_seconds = int(seconds)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


@dataclass
class WhisperOptions:
    model_size: str = os.getenv("WHISPER_MODEL", "base")
    device: str = os.getenv("WHISPER_DEVICE", "auto")
    compute_type: str = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
    beam_size: int = _int_env("WHISPER_BEAM_SIZE", 5)
    vad_filter: bool = _bool_env("WHISPER_VAD_FILTER", True)


_MODEL_CACHE: Dict[Tuple[str, str, str], WhisperModel] = {}


def _probe_audio_duration(path: Path) -> float | None:
    """Return audio duration in seconds if ffprobe is available."""

    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None

    try:
        return float((result.stdout or "0").strip())
    except ValueError:
        return None


def _emit_progress(
    callback: Callable[[float, str], None] | None, progress: float, message: str
) -> None:
    if not callback:
        return

    clamped = max(0.0, min(progress, 1.0))
    callback(clamped, message)


def _load_model(options: WhisperOptions) -> WhisperModel:
    key = (options.model_size, options.device, options.compute_type)
    if key not in _MODEL_CACHE:
        _MODEL_CACHE[key] = WhisperModel(
            options.model_size,
            device=options.device,
            compute_type=options.compute_type,
        )
    return _MODEL_CACHE[key]


def _extract_audio_with_ffmpeg(source_path: Path) -> tuple[Path | None, str | None]:
    try:
        temp_file = tempfile.NamedTemporaryFile(
            suffix=".wav", prefix=f"{source_path.stem}_", dir=source_path.parent, delete=False
        )
        temp_path = Path(temp_file.name)
        temp_file.close()
    except Exception as exc:  # noqa: BLE001 - surfaced to caller for user feedback
        return None, f"임시 오디오 파일을 만들지 못했습니다: {exc}"

    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(source_path),
                "-vn",
                "-acodec",
                "pcm_s16le",
                "-ar",
                "16000",
                "-ac",
                "1",
                str(temp_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
    except FileNotFoundError:
        temp_path.unlink(missing_ok=True)
        return None, "ffmpeg를 찾을 수 없습니다. 녹화 서버에 ffmpeg가 설치되어 있는지 확인하세요."
    except subprocess.CalledProcessError as exc:  # noqa: BLE001 - surfaced to caller for user feedback
        temp_path.unlink(missing_ok=True)
        detail = (exc.stderr or "").strip() or (result.stderr if "result" in locals() else "")
        return None, f"오디오 추출에 실패했습니다. 파일이 손상되었을 수 있습니다: {detail}"

    return temp_path, None


def transcribe_file(
    source_path: Path,
    output_path: Path,
    *,
    options: WhisperOptions | None = None,
    on_progress: Callable[[float, str], None] | None = None,
) -> tuple[Path | None, str | None]:
    """Transcribe a media file with Whisper and save it to ``output_path``.

    Returns (output_path, None) on success or (None, error_message) on failure.
    """

    source_path = source_path.expanduser()
    output_path = output_path.expanduser()

    if not source_path.exists() or not source_path.is_file():
        return None, f"전사 대상 파일을 찾을 수 없습니다: {source_path}"

    if source_path.stat().st_size == 0:
        return None, "전사 대상 파일이 비어 있습니다. 녹화가 정상적으로 완료되었는지 확인하세요."

    cleanup_path: Path | None = None
    try:
        options = options or WhisperOptions()
        model = _load_model(options)

        _emit_progress(on_progress, 0.02, "전사 준비 중...")
        total_duration = _probe_audio_duration(source_path)
        if total_duration:
            _emit_progress(on_progress, 0.05, f"길이 확인: {_format_timestamp(total_duration)}")

        input_path = source_path
        try:
            segments, _ = model.transcribe(
                str(input_path), beam_size=options.beam_size, vad_filter=options.vad_filter
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller for user feedback
            logger.exception("Whisper transcribe failed for %s", source_path)
            _emit_progress(on_progress, 0.0, "오디오를 다시 인코딩하는 중...")

            fallback_path, fallback_error = _extract_audio_with_ffmpeg(source_path)
            if not fallback_path:
                _emit_progress(on_progress, 0.0, "전사에 필요한 오디오를 준비하지 못했습니다.")
                return None, fallback_error or f"전사 작업 중 오류가 발생했습니다: {exc}"

            cleanup_path = fallback_path
            input_path = fallback_path

            try:
                segments, _ = model.transcribe(
                    str(input_path), beam_size=options.beam_size, vad_filter=options.vad_filter
                )
            except Exception as inner_exc:  # noqa: BLE001 - surfaced to the caller for user feedback
                logger.exception(
                    "Whisper transcribe failed after audio extraction for %s", source_path
                )
                _emit_progress(on_progress, 0.0, "오디오 변환 후에도 전사하지 못했습니다.")
                return None, f"오디오 추출 후에도 전사하지 못했습니다: {inner_exc}"

        lines = []
        progress_hint = 0.08
        for segment in segments:
            text = segment.text.strip()
            if not text:
                continue
            start = _format_timestamp(segment.start)
            end = _format_timestamp(segment.end)
            lines.append(f"[{start} - {end}] {text}")

            progress_hint = max(progress_hint, progress_hint + 0.01)
            if total_duration and segment.end:
                progress_hint = max(progress_hint, min(segment.end / total_duration, 0.97))
            _emit_progress(on_progress, progress_hint, f"{_format_timestamp(segment.end)} 처리 중")

        if not lines:
            return None, "전사 결과가 비어 있습니다. 오디오가 포함된 파일인지 확인하세요."

        output_path.parent.mkdir(parents=True, exist_ok=True)
        _emit_progress(on_progress, max(progress_hint, 0.98), "전사 결과를 저장하는 중...")
        header = (
            f"원본 파일: {source_path.name}\n"
            f"저장 위치: {source_path.parent}\n"
            f"전사 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"사용 모델: {options.model_size} ({options.device}/{options.compute_type})\n"
            "\n"
        )
        output_path.write_text(header + "\n".join(lines), encoding="utf-8")

        _emit_progress(on_progress, 1.0, "전사가 완료되었습니다.")
        return output_path, None
    finally:
        if cleanup_path:
            cleanup_path.unlink(missing_ok=True)

