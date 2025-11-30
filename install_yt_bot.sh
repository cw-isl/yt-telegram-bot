#!/usr/bin/env bash
set -euo pipefail

# ==============================
# All-in-one installer for youtube_recorder_bot.py
# - Pre-checks (4 questions)
# - apt/rclone
# - venv + pip install
# - env file (escaped + LANG/LC_ALL)
# - fetch bot code from GitHub Raw
# - systemd unit (uses venv python)
# - admin CLI (yt-botctl, env editor + Google Drive helper)
# ==============================

NOTICE_URL="http://mmm.com"   # TODO: replace with real guide URL

say() { printf "\n%s\n" "$*"; }
err() { printf "\n[ERROR] %s\n" "$*" >&2; }
ask_yn() {
  local q="$1"; local a
  while true; do
    read -r -p "$q [y/n]: " a || true
    a="$(echo "$a" | tr '[:upper:]' '[:lower:]')"
    case "$a" in
      y|yes) return 0 ;;
      n|no)  return 1 ;;
      *) echo "Please type y or n." ;;
    esac
  done
}
ensure_root() {
  if [ "$(id -u)" -ne 0 ]; then
    err "Run as root (sudo)."
    exit 1
  fi
}
require_cmd() { command -v "$1" >/dev/null 2>&1; }

# escape a single line for safe ENV file saving
escape_line() {
  python3 - <<'PY'
import sys
s = sys.stdin.read().rstrip('\n')
s = s.replace('\\', '\\\\').replace('"', '\\"')
print(s)
PY
}

ensure_root

# ------------------------------------------------------------------------------
# 0) Pre-checks
# ------------------------------------------------------------------------------
say "=== Pre-checks ==="
ask_yn "Did you create a Telegram bot token?" || { echo "See: $NOTICE_URL"; exit 1; }
ask_yn "Do you have your Google Drive rclone JSON ready?" || { echo "See: $NOTICE_URL"; exit 1; }
ask_yn "Do you understand that this setup uses rclone for Google Drive only?" || { echo "See: $NOTICE_URL"; exit 1; }
ask_yn "Do you already have a Gemini API key?" || { echo "See: $NOTICE_URL"; exit 1; }

# ------------------------------------------------------------------------------
# 1) Basic dependencies
# ------------------------------------------------------------------------------
say "=== Installing dependencies ==="
apt-get update -y
apt-get install -y python3 python3-pip python3-venv ffmpeg curl jq

if ! require_cmd rclone; then
  say "Installing rclone..."
  curl -fsSL https://rclone.org/install.sh | bash
fi

# ------------------------------------------------------------------------------
# 2) User / directories
# ------------------------------------------------------------------------------
read -r -p "Target user for running the bot (default: current user): " TARGET_USER || true
if [ -z "${TARGET_USER:-}" ]; then
  TARGET_USER="$(logname 2>/dev/null || echo "$SUDO_USER" || id -un)"
fi
if ! id "$TARGET_USER" >/dev/null 2>&1; then
  err "User '$TARGET_USER' does not exist."
  exit 1
fi

# Default BOT_HOME => /home/<user>/yt-bot  (사용자가 입력 안 하면 자동 지정)
BOT_HOME_DEFAULT="/home/${TARGET_USER}/yt-bot"
read -r -p "Bot home directory [default: $BOT_HOME_DEFAULT]: " BOT_HOME || true
BOT_HOME="${BOT_HOME:-$BOT_HOME_DEFAULT}"

# 필요한 폴더 자동 생성 (존재해도 OK)
mkdir -p "$BOT_HOME" \
         "$BOT_HOME/recordings" \
         "$BOT_HOME/tmp" \
         "$BOT_HOME/downloads"
chown -R "$TARGET_USER":"$TARGET_USER" "$BOT_HOME"

# App/Env/Logs
mkdir -p /opt/yt-bot /etc/yt-bot /var/log/yt-bot
touch /var/log/yt-bot/bot.log
chmod 755 /opt/yt-bot
chmod 700 /etc/yt-bot
chmod 644 /var/log/yt-bot/bot.log

# ------------------------------------------------------------------------------
# 3) Python venv + libs
# ------------------------------------------------------------------------------
say "=== Python venv & libs (inside venv) ==="
VENV_DIR="/opt/yt-bot/.venv"
VPY="$VENV_DIR/bin/python"
VPIP="$VENV_DIR/bin/pip"

if [ ! -x "$VPY" ]; then
  python3 -m venv "$VENV_DIR"
fi
"$VPIP" install --upgrade pip
"$VPIP" install pyTelegramBotAPI yt-dlp faster-whisper google-generativeai

