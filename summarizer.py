from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests

logger = logging.getLogger(__name__)

DEFAULT_SUMMARY_MODEL = os.getenv("SUMMARY_MODEL", "gpt-4o-mini")
DEFAULT_SUMMARY_MAX_CHARS = int(os.getenv("SUMMARY_MAX_CHARS", "12000"))
DEFAULT_OPENAI_BASE_URL = os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1")


def _parse_models(value: str | None) -> list[str]:
    if not value:
        return []
    models = [item.strip() for item in value.split(",") if item.strip()]
    # Preserve order while removing duplicates
    seen = set()
    unique_models: list[str] = []
    for model in models:
        if model not in seen:
            seen.add(model)
            unique_models.append(model)
    return unique_models


def available_summary_models() -> list[str]:
    env_models = _parse_models(os.getenv("SUMMARY_MODELS"))
    if env_models:
        return env_models

    fallback = [DEFAULT_SUMMARY_MODEL, "gpt-4o", "o1-mini"]
    # Remove duplicates while keeping order
    seen = set()
    unique_fallback: list[str] = []
    for model in fallback:
        if model not in seen:
            seen.add(model)
            unique_fallback.append(model)
    return unique_fallback


@dataclass
class SummaryResult:
    content: str
    model: str
    truncated: bool
    input_characters: int
    max_characters: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


def _prepare_transcript_text(path: Path, *, max_chars: int | None = None) -> tuple[str, bool]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    if max_chars and len(text) > max_chars:
        return text[:max_chars], True
    return text, False


def _build_openai_request(model: str, transcript_text: str) -> dict[str, Any]:
    return {
        "model": model,
        "temperature": 0.2,
        "messages": [
            {
                "role": "system",
                "content": (
                    "당신은 방송 전사를 간결하게 요약하는 한국어 어시스턴트입니다. "
                    "핵심 사건과 시간 흐름을 유지하고, 중요한 발언자는 구분하세요."
                ),
            },
            {
                "role": "user",
                "content": (
                    "다음 전사를 5~7개의 핵심 bullet로 정리하고, 방송 주요 맥락을 한 문장으로 요약해 주세요. "
                    "불필요한 인사말이나 반복은 제외하세요.\n\n전사 내용:\n" + transcript_text
                ),
            },
        ],
    }


def summarize_transcript(
    transcript_path: Path,
    *,
    api_key: str,
    model: str | None = None,
    max_chars: int | None = DEFAULT_SUMMARY_MAX_CHARS,
    api_base: str | None = DEFAULT_OPENAI_BASE_URL,
    progress_callback: Callable[[float, str], None] | None = None,
) -> tuple[SummaryResult | None, str | None]:
    transcript_path = transcript_path.expanduser()

    if not api_key:
        return None, "ChatGPT API 토큰을 설정한 뒤 다시 시도하세요."

    if not transcript_path.exists() or not transcript_path.is_file():
        return None, "요약할 전사 파일을 찾을 수 없습니다."

    if progress_callback:
        progress_callback(0.05, "전사 파일을 읽는 중...")

    snippet, truncated = _prepare_transcript_text(transcript_path, max_chars=max_chars)
    if progress_callback:
        progress_callback(0.2, "요약 프롬프트를 준비하는 중...")
    target_model = model or DEFAULT_SUMMARY_MODEL

    url = (api_base.rstrip("/") if api_base else DEFAULT_OPENAI_BASE_URL.rstrip("/")) + "/chat/completions"
    payload = _build_openai_request(target_model, snippet)
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    try:
        if progress_callback:
            progress_callback(0.35, "OpenAI API로 요청을 전송했습니다. 응답을 기다리는 중...")
        response = requests.post(url, headers=headers, json=payload, timeout=120)
    except requests.RequestException as exc:  # noqa: BLE001 - surfaced to user
        logger.exception("OpenAI request failed")
        if progress_callback:
            progress_callback(0.0, "요약 요청 중 오류가 발생했습니다.")
        return None, f"요약 요청 중 오류가 발생했습니다: {exc}"

    if response.status_code >= 300:
        try:
            detail = response.json().get("error", {}).get("message")
        except Exception:  # noqa: BLE001 - safe fallback
            detail = response.text
        logger.error("OpenAI API error %s: %s", response.status_code, detail)
        if progress_callback:
            progress_callback(0.0, "요약 응답을 받지 못했습니다.")
        return None, detail or "요약 응답을 받지 못했습니다."

    try:
        data = response.json()
        content = (
            data.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            .strip()
        )
        usage = data.get("usage", {}) if isinstance(data, dict) else {}
    except ValueError:
        return None, "요약 응답을 해석하지 못했습니다."

    if progress_callback:
        progress_callback(0.9, "요약 결과를 정리하는 중...")

    if not content:
        if progress_callback:
            progress_callback(0.0, "빈 요약 결과가 반환되었습니다.")
        return None, "빈 요약 결과가 반환되었습니다."

    if progress_callback:
        progress_callback(1.0, "요약을 완료했습니다.")

    return (
        SummaryResult(
            content=content,
            model=data.get("model", target_model),
            truncated=truncated,
            input_characters=len(snippet),
            max_characters=max_chars,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
        ),
        None,
    )
