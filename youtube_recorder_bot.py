#!/usr/bin/env python3
import os, re, json, time, shutil, logging, tempfile, subprocess, threading, shlex, secrets, signal
from pathlib import Path
from urllib.parse import urlparse
from datetime import datetime

"""
CHANGES (by helper):
- Force rclone config to /home/file/.rclone.conf (copy from autodetected if needed)
- Fix detect_live() boolean bug
- Search multiple candidate recording dirs when stopping/status (handles legacy /root/yt-bot/recordings)
- Inject uniform env into Popen (yt-dlp)
- Provide minimal implementations for _kb_providers/_kb_models/handle_* callbacks
"""

# ===== .env 로드 (고정 경로) =====
os.environ["ENV_PATH"] = "/home/file/.env"
ENV_PATH = Path(os.environ["ENV_PATH"])
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=str(ENV_PATH))
except Exception:
    pass  # python-dotenv 없으면 OS 환경변수 사용

import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# Optional: faster-whisper
try:
    from faster_whisper import WhisperModel
except Exception:
    WhisperModel = None

# ===================== Config =====================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not BOT_TOKEN or ":" not in BOT_TOKEN:
    raise RuntimeError("Set BOT_TOKEN env first.")

# rclone remote & folders
RCLONE_REMOTE = os.environ.get("RCLONE_REMOTE", "onedrive").strip()
RCLONE_FOLDER_VIDEOS = os.environ.get("RCLONE_FOLDER_VIDEOS", "YouTube_Backup").strip()
RCLONE_FOLDER_TRANSCRIPTS = os.environ.get("RCLONE_FOLDER_TRANSCRIPTS", "YouTube_Backup/Transcripts").strip()

# whisper
WHISPER_MODEL  = os.environ.get("WHISPER_MODEL", "small").strip()
WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "auto").strip()

# summary engines
SUMMARY_ENGINE = os.environ.get("SUMMARY_ENGINE", "gemini").strip().lower()

# Gemini
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL   = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash").strip()

# OpenAI
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_MODEL   = os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip()

# Anthropic (Claude)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL   = os.environ.get("ANTHROPIC_MODEL", "claude-3-sonnet-20240229").strip()

BOT_HOME = Path(os.environ.get("BOT_HOME", "/home/file")).expanduser()
BOT_HOME.mkdir(parents=True, exist_ok=True)

# ----- rclone config 경로: 기본(/home/file/.rclone.conf) + 필요시 복사해서 강제 통일 -----
RCLONE_CONF_PATH = BOT_HOME / ".rclone.conf"

def _discover_rclone_conf_path() -> Path | None:
    """rclone가 스스로 말하는 conf 위치를 파싱."""
    try:
        p = subprocess.run(["rclone", "config", "file"], capture_output=True, text=True, timeout=5)
        out = (p.stdout or "") + (p.stderr or "")
        m = re.search(r"(?mi)Configuration file is stored at:\s*\n?\s*(.+)$", out)
        if not m:
            m = re.search(r"(?mi)Config file .*? at:\s*\n?\s*(.+)$", out)
        if m:
            cand = Path(m.group(1).strip())
            return cand if cand.exists() else None
    except Exception:
        pass
    return None

def _force_rclone_conf(default_path: Path) -> Path:
    """
    /home/file/.rclone.conf 를 표준으로 강제.
    - 이미 있으면 그대로 사용
    - 없으면 rclone의 실제 conf 를 찾아와서 복사
    """
    if default_path.exists():
        return default_path
    real = _discover_rclone_conf_path()
    try:
        default_path.parent.mkdir(parents=True, exist_ok=True)
        if real and real.exists():
            shutil.copy2(real, default_path)
    except Exception:
        pass
    return default_path

RCLONE_CONF_PATH = _force_rclone_conf(RCLONE_CONF_PATH)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ytbot")
log.info(f"BOT_HOME={BOT_HOME}")
log.info(f"RCLONE_CONFIG pinned at {RCLONE_CONF_PATH} (exists={RCLONE_CONF_PATH.exists()})")

# ---- summarize engine selection with fallback (logs) ----
def _select_summary_engine():
    eng = SUMMARY_ENGINE
    if eng == "openai" and not OPENAI_API_KEY:
        log.warning("SUMMARY_ENGINE=openai but OPENAI_API_KEY missing; falling back to gemini.")
        eng = "gemini"
    if eng == "gemini" and not GEMINI_API_KEY:
        log.warning("SUMMARY_ENGINE=gemini but GEMINI_API_KEY missing; trying claude.")
        eng = "claude" if ANTHROPIC_API_KEY else "none"
    if eng == "claude" and not ANTHROPIC_API_KEY:
        log.warning("SUMMARY_ENGINE=claude but ANTHROPIC_API_KEY missing; summarization will be disabled.")
        eng = "none"
    return eng

ACTIVE_SUMMARY_ENGINE = _select_summary_engine()
log.info(f"Active summary engine: {ACTIVE_SUMMARY_ENGINE}")

# ===== 모델 선택용 상수 + .env 유틸 =====
PROVIDERS = [("openai", "ChatGPT / OpenAI"), ("gemini", "Gemini / Google"), ("claude", "Claude / Anthropic")]
MODELS = {
    "openai": [("gpt-4o", "멀티모달 플래그십"), ("gpt-4o-mini", "가성비/요약용"), ("gpt-4-turbo", "텍스트 중심(레거시)")],
    "gemini": [("gemini-1.5-pro", "정확도/긴 맥락"), ("gemini-1.5-flash", "저지연/저비용")],
    "claude": [("claude-3-opus-20240229", "최상위"), ("claude-3-sonnet-20240229", "균형"), ("claude-3-haiku-20240307", "고속/저비용")],
}
KEY_ENV   = {"openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY", "claude": "ANTHROPIC_API_KEY"}
MODEL_ENV = {"openai": "OPENAI_MODEL",   "gemini": "GEMINI_MODEL",   "claude": "ANTHROPIC_MODEL"}

def _env_read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

def _env_write_lines(path: Path, lines: list[str]):
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

def _env_set(lines: list[str], key: str, value: str) -> list[str]:
    out, found = [], False
    for line in lines:
        if line.strip().startswith(f"{key}="):
            out.append(f"{key}={value}"); found = True
        else:
            out.append(line)
    if not found:
        out.append(f"{key}={value}")
    return out

def _apply_summary_runtime(provider: str, model_name: str):
    """선택 즉시 런타임 변수 갱신 (재시작 없이 반영)."""
    global SUMMARY_ENGINE, ACTIVE_SUMMARY_ENGINE, GEMINI_MODEL, OPENAI_MODEL, ANTHROPIC_MODEL
    SUMMARY_ENGINE = provider
    if provider == "openai":
        OPENAI_MODEL = model_name
    elif provider == "gemini":
        GEMINI_MODEL = model_name
    elif provider == "claude":
        ANTHROPIC_MODEL = model_name
    def _reselect():
        eng = SUMMARY_ENGINE
        if eng == "openai" and not os.environ.get("OPENAI_API_KEY", "").strip():
            eng = "gemini" if os.environ.get("GEMINI_API_KEY", "").strip() else ("claude" if os.environ.get("ANTHROPIC_API_KEY","").strip() else "none")
        if eng == "gemini" and not os.environ.get("GEMINI_API_KEY", "").strip():
            eng = "claude" if os.environ.get("ANTHROPIC_API_KEY","").strip() else "none"
        if eng == "claude" and not os.environ.get("ANTHROPIC_API_KEY","").strip():
            eng = "none"
        return eng
    ACTIVE_SUMMARY_ENGINE = _reselect()
    log.info(f"Summary engine switched => {SUMMARY_ENGINE} / model={model_name} (active={ACTIVE_SUMMARY_ENGINE})")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None, threaded=True)