# ------------------------------------------------------------------------------
# 4) Environment values (saved to /etc/yt-bot/yt-bot.env)
# ------------------------------------------------------------------------------
say "=== Environment ==="
ENV_FILE="/etc/yt-bot/yt-bot.env"

read -r -p "BOT_TOKEN (optional, press Enter to skip): " RAW_BOT_TOKEN || true
read -r -p "GEMINI_API_KEY (optional, Enter to skip): " RAW_GEMINI || true

# RCLONE_REMOTE is fixed to 'gdrive'
RCLONE_REMOTE="gdrive"

read -r -p "RCLONE_FOLDER_VIDEOS [default: YouTube_Backup]: " RCLONE_FOLDER_VIDEOS || true
RCLONE_FOLDER_VIDEOS="${RCLONE_FOLDER_VIDEOS:-YouTube_Backup}"
read -r -p "RCLONE_FOLDER_TRANSCRIPTS [default: YouTube_Backup/Transcripts]: " RCLONE_FOLDER_TRANSCRIPTS || true
RCLONE_FOLDER_TRANSCRIPTS="${RCLONE_FOLDER_TRANSCRIPTS:-YouTube_Backup/Transcripts}"

# escape & quote
BOT_TOKEN_ESC="$(printf "%s" "${RAW_BOT_TOKEN:-}" | escape_line)"
GEMINI_API_KEY_ESC="$(printf "%s" "${RAW_GEMINI:-}" | escape_line)"
BOT_HOME_ESC="$(printf "%s" "$BOT_HOME" | escape_line)"

cat > "$ENV_FILE" <<EOF
BOT_TOKEN="${BOT_TOKEN_ESC}"
GEMINI_API_KEY="${GEMINI_API_KEY_ESC}"
RCLONE_REMOTE="${RCLONE_REMOTE}"
RCLONE_FOLDER_VIDEOS="${RCLONE_FOLDER_VIDEOS}"
RCLONE_FOLDER_TRANSCRIPTS="${RCLONE_FOLDER_TRANSCRIPTS}"
BOT_HOME="${BOT_HOME_ESC}"
WHISPER_MODEL="small"
WHISPER_DEVICE="auto"
LANG="C.UTF-8"
LC_ALL="C.UTF-8"
EOF
chmod 600 "$ENV_FILE"

if [ -z "${RAW_BOT_TOKEN:-}" ]; then
  say "[WARN] BOT_TOKEN not set. You can configure it later via 'yt-botctl' → Settings."
else
  say "[OK] BOT_TOKEN provided."
fi

# ------------------------------------------------------------------------------
# 5) rclone note
# ------------------------------------------------------------------------------
say "=== rclone note ==="
echo "Remote name is fixed to 'gdrive' (Google Drive)."
echo "Use 'yt-botctl' → 'Install rclone & open rclone config' to install/refresh rclone," 
echo "then pick your Google Drive inside the wizard."

# ------------------------------------------------------------------------------
# 6) Fetch bot code (from GitHub Raw or local path)
# ------------------------------------------------------------------------------
say "=== Fetch bot code ==="
DEFAULT_BOT_CODE_URL="https://raw.githubusercontent.com/cw-isl/yt-telegram-bot/main/youtube_recorder_bot.py"
read -r -p "Bot code Raw URL [default: $DEFAULT_BOT_CODE_URL] (press Enter to use default, or type another URL): " BOT_CODE_URL || true
BOT_CODE_URL="${BOT_CODE_URL:-$DEFAULT_BOT_CODE_URL}"

if curl -fsSL "$BOT_CODE_URL" -o /opt/yt-bot/youtube_recorder_bot.py; then
  echo "Downloaded bot code from: $BOT_CODE_URL"
else
  err "Failed to download from the URL: $BOT_CODE_URL"
  echo "You can provide a local path instead."
  read -r -p "Local path to youtube_recorder_bot.py: " BOT_CODE_PATH
  if [ ! -f "$BOT_CODE_PATH" ]; then
    err "File not found: $BOT_CODE_PATH"
    exit 1
  fi
  install -m 644 "$BOT_CODE_PATH" /opt/yt-bot/youtube_recorder_bot.py
fi

