"""오늘 봉(잠정) — 저장된 일봉 뒤에 지금 시세로 만든 오늘 봉을 붙여 검색한다 (사용자 요청 2026-10-02).

저장 자료는 18시(DATA_READY) 뒤에야 오늘을 받는다. 그 전에 검색하면 어제까지만 본다. 검색식은 전부 가격·거래량으로
만드는 신호라(수급 열을 안 쓴다), 전 종목의 지금 시가·고가·저가·현재가·거래량만 있으면 오늘 봉을 붙여 같은 신호를 낼 수 있다.

  원천      네이버 polling `…/api/realtime/domestic/stock/<코드,코드,…>` — 한 번에 300종목, 전 종목 9회·1초 안.
            막히면 키움 `ka10095`(관심종목정보, 한 번에 100종목 — 200은 1634 오류).
  실측      2026-10-02 15:57 100종목: 키움 = 네이버 시·고·저·종 100/100, 거래량 87/100(장후 시간외 반영 시차).
            ★ FDR `StockListing("KRX")` 는 쓰지 않는다 — 같은 시각 삼성전자 거래량 641만(실제 1,114만), 종가도 달랐다(1/100 일치).
  저장      **raw/price 에 쓰지 않는다.** 장중 봉이 저장되면 last+1 규칙 때문에 그대로 굳는다(2026-09-23 사고).
            앱 메모리의 패널에만 끼운다(core.merge_live). 서버를 다시 띄우거나 「최신 데이터 받기」를 하면 사라진다.
  계산      종목마다 전 이력에 오늘 봉을 붙여 core.stock_rows(패널과 같은 함수)를 돌리고 **마지막 두 행만** 가져온다
            (직전 행도 바뀐다 — 주봉 신호는 그 주의 마지막 거래일에 찍힌다. core.LIVE_TAIL).
            종목당 48ms × 2,469 = 단일 코어 2분 → 자식 프로세스에서 여러 코어로 나눈다. 앱은 2천여 행만 받는다.

    python live.py rows <snap.json> <out.pkl>     (앱이 부른다)
    python live.py                                지금 시세를 받아 요약만 출력
"""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import core  # noqa: E402

NAVER = "https://polling.finance.naver.com/api/realtime/domestic/stock/"
NAVER_BATCH = 300          # 한 번에 300종목까지 실측(0.06초)
KIWOOM_BATCH = 100         # 200 은 return_code 5 [1634]
KIWOOM_PACE = 0.25         # ka10095 유량 — 넉넉히(26회라 합쳐도 7초)
WORKERS = max(1, min(8, os.cpu_count() or 1))      # 16코어 실측: 8개 21.6초 · 12개 20.6초 — 더 늘려도 안 빨라진다. 2코어 서버는 2개
PER_STOCK = 0.048          # 종목당 계산 시간(초, 단일 코어 실측)


def estimate(n: int) -> int:
    """오늘 봉 n 종목을 만드는 데 걸릴 시간(초) 어림 — 화면 안내용. 8개로 나눠도 4배 남짓만 빨라진다(실측 21초)."""
    return max(5, round(n * PER_STOCK / min(WORKERS, 5.5) / 5) * 5)


def _int(s) -> int:
    """'+276,000' · '-1,200' · 276000 → 276000 (키움은 부호로 등락을 싣는다)."""
    t = str(s).replace(",", "").replace("+", "").replace("-", "").strip()
    return int(float(t)) if t else 0


