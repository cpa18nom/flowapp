#!/usr/bin/env bash
# 정본(~/market 의 scan·bin)에서 배포에 들어갈 파일만 이 저장소로 복사한다. 데이터·캐시·테스트·연구 스크립트는 넣지 않는다.
set -euo pipefail
SRC="${MARKET_SRC:-$HOME/market}"
DST="$(cd "$(dirname "$0")/.." && pwd)"
rm -rf "$DST/scan" "$DST/bin"
mkdir -p "$DST/scan/static" "$DST/bin"
for f in "$SRC"/scan/*.py; do cp "$f" "$DST/scan/"; done
cp "$SRC"/scan/static/* "$DST/scan/static/"
for f in "$SRC"/bin/*.py; do case "$(basename "$f")" in test_*) ;; *) cp "$f" "$DST/bin/";; esac; done
echo "sync: scan $(ls "$DST/scan"/*.py | wc -l)개 · bin $(ls "$DST/bin"/*.py | wc -l)개"
