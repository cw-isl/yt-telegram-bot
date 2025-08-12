#!/usr/bin/env bash
set -euo pipefail

# ===== Pre-checks (all must be YES) =====
echo "=== Pre-install checks ==="
read -rp "1) Have you created a Telegram BOT token? (yes/no): " Q1
read -rp "2) Have you generated the OneDrive OAuth JSON (for rclone)? (yes/no): " Q2
read -rp "3) Do you acknowledge that rclone must use OneDrive only for this setup? (yes/no): " Q3

to_lower() { echo "$1" | tr '[:upper:]' '[:lower:]'; }
ANS1=$(to_lower "${Q1:-no}")
ANS2=$(to_lower "${Q2:-no}")
ANS3=$(to_lower "${Q3:-no}")

if [[ "${ANS1}" != "yes" || "${ANS2}" != "yes" || "${ANS3}" != "yes" ]]; then
  echo
  echo "One or more answers were not 'yes'."
  echo "Please see the setup guide: http://mmm.com"
  echo "Aborting."
  exit 1
fi

echo "All checks passed. Continuing installation..."
echo

echo "=== YouTube Recorder Bot installer (Ubuntu) ==="

# ----- 0) Ask inputs -----
read -rp "Install directory (absolute) [/home/file]: " INSTALL_DIR
INSTALL_DIR="${INSTALL_DIR:-/home/file}"

# Detect owner for files (use calling user if run via sudo)
OWNER_USER="${SUDO_USER:-$USER}"
OWNER_GROUP="$(id -gn "${OWNER_USER}")"

# Create install dir if missing, fix ownership
sudo mkdir -p "$INSTALL_DIR"
sudo chown -R "${OWNER_USER}:${OWNER_GROUP}" "$INSTALL_DIR"

read -rp "Telegram BOT_TOKEN (format 123456:ABC...): " BOT_TOKEN
if [[ -z "${BOT_TOKEN}" ]]; then
  echo "BOT_TOKEN is required."
  exit 1
fi

read -rp "rclone remote name [onedrive]: " RCLONE_REMOTE
RCLONE_REMOTE="${RCLONE_REMOTE:-onedrive}"

read -rp "Remote folder for videos [YouTube_Backup]: " RCLONE_FOLDER_VIDEOS
RCLONE_FOLDER_VIDEOS="${RCLONE_FOLDER_VIDEOS:-YouTube_Backup}"

read -rp "Remote folder for transcripts [YouTube_Backup/Transcripts]: " RCLONE_FOLDER_TRANSCRIPTS
RCLONE_FOLDER_TRANSCRIPTS="${RCLONE_FOLDER_TRANSCRIPTS:-YouTube_Backup/Transcripts}"

read -rp "Whisper model [small]: " WHISPER_MODEL
WHISPER_MODEL="${WHISPER_MODEL:-small}"

read -rp "Whisper device (auto/cpu/cuda) [auto]: " WHISPER_DEVICE
WHISPER_DEVICE="${WHISPER_DEVICE:-auto}"

read -rp "Use Gemini summarization? (y/N): " USE_GEM
USE_GEM="${USE_GEM:-N}"
GEMINI_API_KEY=""
GEMINI_MODEL="gemini-1.5-flash"
if [[ "${USE_GEM,,}" == "y" || "${USE_GEM,,}" == "yes" ]]; then
  read -rp "GEMINI_API_KEY: " GEMINI_API_KEY
  read -rp "GEMINI_MODEL [gemini-1.5-flash]: " GEMINI_MODEL_IN
  GEMINI_MODEL="${GEMINI_MODEL_IN:-gemini-1.5-flash}"
fi

# ----- 1) System deps -----
echo "=== Installing system packages ==="
sudo apt-get update -y
sudo apt-get install -y python3-venv ffmpeg yt-dlp rclone curl

# ----- 2) Python venv -----
VENV_DIR="${INSTALL_DIR}/ytbot-venv"
if [[ ! -d "${VENV_DIR}" ]]; then
  echo "=== Creating venv at ${VENV_DIR} ==="
  sudo -u "${OWNER_USER}" -H python3 -m venv "${VENV_DIR}"
fi

# shellcheck disable=SC1090
source "${VENV_DIR}/bin/activate"
pip install --upgrade pip
pip install requests pyTelegramBotAPI faster-whisper google-generativeai