# ---------- HELP ----------
HELP_TEXT = (
    "Commands:\n"
    "/help   /menu /메뉴\n"
    "/ls <rclone-path>\n"
    "/status   /stop   /kill\n"
    "/env  - 환경설정 조회/변경 (.env 통일)\n"
    "/model - 요약 엔진/모델 선택\n"
    "/setkey <prov> <KEY>\n"
    "smr [path] / dwn [path]\n"
)

# ===================== Small helpers =====================
def _merged_env():
    merged_env = os.environ.copy()
    merged_env.setdefault("LC_ALL", "C")
    merged_env.setdefault("RCLONE_PROGRESS", "0")
    if RCLONE_CONF_PATH.exists():
        merged_env["RCLONE_CONFIG"] = str(RCLONE_CONF_PATH)
    return merged_env

def _send(chat_id: int, text: str):
    try: bot.send_message(chat_id, text)
    except Exception: pass

def run_cmd(cmd, cwd=None, timeout=None, env=None):
    merged_env = _merged_env()
    if env: merged_env.update(env)
    log.info("$ " + (cmd if isinstance(cmd, str) else " ".join(shlex.quote(str(x)) for x in cmd)))
    p = subprocess.run(cmd, cwd=cwd, env=merged_env, capture_output=True, text=False,
                       timeout=timeout, shell=isinstance(cmd, str))
    out = p.stdout.decode("utf-8", errors="ignore").strip()
    err = p.stderr.decode("utf-8", errors="ignore").strip()
    return p.returncode, out, err

def popen_cmd(cmd, cwd=None, env=None):
    merged_env = _merged_env()
    if env: merged_env.update(env)
    log.info("$ [popen] " + " ".join(shlex.quote(str(x)) for x in cmd))
    return subprocess.Popen(cmd, cwd=cwd, env=merged_env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=False)

def is_url(s: str) -> bool:
    try:
        u = urlparse(s.strip());  return u.scheme in ("http", "https") and u.netloc != ""
    except Exception: return False

def sanitize_filename(name: str) -> str:
    name = re.sub(r"\s+", " ", name).strip()
    name = re.sub(r'[\\/:*?"<>|\n\r\t]+', "_", name)
    return name or "file"

def ensure_dir(p: Path): p.mkdir(parents=True, exist_ok=True)
def kst_timestamp_prefix() -> str: return datetime.now().strftime("%Y.%m.%d_%H%M%S")

def safe_template(out_dir: Path) -> str:
    ts = kst_timestamp_prefix()
    return str(out_dir / f"{ts}-%(title).80B.%(ext)s")

# ===================== Time & range helpers =====================
def _parse_timecode(text: str) -> float:
    text = (text or "").strip()
    if not text:
        raise ValueError("empty timecode")
    parts = text.split(":")
    total = 0.0
    for part in parts:
        if part == "":
            raise ValueError("invalid timecode segment")
        try:
            value = float(part)
        except ValueError as e:
            raise ValueError("invalid timecode number") from e
        total = total * 60.0 + value
    if total < 0:
        raise ValueError("negative time not allowed")
    return total

def parse_time_range(expr: str) -> tuple[float, float | None]:
    text = (expr or "").strip()
    if not text:
        raise ValueError("빈 입력")
    low = text.lower()
    if low in {"all", "full", "entire", "whole", "전체", "전체구간", "원본"}:
        return 0.0, None
    m = re.split(r"[~\-–—]", text, maxsplit=1)
    if len(m) != 2:
        raise ValueError("`시작~끝` 형식으로 입력하세요")
    start_raw, end_raw = (m[0].strip(), m[1].strip())
    start = 0.0 if start_raw == "" else _parse_timecode(start_raw)
    end = None if end_raw == "" else _parse_timecode(end_raw)
    if end is not None and end <= start:
        raise ValueError("끝 시간이 시작 시간보다 커야 합니다")
    return start, end

def format_seconds_hms(sec: float) -> str:
    if sec is None:
        return "??:??:??"
    if sec < 0:
        sec = 0
    total = int(sec)
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h:02d}:{m:02d}:{s:02d}"

def format_seconds_label(sec: float) -> str:
    if sec < 0:
        sec = 0
    total = int(round(sec))
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h:02d}{m:02d}{s:02d}"

def probe_media_duration(path: Path) -> float | None:
    try:
        rc, out, _ = run_cmd([
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ])
        if rc != 0:
            return None
        return float(out.strip()) if out.strip() else None
    except Exception:
        return None

def extract_media_segment(src: Path, dst: Path, start: float, end: float | None) -> Path | None:
    if start < 0:
        start = 0.0
    duration = None if end is None else max(0.0, end - start)
    dst.unlink(missing_ok=True)
    cmd = ["ffmpeg", "-y", "-ss", f"{start:.3f}", "-i", str(src)]
    if duration is not None and duration > 0:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-c", "copy", "-avoid_negative_ts", "1", "-movflags", "+faststart", str(dst)]
    rc, _, _ = run_cmd(cmd)
    if rc == 0 and dst.exists() and dst.stat().st_size > 0:
        return dst

    dst.unlink(missing_ok=True)
    cmd = ["ffmpeg", "-y", "-ss", f"{start:.3f}", "-i", str(src)]
    if duration is not None and duration > 0:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart", "-avoid_negative_ts", "1",
        str(dst),
    ]
    rc, _, err = run_cmd(cmd)
    if rc != 0 or not dst.exists() or dst.stat().st_size == 0:
        log.error(f"Segment extraction failed: {err}")
        return None
    return dst

# ===================== rclone helpers =====================
def rclone_path(path: str) -> str:
    if not path: return f"{RCLONE_REMOTE}:"
    if path.startswith(f"{RCLONE_REMOTE}:"): return path
    if path.startswith("/"): path = path[1:]
    return f"{RCLONE_REMOTE}:/{path}"

def rclone_list_dirs_files(path: str):
    remote = rclone_path(path)
    rc1, out1, _ = run_cmd(["rclone", "lsf", remote, "--dirs-only"])
    rc2, out2, _ = run_cmd(["rclone", "lsf", remote, "--files-only"])
    dirs = [x.strip("/").strip() for x in out1.splitlines() if x.strip()] if rc1 == 0 else []
    files = [x.strip() for x in out2.splitlines() if x.strip()] if rc2 == 0 else []
    return dirs, files

def _join_remote(remote_dir: str, name: str) -> str:
    if remote_dir.endswith(":"):
        return remote_dir + "/" + name
    if remote_dir.endswith("/"):
        return remote_dir + name
    return remote_dir + "/" + name

