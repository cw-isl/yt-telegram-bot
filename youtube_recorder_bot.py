#!/usr/bin/env python3
import os, re, json, time, shutil, logging, tempfile, subprocess, threading, shlex, secrets
from pathlib import Path
from urllib.parse import urlparse

import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# Optional: faster-whisper / gemini
try:
    from faster_whisper import WhisperModel
except Exception:
    WhisperModel = None

# ===================== Config (unchanged) =====================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not BOT_TOKEN or ":" not in BOT_TOKEN:
    raise RuntimeError("Set BOT_TOKEN env first.")

RCLONE_REMOTE = os.environ.get("RCLONE_REMOTE", "onedrive")
RCLONE_FOLDER_VIDEOS = os.environ.get("RCLONE_FOLDER_VIDEOS", "YouTube_Backup")
RCLONE_FOLDER_TRANSCRIPTS = os.environ.get("RCLONE_FOLDER_TRANSCRIPTS", "YouTube_Backup/Transcripts")

WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small")
WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "auto")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash")

BOT_HOME = Path(os.environ.get("BOT_HOME", "/home/file")).expanduser()
BOT_HOME.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ytbot")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None, threaded=True)

HELP_TEXT = (
    "Commands:\n"
    "/help                         - show help\n"
    "/ls <rclone-path>             - list remote (e.g. /ls onedrive: or /ls onedrive:/Folder)\n"
    "/stop                         - stop live recording (upload & remove local)\n"
    "smr [path]                    - browse OneDrive, pick a file to transcribe/summarize\n\n"
    "Direct input:\n"
    "- Paste a YouTube or normal video URL: it will download & upload only (no transcript/summary).\n"
)

# ===================== Small helpers =====================
def _send(chat_id: int, text: str):
    try:
        bot.send_message(chat_id, text)
    except Exception:
        pass

def run_cmd(cmd, cwd=None, timeout=None, env=None):
    """
    Run external command safely.
    Capture raw bytes and decode with errors='ignore' to avoid UnicodeDecodeError.
    Also reduce noisy progress output.
    Returns (returncode, stdout_str, stderr_str).
    """
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    merged_env.setdefault("LC_ALL", "C")
    merged_env.setdefault("RCLONE_PROGRESS", "0")

    log.info("$ " + (cmd if isinstance(cmd, str) else " ".join(shlex.quote(str(x)) for x in cmd)))
    p = subprocess.run(
        cmd,
        cwd=cwd,
        env=merged_env,
        capture_output=True,
        text=False,              # <— collect bytes
        timeout=timeout,
        shell=isinstance(cmd, str)
    )
    out = p.stdout.decode("utf-8", errors="ignore").strip()
    err = p.stderr.decode("utf-8", errors="ignore").strip()
    return p.returncode, out, err

def is_url(s: str) -> bool:
    try:
        u = urlparse(s.strip())
        return u.scheme in ("http", "https") and u.netloc != ""
    except Exception:
        return False

def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\n\r\t]+', "_", name).strip() or "file"

def ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)

def shlexq(x) -> str:
    return shlex.quote(str(x))

# ===================== rclone helpers =====================
def rclone_path(path: str) -> str:
    if not path:
        return f"{RCLONE_REMOTE}:"
    if path.startswith(f"{RCLONE_REMOTE}:"):
        return path
    if path.startswith("/"):
        path = path[1:]
    return f"{RCLONE_REMOTE}:/{path}"

def rclone_list_dirs_files(path: str):
    remote = rclone_path(path)
    rc1, out1, _ = run_cmd(["rclone", "lsf", remote, "--dirs-only"])
    rc2, out2, _ = run_cmd(["rclone", "lsf", remote, "--files-only"])
    dirs = [x.strip("/").strip() for x in out1.splitlines() if x.strip()] if rc1 == 0 else []
    files = [x.strip() for x in out2.splitlines() if x.strip()] if rc2 == 0 else []
    return dirs, files

def rclone_upload(local_file: Path, remote_folder: str) -> bool:
    remote = rclone_path(remote_folder)
    rc, _, err = run_cmd(["rclone", "copy", str(local_file), remote, "-P", "--stats=0"])
    if rc != 0:
        log.error(err)
        return False
    return True

