#!/usr/bin/env bash
# 정본 → 이 저장소 동기화 → 커밋 → 푸시. 사용자 PC의 「업데이트 설치」가 이 커밋을 받는다.
#   tools/publish.sh "바뀐 내용 한 줄"
set -euo pipefail
cd "$(dirname "$0")/.."
tools/sync.sh
git add -A
if git diff --cached --quiet; then echo "바뀐 것 없음"; exit 0; fi
git commit -q -m "${1:-flowapp 업데이트}"
git push -q origin main
echo "✓ 푸시 $(git rev-parse --short HEAD)"