def rclone_upload(local_file: Path, remote_folder: str) -> bool:
    """
    OneDrive가 간헐적으로 `itemNotFound`를 뱉는 이슈 완화:
    - 사전 mkdir
    - copyto(목적 파일 경로 지정) 사용 → 디렉토리 스캔 최소화
    - 재시도 + chunk-size 축소 fallback
    """
    remote_dir = rclone_path(remote_folder)
    # ensure remote dir exists (ignore rc)
    run_cmd(["rclone", "mkdir", remote_dir])

    # prefer original name; rclone가 OneDrive 금지문자 인코딩 처리함
    dest = _join_remote(remote_dir, local_file.name)

    def _try(args):
        rc, _, err = run_cmd(args)
        if rc != 0:
            log.error(f"rclone upload failed rc={rc}\n{err}")
        return rc == 0, err

    # 1) copyto 기본 시도
    ok, err = _try([
        "rclone", "copyto", str(local_file), dest,
        "--transfers", "1", "--checkers", "4",
        "--retries", "3", "--low-level-retries", "10",
        "--retries-sleep", "10s",
        "--progress=false", "--stats=0",
        "--ignore-times", "--checksum",
        "--no-traverse",
        "--onedrive-chunk-size", "10M",
    ])
    if ok: return True

    # 2) itemNotFound/경로 race → mkdir 재확인 후 chunk 더 작게 + 강한 no-traverse로 재시도
    if "itemNotFound" in (err or "") or "Item not found" in (err or ""):
        run_cmd(["rclone", "mkdir", remote_dir])  # re-ensure
        ok, _ = _try([
            "rclone", "copyto", str(local_file), dest,
            "--transfers", "1", "--checkers", "2",
            "--retries", "4", "--low-level-retries", "20",
            "--retries-sleep", "15s",
            "--progress=false", "--stats=0",
            "--ignore-times", "--checksum",
            "--no-traverse",
            "--onedrive-chunk-size", "5M",
        ])
        if ok: return True

    # 3) 최후 fallback: 디렉토리 대상으로 copy (엔진이 파일명 결정을 함)
    ok, _ = _try([
        "rclone", "copy", str(local_file), remote_dir,
        "--transfers", "1", "--checkers", "2",
        "--retries", "4", "--low-level-retries", "20",
        "--retries-sleep", "15s",
        "--progress=false", "--stats=0",
        "--ignore-times", "--checksum",
        "--onedrive-chunk-size", "5M",
    ])
    return ok

def rclone_download(remote_file: str, local_dir: Path) -> Path | None:
    ensure_dir(local_dir)
    remote = rclone_path(remote_file)
    rc, _, err = run_cmd(["rclone", "copy", remote, str(local_dir), "-P", "--stats=0"])
    if rc != 0:
        log.error(err); return None
    files = sorted(local_dir.glob("*"), key=lambda x: x.stat().st_mtime)
    return files[-1] if files else None

# ===================== yt-dlp helpers =====================
VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".ts", ".m4v"}

def yt_info(url: str) -> dict | None:
    rc, out, _ = run_cmd(["yt-dlp", "-J", url], timeout=30)
    if rc == 0 and out.strip():
        try: return json.loads(out)
        except Exception: return None
    return None

def detect_live(url: str) -> bool:
    info = yt_info(url); live = False
    if isinstance(info, dict):
        node = info.get("entries", [None])[0] if "entries" in info else info
        if isinstance(node, dict) and node.get("is_live") is True:
            live = True
    # 🔧 BUGFIX: 예전 코드의 'live = True or live' → 항상 True가 되어버림
    pattern = ("/live/" in url.lower()) or ("watch?v=" in url.lower() and "live" in url.lower())
    live = live or pattern
    return live

def _yt_common_opts():
    return [
        "--no-progress", "-N", "8", "--http-chunk-size", "10M",
        "--hls-prefer-ffmpeg",
        "--no-keep-fragments",
        "--downloader-args", "ffmpeg:-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 2",
        "-f", "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/best",
        "--remux-video", "mp4", "--merge-output-format", "mp4",
        "--postprocessor-args", "ffmpeg:-movflags +faststart -bsf:a aac_adtstoasc",
    ]

def yt_download(url: str, out_dir: Path) -> Path | None:
    ensure_dir(out_dir)
    tmpl = safe_template(out_dir)
    rc, _, err = run_cmd(["yt-dlp", *(_yt_common_opts()), "-o", tmpl, url])
    if rc != 0:
        log.error(err); return None
    vids = [p for p in out_dir.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTS]
    vids.sort(key=lambda x: x.stat().st_mtime)
    return vids[-1] if vids else None

def yt_record_live(url: str, out_dir: Path) -> subprocess.Popen | None:
    ensure_dir(out_dir)
    tmpl = safe_template(out_dir)
    cmd = ["yt-dlp", "--no-part", "--live-from-start", *(_yt_common_opts()), "-o", tmpl, url]
    try:
        return popen_cmd(cmd)
    except Exception as e:
        log.error(f"record live failed: {e}"); return None

def _pick_latest_video(base: Path, min_size_mb: float = 5.0) -> Path | None:
    if not base.exists(): return None
    cands = []
    for p in base.iterdir():
        try:
            if p.is_file() and p.suffix.lower() in VIDEO_EXTS and p.stat().st_size >= min_size_mb * 1024 * 1024:
                cands.append(p)
        except Exception: pass
    if not cands: return None
    return max(cands, key=lambda x: x.stat().st_mtime)

def _candidate_record_dirs() -> list[Path]:
    # 과거 경로 포함: /root/yt-bot/recordings 등
    cands = [
        BOT_HOME / "recordings",
        Path("/home/file/recordings"),
        Path("/root/yt-bot/recordings"),
        Path.home() / "yt-bot" / "recordings",
    ]
    uniq, seen = [], set()
    for p in cands:
        s = str(p)
        if s not in seen:
            uniq.append(p); seen.add(s)
    return uniq

def _pick_latest_video_across(paths: list[Path], min_size_mb: float = 5.0) -> tuple[Path | None, Path | None]:
    best_file, best_dir, best_mtime = None, None, -1
    for d in paths:
        f = _pick_latest_video(d, min_size_mb=min_size_mb)
        if f:
            mt = f.stat().st_mtime
            if mt > best_mtime:
                best_file, best_dir, best_mtime = f, d, mt
    return best_file, best_dir

