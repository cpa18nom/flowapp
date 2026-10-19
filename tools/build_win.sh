#!/usr/bin/env bash
# 윈도우 배포 압축본 만들기: 파이썬(설치 불필요) + 라이브러리 + 코드 → flowapp-win.zip
#   tools/build_win.sh [출력폴더]
# 빌드 폴더는 윈도우 디스크(C:\Users\user\flowapp-build) — 거기서 바로 윈도우로 시험 실행한다.
set -euo pipefail
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PY_TAG=20260901
PY_FILE="cpython-3.12.14+${PY_TAG}-x86_64-pc-windows-msvc-install_only.tar.gz"
PY_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PY_TAG}/${PY_FILE/+/%2B}"
CACHE="$HOME/.cache/flowapp-build"
BUILD="${FLOWAPP_BUILD_DIR:-/mnt/c/Users/user/flowapp-build}"
OUT="${1:-$BUILD}"
B="$BUILD/flowapp"

mkdir -p "$CACHE" "$OUT"
if [ ! -f "$CACHE/$PY_FILE" ]; then
  curl -fL -o "$CACHE/$PY_FILE.part" "$PY_URL" && mv "$CACHE/$PY_FILE.part" "$CACHE/$PY_FILE"
fi
want=$(curl -fsL "https://github.com/astral-sh/python-build-standalone/releases/download/${PY_TAG}/SHA256SUMS" | awk -v f="$PY_FILE" '$2==f{print $1}')
got=$(sha256sum "$CACHE/$PY_FILE" | cut -d' ' -f1)
[ -n "$want" ] && [ "$want" = "$got" ] || { echo "✗ 파이썬 압축본 해시 불일치"; exit 1; }

rm -rf "$B" && mkdir -p "$B"
tar xzf "$CACHE/$PY_FILE" -C "$B" && mv "$B/python" "$B/runtime"
uv pip install -q --target "$B/runtime/Lib/site-packages" \
  --python-platform x86_64-pc-windows-msvc --python-version 3.12 -r "$REPO_DIR/requirements.txt"
mkdir -p "$B/app"
git -C "$REPO_DIR" archive HEAD | tar x -C "$B/app"
sha=$(git -C "$REPO_DIR" rev-parse HEAD); day=$(git -C "$REPO_DIR" log -1 --format=%cs)
printf '{"sha": "%s", "date": "%s"}' "$sha" "$day" > "$B/app/VERSION.json"
cp "$REPO_DIR/win/flowapp 실행.bat" "$B/"
cp "$REPO_DIR/win/사용법.txt" "$B/"
python3 - "$B" "$OUT/flowapp-win.zip" <<'PY'
import os, sys, zipfile
src, dst = sys.argv[1], sys.argv[2]
tmp = dst + ".part"
with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
    for d, _, fs in os.walk(src):
        for f in fs:
            p = os.path.join(d, f)
            z.write(p, os.path.join("flowapp", os.path.relpath(p, src)))
os.replace(tmp, dst)
print(f"✓ {dst} {os.path.getsize(dst)/1e6:,.0f}MB")
PY