# ----- 3) App directories (ensure) -----
RECORD_DIR="${INSTALL_DIR}/recordings"
mkdir -p "${RECORD_DIR}"
chown -R "${OWNER_USER}:${OWNER_GROUP}" "${RECORD_DIR}"

# ----- 4) Pre-create remote folders (if possible) -----
echo "=== Ensuring remote folders exist via rclone ==="
if rclone about "${RCLONE_REMOTE}:" >/dev/null 2>&1; then
  rclone mkdir "${RCLONE_REMOTE}:${RCLONE_FOLDER_VIDEOS}" || true
  rclone mkdir "${RCLONE_REMOTE}:${RCLONE_FOLDER_TRANSCRIPTS}" || true
else
  echo "WARNING: rclone remote '${RCLONE_REMOTE}:' not reachable now."
  echo "Run 'rclone config' later if needed. Installation continues."
fi

# ----- 5) Write bot code -----
BOT_FILE="${INSTALL_DIR}/youtube_recorder_bot.py"
echo "=== Writing bot code to ${BOT_FILE} ==="
cat <<'PY' > "${BOT_FILE}"
#!/usr/bin/env python3
# All strings are English-only to avoid encoding issues.

import os, re, json, uuid, logging, tempfile, subprocess, threading
from pathlib import Path
from urllib.parse import urlparse

import requests
import telebot
from telebot import types

# -------- Config --------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not BOT_TOKEN or ":" not in BOT_TOKEN:
    raise RuntimeError("Set BOT_TOKEN env first (looks like '123456:ABC...').")

RCLONE_REMOTE = os.environ.get("RCLONE_REMOTE", "onedrive").strip()
RCLONE_FOLDER_VIDEOS = os.environ.get("RCLONE_FOLDER_VIDEOS", "YouTube_Backup").strip()
RCLONE_FOLDER_TRANSCRIPTS = os.environ.get("RCLONE_FOLDER_TRANSCRIPTS", "YouTube_Backup/Transcripts").strip()

WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small").strip()
WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "auto").strip()

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash").strip()

SAVE_DIR = Path(os.environ.get("BOT_HOME", str(Path.home()))).expanduser()
RECORD_DIR = SAVE_DIR / "recordings"
RECORD_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ytbot")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None, threaded=True)

HELP_TEXT = (
    "Commands:\n"
    "/help                       - show help\n"
    "/ls <rclone-path>           - list remote (e.g. /ls onedrive: or /ls onedrive:/Folder)\n"
    "/textlink <url>             - direct download & upload\n"
    "/normal <url>               - try to convert OneDrive/SharePoint link, then download\n"
    "\nShort trigger (no slash):\n"
    "smr [path]                  - list remote (default: onedrive:)\n"
)

# -------- Utils --------
def _send(chat_id: int, text: str):
    try: bot.send_message(chat_id, text)
    except Exception: pass

def run_cmd(cmd: list[str], timeout: int | None = None) -> tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr

def sanitize_filename(name: str, ext: str | None = None) -> str:
    base = re.sub(r"[\\/:*?\"<>|]+", "_", name).strip() or "file"
    if ext:
        if not ext.startswith("."): ext = "." + ext
        base += ext
    return base

# -------- OneDrive helpers --------
def is_onedrive_url(u: str) -> bool:
    lu = u.lower()
    return ("1drv.ms" in lu) or ("sharepoint.com" in lu) or ("my.sharepoint.com" in lu) or ("live.com" in lu)

def to_direct_download(url: str) -> str | None:
    u = url.strip()
    if not is_onedrive_url(u): return None
    if "download.aspx?share=" in u: return u
    try:
        if "1drv.ms" in u:
            r = requests.get(u, allow_redirects=True, timeout=20)
            for resp in r.history + [r]:
                loc = resp.headers.get("Location")
                if loc and "download.aspx" in loc: return loc
            if "download.aspx" in r.url: return r.url
            if "download=1" not in r.url:
                return r.url + ("&" if "?" in r.url else "?") + "download=1"
            return r.url
        if "download=1" not in u:
            return u + ("&" if "?" in u else "?") + "download=1"
        return u
    except Exception:
        return None