# ---- remux safety ----
def ensure_mp4_faststart(src: Path) -> Path:
    try:
        rc, out, _ = run_cmd(["ffprobe","-v","error","-show_entries","format=format_name","-of","default=nw=1:nk=1",str(src)])
        fmt = (out or "").strip().lower() if rc == 0 else ""
        dst = src.with_suffix(".mp4"); tmp = src.with_suffix(".fixed.mp4")
        if "mp4" in fmt and "ism" not in fmt:
            rc2, _, _ = run_cmd(["ffmpeg","-y","-err_detect","ignore_err","-i",str(src),"-c","copy","-movflags","+faststart","-bsf:a","aac_adtstoasc",str(tmp)])
            if rc2 == 0 and tmp.exists() and tmp.stat().st_size > 0:
                try: src.unlink(missing_ok=True)
                except Exception: pass
                tmp.rename(dst); return dst
            return src
        rc3, _, _ = run_cmd(["ffmpeg","-y","-err_detect","ignore_err","-fflags","+genpts","-i",str(src),"-c","copy","-movflags","+faststart","-bsf:a","aac_adtstoasc",str(dst)])
        if rc3 == 0 and dst.exists() and dst.stat().st_size > 0: return dst
    except Exception as e:
        log.error(f"ensure_mp4_faststart failed: {e}")
    return src

# ===================== whisper =====================
_whisper_singleton = {"model": None}
def get_whisper():
    if _whisper_singleton["model"] is None:
        if WhisperModel is None: raise RuntimeError("faster-whisper not installed.")
        _whisper_singleton["model"] = WhisperModel(WHISPER_MODEL, device=WHISPER_DEVICE, compute_type="int8")
    return _whisper_singleton["model"]

def transcribe_file(media_path: Path) -> str:
    model = get_whisper()
    segments, _info = model.transcribe(str(media_path), beam_size=5, vad_filter=True)
    lines = [seg.text.strip() for seg in segments]
    return "\n".join([x for x in lines if x])

# ===================== summarizers =====================
def build_summary_prompt_ko(transcript: str) -> str:
    return f"""
다음 전사 내용을 한국어로만 요약하세요.

출력 형식 지침:
- '날짜/제목', '서론', '본론', '결론 및 적용', '3줄요약' 순서로 섹션을 구성하세요.
- '3줄요약'을 제외한 모든 섹션의 문단은 아래 기호로 계층 구조를 명확히 표현하세요:
  □ 대문단
    1. 중문단
        - 소문단
           → 소문단 하위 카테고리
- '날짜/제목' 섹션에는 가능한 경우 영상의 날짜와 핵심 주제를 함께 제시하세요. 정보가 없으면 전사에서 유추한 핵심 주제를 간결히 적으세요.
- 각 대문단은 설교 핵심 정리 예시에 준할 만큼 구체적인 사실, 신학적 의미, 상징, 적용점을 충분히 기술하세요.
- 필요 시 중문단과 소문단을 활용하여 근거, 설명, 성경적 연결고리, 실제 적용을 세부적으로 작성하세요.
- '결론 및 적용' 섹션에서는 실천적 적용과 믿음의 결단을 명시하세요.
- '3줄요약' 섹션은 한 문장씩 총 3줄로 작성하고 불릿을 사용하지 마세요.

전사:
\"\"\"{transcript}\"\"\""""

def summarize_with_gemini_ko(text: str) -> str:
    if not GEMINI_API_KEY: return "Gemini API key missing."
    try:
        import google.generativeai as genai
    except Exception:
        return "google-generativeai not installed."
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(GEMINI_MODEL)
    resp = model.generate_content(build_summary_prompt_ko(text), generation_config={"max_output_tokens": 2048})
    return (getattr(resp, "text", "") or "").strip()

def summarize_with_openai_ko(text: str) -> str:
    if not OPENAI_API_KEY: return "OpenAI API key missing."
    try:
        from openai import OpenAI
    except Exception:
        return "openai python package not installed."
    client = OpenAI(api_key=OPENAI_API_KEY)
    prompt = build_summary_prompt_ko(text)
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=2048, temperature=0.2,
    )
    try: return resp.choices[0].message.content.strip()
    except Exception: return "OpenAI response parsing error."

def summarize_with_claude_ko(text: str) -> str:
    if not ANTHROPIC_API_KEY: return "Anthropic API key missing."
    try:
        import anthropic
    except Exception:
        return "anthropic python package not installed."
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = build_summary_prompt_ko(text)
    try:
        msg = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=2048,
            temperature=0.2,
            messages=[{"role":"user","content":prompt}],
        )
        if getattr(msg, "content", None):
            parts = []
            for b in msg.content:
                t = getattr(b, "text", None)
                if t: parts.append(t)
            return "\n".join(parts).strip() or "Empty response from Claude."
        return "Claude response parsing error."
    except Exception as e:
        return f"Claude error: {e}"

def summarize_ko(text: str) -> str:
    if ACTIVE_SUMMARY_ENGINE == "openai":  return summarize_with_openai_ko(text)
    if ACTIVE_SUMMARY_ENGINE == "gemini":  return summarize_with_gemini_ko(text)
    if ACTIVE_SUMMARY_ENGINE == "claude":  return summarize_with_claude_ko(text)
    return "Summarization engine not configured. Check SUMMARY_ENGINE / API keys in .env."

# ===================== Jobs & recording state =====================
active_jobs = {}
recording_procs = {}     # chat_id -> subprocess.Popen
STOP_LOCK = BOT_HOME / ".stop.lock"

def process_pipeline(url: str, chat_id: int, do_transcribe: bool, do_summary: bool):
    if detect_live(url):  # 라이브는 여기선 다운로드만
        do_transcribe = False; do_summary = False
    job = {"status": "downloading", "url": url}
    active_jobs[chat_id] = job
    _send(chat_id, "Working...")
    workdir = Path(tempfile.mkdtemp(prefix="job_", dir=str(BOT_HOME)))
    try:
        media = yt_download(url, workdir)
        if not media: _send(chat_id, "Download failed."); return
        media = ensure_mp4_faststart(media)
        job["status"] = "uploading"
        ok = rclone_upload(media, RCLONE_FOLDER_VIDEOS)
        if not ok: _send(chat_id, "Upload failed."); return
        if not (do_transcribe or do_summary):
            try: media.unlink(missing_ok=True)
            except Exception: pass
            _send(chat_id, "Uploaded."); return
        if do_transcribe:
            _send(chat_id, "Transcribing...")
            transcript = transcribe_file(media)
            name_base = sanitize_filename(media.stem)
            tr_path = workdir / f"{name_base}.txt"
            tr_path.write_text(transcript, encoding="utf-8")
            rclone_upload(tr_path, RCLONE_FOLDER_TRANSCRIPTS)
            if do_summary:
                _send(chat_id, "Summarizing...")
                summary = summarize_ko(transcript)
                sm_path = workdir / f"{name_base}.summary.txt"
                sm_path.write_text(summary, encoding="utf-8")
                rclone_upload(sm_path, RCLONE_FOLDER_TRANSCRIPTS)
                _send(chat_id, "Done. Transcript & summary uploaded.")
            else:
                _send(chat_id, "Done. Transcript uploaded.")
    finally:
        try: shutil.rmtree(workdir, ignore_errors=True)
        except Exception: pass
        job["status"] = "idle"

def start_live_record(chat_id: int, url: str):
    if chat_id in recording_procs and recording_procs[chat_id] and recording_procs[chat_id].poll() is None:
        _send(chat_id, "Live recording already running."); return
    if not detect_live(url):
        _send(chat_id, "Not detected as live. Running normal download.")
        threading.Thread(target=process_pipeline, args=(url, chat_id, False, False), daemon=True).start(); return
    outdir = BOT_HOME / "recordings"
    p = yt_record_live(url, outdir)
    if p:
        recording_procs[chat_id] = p
        _send(chat_id, "Recording live… Use /stop to finish and upload.")
    else:
        _send(chat_id, "Failed to start live recording.")