def rclone_download(remote_file: str, local_dir: Path) -> Path | None:
    ensure_dir(local_dir)
    remote = rclone_path(remote_file)
    rc, _, err = run_cmd(["rclone", "copy", remote, str(local_dir), "-P", "--stats=0"])
    if rc != 0:
        log.error(err)
        return None
    files = sorted(local_dir.glob("*"), key=lambda x: x.stat().st_mtime)
    return files[-1] if files else None

# ===================== yt-dlp helpers =====================
def yt_info(url: str) -> dict | None:
    rc, out, _ = run_cmd(["yt-dlp", "-J", url], timeout=30)
    if rc == 0 and out.strip():
        try:
            return json.loads(out)
        except Exception:
            return None
    return None

def detect_live(url: str) -> bool:
    info = yt_info(url)
    live = False
    if isinstance(info, dict):
        node = info.get("entries", [None])[0] if "entries" in info else info
        if isinstance(node, dict) and node.get("is_live") is True:
            live = True
    if ("/live/" in url.lower()) or ("live" in url.lower()):
        live = True or live
    return live

def yt_download(url: str, out_dir: Path) -> Path | None:
    ensure_dir(out_dir)
    tmpl = str(out_dir / "%(title)s-%(id)s.%(ext)s")
    rc, _, err = run_cmd(["yt-dlp", "--no-progress", "-f", "best", "-o", tmpl, url])
    if rc != 0:
        log.error(err)
        return None
    vids = sorted(out_dir.glob("*"), key=lambda x: x.stat().st_mtime)
    return vids[-1] if vids else None

def yt_record_live(url: str, out_dir: Path) -> subprocess.Popen | None:
    ensure_dir(out_dir)
    tmpl = str(out_dir / "%(title)s-%(id)s.%(ext)s")
    cmd = ["yt-dlp", "--hls-use-mpegts", "--no-progress", "-N", "4", "-f", "best", "-o", tmpl, url]
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        return p
    except Exception as e:
        log.error(f"record live failed: {e}")
        return None

# ===================== whisper / gemini =====================
_whisper_singleton = {"model": None}

def get_whisper():
    if _whisper_singleton["model"] is None:
        if WhisperModel is None:
            raise RuntimeError("faster-whisper not installed.")
        _whisper_singleton["model"] = WhisperModel(WHISPER_MODEL, device=WHISPER_DEVICE, compute_type="int8")
    return _whisper_singleton["model"]

def transcribe_file(media_path: Path) -> str:
    model = get_whisper()
    segments, _info = model.transcribe(str(media_path), beam_size=5, vad_filter=True)
    lines = [seg.text.strip() for seg in segments]
    return "\n".join([x for x in lines if x])

def build_summary_prompt_ko(transcript: str) -> str:
    return f"""
다음 전사 내용을 한국어로만 요약하세요.
형식:
- 섹션 헤더 사용
- 핵심 내용 불릿 10~18개 (구체적으로)
- '핵심 인물/용어' 섹션 (이름·역할·정의)
- '숫자·지표' 섹션 (날짜·수치·통계·금액 등)
- '중요 인용' 섹션 (있다면 2~4개, 한 줄)
- 마지막 TL;DR 3~5줄
전사:
\"\"\"{transcript}\"\"\"
"""

def summarize_with_gemini_ko(text: str) -> str:
    if not GEMINI_API_KEY:
        return "Gemini API key missing."
    try:
        import google.generativeai as genai
    except Exception:
        return "google-generativeai not installed."
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(GEMINI_MODEL)
    resp = model.generate_content(build_summary_prompt_ko(text), generation_config={"max_output_tokens": 2048})
    return (getattr(resp, "text", "") or "").strip()

# ===================== Jobs & recording =====================
active_jobs = {}
recording_procs = {}