# -------- rclone helpers --------
def rclone_path(remote_or_path: str) -> str:
    if ":" in remote_or_path.split()[0]: return remote_or_path
    return f"{RCLONE_REMOTE}:{remote_or_path}"

def rclone_lsjson(target: str) -> tuple[bool, list[dict] | str]:
    target = rclone_path(target)
    code, out, err = run_cmd(["rclone", "lsjson", target])
    if code == 0:
        try: return True, json.loads(out)
        except Exception: return False, "Invalid JSON from rclone."
    return False, (err.strip() or "rclone lsjson failed")

def rclone_copy_file(local_file: Path, remote_subdir: str) -> tuple[bool, str]:
    target = rclone_path(remote_subdir)
    code, out, err = run_cmd(["rclone", "copy", str(local_file), target, "--progress"])
    return (code == 0), (out if code == 0 else err)

def rclone_copy_from_remote(remote_file: str, local_dir: Path) -> tuple[bool, Path | None, str]:
    target = rclone_path(remote_file)
    local_dir.mkdir(parents=True, exist_ok=True)
    code, out, err = run_cmd(["rclone", "copy", target, str(local_dir)])
    if code != 0: return False, None, (err or out)
    files = list(local_dir.glob("*"))
    if not files: return False, None, "No file copied."
    return True, max(files, key=lambda p: p.stat().st_mtime), "OK"

# -------- HTTP downloader --------
def guess_filename_from_headers(resp: requests.Response, fallback_name: str) -> str:
    cd = resp.headers.get("Content-Disposition", "")
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd, flags=re.IGNORECASE)
    if m: return sanitize_filename(m.group(1))
    return sanitize_filename(fallback_name)

def download_via_requests(direct_url: str, chat_id: int) -> Path | None:
    try:
        with requests.get(direct_url, stream=True, timeout=30) as r:
            if r.status_code >= 400:
                _send(chat_id, f"HTTP error {r.status_code} while downloading.")
                return None
            parsed = urlparse(direct_url)
            fallback = Path(parsed.path).name or "file"
            filename = guess_filename_from_headers(r, fallback)
            if "." not in filename:
                ctype = r.headers.get("Content-Type", "")
                ext = ".mp4" if "mp4" in ctype else ".bin"
                filename += ext
            local_path = RECORD_DIR / filename
            with open(local_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 512):
                    if chunk: f.write(chunk)
            return local_path
    except Exception as e:
        _send(chat_id, f"Download error: {e}")
        return None

# -------- transcription & summarization --------
def transcribe_whisper(local_path: Path) -> str | None:
    try:
        from faster_whisper import WhisperModel
    except Exception:
        return None
    try:
        model = WhisperModel(WHISPER_MODEL, device=WHISPER_DEVICE)
        segments, info = model.transcribe(str(local_path), beam_size=1)
        lines = [seg.text.strip() for seg in segments if getattr(seg, "text", "").strip()]
        text = "\n".join(lines).strip()
        return text or None
    except Exception:
        return None

def summarize_gemini(text: str) -> str | None:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    model_name = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash").strip()
    if not api_key: return None
    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(model_name)
        prompt = (
            "Summarize the following transcript in Korean with concise bullet points:\n"
            "1) Key points (<=10 lines)\n2) Details\n3) Action items\n\nTranscript:\n"
        )
        resp = model.generate_content(prompt + text[:120000])
        out = getattr(resp, "text", None)
        return (out or "").strip() or None
    except Exception:
        return None

# -------- pipeline --------
def process_remote_file_async(chat_id: int, remote_file: str):
    try:
        _send(chat_id, f"Processing:\n{remote_file}")
        with tempfile.TemporaryDirectory() as td:
            from pathlib import Path
            tmpd = Path(td)
            ok, local_path, msg = rclone_copy_from_remote(remote_file, tmpd)
            if not ok or not local_path:
                _send(chat_id, f"Remote copy failed:\n{msg[:1500]}")
                return
            _send(chat_id, "Transcribing...")
            transcript = transcribe_whisper(local_path)
            summary = None
            if transcript:
                _send(chat_id, "Summarizing...")
                summary = summarize_gemini(transcript)
            if transcript:
                tpath = tmpd / (local_path.stem + ".txt")
                tpath.write_text(transcript, encoding="utf-8")
                rclone_copy_file(tpath, RCLONE_FOLDER_TRANSCRIPTS)
            if summary:
                spath = tmpd / (local_path.stem + "_summary.txt")
                spath.write_text(summary, encoding="utf-8")
                rclone_copy_file(spath, RCLONE_FOLDER_TRANSCRIPTS)
            _send(chat_id, "Done.")
    except Exception as e:
        _send(chat_id, f"Error: {e}")