# ------------------------------------------------------------------------------
# 7) systemd unit (use venv python)
# ------------------------------------------------------------------------------
say "=== systemd unit ==="
UNIT=/etc/systemd/system/youtube_bot.service
cat > "$UNIT" <<EOF
[Unit]
Description=YouTube Recorder Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
User=$TARGET_USER
Group=$TARGET_USER
WorkingDirectory=$BOT_HOME
EnvironmentFile=$ENV_FILE
ExecStart=$VENV_DIR/bin/python /opt/yt-bot/youtube_recorder_bot.py
Restart=always
RestartSec=5
StandardOutput=append:/var/log/yt-bot/bot.log
StandardError=append:/var/log/yt-bot/bot.log

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload

# ▶ If BOT_TOKEN empty: install but do not start
if [ -z "${RAW_BOT_TOKEN:-}" ]; then
  say "[INFO] BOT_TOKEN is empty → service will be installed but NOT started."
  systemctl enable youtube_bot
  say "Later, run:  yt-botctl   → Settings → set BOT_TOKEN  → Restart"
else
  systemctl enable --now youtube_bot
fi

# ------------------------------------------------------------------------------
# 8) Admin CLI (yt-botctl) — env editor + Google Drive helper
# ------------------------------------------------------------------------------
say "=== Create admin CLI (yt-botctl) ==="
cat > /usr/local/bin/yt-botctl <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

ENV_DIR="/etc/yt-bot"
ENV_FILE="$ENV_DIR/yt-bot.env"
UNIT="youtube_bot.service"
FIXED_REMOTE="gdrive"

ensure_env_file() {
  sudo mkdir -p "$ENV_DIR"
  if [ ! -f "$ENV_FILE" ]; then
    sudo tee "$ENV_FILE" >/dev/null <<'EOT'
# Telegram bot token (optional – set later via yt-botctl)
BOT_TOKEN=""

# Gemini API key (optional – set later via yt-botctl)
GEMINI_API_KEY=""

# Rclone settings (remote name is fixed to 'gdrive')
RCLONE_REMOTE="gdrive"
RCLONE_FOLDER_VIDEOS="YouTube_Backup"
RCLONE_FOLDER_TRANSCRIPTS="YouTube_Backup/Transcripts"

# Bot home directory
BOT_HOME="/home/REPLACE_ME"

# Whisper settings
WHISPER_MODEL="small"
WHISPER_DEVICE="auto"

LANG="C.UTF-8"
LC_ALL="C.UTF-8"
EOT
    sudo chmod 600 "$ENV_FILE"
  fi
}