def process_pipeline(url: str, chat_id: int, do_transcribe: bool, do_summary: bool):
    live = detect_live(url)
    if live:
        do_transcribe = False
        do_summary = False

    job = {"status": "downloading", "url": url}
    active_jobs[chat_id] = job
    _send(chat_id, "Working...")

    workdir = Path(tempfile.mkdtemp(prefix="job_", dir=str(BOT_HOME)))
    try:
        media = yt_download(url, workdir)
        if not media:
            _send(chat_id, "Download failed.")
            return

        job["status"] = "uploading"
        ok = rclone_upload(media, RCLONE_FOLDER_VIDEOS)
        if not ok:
            _send(chat_id, "Upload failed.")
            return

        if not (do_transcribe or do_summary):
            try:
                media.unlink(missing_ok=True)
            except Exception:
                pass
            _send(chat_id, "Uploaded.")
            return

        if do_transcribe:
            _send(chat_id, "Transcribing...")
            transcript = transcribe_file(media)
            name_base = sanitize_filename(media.stem)
            tr_path = workdir / f"{name_base}.txt"
            tr_path.write_text(transcript, encoding="utf-8")
            rclone_upload(tr_path, RCLONE_FOLDER_TRANSCRIPTS)

            if do_summary:
                _send(chat_id, "Summarizing...")
                summary = summarize_with_gemini_ko(transcript)
                sm_path = workdir / f"{name_base}.summary.txt"
                sm_path.write_text(summary, encoding="utf-8")
                rclone_upload(sm_path, RCLONE_FOLDER_TRANSCRIPTS)
                _send(chat_id, "Done. Transcript & summary uploaded.")
            else:
                _send(chat_id, "Done. Transcript uploaded.")
    finally:
        try:
            shutil.rmtree(workdir, ignore_errors=True)
        except Exception:
            pass
        job["status"] = "idle"

def start_live_record(chat_id: int, url: str):
    if chat_id in recording_procs and recording_procs[chat_id] and recording_procs[chat_id].poll() is None:
        _send(chat_id, "Live recording already running.")
        return
    if not detect_live(url):
        _send(chat_id, "Not detected as live. Running normal download.")
        threading.Thread(target=process_pipeline, args=(url, chat_id, False, False), daemon=True).start()
        return
    outdir = BOT_HOME / "recordings"
    p = yt_record_live(url, outdir)
    if p:
        recording_procs[chat_id] = p
        _send(chat_id, "Recording live...")
    else:
        _send(chat_id, "Failed to start live recording.")

def stop_live_record(chat_id: int):
    p = recording_procs.get(chat_id)
    if not p or p.poll() is not None:
        _send(chat_id, "No active live recording.")
        return
    try:
        p.terminate()
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
    except Exception:
        pass

    outdir = BOT_HOME / "recordings"
    files = sorted(outdir.glob("*"), key=lambda x: x.stat().st_mtime) if outdir.exists() else []
    if not files:
        _send(chat_id, "Stopped. No recorded file found.")
        return
    latest = files[-1]
    ok = rclone_upload(latest, RCLONE_FOLDER_VIDEOS)
    if ok:
        try:
            latest.unlink(missing_ok=True)
        except Exception:
            pass
        _send(chat_id, "Live recording stopped and uploaded.")
    else:
        _send(chat_id, "Stopped but upload failed.")

# ===================== Token store for safe callback_data =====================
class _TokenStore:
    def __init__(self, ttl_sec: int = 3600):
        self.ttl = ttl_sec
        self._lock = threading.Lock()
        self._data = {}  # token -> (kind, value, ts)

    def _gc(self):
        now = time.time()
        dead = [t for t, (_, _, ts) in self._data.items() if now - ts > self.ttl]
        for t in dead:
            self._data.pop(t, None)

    def put(self, kind: str, value: str) -> str:
        token = secrets.token_urlsafe(16)[:40]  # keep short (<< 64 bytes)
        with self._lock:
            self._gc()
            self._data[token] = (kind, value, time.time())
        return token

    def get(self, token: str):
        with self._lock:
            self._gc()
            return self._data.get(token, None)

TOKENS = _TokenStore(ttl_sec=3600)

def make_cb_token(kind: str, value: str) -> str:
    return "T:" + TOKENS.put(kind, value)

def parse_cb_token(data: str):
    if not data or not data.startswith("T:"):
        return (None, None)
    rec = TOKENS.get(data[2:])
    if not rec:
        return (None, None)
    return rec[0], rec[1]

