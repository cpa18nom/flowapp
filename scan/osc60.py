"""60분봉 오실레이터 검색 (사용자 요청 2026-09-30).

일봉·주봉 검색식은 저장된 시세로 패널에서 바로 계산하지만, **분봉은 저장해 두지 않는다** — 종목 화면의 분봉 차트도
열 때마다 그 종목 하나만 키움(ka10080)에서 받아 그린다. 그래서 60분봉 검색은 대상 종목을 하나씩 조회해야 한다
(거래대금 하한을 넘는 약 800종목 × 요청 간격 0.143초 ≈ 2분 — 간격 실측은 PACE 주석). 단추를 눌러 시작하고, 결과는 파일에 남겨 다시 본다.
과거 분봉 이력이 없으므로 **과거 성적(검정)은 낼 수 없다.**

판정 (정규장 60분봉, 오실레이터는 일봉과 같은 식 core.stoch_lines):
  양봉 전환     : 최근 RECENT 봉 안에 osc 가 음 → 0 이상으로 바뀌었고, 지금도 양봉
  음봉 축소 시작 : 지금 osc 가 음수이고 직전 봉보다 올라와 있으며, 그 축소가 최근 RECENT 봉 안에 시작됐다
  양봉          : 지금 osc 가 0 이상(언제 바뀌었든). 사건 봉 = 그 양봉 구간이 시작된 봉
  음봉 3칸 축소  : 이번 음봉 구간에서 가장 깊은 봉 뒤로 마지막 봉까지 연달아 3봉 이상 줄었고, 3칸째가 최근 RECENT 봉 안
                  (일봉·주봉의 core.osc_shrink_n 과 같은 정의 — 사건 봉 = 3칸째 봉)
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path

import core

KINDS = {"osc60_up": "60분봉 오실레이터 음봉→양봉 전환 (실시간 조회)", "osc60_pos": "60분봉 오실레이터 양봉 상태 (실시간 조회)",
         "osc60_shrink": "60분봉 오실레이터 음봉 축소 시작 (실시간 조회)", "osc60_shrink3": "60분봉 오실레이터 최대 음봉 뒤 3칸 축소 (실시간 조회)"}
SHRINK_N = core.OSC_SHRINK_N   # '최대 음봉에서 3칸' — 일봉·주봉(core.osc_shrink_n)과 같은 칸 수
RECENT = 6            # 최근 몇 봉 안의 사건을 볼지 (60분봉 6개 ≈ 하루)
PACE = 0.143          # 요청 **시작** 간 간격(초). ka10080 실측 2026-10-01(순차, 429 는 0.3초 뒤 재시도):
#   0.25초 3.9종목/초·429 0% (800종목 3.4분) · 0.143초 6.3종목/초·3% (2.1분, 채택) · 0.1초 6.7종목/초·7% · 무간격 7.7종목/초·13%.
#   예전에는 응답을 받은 **뒤에** 0.25초를 쉬어 종목당 약 0.29초였다(응답 0.036초 — 800종목 약 3.8분, 계산값). 재시도는 minute.fetch 가 한다.
FILE = core.CACHE / "osc60_scan.json"
_STATE = {"running": False, "done": 0, "total": 0, "msg": "", "fail": 0, "ended": 0.0, "saved": True}
FRESH_MIN = 30        # 장중에는 받은 지 이 분이 지나면 다시 받는다 (60분봉 한 봉의 절반)
RETRY_MIN = 10        # 방금 끝난 검색이 결과를 못 남겼으면(전 종목 실패 등) 이 분 동안은 자동으로 다시 시작하지 않는다
_LOCK = threading.Lock()


def classify(bars: list[dict]) -> list[dict]:
    """봉 목록(과거→최신, osc 포함) → 해당하는 검색식마다 {"kind": …, "t": 사건 봉 시각, "px": 그 봉 종가, "now": 마지막 종가}.
    한 종목이 둘에 같이 걸릴 수 있다(양봉 전환 ⊂ 양봉, 3칸 축소 ⊂ 축소 중). 해당 없으면 빈 목록."""
    b = [x for x in bars if x.get("osc") is not None]
    if len(b) < RECENT + 2:
        return []
    osc = [x["osc"] for x in b]
    n = len(b)
    hit = lambda kind, i: {"kind": kind, "t": b[i]["t"], "px": b[i]["c"], "now": b[-1]["c"]}     # noqa: E731
    out = []
    if osc[-1] >= 0:
        i = n - 1
        while i > 0 and osc[i - 1] >= 0:                    # 지금 양봉 구간이 시작된 봉(받은 자료가 거기까지 없으면 첫 봉)
            i -= 1
        out.append(hit("osc60_pos", i))
        if i > 0 and i > n - 1 - RECENT:                    # 그 시작(음→양 전환)이 최근 RECENT 봉 안이면 '양봉 전환'
            out.append(hit("osc60_up", i))
        return out
    if osc[-1] > osc[-2]:                                   # 음봉인데 줄어드는 중
        i = n - 1
        while i - 1 > n - 1 - RECENT and osc[i - 1] < 0 and osc[i - 1] > osc[i - 2]:
            i -= 1                                          # 축소가 시작된 봉까지 거슬러 간다
        if i > n - 1 - RECENT:
            out.append(hit("osc60_shrink", i))
        # 최대 음봉에서 SHRINK_N 칸: 마지막 봉까지 연달아 k 봉 줄었고(k ≥ N), 줄기 직전 봉이 이번 음봉 구간의 최저
        j = n - 1
        while j > 0 and osc[j] < 0 and osc[j] > osc[j - 1]:
            j -= 1                                          # j = 줄기 직전 봉(바닥)
        k = n - 1 - j
        if k >= SHRINK_N and k - SHRINK_N < RECENT and osc[j] < 0:
            m = j
            while m > 0 and osc[m - 1] < 0:
                m -= 1                                      # 이번 음봉 구간의 첫 봉
            if osc[j] <= min(osc[m:j + 1]):
                out.append(hit("osc60_shrink3", j + SHRINK_N))
    return out


def status() -> dict:
    with _LOCK:
        return dict(_STATE)


def load() -> dict | None:
    try:
        return json.loads(FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def run(codes: list[str], fetch=None, pace: float = PACE) -> dict:
    """codes 를 하나씩 조회해 분류하고 결과를 저장한다. fetch(code) → 봉 목록(테스트에서 바꿔 끼운다)."""
    if fetch is None:
        import minute                                       # noqa: PLC0415
        fetch = lambda c: minute.fetch(c, 60)["bars"]       # noqa: E731
    rows, fail, t0, err = [], 0, time.time(), ""
    with _LOCK:
        _STATE.update(running=True, done=0, total=len(codes), fail=0, msg="60분봉을 받는 중…")
    try:
        for i, code in enumerate(codes, 1):
            t_req = time.monotonic()
            try:
                rows.extend(dict(hit, code=code) for hit in classify(fetch(code)))
            except Exception as exc:                        # noqa: BLE001 — 한 종목 실패로 멈추지 않는다
                fail += 1
                err = err or str(exc)
            with _LOCK:
                _STATE.update(done=i, fail=fail)
            if pace:                                        # 간격은 요청 시작 기준 — 응답 시간만큼 덜 쉰다
                time.sleep(max(0.0, pace - (time.monotonic() - t_req)))
        out = {"at": datetime.now().strftime("%Y-%m-%d %H:%M"), "total": len(codes), "fail": fail,
               "sec": round(time.time() - t0), "rows": rows}
        if codes and fail == len(codes):
            # 전 종목 실패(키움 IP 미등록 8050 등) — 빈 결과로 지난 검색 결과를 덮어쓰지 않는다(2026-09-30 실제로 덮어써서 잃었다)
            with _LOCK:
                _STATE.update(saved=False, msg=f"검색 실패 — 한 종목도 받지 못했습니다. 지난 결과는 그대로 둡니다. ({err[:120]})")
            return out
        FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(FILE) + ".tmp")
        tmp.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        tmp.replace(FILE)
        with _LOCK:
            _STATE.update(saved=True, msg=f"완료 — {len(codes):,}종목 중 {len({r["code"] for r in rows}):,}종목 해당 · 실패 {fail} · {out['sec']}초")
        return out
    finally:
        with _LOCK:
            _STATE.update(running=False, ended=time.time())


def last_close(now: datetime) -> datetime:
    """now 이전 가장 최근 정규장 마감(평일 15:30). 공휴일은 모른다 — 휴일엔 한 번 더 받을 뿐이다."""
    from datetime import timedelta                          # noqa: PLC0415
    d = now.replace(hour=15, minute=30, second=0, microsecond=0)
    if now < d:
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def stale(res: dict | None, n_codes: int, now: datetime | None = None) -> bool:
    """「검색」을 누를 때 새로 받아야 하나. 결과가 없거나, 대상 종목 수가 달라졌거나(거래대금 하한 변경),
    마지막 마감 뒤에 받은 게 아니면서 FRESH_MIN 분이 지났으면 새로 받는다. 마감 뒤에 받은 결과는 다음 장까지 그대로 쓴다."""
    now = now or datetime.now()
    if not res:
        return True
    if res.get("total") != n_codes:
        return True
    at = datetime.strptime(res["at"], "%Y-%m-%d %H:%M")
    in_session = now.weekday() < 5 and (9, 0) <= (now.hour, now.minute) < (15, 30)
    if in_session:                                          # 장중: 봉이 계속 생긴다 → FRESH_MIN 분이 지나면 새로
        return (now - at).total_seconds() > FRESH_MIN * 60
    return at < last_close(now)                             # 장 밖: 마지막 마감 뒤에 받았으면 그대로 쓴다


def should_start(res: dict | None, n_codes: int, now: datetime | None = None) -> bool:
    """자동 시작 여부 — 낡았고, 지금 돌고 있지 않고, 방금 실패로 끝난 게 아닐 때만(새로고침이 무한 재시작이 되지 않게)."""
    st = status()
    if st["running"] or not stale(res, n_codes, now):
        return False
    return st["saved"] or time.time() - st["ended"] > RETRY_MIN * 60


def start(codes: list[str]) -> bool:
    """백그라운드로 시작. 이미 돌고 있으면 False."""
    with _LOCK:
        if _STATE["running"]:
            return False
        _STATE.update(running=True, done=0, total=len(codes), msg="시작하는 중…")
    threading.Thread(target=run, args=(codes,), daemon=True).start()
    return True
