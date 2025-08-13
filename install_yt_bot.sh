#!/usr/bin/env bash
###############################################################################
# YouTube Telegram Bot – Installer (extended w/ comments & guards)
# - Pre-check questions (y/yes/n/no)
# - Package install (python3, pip, ffmpeg, yt-dlp, rclone, curl)
# - Creates BOT_HOME and related folders
# - systemd service + drop-in env file
# - Admin CLI: ytbotctl (config menu / uninstall)
# - Default ExecStart: /usr/bin/python3 /home/file/youtube_recorder_bot.py
###############################################################################
set -euo pipefail

# ------------------------------ Constants ------------------------------------
SERVICE_NAME="youtube_bot.service"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}"
DROPIN_DIR="/etc/systemd/system/${SERVICE_NAME}.d"
ENV_FILE="${DROPIN_DIR}/env.conf"

# 기본 BOT_HOME (변경 가능)
BOT_HOME_DEFAULT="/home/file"
APP_DIR="${BOT_HOME_DEFAULT}/ytbot"
VENV_DIR="${APP_DIR}/.venv"
APP_MAIN="${BOT_HOME_DEFAULT}/youtube_recorder_bot.py"   # 기존 경로 유지
CTL="/usr/local/bin/ytbotctl"

# 추후 실제 가이드 링크로 바꿔 넣으세요.
GUIDE_URL="http://mmm.com"

# 색상 출력(가독성)
C0='\033[0m'; C1='\033[1;36m'; C2='\033[1;32m'; C3='\033[1;33m'; CERR='\033[1;31m'
info(){ echo -e "${C1}[INFO]${C0} $*"; }
ok(){ echo -e "${C2}[OK]${C0}   $*"; }
warn(){ echo -e "${C3}[WARN]${C0} $*"; }
err(){ echo -e "${CERR}[ERR]${C0}  $*" >&2; }

# ------------------------------ Helpers --------------------------------------
to_lower(){ echo "$1" | tr '[:upper:]' '[:lower:]'; }

confirm(){
  # confirm "question"  (accept y/yes/n/no)
  local q="$1" a
  read -rp "$q [y/N]: " a || true
  a=$(to_lower "${a:-n}")
  [[ "$a" == "y" || "$a" == "yes" ]]
}

need_pkg(){
  # need_pkg cmd debian-package
  local cmd="$1" pkg="$2"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    info "Installing package: $pkg"
    apt-get update -y
    apt-get install -y "$pkg"
  fi
}

ensure_root(){
  if [[ $EUID -ne 0 ]]; then
    err "Please run as root (or use sudo)."
    exit 1
  fi
}

ensure_dirs(){
  mkdir -p "$BOT_HOME" "$APP_DIR" "${BOT_HOME}/recordings"
  mkdir -p "$DROPIN_DIR"
  chown -R "$(id -u):$(id -g)" "$BOT_HOME" || true
}

write_env_file(){
  cat > "$ENV_FILE" <<EOF
[Service]
Environment="BOT_TOKEN=${BOT_TOKEN}"
Environment="GEMINI_API_KEY=${GEMINI_API_KEY}"
Environment="RCLONE_REMOTE=${RCLONE_REMOTE}"
Environment="RCLONE_FOLDER_VIDEOS=${RCLONE_FOLDER_VIDEOS}"
Environment="RCLONE_FOLDER_TRANSCRIPTS=${RCLONE_FOLDER_TRANSCRIPTS}"
Environment="WHISPER_MODEL=${WHISPER_MODEL}"
Environment="WHISPER_DEVICE=${WHISPER_DEVICE}"
Environment="BOT_HOME=${BOT_HOME}"
EOF
  ok "Wrote env file: ${ENV_FILE}"
}

install_service(){
  cat > "$SERVICE_FILE" <<'EOF'
[Unit]
Description=YouTube Recorder Telegram Bot
After=network.target

[Service]
Type=simple
# NOTE: 경로는 /home/file/youtube_recorder_bot.py 로 고정 (요청 사항)
ExecStart=/usr/bin/python3 /home/file/youtube_recorder_bot.py
WorkingDirectory=/home/file
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  ok "Service file installed: ${SERVICE_FILE}"
}