# ===================== RSM browse (tokenized keyboard) =====================
def _kb_for_dir_listing(base_path: str, folders: list[str], files: list[str]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)

    up_tok = make_cb_token('up', base_path or "")
    kb.add(
        InlineKeyboardButton("⬆️", callback_data=up_tok),
        InlineKeyboardButton("..",  callback_data=up_tok)
    )

    row = []
    for d in folders:
        full_path = f"{(base_path or '').rstrip('/')}/{d}".lstrip("/")
        tok = make_cb_token('dir', full_path)
        row.append(InlineKeyboardButton(f"{d}/", callback_data=tok))
        if len(row) == 2:
            kb.add(*row); row = []
    if row: kb.add(*row)

    row = []
    for f in files:
        full_path = f"{(base_path or '').rstrip('/')}/{f}".lstrip("/")
        tok = make_cb_token('file', full_path)
        row.append(InlineKeyboardButton(f, callback_data=tok))
        if len(row) == 2:
            kb.add(*row); row = []
    if row: kb.add(*row)

    return kb

def send_dir_listing(chat_id: int, base_path: str):
    dirs, files = rclone_list_dirs_files(base_path)
    remote_shown = rclone_path(base_path or "")
    kb = _kb_for_dir_listing(base_path, dirs, files)
    bot.send_message(chat_id, f"List {remote_shown}:\nSelect a file to transcribe/summarize:", reply_markup=kb)

def handle_rsm_file_selected(chat_id: int, remote_rel: str):
    _send(chat_id, "Downloading file…")
    workdir = Path(tempfile.mkdtemp(prefix="rsm_", dir=str(BOT_HOME)))
    try:
        local = rclone_download(remote_rel, workdir)
        if not local:
            _send(chat_id, "Download failed.")
            return
        _send(chat_id, "Transcribing…")
        transcript = transcribe_file(local)
        base = sanitize_filename(local.stem)
        tr = workdir / f"{base}.txt"
        tr.write_text(transcript, encoding="utf-8")
        rclone_upload(tr, RCLONE_FOLDER_TRANSCRIPTS)

        _send(chat_id, "Summarizing…")
        summary = summarize_with_gemini_ko(transcript)
        sm = workdir / f"{base}.summary.txt"
        sm.write_text(summary, encoding="utf-8")
        rclone_upload(sm, RCLONE_FOLDER_TRANSCRIPTS)
        _send(chat_id, "Done. Transcript & summary uploaded.")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

# ===================== Handlers =====================
@bot.message_handler(commands=["help", "start"])
def cmd_help(m):
    bot.reply_to(m, HELP_TEXT)

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
def cmd_stop(m):
    stop_live_record(m.chat.id)

@bot.message_handler(func=lambda m: m.text and m.text.strip().lower().startswith("smr"))
def trig_smr(m):
    arg = m.text.strip().split(maxsplit=1)
    start = arg[1] if len(arg) == 2 else ""
    send_dir_listing(m.chat.id, start)

@bot.callback_query_handler(func=lambda c: True)
def on_cb(c):
    kind, value = parse_cb_token(c.data)
    if not kind:
        try:
            bot.answer_callback_query(c.id, "Expired or invalid selection.")
        except Exception:
            pass
        return
    try:
        bot.answer_callback_query(c.id)
    except Exception:
        pass

    chat_id = c.message.chat.id
    if kind == 'up':
        base = value or ""
        if "/" in base.strip("/"):
            parent = "/".join(base.strip("/").split("/")[:-1])
        else:
            parent = ""
        send_dir_listing(chat_id, parent)
        return
    if kind == 'dir':
        send_dir_listing(chat_id, value or "")
        return
    if kind == 'file':
        handle_rsm_file_selected(chat_id, value or "")
        return
    bot.send_message(chat_id, "Unknown selection.")

# URL: download & upload only (no transcribe/summary)
@bot.message_handler(func=lambda m: m.text and is_url(m.text.strip()))
def handle_url(m):
    url = m.text.strip()
    threading.Thread(target=process_pipeline, args=(url, m.chat.id, False, False), daemon=True).start()

# ===================== Main =====================
if __name__ == "__main__":
    log.info("Bot started.")
    bot.infinity_polling(timeout=60, long_polling_timeout=60)