mask() {
  local v="${1:-}"
  if [ -z "$v" ]; then echo ""; return; fi
  local len=${#v}
  if [ $len -le 6 ]; then echo "******"; else echo "${v:0:3}****${v: -3}"; fi
}

getv() {
  local key="$1"
  ( set -a; . "$ENV_FILE"; set +a; eval "printf '%s' \"\${$key-}\"" )
}

svc_user() { systemctl show "$UNIT" -p User --value 2>/dev/null || id -un; }
svc_home() { getent passwd "$(svc_user)" | cut -d: -f6; }
rclone_conf_path() { local home; home="$(svc_home)"; echo "$home/.config/rclone/rclone.conf"; }

# --- rclone 설치 + 설정 마법사 열기 (메뉴 9번) ---
rclone_install_and_config() {
  echo "Installing/refreshing rclone..."
  curl -fsSL https://rclone.org/install.sh | sudo bash

  local user home conf dir
  user="$(svc_user)"; home="$(svc_home)"
  conf="$(rclone_conf_path)"; dir="$(dirname "$conf")"

  sudo -u "$user" mkdir -p "$dir"
  # gdrive 섹션이 없으면 최소 스텁 생성
  if ! grep -q "^\[${FIXED_REMOTE}\]" "$conf" 2>/dev/null; then
    echo "Creating minimal gdrive remote stub at $conf"
    sudo tee -a "$conf" >/dev/null <<EOT
[${FIXED_REMOTE}]
type = drive
EOT
    sudo chown "$user":"$user" "$conf"
    sudo chmod 600 "$conf"
  fi

  echo
  echo "Launching rclone wizard. First, trying 'rclone config reconnect ${FIXED_REMOTE}:' ..."
  if sudo -u "$user" rclone config reconnect "${FIXED_REMOTE}:"; then
    echo "Reconnect finished."
  else
    echo "Reconnect not available or failed. Opening full 'rclone config' UI."
    sudo -u "$user" rclone config
  fi

  echo
  echo "Testing: rclone about ${FIXED_REMOTE}:"
  if sudo -u "$user" rclone about "${FIXED_REMOTE}:"; then
    echo "OK."
  else
    echo "WARN: 'about' failed. You may need to re-run 'rclone config'."
  fi
}

# ---------- Base64 안전 저장: 원자적 replace-or-append ----------
setv_b64() {
  local key="$1" val_b64="$2"
  sudo python3 - "$ENV_FILE" "$key" "$val_b64" <<'PY'
import sys,base64,os,tempfile
env_path,key,val_b64 = sys.argv[1], sys.argv[2], sys.argv[3]
val = base64.b64decode(val_b64.encode()).decode()

lines=[]
if os.path.exists(env_path):
    with open(env_path,'r',encoding='utf-8',errors='ignore') as f:
        lines=f.readlines()

found=False
out=[]
for ln in lines:
    if ln.startswith(key+"="):
        out.append(f'{key}="{val}"\n')
        found=True
    else:
        out.append(ln)
if not found:
    out.append(f'{key}="{val}"\n')

os.makedirs(os.path.dirname(env_path), exist_ok=True)
fd,tmp = tempfile.mkstemp(dir=os.path.dirname(env_path))
with os.fdopen(fd,'w',encoding='utf-8') as f:
    f.writelines(out)
os.replace(tmp, env_path)
os.chmod(env_path, 0o600)
PY
}

# --- (선택) Google Drive 드라이브 확인 도구 ---
select_gdrive_drive() {
  local user
  user="$(svc_user)"

  echo "rclone backend drives ${FIXED_REMOTE}: 로 Google Drive 드라이브 목록을 확인합니다..."
  if sudo -u "$user" rclone backend drives "${FIXED_REMOTE}:"; then
    echo "위 목록을 참고해 rclone config에서 원하는 드라이브를 선택하거나 변경하세요."
  else
    echo "드라이브 목록을 불러오지 못했습니다. rclone config를 다시 실행하세요."
  fi
}

ensure_remote_dirs() {
  local user remote folder_v folder_t
  remote="${FIXED_REMOTE}"
  folder_v="$(getv RCLONE_FOLDER_VIDEOS)"
  folder_t="$(getv RCLONE_FOLDER_TRANSCRIPTS)"
  user="$(systemctl show "$UNIT" -p User --value 2>/dev/null || id -un)"
  echo "Ensuring remote folders on ${remote}:/"
  sudo -u "$user" rclone mkdir "${remote}:/${folder_v}" || true
  sudo -u "$user" rclone mkdir "${remote}:/${folder_t}" || true
  echo "Done."
}

print_settings() {
  echo "Current settings:"
  local BT GK VF TF BH WM WD
  BT="$(getv BOT_TOKEN)"
  GK="$(getv GEMINI_API_KEY)"
  VF="$(getv RCLONE_FOLDER_VIDEOS)"
  TF="$(getv RCLONE_FOLDER_TRANSCRIPTS)"
  BH="$(getv BOT_HOME)"
  WM="$(getv WHISPER_MODEL)"
  WD="$(getv WHISPER_DEVICE)"
  printf "  %-26s = %s\n" "BOT_TOKEN"                 "$(mask "$BT")"
  printf "  %-26s = %s\n" "GEMINI_API_KEY"            "$(mask "$GK")"
  printf "  %-26s = %s\n" "RCLONE_REMOTE"             "gdrive (fixed)"
  printf "  %-26s = %s\n" "RCLONE_FOLDER_VIDEOS"      "${VF}"
  printf "  %-26s = %s\n" "RCLONE_FOLDER_TRANSCRIPTS" "${TF}"
  printf "  %-26s = %s\n" "BOT_HOME"                  "${BH}"
  printf "  %-26s = %s\n" "WHISPER_MODEL"             "${WM}"
  printf "  %-26s = %s\n" "WHISPER_DEVICE"            "${WD}"
  echo
  echo "rclone.conf: $(rclone_conf_path)"
}

show_env_all() {
  ensure_env_file
  echo
  echo "===== $ENV_FILE (raw) ====="
  sudo nl -ba "$ENV_FILE" || true
  echo "==========================="
  read -r -p "Enter to continue..." _
}

restart_service() {
  sudo systemctl daemon-reload
  sudo systemctl restart "$UNIT"
  sleep 1
}

menu_settings() {
  ensure_env_file
  while true; do
    clear
    print_settings
    cat <<'EOM'
Edit which setting?
  1) BOT_TOKEN
  2) GEMINI_API_KEY
  3) RCLONE_FOLDER_VIDEOS
  4) RCLONE_FOLDER_TRANSCRIPTS
  5) BOT_HOME
  6) WHISPER_MODEL
  7) WHISPER_DEVICE
  8) Ensure rclone remote folders
  9) Install rclone & open rclone config (recommended)
 10) Check Google Drive list (Shared Drives/My Drive)
 11) Show ENV file (raw contents)
  0) Back
