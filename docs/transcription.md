# 전사(Whisper) 설정 가이드

웹 UI의 "전사 및 요약" 섹션이 실제 Whisper 전사를 수행하도록 설정하는 방법입니다. 설치와 환경변수만 준비하면 추가 코딩 없이 바로 동작합니다.

## 1) 필수 파이썬 패키지 설치
`requirements.txt`에 `faster-whisper`가 포함되었습니다. 가상환경을 사용한다면 아래와 같이 설치하세요.

```bash
pip install -r requirements.txt
```

> 시스템에 ffmpeg가 설치되어 있어야 오디오를 추출하고 Whisper가 정상 작동합니다.

## 2) Whisper 모델 옵션 (환경 변수)
`transcriber.py`는 환경변수를 읽어 모델과 추론 방식을 조정합니다. 모두 선택 사항이며 지정하지 않으면 괄호 안 기본값을 사용합니다.

| 변수 | 설명 |
| --- | --- |
| `WHISPER_MODEL` (`base`) | 사용할 모델 이름(예: `small`, `medium`, 경량을 원하면 `tiny`). 처음 실행 시 자동 다운로드됩니다. |
| `WHISPER_DEVICE` (`auto`) | `auto`, `cpu`, `cuda` 중 선택. GPU가 없다면 `cpu`. |
| `WHISPER_COMPUTE_TYPE` (`int8`) | 추론 정밀도. GPU가 있다면 `float16`, CPU는 `int8` 또는 `int8_float32` 권장. |
| `WHISPER_BEAM_SIZE` (`5`) | 디코딩 beam size. 값이 커질수록 정확도↑, 속도↓. |
| `WHISPER_VAD_FILTER` (`true`) | `true/false`. 음성 감지 기반으로 무음 구간을 건너뛰어 잡음을 줄입니다. |

예시: GPU에서 small 모델을 쓰고 싶다면

```bash
export WHISPER_MODEL=small
export WHISPER_DEVICE=cuda
export WHISPER_COMPUTE_TYPE=float16
```

## 3) 전사 결과 경로
`config/defaults.yaml`의 `paths.transcripts` 값(기본 `/root/rcbot/downloads/transcripts`) 아래에 `원본파일명_transcript.txt` 형식으로 저장됩니다. UI 설정 페이지에서도 경로를 바꿀 수 있습니다.

## 4) 사용 방법
1. 위 설정을 마친 뒤 웹 UI에서 `전사` 섹션으로 이동합니다.
2. 다운로드 파일 또는 라이브 녹화 파일을 선택하고 "전사 시작"을 누릅니다.
3. Whisper가 전사를 완료하면 타임스탬프가 포함된 텍스트 파일이 생성되고, 동일한 영역의 드롭다운에서 확인할 수 있습니다.