create_ctl(){
  cat > "$CTL" <<'EOS'
#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="youtube_bot.service"
DROPIN_DIR="/etc/systemd/system/${SERVICE_NAME}.d"
ENV_FILE="${DROPIN_DIR}/env.conf"

C0='\033[0m'; C1='\033[1;36m'; C2='\033[1;32m'; C3='\033[1;33m'; CERR='\033[1;31m'
info(){ echo -e "${C1}[INFO]${C0} $*"; }
ok(){ echo -e "${C2}[OK]${C0}   $*"; }
warn(){ echo -e "${C3}[WARN]${C0} $*"; }
err(){ echo -e "${CERR}[ERR]${C0}  $*" >&2; }

require_root(){
  if [[ $EUID -ne 0 ]]; then
    exec sudo -E /usr/bin/env bash "$0" "$@"
  fi
}

get_val(){
  local k="$1"
  [[ -f "$ENV_FILE" ]] || { err "env.conf not found: $ENV_FILE"; exit 1; }
  awk -F'[="]' -v key="$k" '$0 ~ "Environment=\""key"=" {print $3; found=1} END{ if(found!=1) exit 1 }' "$ENV_FILE"
}

set_val(){
  local k="$1" v="$2"
  [[ -f "$ENV_FILE" ]] || { err "env.conf not found: $ENV_FILE"; exit 1; }
  sed -i "s|Environment=\"${k}=.*\"|Environment=\"${k}=${v}\"|g" "$ENV_FILE"
  systemctl daemon-reload
  systemctl restart "$SERVICE_NAME"
  ok "Updated ${k} and restarted service."
}

uninstall_all(){
  warn "This will stop & remove the service. Proceed?"
  read -rp "(y/N): " a; a=$(echo "$a" | tr '[:upper:]' '[:lower:]')
  [[ "$a" == "y" || "$a" == "yes" ]] || { echo "Canceled."; exit 0; }

  systemctl stop "$SERVICE_NAME" || true
  systemctl disable "$SERVICE_NAME" || true
  rm -f "/etc/systemd/system/${SERVICE_NAME}"
  rm -rf "/etc/systemd/system/${SERVICE_NAME}.d"
  systemctl daemon-reload
  ok "Service removed."

  read -rp "Also remove bot files under BOT_HOME? (y/N): " b; b=$(echo "$b" | tr '[:upper:]' '[:lower:]')
  if [[ "$b" == "y" || "$b" == "yes" ]]; then
    BH="$(get_val BOT_HOME || true)"
    if [[ -n "${BH:-}" && -d "$BH" ]]; then
      rm -rf "$BH/ytbot" "$BH/youtube_recorder_bot.py"
      ok "Removed files under ${BH}."
    fi
  fi
}

menu_config(){
  clear
  echo "===== Current configuration ====="
  for k in BOT_TOKEN GEMINI_API_KEY RCLONE_REMOTE RCLONE_FOLDER_VIDEOS RCLONE_FOLDER_TRANSCRIPTS WHISPER_MODEL WHISPER_DEVICE BOT_HOME; do
    v="$(get_val "$k" || echo '?')"
    if [[ "$k" == "BOT_TOKEN" || "$k" == "GEMINI_API_KEY" ]]; then
      if [[ "$v" != "?" && ${#v} -gt 8 ]]; then v="${v:0:4}****${v: -4}"; fi
    fi
    printf "  %-28s : %s\n" "$k" "$v"
  done
  cat <<MENU

Select the item to change:
  1) BOT_TOKEN
  2) GEMINI_API_KEY
  3) RCLONE_REMOTE
  4) RCLONE_FOLDER_VIDEOS
  5) RCLONE_FOLDER_TRANSCRIPTS
  6) WHISPER_MODEL
  7) WHISPER_DEVICE
  8) BOT_HOME
  9) Back
MENU
  read -rp "Enter number: " n
  case "$n" in
    1) k="BOT_TOKEN" ;;
    2) k="GEMINI_API_KEY" ;;
    3) k="RCLONE_REMOTE" ;;
    4) k="RCLONE_FOLDER_VIDEOS" ;;
    5) k="RCLONE_FOLDER_TRANSCRIPTS" ;;
    6) k="WHISPER_MODEL" ;;
    7) k="WHISPER_DEVICE" ;;
    8) k="BOT_HOME" ;;
    9) return 0 ;;
    *) echo "Invalid"; read -rp "Enter to continue..." _; return 0 ;;
  esac
  read -rp "New value for ${k}: " nv
  set_val "$k" "$nv"
  read -rp "Press Enter..." _
}