def pipeline_upload_and_cleanup(local_path: Path, chat_id: int):
    _send(chat_id, f"Uploading: {local_path.name}")
    ok, msg = rclone_copy_file(local_path, RCLONE_FOLDER_VIDEOS)
    if not ok:
        _send(chat_id, "Upload failed.")
        _send(chat_id, f"rclone: {msg[:1500]}")
        return
    try: local_path.unlink(missing_ok=True)
    except Exception: pass
    _send(chat_id, "Upload complete and local file removed.")

# -------- inline browser --------
TOKENS: dict[str, dict] = {}

def _make_token(payload: dict) -> str:
    t = uuid.uuid4().hex[:12]
    TOKENS[t] = payload
    return t

def _resolve(t: str) -> dict | None:
    return TOKENS.get(t)

def _list_keyboard(path: str, items: list[dict], offset: int = 0):
    kb = types.InlineKeyboardMarkup()
    PAGE = 20
    view = items[offset:offset + PAGE]
    for it in view:
        name = it.get("Name", "")
        isdir = it.get("IsDir", False)
        full = path.rstrip("/") + "/" + name if not path.endswith(":") else path + name
        if isdir:
            tok = _make_token({"op": "nav", "path": full, "offset": 0})
            kb.add(types.InlineKeyboardButton(f"[DIR] {name}", callback_data=f"NAV:{tok}"))
        else:
            tok = _make_token({"op": "proc", "file": full})
            kb.add(types.InlineKeyboardButton(f"[FILE] {name}", callback_data=f"PROC:{tok}"))
    if offset > 0 or (offset + PAGE) < len(items):
        row = []
        if offset > 0:
            tokp = _make_token({"op": "page", "path": path, "offset": max(0, offset - PAGE)})
            row.append(types.InlineKeyboardButton("[Prev]", callback_data=f"PAGE:{tokp}"))
        if (offset + PAGE) < len(items):
            tokn = _make_token({"op": "page", "path": path, "offset": offset + PAGE})
            row.append(types.InlineKeyboardButton("[Next]", callback_data=f"PAGE:{tokn}"))
        if row: kb.row(*row)
    if "/" in path and not path.endswith(":"):
        parent = path.rsplit("/", 1)[0]
        tokup = _make_token({"op": "nav", "path": parent, "offset": 0})
        kb.add(types.InlineKeyboardButton("[Up]", callback_data=f"NAV:{tokup}"))
    return kb

def send_listing(chat_id: int, path: str, message_id: int | None = None, offset: int = 0, edit: bool = False):
    ok, res = rclone_lsjson(path)
    if not ok:
        _send(chat_id, f"rclone lsjson error:\n{res}")
        return
    items: list[dict] = list(res)  # type: ignore
    title = f"Listing: {rclone_path(path)}\nSelect folder or file."
    kb = _list_keyboard(rclone_path(path), items, offset=offset)
    if edit and message_id:
        bot.edit_message_text(title, chat_id, message_id, reply_markup=kb, parse_mode=None)
    else:
        bot.send_message(chat_id, title, reply_markup=kb)

# -------- handlers --------
@bot.message_handler(commands=["help", "start"])
def on_help(m: telebot.types.Message): bot.reply_to(m, HELP_TEXT)

@bot.message_handler(commands=["ls"])
def on_ls(m: telebot.types.Message):
    chat_id = m.chat.id
    arg = m.text.strip().split(" ", 1)
    path = arg[1].strip() if len(arg) > 1 and arg[1].strip() else f"{RCLONE_REMOTE}:"
    send_listing(chat_id, path)

@bot.message_handler(func=lambda m: isinstance(m.text, str) and m.text.lower().startswith("smr"))
def on_smr_trigger(m: telebot.types.Message):
    chat_id = m.chat.id
    parts = m.text.strip().split(" ", 1)
    path = parts[1].strip() if len(parts) > 1 and parts[1].strip() else f"{RCLONE_REMOTE}:"
    send_listing(chat_id, path)