def fetch_naver(codes: list[str]) -> tuple[pd.Timestamp | None, dict]:
    """(거래일, {코드: (시,고,저,현재가,거래량)}). 그날 체결이 없는 종목은 뺀다."""
    got = []
    for i in range(0, len(codes), NAVER_BATCH):
        req = urllib.request.Request(NAVER + ",".join(codes[i:i + NAVER_BATCH]), headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            got += json.loads(r.read().decode("utf-8")).get("datas") or []
    days = Counter(str(x.get("localTradedAt", ""))[:10] for x in got if _int(x.get("accumulatedTradingVolumeRaw", 0)) > 0)
    days.pop("", None)
    if not days:
        return None, {}
    day = max(days)                                  # 정지 종목은 옛 날짜를 들고 있다 — 가장 늦은 날이 오늘 장
    bars = {}
    for x in got:
        if str(x.get("localTradedAt", ""))[:10] != day:
            continue
        b = tuple(_int(x.get(k, 0)) for k in ("openPriceRaw", "highPriceRaw", "lowPriceRaw", "closePriceRaw",
                                              "accumulatedTradingVolumeRaw"))
        if min(b) > 0:
            bars[str(x["itemCode"])] = b
    return pd.Timestamp(day), bars


def fetch_kiwoom(codes: list[str]) -> tuple[pd.Timestamp | None, dict]:
    """키움 관심종목정보(ka10095). 네이버가 막혔을 때만 쓴다."""
    import flow                                            # noqa: PLC0415
    got = []
    for i in range(0, len(codes), KIWOOM_BATCH):
        t = time.time()
        _, j = flow.kiwoom_post("/api/dostk/stkinfo", "ka10095", {"stk_cd": "|".join(codes[i:i + KIWOOM_BATCH])})
        if j.get("return_code") != 0:
            raise RuntimeError(f"키움 ka10095: {str(j.get('return_msg'))[:60]}")
        got += j.get("atn_stk_infr") or []
        time.sleep(max(0.0, KIWOOM_PACE - (time.time() - t)))
    days = Counter(str(x.get("dt", "")) for x in got if _int(x.get("trde_qty", 0)) > 0)
    days.pop("", None)
    if not days:
        return None, {}
    day = max(days)
    bars = {}
    for x in got:
        if str(x.get("dt", "")) != day:
            continue
        b = tuple(_int(x.get(k, 0)) for k in ("open_pric", "high_pric", "low_pric", "cur_prc", "trde_qty"))
        if min(b) > 0:
            bars[str(x["stk_cd"])[:6]] = b
    return pd.Timestamp(day), bars


def fetch(codes: list[str]) -> dict:
    """{"day", "at", "src", "bars"}. 네이버 → (실패 시) 키움. 둘 다 안 되면 예외."""
    try:
        day, bars = fetch_naver(codes)
        src = "네이버"
        if day is None:
            raise RuntimeError("네이버 응답에 오늘 체결이 없습니다")
    except Exception as e:                                       # noqa: BLE001
        print(f"  ⚠ 네이버 시세 실패({repr(e)[:60]}) → 키움", file=sys.stderr, flush=True)
        day, bars = fetch_kiwoom(codes)
        src = "키움"
        if day is None:
            raise RuntimeError("키움 응답에도 오늘 체결이 없습니다") from e
    return {"day": day, "at": datetime.now(), "src": src, "bars": bars}


# ────────────────────────────── 오늘 행 만들기 (자식 프로세스)

_JOB: dict = {}


def _job_init(job: dict) -> None:
    """윈도우(spawn) 작업 프로세스 시작 때 _JOB 을 채운다."""
    _JOB.update(job)


def _one(item) -> pd.DataFrame | None:
    code, bar = item
    d = core.read_price(code)
    if d is None or len(d) < core.PANEL_MIN_ROWS or d.index[-1] >= _JOB["day"]:
        return None                              # 패널에 없는 종목이거나, 그날 봉이 이미 저장돼 있다
    d.loc[_JOB["day"]] = bar
    return core.stock_rows(code, d, _JOB["mc"].get(code), _JOB["cat"]).iloc[-core.LIVE_TAIL:]     # 직전 행도 바뀐다(주봉 표시가 옮겨 간다)


def build_rows(day, bars: dict, workers: int = WORKERS) -> pd.DataFrame:
    """오늘 봉을 붙였을 때 종목마다 패널의 **마지막 LIVE_TAIL 행**(직전 거래일 + 오늘). core.merge_live 로 끼우면
    저장 자료를 그날까지 받아 패널을 새로 만든 것과 한 칸도 다르지 않다(test_live)."""
    codes = core.universe().Code
    _JOB.update(day=pd.Timestamp(day), cat=pd.CategoricalDtype(sorted(codes.unique())),
                mc=core.marcap_tail(codes))
    items = [(c, tuple(int(v) for v in bars[c])) for c in codes if c in bars]
    if workers <= 1:
        out = [_one(it) for it in items]
    elif sys.platform == "win32":                                   # fork 가 없다 — _JOB 을 작업 프로세스 시작 때 넘긴다
        with mp.get_context("spawn").Pool(workers, initializer=_job_init, initargs=(dict(_JOB),)) as pool:
            out = pool.map(_one, items, chunksize=16)
    else:
        with mp.get_context("fork").Pool(workers) as pool:          # _JOB 은 fork 로 물려받는다
            out = pool.map(_one, items, chunksize=16)
    out = [o for o in out if o is not None]
    if not out:
        return pd.DataFrame(columns=core._PANEL_COLS)
    return pd.concat(out, ignore_index=True)


def main(argv: list[str]) -> int:
    if len(argv) == 4 and argv[1] == "rows":
        snap = json.loads(Path(argv[2]).read_text(encoding="utf-8"))
        rows = build_rows(snap["day"], snap["bars"])
        rows.to_pickle(argv[3])
        print(f"오늘 봉 {int((rows.dt == rows.dt.max()).sum()) if len(rows) else 0:,}종목")
        return 0
    t = time.time()
    s = fetch(list(core.universe().Code))
    print(f"{s['src']} · 거래일 {s['day']:%Y-%m-%d} · {len(s['bars']):,}종목 · {time.time() - t:.1f}초")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
