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
# - admin CLI (yt-botctl)
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
require_cmd() {
  command -v "$1" >/dev/null 2>&1
}

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
ask_yn "Do you have your OneDrive rclone JSON ready?" || { echo "See: $NOTICE_URL"; exit 1; }
ask_yn "Do you understand that this setup uses rclone for OneDrive only?" || { echo "See: $NOTICE_URL"; exit 1; }
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

BOT_HOME_DEFAULT="/home/${TARGET_USER}"
read -r -p "Bot home directory [default: $BOT_HOME_DEFAULT]: " BOT_HOME || true
BOT_HOME="${BOT_HOME:-$BOT_HOME_DEFAULT}"
mkdir -p "$BOT_HOME" "$BOT_HOME/recordings"
chown -R "$TARGET_USER":"$TARGET_USER" "$BOT_HOME"

# App/Env/Logs
mkdir -p /opt/yt-bot /etc/yt-bot /var/log/yt-bot
touch /var/log/yt-bot/bot.log
chmod 755 /opt/yt-bot
chmod 700 /etc/yt-bot
chmod 644 /var/log/yt-bot/bot.log

# ------------------------------------------------------------------------------
# 3) Python venv + libs  (PATCH: venv 도입)
# ------------------------------------------------------------------------------
say "=== Python venv & libs (inside venv) ==="
VENV_DIR="/opt/yt-bot/.venv"
VPY="$VENV_DIR/bin/python"
VPIP="$VENV_DIR/bin/pip"

if [ ! -x "$VPY" ]; then
  python3 -m venv "$VENV_DIR"
fi
"$VPIP" install --upgrade pip
# 최소 의존성 설치 (requirements.txt가 없다면)
"$VPIP" install pyTelegramBotAPI yt-dlp faster-whisper google-generativeai

# ------------------------------------------------------------------------------
# 4) Environment values (saved to /etc/yt-bot/yt-bot.env)  (PATCH: 이스케이프 + LANG)
# ------------------------------------------------------------------------------
say "=== Environment ==="
ENV_FILE="/etc/yt-bot/yt-bot.env"

read -r -p "BOT_TOKEN: " RAW_BOT_TOKEN
if [ -z "$RAW_BOT_TOKEN" ]; then err "BOT_TOKEN is required."; exit 1; fi
read -r -p "GEMINI_API_KEY (optional, Enter to skip): " RAW_GEMINI || true
read -r -p "RCLONE_REMOTE [default: onedrive]: " RCLONE_REMOTE || true
RCLONE_REMOTE="${RCLONE_REMOTE:-onedrive}"
read -r -p "RCLONE_FOLDER_VIDEOS [default: YouTube_Backup]: " RCLONE_FOLDER_VIDEOS || true
RCLONE_FOLDER_VIDEOS="${RCLONE_FOLDER_VIDEOS:-YouTube_Backup}"
read -r -p "RCLONE_FOLDER_TRANSCRIPTS [default: YouTube_Backup/Transcripts]: " RCLONE_FOLDER_TRANSCRIPTS || true
RCLONE_FOLDER_TRANSCRIPTS="${RCLONE_FOLDER_TRANSCRIPTS:-YouTube_Backup/Transcripts}"

# escape & quote
BOT_TOKEN="$(printf "%s" "$RAW_BOT_TOKEN" | escape_line)"
GEMINI_API_KEY="$(printf "%s" "${RAW_GEMINI:-}" | escape_line)"
BOT_HOME_ESC="$(printf "%s" "$BOT_HOME" | escape_line)"

cat > "$ENV_FILE" <<EOF
BOT_TOKEN="${BOT_TOKEN}"
GEMINI_API_KEY="${GEMINI_API_KEY}"
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

# ------------------------------------------------------------------------------
# 5) rclone remote check
# ------------------------------------------------------------------------------
say "=== rclone remote check ==="
if ! rclone listremotes | grep -q "^${RCLONE_REMOTE}:"; then
  echo "Remote '$RCLONE_REMOTE' not found in rclone config."
  echo "Open a new terminal and run: rclone config"
  echo "After creating the remote, press Enter to continue."
  read -r
fi

say "=== Ensure remote folders ==="
rclone mkdir "${RCLONE_REMOTE}:/${RCLONE_FOLDER_VIDEOS}" || true
rclone mkdir "${RCLONE_REMOTE}:/${RCLONE_FOLDER_TRANSCRIPTS}" || true

# ------------------------------------------------------------------------------
# 6) Fetch bot code (from GitHub Raw or local path)
# ------------------------------------------------------------------------------
say "=== Fetch bot code ==="
DEFAULT_BOT_CODE_URL="https://raw.githubusercontent.com/<YOUR_GH_USER>/<YOUR_REPO>/main/youtube_recorder_bot.py"
read -r -p "Bot code Raw URL [default: $DEFAULT_BOT_CODE_URL] (leave blank to provide a local path): " BOT_CODE_URL || true

if [ -n "${BOT_CODE_URL:-}" ]; then
  if curl -fsSL "$BOT_CODE_URL" -o /opt/yt-bot/youtube_recorder_bot.py; then
    echo "Downloaded bot code from: $BOT_CODE_URL"
  else
    err "Failed to download from the URL."
    exit 1
  fi
else
  read -r -p "Local path to youtube_recorder_bot.py: " BOT_CODE_PATH
  if [ ! -f "$BOT_CODE_PATH" ]; then
    err "File not found: $BOT_CODE_PATH"
    exit 1
  fi
  install -m 644 "$BOT_CODE_PATH" /opt/yt-bot/youtube_recorder_bot.py