@bot.callback_query_handler(func=lambda c: True)
def on_cbq(c: telebot.types.CallbackQuery):
    chat_id = c.message.chat.id
    msgid = c.message.message_id
    data = c.data or ""
    try:
        kind, tok = data.split(":", 1)
    except ValueError:
        bot.answer_callback_query(c.id); return
    payload = _resolve(tok) or {}
    op = payload.get("op")

    if kind == "NAV" and op == "nav":
        send_listing(chat_id, payload.get("path", f"{RCLONE_REMOTE}:"), message_id=msgid, offset=0, edit=True)
        bot.answer_callback_query(c.id); return

    if kind == "PAGE" and op == "page":
        send_listing(chat_id, payload.get("path", f"{RCLONE_REMOTE}:"), message_id=msgid,
                     offset=int(payload.get("offset", 0)), edit=True)
        bot.answer_callback_query(c.id); return

    if kind == "PROC" and op == "proc":
        remote_file = payload.get("file")
        bot.answer_callback_query(c.id, show_alert=False, text="Processing started")
        threading.Thread(target=process_remote_file_async, args=(chat_id, remote_file), daemon=True).start()
        return

    bot.answer_callback_query(c.id)

@bot.message_handler(commands=["textlink", "normal"])
def on_textlink(m: telebot.types.Message):
    chat_id = m.chat.id
    parts = m.text.strip().split(" ", 1)
    if len(parts) < 2:
        _send(chat_id, "Usage: /textlink <url>\n       /normal <url>")
        return
    link = parts[1].strip()
    _send(chat_id, "Working...")

    direct = link
    if m.text.startswith("/normal"):
        if is_onedrive_url(link):
            fixed = to_direct_download(link)
            if not fixed:
                _send(chat_id, "Could not convert share link to direct download.")
                return
            direct = fixed

    local = download_via_requests(direct, chat_id)
    if not local: return
    _send(chat_id, f"Downloaded to: {local.name}")
    pipeline_upload_and_cleanup(local, chat_id)

@bot.message_handler(func=lambda m: True, content_types=["text"])
def on_text(m: telebot.types.Message):
    bot.reply_to(m, HELP_TEXT)

# -------- main --------
if __name__ == "__main__":
    log.info("Bot running (inline browser + 'smr' trigger).")
    bot.infinity_polling(timeout=30, long_polling_timeout=30)
PY

sudo chown "${OWNER_USER}:${OWNER_GROUP}" "${BOT_FILE}"
sudo chmod 644 "${BOT_FILE}"

# ----- 6) systemd service (WorkingDirectory + env) -----
echo "=== Creating systemd service ==="
SERVICE_FILE="/etc/systemd/system/youtube_bot.service"
sudo bash -c "cat > '${SERVICE_FILE}'" <<EOF
[Unit]
Description=YouTube Recorder Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
ExecStart=${VENV_DIR}/bin/python ${BOT_FILE}
Restart=on-failure
RestartSec=5
User=${OWNER_USER}
Group=${OWNER_GROUP}
Environment=BOT_TOKEN=${BOT_TOKEN}
Environment=RCLONE_REMOTE=${RCLONE_REMOTE}
Environment=RCLONE_FOLDER_VIDEOS=${RCLONE_FOLDER_VIDEOS}
Environment=RCLONE_FOLDER_TRANSCRIPTS=${RCLONE_FOLDER_TRANSCRIPTS}
Environment=WHISPER_MODEL=${WHISPER_MODEL}
Environment=WHISPER_DEVICE=${WHISPER_DEVICE}
Environment=BOT_HOME=${INSTALL_DIR}
Environment=GEMINI_API_KEY=${GEMINI_API_KEY}
Environment=GEMINI_MODEL=${GEMINI_MODEL}

[Install]
WantedBy=multi-user.target
EOF

# ----- 7) enable & start -----
echo "=== Enabling and starting service ==="
sudo systemctl daemon-reload
sudo systemctl enable --now youtube_bot
sudo systemctl status youtube_bot --no-pager || true

echo "=== Done. Tips ==="
echo "- Logs: journalctl -u youtube_bot -f"
echo "- Telegram: 'smr' to browse OneDrive; click [FILE] to process."
echo "- If rclone remote isn't configured yet, run: rclone config"