main_menu(){
  require_root
  while true; do
    clear
    echo "=== ytbot control ==="
    echo "  1) Change configuration"
    echo "  2) Delete (uninstall)"
    echo "  3) Exit"
    read -rp "Select: " sel
    case "$sel" in
      1) menu_config ;;
      2) uninstall_all; exit 0 ;;
      3) exit 0 ;;
      *) echo "Invalid"; sleep 1 ;;
    esac
  done
}

main_menu "$@"
EOS
  chmod +x "$CTL"
  ok "Installed admin tool: ${CTL}"
}

# --------------------------- Pre-check questions ------------------------------
ensure_root
echo
echo "================ Pre-install checks ================"
confirm "1) Have you created a Telegram Bot token?" \
  || { err "See: ${GUIDE_URL}"; exit 1; }
confirm "2) Have you generated the OneDrive OAuth JSON for rclone?" \
  || { err "See: ${GUIDE_URL}"; exit 1; }
confirm "3) Do you acknowledge that rclone will be used for OneDrive only?" \
  || { err "See: ${GUIDE_URL}"; exit 1; }
confirm "4) Have you created a Gemini API key?" \
  || { err "See: ${GUIDE_URL}"; exit 1; }
ok "All pre-checks passed."

# ------------------------------ Packages -------------------------------------
export DEBIAN_FRONTEND=noninteractive
need_pkg curl curl
need_pkg python3 python3
need_pkg pip python3-pip
need_pkg ffmpeg ffmpeg
need_pkg rclone rclone
need_pkg yt-dlp yt-dlp

# ------------------------------ Inputs ---------------------------------------
echo
echo "================ Basic settings ===================="
BOT_HOME="${BOT_HOME_DEFAULT}"
RCLONE_REMOTE="${RCLONE_REMOTE:-onedrive}"
RCLONE_FOLDER_VIDEOS="YouTube_Backup"
RCLONE_FOLDER_TRANSCRIPTS="YouTube_Backup/Transcripts"
WHISPER_MODEL="small"
WHISPER_DEVICE="auto"

read -rp "Enter BOT_TOKEN: " BOT_TOKEN
read -rp "Enter GEMINI_API_KEY: " GEMINI_API_KEY
read -rp "Enter BOT_HOME [${BOT_HOME_DEFAULT}]: " t; BOT_HOME="${t:-$BOT_HOME_DEFAULT}"
read -rp "Enter rclone remote name [${RCLONE_REMOTE}]: " t; RCLONE_REMOTE="${t:-$RCLONE_REMOTE}"
read -rp "Enter OneDrive videos folder [${RCLONE_FOLDER_VIDEOS}]: " t; RCLONE_FOLDER_VIDEOS="${t:-$RCLONE_FOLDER_VIDEOS}"
read -rp "Enter OneDrive transcripts folder [${RCLONE_FOLDER_TRANSCRIPTS}]: " t; RCLONE_FOLDER_TRANSCRIPTS="${t:-$RCLONE_FOLDER_TRANSCRIPTS}"

# 로컬 저장 디렉토리는 BOT_HOME/recordings 고정 사용
ensure_dirs

# ------------------------------ Python env -----------------------------------
info "Preparing Python virtual environment..."
python3 -m venv "$VENV_DIR" || true
# shellcheck disable=SC1090
. "${VENV_DIR}/bin/activate"
pip install --upgrade pip
pip install numpy requests pyTelegramBotAPI faster-whisper scikit-learn yt-dlp
ok "Python dependencies ready."

# ------------------------------ App file -------------------------------------
if [[ ! -f "$APP_MAIN" ]]; then
  warn "Bot main file not found at ${APP_MAIN}. Creating placeholder."
  cat > "$APP_MAIN" <<'PY'
print("youtube_recorder_bot.py placeholder. Replace with your bot code.")
PY
fi
chmod 644 "$APP_MAIN" || true

# ------------------------------ Service --------------------------------------
install_service
write_env_file
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"
ok "Service enabled & started."

# ------------------------------ Admin tool -----------------------------------
create_ctl

echo
echo "===================================================="
echo -e " ${C2}Install completed!${C0}"
echo " Service : ${SERVICE_NAME}"
echo " Manage  : sudo ytbotctl"
echo " Logs    : journalctl -u ${SERVICE_NAME} -f"
echo " Exec    : /usr/bin/python3 ${APP_MAIN}"
echo " Env     : ${ENV_FILE}"
echo "===================================================="