fi

# ------------------------------------------------------------------------------
# 7) systemd unit  (PATCH: ExecStart에 venv 파이썬 사용)
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
systemctl enable --now youtube_bot

# ------------------------------------------------------------------------------
# 8) Admin CLI (yt-botctl)  (원본 유지)
# ------------------------------------------------------------------------------
say "=== Create admin CLI (yt-botctl) ==="
cat > /usr/local/bin/yt-botctl <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="/etc/yt-bot/yt-bot.env"
UNIT="youtube_bot.service"

mask() {
  local v="$1"
  if [ -z "$v" ]; then echo ""; return; fi
  local len=${#v}
  if [ $len -le 6 ]; then echo "******"; else echo "${v:0:3}****${v: -3}"; fi
}

print_settings() {
  echo "Current settings:"
  if [ ! -f "$ENV_FILE" ]; then echo "Env file not found: $ENV_FILE"; return; fi
  grep -E '^(BOT_TOKEN|GEMINI_API_KEY|RCLONE_REMOTE|RCLONE_FOLDER_VIDEOS|RCLONE_FOLDER_TRANSCRIPTS|BOT_HOME|WHISPER_MODEL|WHISPER_DEVICE)=' "$ENV_FILE" | while read -r line; do
    key="${line%%=*}"
    val="${line#*=}"
    if [[ "$key" == "BOT_TOKEN" || "$key" == "GEMINI_API_KEY" ]]; then
      val="$(mask "$val")"
    fi
    printf "  %-26s = %s\n" "$key" "$val"
  done
}

menu_settings() {
  while true; do
    clear
    print_settings
    cat <<EOM

Edit which setting?
  1) BOT_TOKEN
  2) GEMINI_API_KEY
  3) RCLONE_REMOTE
  4) RCLONE_FOLDER_VIDEOS
  5) RCLONE_FOLDER_TRANSCRIPTS
  6) BOT_HOME
  7) WHISPER_MODEL
  8) WHISPER_DEVICE
  9) Back
EOM
    read -r -p "> " sel
    case "$sel" in
      1) key="BOT_TOKEN" ;;
      2) key="GEMINI_API_KEY" ;;
      3) key="RCLONE_REMOTE" ;;
      4) key="RCLONE_FOLDER_VIDEOS" ;;
      5) key="RCLONE_FOLDER_TRANSCRIPTS" ;;
      6) key="BOT_HOME" ;;
      7) WHISPER_MODEL ;;
      8) WHISPER_DEVICE ;;
      9) break ;;
      *) continue ;;
    esac
    read -r -p "New value for $key: " val
    if [ -n "$val" ]; then
      sudo sed -i "s|^${key}=.*|${key}=${val}|" "$ENV_FILE" || echo "${key}=${val}" | sudo tee -a "$ENV_FILE" >/dev/null
      echo "Updated."
      sudo systemctl restart "$UNIT"
      sleep 1
    fi
  done
}

menu_delete() {
  echo "This will remove service, env, and code."
  read -r -p "Type 'delete' to confirm: " x
  if [ "$x" != "delete" ]; then
    echo "Canceled."
    return
  fi
  systemctl disable --now "$UNIT" || true
  rm -f /etc/systemd/system/"$UNIT"
  systemctl daemon-reload || true
  rm -f "$ENV_FILE"
  rm -rf /opt/yt-bot
  echo "Removed."
}

menu_main() {
  while true; do
    cat <<EOM

yt-botctl menu:
  1) Settings
  2) Delete
  3) Status
  4) Restart
  5) Logs (follow)
  6) Exit
EOM
    read -r -p "> " sel
    case "$sel" in
      1) menu_settings ;;
      2) menu_delete ;;
      3) systemctl status youtube_bot --no-pager ;;
      4) systemctl restart youtube_bot ;;
      5) journalctl -u youtube_bot -f ;;
      6) break ;;
      *) ;;
    esac
  done
}

case "${1:-}" in
  settings) menu_settings ;;
  delete) menu_delete ;;
  status) systemctl status youtube_bot --no-pager ;;
  restart) systemctl restart youtube_bot ;;
  logs) journalctl -u youtube_bot -f ;;
  *) menu_main ;;
esac
EOF
chmod 755 /usr/local/bin/yt-botctl

# ------------------------------------------------------------------------------
# 9) README (for reference on server)
# ------------------------------------------------------------------------------
cat > /opt/yt-bot/README.installed.md <<'EOF'
# YouTube/Live Recorder Telegram Bot — Installed

## Telegram usage
- Send any video/YouTube URL: download & upload only (no transcript/summary).
- Live URL: starts recording; send `/stop` to finish and upload.
- `smr [path]`: browse OneDrive → select a file → transcribe (Whisper) + summarize (Gemini, Korean) → upload.

## Manage
- `yt-botctl`             : menu
- `yt-botctl settings`    : edit env values
- `yt-botctl status`      : systemd status
- `yt-botctl logs`        : follow logs
- `yt-botctl delete`      : uninstall

## Files/Dirs
- Code      : /opt/yt-bot/youtube_recorder_bot.py
- Env       : /etc/yt-bot/yt-bot.env
- Logs      : /var/log/yt-bot/bot.log
- Work home : $BOT_HOME (recordings/, temp jobs)
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
echo "Manage with: yt-botctl"