EOM
    read -r -p "> " sel
    case "$sel" in
      1) key="BOT_TOKEN" ;;
      2) key="GEMINI_API_KEY" ;;
      3) key="RCLONE_FOLDER_VIDEOS" ;;
      4) key="RCLONE_FOLDER_TRANSCRIPTS" ;;
      5) key="BOT_HOME" ;;
      6) key="WHISPER_MODEL" ;;
      7) key="WHISPER_DEVICE" ;;
      8) ensure_remote_dirs; read -r -p "Enter to continue..." _; continue ;;
      9) rclone_install_and_config; read -r -p "Enter to continue..." _; continue ;;
     10) select_gdrive_drive; read -r -p "Enter to continue..." _; continue ;;
     11) show_env_all; continue ;;
      0) break ;;
      *) continue ;;
    esac
    cur="$(getv "$key")"
    echo "Current $key: ${cur}"
    read -r -p "New value for $key (leave empty to cancel): " val
    [ -z "${val}" ] && continue
    val_b64="$(printf "%s" "$val" | base64 | tr -d '\n')"
    setv_b64 "$key" "$val_b64"
    echo "Saved → $key updated."
    echo "Updated. Restarting service..."
    restart_service
  done
}

menu_delete() {
  echo "This will remove service, env, and app files."
  read -r -p "Type 'delete' to confirm: " x
  [ "$x" != "delete" ] && { echo "Canceled."; return; }
  sudo systemctl disable --now "$UNIT" || true
  sudo rm -f /etc/systemd/system/"$UNIT"
  sudo systemctl daemon-reload || true
  sudo rm -f "$ENV_FILE"
  sudo rm -rf /opt/yt-bot
  echo "Removed."
}

menu_main() {
  ensure_env_file
  while true; do
    cat <<'EOM'

yt-botctl menu:
  1) Settings (edit & save)
  2) Delete (uninstall)
  3) Status
  4) Restart
  5) Logs (follow)
  6) Exit
EOM
    read -r -p "> " sel
    case "$sel" in
      1) menu_settings ;;
      2) menu_delete ;;
      3) sudo systemctl status "$UNIT" --no-pager ;;
      4) echo "Restarting..."; restart_service; echo "Restarted." ;;
      5) sudo journalctl -u "$UNIT" -f ;;
      6) break ;;
      *) ;;
    esac
  done
}

case "${1:-}" in
  settings) menu_settings ;;
  delete)   menu_delete   ;;
  status)   sudo systemctl status "$UNIT" --no-pager ;;
  restart)  restart_service ;;
  logs)     sudo journalctl -u "$UNIT" -f ;;
  *)        menu_main ;;
esac
EOF
chmod 755 /usr/local/bin/yt-botctl

# ------------------------------------------------------------------------------
# 9) README (for reference on server)
# ------------------------------------------------------------------------------
cat > /opt/yt-bot/README.installed.md <<EOF
# YouTube/Live Recorder Telegram Bot — Installed

## Telegram usage
- Send any video/YouTube URL: download & upload only (no transcript/summary).
- Live URL: starts recording; send \`/stop\` to finish and upload.
- \`smr [path]\`: browse Google Drive → select a file → transcribe (Whisper) + summarize (Gemini, Korean) → upload.

## Manage
- \`yt-botctl\`             : menu
- \`yt-botctl settings\`    : edit env values / install rclone / configure Google Drive
- \`yt-botctl status\`      : systemd status
- \`yt-botctl logs\`        : follow logs
- \`yt-botctl delete\`      : uninstall

## Files/Dirs
- Code      : /opt/yt-bot/youtube_recorder_bot.py
- Env       : /etc/yt-bot/yt-bot.env
- Logs      : /var/log/yt-bot/bot.log
- Work home : '"$BOT_HOME"' (recordings/, tmp/, downloads/)
- rclone    : ~<service-user>/.config/rclone/rclone.conf  (remote name: gdrive)
EOF

# ------------------------------------------------------------------------------
# 10) Post-checks
# ------------------------------------------------------------------------------
say "=== Post-checks ==="
systemctl status youtube_bot --no-pager || true
echo
echo "Recent logs:"
journalctl -u youtube_bot -n 50 --no-pager || true

say "Installation complete."
if [ -z "${RAW_BOT_TOKEN:-}" ]; then
  echo "Service installed but not started (BOT_TOKEN missing)."
  echo "Run 'yt-botctl' → Settings to set BOT_TOKEN, then choose 'Restart'."
else
  echo "Service enabled and started."
fi
echo "Manage with: yt-botctl"