def stop_live_record(chat_id: int):
    try:
        STOP_LOCK.touch(exist_ok=False)
    except Exception:
        _send(chat_id, "Stop already in progress."); return
    p = recording_procs.get(chat_id)
    if p and p.poll() is None:
        try:
            p.send_signal(signal.SIGINT)
            try: p.wait(timeout=20)
            except subprocess.TimeoutExpired:
                p.terminate()
                try: p.wait(timeout=10)
                except subprocess.TimeoutExpired: p.kill()
        except Exception:
            pass
        finally:
            recording_procs.pop(chat_id, None)
    # 녹화 프로세스 유무와 관계없이, 후보 디렉토리에서 최신 파일을 찾아 업로드
    latest, found_dir = _pick_latest_video_across(_candidate_record_dirs(), min_size_mb=5.0)
    if not latest:
        _send(chat_id, "Stopped. No valid recorded file (or too small).")
        STOP_LOCK.unlink(missing_ok=True); return
    final_path = ensure_mp4_faststart(latest)
    ok = rclone_upload(final_path, RCLONE_FOLDER_VIDEOS)
    if ok:
        try: final_path.unlink(missing_ok=True)
        except Exception: pass
        where = f" from {found_dir}" if found_dir else ""
        _send(chat_id, "Live recording stopped and uploaded" + where + ".")
    else:
        _send(chat_id, "Stopped but upload failed.")
    STOP_LOCK.unlink(missing_ok=True)

# ===================== Token store (callback_data 안전) =====================
class _TokenStore:
    def __init__(self, ttl_sec: int = 3600):
        self.ttl = ttl_sec
        self._lock = threading.Lock()
        self._data = {}  # token -> (kind, value, ts)
    def _gc(self):
        now = time.time()
        dead = [t for t, (_, _, ts) in self._data.items() if now - ts > self.ttl]
        for t in dead: self._data.pop(t, None)
    def put(self, kind: str, value: str) -> str:
        token = secrets.token_urlsafe(16)[:40]
        with self._lock:
            self._gc(); self._data[token] = (kind, value, time.time())
        return token
    def get(self, token: str):
        with self._lock:
            self._gc(); return self._data.get(token, None)

TOKENS = _TokenStore(ttl_sec=3600)
def make_cb_token(kind: str, value: str) -> str: return "T:" + TOKENS.put(kind, value)
def parse_cb_token(data: str):
    if not data or not data.startswith("T:"): return (None, None)
    rec = TOKENS.get(data[2:]);  return (rec[0], rec[1]) if rec else (None, None)

# ===== /env 상태 관리 =====
PENDING_ENV_EDIT: dict[int, str] = {}  # chat_id -> varname
PENDING_SMR_RANGE: dict[int, dict[str, str]] = {}  # chat_id -> {"remote_path": str}

ENV_EDITABLE_KEYS = [
    "BOT_TOKEN",                 # ⚠️ 변경 후 재시작 필요
    "RCLONE_REMOTE",
    "RCLONE_FOLDER_VIDEOS",
    "RCLONE_FOLDER_TRANSCRIPTS",
    "SUMMARY_ENGINE",
    "OPENAI_MODEL",
    "GEMINI_MODEL",
    "ANTHROPIC_MODEL",
    "WHISPER_MODEL",
    "WHISPER_DEVICE",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "ANTHROPIC_API_KEY",
]

def _mask(v: str) -> str:
    if not v: return "(empty)"
    if len(v) <= 6: return "*" * len(v)
    return v[:3] + "*" * (len(v)-7) + v[-4:]

def _env_as_dict() -> dict:
    d = {
        "BOT_TOKEN": _mask(os.environ.get("BOT_TOKEN","")),
        "RCLONE_REMOTE": os.environ.get("RCLONE_REMOTE", ""),
        "RCLONE_FOLDER_VIDEOS": os.environ.get("RCLONE_FOLDER_VIDEOS", ""),
        "RCLONE_FOLDER_TRANSCRIPTS": os.environ.get("RCLONE_FOLDER_TRANSCRIPTS", ""),
        "SUMMARY_ENGINE": os.environ.get("SUMMARY_ENGINE", SUMMARY_ENGINE),
        "OPENAI_MODEL": os.environ.get("OPENAI_MODEL", OPENAI_MODEL),
        "GEMINI_MODEL": os.environ.get("GEMINI_MODEL", GEMINI_MODEL),
        "ANTHROPIC_MODEL": os.environ.get("ANTHROPIC_MODEL", ANTHROPIC_MODEL),
        "WHISPER_MODEL": os.environ.get("WHISPER_MODEL", WHISPER_MODEL),
        "WHISPER_DEVICE": os.environ.get("WHISPER_DEVICE", WHISPER_DEVICE),
        "OPENAI_API_KEY": _mask(os.environ.get("OPENAI_API_KEY","")),
        "GEMINI_API_KEY": _mask(os.environ.get("GEMINI_API_KEY","")),
        "ANTHROPIC_API_KEY": _mask(os.environ.get("ANTHROPIC_API_KEY","")),
        "BOT_HOME": str(BOT_HOME),
        "ENV_PATH": str(ENV_PATH),
    }
    return d

def _kb_env_root():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("🔎 환경설정 조회", callback_data=make_cb_token("env_view", "")),
        InlineKeyboardButton("✏️ 환경설정 변경", callback_data=make_cb_token("env_change", "")),
    ); return kb

def _kb_env_change_list():
    kb = InlineKeyboardMarkup(row_width=1)
    for k in ENV_EDITABLE_KEYS:
        kb.add(InlineKeyboardButton(k, callback_data=make_cb_token("env_setkey", k)))
    kb.add(InlineKeyboardButton("⬅️ 뒤로", callback_data=make_cb_token("env_back", "")))
    return kb

