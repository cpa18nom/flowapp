#!/usr/bin/env bash
# 데이터 묶음 flowapp-data.zip — 새로 설치한 사람이 처음 한 번 받는다(런처가 MARKET_HOME 에 푼다).
#   tools/make_data.sh [출력폴더]      · 올리기: gh release upload data <zip> --clobber -R cpa18nom/flowapp
# 넣는 것: raw 의 시세·수급·5분봉·거래대금·시총·메타 + 화면이 쓰는 학습 결과 몇 개. 백업·연구 캐시·지표 패널(각자 계산)은 뺀다.
set -euo pipefail
SRC="${MARKET_SRC:-$HOME/market}"
OUT="${1:-/mnt/c/Users/user/flowapp-build}"
mkdir -p "$OUT"
python3 - "$SRC" "$OUT/flowapp-data.zip" <<'PY'
import os, sys, zipfile
src, dst = sys.argv[1], sys.argv[2]
dirs = ["raw/price", "raw/flow", "raw/meta", "raw/marcap", "raw/min5", "raw/amount", "raw/_daily"]
files = ["scan/cache/winmodel_final.pkl", "scan/cache/strategy_events.pkl", "scan/cache/strategy_paths.npz",
         "scan/cache/osc60_scan.json"]
tmp, n = dst + ".part", 0
with zipfile.ZipFile(tmp, "w") as z:
    paths = [f for f in files if os.path.exists(os.path.join(src, f))]
    for d in dirs:
        for r, _, fs in os.walk(os.path.join(src, d)):
            paths += [os.path.relpath(os.path.join(r, f), src) for f in fs if not f.endswith((".tmp", ".part", ".lock"))]
    for rel in sorted(paths):
        packed = rel.endswith((".gz", ".parquet", ".pkl", ".npz"))
        z.write(os.path.join(src, rel), rel.replace(os.sep, "/"),
                compress_type=zipfile.ZIP_STORED if packed else zipfile.ZIP_DEFLATED)
        n += 1
os.replace(tmp, dst)
print(f"✓ {dst} · {n:,}개 파일 · {os.path.getsize(dst)/1e6:,.0f}MB")
PY
