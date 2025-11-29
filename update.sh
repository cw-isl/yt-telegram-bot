#!/usr/bin/env bash
set -euo pipefail

# 기본 설정
BASE_DIR="/root/rcbot"
BACKUP_PATH="/root/rcbotbak.tar.gz"
SERVICE_NAME="yt-telegram-bot.service"  # 실제 서비스 이름으로 변경하세요.

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1"
}

stop_service() {
  if systemctl list-units --type=service --all | grep -q "${SERVICE_NAME}"; then
    log "서비스 중지: ${SERVICE_NAME}"
    systemctl stop "${SERVICE_NAME}"
  else
    log "서비스(${SERVICE_NAME})가 존재하지 않아 중지 단계를 건너뜁니다."
  fi
}

backup_repo() {
  log "백업 생성: ${BACKUP_PATH}"
  tar -czf "${BACKUP_PATH}" -C "$(dirname "${BASE_DIR}")" "$(basename "${BASE_DIR}")"
}

update_repo() {
  log "깃 저장소 업데이트"
  git -C "${BASE_DIR}" fetch origin main
  git -C "${BASE_DIR}" reset --hard origin/main
}

reboot_host() {
  log "시스템 재부팅"
  reboot
}

main() {
  stop_service
  backup_repo
  update_repo
  reboot_host
}

main "$@"