# ===================== Browsers (SMR & DWN) =====================
def _kb_for_dir_listing(base_path: str, folders: list[str], files: list[str]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    up_tok = make_cb_token('up', base_path or "")
    kb.add(InlineKeyboardButton("⬆️", callback_data=up_tok),
           InlineKeyboardButton("..",  callback_data=up_tok))
    row = []
    for d in folders:
        full_path = f"{(base_path or '').rstrip('/')}/{d}".lstrip("/")
        row.append(InlineKeyboardButton(f"{d}/", callback_data=make_cb_token('dir', full_path)))
        if len(row) == 2: kb.add(*row); row = []
    if row: kb.add(*row); row = []
    for f in files:
        full_path = f"{(base_path or '').rstrip('/')}/{f}".lstrip("/")
        row.append(InlineKeyboardButton(f"{f}", callback_data=make_cb_token('file', full_path)))
        if len(row) == 2: kb.add(*row); row = []
    if row: kb.add(*row)
    return kb

def _kb_for_dir_listing_dwn(base_path: str, folders: list[str], files: list[str]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    up_tok = make_cb_token('dwn_up', base_path or "")
    kb.add(InlineKeyboardButton("⬆️", callback_data=up_tok),
           InlineKeyboardButton("..",  callback_data=up_tok))
    row = []
    for d in folders:
        full_path = f"{(base_path or '').rstrip('/')}/{d}".lstrip("/")
        row.append(InlineKeyboardButton(f"{d}/", callback_data=make_cb_token('dwn_dir', full_path)))
        if len(row) == 2: kb.add(*row); row = []
    if row: kb.add(*row); row = []
    for f in files:
        full_path = f"{(base_path or '').rstrip('/')}/{f}".lstrip("/")
        row.append(InlineKeyboardButton(f"⬇️ {f}", callback_data=make_cb_token('dwn_file', full_path)))
        if len(row) == 2: kb.add(*row); row = []
    if row: kb.add(*row)
    return kb

def send_dir_listing(chat_id: int, base_path: str):
    dirs, files = rclone_list_dirs_files(base_path)
    kb = _kb_for_dir_listing(base_path, dirs, files)
    bot.send_message(chat_id, f"List {rclone_path(base_path or '')}:\nSelect a file to transcribe/summarize:", reply_markup=kb)

def send_dir_listing_dwn(chat_id: int, base_path: str):
    dirs, files = rclone_list_dirs_files(base_path)
    kb = _kb_for_dir_listing_dwn(base_path, dirs, files)
    bot.send_message(chat_id, f"Download from {rclone_path(base_path or '')}:\nTap a file to send it here.", reply_markup=kb)

# ===================== 메뉴 (/menu, /메뉴) =====================
def _kb_providers() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)
    for k, label in PROVIDERS:
        kb.add(InlineKeyboardButton(f"{label}", callback_data=make_cb_token("mdl_prov", k)))
    return kb

def _kb_models(provider: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)
    for m, label in MODELS.get(provider, []):
        kb.add(InlineKeyboardButton(f"{m} – {label}", callback_data=make_cb_token("mdl_model", f"{provider}|{m}")))
    kb.add(InlineKeyboardButton("⬅️ 뒤로", callback_data=make_cb_token("mdl_back","")))
    return kb

def _kb_main_menu():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("🔽 URL 다운로드", callback_data=make_cb_token("prompt_url","download")),
        InlineKeyboardButton("🎥 라이브 녹화 시작", callback_data=make_cb_token("prompt_url","live")),
    )
    kb.add(
        InlineKeyboardButton("🛑 라이브 정지(/stop)", callback_data=make_cb_token("menu_stop","")),
        InlineKeyboardButton("📊 상태 보기(/status)", callback_data=make_cb_token("menu_status","")),
    )
    kb.add(
        InlineKeyboardButton("📂 SMR: 전사/요약", callback_data=make_cb_token("menu_smr","")),
        InlineKeyboardButton("⬇️ DWN: 파일받기", callback_data=make_cb_token("menu_dwn","")),
    )
    kb.add(
        InlineKeyboardButton("⚙️ 환경설정", callback_data=make_cb_token("menu_env","")),
        InlineKeyboardButton("🤖 모델 선택", callback_data=make_cb_token("menu_model","")),
    )
    return kb

@bot.message_handler(commands=["menu", "메뉴"])
def cmd_menu(m):
    bot.send_message(m.chat.id, "원하는 기능을 선택하세요:", reply_markup=_kb_main_menu())

# ===================== Handlers =====================
@bot.message_handler(commands=["help", "start"])
def cmd_help(m): bot.reply_to(m, HELP_TEXT)

@bot.message_handler(commands=["ls"])
def cmd_ls(m):
    arg = m.text.strip().split(maxsplit=1)
    path = arg[1] if len(arg) == 2 else ""
    dirs, files = rclone_list_dirs_files(path)
    lines = [f"List {rclone_path(path)}:"]
    for d in dirs: lines.append(d + "/")
    for f in files: lines.append(f)
    bot.reply_to(m, "\n".join(lines) if len(lines) > 1 else f"List {rclone_path(path)}: (empty or error)")

@bot.message_handler(commands=["stop"])
def cmd_stop(m): stop_live_record(m.chat.id)

@bot.message_handler(commands=["kill"])
def cmd_kill(m):
    chat_id = m.chat.id
    p = recording_procs.get(chat_id)
    if p and p.poll() is None:
        try:
            p.terminate(); time.sleep(2)
            if p.poll() is None: p.kill()
        except Exception: pass
        recording_procs.pop(chat_id, None)
        _send(chat_id, "Force-stopped current job.")
    else:
        try: subprocess.run("pgrep -f 'yt-dlp|ffmpeg' | xargs -r kill -9", shell=True)
        except Exception: pass
        _send(chat_id, "No active job.")

@bot.message_handler(commands=["status"])
def cmd_status(m):
    chat_id = m.chat.id
    p = recording_procs.get(chat_id)
    alive = (p and p.poll() is None)
    msg = [f"Recording process: {'RUNNING' if alive else 'IDLE'}"]
    # 후보 디렉토리 전체에서 최신 파일 확인
    latest, found_dir = _pick_latest_video_across(_candidate_record_dirs(), min_size_mb=5.0)
    if latest:
        try:
            sz = latest.stat().st_size
            msg.append(f"Latest file: {latest.name} ({sz//(1024*1024)} MB) in {found_dir}")
        except Exception:
            pass
    bot.send_message(chat_id, "\n".join(msg))

@bot.message_handler(commands=["env"])
def cmd_env(m): bot.reply_to(m, "환경설정 메뉴를 선택하세요:", reply_markup=_kb_env_root())

@bot.message_handler(commands=["model"])
def cmd_model(m): bot.reply_to(m, "요약 엔진 공급자를 선택하세요:", reply_markup=_kb_providers())

@bot.message_handler(commands=["setkey"])
def cmd_setkey(m):
    try:
        _, prov, key = m.text.strip().split(maxsplit=2)
        prov = prov.lower()
        if prov not in KEY_ENV:
            bot.reply_to(m, "지원하지 않는 공급자입니다. openai | gemini | claude"); return
        env_key = KEY_ENV[prov]
        lines = _env_read_lines(ENV_PATH)
        lines = _env_set(lines, env_key, key)
        _env_write_lines(ENV_PATH, lines)
        os.environ[env_key] = key  # 런타임 반영
        bot.reply_to(m, f"{env_key} 저장 완료.")
    except Exception:
        bot.reply_to(m, "형식: /setkey <openai|gemini|claude> <API_KEY>")

