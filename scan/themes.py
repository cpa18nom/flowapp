"""테마 — 검색된 종목이 어떤 테마에 속하는지 (사용자 요청 2026-10-02).

원천은 **네이버 증권 테마**(m.stock.naver.com/api/stocks/theme). 실측 2026-10-02:
  네이버  264테마 · 유니버스 2,556종목 중 2,298종목(90%) · 종목당 중앙값 2개 · 전부 받는 데 1초
  키움    ka90001/ka90002 142테마 · 638종목(25%) — 에코프로비엠·한화에어로스페이스가 테마 없음. 그래서 안 쓴다.
옛 주소 finance.naver.com/sise/theme.naver 는 stock.naver.com 으로 302 된다(HTML 긁기 불가) → JSON API 를 쓴다.

★ 테마 구성은 **받은 시점의 분류**다. 과거 어느 날의 구성이 아니므로 검정(backtest)에 넣으면 미래참조가 된다 —
  화면 표시에만 쓴다.

    python themes.py            받아서 raw/meta/themes.json 에 저장 (「최신 데이터 받기」도 같이 받는다)
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import core

FILE = Path(os.environ.get("FLOWAPP_THEMES_FILE", core.META / "themes.json"))
API = "https://m.stock.naver.com/api/stocks/theme"
PAGE = 100                 # API 가 한 번에 주는 최대 행 수
MIN_THEMES = 150           # 이보다 적게 오면 고장으로 보고 옛 파일을 지킨다(평소 260개 안팎)
MIN_FILLED = 0.95          # 목록이 말한 종목 수 대비 실제로 받은 비율
_LOCK = threading.Lock()
_CACHE: dict = {"mtime": None, "data": None}


def _get(url: str, tries: int = 3) -> dict:
    """한 번 받는다. 연결 오류는 잠깐 쉬고 다시(실측: 270회 중 TLS 핸드셰이크 시간초과가 한 번씩 난다)."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError:
            raise                                   # 400·404 는 다시 해도 같다
        except OSError:
            if i == tries - 1:
                raise
            time.sleep(1 + i)


def _pages(url: str, key: str, idkey: str, tries: int = 5) -> list[dict]:
    """여러 쪽을 이어 받는다. ★ 목록은 **등락률 순**이라 장중에는 쪽을 받는 사이에 순서가 바뀐다 —
    한 행이 두 쪽에 나오면 다른 한 행은 빠진 것이다(실측 2026-10-02 15시: 264행 중 고유 262). 그래서 겹친 행이
    없는 회차가 나올 때까지 다시 받고, 그동안 본 것은 합친다."""
    seen: dict = {}
    for _ in range(tries):
        rows, p = [], 1
        while True:
            got = _get(f"{url}?page={p}&pageSize={PAGE}").get(key) or []
            rows += got
            if len(got) < PAGE:
                break
            p += 1
        seen.update((r[idkey], r) for r in rows)
        if len({r[idkey] for r in rows}) == len(rows):
            break
    return list(seen.values())


def fetch() -> list[dict]:
    """네이버 테마 전부 → [{"no", "name", "codes"}]. 덜 받았으면 ValueError(옛 파일을 덮지 않게)."""
    groups = _pages(API, "groups", "no")
    if len(groups) < MIN_THEMES:
        raise ValueError(f"테마가 {len(groups)}개뿐입니다(평소 {MIN_THEMES}개 이상)")
    with ThreadPoolExecutor(8) as ex:
        members = list(ex.map(lambda g: _pages(f"{API}/{g['no']}", "stocks", "itemCode"), groups))
    want, got = sum(int(g["totalCount"]) for g in groups), sum(len(m) for m in members)
    if got < want * MIN_FILLED:
        raise ValueError(f"구성 종목을 {got}/{want}개만 받았습니다")
    return [{"no": int(g["no"]), "name": str(g["name"]), "codes": sorted(str(s["itemCode"]) for s in m)}
            for g, m in sorted(zip(groups, members), key=lambda x: int(x[0]["no"]))]


def update(log=print) -> bool:
    """받아서 저장. 실패해도 예외를 올리지 않는다 — 테마가 없어도 검색·차트는 돌아간다."""
    try:
        themes = fetch()
    except Exception as e:                                       # noqa: BLE001
        log(f"⚠ 테마 갱신 실패({repr(e)[:80]}) → 기존 파일 사용")
        return False
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"at": datetime.now().strftime("%Y-%m-%d %H:%M"), "source": API, "themes": themes},
                              ensure_ascii=False), encoding="utf-8")
    tmp.replace(FILE)
    log(f"테마 {len(themes)}개 · 종목 {len({c for t in themes for c in t['codes']}):,}개 갱신")
    return True


def load() -> dict:
    """{"at", "name": {no: 이름}, "size": {no: 종목 수}, "by_code": {코드: [no, …]}}. 파일이 바뀔 때만 다시 읽는다.
    종목의 테마는 **좁은 테마 먼저**(구성 종목이 적은 순) — '밸류업'·'지주사' 같은 넓은 묶음이 뒤로 간다."""
    try:
        mtime = FILE.stat().st_mtime_ns
    except OSError:
        return {"at": "", "name": {}, "size": {}, "by_code": {}}
    with _LOCK:
        if _CACHE["mtime"] != mtime:
            try:
                raw = json.loads(FILE.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return {"at": "", "name": {}, "size": {}, "by_code": {}}
            name = {t["no"]: t["name"] for t in raw["themes"]}
            size = {t["no"]: len(t["codes"]) for t in raw["themes"]}
            by_code: dict[str, list[int]] = {}
            for t in raw["themes"]:
                for c in t["codes"]:
                    by_code.setdefault(c, []).append(t["no"])
            for nos in by_code.values():
                nos.sort(key=lambda n: (size[n], name[n]))
            _CACHE.update(mtime=mtime, data={"at": raw.get("at", ""), "name": name, "size": size, "by_code": by_code})
        return _CACHE["data"]


def of(code: str) -> list[tuple[int, str]]:
    """그 종목의 테마 [(no, 이름)] — 좁은 테마 먼저. 없으면 빈 목록."""
    d = load()
    return [(n, d["name"][n]) for n in d["by_code"].get(str(code), [])]


def tally(codes, least: int = 2) -> list[tuple[int, str, int]]:
    """종목 묶음에서 겹치는 테마 [(no, 이름, 그 묶음 안의 종목 수)] — **그 묶음에 몰린 테마 먼저**.
    순서 = 겹친 수 × 그 테마에서 차지하는 비율(k²/테마 크기). 겹친 수만으로 줄 세우면 '밸류업'·'지주사'처럼
    구성 종목이 100개 넘는 묶음이 늘 맨 앞에 온다(실측: 정유 3종목 중 2종목보다 밸류업 120종목 중 3종목이 앞)."""
    d = load()
    n = Counter(t for c in dict.fromkeys(map(str, codes)) for t in d["by_code"].get(c, []))
    hit = [(t, d["name"][t], k) for t, k in n.items() if k >= least]
    return sorted(hit, key=lambda x: (-x[2] * x[2] / d["size"][x[0]], -x[2], x[1]))


if __name__ == "__main__":
    sys.exit(0 if update() else 1)
