#!/usr/bin/env bash
set -euo pipefail

# 기본 설정
BASE_DIR="/root/rcbot"
BRANCH="main"
COMMIT_MSG="Backup from $(hostname) at $(date '+%Y-%m-%d %H:%M:%S')"

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1"
}

push_backup() {
  log "변경사항 추가"
  git -C "${BASE_DIR}" add -A

  log "커밋 생성"
  git -C "${BASE_DIR}" commit -m "${COMMIT_MSG}" || log "새로운 변경사항이 없어 커밋을 생략합니다."

  log "메인 브랜치에 강제 푸시"
  git -C "${BASE_DIR}" push origin "HEAD:${BRANCH}" --force
}

push_backup "$@"