@bot.message_handler(func=lambda m: m.chat.id in PENDING_ENV_EDIT and m.text)
def handle_env_value_input(m):
    chat_id = m.chat.id
    var = PENDING_ENV_EDIT.pop(chat_id, None)
    if not var: return
    val = m.text.strip()
    lines = _env_read_lines(ENV_PATH)
    lines = _env_set(lines, var, val)
    _env_write_lines(ENV_PATH, lines)
    os.environ[var] = val

    global RCLONE_REMOTE, RCLONE_FOLDER_VIDEOS, RCLONE_FOLDER_TRANSCRIPTS
    global SUMMARY_ENGINE, OPENAI_MODEL, GEMINI_MODEL, ANTHROPIC_MODEL, WHISPER_MODEL, WHISPER_DEVICE, BOT_TOKEN
    if var == "RCLONE_REMOTE": RCLONE_REMOTE = val
    if var == "RCLONE_FOLDER_VIDEOS": RCLONE_FOLDER_VIDEOS = val
    if var == "RCLONE_FOLDER_TRANSCRIPTS": RCLONE_FOLDER_TRANSCRIPTS = val
    if var in ("SUMMARY_ENGINE","OPENAI_MODEL","GEMINI_MODEL","ANTHROPIC_MODEL"):
        if var == "SUMMARY_ENGINE": SUMMARY_ENGINE = val
        if var == "OPENAI_MODEL": OPENAI_MODEL = val
        if var == "GEMINI_MODEL": GEMINI_MODEL = val
        if var == "ANTHROPIC_MODEL": ANTHROPIC_MODEL = val
        try:
            model_pick = OPENAI_MODEL if SUMMARY_ENGINE=="openai" else (GEMINI_MODEL if SUMMARY_ENGINE=="gemini" else ANTHROPIC_MODEL)
            _apply_summary_runtime(SUMMARY_ENGINE, model_pick)
        except Exception: pass
    if var == "WHISPER_MODEL": WHISPER_MODEL = val
    if var == "WHISPER_DEVICE": WHISPER_DEVICE = val
    if var == "BOT_TOKEN":
        BOT_TOKEN = val
        bot.reply_to(m, "✅ BOT_TOKEN 저장 완료. *프로세스 재시작* 후 적용됩니다.", parse_mode=None); return

    bot.reply_to(m, f"✅ {var} = `{val}` 저장 및 반영 완료", parse_mode=None)

@bot.message_handler(func=lambda m: m.text and m.text.strip().lower().startswith("smr"))
def trig_smr(m):
    arg = m.text.strip().split(maxsplit=1)
    start = arg[1] if len(arg) == 2 else ""
    send_dir_listing(m.chat.id, start)

@bot.message_handler(func=lambda m: m.text and m.text.strip().lower().startswith("dwn"))
def trig_dwn(m):
    arg = m.text.strip().split(maxsplit=1)
    start = arg[1] if len(arg) == 2 else ""
    send_dir_listing_dwn(m.chat.id, start)

# ---------- 파일 선택 콜백 동작(최소 구현) ----------
def _with_tempdir(func):
    def _wrap(*a, **kw):
        workdir = Path(tempfile.mkdtemp(prefix="cb_", dir=str(BOT_HOME)))
        try:
            return func(workdir, *a, **kw)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
    return _wrap

def handle_rsm_file_selected(chat_id: int, remote_path: str):
    PENDING_SMR_RANGE[chat_id] = {"remote_path": remote_path}
    display_path = rclone_path(remote_path)
    msg = (
        f"선택한 파일: {display_path}\n"
        "전사/요약할 구간을 `시작~끝` 형식으로 입력하세요.\n"
        "예) 00:05:00~00:12:30  |  00:10:00~  (끝까지)\n"
        "전체 파일은 `all` 또는 `full` 입력, 취소는 `/cancel` 또는 `취소`."
    )
    _send(chat_id, msg)

@bot.message_handler(func=lambda m: m.chat.id in PENDING_SMR_RANGE and m.text)
def handle_smr_range_input(m):
    chat_id = m.chat.id
    entry = PENDING_SMR_RANGE.get(chat_id)
    if not entry:
        return
    text = m.text.strip()
    if not text:
        return
    lowered = text.lower()
    if text.startswith("/") and lowered not in {"/cancel", "/취소"}:
        return  # allow 다른 명령어
    if lowered in {"/cancel", "cancel"} or text in {"취소", "/취소"}:
        PENDING_SMR_RANGE.pop(chat_id, None)
        _send(chat_id, "SMR 입력을 취소했습니다.")
        return
    try:
        start_sec, end_sec = parse_time_range(text)
    except ValueError as e:
        _send(chat_id, f"시간 형식 오류: {e}\n예) 00:05:00~00:12:30 또는 all")
        return

    PENDING_SMR_RANGE.pop(chat_id, None)
    end_display = "END" if end_sec is None else format_seconds_hms(end_sec)
    _send(chat_id, f"SMR: {format_seconds_hms(start_sec)} → {end_display} 구간 처리 시작")
    _launch_smr_job(chat_id, entry["remote_path"], start_sec, end_sec)

def _launch_smr_job(chat_id: int, remote_path: str, start: float, end: float | None):
    threading.Thread(target=_execute_smr_job, args=(chat_id, remote_path, start, end), daemon=True).start()

@_with_tempdir
def _execute_smr_job(workdir: Path, chat_id: int, remote_path: str, start_sec: float, end_sec: float | None):
    display_path = rclone_path(remote_path)
    _send(chat_id, f"SMR: `{display_path}` 다운로드 중 …")
    local = rclone_download(remote_path, workdir)
    if not local:
        _send(chat_id, "Download failed."); return

    duration = probe_media_duration(local)
    start_val = max(0.0, start_sec)
    end_val = end_sec
    if duration is not None:
        if start_val >= duration:
            _send(chat_id, f"요청한 시작 시간이 영상 길이({format_seconds_hms(duration)})보다 길어요.")
            return
        if end_val is None or end_val > duration:
            end_val = duration
    if end_val is not None and end_val <= start_val:
        _send(chat_id, "요청 구간이 너무 짧습니다.")
        return

    pretty_start = format_seconds_hms(start_val)
    pretty_end = "END" if end_val is None else format_seconds_hms(end_val)
    if end_val is not None:
        seg_len = max(0.0, end_val - start_val)
        seg_info = f" ({format_seconds_hms(seg_len)} 길이)"
    else:
        seg_info = ""

    clip_path = local
    need_clip = start_val > 0 or (end_val is not None and (duration is None or end_val < duration))
    if need_clip:
        _send(chat_id, f"구간 추출 중… {pretty_start} → {pretty_end}{seg_info}")
        clip_dst = workdir / f"clip{local.suffix}"
        clip = extract_media_segment(local, clip_dst, start_val, end_val)
        if not clip:
            _send(chat_id, "구간 추출에 실패했습니다.")
            return
        clip_path = clip
    else:
        _send(chat_id, f"전체 구간 처리 중… ({pretty_start} → {pretty_end})")

    range_end_for_label = end_val if end_val is not None else (duration if duration is not None else None)
    if range_end_for_label is None:
        if start_val <= 0 and end_sec is None:
            range_tag = "full"
        else:
            range_tag = f"{format_seconds_label(start_val)}-END"
    elif start_val <= 0 and duration is not None and abs(range_end_for_label - duration) < 1.0:
        range_tag = "full"
    else:
        range_tag = f"{format_seconds_label(start_val)}-{format_seconds_label(range_end_for_label)}"

    name_base_raw = local.stem if range_tag == "full" else f"{local.stem}_{range_tag}"
    name_base = sanitize_filename(name_base_raw)

    try:
        _send(chat_id, "Transcribing…")
        tx = transcribe_file(clip_path)
        tr = workdir / f"{name_base}.txt"
        tr.write_text(tx, encoding="utf-8")
        rclone_upload(tr, RCLONE_FOLDER_TRANSCRIPTS)
        _send(chat_id, "Summarizing…")
        sm = summarize_ko(tx)
        smp = workdir / f"{name_base}.summary.txt"
        smp.write_text(sm, encoding="utf-8")
        rclone_upload(smp, RCLONE_FOLDER_TRANSCRIPTS)
        final_msg = (
            "완료 ✅ 전사 & 요약 업로드됨.\n"
            f"- 전사 파일: {tr.name}\n"
            f"- 요약 파일: {smp.name}\n"
            f"구간: {pretty_start} → {pretty_end}{seg_info}"
        )
        _send(chat_id, final_msg)
    except Exception as e:
        _send(chat_id, f"SMR failed: {e}")

@_with_tempdir
def handle_dwn_file_selected(workdir: Path, chat_id: int, remote_path: str):
    _send(chat_id, f"Downloading `{remote_path}` …")
    local = rclone_download(remote_path, workdir)
    if not local:
        _send(chat_id, "Download failed."); return
    try:
        sz = local.stat().st_size
        caption = f"{local.name} ({sz//(1024*1024)} MB)"
    except Exception:
        caption = local.name
    try:
        with open(local, "rb") as f:
            bot.send_document(chat_id, f, visible_file_name=local.name, caption=caption)
    except Exception:
        _send(chat_id, f"Downloaded to temp. File may be too large to send via Telegram ({caption}).")

# ---------- 콜백 ----------
@bot.callback_query_handler(func=lambda c: True)
def on_cb(c):
    kind, value = parse_cb_token(c.data)
    if not kind:
        try: bot.answer_callback_query(c.id, "Expired or invalid selection.")
        except Exception: pass
        return
    try: bot.answer_callback_query(c.id)
    except Exception: pass

    chat_id = c.message.chat.id

    # 메뉴 콜백
    if kind == "prompt_url":
        mode = value or "download"
        if mode == "download":
            bot.send_message(chat_id, "🔗 동영상 URL을 채팅창에 붙여넣으세요. (일반 다운로드 → 업로드)")
        else:
            bot.send_message(chat_id, "🔴 라이브 URL을 채팅창에 붙여넣으세요. (녹화 시작, 종료는 /stop)")
        return
    if kind == "menu_stop":
        stop_live_record(chat_id); return
    if kind == "menu_status":
        cmd_status(type("x",(object,),{"chat":type("y",(object,),{"id":chat_id})})()); return
    if kind == "menu_smr":
        bot.send_message(chat_id, "원드라이브 경로를 선택하세요:", reply_markup=_kb_for_dir_listing("", *rclone_list_dirs_files(""))); return
    if kind == "menu_dwn":
        bot.send_message(chat_id, "다운로드할 경로를 선택하세요:", reply_markup=_kb_for_dir_listing_dwn("", *rclone_list_dirs_files(""))); return
    if kind == "menu_env":
        bot.send_message(chat_id, "환경설정 메뉴:", reply_markup=_kb_env_root()); return
    if kind == "menu_model":
        bot.send_message(chat_id, "요약 엔진 공급자를 선택하세요:", reply_markup=_kb_providers()); return

    # ----- ENV flow -----
    if kind == "env_view":
        d = _env_as_dict()
        lines = ["현재 환경설정:"]
        for k,v in d.items(): lines.append(f"- {k}: {v}")
        bot.send_message(chat_id, "\n".join(lines)); return
    if kind == "env_change":
        bot.send_message(chat_id, "변경할 항목을 선택하세요:", reply_markup=_kb_env_change_list()); return
    if kind == "env_setkey":
        var = value or ""
        PENDING_ENV_EDIT[chat_id] = var
        bot.send_message(chat_id, f"`{var}` 새 값을 입력하세요.\n(입력 즉시 .env 저장 및 런타임 반영)", parse_mode=None); return
    if kind == "env_back":
        bot.send_message(chat_id, "환경설정 메뉴:", reply_markup=_kb_env_root()); return

    # ----- SMR flow -----
    if kind == 'up':
        base = value or ""
        parent = "/".join(base.strip("/").split("/")[:-1]) if "/" in base.strip("/") else ""
        send_dir_listing(chat_id, parent); return
    if kind == 'dir':
        send_dir_listing(chat_id, value or ""); return
    if kind == 'file':
        handle_rsm_file_selected(chat_id, value or ""); return

    # ----- DWN flow -----
    if kind == 'dwn_up':
        base = value or ""
        parent = "/".join(base.strip("/").split("/")[:-1]) if "/" in base.strip("/") else ""
        send_dir_listing_dwn(chat_id, parent); return
    if kind == 'dwn_dir':
        send_dir_listing_dwn(chat_id, value or ""); return
    if kind == 'dwn_file':
        handle_dwn_file_selected(chat_id, value or ""); return

    # ----- MODEL PICKER flow -----
    if kind == "mdl_prov":
        prov = (value or "").lower()
        bot.send_message(chat_id, f"{prov.upper()} 모델을 선택하세요:", reply_markup=_kb_models(prov)); return
    if kind == "mdl_back":
        bot.send_message(chat_id, "공급자를 다시 선택하세요:", reply_markup=_kb_providers()); return
    if kind == "mdl_model":
        pv, model = (value or "").split("|", 1)
        pv = pv.lower()
        lines = _env_read_lines(ENV_PATH)
        if ENV_PATH.exists(): shutil.copy2(ENV_PATH, ENV_PATH.with_suffix(".env.bak"))
        lines = _env_set(lines, "SUMMARY_ENGINE", pv)
        lines = _env_set(lines, MODEL_ENV[pv], model)
        _env_write_lines(ENV_PATH, lines)
        _apply_summary_runtime(pv, model)
        key_env = KEY_ENV[pv]
        has_env_key = os.environ.get(key_env, "").strip() or any(l.strip().startswith(key_env+"=") for l in lines)
        if not has_env_key:
            bot.send_message(chat_id, f"선택 완료 ✅\n엔진: {pv}\n모델: {model}\n\n⚠️ {key_env}가 없습니다. `/setkey {pv} <API_KEY>` 로 등록해주세요.")
        else:
            bot.send_message(chat_id, f"선택 완료 ✅\n엔진: {pv}\n모델: {model}\n(.env 저장 및 런타임 반영)")
        return

    bot.send_message(chat_id, "Unknown selection.")

# URL 핸들러
def is_live_url(url: str) -> bool: return detect_live(url)

@bot.message_handler(func=lambda m: m.text and is_url(m.text.strip()))
def handle_url(m):
    url = m.text.strip()
    try:
        if is_live_url(url):
            start_live_record(m.chat.id, url); return
        threading.Thread(target=process_pipeline, args=(url, m.chat.id, False, False), daemon=True).start()
    except Exception as e:
        try: bot.send_message(m.chat.id, f"Error: {e}")
        except Exception: pass

# ===================== Main =====================
if __name__ == "__main__":
    log.info("Bot started.")
    bot.infinity_polling(timeout=60, long_polling_timeout=60)
