#!/usr/bin/env python3
"""flowapp — 스캐너 검색 + 수급분석 차트를 한 프로그램으로 묶은 로컬 앱.

    flowapp [--port 8700] [--no-open]

Streamlit 이 아니다. 표준 `http.server` 위에 얹은 ~300줄이고 재실행·캐시·세션이 없다.
정적 HTML 로는 3번 요구(아무 종목이나 검색·클릭)를 못 채워서 서버로 갔다 —
2,555종목을 미리 만들면 13분·3.3GB 인데, 서버는 요청 때 0.3초에 한 장을 만든다.

  /                 검색식 설정 + 후보 목록 + 종목 검색
  /stock/<코드>      일·주·월봉 + 분산 오버레이 + (일봉에) 신호 마커 + 표
  /api/search?q=    종목명·코드 검색 (JSON)
  /static/*.js      차트 그리기(lightweight-charts·chart.js) — 2026-10-02 plotly 에서 옮김
  /plotly.js        DART 상세 섹션만 쓴다(누를 때 받는다)
  (테마)            결과 표·종목 화면의 테마는 themes.py(네이버 증권) — 2026-10-02
  /api/live         오늘 봉(잠정) 받기 상태 — 「검색」이 낡았을 때 live.py 로 받아 패널에 끼운다(2026-10-02)

검색 로직은 **스캐너 것을 그대로 쓴다**(core.build_panel / make_signal / latest_candidates).
여기서 다시 구현하지 않는다 — 갈라지면 화면과 검정이 어긋난다. 화면은 속도 때문에 최근 구간만
떼어 계산하지만(core.recent_signal · full_signal) 신호 판정은 같은 make_signal 이다.
"""
from __future__ import annotations

import base64
import hashlib
import gzip
import hmac
import html
import os
import re
import json
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from datetime import date, datetime, time as dtime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core      # noqa: E402
import board     # noqa: E402
import viewer    # noqa: E402
import dartreport_v3  # noqa: E402
import dart_collector  # noqa: E402
import viewer_cache  # noqa: E402
import analysis  # noqa: E402
import appsettings  # noqa: E402
import research  # noqa: E402
import opinion  # noqa: E402
import realtime  # noqa: E402
import minute  # noqa: E402
import defense  # noqa: E402
import groups  # noqa: E402
import osc60  # noqa: E402
import themes  # noqa: E402
import live  # noqa: E402
import watch  # noqa: E402
import picks  # noqa: E402
import top3  # noqa: E402

# 사이드바 설정을 그대로 옮긴 기본값 (app.py 와 같은 값)
DEFAULTS = {
    # 검색 기본 조건 없음(사용자 요청 2026-10-16): 거래대금 하한·급등 제외·수급 조건은 결과 위 「필터」에서 켠다 — 숨은 조건으로 종목이 빠지지 않게
    "kind": "win_combo", "minval": 0.0, "volq": 0.30, "hh": 120,
    "cafe": 15.0, "algr": 20.0, "algp": 15.0, "surge": 0, "flow": 0, "frgn": 0, "orgn": 0, "days": 20,   # 5일이면 첫 화면이 빈 표인 날이 많다(2026-09-30)
    "unit": "d",       # 기간의 단위: d 거래일 · w 주봉 · m 월봉 (days 가 봉 수) — 2026-10-01 사용자 요청
    "start": "", "end": "",   # 직접 정한 기간(YYYY-MM-DD). start 가 있으면 days·unit 대신 쓴다 — 2026-10-02 사용자 요청
    "tf": "d",                # 봉 단위: d 일봉 · w 주봉 · h 60분봉 — 같은 검색식을 그 봉으로 계산한 패널에서 찾는다(2026-10-06 사용자 요청)
    # 재진입 검색식(신호 뒤 눌림 → 회복, 2026-10-05): 신호 뒤 며칠 안 · 눌림 깊이(%) · 그 사이 거래량 배수(0 = 안 봄)
    "rcw": core.RECLAIM_WINDOW, "rcd": core.RECLAIM_DIP, "rcv": core.RECLAIM_VMULT,
}
# 검정·추천·감시 기준선이 쓰는 설정 — 화면 기본값(DEFAULTS)과 분리한다. 승률·★·추천 모델은 이 설정으로 쟀다.
VALIDATED = {"minval": 10.0, "surge": 1, "flow": 1}
NUM = {"minval": float, "volq": float, "hh": int, "cafe": float,
       "algr": float, "algp": float, "surge": int, "flow": int, "frgn": int, "orgn": int, "days": int,
       "rcw": int, "rcd": float, "rcv": float}
CAP = 300          # 표에 그릴 최대 행 수

_PANEL = {"df": None, "at": None}                                  # 일봉 패널(오늘 봉·감시·추천·테스트가 이 이름을 쓴다)
_PANELS = {"w": {"df": None, "at": None}, "h": {"df": None, "at": None}}   # 주봉·60분봉 패널(2026-10-06)
_LOCK = threading.Lock()


def req_tf() -> str:
    """이 요청의 봉 단위(없으면 일봉 — 감시·추천 스레드, 테스트)."""
    return getattr(_REQ, "tf", "d") or "d"


def _P(tf: str | None = None) -> dict:
    """봉 단위의 패널 상태. 일봉은 모듈 전역 _PANEL 그 자체(테스트가 바꿔 끼운다)."""
    tf = tf or req_tf()
    return _PANEL if tf == "d" else _PANELS[tf]
_UPDATE = {"running": False, "log": "", "done": False, "steps": [], "t0": None, "t1": None}
_UPDATE_LOCK = threading.Lock()
# 「최신 데이터 받기」의 단계 — 화면에 '무엇을 어디서 받는지 · 얼마나 걸리는지'를 단계마다 보인다(사용자 요청 2026-10-06).
# 순서는 시작 순서. ①과 ②는 동시에 돈다(네이버와 키움은 한도가 따로). 수급(⑤)은 5분봉(②)과 같은 키움 키 한도를 쓰므로 그 뒤.
UPDATE_STEPS = (
    ("price", "① 시세", "네이버 일봉(시가·고가·저가·종가·거래량) · 전 종목 증분, 최근 10일은 수정주가 대조"),
    ("min5", "② 5분봉", "키움 분봉차트(ka10080) 5분봉 · 60분봉 검색용 · ①과 동시에"),
    ("marcap", "③ 시총", "KRX 일별 시가총액·거래대금(marcap, GitHub) · 올해 파일 한 개"),
    ("themes", "④ 테마", "네이버 증권 테마 분류 · 264개 테마 × 소속 종목"),
    ("flow", "⑤ 수급", "키움 투자자별 순매수(ka10059) · 개인·외국인·기관 등 · ② 가 끝난 뒤(같은 키움 한도)"),
    ("amount", "⑥ 거래대금", "키움 일봉차트(ka10081) 실제 거래대금 · 종목당 1쪽(≈600일) · 차트 「거래대금(억)」이 이 값을 쓴다 · ⑤ 다음(같은 키움 한도)"),
    ("panel", "⑦ 지표", "일봉·주봉·60분봉 패널 다시 계산 · 봉 단위마다 종목 병렬, 봉 단위는 차례로(메모리)"),
)
KIWOOM_PACE = "0.1"   # 키움 요청 간격(초). flow.py 실측표: 0.143 → 6.6종목/초, 0.1 → 7.9종목/초(429 7%, 짧게 재시도) — 더 당겨도 안 빨라진다
# ── 서버 이전 대비: 경로·자격은 환경변수로 덮어쓸 수 있다. 값이 없으면 로컬 기본값 그대로.
STATE_DIR = Path(os.environ.get("FLOWAPP_STATE_DIR", Path.home()))
UPDATE_LOG = STATE_DIR / ".market_update.log"
UPDATE_LOCK_FILE = STATE_DIR / ".market_update.lock"
# 설정돼 있으면 모든 요청에 HTTP Basic 인증을 요구한다(사용자명은 아무거나). 비우면 잠금 없음.
PASSWORD = os.environ.get("FLOWAPP_PASSWORD", "")
# 보기 전용 비밀번호(사용자 요청 2026-10-02 — 서버에 올려 지인과 공유). 이 비밀번호로 들어온 사람은 검색·차트는 보되
# 데이터 받기·관심 그룹 편집·견해 새로 쓰기(유료 API)는 못 한다. 비우면 보기 전용 접속은 없다.
VIEW_PASSWORD = os.environ.get("FLOWAPP_VIEW_PASSWORD", "")
_REQ = threading.local()               # 요청마다 스레드가 하나다(ThreadingHTTPServer) — 그 요청의 권한을 화면 함수가 읽는다


def is_owner() -> bool:
    """이 요청이 관리자(전체 권한)인가. 요청 밖(테스트·도구)에서는 관리자로 본다."""
    return getattr(_REQ, "role", "owner") == "owner"
# 수급 수집기는 키움 클라이언트가 깔린 venv 에서만 돈다 (~/.local/bin/flow 와 같은 인터프리터)
# 수급 수집기(httpx 필요)를 돌릴 파이썬. 로컬은 kiwoom-mcp venv, 없으면(서버 단일 venv) 이 파이썬.
_KIWOOM_PY = Path.home() / ".venvs" / "kiwoom-mcp" / "bin" / "python"
FLOW_PYTHON = Path(os.environ.get("FLOW_PYTHON")
                   or (_KIWOOM_PY if _KIWOOM_PY.exists() else sys.executable))


def valid_code(code: str) -> bool:
    """종목코드 6자리. 2024년부터 신규상장은 영문이 섞인다(0161M0 네오사피엔스)."""
    return bool(re.fullmatch(r"[0-9A-Z]{6}", code))


def panel(tf: str | None = None):
    """전종목 패널(봉 단위별). 최초 1회만 만들고(일봉 ≈30초 · 60분봉 ≈4분, 자식 프로세스) 이후 메모리에서 쓴다."""
    tf = tf or req_tf()
    P = _P(tf)
    with _LOCK:
        if P["df"] is None:
            t = time.time()
            print(f"  {core.TFS[tf]} 패널 만드는 중…", file=sys.stderr, flush=True)
            core.ensure_panel_cache(tf=tf)          # 없으면 자식 프로세스가 만든다(앱 메모리 보호)
            P["df"] = core.slim_for_app(core.build_panel(tf=tf))       # 캐시를 읽기만 한다 · 화면이 안 읽는 열은 메모리에서 뺀다
            P["at"] = time.time()
            P["stored"] = P["df"].dt.max()          # 저장 자료의 마지막 봉(오늘 봉을 붙이기 전)
            print(f"  준비 완료 {time.time()-t:.0f}초 · "
                  f"{len(P['df']):,}행 {P['df'].Code.nunique():,}종목",
                  file=sys.stderr, flush=True)
        P["used"] = time.time()
        dropped = _drop_idle_panels(tf)
    if dropped:
        release_memory()
    return P["df"]


PANEL_IDLE_SEC = 30 * 60     # 주봉·60분봉 패널은 이만큼 안 쓰면 메모리에서 내린다(캐시에서 다시 읽는 데 2~3초). 일봉은 늘 둔다


def _drop_idle_panels(keep: str) -> list[str]:
    """_LOCK 안에서 부른다. 한참 안 쓴 주봉·60분봉 패널(각 0.2·0.7GB)을 놓는다 — 상주 메모리 2.3GB → 1.4GB (사용자 "메모리를 너무 많이 잡아먹네", 2026-10-06).
    파생값(_DERIVEDS)도 같이 비운다 — 패널 시각(at)이 바뀌면 어차피 버리지만 그때까지 메모리를 쥔다."""
    now, out = time.time(), []
    for tf, P in _PANELS.items():
        if tf != keep and P["df"] is not None and now - P.get("used", now) > PANEL_IDLE_SEC:
            P["df"], P["at"] = None, None
            with _DERIVED_LOCK:
                _DERIVEDS[tf].update(at=None, v={}, locks={})
            out.append(tf)
            print(f"  {core.TFS[tf]} 패널 {PANEL_IDLE_SEC // 60}분 미사용 → 메모리에서 내림", file=sys.stderr, flush=True)
    return out


def release_memory() -> None:
    """패널을 갈아 끼운 뒤 옛 메모리를 OS 에 돌려준다. 파이썬·numpy 는 큰 배열을 놓아도 힙에 쥐고 있어 상주 메모리가 줄지 않는다
    (실측 2026-10-06: 오늘 봉을 붙일 때마다 1,405 → 1,907 → 2,079 → 2,232MB). gc 뒤 glibc malloc_trim 으로 빈 힙을 반납한다(리눅스만)."""
    import ctypes
    import gc
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except OSError:                                 # glibc 가 아닌 환경(맥 등) — 조용히 넘어간다
        pass


def tune_malloc() -> None:
    """glibc 가 놓은 큰 배열을 쥐고 있지 않게 한다(리눅스만, 서버 시작 때 한 번). 2026-10-19 실측:
    큰 배열(≥128KB)은 mmap 으로 받았다가 놓으면 바로 OS 로 돌아가야 하는데, glibc 는 mmap 덩어리를 한 번 놓을 때마다
    그 크기까지 기준을 올려(동적 임계값) 다음부터는 힙에서 준다 → 검색 뒤 차트 신호일 미리 계산(수백 MB 임시)이 끝나도
    상주가 +250MB 남았다. 기준을 128KB 로 고정하면 동적 조정이 꺼진다. 요청마다 스레드가 새로 떠 아레나도 2개로 묶는다.
    검색 화면 → 종목 화면 2개 순서에서 1,443 → 985MB(같은 결과 · 같은 속도)."""
    if not sys.platform.startswith("linux"):
        return
    import ctypes
    try:
        libc = ctypes.CDLL("libc.so.6")
        libc.mallopt(-3, 128 * 1024)       # M_MMAP_THRESHOLD
        libc.mallopt(-8, 2)                # M_ARENA_MAX
    except (OSError, AttributeError):
        pass


def invalidate_panel():
    """패널 캐시 무효화 (데이터 업데이트 후 호출)."""
    with _LOCK:
        for P in (_PANEL, *_PANELS.values()):
            P["df"] = None
            P["at"] = None
    with _LIVE_LOCK:                       # 붙여 둔 오늘 봉도 같이 사라진다 — 다음 검색 때 다시 받는다
        _LIVE.update(day=None, at=None, n=0, next=0.0)


# ────────────────────────────── 오늘 봉(잠정) — live.py
# 저장 자료는 18시 뒤에야 오늘을 받는다. 그 전에 「검색」을 누르면 지금 시세로 오늘 봉을 만들어 **메모리의 패널에만** 붙인다.
_LIVE: dict = {"day": None, "at": None, "src": "", "n": 0, "sec": 0.0, "running": False, "msg": "", "next": 0.0, "t0": 0.0}
_LIVE_LOCK = threading.Lock()
LIVE_OPEN = dtime(9, 1)        # 이 시각 전에는 오늘 체결이 없다
LIVE_FINAL = dtime(15, 40)     # 이 뒤에 받은 봉은 종가가 확정된 것 — 그날은 다시 받지 않는다
LIVE_FRESH_MIN = 5             # 장중에는 「검색」을 누를 때 이보다 오래됐으면 다시 받는다
LIVE_RETRY_MIN = 10            # 실패했거나 오늘 장이 없으면(휴장) 이만큼 쉬었다가
LIVE_STUCK_SEC = 15 * 60       # 받는 중 표시가 이보다 오래 남으면 멈춘 것으로 보고 푼다(윈도우판 첫 실행에서 한 번 '받는 중'에 멈춤 — 원인 미확인, 2026-10-19)
LIVE_ON = False                # 서버로 띄울 때만 켠다(main) — 테스트·도구가 flowapp 을 import 해 화면을 그려도 시세를 받으러 나가지 않는다


def live_due(now: datetime | None = None) -> bool:
    """지금 「검색」이 오늘 봉을 (다시) 받아야 하는가."""
    now = now or datetime.now()
    with _LIVE_LOCK:
        _live_unstick()
        st = dict(_LIVE)
    if not LIVE_ON or st["running"] or now.weekday() >= 5 or now.time() < LIVE_OPEN:
        return False
    stored = _PANEL.get("stored")
    if _PANEL["df"] is None or stored is None or stored.date() >= now.date():
        return False                                   # 오늘 봉이 이미 저장돼 있다(18시 뒤 업데이트)
    if st["day"] is not None and st["day"].date() == now.date() and st["at"].time() >= LIVE_FINAL:
        return False                                   # 마감 뒤에 받은 봉 — 더 바뀌지 않는다
    return time.time() >= st["next"]


def live_start() -> None:
    with _LIVE_LOCK:
        if _LIVE["running"]:
            return
        _LIVE.update(running=True, msg="오늘 시세를 받는 중…", t0=time.time())
    threading.Thread(target=_live_run, daemon=True).start()


def _live_run() -> None:
    t, wait, msg = time.time(), LIVE_RETRY_MIN, ""
    tmp = Path(tempfile.gettempdir()) / f"flowapp_live_{os.getpid()}"
    try:
        p = panel("d")
        stored = _PANEL["stored"]
        snap = live.fetch([str(c) for c in p.Code.cat.categories])
        if snap["day"] <= stored:
            msg = f"오늘 시세가 아직 없습니다(마지막 거래일 {snap['day']:%m-%d} · 휴장이거나 장 시작 전)"
            return
        with _LIVE_LOCK:
            _LIVE["msg"] = f"오늘 봉을 만드는 중… ({len(snap['bars']):,}종목 · 약 {live.estimate(len(snap['bars']))}초)"
        src, dst = tmp.with_suffix(".json"), tmp.with_suffix(".pkl")
        src.write_text(json.dumps({"day": f"{snap['day']:%Y-%m-%d}", "bars": snap["bars"]}), encoding="utf-8")
        # 종목마다 전 이력으로 지표를 다시 내야 한다(단일 코어 2분) → 자식 프로세스가 여러 코어로 나눠 하고 끝나면 메모리를 반납한다
        subprocess.run([sys.executable, str(Path(live.__file__).resolve()), "rows", str(src), str(dst)],
                       check=True, timeout=600, capture_output=True)
        rows = pd.read_pickle(dst)
        with _LOCK:
            if _PANEL["df"] is None or _PANEL.get("stored") != stored:
                msg = "받는 사이 저장 자료가 바뀌었습니다 — 다시 검색하십시오"
                wait = 0
                return
            _PANEL["df"] = core.merge_live(_PANEL["df"], rows, snap["day"])
            _PANEL["at"] = time.time()
        release_memory()                       # 옛 패널(0.8GB)을 OS 에 돌려준다 — 매시간 붙이면 상주 메모리가 150MB 씩 늘었다(실측 2026-10-06)
        wait = LIVE_FRESH_MIN
        with _LIVE_LOCK:
            _LIVE.update(day=snap["day"], at=snap["at"], src=snap["src"], n=int((rows.dt == snap["day"]).sum()), bars=snap["bars"],
                         sec=round(time.time() - t, 1))
    except Exception as e:                                       # noqa: BLE001
        msg = f"오늘 시세를 받지 못했습니다 — {repr(e)[:80]}"
        print(f"  ⚠ {msg}", file=sys.stderr, flush=True)
    finally:
        for f in (tmp.with_suffix(".json"), tmp.with_suffix(".pkl")):
            f.unlink(missing_ok=True)
        with _LIVE_LOCK:
            _LIVE.update(running=False, msg=msg, next=time.time() + wait * 60)


def today_bar(code: str) -> dict:
    """종목 화면의 '오늘 봉' 첫 값(2026-10-15, 사용자 "당일 차트는 반영이 왜케 느리지").
    차트는 키움 웹소켓 체결이 와야 오늘 봉을 그렸다 — 장 마감 뒤에 연 화면·체결이 뜸한 종목은 18시 데이터 받기 전까지 오늘 봉이 없었다.
    네이버 시세는 거래일(localTradedAt)이 붙어 있어 휴장일에 전 거래일 값을 오늘로 그릴 위험이 없다(live.fetch_naver 가 날짜를 준다).
    오늘 체결이 없거나 받지 못하면 {} — 화면은 웹소켓 체결을 기다린다."""
    try:
        day, bars = live.fetch_naver([code])
    except Exception:
        return {}
    b = bars.get(code)
    if day is None or b is None or day.date() != datetime.now().date():
        return {}
    o, h, l, c, v = b
    return {"today": f"{day:%Y-%m-%d}", "open": o, "high": h, "low": l, "price": c, "volume": v}


def _live_unstick() -> None:
    """_LIVE_LOCK 안에서 부른다. 오래 '받는 중'이면 풀어 다음 「검색」이 다시 받게 한다."""
    if _LIVE["running"] and time.time() - _LIVE["t0"] > LIVE_STUCK_SEC:
        _LIVE.update(running=False, msg="오늘 시세 받기가 멈춰 취소했습니다 — 다시 검색하면 새로 받습니다", next=0.0)


def live_status() -> dict:
    with _LIVE_LOCK:
        _live_unstick()
        st = dict(_LIVE)
    return {"running": st["running"], "msg": st["msg"], "n": st["n"], "sec": st["sec"], "src": st["src"],
            "day": f"{st['day']:%Y-%m-%d}" if st["day"] is not None else None,
            "at": f"{st['at']:%H:%M}" if st["at"] is not None else None,
            "final": bool(st["at"] is not None and st["at"].time() >= LIVE_FINAL)}


def live_note() -> str:
    """결과 머리 옆의 한 줄 — 오늘 봉이 붙어 있는지, 받는 중인지."""
    st = live_status()
    if st["running"]:
        text = st["msg"] + " 끝나면 결과가 저절로 바뀝니다"
    elif st["day"]:
        text = (f'오늘({st["day"][5:]}) {st["at"]} 시세까지 반영 · {st["n"]:,}종목 · '
                + ("종가 확정" if st["final"] else "장중 잠정 — 마감 전이라 신호가 바뀔 수 있습니다"))
    else:
        text = st["msg"]
    return f'<span id="live-msg" class="note live{" on" if st["day"] else ""}" role="status">{html.escape(text)}</span>'


LIVE_HELP = ('<p class="note"><b>오늘 봉</b> = 「검색」을 누를 때 전 종목의 지금 시가·고가·저가·현재가·거래량으로 만든 오늘 일봉입니다. '
             f'장중에는 {LIVE_FRESH_MIN}분보다 오래됐으면 다시 받고, {LIVE_FINAL:%H:%M} 뒤에 받은 것은 종가가 확정된 봉이라 그날은 그대로 씁니다. '
             '<b>장중 잠정</b> 신호는 마감 때 사라질 수 있고, 거래량·거래대금은 그 시각까지만 쌓인 값이라 거래량을 보는 검색식은 장 초반에 덜 걸립니다. '
             '오늘 봉은 검색에만 쓰고 저장하지 않습니다 — 종목 차트의 신호 표시(▼)와 수급은 데이터 기준일까지입니다.</p>')


LIVE_JS = """<script>
(function(){
  // 오늘 봉은 「검색」을 누른 요청이 서버에서 받기 시작한다(live_due). 화면은 기다렸다가 끝나면 다시 그린다.
  var msg=document.getElementById('live-msg'), res=document.getElementById('res'); if(!msg) return;
  function poll(){ fetch('/api/live').then(function(r){return r.json();}).then(function(d){
      if(d.running){ msg.textContent=d.msg+' 끝나면 결과가 저절로 바뀝니다'; setTimeout(poll,1500); }
      else location.reload();
    }).catch(function(){ setTimeout(poll,3000); }); }
  if(msg.dataset.run==='1'){ if(res) res.classList.add('stale'); setTimeout(poll,1500); }
})();
</script>"""


_DERIVEDS: dict = {t: {"at": None, "v": {}, "locks": {}} for t in ("d", "w", "h")}   # 봉 단위별 — 패널에서 한 번만 뽑아 두는 값(거래일 목록·종가 배열·전 구간 신호…)
_DERIVED_LOCK = threading.Lock()
DERIVED_MAX = 12                            # 전 구간 신호 한 개가 600만 칸(6MB)이라 설정별로 무한정 쌓지 않는다


def derived(key, build):
    """패널이 그대로인 동안 build(패널) 결과를 재사용한다. 패널이 바뀌면(업데이트·재구축) 통째로 버린다.
    같은 값을 두 요청이 동시에 만들지 않도록 **그 값만** 잠근다 — 차트 신호일을 미리 만드는 스레드(1초)가
    다른 검색을 붙잡으면 안 된다."""
    p = panel()
    _DERIVED, P = _DERIVEDS[req_tf()], _P()
    with _DERIVED_LOCK:
        if _DERIVED["at"] != P["at"]:
            _DERIVED.update(at=P["at"], v={}, locks={})
        at, v = _DERIVED["at"], _DERIVED["v"]
        if key in v:
            v[key] = v.pop(key)             # 최근에 쓴 것을 뒤로
            return v[key]
        making = _DERIVED["locks"].setdefault(key, threading.Lock())
    with making:
        with _DERIVED_LOCK:
            if _DERIVED["at"] == at and key in v:
                return v[key]
        value = build(p)
        with _DERIVED_LOCK:
            if _DERIVED["at"] == at:        # 만드는 사이 패널이 바뀌었으면 넣지 않는다
                v[key] = value
                _DERIVED["locks"].pop(key, None)
                while len(v) > DERIVED_MAX:
                    v.pop(next(iter(v)))
        return value


# 이 시각 전이면 오늘 봉·수급은 미확정이다. 시세는 15:30 마감이지만 키움 투자자별 수치는
# 장 마감 뒤에도 한동안 바뀌므로 둘 다 이 시각으로 자른다.
DATA_READY = dtime(18, 0)
REVISIT_DAYS = 10     # 시세는 최근 이만큼 다시 받아 덮어쓴다 — 장중에 굳은 봉을 교정하는 유일한 길


def data_cutoff(now: datetime | None = None) -> date:
    """받아도 되는 마지막 날짜. 확정 전이면 전날까지(주말·휴장일은 받을 게 없어 자연히 빠진다)."""
    now = now or datetime.now()
    return now.date() if now.time() >= DATA_READY else now.date() - timedelta(days=1)


def last_trading_day(state_path: Path, until: date) -> str | None:
    """시세 저장소에서 until 이하 가장 많은 종목이 도달한 날짜 = 실제 마지막 영업일(YYYY-MM-DD).

    수급 증분은 '최종일 < cutoff' 인 종목을 다시 받으므로, cutoff 를 달력 날짜(일요일 등)로
    주면 이미 최신인 2,500종목을 전부 다시 받는다(≈50분 낭비). 영업일로 맞춰야 한다.
    """
    try:
        last = json.loads(state_path.read_text())["last"]
    except (OSError, ValueError, KeyError):
        return None
    days = [v for v in last.values() if v and v <= until.isoformat()]
    if not days:
        return None
    return max(set(days), key=days.count)


def _set_status(msg: str, done: bool | None = None):
    with _UPDATE_LOCK:
        _UPDATE["log"] = msg
        if done is not None:
            _UPDATE["done"] = done


def _steps_reset():
    with _UPDATE_LOCK:
        _UPDATE["steps"] = [{"key": k, "name": n, "what": w, "state": "대기", "t0": None, "t1": None, "detail": ""}
                            for k, n, w in UPDATE_STEPS]
        _UPDATE["t0"], _UPDATE["t1"] = time.time(), None


def _step(key: str) -> dict:
    return next(x for x in _UPDATE["steps"] if x["key"] == key)


def _step_set(key: str, state: str | None = None, detail: str | None = None):
    """단계의 상태(대기·진행·완료·실패·건너뜀)와 진행 글을 바꾸고, 한 줄 상태(log)도 진행 중인 단계로 다시 쓴다."""
    with _UPDATE_LOCK:
        x = _step(key)
        if state is not None:
            x["state"] = state
            if state == "진행":
                x["t0"], x["t1"] = time.time(), None
            elif state in ("완료", "실패"):
                x["t1"] = time.time()
        if detail is not None:
            x["detail"] = detail
        run = [y for y in _UPDATE["steps"] if y["state"] == "진행"]
        if run:
            _UPDATE["log"] = " · ".join(f"{y['name']} {y['detail'] or '시작…'}".strip() for y in run) \
                             + f" · 전체 {(time.time() - _UPDATE['t0']) / 60:.1f}분 경과"


def _fmt_min(sec: float | None) -> str:
    return "" if sec is None else (f"{sec:.0f}초" if sec < 90 else f"{sec / 60:.1f}분")


def steps_view() -> list[dict]:
    """화면·API 용 단계 목록 — 걸린 시간을 글로 붙여서."""
    now = time.time()
    with _UPDATE_LOCK:
        out = []
        for x in _UPDATE["steps"]:
            el = None if x["t0"] is None else (x["t1"] or now) - x["t0"]
            out.append({**x, "elapsed": _fmt_min(el)})
        total = None if _UPDATE["t0"] is None else (_UPDATE["t1"] or now) - _UPDATE["t0"]
    return out, _fmt_min(total)


_LOG_LOCK = threading.Lock()


def _step_log_path(key: str) -> Path:
    return UPDATE_LOG.with_name(UPDATE_LOG.name + f".{key}")


def _run_step(cmd: list[str], key: str, logf) -> int:
    """하위 명령을 단계 전용 로그에 돌리며 마지막 줄을 그 단계의 진행 글로 비춘다. 끝나면 전체 로그에 이어 붙인다
    (①과 ②가 동시에 돌므로 한 파일에 섞어 쓰면 '마지막 줄'이 뒤섞인다)."""
    sp = _step_log_path(key)
    with sp.open("w") as f:
        f.write(f"$ {' '.join(cmd)}\n"); f.flush()
        proc = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT, text=True)
        while proc.poll() is None:
            time.sleep(1)
            try:
                tail = [ln for ln in sp.read_text(errors="replace").splitlines() if ln.strip() and not ln.startswith("$ ")]
                _step_set(key, detail=tail[-1].strip() if tail else "")
            except OSError:
                pass
    with _LOG_LOCK:
        logf.write("\n" + sp.read_text(errors="replace")); logf.flush()
    return proc.returncode


def _step_result(key: str) -> str:
    """단계 로그의 '■ 완료 …' 줄(없으면 마지막 줄) — 표에 남길 결과 한 줄."""
    try:
        lines = [ln.strip() for ln in _step_log_path(key).read_text(errors="replace").splitlines() if ln.strip()]
    except OSError:
        return ""
    done = [ln for ln in lines if ln.startswith("■ 완료")]
    return (done[-1] if done else (lines[-1] if lines else "")).lstrip("■ ")


def all_failed(result: str) -> bool:
    """'완료 성공 0 … 실패 N' — 끝나긴 했지만 한 종목도 못 받았다(키움 키·허용 IP 문제일 때 이렇게 끝난다)."""
    return bool(re.search(r"성공 0\b.*실패 [1-9]", result))


def _cmd_step(cmd: list[str], key: str, logf) -> int:
    _step_set(key, "진행", "")
    rc = _run_step(cmd, key, logf)
    res = _step_result(key)
    if rc == 0 and all_failed(res) and key in ("min5", "flow", "amount"):
        _step_set(key, "실패", f"{res} — 키움 키·허용 IP 를 확인하세요")
    else:
        _step_set(key, "완료" if rc == 0 else "실패", res or (f"exit {rc}" if rc else "완료"))
    return rc


def _build_panels(tfs=("d", "w", "h")) -> None:
    """지표 패널 캐시를 봉 단위마다 자식 프로세스로 만든다(core.ensure_panel_cache — 그 안에서 종목 병렬). **차례로** 돈다:
    셋을 동시에 돌리면 빠르지만(66초 vs 약 100초) 실메모리가 +3.9GB 까지 올랐다(2026-10-06 실측, 사용자 "메모리를 너무 많이 잡아먹네")."""
    for tf in tfs:
        _step_set("panel", detail=f"{core.TFS[tf]} 패널 계산 중…")
        core.ensure_panel_cache(tf=tf)


def update_summary(log_text: str) -> str:
    """업데이트 로그에서 사람이 볼 요약만 뽑는다: 단계별 성공·실패, 유니버스 편입·제외.
    '■ 완료' 줄은 바로 앞의 머리글(■ 수급 증분 / ■ 5분봉 증분 …)로 어느 단계인지 가린다 — 단계가 동시에 돌아 순서가 바뀌어도 맞다."""
    got, cur = {}, "시세"
    for ln in log_text.splitlines():
        ln = ln.strip()
        if ln.startswith("■ 수급"):
            cur = "수급"
        elif ln.startswith("■ 5분봉"):
            cur = "5분봉"
        elif ln.startswith("■ 증분 대상") or ln.startswith("■ 백필"):
            cur = "시세"
        elif ln.startswith("■ 완료"):
            got.setdefault(cur, ln.lstrip("■ ").removeprefix("완료 "))
    out = [f"{k} {got[k]}" for k in ("시세", "수급", "5분봉") if k in got]
    uni = [ln.strip() for ln in log_text.splitlines() if ln.strip().startswith("유니버스 ")]
    if uni:
        out.append(uni[-1])
    return " · ".join(out)


MARCAP_URL = "https://github.com/FinanceData/marcap/raw/master/data/marcap-{year}.parquet"


def refresh_marcap(logf, year: int | None = None) -> bool:
    """올해 marcap(KRX 일별 시총·거래대금, 바닥권 거래대금폭발 신호의 원천)을 새로 받는다.
    실패해도 업데이트를 멈추지 않는다 — 신호가 최근 며칠은 거래량÷주식수로 이어지므로 치명적이지 않다."""
    import urllib.request
    year = year or date.today().year
    dst = core.MARCAP / f"marcap-{year}.parquet"
    tmp = dst.with_suffix(".tmp")
    try:
        core.MARCAP.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(MARCAP_URL.format(year=year), timeout=60) as r, tmp.open("wb") as f:
            f.write(r.read())
        import pandas as pd                                     # noqa: PLC0415
        last = pd.read_parquet(tmp, columns=["Date"]).Date.max()   # 읽히는 파일인지 확인한 뒤 교체
        tmp.replace(dst)
        logf.write(f"marcap-{year}.parquet 갱신 · 마지막 날짜 {last:%Y-%m-%d}\n"); logf.flush()
        return True
    except Exception as e:                                       # noqa: BLE001
        tmp.unlink(missing_ok=True)
        logf.write(f"⚠ marcap 갱신 실패({repr(e)[:60]}) → 기존 파일 사용\n"); logf.flush()
        return False


def try_lock(f) -> bool:
    """업데이트 중복 차단용 파일 잠금(잡지 못하면 False). 리눅스는 flock, 윈도우는 msvcrt."""
    if sys.platform == "win32":
        import msvcrt
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def update_data_async():
    """시세(네이버)와 5분봉(키움)을 동시에 → 시총·테마 → 수급(키움) → 지표 패널. 장중이면 전 영업일까지만. flock 으로 중복 차단.
    키움 두 단계는 같은 키 한도를 쓰므로 차례로(한도 ≈8건/초, 동시에 쏴도 안 빨라진다 — flow.py 실측표)."""
    _steps_reset()
    with _UPDATE_LOCK:
        _UPDATE["running"] = True
        _UPDATE["done"] = False
        _UPDATE["log"] = "업데이트 시작…"

    lock_file = None
    try:
        lock_file = open(UPDATE_LOCK_FILE, 'w')
        if not try_lock(lock_file):
            _set_status("이미 업데이트 중입니다", done=True)
            return

        t = time.time()
        bin_dir = Path(__file__).resolve().parent.parent / "bin"
        cutoff = data_cutoff()
        rc5 = {"rc": None}
        kiwoom_ok = not appsettings.enabled() or appsettings.kiwoom_ready()     # 배포판에서 키를 아직 안 넣었으면 키움 단계는 건너뛴다
        no_key = "키움 키 없음 — 「설정」에서 넣으세요"
        with UPDATE_LOG.open('w') as logf:
            logf.write(f"업데이트 시작 {datetime.now():%Y-%m-%d %H:%M:%S} · 기준일 {cutoff} · 키움 간격 {KIWOOM_PACE}초\n"); logf.flush()
            # ② 5분봉 — 네이버 시세와 동시에(키움 쪽은 비어 있다). 실패해도 시세·수급 결과는 유효.
            if not kiwoom_ok:
                th5 = None
                _step_set("min5", "건너뜀", no_key)
            elif (bin_dir / "min5.py").exists():
                def _min5():
                    rc5["rc"] = _cmd_step([str(FLOW_PYTHON), str(bin_dir / "min5.py"), "--sleep", KIWOOM_PACE, "update"], "min5", logf)
                th5 = threading.Thread(target=_min5, daemon=True)
                th5.start()
            else:
                th5 = None
                _step_set("min5", "건너뜀", "bin/min5.py 없음")
            frm = (cutoff - timedelta(days=REVISIT_DAYS)).isoformat()
            rc = _cmd_step([sys.executable, str(bin_dir / "mkt.py"), "update", "--from", frm, "--until", cutoff.isoformat()], "price", logf)
            if rc != 0:
                for k in ("marcap", "themes", "flow", "amount", "panel"):
                    _step_set(k, "건너뜀", "시세 실패")
                if th5:
                    th5.join()
                _set_status(f"✗ 시세 오류 (exit code: {rc}) · 로그 {UPDATE_LOG}", done=True)
                return
            _step_set("marcap", "진행", "")
            ok = refresh_marcap(logf)
            _step_set("marcap", "완료" if ok else "실패", "올해 파일 갱신" if ok else "실패 → 기존 파일 사용")
            _step_set("themes", "진행", "")
            try:
                themes.update(lambda m: (logf.write(m + "\n"), logf.flush(), _step_set("themes", detail=m)))
                _step_set("themes", "완료")
            except Exception as e:                               # noqa: BLE001 — 테마는 없어도 검색은 된다
                logf.write(f"⚠ 테마 갱신 실패({repr(e)[:60]})\n"); logf.flush()
                _step_set("themes", "실패", repr(e)[:60])

            day = last_trading_day(core.META / "state.json", cutoff)
            if not day:
                if th5:
                    th5.join()
                _set_status("✗ 시세 저장소에서 마지막 영업일을 못 찾았습니다", done=True)
                return
            if th5:
                if th5.is_alive():
                    _step_set("flow", detail="5분봉이 끝나길 기다리는 중(같은 키움 한도)")
                th5.join()
                if rc5["rc"] not in (0, None):
                    logf.write(f"⚠ 5분봉 갱신 실패(exit {rc5['rc']}) — 60분봉 검색은 이전 자료로 남는다\n"); logf.flush()
            if kiwoom_ok:
                rc = _cmd_step([str(FLOW_PYTHON), str(bin_dir / "flow.py"), "--sleep", KIWOOM_PACE, "update", "--until", day.replace("-", "")], "flow", logf)
            else:
                rc = 0
                _step_set("flow", "건너뜀", no_key)
            # ⑥ 거래대금(키움 ka10081, 사용자 요청 2026-10-07) — 실패해도 차트는 종가×거래량으로 메우므로 뒤 단계를 막지 않는다
            if not kiwoom_ok:
                _step_set("amount", "건너뜀", no_key)
            elif (bin_dir / "amount.py").exists():
                liq_file = STATE_DIR / ".amount_codes"                     # 유동 종목(20일 거래대금 ≥ 기본 하한)만 — 2,556→약 1,200종목, 6분→3분(2026-10-07)
                try:
                    liq_file.write_text("\n".join(liquid_codes()), encoding="utf-8")
                    extra = ["--only-file", str(liq_file)]
                except Exception as e:                           # noqa: BLE001 — 못 만들면 전 종목
                    logf.write(f"⚠ 유동 종목 목록 실패({repr(e)[:50]}) — 전 종목으로\n"); logf.flush()
                    extra = []
                rca = _cmd_step([str(FLOW_PYTHON), str(bin_dir / "amount.py"), "--sleep", KIWOOM_PACE, "update", *extra], "amount", logf)
                if rca != 0:
                    logf.write(f"⚠ 거래대금 갱신 실패(exit {rca}) — 차트는 종가×거래량 근사로 남는다\n"); logf.flush()
            else:
                _step_set("amount", "건너뜀", "bin/amount.py 없음")

        _step_set("panel", "진행", "")
        invalidate_panel()
        try:
            _build_panels()
            panel()
            release_memory()
            _step_set("panel", "완료", "일봉·주봉·60분봉")
        except Exception as e:                                   # noqa: BLE001
            _step_set("panel", "실패", repr(e)[:80])
        with _UPDATE_LOCK:
            _UPDATE["t1"] = time.time()
        elapsed = time.time() - t
        if rc == 0:
            try:
                summ = update_summary(UPDATE_LOG.read_text(errors="replace"))
            except OSError:
                summ = ""
            _set_status((f"✓ 완료 — 시세·수급 {day} 까지 ({elapsed/60:.1f}분)" if kiwoom_ok else
                         f"✓ 완료 — 시세 {day} 까지 ({elapsed/60:.1f}분) · 수급·5분봉은 키움 키가 없어 건너뜀")
                        + (f" · {summ}" if summ else ""), done=True)
        else:
            _set_status(f"✗ 수급 오류 (exit code: {rc}) · 시세는 {day} 까지 반영됨 · 로그 {UPDATE_LOG}",
                        done=True)
    except Exception as e:
        _set_status(f"✗ 오류: {e}", done=True)
    finally:
        with _UPDATE_LOCK:
            _UPDATE["running"] = False
            if _UPDATE["t1"] is None and _UPDATE["t0"] is not None:
                _UPDATE["t1"] = time.time()
        if lock_file:
            lock_file.close()


UNITS = {"d": "거래일", "w": "주", "m": "개월"}          # 기간 단위 → 화면에 쓰는 말
SPAN_CHOICES = (5, 10, 20, 30)                            # 단위마다 고를 수 있는 봉 수 (사용자 요청 2026-10-01)
SPAN_ROWS = (("d", "일"), ("w", "주"), ("m", "개월"))     # "5월"은 달 이름으로 읽힌다 — "5개월"


def opts(q: dict) -> dict:
    o = dict(DEFAULTS)
    for k, v in q.items():
        if k in o:
            try:
                # 마지막 값을 쓴다 — 체크박스는 hidden(0) 뒤에 checkbox(1)를 보내므로 해제하면 0 만 온다
                o[k] = NUM[k](v[-1]) if k in NUM else v[-1]
            except (ValueError, TypeError):
                pass
    m = re.fullmatch(r"(\d{1,4})([dwm])", (q.get("span") or [""])[-1])     # 검색 폼의 기간 단추: "5w" = 5주
    if m:
        o["days"], o["unit"] = int(m.group(1)), m.group(2)
    if o["unit"] not in UNITS:
        o["unit"] = DEFAULTS["unit"]
    if o["tf"] not in core.TFS:
        o["tf"] = DEFAULTS["tf"]
    o["days"] = max(1, o["days"])
    for k in ("start", "end"):                       # 날짜 칸은 YYYY-MM-DD 만 받는다(빈칸 = 안 씀)
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(o[k])):
            o[k] = ""
    if m:                                            # 기간 단추를 눌렀으면 직접 정한 날짜는 지운다(단추가 이긴다)
        o["start"] = o["end"] = ""
    if o["end"] and not o["start"]:
        o["start"] = str(trading_dates()[0].date())
    if o["start"] and o["end"] and o["end"] < o["start"]:
        o["start"], o["end"] = o["end"], o["start"]
    if o["kind"] not in core.SIGNALS.values() and o["kind"] not in osc60.KINDS:
        o["kind"] = DEFAULTS["kind"]
    return o


def period_label(o: dict) -> str:
    if o["start"]:
        return f'{o["start"]} ~ {o["end"] or "오늘"}'
    return f'최근 {o["days"]}{UNITS[o["unit"]]}'


def trading_dates():
    return derived("dates", lambda p: pd.DatetimeIndex(np.sort(p.dt.unique())))


def period_cut(o: dict) -> pd.Timestamp:
    """검색 기간이 시작하는 거래일 — 패널 전체의 거래일로 센다(core.window_cut). 직접 정한 시작일이 있으면 그 날."""
    if o["start"]:
        return pd.Timestamp(o["start"])
    return core.window_cut(trading_dates(), o["days"], o["unit"])


def wider(o: dict) -> dict | None:
    """결과가 없을 때 권할 '한 단계 넓은' 기간. 같은 단위의 다음 칸, 없으면 다음 단위의 더 긴 칸. 직접 정한 기간이면 권하지 않는다."""
    if o["start"]:
        return None
    ladder = [(5, "d"), (10, "d"), (20, "d"), (30, "d"), (10, "w"), (20, "w"), (30, "w"), (10, "m"), (20, "m"), (30, "m")]
    approx = {"d": 1, "w": 5, "m": 21}
    now = o["days"] * approx[o["unit"]]
    same = next(((n, u) for n, u in ladder if u == o["unit"] and n > o["days"]), None)
    nxt = same or next(((n, u) for n, u in ladder if n * approx[u] > now), None)
    return dict(o, days=nxt[0], unit=nxt[1]) if nxt else None


def sig_kwargs(o: dict) -> dict:
    return dict(vol_q=o["volq"], hh_days=o["hh"], cafe_decline=o["cafe"], alg_rise=o["algr"],
                alg_pullback=o["algp"], exclude_surge=bool(o["surge"]), flow_ok=bool(o["flow"]),
                frgn_ok=bool(o["frgn"]), orgn_ok=bool(o["orgn"]),
                reclaim_window=max(1, o["rcw"]), reclaim_dip=max(0.0, o["rcd"]), reclaim_vmult=max(0.0, o["rcv"]))


def min_val(o: dict) -> float:
    """거래대금 하한(원). 화면 입력은 하루 **억** — 20봉 평균이 봉 단위에 따라 다르므로(60분봉 = 하루의 1/7, 주봉 = 5배) 맞춰 준다."""
    return o["minval"] * 1e8 / core.TF_BARS_PER_DAY[o.get("tf", "d")]


def up_days(o: dict):
    """유동성으로 좁힌 유니버스의 날짜별 상승국면(조합 1위 검색식용). 전 구간 계산이라 거래대금 하한별로 한 번만."""
    return derived(("up", o["minval"]), lambda p: core.liquid_up_days(p, min_val(o)))


def scan(o: dict):
    """후보 목록 + 그 기간의 프레임·마스크. **최근 구간만** 떼어 계산한다(core.recent_signal) —
    전 구간 사본을 만들던 예전 경로(apply_liquidity → make_signal)와 결과가 같다는 것은 test_flowtool 이 지킨다."""
    # ★ val20 은 **원** 단위(종가×거래량)인데 화면 입력은 **억**이다.
    #   app.py 도 `min_val_eok * 1e8` 로 넘긴다. 그대로 넘기면 "10원 이상"이 되어
    #   유동성 필터가 통째로 꺼진다(실측: 골든크로스 후보 1,856건 → 필터 적용 시 급감).
    cut = period_cut(o)
    win, mask = core.recent_signal(panel(), min_val(o), o["kind"], cut,
                                   up_days=up_days(o) if o["kind"] in core.REGIME_KINDS else None,
                                   # 재진입 검색식은 구간을 잘라서는 못 구한다 — 설정별로 만들어 둔 전 구간 신호에서 뗀다
                                   fullmask=full_mask(o) if o["kind"] in core.RECLAIM else None, **sig_kwargs(o))
    if o["end"]:                        # 직접 정한 기간의 끝 — 그 날 이후에 걸린 것은 뺀다
        mask = mask & (win.dt <= pd.Timestamp(o["end"])).to_numpy()
    if o["kind"] in core.OSC_STATE:     # 상태 검색식: 기간 안에 하루라도 그 상태였던 종목 — 발생일은 상태가 시작된 날
        return win, mask, core.state_candidates(win, mask, panel(), core.OSC_STATE[o["kind"]], cut=cut)
    return win, mask, core.latest_candidates(win, mask, cut=cut, rank=False)    # 변동성순위는 화면에 없다


def full_mask(o: dict):
    """전 구간 신호(패널 행 순서 bool 배열) — 종목 차트의 신호일(▲)용. 설정별로 한 번만 만든다:
    순위를 매기는 검색식(신고가·승률형·조합)은 0.6~1.1초가 걸려, 종목을 열 때마다 다시 만들면 그만큼 느리다."""
    kind = core.OSC_STATE.get(o["kind"], o["kind"])     # 양봉인 날마다 찍으면 차트가 ▲로 덮인다 — 양봉이 시작된 날만
    kw = sig_kwargs(o)
    if o["kind"] in core.OSC_STATE:
        kw["exclude_surge"] = False                     # 시작일은 사건 열 그대로(급등일이어도 양봉이 시작된 날이다)
    key = ("mask", kind, o["minval"], *kw.values())
    return derived(key, lambda p: core.full_signal(p, min_val(o), kind, **kw))


def signal_dates(o: dict, code: str) -> list:
    """한 종목의 신호 발생일(날짜순). 화면의 검색 결과와 같은 정의(같은 유동성·같은 설정)."""
    p, m = panel(), full_mask(o)
    rows = derived("rows", code_spans)
    s = rows.get(code)
    if s is None:
        return []
    i = np.arange(*s) if isinstance(s, tuple) else s
    return list(pd.to_datetime(p.dt.to_numpy()[i[m[i]]]))


def code_spans(p: pd.DataFrame) -> dict:
    """종목 → 그 종목 행의 (시작, 끝) 위치. 패널은 종목별로 행이 붙어 있어 구간 하나로 충분하다 — 종목별 위치 배열(600만 칸, 48MB)을
    상주시키지 않는다(2026-10-19). 붙어 있지 않으면(그럴 일 없지만) 예전처럼 위치 배열로."""
    codes = p.Code.cat.codes.to_numpy()
    start = np.flatnonzero(np.r_[True, codes[1:] != codes[:-1]])
    if len(start) != len(np.unique(codes)):
        return {str(c): i for c, i in p.groupby("Code", observed=True, sort=False).indices.items()}
    end = np.r_[start[1:], len(codes)]
    cats = p.Code.cat.categories
    return {str(cats[codes[s]]): (int(s), int(e)) for s, e in zip(start, end)}


def label_of(kind: str, tf: str = "d") -> str:
    """검색식 이름. 패널 신호(core.SIGNALS)와 60분봉 실시간 검색(osc60.KINDS)을 함께 찾는다.
    봉 단위가 일봉이 아니면 이름의 「일봉」을 그 봉으로 바꾼다 — 검색식의 '일봉 오실레이터'는 그 패널의 봉으로 계산한 오실레이터다
    (60분봉 패널에서는 60분봉 오실레이터, 사용자 질문 2026-10-06). 「주봉」은 어느 패널에서든 그 주의 봉으로 다시 묶어 계산하므로 그대로다."""
    name = osc60.KINDS.get(kind) or next(k for k, v in core.SIGNALS.items() if v == kind)
    if tf != "d" and kind in core.SIGNALS.values():
        name = name.replace("일봉", core.TFS[tf])
    return name


def qs(o: dict) -> str:
    return urllib.parse.urlencode(o)


# ─────────────────────────── 화면

NAV_CSS = viewer.CSS + """
:root{--box:#b9c5d4;--surface:#fff;--page-bg:#f3f6fb;--card-gap:14px;
      --shadow:0 2px 8px rgba(16,24,40,.06)}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--box:#3a424e;--surface:#1d222a;
      --page-bg:#14171c;--shadow:none}}
form.cfg{display:flex;flex-wrap:wrap;gap:10px 14px;align-items:flex-end;
         padding:12px 14px;border:1px solid var(--line);border-radius:8px;margin:0 0 14px}
form.cfg label{display:flex;flex-direction:column;gap:3px;font-size:11.5px;color:var(--muted)}
form.cfg select,form.cfg input{font:inherit;font-size:12.5px;padding:4px 7px;
         border:1px solid var(--line);border-radius:5px;background:var(--bg);color:var(--fg)}
form.cfg input[type=number]{width:84px}
form.cfg button{font:inherit;font-size:12.5px;font-weight:600;padding:6px 18px;cursor:pointer;
         border:1px solid var(--neg);background:var(--neg);color:#fff;border-radius:6px}
.chk{flex-direction:row!important;align-items:center;gap:6px!important}
.picks .card{border:1px solid var(--box);border-radius:10px;padding:14px 18px;margin:12px 0;background:var(--surface)}.picks .card.dim{opacity:.55}.picks .card header{display:flex;justify-content:space-between;align-items:center;font-size:12px;color:var(--muted)}.picks .card h3{margin:6px 0 2px;font-size:17px}.picks .kinds{margin:0 0 8px;font-size:13px;color:var(--muted)}.picks dl{margin:0;display:grid;grid-template-columns:5.5em 1fr;gap:4px 10px;font-size:13px}.picks dt{font-weight:700;color:var(--muted)}.picks dd{margin:0}.picks dd ul{margin:0;padding-left:18px}.picks button{padding:7px 14px;border:1px solid var(--box);border-radius:6px;background:var(--surface);color:inherit;cursor:pointer;font:inherit;font-size:13px}.picks button.go{background:var(--neg);border-color:var(--neg);color:#fff;font-weight:600}.picks form.inline{display:inline-flex;margin:0 8px 8px 0}.picks .warn{color:#b45309}.picks section{margin-top:16px;padding:14px 18px}.picks section h2{margin:0 0 8px;font-size:15px}.watch-add{margin:10px 0 2px}.watch-add form{display:inline}.watch-add button,.watch button{padding:7px 14px;border:1px solid var(--box);border-radius:6px;background:var(--surface);color:inherit;cursor:pointer;font:inherit;font-size:13px}.watch button.go{background:var(--neg);border-color:var(--neg);color:#fff;font-weight:600}.watch form.inline{display:inline-flex;gap:6px;align-items:center;margin:0 10px 10px 0}.watch section{margin-bottom:16px;padding:16px 20px}.watch section h2{margin:0 0 10px;font-size:16px}.watch td .c2{display:block}.watch input{padding:6px 8px;border:1px solid var(--box);border-radius:6px;font:inherit;background:var(--surface);color:inherit}.watch ol.note,.watch ul.note{margin:0 0 10px;padding-left:20px;line-height:1.7}.flowopt{margin:6px 0 2px;font-size:13px}.flowopt .chk{display:block;margin-top:4px}.flowopt .note.warn{color:#b45309}td.low{font-weight:600}.flowopt .note{display:block;margin:2px 0 0 22px;font-size:12px;color:var(--muted)}
a{color:var(--neg);text-decoration:none} a:hover{text-decoration:underline}
tbody tr:hover{background:var(--head)}
.srch{position:relative;margin:0 0 14px}
.srch input{width:100%;font:inherit;font-size:14px;padding:9px 12px;
            border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg)}
.hits{position:absolute;z-index:9;left:0;right:0;top:100%;background:var(--bg);
      border:1px solid var(--line);border-radius:8px;margin-top:4px;max-height:320px;
      overflow:auto;box-shadow:0 6px 24px rgba(0,0,0,.12)}
.hits a{display:block;padding:7px 12px;color:var(--fg);border-bottom:1px solid var(--line)}
.hits a:last-child{border-bottom:0}
.hits a:hover,.hits a.sel{background:var(--head);text-decoration:none}
.hits .c{color:var(--muted);font-size:11.5px;margin-left:6px}
.back{font-size:12.5px;margin-bottom:10px;display:inline-block}
.empty{padding:22px;text-align:center;color:var(--muted);
       border:1px dashed var(--line);border-radius:8px}
/* Plotly와 동일한 밝은 분석 화면을 유지한다. */
:root{--bg:#fff;--fg:#172b45;--line:#dce3ed;--head:#eef3f9;--muted:#52647b;
      --pos:#c0392b;--neg:#215dcc;--band:#f8fafc;color-scheme:light}
body{max-width:none;margin:auto;padding:28px;background:var(--page-bg);font-size:14px;line-height:1.6}
h1{font-size:24px;letter-spacing:-.7px;margin-bottom:6px}
h2{font-size:17px;margin:28px 0 12px}
.sub{font-size:13px;margin-bottom:20px}.note{font-size:12px;line-height:1.8}
.srch input{min-height:48px;background:#fff;font-size:16px}
.hits:empty{display:none}.hits a{padding:12px 16px}.hits .c{font-size:12px}
.search-status{padding:12px 16px;color:var(--muted)}
:focus-visible{outline:3px solid #739bea;outline-offset:3px}
form.cfg{padding:20px;gap:18px;background:#fff;border-radius:12px}
form.cfg label{font-size:12px;gap:6px;flex:1 1 155px;color:var(--fg)}
form.cfg select,form.cfg input{font-size:14px;min-height:40px}
form.cfg input[type=number]{width:100%}
form.cfg input[type=checkbox]{min-height:20px;width:18px;height:18px}
form.cfg button{min-height:44px;font-size:14px}button:disabled{opacity:.6;cursor:wait}
.wrap{background:#fff;border-radius:10px;border-color:var(--box)}th,td{padding:6px 11px}
/* 표는 내용만큼만 넓히고, 화면보다 길 때 표 안에서 스크롤한다. */
.wrap{width:fit-content;max-width:100%}
.wrap table{width:auto;table-layout:auto}
.wrap th,.wrap td{height:auto;vertical-align:middle;line-height:1.55}
.wrap th{white-space:nowrap;word-break:keep-all;overflow-wrap:normal;
         min-width:0;max-width:none;padding:6px 10px}
.wrap td{white-space:nowrap}
.wrap .k{white-space:normal;word-break:keep-all;overflow-wrap:anywhere;
         min-width:8em;max-width:18em;z-index:1}
.wrap thead .k{z-index:2}
.wrap td:not(.k),.wrap th:not(.k){text-align:right;font-variant-numeric:tabular-nums}
.wrap th.k{text-align:left}
.wrap thead th.k{white-space:nowrap;max-width:none}
/* 설명형 2열 표는 긴 문장에 따라 행 높이를 늘린다. 숫자 표는 줄바꿈하지 않는다. */
.wrap table:not(:has(thead)) td{white-space:normal;word-break:keep-all;
                             overflow-wrap:anywhere;max-width:48em;min-width:14em;text-align:left}
.stk-main{min-width:0;background:var(--surface);border:1px solid var(--box);border-radius:12px;padding:20px;box-shadow:var(--shadow)}
.fc{border:1px solid var(--box);border-radius:6px}
.chartbox{overflow-x:auto}
.chart-mobile-hint{display:none}.back{font-size:14px;padding:4px 0}
@media(max-width:1100px){.chart-mobile-hint{display:block}}
@media(max-width:900px){body{padding:16px}.stk-main{padding:12px}}
@media(max-width:600px){body{padding:12px}h1{font-size:21px}
 form.cfg{padding:14px;gap:14px}form.cfg label{flex-basis:calc(50% - 14px);min-width:0}
 form.cfg select{width:100%}form.cfg button{flex:1 1 100%}
 .pbtn{min-height:42px;padding:8px 12px}.chart-mobile-hint{display:block}
 th,td{padding:6px 8px}.stk-main{padding:10px}}
/* ── 2026-09-30 UI 개편: 상단 메뉴 · 검색식 설명 카드 · 종목당 한 줄 결과표 */
.top{display:flex;flex-wrap:wrap;align-items:center;gap:10px 18px;margin:-28px -28px 22px;padding:12px 28px;
     background:var(--surface);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:20}
.top .brand{font-size:17px;font-weight:800;letter-spacing:-.5px;color:var(--fg)}
.top .brand:hover{text-decoration:none}
.top nav{display:flex;gap:4px;flex-wrap:wrap}
.top nav a{padding:7px 14px;border-radius:999px;font-size:13.5px;font-weight:600;color:var(--muted)}
.top nav a:hover{background:var(--head);text-decoration:none}
.top nav a.on{background:var(--neg);color:#fff}
.top .right{margin-left:auto;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.chip{font-size:12px;color:var(--muted);background:var(--head);border-radius:999px;padding:5px 12px;white-space:nowrap}
.btn2{appearance:none;font:inherit;font-size:12.5px;font-weight:600;cursor:pointer;padding:7px 14px;min-height:34px;
      border:1px solid var(--line);background:#fff;color:var(--fg);border-radius:8px}
.btn2:hover{border-color:var(--neg)}
#update-status{flex:1 1 100%;margin:0;font-size:12px;text-align:right}
#update-status:empty{display:none}
.upsteps{flex:1 1 100%;display:flex;flex-direction:column;gap:3px;font-size:12px;text-align:left}
.upsteps:empty{display:none}
.upsteps.folded{display:none}
#update-fold{padding:7px 10px}
.us-row{display:grid;grid-template-columns:72px minmax(0,1fr) auto;gap:2px 10px;align-items:baseline;padding:4px 8px;border-radius:6px;background:#eef2f7;border-left:4px solid #b4c2d4}
.us-row b{font-weight:700;color:#1f2d3d}.us-what{color:#4a5a70;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.us-state{font-weight:700;white-space:nowrap;color:#4a5a70}.us-detail{grid-column:2/4;color:#2b3a4e;font-variant-numeric:tabular-nums;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.us-run{background:#fff6e0;border-left-color:#d97706}.us-run .us-state{color:#b45309}
.us-ok{border-left-color:#15803d}.us-ok .us-state{color:#15803d}
.us-bad{background:#fdecec;border-left-color:#dc2626}.us-bad .us-state{color:#b91c1c}
.us-skip{opacity:.6}.us-wait{opacity:.75}
.us-total{text-align:right;color:#4a5a70;font-weight:700;padding:2px 8px}
@media(max-width:700px){.us-row{grid-template-columns:64px minmax(0,1fr)}.us-what{display:none}.us-state{grid-column:2}.us-detail{grid-column:1/3}}
.lead{font-size:14px;color:var(--muted);margin:0 0 14px}
.sig{background:var(--surface);border:1px solid var(--box);border-radius:12px;padding:18px 20px;margin:0;box-shadow:var(--shadow)}
.card-text{flex:1 1 100%}
.sig .what{font-size:14.5px;margin:4px 0 4px}
.sig .perf{font-size:13px;color:var(--muted);margin:0}
.badge{display:inline-block;font-size:12px;font-weight:700;border-radius:999px;padding:2px 10px;vertical-align:middle;margin-left:8px}
.badge.good{background:#e7f6ec;color:#16794a}.badge.weak,.badge.osc{background:#eef1f5;color:#52647b}.badge.bad{background:#fdecea;color:#b3261e}
form.cfg.slim{border:0;padding:0;margin:0;background:none;gap:12px;align-items:flex-end}
form.cfg.slim label.main{flex:1 1 320px;max-width:560px;font-size:12px}
form.cfg.slim select{font-size:15px;min-height:44px;font-weight:600}
form.cfg details{flex:1 1 100%;border-top:1px solid var(--line);padding-top:10px;margin-top:2px}
form.cfg details summary{cursor:pointer;font-size:12.5px;color:var(--muted);user-select:none}
form.cfg details .row{display:flex;flex-wrap:wrap;gap:14px;align-items:flex-end;margin-top:12px}
form.cfg details label{flex:0 1 220px}
form.cfg button.go{min-width:132px;font-size:15.5px;min-height:44px}
.span{flex:1 1 100%;display:flex;flex-wrap:wrap;gap:8px 20px;align-items:center}
.span-t{font-size:12.5px;color:var(--muted)}.span-t b{color:var(--fg)}
.span-row{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.span-row.range input[type=date]{padding:5px 8px;border:1px solid var(--line);border-radius:6px;font:inherit;font-size:13px}
form.cfg label.pill{flex:0 0 auto;flex-direction:row;cursor:pointer;position:relative;gap:0}
form.cfg .pill input{position:absolute;opacity:0;width:1px;height:1px;min-height:0;padding:0;border:0}
.pill span{display:inline-block;padding:6px 13px;border:1px solid var(--line);border-radius:999px;background:#fff;
           font-size:13px;font-weight:600;color:var(--fg);white-space:nowrap}
.pill:hover span{border-color:var(--neg)}
.pill input:checked+span{background:var(--neg);border-color:var(--neg);color:#fff}
.pill input:focus-visible+span{outline:3px solid #739bea;outline-offset:2px}
#res.stale{opacity:.4}
.stale-note{margin:14px 0 0;font-size:13px;color:#b3261e}
.wrap.wide th.fw,.wrap.wide td.fw{border-left:1px solid var(--line)}
/* 결과도 검색 칸처럼 카드 한 장으로 — 박스 구분(사용자 요청 2026-10-02) */
#res{background:var(--surface);border:1px solid var(--box);border-radius:12px;padding:14px 18px 16px;margin:var(--card-gap) 0 0;box-shadow:var(--shadow)}
.res-head{display:flex;flex-wrap:wrap;align-items:baseline;gap:8px 16px;margin:0 0 10px;padding:0 0 8px;border-bottom:1px solid var(--line)}
.res-head h2{margin:0}
.days{display:flex;gap:6px;align-items:center;font-size:12.5px;color:var(--muted)}
.days a{padding:4px 12px;border:1px solid var(--line);border-radius:999px;background:#fff;color:var(--fg);font-weight:600}
.days a.on{background:var(--neg);border-color:var(--neg);color:#fff}
.days a:hover{text-decoration:none;border-color:var(--neg)}
/* 표는 내용 폭에 맞춘다(사용자 요청 2026-10-02 — 화면 끝까지 늘리면 좁은 열이 빈칸 투성이가 된다) */
.wrap.wide{width:fit-content;max-width:100%}.wrap.wide table{width:auto}
.wrap.wide td,.wrap.wide th{padding:6px 12px}.wrap.wide thead th{padding:7px 12px}.wrap.wide thead th{white-space:nowrap}
.wrap.wide td.k a{font-size:14.5px}
.wrap.wide .c2{display:block;font-size:12px;font-weight:400;color:var(--muted)}
.wrap.wide td.ret{font-weight:700}
/* 테마(네이버 증권) — 결과 표의 테마 칸 · 겹치는 테마 단추 · 종목 화면 테마 줄 (사용자 요청 2026-10-02) */
.wrap.wide td.th,.wrap.wide th.th{text-align:left;max-width:15em;overflow:hidden;text-overflow:ellipsis}
.wrap.wide td.th .c2{overflow:hidden;text-overflow:ellipsis}
.chip.live{background:#fdecea;color:#b3261e;font-weight:600}
.note.live{margin:0}.note.live.on{color:#b3261e}
.thbar,.themes{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin:0 0 10px;font-size:12.5px}
.thbar .lab,.themes .lab{color:var(--muted);font-weight:600;margin-right:4px}
.thbar button{font-family:inherit;padding:4px 11px;border:1px solid var(--line);border-radius:999px;background:#fff;color:var(--fg);font-size:12.5px;min-height:0;cursor:pointer}
.thbar button b{color:var(--neg);margin-left:2px}
.thbar button:hover{border-color:var(--neg)}
.thbar button[aria-pressed=true]{background:var(--neg);border-color:var(--neg);color:#fff}
.thbar button[aria-pressed=true] b{color:#fff}
.thbar .note,.themes .note{margin:0 0 0 6px}
.themes{margin:10px 0 0}.themes .chip{padding:3px 10px}
/* 관심 그룹 */
.gcards{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(250px,100%),1fr));gap:var(--card-gap);margin:0 0 20px}
.gcard{display:block;background:var(--surface);border:1px solid var(--box);border-radius:12px;padding:16px 18px;color:var(--fg);box-shadow:var(--shadow)}
.gcard:hover{border-color:var(--neg);text-decoration:none}
.gcard b{font-size:16px}.gcard .m{display:block;font-size:12px;color:var(--muted);margin:2px 0 10px}
.gcard .v{font-size:20px;font-weight:700}.gcard .r{font-size:12.5px;margin-left:8px}
.inline{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:0 0 14px}
.inline input[type=text],.inline select{font:inherit;font-size:14px;min-height:40px;padding:6px 10px;border:1px solid var(--line);
      border-radius:8px;background:#fff;color:var(--fg);min-width:200px}
.inline .btn2{min-height:40px}
.group-create{width:min(100%,520px);padding:14px;background:var(--surface);
      border:1px solid var(--box);border-radius:12px;box-shadow:var(--shadow)}
.group-create input[type=text]{flex:1 1 200px;min-width:0}
.btn1{appearance:none;font:inherit;font-size:13.5px;font-weight:600;cursor:pointer;min-height:40px;padding:8px 16px;
      border:1px solid var(--neg);background:var(--neg);color:#fff;border-radius:8px}
.xbtn{appearance:none;font:inherit;font-size:12px;cursor:pointer;border:1px solid var(--line);background:#fff;color:var(--muted);
      border-radius:6px;padding:3px 9px}.xbtn:hover{border-color:var(--pos);color:var(--pos)}
.stats{display:flex;flex-wrap:wrap;gap:var(--card-gap);margin:0 0 var(--card-gap)}
.stat{background:var(--surface);border:1px solid var(--box);border-radius:10px;padding:10px 16px;min-width:120px;box-shadow:var(--shadow)}
.stat .l{display:block;font-size:12px;color:var(--muted)}.stat .n{font-size:18px;font-weight:700}
.msg{background:#fdecea;color:#b3261e;border-radius:8px;padding:10px 14px;margin:0 0 14px;font-size:13px}
.hits a.add::after{content:"+ 담기";float:right;font-size:12px;font-weight:700;color:var(--neg)}
@media(max-width:900px){.top{margin:-16px -16px 16px;padding:10px 16px}}
@media(max-width:600px){.top{margin:-12px -12px 14px;padding:10px 12px;position:static}.top .right{margin-left:0}
 .top nav{width:100%;gap:2px;justify-content:space-between;flex-wrap:nowrap}.top nav a{padding:7px 6px;font-size:12px;white-space:nowrap}
 .hide-m{display:none}.wrap.wide td,.wrap.wide th{padding:6px 8px}.wrap.wide .k{min-width:6.5em}
 #update-status{text-align:left}form.cfg.slim label.main{flex-basis:100%}}
/* 비교·검정 및 종목 분석은 본문 카드와 같은 테두리와 간격을 쓴다. */
#res-section,#analysis-section,#op-section,#dart-section{min-width:0;background:var(--surface)!important;
  border:1px solid var(--box);border-radius:12px;padding:18px 20px;box-shadow:var(--shadow)}
#analysis-section,#op-section,#dart-section{margin-top:var(--card-gap)!important}
#res-section>h2:first-child,#analysis-section>h2:first-child,#dart-section>h2:first-child{margin-top:0}
.lg button{font-size:12px}
@media(max-width:600px){#res-section,#analysis-section,#op-section,#dart-section{padding:14px}}
"""

SEARCH_JS = """
<script>
(function(){
  var box=document.getElementById('q'), hits=document.getElementById('hits'), i=-1, t, revision=0, enterGo=false;
  if(!box) return;
  box.setAttribute('aria-label','종목명 또는 종목코드 검색');
  hits.setAttribute('aria-live','polite');
  function close(){clearTimeout(t);revision++;hits.innerHTML='';i=-1;}
  function status(message){hits.replaceChildren();var el=document.createElement('div');
    el.className='search-status';el.textContent=message;hits.appendChild(el);}
  function go(){
    var v=box.value.trim(), request=++revision;
    if(v.length<1){close();return;}
    status('종목을 찾고 있습니다…');
    fetch('/api/search?q='+encodeURIComponent(v)).then(r=>{if(!r.ok)throw Error(r.status);return r.json();}).then(function(rows){
      if(request!==revision)return;
      hits.replaceChildren();
      rows.forEach(function(r){var a=document.createElement('a'), s=document.createElement('span');
        a.href=r.url;a.textContent=r.name;s.className='c';
        s.textContent=r.code+' · '+r.market+(r.cap?' · '+r.cap:'');a.appendChild(s);hits.appendChild(a);});
      if(!rows.length)status('찾은 종목이 없습니다. 종목명 또는 6자리 코드를 확인하세요.');
      i=-1;
      if(enterGo&&rows.length){enterGo=false;location.href=rows[0].url;return;}   // 결과가 오기 전에 Enter 를 눌렀으면 첫 종목으로 바로 간다
      enterGo=false;
    }).catch(function(){if(request===revision)status('검색에 실패했습니다. 검색어를 다시 입력해 주세요.');});
  }
  box.addEventListener('input',function(){enterGo=false;close();t=setTimeout(go,150);});
  box.addEventListener('keydown',function(e){
    if(e.isComposing||e.keyCode===229) return;                     // 한글 조합 중 Enter·화살표는 IME 몫
    var as=hits.querySelectorAll('a[href]');
    if(e.key==='ArrowDown'||e.key==='ArrowUp'){
      if(!as.length) return; e.preventDefault();
      if(i>=0) as[i].classList.remove('sel');
      i = e.key==='ArrowDown' ? (i+1)%as.length : (i<=0?as.length-1:i-1);
      as[i].classList.add('sel'); as[i].scrollIntoView({block:'nearest'});
    } else if(e.key==='Enter'){
      if(i>=0&&as[i]){e.preventDefault();location.href=as[i].href;}
      else if(as.length){e.preventDefault();location.href=as[0].href;}
      else if(box.value.trim()){e.preventDefault();enterGo=true;clearTimeout(t);go();}   // 목록이 아직 없으면 바로 검색하고 첫 종목으로(사용자 요청 2026-10-08)
    } else if(e.key==='Escape'){close();}
  });
  document.addEventListener('click',function(e){
    if(!hits.contains(e.target)&&e.target!==box) close();});
})();
</script>
"""


def page(title: str, body: str) -> bytes:
    assets = Path(__file__).resolve().parent / "static"
    css_v, js_v = ((assets / n).stat().st_mtime_ns for n in ("ui.css", "ui.js"))
    return (f"""<!DOCTYPE html><html lang="ko" data-theme="light"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>{NAV_CSS}</style>
<link rel="stylesheet" href="/static/ui.css?v={css_v}">
<script src="/static/ui.js?v={js_v}" defer></script></head>
<body data-theme="light">{body}</body></html>""").encode("utf-8")


# 검색식 설명(초보가 읽는 말로) — 성적은 2026-09-30 검정값(README 「검색식 조합 검정」·「★ 수리」 절). 검정을 다시 하면 같이 고친다.
#   등급: good=추천 · weak=효과 약함/없음 · bad=주의(검정에서 오히려 나빴다).  fields=이 검색식이 실제로 쓰는 설정칸.
SIGNAL_INFO = {
    "win_combo": ("good", "추천", ("volq", "hh"),
                  "조용히 오르던(변동성 하위 30%) 종목이 120일 신고가를 새로 쓴 날을 찾습니다. 1년 상승률이 상위 30%이고 종가가 52주 최고가의 90% 이상인 종목만 남깁니다. (옛 이름 「승률형」)",
                  "2020년 이후 2,160건(상장폐지 종목 포함): 2달 뒤 오른 비율 53%, 반년 뒤 58% · 시장 평균보다 반년 +7.6%p. "
                  "급락장·횡보장(2008·2018·2019년)에는 졌습니다."),
    "best_combo": ("good", "추천 · 상승장 전용", ("volq", "hh"),
                   "시장 전체가 오르는 국면(유니버스 지수가 120일 평균 위)에서만, 조용히 오르던 종목의 120일 신고가를 찾습니다. 오실레이터가 양봉이거나 음봉이 줄어드는 날이어야 합니다. 하락국면에는 후보가 나오지 않습니다. (옛 이름 「조합 1위」)",
                   "2020년 이후 9,850건: 시장 평균보다 반년 +5.0%p, 1년 +8.1%p. 건별로는 절반쯤 지고 소수의 큰 상승이 평균을 만듭니다."),
    "lowvol_breakout": ("good", "추천 · 기본형", ("volq", "hh"),
                        "변동성이 낮은(조용한) 종목이 신고가를 돌파한 날을 찾습니다. 위 두 검색식의 바탕입니다. (옛 이름 「저변동+신고가 주력」)",
                        "2020년 이후: 시장 평균보다 반년 약 +5%p, 오른 비율 약 50%."),
    # 식이형 전사본에서 뽑은 다섯 후보 중 하나(2026-10-04, combo.py v6). 나머지 넷은 조합해도 OOS 에서 통하지 않아 화면에 넣지 않았다(README).
    "bb40_combo": ("weak", "참고 · 상승장 전용 · 유망하나 건수 적음", (),
                   "저자 화면의 「상한선」(대표가 (고+저+종)÷3 의 40일 평균 + 2σ)을 양봉 종가로 넘은 날 중, 직전 60거래일 동안 그런 날이 없던 첫 돌파를 찾습니다. "
                   "종가가 52주 최고 종가의 90% 이상이고 시장이 상승국면일 때만 남깁니다(식이형 「볼린저밴드 상한선 돌파」). 하락국면에는 후보가 나오지 않습니다.",
                   "2020년 이후 1,574건(상장폐지 종목 포함): 시장 평균보다 1달 +3.4%p, 2달 +3.7%p, 1년 +9.0%p · 2달 뒤 오른 비율 48%. "
                   "상한선 돌파만 보면(조건 없이) 오히려 나빴습니다 — 2달 −0.7%p. 건수가 적어 해마다 편차가 큽니다."),
    # 조합 탐색(combo_win.py, 2026-10-04): 검색식 × 차트·수급 조건 2~4개 21,631개 중 2021~24·2025~ 두 구간 모두 4주 승률이 높았던 두 갈래.
    #   두 구간을 다 보고 고른 것이라 순수한 사후 확인은 아니다 — 등급은 '참고'.
    "win_tight": ("weak", "참고 · 승률 높음 · 건수 적음", ("volq", "hh"),
                  "「저변동 신고가 + 1년 상승률 상위 + 52주 고가권」(★ 맨 위 검색식)에 조건 하나를 더했습니다 — 종가가 20일선 위로 5% 넘게 벌어지지 않은 날만 남깁니다. "
                  "이미 며칠 급하게 오른 뒤의 신고가를 빼는 조건입니다.",
                  "4주 뒤 오른 비율: 2021~2024년 371건 중 60%, 2025년 이후 109건 중 56%(조건 없는 원래 검색식은 50%·57%). "
                  "2개월 뒤는 62%·52%. 한 해 100건 안팎이라 편차가 큽니다."),
    "oscw_trend": ("weak", "참고 · 승률 높음", ("volq",),
                   "주봉 오실레이터가 음봉인 채로 줄어들기 시작한 주 가운데, 변동성이 낮고(하위 30%) 이동평균선이 정배열(20일 > 60일 > 120일)인 종목만 남깁니다. "
                   "오르는 추세 안에서 주봉으로 쉬어 가던 자리입니다. 그 주의 마지막 거래일에 표시됩니다.",
                   "4주 뒤 오른 비율: 2021~2024년 399건 중 57%, 2025년 이후 242건 중 57%(주봉 음봉 축소만 보면 43%·47%). 시장 평균보다 4주 +2.3%p. "
                   "수급 조건(개인 분산 30% 이하)까지 겹치면 59%·62%였습니다."),
    # 재진입(2026-10-05): reclaim.py 검정(상장폐지 포함 marcap 2007~, OOS 2020~, 60일 안·눌림 5%·거래량 무관)의 값.
    "reclaim_win": ("weak", "참고 · 원 검색식보다 낫지 않음", ("volq", "hh", "rcw", "rcd", "rcv"),
                    "「저변동 신고가 + 1년 상승률 상위 + 52주 고가권」에 걸린 뒤 종가가 그날 종가(신호가)보다 5% 넘게 내려갔다가, 60거래일 안에 처음으로 신호가 위에서 닫힌 날을 찾습니다. "
                    "신호를 놓쳤을 때 다시 들어갈 자리입니다. 검색일은 회복한 날입니다.",
                    "2020년 이후 628건(상장폐지 종목 포함): 시장 평균보다 2달 +1.9%p, 반년 +7.0%p — 신호일에 산 경우(2달 +4.2%p, 반년 +7.6%p)보다 낫지 않았습니다. "
                    "거래량이 터진 경우만 고르면 더 낮았습니다(반년 +4.9%p)."),
    "reclaim_best": ("weak", "참고 · 원 검색식보다 낫지 않음", ("volq", "hh", "rcw", "rcd", "rcv"),
                     "「저변동 신고가 + 시장 상승국면 + 오실레이터 호전」에 걸린 뒤 종가가 신호가보다 5% 넘게 내려갔다가, 60거래일 안에 처음으로 신호가 위에서 닫힌 날을 찾습니다. "
                     "신호는 상승국면에서만 나지만 회복일은 그 뒤 하락국면에도 나올 수 있습니다.",
                     "2020년 이후 2,973건(상장폐지 종목 포함): 시장 평균보다 2달 +2.1%p, 반년 +5.4%p — 신호일에 산 경우(2달 +1.6%p, 반년 +4.9%p)와 비슷했습니다."),
    "reclaim_lowvol": ("weak", "참고 · 원 검색식보다 낫지 않음", ("volq", "hh", "rcw", "rcd", "rcv"),
                       "「저변동 신고가 돌파」에 걸린 뒤 종가가 신호가보다 5% 넘게 내려갔다가, 60거래일 안에 처음으로 신호가 위에서 닫힌 날을 찾습니다.",
                       "2020년 이후 3,521건(상장폐지 종목 포함): 시장 평균보다 2달 +2.0%p, 반년 +5.6%p — 신호일에 산 경우(2달 +1.6%p, 반년 +5.0%p)와 비슷했습니다. "
                       "신호 뒤 60일 안에 한 번은 신호가 아래로 내려가는 경우가 92%라, 눌렸다 회복하는 것은 예외가 아니라 보통입니다."),
    "ema_div": ("weak", "참고", (),
                "장기 지수이동평균선(120·240·480일)이 한데 모였다가(간격 5% 이내) 정배열로 벌어지기 시작한 날을 찾습니다(식이형 방식).",
                "반년 뒤 시장 평균보다 +2.8%p로 약하게 유효했습니다. 1~2달 안에는 효과가 거의 없습니다."),
    "bb_break": ("weak", "참고 · 단기만", (),
                 "종가가 볼린저밴드(20일·2σ) 상단과 직전 60일 최고가를 함께 처음 넘은 날을 찾습니다(식이형 방식).",
                 "1주일은 시장보다 +0.5%p 앞서지만 그 뒤로는 효과가 사라졌습니다."),
    "kijun_pull": ("weak", "효과 없음", (),
                   "일목균형표 기준선(26일) 위에 있던 종목이 기준선까지 눌렸다가 전일 고가를 넘으며 다시 오르는 날을 찾습니다(식이형 방식).",
                   "1달·2달 뒤 성적이 시장 평균과 차이가 없었습니다."),
    "cafe_juryeok": ("weak", "효과 없음", ("cafe",),
                     "크게 빠진 뒤 20일선 아래에서 좁게 횡보하다가 20일선에 처음 닿은 날을 찾습니다(카페 검색기 「카페주력」 재현).",
                     "검정에서 시장 평균과 차이가 없었습니다."),
    "algkkagi": ("weak", "효과 없음", ("algr", "algp"),
                 "한 번 오른 뒤 얕게 눌려 20일선을 내줬다가 되찾은 날을 찾습니다(카페 검색기 「알까기」 재현).",
                 "검정에서 시장 평균과 차이가 없었습니다."),
    # ── 오실레이터 묶음(사용자 요청 2026-09-30). 성적: 앱 패널 2014~ 시장초과 — 네 개 모두 시장 평균과 차이가 없다.
    "stoch_turn_up": ("osc", "타점 참고 · 단독 효과 없음", (),
                      "일봉 오실레이터 막대가 파란 음봉에서 빨간 양봉으로 바뀐 날을 찾습니다(일봉양전).",
                      "78,518건: 1달 뒤 시장 평균보다 +0.12%p로 차이가 없었습니다. 다른 검색식으로 고른 종목의 매매 시점을 볼 때 쓰십시오."),
    "osc_shrink_d": ("osc", "타점 참고 · 단독 효과 없음", (),
                     "일봉 오실레이터가 음봉인 채로 줄어들기 시작한 첫날을 찾습니다(양봉 전환보다 이른 자리).",
                     "183,530건: 1달 뒤 시장 평균보다 −0.06%p로 차이가 없었습니다. 신호가 매우 잦습니다."),
    "osc_up_w": ("osc", "타점 참고 · 단독 효과 없음", (),
                 "주봉 오실레이터가 음봉에서 양봉으로 바뀐 주를 찾습니다. 그 주의 마지막 거래일에 표시됩니다.",
                 "17,266건: 1달 뒤 +0.06%p, 3달 뒤 +0.25%p로 시장 평균과 차이가 없었습니다."),
    "osc_shrink_w": ("osc", "타점 참고 · 단독 효과 없음", (),
                     "주봉 오실레이터가 음봉인 채로 줄어들기 시작한 주를 찾습니다. 그 주의 마지막 거래일에 표시됩니다.",
                     "33,409건: 1달 뒤 +0.42%p(우연 범위)로 시장 평균과 차이가 없었습니다."),
    # ── 양봉 상태 · 최대 음봉에서 3칸 축소(사용자 요청 2026-09-30 밤). 성적: 패널 v10, 거래대금 10억↑, core.event_study 20일.
    "osc_pos_d": ("osc", "타점 참고 · 단독 효과 없음", (),
                  "지금 일봉 오실레이터 막대가 빨간 양봉인 종목을 전부 보여 줍니다. 언제 양봉으로 바뀌었는지는 「양봉 시작일」에 나옵니다.",
                  "양봉인 날 1,359,311건: 1달 뒤 시장 평균보다 +0.10%p로 사실상 차이가 없었습니다. 다른 검색식으로 고른 종목을 거르는 용도로 쓰십시오."),
    "osc_pos_w": ("osc", "타점 참고 · 단독 효과 없음", (),
                  "지금 주봉 오실레이터 막대가 빨간 양봉인 종목을 전부 보여 줍니다(이번 주는 기준일까지의 봉으로 판정).",
                  "양봉인 주 295,621건: 1달 뒤 −0.09%p, 3달 뒤 −0.20%p로 시장 평균과 차이가 없었습니다."),
    "osc_shrink3_d": ("osc", "공격적 타점 · 단독 효과 없음", (),
                      "일봉 오실레이터의 가장 깊은 음봉 뒤로 3칸 연달아 줄어든 날을 찾습니다. 양봉 전환을 기다리지 않는 공격적인 자리입니다.",
                      "65,742건: 1달 뒤 시장 평균보다 −0.04%p로 차이가 없었고, 날짜별로 묶어 보면 오히려 조금 불리했습니다. 더 깊은 음봉이 다시 나올 수 있습니다."),
    "osc_shrink3_w": ("osc", "공격적 타점 · 단독 효과 없음", (),
                      "주봉 오실레이터의 가장 깊은 음봉 뒤로 3주 연달아 줄어든 주를 찾습니다. 그 주의 마지막 거래일에 표시됩니다.",
                      "13,144건: 1달 뒤 +0.54%p, 3달 뒤 +0.10%p로 우연 범위였습니다."),
    "osc_mtf": ("osc", "타점 참고 · 단독 효과 없음", (),
                "월봉과 주봉 오실레이터가 둘 다 양봉이면서 아직 커지는 중(고점을 찍기 전)일 때, 일봉 오실레이터가 음봉에서 양봉으로 바뀐 날을 찾습니다. "
                "주봉·월봉은 그날까지의 봉으로 판정합니다.",
                "5,356건: 1달 뒤 시장 평균보다 +0.33%p, 3달 뒤 +0.91%p, 6달 뒤 +1.90%p였지만 모두 우연 범위(t 1.2 이하)였습니다. "
                "일봉 양봉 전환만 볼 때(+0.12%p)보다 숫자는 낫지만 통계적으로 확인되지는 않았습니다."),
    "osc60_pos": ("osc", "타점 참고 · 성적 없음", (),
                  "지금 60분봉 오실레이터가 양봉인 종목을 전부 보여 줍니다. 「양봉이 시작된 봉」이 언제 바뀌었는지 알려 줍니다.",
                  "분봉은 저장해 두지 않아 과거 성적을 낼 수 없습니다. 검색할 때마다 키움에서 종목별로 새로 받습니다(수 분)."),
    "osc60_shrink3": ("osc", "공격적 타점 · 성적 없음", (),
                      "60분봉 오실레이터의 가장 깊은 음봉 뒤로 3칸 이상 연달아 줄어든 종목을 찾습니다(3칸째가 최근 6봉 안, 지금도 줄어드는 중).",
                      "분봉은 저장해 두지 않아 과거 성적을 낼 수 없습니다. 검색할 때마다 키움에서 종목별로 새로 받습니다(수 분)."),
    "osc60_up": ("osc", "타점 참고 · 성적 없음", (),
                 "60분봉 오실레이터가 최근 6봉(약 하루) 안에 음봉에서 양봉으로 바뀌었고 지금도 양봉인 종목을 찾습니다.",
                 "분봉은 저장해 두지 않아 과거 성적을 낼 수 없습니다. 검색할 때마다 키움에서 종목별로 새로 받습니다(수 분)."),
    "osc60_shrink": ("osc", "타점 참고 · 성적 없음", (),
                     "60분봉 오실레이터가 음봉인데 최근 6봉(약 하루) 안에 줄어들기 시작한 종목을 찾습니다.",
                     "분봉은 저장해 두지 않아 과거 성적을 낼 수 없습니다. 검색할 때마다 키움에서 종목별로 새로 받습니다(수 분)."),
    "gc": ("weak", "효과 없음", (),
           "단기 이동평균선(3일 또는 5일)이 20일선을 위로 뚫은 날을 찾습니다(골든크로스).",
           "검정에서 시장 평균과 차이가 없었습니다."),
    "sik_pullback": ("bad", "주의 · 매수 신호 아님", (),
                     "거래량이 2배 넘게 터진 +10% 장대양봉(기준봉) 뒤 20일 안에, 그 봉 몸통의 중간값까지 처음 내려온 날을 찾습니다(식이형 「기준봉 눌림」).",
                     "검정에서는 오히려 나빴습니다 — 반년 뒤 시장 평균보다 −5.3%p. 피해야 할 자리를 보는 용도입니다."),
    "bottom_spike": ("bad", "주의 · 매수 신호 아님", (),
                     "52주 신저가 뒤 20일 넘게 좁게 횡보하다가 하루 거래대금이 시가총액의 30% 넘게 터진 날을 찾습니다.",
                     "검정에서는 오히려 나빴습니다 — 반년 뒤 시장 평균보다 −4.2%p."),
    # 보조지표 관계 전수 탐색(indsetup.py, 2026-10-07)에서 전 기간 양쪽(2007~19·2020~) 다 통한 유일한 시나리오.
    "osd_vol": ("weak", "참고 · 과매도 반등 · 2주~6주 보유", (),
                "스토캐스틱(12·26·9)의 fastD 가 20 아래(과매도)인 날, 거래량이 전날까지 20일 평균의 1.5배 넘게 실리고 VWAP(120일)이 60일선 위인 종목을 찾습니다. "
                "차트 보조지표끼리의 돌파·위치·오실레이터 조합 8,000여 개를 2007년부터 전 기간 검정했을 때 앞·뒤 기간 모두 통한 유일한 조합입니다.",
                "2007년 이후 9,394건(상장폐지 포함): 2주 뒤 오른 비율 53%, 1달 54%(아무 날 45%) · 시장 평균보다 1달 +1.6%p, 6주 +1.3%p. "
                "우위가 크지 않고 1~3일 안의 승률은 48~52%로 작습니다 — 반등은 2주~6주에 걸쳐 납니다. 같은 종목에서 며칠 연속 날 수 있습니다. "
                "★ 과매도 종목은 대개 개인 분산이 30%를 넘어 「수급 조건」을 켜면 신호의 95%가 걸러집니다(최근 1주 5건 → 0건) — 건수를 보려면 끄십시오. 남는 5%는 성적이 더 좋았습니다(아래 수급 줄)."),
    # 매집 의심(2026-10-15, README 「매집 의심 검색식」 절) — 국내 수급론 + Wyckoff 흡수 단계를 숫자로 옮긴 상태 검색식.
    "accum": ("weak", "참고 · 2024년 이후만 유효 · 급등 신호 아님", (),
              "최근 120거래일 동안 세력(외국인·기관·기타법인 등 개인을 뺀 쪽)이 거래량의 5% 넘게 순매수하고 개인은 내다 판 종목 가운데, "
              "주가는 고저폭 60% 안에서 횡보하며(120일 변화 −15~+25%) 아직 튀지 않았고, 세력 분산비율 0.9 이상·개인 분산비율 0.3 이하(세력이 아직 안 팔았다), "
              "하락일 거래량이 상승일보다 적고, 종가가 세력 평단(순매수일 가중) 위 0~15% 인 날을 보여 줍니다(매일 날 수 있는 상태 검색식). "
              "외국인·기관이 산 대형주도 걸립니다 — 기타법인 매수는 대개 대주주·지주사 지분 확대나 자사주이니 공시로 주체를 확인하십시오.",
              "2021-03 이후 지금 상장된 종목, 월 1회 표본: 2024년 이후 시장 평균보다 3달 +2.2%p·6달 +5.6%p(t 2.5), 6달 뒤 오른 비율 57%. "
              "2021~2023년에는 3달 +0.6%p·6달 +2.2%p로 우연 범위였습니다(장세를 탑니다). 3달 안 2배 확률은 0.2% 로 아무 종목(1.2%)보다 낮습니다 — 덜 빠지고 꾸준한 쪽입니다. "
              "상장폐지 종목의 수급을 받을 수 없어 실제보다 좋게 나왔을 수 있습니다."),
    # 사용자 그림 둘(2026-10-07, README 「인위적으로 떨어뜨린 그림」 절). 사용자 요청으로 화면에 두되, 검정 평균이 시장 아래라 '주의'.
    "wave_collapse": ("bad", "주의 · 모양 보기용 · 검정 효과 없음", (),
                      "지난 1년 안에 여러 번 출렁이다 마지막 파동이 가장 높고 가장 크게 나온 뒤(앞 파동 폭 중앙값의 1.3배↑), 그 구조의 바닥 아래로 종가가 떨어진 '붕괴일'을 잡습니다. "
                      "붕괴일 뒤 120거래일 안에서 20일 폭이 15% 이내로 횡보하고 일봉 오실레이터가 양봉인 날을 보여 줍니다(매일 날 수 있는 상태 검색식).",
                      "2007년 이후 2,165건(상장폐지 포함): 이 매수 시점의 2달 뒤 시장초과는 2007~2019 +0.8%p, 2020년 이후 −0.8%p로 엇갈려 효과가 없었습니다. 오른 비율 41~47%."),
    "stairs_ready": ("weak", "참고 · 분출 전 매수 자리 · 앞뒤 기간 모두 유효", (),
                     "「계단식 매집봉 반복 뒤 1년 고점 돌파」의 장대양봉이 나오기 **전** 자리입니다. 1년 안에 매집봉 묶음이 3번 이상이고 바닥을 깨지 않은 종목 가운데, "
                     "아직 1년 최고 종가를 넘지 않았지만 그 85% 이상에 있고, 최근 10일 동안 매집봉이 없고 평균 거래대금이 평소(120일 중앙값)의 2배 이하로 말랐으며, "
                     "20일 고가/저가 폭이 15% 이내로 좁혀진 날을 보여 줍니다(매일 날 수 있는 상태 검색식).",
                     "2007년 이후(상장폐지 포함) 3,055건: 20일 안에 분출(매집봉으로 1년 고점 돌파)이 나는 비율 12~15%(아무 날 1.2%). "
                     "시장 평균보다 60일 IS +4.1%p(t 5.4)·OOS +3.3%p(t 3.7), 120일 +5.3%p·+4.6%p, 2020년 이후 해마다 플러스(2025년 +0.1%p). "
                     "다만 60일 뒤 오른 비율은 44~48%, 중앙값 −0.7~−2.3% — 소수의 분출이 평균을 만듭니다. 고점에 붙거나(7% 이내) 거래가 붙은 날은 분출 확률은 높아도 수익이 낮았습니다."),
    "crow_star": ("bad", "주의 · 모양 보기용 · 평균은 시장 아래", (),
                  "5일선 > 20일선 > 60일선 정배열이 흑삼병 전날부터 오늘까지 이어지는 가운데, 세 봉 연속 음봉(흑삼병 — 종가가 연달아 낮아지고 세 몸통 합 3% 이상) 바로 다음 날 "
                  "짧은 봉(몸통이 전일 종가 3% 이하이고 흑삼병 가장 큰 몸통의 60% 이하)이 양봉이거나 도지(십자·샛별형·짧은 꽉 찬 양봉)로 나오고, "
                  "그날 주봉·월봉 오실레이터가 양봉이거나 음봉이 줄어드는 중인 날을 잡습니다. 예: 듀켐바이오 2026-10-14(다음 날 장대양봉).",
                  "2007년 이후(상장폐지 포함) 14,426건: 2달 뒤 시장초과 2007~2019 +0.2%p(t −1.4, 효과 없음), 2020년 이후 −0.8%p(t −2.7) — 2022~2026 해마다 마이너스. 오른 비율 41%. "
                  "정배열 조건이 없을 때(2020년 이후 −0.9%p)보다 조금 덜 나쁠 뿐 시장 아래입니다."),
    "stairs_break": ("bad", "주의 · 모양 보기용 · 평균은 시장 아래", (),
                     "1년(250거래일) 안에 거래대금이 평소(120일 중앙값)의 10배 넘게 터지고 고가가 전일 종가보다 10% 넘게 올라간 매집봉이 "
                     "20거래일 넘게 떨어진 묶음으로 3번 이상 나왔고, 그 1년 저가가 그 전 1년 저가보다 낮지 않은(바닥을 깨지 않은) 종목이 "
                     "오늘 매집봉으로 1년 최고 종가를 넘긴 날(분출)을 찾습니다. 예: 에스엠벡셀 2026-10-15.",
                     "2007년 이후(상장폐지 포함) 4,700건: 시장 평균보다 60일 IS −1.4%p·OOS −1.1%p, 120일 IS −1.7%p·OOS −4.5%p(t −2.7), 120일 뒤 오른 비율 28~34%. "
                     "같은 1년 고점 돌파라도 매집봉 없이 조용히 넘은 경우(120일 +4.5%p·+8.0%p)가 훨씬 나았습니다. "
                     "60일 안에 +50% 를 한 번 찍는 비율은 24~26%(조용한 돌파 10~13%)로 더 크게 출렁이지만, 익절 20~50%·손절 10~20% 규칙을 걸어도 평균 −4~+1% 였습니다."),
    "spike_accum": ("bad", "주의 · 모양 보기용 · 검정 결과는 README", (),
                    "앞 40일 최저가 대비 40% 넘게 급등(거래량 3배↑)한 고점이 확정된 뒤 120거래일 안에 종가가 고점의 55% 아래로 떨어진 '반토막일'을 잡습니다. "
                    "그 뒤 250거래일 안, 종가가 이후 최저가의 1.35배 이내(바닥권)이고 최근 40일에 매집 봉(양봉·윗꼬리 3%↑·거래량 20일 평균 3배↑)이 2개 이상이며 20일선이 20일 전보다 낮지 않은 날을 보여 줍니다.",
                    "2007년 이후 6,786건(상장폐지 포함): 이 조건의 2달 뒤 시장초과는 2007~2019 +0.3%p, 2020년 이후 −1.8%p, 오른 비율 33~39%. "
                    "같은 바닥권에서 매집 봉이 하나도 없는 날(+0.5%p·+0.4%p)이 오히려 나았습니다 — 바닥의 윗꼬리 거래량 봉은 평균적으로 매집이 아니라 물량 소화였습니다."),
}
# 검색식 분류(사용자 요청 2026-10-02) — **로직 유형**으로 묶는다. 예전에는 성적 등급(추천/참고/주의)으로 묶어서
# 신고가 돌파와 이평선 검색식이 섞여 있었다. 모든 검색식이 정확히 한 묶음에 든다(test_home_ui).
SIGNAL_GROUPS = (
    # 로직 유형으로 묶는다(2026-10-02). 모든 검색식이 정확히 한 묶음에 든다(test_home_ui). 순서 = core.SIGNALS 순서.
    # 「오실레이터」 묶음 이름의 봉 단위는 화면이 tf 로 채운다(group_title) — 60분봉 패널이면 '오실레이터 · 60분봉'.
    ("신고가 돌파", ("win_combo", "best_combo", "lowvol_breakout", "win_tight", "bb40_combo", "bb_break")),
    ("이동평균선", ("ema_div", "gc")),
    ("눌림목 · 반등", ("kijun_pull", "algkkagi", "cafe_juryeok", "sik_pullback", "osd_vol")),
    ("재진입 (놓친 신호가 눌렸다 회복한 날)", ("reclaim_win", "reclaim_best", "reclaim_lowvol")),
    ("거래대금 · 수급", ("bottom_spike", "accum")),
    ("사용자 그림 (분출 전 수렴만 검정 통과)", ("wave_collapse", "spike_accum", "stairs_ready", "stairs_break", "crow_star")),
    ("오실레이터 · {tf} (매매 시점 참고)", ("stoch_turn_up", "osc_pos_d", "osc_shrink_d", "osc_shrink3_d", "osc_mtf")),
    ("오실레이터 · 주봉 (매매 시점 참고)", ("osc_up_w", "osc_pos_w", "osc_shrink_w", "osc_shrink3_w", "oscw_trend")),
    ("오실레이터 · 60분봉 실시간 (검색할 때 키움에서 새로 받음)", ("osc60_up", "osc60_pos", "osc60_shrink", "osc60_shrink3")),
)


def group_title(title: str, tf: str = "d") -> str:
    return title.format(tf=core.TFS.get(tf, "일봉"))
FIELD_HTML = {
    "volq": ('조용한 종목 기준 (0.3 = 변동성이 가장 낮은 30%)', 'step="any" min="0.05" max="1"'),
    "hh": ('신고가 기간 (거래일)', 'step="any" min="5"'),
    "cafe": ('횡보 전 최소 하락률 (%)', 'step="any"'),
    "algr": ('눌림 전 최소 상승률 (%)', 'step="any"'),
    "algp": ('최대 눌림 깊이 (%)', 'step="any"'),
    "rcw": ('신호 뒤 며칠 안에 회복 (거래일)', 'step="1" min="1" max="250"'),
    "rcd": ('눌림 깊이 — 종가가 신호가보다 이만큼 아래로 (%)', 'step="any" min="0" max="50"'),
    "rcv": ('그 사이 거래량 배수 (0 = 안 봄, 2 = 20일 평균의 2배인 날이 있어야)', 'step="any" min="0"'),
}
# 검색일(신호일) 종가 대비 그 뒤 종가의 수익률을 낼 기간(거래일) — 사용자 요청 2026-10-01. 6개월 = 120거래일.
HORIZONS = ((5, "5일 뒤"), (10, "10일 뒤"), (30, "30일 뒤"), (120, "6개월 뒤"))
TF_BAR = {"d": "일", "w": "주", "h": "봉"}


def horizon_label(h: int, tf: str) -> str:
    """'5일 뒤' 를 봉 단위에 맞춰 — 주봉 '5주 뒤', 60분봉 '5봉 뒤'(7봉 = 하루)."""
    if tf == "d":
        return dict(HORIZONS)[h]
    return f"{h}{TF_BAR[tf]} 뒤" + (f"(≈{h / 7:.0f}일)" if tf == "h" and h >= 7 else "")


def data_date() -> str:
    try:
        panel()
        st = _P()["stored"]
        return f"{st:%Y-%m-%d %H:%M}" if req_tf() == "h" else f"{st:%Y-%m-%d}"   # 저장 자료의 마지막 봉 — 붙여 둔 오늘 봉(잠정)은 따로 적는다
    except Exception:        # noqa: BLE001 — 패널이 아직 없어도 메뉴는 그린다
        return "—"


def update_bar() -> str:
    """데이터 기준일 + 업데이트 단추 + 진행 상태. 상태 줄은 요약의 첫 마디만 보이고 전체는 툴팁에 둔다."""
    with _UPDATE_LOCK:
        full, running = _UPDATE["log"], _UPDATE["running"]
    short = html.escape(full.split(" · ")[0]) if full else ""
    steps = steps_html()
    lv = live_status()
    live_chip = (f'<span class="chip live" title="검색에만 쓰는 오늘 봉입니다({lv["src"]} 시세 · {lv["n"]:,}종목). '
                 f'저장 자료와 차트·수급은 기준일 그대로입니다.">＋ 오늘 {lv["at"]} 시세{"" if lv["final"] else "(잠정)"}</span>'
                 if lv["day"] else "")
    if not is_owner():
        return (f'<span class="chip" title="시세·수급이 반영된 마지막 거래일">데이터 기준일 {data_date()}</span>{live_chip}'
                '<span class="chip" title="검색·차트는 볼 수 있고, 데이터 받기·관심 그룹 편집은 관리자만 합니다">보기 전용</span>')
    return f"""<span class="chip" title="시세·수급이 반영된 마지막 거래일">데이터 기준일 {data_date()}</span>{live_chip}
<button id="update-button" class="btn2" type="button" onclick="updateData()"{" disabled" if running else ""}>↻ 최신 데이터 받기</button>
<button id="update-fold" class="btn2" type="button" onclick="toggleSteps()" aria-controls="update-steps"{"" if steps else " hidden"}>▴ 접기</button>
<div id="update-status" class="note" role="status" title="{html.escape(full)}">{short}</div>
<div id="update-steps" class="upsteps">{steps}</div>
<script>
// 단계 표 접기(사용자 요청 2026-10-08) — 접은 상태는 이 브라우저에 기억하고, 새로 받기를 누르면 다시 펼친다
function stepsFolded(){{try{{return localStorage.getItem('flowapp.upfold')==='1';}}catch(e){{return false;}}}}
function applyFold(){{
  var box=document.getElementById('update-steps'), b=document.getElementById('update-fold'); if(!box||!b) return;
  var f=stepsFolded(); box.classList.toggle('folded', f); b.hidden=!box.innerHTML.trim();
  b.textContent=f?'▾ 단계 보기':'▴ 접기'; b.setAttribute('aria-expanded', String(!f));
}}
function toggleSteps(){{try{{localStorage.setItem('flowapp.upfold', stepsFolded()?'0':'1');}}catch(e){{}} applyFold();}}
var STEP_CLS={{"대기":"wait","진행":"run","완료":"ok","실패":"bad","건너뜀":"skip"}};
function esc(s){{return String(s==null?"":s).replace(/[&<>"]/g,c=>({{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}})[c]);}}
function renderSteps(d){{
  var box=document.getElementById('update-steps'); if(!box||!d.steps) return;
  var any=d.steps.some(s=>s.state!=="대기"); if(!any){{box.innerHTML="";applyFold();return;}}
  box.innerHTML=d.steps.map(s=>'<div class="us-row us-'+(STEP_CLS[s.state]||'wait')+'" title="'+esc(s.what)+'">'
    +'<b>'+esc(s.name)+'</b><span class="us-what">'+esc(s.what)+'</span>'
    +'<span class="us-state">'+esc(s.state)+(s.elapsed?' · '+esc(s.elapsed):'')+'</span>'
    +(s.detail?'<span class="us-detail">'+esc(s.detail)+'</span>':'')+'</div>').join('')
    +'<div class="us-total">전체 '+esc(d.total||'')+(d.done?'':' 경과')+'</div>';
  applyFold();
}}
function showStatus(msg, d){{ msg.textContent = String(d.status).split(' · ')[0]; msg.title = d.status; renderSteps(d); }}
function updateData(){{
  var msg = document.getElementById('update-status'), button=document.getElementById('update-button');
  button.disabled=true;msg.textContent='업데이트를 요청하고 있습니다…';
  try{{localStorage.setItem('flowapp.upfold','0');}}catch(e){{}} applyFold();
  fetch('/api/update', {{method:'POST'}}).then(r=>r.json()).then(d=>{{
    showStatus(msg, d);
    button.disabled=!d.done;
    if(!d.done) pollStatus(msg);
  }}).catch(e=>{{button.disabled=false;msg.textContent='업데이트 요청 실패: '+e;}});
}}
function pollStatus(msg){{
  setTimeout(()=>{{
    fetch('/api/update').then(r=>r.json()).then(d=>{{
      showStatus(msg, d);
      document.getElementById('update-button').disabled=!d.done;
      if(!d.done) pollStatus(msg);
    }}).catch(()=>{{document.getElementById('update-button').disabled=false;msg.textContent='상태 조회 실패. 업데이트는 서버에서 계속 진행될 수 있습니다.';}});
  }}, 1000);
}}
(function(){{var b=document.getElementById('update-button');if(b&&b.disabled){{pollStatus(document.getElementById('update-status'));}}}})();
applyFold();
</script>"""


def steps_html() -> str:
    """단계 표의 서버 쪽 첫 그림(업데이트 중에 페이지를 열어도 바로 보이게). 이후는 JS renderSteps 가 1초마다 다시 그린다."""
    steps, total = steps_view()
    if not any(x["state"] != "대기" for x in steps):
        return ""
    cls = {"대기": "wait", "진행": "run", "완료": "ok", "실패": "bad", "건너뜀": "skip"}
    rows = "".join(f'<div class="us-row us-{cls[x["state"]]}" title="{html.escape(x["what"])}"><b>{html.escape(x["name"])}</b>'
                   f'<span class="us-what">{html.escape(x["what"])}</span>'
                   f'<span class="us-state">{x["state"]}{" · " + x["elapsed"] if x["elapsed"] else ""}</span>'
                   + (f'<span class="us-detail">{html.escape(x["detail"])}</span>' if x["detail"] else "") + "</div>" for x in steps)
    with _UPDATE_LOCK:
        done = _UPDATE["done"] or not _UPDATE["running"]
    return rows + f'<div class="us-total">전체 {total}{"" if done else " 경과"}</div>'


def topbar(active: str, o: dict) -> str:
    """모든 화면 공통 상단 메뉴. active = home | backtest | compare | stock."""
    tabs = (("home", "/", "종목 찾기"), ("groups", "/groups", "관심 그룹"),
            ("backtest", "/backtest", "이 검색식 성적"), ("compare", "/compare", "검색식 비교"), ("picks", "/picks", "시간별 추천"), ("top3", "/top3", "오늘의 3종목"), ("track", "/track", "추천 추적")) + ((("watch", "/watch", "감시"),) if is_owner() else ()) + ((("settings", "/settings", "설정"),) if appsettings.enabled() else ())
    nav = "".join(f'<a href="{u}?{qs(o)}"{" class=on" if k == active else ""}>{t}</a>' for k, u, t in tabs)
    return (f'<header class="top"><a class="brand" href="/?{qs(o)}">수급분석 스캐너</a><nav>{nav}</nav>'
            f'<div class="right">{update_bar()}</div></header>')


# ★ = 승률이 높은 검색식(사용자 요청 2026-10-04). 기준: **2025년 이후**(2025-01-02~2026-10-01, 화면 기본 설정 — 거래대금 10억↑·수급 조건 켬)
#   신호일 종가에서 4주 뒤와 2개월 뒤에 **오른 비율이 둘 다 52% 이상**. 값 = (건수, 4주 오른 비율, 2개월 오른 비율, 2021~2024년 4주 오른 비율).
#   winners.py 사건표(cache/winners_events.pkl)에서 센 값 — 검정을 다시 하면 같이 고친다. 2021~2024년에는 모두 50% 아래였다(약세장 포함).
WIN_STAR_MIN = 52
WIN_STAR = {
    "win_combo": (496, 60, 60, 44),
    "best_combo": (1209, 52, 52, 48),
    "lowvol_breakout": (1217, 52, 54, 45),
    "bb40_combo": (115, 53, 56, 46),
    "cafe_juryeok": (5607, 56, 55, 45),
    "osc_shrink_w": (1315, 52, 54, 48),
    "osc_shrink3_w": (507, 53, 53, 43),
    "win_tight": (78, 58, 56, 51),
    "oscw_trend": (133, 62, 61, 60),
}
WIN_STAR_MIN_N = 60        # 2025년 이후 건수가 이보다 적으면 별표를 주지 않는다


# 검색식별 보유 기간별 승률(사용자 요청 2026-10-04 "검색식별로 가장 높은 승률을 가진 기간과 승률을 표시") — 2025년 이후, 화면 기본 설정(수급 조건 켬).
#   값 = (건수, (1주·2주·3주·4주·2개월·3개월 뒤 오른 비율 %)). winners 사건표에서 센 값 — test_보유기간별_승률은_사건표에서_센_값과_같다 가 대조한다.
WIN_HZ = ("1주", "2주", "3주", "4주", "2개월", "3개월")
WIN_RATES = {
    "win_combo": (496, (61, 61, 62, 60, 60, 67)),
    "best_combo": (1209, (54, 53, 52, 52, 52, 57)),
    "lowvol_breakout": (1217, (53, 52, 52, 52, 54, 59)),
    "bb_break": (1702, (51, 51, 49, 49, 48, 54)),
    "ema_div": (502, (49, 50, 50, 50, 50, 45)),
    "gc": (4912, (50, 49, 51, 51, 51, 51)),
    "kijun_pull": (1493, (48, 48, 49, 47, 48, 51)),
    "algkkagi": (2786, (49, 50, 51, 51, 52, 49)),
    "cafe_juryeok": (5607, (49, 51, 54, 56, 55, 54)),
    "sik_pullback": (1102, (45, 42, 42, 42, 39, 39)),
    "bottom_spike": (16, (50, 56, 50, 33, 27, 13)),
    "stoch_turn_up": (2591, (47, 47, 49, 49, 49, 48)),
    "osc_shrink_d": (6445, (48, 49, 48, 49, 50, 49)),
    "osc_shrink3_d": (2232, (47, 45, 45, 45, 49, 48)),
    "osc_mtf": (307, (49, 49, 49, 48, 48, 47)),
    "osc_up_w": (649, (48, 49, 47, 47, 53, 51)),
    "osc_shrink_w": (1315, (49, 49, 51, 52, 54, 54)),
    "osc_shrink3_w": (507, (49, 50, 54, 53, 53, 57)),
    "bb40_combo": (115, (49, 54, 55, 53, 56, 61)),
    "win_tight": (78, (60, 63, 64, 58, 56, 71)),
    "oscw_trend": (133, (54, 52, 58, 62, 61, 64)),
    "reclaim_win": (166, (54, 50, 51, 48, 51, 59)),
    "reclaim_best": (432, (53, 52, 50, 50, 48, 47)),
    "reclaim_lowvol": (497, (52, 50, 50, 50, 49, 49)),
}


# 수급 조건(개인 분산비율 ≤ 30%)을 걸기 전 → 건 뒤의 4주 시장초과(%p), 검색식별 (2026-10-05). 값 = (2021~2024 전체, 조건 뒤, 2025~ 전체, 조건 뒤).
#   winners 사건표에서 수급 값이 있는 사건만(2021-03~) 센 값 — test_수급조건_전후_수치는_사건표와_같다 가 대조한다. 30건 미만은 None.
#   ★ 저변동 신고가 계열은 2021~2024년에 조건을 걸면 **나빠졌다**(승률형 +2.0 → +0.7) — 여러 검색식의 중앙값만 보면 가려진다. 화면이 검색식마다 적는다.
FLOW_PAST = {
    "win_combo": (2.0, 0.7, 3.7, 4.6),
    "best_combo": (1.2, 0.9, 0.8, 2.8),
    "lowvol_breakout": (0.8, 0.5, 1.2, 3.0),
    "bb_break": (0.3, 1.2, 2.1, 3.1),
    "ema_div": (0.0, 0.3, 0.3, 1.2),
    "gc": (0.1, 0.7, 0.4, 1.0),
    "kijun_pull": (0.2, 0.6, 0.8, 1.3),
    "algkkagi": (0.0, 0.4, 0.6, 2.0),
    "cafe_juryeok": (0.1, 0.2, 0.0, 0.7),
    "sik_pullback": (-1.5, 0.6, -1.7, 1.4),
    "bottom_spike": (-1.8, None, -1.9, None),
    "stoch_turn_up": (0.2, 0.9, 0.4, 1.1),
    "osc_shrink_d": (0.0, 0.4, -0.2, 1.4),
    "osc_shrink3_d": (0.1, 0.8, -0.5, 0.9),
    "osc_mtf": (0.1, 1.1, 1.4, 2.6),
    "osc_up_w": (0.0, 0.9, 0.4, 3.1),
    "osc_shrink_w": (0.2, 0.9, 1.5, 1.4),
    "osc_shrink3_w": (0.4, 0.6, 1.6, 4.4),
    "bb40_combo": (2.3, 2.6, 4.7, 5.6),
    "win_tight": (2.2, 0.8, 1.8, 1.9),
    "oscw_trend": (2.2, 1.6, 2.5, 2.8),
    "reclaim_win": (0.1, -0.3, 0.6, 0.7),
    "reclaim_best": (0.1, 0.3, 1.7, 2.0),
    "reclaim_lowvol": (0.2, 0.0, 1.7, 1.8),
    "osd_vol": (0.8, 2.1, 3.6, 5.2),          # 2026-10-07 패널 실측(flow 자료 2021~): 조건을 켜면 건수가 3,636→177 · 812→44 로 95% 줄지만 남은 쪽이 낫다
}


def flow_past_note(kind: str) -> str:
    """이 검색식에서 수급 조건이 과거에 어땠는지 한 줄. 나빠진 기간이 있으면 그렇게 적는다."""
    v = FLOW_PAST.get(kind)
    if not v or None in v:
        return ""
    a0, a1, b0, b1 = v
    word = lambda x, y: "나빠짐" if y < x else "좋아짐"
    warn = a1 < a0 or b1 < b0
    return (f'<span class="note{" warn" if warn else ""}"> <b>이 검색식에서는</b> 조건을 걸면 4주 뒤 시장 대비 성적이 '
            f'2021~2024년 {a0:+.1f} → {a1:+.1f}%p(<b>{word(a0, a1)}</b>), 2025년 이후 {b0:+.1f} → {b1:+.1f}%p(<b>{word(b0, b1)}</b>)였습니다.'
            + (' 기간에 따라 방향이 달랐으니 끈 결과도 함께 보십시오.' if warn else '') + '</span>')


def win_best(kind: str):
    """(가장 승률이 높은 보유 기간, 그 승률, 건수) — 같은 값이면 짧은 기간. 자료가 없으면 None."""
    if kind not in WIN_RATES:
        return None
    n, r = WIN_RATES[kind]
    i = max(range(len(r)), key=lambda j: (r[j], -j))
    return WIN_HZ[i], r[i], n


def signal_card(o: dict) -> str:
    grade, tag, _, what, perf = SIGNAL_INFO.get(o["kind"], ("weak", "설명 없음", (), "", ""))
    star = ""
    if o["kind"] in WIN_STAR:
        n, u4, u8, past = WIN_STAR[o["kind"]]
        star = (f'<p class="perf star">★ 승률 높음 · 2025년 이후 {n:,}건 중 4주 뒤 {u4}%, 2개월 뒤 {u8}%가 올랐습니다(수급 조건을 켠 설정). '
                + (f'2021~2024년에도 4주 뒤 오른 비율이 {past}%였습니다.</p>' if past >= 50 else
                   f'2021~2024년에는 4주 뒤 오른 비율이 {past}%였습니다 — 장세를 탑니다.</p>'))
    hz = ""
    if o["kind"] in WIN_RATES:
        n, r = WIN_RATES[o["kind"]]
        lab, top, _ = win_best(o["kind"])
        cells = " · ".join(f'<b>{h} {v}%</b>' if h == lab else f'{h} {v}%' for h, v in zip(WIN_HZ, r))
        hz = (f'<p class="perf hz">보유 기간별 오른 비율 · {cells} — 가장 높은 기간 <b>{lab} {top}%</b> '
              f'(2025년 이후 {n:,}건{" · 건수가 적어 참고만" if n < WIN_STAR_MIN_N else ""}, 수급 조건을 켠 설정)</p>')
    tfnote = ("" if o.get("tf", "d") == "d" else
              f'<p class="perf tfnote"><b>{core.TFS[o["tf"]]}으로 계산 중</b> — 이 검색식의 지표(오실레이터·이동평균·신고가·볼린저)는 전부 {core.TFS[o["tf"]]} 기준이고, '
              f'「일봉」이라 적힌 것은 {core.TFS[o["tf"]]}을 뜻합니다. 아래 성적은 일봉으로 잰 값이며 {core.TFS[o["tf"]]} 성적은 아직 재지 않았습니다.</p>')
    return (tfnote + f'<p class="what">{html.escape(what)}<span class="badge {grade}">{html.escape(tag)}</span></p>'
            f'<p class="perf">과거 성적 · {html.escape(perf)}</p>{star}{hz}')


def cfg_form(o: dict) -> str:
    """검색식 고르기 + 그 검색식이 쓰는 설정만. 쓰지 않는 값은 hidden 으로 실어 URL 을 보존한다."""
    sig = ""
    for title, kinds in SIGNAL_GROUPS:         # 무엇을 찾는지(로직)로 묶는다. 성적 등급은 설명 카드의 배지 + 추천 표시로
        opt = "".join(
            f'<option value="{k}"{" selected" if k == o["kind"] else ""}>{"★ " if k in WIN_STAR else ""}{html.escape(label_of(k, o["tf"]))}'
            f'{" · {} {}%".format(*win_best(k)[:2]) if k in WIN_RATES else ""}'
            f'{" · 추천" if SIGNAL_INFO[k][0] == "good" else ""}</option>' for k in kinds)
        sig += f'<optgroup label="{html.escape(group_title(title, o["tf"]))}">{opt}</optgroup>'
    used = SIGNAL_INFO.get(o["kind"], ("", "", ()))[2]
    fields = "".join(
        f'<label>{FIELD_HTML[k][0]}<input type="number" name="{k}" value="{o[k]}" {FIELD_HTML[k][1]}></label>'
        for k in used)
    hidden = "".join(f'<input type="hidden" name="{k}" value="{o[k]}">' for k in FIELD_HTML if k not in used)
    # 기간은 단추(span)가 우선이고, 단추에 없는 값(주소로 직접 준 기간)은 이 두 칸이 지킨다
    hidden += f'<input type="hidden" name="days" value="{o["days"]}"><input type="hidden" name="unit" value="{o["unit"]}">'
    first, last = trading_dates()[0].date(), trading_dates()[-1].date()
    osc = o["kind"] in osc60.KINDS            # 60분봉 검색은 기간이 없다(최근 6봉) — 「검색」이 낡은 결과면 키움에서 새로 받는다(osc60_html)
    # 봉 단위(2026-10-06): 같은 검색식을 일봉·주봉·60분봉 패널에서 돌린다. 60분봉은 5분봉 저장소(약 1년)로 만든다.
    tfs = "" if osc else (
        # .span 클래스를 쓰지 않는다 — ui.js(compactPeriods)가 첫 .span 을 기간 묶음으로 잡아 단추를 바꾼다
        '<div class="tfbar" role="group" aria-label="봉 단위"><span class="span-t"><b>봉 단위</b> · 검색식을 어느 봉으로 계산할지</span>'
        '<div class="tf-row">' + "".join(
            f'<label class="pill"><input type="radio" name="tf" value="{t}"{" checked" if t == o["tf"] else ""}><span>{name}</span></label>'
            for t, name in core.TFS.items()) + '</div>'
        + ('<span class="note tfnote">60분봉 = 5분봉 저장소(2025-10~, 「최신 데이터 받기」 때 같이 갱신)로 만든 봉 · 오늘 장중 봉은 안 붙습니다 · 신고가 120 = 120봉(약 17거래일) · 거래대금 하한은 하루치로 환산</span>'
           if o["tf"] == "h" else ('<span class="note tfnote">주봉 = 일봉을 금요일 마감으로 합친 봉 · 신고가 120 = 120주 · 기간 단위 「일」은 그 주의 마지막 거래일로 셉니다</span>' if o["tf"] == "w" else ""))
        + '</div>')
    span = "" if osc else (
        tfs + '<div class="span" role="group" aria-label="검색 기간"><span class="span-t"><b>기간</b> · 이 기간 안에 한 번이라도 걸린 종목</span>'
        + "".join(f'<div class="span-row" data-unit="{u}">' + "".join(
            f'<label class="pill"><input type="radio" name="span" value="{n}{u}"'
            f'{" checked" if (n, u) == (o["days"], o["unit"]) and not o["start"] else ""}><span>{n}{name}</span></label>'
            for n in SPAN_CHOICES) + '</div>' for u, name in SPAN_ROWS)
        # 직접 기간(사용자 요청 2026-10-02): 날짜를 넣으면 위 단추 선택이 풀리고, 단추를 누르면 날짜가 지워진다(CFG_JS)
        + f'<div class="span-row range" data-unit="range"><span class="span-t"><b>직접</b></span>'
          f'<input type="date" name="start" value="{o["start"]}" min="{first}" max="{last}" aria-label="시작일">'
          f'<span class="span-t">~</span>'
          f'<input type="date" name="end" value="{o["end"]}" min="{first}" max="{last}" aria-label="끝일">'
          f'<span class="span-t">끝일을 비우면 오늘까지</span></div></div>')
    if osc:
        span += f'<input type="hidden" name="tf" value="{o["tf"]}">'
    keep = "".join(f'<input type="hidden" name="{k}" value="{o[k]}">' for k in FILTER_KEYS)   # 필터 막대가 꺼져 있을 때도 값을 지킨다
    return f"""<form class="cfg slim" id="cfg" method="get" action="/" onsubmit="cfgBusy(this)">
<div class="cfg-top"><label class="main">검색식<select name="kind" onchange="cfgKind(this)">{sig}</select></label>
<button type="submit" class="go">검색</button></div>
{hidden}{keep}<details class="card-text"><summary>검색식 설명과 과거 성적 보기</summary>{signal_card(o)}</details>
{span}
{f'<details{" open" if any(o[k] != DEFAULTS[k] for k in used) else ""}><summary>세부 설정 (바꾸지 않아도 됩니다)</summary><div class="row">{fields}</div></details>' if fields else ""}
</form>"""


FILTER_KEYS = ("minval", "surge", "flow", "frgn", "orgn")


def filter_bar(o: dict) -> str:
    """결과 위 「필터」(사용자 요청 2026-10-16) — 예전 검색 기본 조건(거래대금 하한·급등 제외·수급 조건)을 기본 끔으로 옮겼다.
    입력은 form="cfg" 로 검색 폼에 실려, 바꾸면 곧바로 다시 검색한다(검색 0.03초대). 폼의 hidden 보다 뒤에 오므로 이 값이 이긴다."""
    if o["kind"] in osc60.KINDS:                 # 60분봉 실시간 검색은 패널을 쓰지 않는다 — 필터 해당 없음
        return ""
    chk = lambda k, label, tip="": (f'<input type="hidden" name="{k}" value="0" form="cfg"><label class="chk"{f" title={chr(34)}{tip}{chr(34)}" if tip else ""}>'   # noqa: E731
                                    f'<input type="checkbox" name="{k}" value="1" form="cfg"{" checked" if o[k] else ""}> {label}</label>')
    surge = ("" if o["kind"] in core.NO_SURGE_EXCLUDE else chk("surge", "급등일 빼기", "거래량 3배·5% 이상 급등한 날의 신호는 뺍니다"))
    on = sum(bool(o[k]) for k in FILTER_KEYS)
    return (f'<div class="filterbar" role="group" aria-label="결과 필터"><span class="filter-t"><b>필터</b>{f" · {on}개 켬" if on else " · 없음"}</span>'
            f'<label class="minval">거래대금 ≥ <input type="number" name="minval" value="{o["minval"]:g}" step="any" min="0" form="cfg" aria-label="최소 거래대금(20일 평균, 억 원)">억</label>'
            + surge
            + chk("flow", f"수급 조건 · 개인 분산 {core.IND_DISP_MAX:.0%} 이하", "개인 추정 보유가 고점의 30% 아래로 줄어든 날만 — 2025년 이후 19개 검색식 중 18개에서 4주 뒤 성적이 좋아졌습니다")
            + chk("frgn", "외국인 5일 순매수", f"최근 5일 외국인 순매수 ÷ 거래량 {core.FRGN5_MIN:.0%} 이상인 날만")
            + chk("orgn", "기관 5일 순매수", f"최근 5일 기관 순매수 ÷ 거래량 {core.ORGN5_MIN:.0%} 이상인 날만")
            + (f'<a class="filter-reset" href="/?{qs(dict(o, minval=0.0, surge=0, flow=0, frgn=0, orgn=0))}">모두 끄기</a>' if on else "")
            + '<span class="note">승률·★ 수치는 거래대금 10억↑·급등일 빼기·수급 조건을 켜고 잰 값입니다.</span>'
            + (f'<details class="filter-past"><summary>이 검색식에서 수급 조건을 걸면 성적이 어떻게 바뀌었나</summary>{past}</details>' if (past := flow_past_note(o["kind"])) else "")
            + '</div>'
            # 바꾸면 곧바로 다시 검색 — 숫자 칸은 Enter·칸을 벗어날 때(change)
            + "<script>document.querySelectorAll('.filterbar input[form=cfg]').forEach(function(i){i.addEventListener('change',function(){"
              "var f=document.getElementById('cfg');if(f){f.requestSubmit?f.requestSubmit():f.submit();}});});</script>")


# 검색식을 바꾸면 설명·설정칸만 새로 그리고(검색은 하지 않는다), 「검색」 단추를 눌러야 결과가 바뀐다 — 사용자 요청 2026-10-01.
CFG_JS = """<script>
function cfgStale(){ var r=document.getElementById('res'), n=document.getElementById('stale-note');
  if(r) r.classList.add('stale'); if(n) n.hidden=false; }
function cfgKind(sel){
  var f=sel.form;
  fetch('/api/form?'+new URLSearchParams(new FormData(f))).then(function(r){ if(!r.ok) throw Error(r.status); return r.text(); })
    .then(function(h){ var t=document.createElement('template'); t.innerHTML=h.trim(); f.replaceWith(t.content.firstChild);
      cfgStale(); var s=document.querySelector('#cfg select'); if(s) s.focus(); })
    .catch(function(){ f.submit(); });          // 폼을 못 받으면 예전처럼 바로 검색한다
}
function cfgBusy(f){ var b=f.querySelector('.go'); if(b){ b.dataset.t=b.textContent; b.textContent='검색 중…'; b.disabled=true; } }
document.addEventListener('change', function(e){
  var f=e.target.closest && e.target.closest('#cfg'); if(!f) return;
  if(e.target.name==='span'){ f.querySelectorAll('input[type=date]').forEach(function(d){ d.value=''; }); }       // 단추가 이긴다
  if(e.target.type==='date' && e.target.value){ f.querySelectorAll('input[name=span]').forEach(function(r){ r.checked=false; }); }
  if(e.target.name!=='kind') cfgStale(); });
document.addEventListener('input', function(e){
  if(e.target.type==='number' && e.target.closest && e.target.closest('#cfg')) cfgStale(); });
window.addEventListener('pageshow', function(){            // 뒤로 가기로 돌아왔을 때 단추가 잠긴 채 남지 않게
  var b=document.querySelector('#cfg .go'); if(b && b.disabled){ b.disabled=false; b.textContent=b.dataset.t||'검색'; } });
</script>"""
STALE_NOTE = ('<p id="stale-note" class="stale-note" role="status" hidden>조건을 바꿨습니다 — '
              '<b>검색</b> 단추를 눌러야 아래 결과가 바뀝니다.</p>')


def last_close() -> dict:
    """종목 → (마지막 종가, 그 날짜). 패널이 바뀔 때만 다시 만든다."""
    p, P = panel(), _P()
    if P.get("last_at") != P["at"] or "last" not in P:
        t = p.drop_duplicates("Code", keep="last")
        P["last"] = dict(zip(t.Code.astype(str), zip(t.close.astype(float), t.dt)))
        P["last_at"] = P["at"]
    return P["last"]


def forward_tables(cand) -> tuple[dict, dict]:
    """후보 행마다 HORIZONS 뒤의 (수익률, 종가). 검색일(신호일) 종가 대비이고, 아직 그 기간이 안 지났으면 NaN.
    core.forward_return 과 같은 값이다(test_osc) — 패널 배열을 한 번만 만들어 두고 쓴다."""
    arr = derived("arrays", core.panel_arrays)
    i = cand.index.to_numpy()
    fpx = {h: pd.Series(core.forward_close(arr, i, h), index=cand.index) for h, _ in HORIZONS}
    base = arr[1][i]
    return {h: fpx[h] / base - 1 for h in fpx}, fpx


def candidate_rows(cand, fwd: dict | None = None, fpx: dict | None = None) -> list[dict]:
    """신호 행(최신순) → 종목당 한 줄: 가장 최근 신호일·그날 종가·기간 내 횟수 + 현재가·신호 후 수익률.
    fwd = {거래일 수: 신호 행 라벨 → 그만큼 뒤의 수익률(비율)} 을 주면 "뒤"[h] 에 %로 싣는다. 아직 그 기간이 안 지났으면 NaN.
    fpx = 같은 모양의 '그만큼 뒤의 종가' → "뒤가"[h]. 상태 검색식(상태끝 열)이면 "끝" = 마지막으로 그 상태였던 날."""
    last, rows = last_close(), []
    fwd, fpx = fwd or {}, fpx or {}
    codes = cand.종목코드.astype(str)
    times = codes.value_counts()
    for r in cand[~codes.duplicated().to_numpy()].itertuples():      # 종목마다 가장 최근 신호 행만 돈다(신호가 수만 건일 수 있다)
        code = str(r.종목코드)
        now, now_dt = last.get(code, (float("nan"), None))
        d = {"코드": code, "종목명": str(r.종목명), "시장": str(r.시장), "신호일": r.발생일,
             "신호가": float(r.종가), "현재가": now, "횟수": int(times[code]),
             "수익률": (now / float(r.종가) - 1) * 100 if r.종가 else float("nan"),
             "거래대금": float(r.거래대금20일) / 1e8,
             "개인분산": float(getattr(r, "개인분산", float("nan"))) * 100,
             "뒤": {h: float(f.get(r.Index, float("nan"))) * 100 for h, f in fwd.items()},
             "뒤가": {h: float(f.get(r.Index, float("nan"))) for h, f in fpx.items()},
             "끝": getattr(r, "상태끝", None)}
        rows.append(d)
    return rows


def _ret_cell(v: float, cls: str = "", px: float | None = None) -> str:
    """수익률 칸. px(그날 종가)를 주면 수익률 아래에 작게 적는다."""
    c = "pos" if v > 0 else "neg" if v < 0 else ""
    sub = f'<span class="c2">{px:,.0f}</span>' if px is not None and px == px else ""
    return f'<td class="{cls} {c}">{v:+.1f}%{sub}</td>' if v == v else f'<td class="{cls} muted">—</td>'


THEME_TOP = 2        # 표 한 칸에 적는 테마 수 — 나머지는 '외 N'(마우스를 올리면 전부)
THEME_CHIPS = 14     # 결과 위에 늘어놓는 '겹치는 테마' 단추 수


def theme_cell(code: str) -> tuple[str, str]:
    """(tr 에 붙일 data-th 속성, 테마 칸). 좁은 테마 먼저 THEME_TOP 개만 적고 전부는 title 에 싣는다."""
    th = themes.of(code)
    if not th:
        return "", '<td class="th hide-m muted">—</td>'
    names = [html.escape(n) for _, n in th]
    rest = f" 외 {len(th) - THEME_TOP}" if len(th) > THEME_TOP else ""
    second = f'<span class="c2">{names[1]}{rest}</span>' if len(th) > 1 else ""
    return (f' data-th=" {" ".join(str(no) for no, _ in th)} "',
            f'<td class="th hide-m" title="{" · ".join(names)}">{names[0]}{second}</td>')


def theme_bar(codes) -> str:
    """결과 종목들에 겹치는 테마 단추 줄 — 누르면 그 테마 종목만 남긴다(THEME_JS). 겹치는 테마가 없으면 빈 문자열."""
    hit = themes.tally(codes)[:THEME_CHIPS]
    if not hit:
        return ""
    return ('<div class="thbar" id="thbar"><span class="lab">겹치는 테마</span>'
            + "".join(f'<button type="button" data-no="{no}" aria-pressed="false">{html.escape(nm)} <b>{k}</b></button>'
                      for no, nm, k in hit)
            + '<span class="note" id="thbar-msg" role="status"></span></div>')


def defense_line(code: str) -> str:
    """종목 화면 머리의 「폭락 방어력」 줄(60일 베타·하락일 방어, `defense.py`). 실패해도 화면은 그대로 낸다."""
    try:
        df = panel("d")
        P = _P("d")
        return defense.line(defense.get(df, P["stored"], (P["stored"], P["at"])), code)
    except Exception as e:                                       # noqa: BLE001
        print(f"  ⚠ 방어력 계산 실패 — {e!r}"[:160], file=sys.stderr, flush=True)
        return ""


def theme_line(code: str) -> str:
    """종목 화면 머리의 테마 줄."""
    th = themes.of(code)
    if not th:
        return ""
    return ('<p class="themes"><span class="lab">테마</span>'
            + "".join(f'<span class="chip">{html.escape(n)}</span>' for _, n in th)
            + f'<span class="note">네이버 증권 분류 · {html.escape(themes.load()["at"])} 기준</span></p>')


THEME_JS = """<script>
(function(){
  var bar=document.getElementById('thbar'); if(!bar) return;
  var msg=document.getElementById('thbar-msg'), rows=document.querySelectorAll('#res tbody tr');
  bar.addEventListener('click', function(e){
    var b=e.target.closest('button'); if(!b) return;
    var on=b.getAttribute('aria-pressed')!=='true', n=0;
    bar.querySelectorAll('button').forEach(function(x){ x.setAttribute('aria-pressed','false'); });
    if(on) b.setAttribute('aria-pressed','true');
    var key=' '+b.dataset.no+' ';
    rows.forEach(function(r){ var show=!on||(r.dataset.th||'').indexOf(key)>=0; r.hidden=!show; if(show) n++; });
    msg.textContent = on ? n+'종목만 표시 중 — 다시 누르면 전체' : '';
  });
})();
</script>"""


OSC60_JS = """<script>
(function(){
  // 받기는 「검색」을 누른 요청이 서버에서 시작한다(osc60.should_start). 화면은 진행 상황만 보여 주고 끝나면 다시 그린다.
  var watching=false, msg=document.getElementById('osc60-msg'), res=document.getElementById('res');
  function poll(){ fetch('/api/osc60').then(r=>r.json()).then(show).catch(function(){ setTimeout(poll,3000); }); }
  function show(d){
    if(d.running){ watching=true; if(res) res.classList.add('stale');
      msg.textContent='60분봉을 새로 받는 중… '+d.done+' / '+d.total+'종목'+(d.fail?' (실패 '+d.fail+')':'')+' — 끝나면 결과가 저절로 바뀝니다';
      setTimeout(poll,1500); }
    else if(watching){ location.reload(); }          // 방금 끝났다 → 결과를 다시 그린다
  }
  poll();                                             // 다른 창에서 이미 돌리고 있어도 진행 상황을 이어서 보여 준다
})();
</script>"""


STATIC_FILES = {n: Path(__file__).resolve().parent / "static" / n for n in ("lwc.js", "chart.js", "ui.css", "ui.js")}   # 화이트리스트 — 경로 조작 차단
OSC60_SEC = 0.16      # 종목당 걸리는 시간(초) — osc60.PACE 0.143 에 429 재시도 3% 를 더한 실측(6.3종목/초, 2026-10-01)
OSC60_BAR = {"osc60_pos": "양봉이 시작된 봉", "osc60_shrink3": "3칸째 봉"}     # 표의 '사건 봉' 제목(나머지는 '바뀐 봉')


def osc60_codes(o: dict) -> list[str]:
    """60분봉 검색 대상: 마지막 거래일에 거래대금 하한을 넘는 종목."""
    p = panel()
    last = p[p.dt == p.dt.max()]
    return sorted(last.Code[last.val20 >= o["minval"] * 1e8].astype(str))


def osc60_html(o: dict) -> bytes:
    """60분봉 오실레이터 검색 화면 — 「검색」을 누르면 저장된 결과가 낡았을 때만 키움에서 새로 받는다(osc60.should_start).
    따로 있던 「60분봉 검색」 단추는 합쳤다(사용자 요청 2026-10-02 — 검색식 목록과 단추가 둘이라 헷갈렸다)."""
    res, codes = osc60.load(), osc60_codes(o)
    if osc60.should_start(res, len(codes)):
        osc60.start(codes)
    st = osc60.status()
    uni = core.universe().set_index("Code")
    if res:
        mine = sorted((r for r in res["rows"] if r["kind"] == o["kind"]), key=lambda r: r["t"], reverse=True)
        tr = []
        has_th = bool(themes.load()["by_code"])
        for r in mine:
            ret = (r["now"] / r["px"] - 1) * 100 if r["px"] else float("nan")
            t = r["t"]
            th_attr, th_td = theme_cell(r["code"]) if has_th else ("", "")
            tr.append(f'<tr{th_attr}><td class="k"><a href="/stock/{r["code"]}?{qs(o)}">{html.escape(str(uni.Name.get(r["code"], r["code"])))}</a>'
                      f'<span class="c2">{r["code"]} · {html.escape(str(uni.Market.get(r["code"], "")))}</span></td>{th_td}'
                      f'<td>{t[4:6]}-{t[6:8]} {t[8:10]}:{t[10:12]}</td><td class="hide-m">{r["px"]:,.0f}</td>'
                      f'<td>{r["now"]:,.0f}</td>{_ret_cell(ret, "ret")}</tr>')
        head = f'{len(mine)}종목 · {res["at"]} 검색 ({res["total"]:,}종목 조회, {res["sec"]}초)'
        table = ((theme_bar(r["code"] for r in mine) if has_th else "")
                 + '<div class="wrap wide"><table><thead><tr><th class="k">종목</th>'
                 + ('<th class="th hide-m">테마</th>' if has_th else "")
                 + f'<th>{OSC60_BAR.get(o["kind"], "바뀐 봉")}(시작 시각)</th>'
                 '<th class="hide-m">그 봉 종가</th><th>마지막 60분봉 종가</th><th>그 뒤 수익률</th></tr></thead>'
                 f'<tbody>{"".join(tr)}</tbody></table></div>' if tr else
                 '<div class="empty">이번 검색에서는 해당 종목이 없었습니다.</div>')
    else:
        head, table = "아직 결과가 없습니다", '<div class="empty">60분봉을 받는 중입니다. 끝나면 결과가 저절로 나옵니다.</div>'
    n = len(codes)
    body = f"""{topbar("home", o)}
<div class="srch"><input id="q" autocomplete="off" placeholder="종목명이나 코드로 바로 찾기 — 예: 삼성전자 · 005930 · 하이닉스">
  <div class="hits" id="hits"></div></div>
<p class="lead">60분봉 검색식은 <b>검색</b>을 누를 때 결과가 {osc60.FRESH_MIN}분보다 오래됐으면 키움에서 새로 받습니다
({n:,}종목 · 약 {max(1, round(n * OSC60_SEC / 60))}분). 장 마감 뒤에 받은 결과는 다음 장까지 그대로 씁니다.</p>
<section class="sig">{cfg_form(o)}</section>
{STALE_NOTE}
<div class="inline" style="margin-top:16px"><span id="osc60-msg" class="note" role="status">{html.escape(st["msg"]) if st["msg"] and not st["running"] else ""}</span></div>
<div id="res"><div class="res-head"><h2>{html.escape(head)}</h2></div>
{table}</div>
<p class="note">분봉은 저장해 두지 않아서 검색할 때마다 키움에서 종목별로 새로 받습니다. 결과는 검색한 시각 기준이며,
60분봉 검색식 네 개(양봉·양봉 전환·음봉 축소 시작·음봉 3칸 축소)가 한 번의 검색 결과를 같이 씁니다. 과거 성적은 낼 수 없습니다.</p>
{SEARCH_JS}{CFG_JS}{OSC60_JS}{THEME_JS}"""
    return page("수급분석 스캐너", body)


def watch_add_form(o: dict) -> str:
    """검색 결과 아래의 「이 검색식 감시」 — 지금 화면의 검색식·조건 그대로 감시 목록에 넣는다(관리자만)."""
    if not is_owner() or o["kind"] in osc60.KINDS:
        return ""
    return ('<div class="watch-add">' + _watch_form({"act": "add", **{k: o[k] for k in WATCH_KEYS}},
                                                 "<button>🔔 이 검색식 감시</button>")
            + '<span class="note"> 장중에 이 검색식에 새로 걸리는 종목을 텔레그램으로 받습니다 — <a href="/watch">감시 화면</a>에서 시작·중지합니다.</span></div>')


def board_html(o: dict) -> str:
    """전광판 — 외국인·기관 5일 순매수대금 상위 3(코스피·코스닥 따로, 2026-10-16). 집계 전이면 자리만 둔다."""
    try:
        d = board.data()
    except Exception as e:                      # 전광판 때문에 검색 화면이 죽으면 안 된다
        return f'<div class="board"><p class="board-empty">전광판 집계 실패 — {html.escape(str(e)[:80])}</p></div>'
    if not d["boards"]:
        return '<div class="board"><p class="board-empty">수급 원본이 없습니다</p></div>'
    tail = qs(o)

    def col(mk, who):
        li = "".join(f'<li><a href="/stock/{c}?{tail}">{html.escape(str(n))}</a><b>{board.eok(v)}</b></li>' for c, n, v in d["boards"][mk][who])
        return f'<ol>{li or "<li>—</li>"}</ol>'
    rows = "".join(f'<tr><th>{lab}</th><td>{col("KOSPI", who)}</td><td>{col("KOSDAQ", who)}</td></tr>'
                   for who, lab in (("frgn", "외국인"), ("orgn", "기관")))
    return (f'<div class="board" title="최근 5거래일 순매수 수량 × 그날 종가의 합(키움 수급 원본은 수량만 있어 종가로 근사)">'
            f'<div class="board-head"><strong>전광판</strong><span>5일 순매수대금 · {d["day"]:%m/%d} 기준</span></div>'
            f'<table><thead><tr><th></th><th>코스피</th><th>코스닥</th></tr></thead><tbody>{rows}</tbody></table></div>')


def index_html(o: dict) -> bytes:
    if o["kind"] in osc60.KINDS:
        return osc60_html(o)
    _, _, cand = scan(o)
    if o["tf"] == "d" and live_due():   # 결과는 일단 지금 패널로 그리고, 오늘 봉이 붙으면 화면이 다시 그린다(LIVE_JS). 주봉·60분봉은 저장분까지
        live_start()
    state = o["kind"] in core.OSC_STATE          # 상태 검색식 — 기간 안에 하루라도 양봉이었던 종목(발생일 = 양봉 시작일)
    up = up_days(o) if o["kind"] in core.REGIME_KINDS else None
    period, end = period_label(o), trading_dates()[-1]
    n_stock = 0
    if cand.empty and up is not None and not up.iloc[-1]:
        # 조합 1위는 시장 상승국면에서만 신호를 낸다 — 0건이 고장으로 보이지 않게 이유를 적는다
        since = up[up].index.max()
        table = ('<div class="empty">지금은 <b>시장 하락국면</b>이라 이 검색식은 쉬는 중입니다 '
                 f'(유니버스 동일가중 지수가 120일 평균 아래 · 마지막 상승국면 {since:%Y-%m-%d}). '
                 '국면이 돌아오면 다시 후보가 나옵니다.</div>')
    elif cand.empty:
        w = wider(o)
        more = (f' <a href="/?{qs(w)}"><b>{period_label(w)}로 넓혀 보기 →</b></a>' if w else
                " 세부 설정에서 최소 거래대금을 낮춰 보십시오.")
        what = "오실레이터가 양봉이었던 종목이" if state else "이 검색식에 걸린 종목이"
        table = f'<div class="empty">{period} 안에는 {what} 없습니다.{more}</div>'
    else:
        rows = candidate_rows(cand, *forward_tables(cand))
        cap = len(rows) if state else CAP        # 양봉 검색은 전부 보여 준다(자르면 오래 양봉인 종목이 빠진다)
        n_stock, shown, tr = len(rows), rows[:cap], []
        th_info = themes.load()
        has_th = bool(th_info["by_code"])
        with _LIVE_LOCK:
            live_day, soft = _LIVE["day"], bool(_LIVE["at"] is not None and _LIVE["at"].time() < LIVE_FINAL)
        for d in shown:
            th_attr, th_td = theme_cell(d["코드"]) if has_th else ("", "")
            u = f"/stock/{d['코드']}?{qs(o)}"
            ret = d["수익률"]
            cls = "pos" if ret > 0 else "neg" if ret < 0 else ""
            ret_s = f'{ret:+.1f}%' if ret == ret else "—"
            now_s = f'{d["현재가"]:,.0f}' if d["현재가"] == d["현재가"] else "—"
            if state:
                tail = " · 지금도 양봉" if d["끝"] == end else f' · {d["끝"]:%m-%d} 까지 양봉'
            else:
                tail = f' · {d["횟수"]}회' if d["횟수"] > 1 else ""
            if soft and d["신호일"] == live_day:        # 장중에 만든 오늘 봉에서 난 신호 — 마감 때 사라질 수 있다
                tail += " · 장중 잠정"
            tr.append(
                f'<tr{th_attr} data-search="{html.escape(d["종목명"] + " " + d["코드"], quote=True)}" '
                f'data-date="{d["신호일"]:%Y%m%d}" data-ret="{ret if ret == ret else ""}" data-value="{d["거래대금"]}">'
                f'<td class="k"><a href="{u}">{html.escape(d["종목명"])}</a>'
                f'<span class="c2">{d["코드"]} · {html.escape(d["시장"])}</span></td>{th_td}'
                + (f'<td>{d["신호일"]:%m-%d %H:%M}<span class="c2">{d["신호일"]:%Y}{tail}</span></td>' if o["tf"] == "h" else
                   f'<td>{d["신호일"]:%m-%d}<span class="c2">{d["신호일"]:%Y}{tail}</span></td>')
                + f'<td class="hide-m">{d["신호가"]:,.0f}</td><td>{now_s}</td>'
                f'<td class="ret {cls}">{ret_s}</td>'
                f'<td class="hide-m">{d["거래대금"]:,.0f}</td>'
                + (f'<td class="hide-m{" low" if d["개인분산"] <= core.IND_DISP_MAX * 100 else ""}">{d["개인분산"]:.0f}%</td>'
                   if d["개인분산"] == d["개인분산"] else '<td class="hide-m muted">—</td>')
                + "".join(_ret_cell(d["뒤"][h], "hide-m fw", d["뒤가"][h]) for h, _ in HORIZONS) + '</tr>')
        more = ("" if n_stock <= cap else
                f'<p class="note">{n_stock:,}종목 중 최신 {CAP:,}종목만 표시합니다.</p>')
        day = "양봉 시작일" if state else "검색일"
        table = ((theme_bar(d["코드"] for d in shown) if has_th else "")
                 + '<div class="wrap wide"><table><thead><tr><th class="k">종목</th>'
                 + ('<th class="th hide-m">테마</th>' if has_th else "")
                 + f'<th>{"양봉 시작일" if state else "검색일 (신호가 난 날)"}</th>'
                 f'<th class="hide-m">{day} 종가</th><th>현재가</th><th>지금까지</th>'
                 '<th class="hide-m">하루 거래대금(억)</th><th class="hide-m">개인 분산</th>'
                 + "".join(f'<th class="hide-m fw">{horizon_label(h, o["tf"])}</th>' for h, _ in HORIZONS)
                 + f'</tr></thead><tbody>{"".join(tr)}</tbody></table></div>'
                 f'<details class="res-explain"><summary>표 읽는 방법 · 데이터 기준</summary><p class="note"><b>{day}</b> = 기간 안에서 이 검색식에 걸린 가장 최근 날입니다. '
                 f'<b>지금까지</b> = {day} 종가에서 데이터 기준일 종가까지. '
                 f'<b>개인 분산</b> = {"마지막으로 양봉이던 날" if state else day}의 개인 분산비율(개인 보유가 고점의 몇 %인지 — 낮을수록 개인이 판 종목, '
                 f'{core.IND_DISP_MAX:.0%} 이하가 「수급 조건」). 수급 자료가 아직 없는 오늘 봉은 직전 값입니다. '
                 f'<b>5일·10일·30일·6개월 뒤</b> = {day}에서 5·10·30·120거래일 뒤의 종가(작은 글씨)와, {day} 종가 대비 수익률입니다. '
                 '아직 그 기간이 지나지 않았으면 —로 둡니다(30일·6개월 뒤를 보려면 기간을 주·월 단위로 넓히세요). 빨강은 상승, 파랑은 하락입니다.</p>'
                 + (f'<p class="note"><b>테마</b> = 네이버 증권의 테마 분류입니다({html.escape(th_info["at"])} 기준 · 「최신 데이터 받기」 때 같이 갱신). '
                    f'범위가 좁은 테마부터 {THEME_TOP}개를 적고, 칸에 마우스를 올리면 전부 보입니다. '
                    '<b>겹치는 테마</b>는 결과에 몰린 테마부터 놓았고(숫자 = 결과 안의 종목 수), 누르면 그 테마 종목만 남습니다. 지금 기준 분류라 검색일 당시와 다를 수 있습니다.</p>'
                    if has_th else "")
                 + ('<p class="note">이 검색식은 <b>기간 안에 하루라도 오실레이터가 양봉이었던 종목</b>을 보여 줍니다. '
                    '양봉 시작일은 음봉에서 양봉으로 바뀐 날(주봉은 그 주의 마지막 거래일)이고, 그 아래에 지금도 양봉인지 적습니다. '
                    '최근에 바뀐 종목이 위에 옵니다.</p>'
                    if state else "") + more + '</details>')
    count = f"{n_stock}종목" + (f" (신호 {len(cand)}건)" if len(cand) != n_stock else "")
    body = f"""{topbar("home", o)}
<div class="workspace-head"><div><h1>종목 찾기</h1><p>수급과 가격 흐름으로 후보를 찾고, 차트로 확인하세요.</p></div>
{board_html(o)}
<div class="srch"><input id="q" autocomplete="off"
  placeholder="종목명 또는 코드 검색"><span class="search-key">/</span>
  <div class="hits" id="hits"></div></div></div>
<div id="recent-stocks" class="recent-stocks" hidden></div>
<section class="sig">{cfg_form(o)}</section>
{STALE_NOTE}
<div class="stats-row"><div class="stat"><span>검색 결과</span><strong>{n_stock:,}<small>종목</small></strong></div>
<div class="stat"><span>검색 기간</span><strong>{html.escape(period)}</strong></div>
<div class="stat"><span>시세 데이터 기준일</span><strong>{html.escape(data_date())}</strong></div></div>
<div id="res"><div class="res-head"><h2>{period}{" 안에 양봉이었던 종목" if state else ""} · {count}</h2>{live_note().replace('role="status"', 'role="status" data-run="1"') if live_status()["running"] else live_note()}</div>
{filter_bar(o)}
<div class="results-tools"><input id="results-filter" type="search" placeholder="결과에서 종목명·코드 찾기" aria-label="검색 결과 필터">
<select id="results-sort" aria-label="결과 정렬"><option value="date">최근 신호순</option><option value="ret">수익률 높은순</option><option value="value">거래대금 높은순</option><option value="name">이름순</option></select>
<span id="results-count" role="status"></span><label class="detail-toggle"><input id="results-detail" type="checkbox">신호 이후 수익률 보기</label></div>
<div class="empty" id="results-empty" hidden>일치하는 종목이 없습니다. 필터를 바꿔보세요.</div>
{table}</div>
{LIVE_HELP if live_status()["day"] else ""}
{watch_add_form(o)}
<p class="note">이 목록은 조건에 걸린 종목일 뿐 매수 추천이 아닙니다. 가장 좋은 검색식도 건별로는 절반쯤 집니다 —
<a href="/backtest?{qs(o)}"><b>이 검색식의 과거 성적</b></a>을 먼저 확인하고, 여러 종목에 나눠서 판단하십시오.</p>
{SEARCH_JS}{CFG_JS}{THEME_JS}{LIVE_JS}"""
    return page("수급분석 스캐너", body)


# ─────────────────────────── 실시간 감시 (watch.py) — 사용자 요청 2026-10-05

WATCH_KEYS = ("kind", "minval", "volq", "hh", "cafe", "algr", "algp", "surge", "flow", "frgn", "orgn", "rcw", "rcd", "rcv")   # 감시가 기억하는 검색 설정(기간은 아니다 — 늘 '오늘')
WATCH_REFRESH_TIMEOUT = 600


def watch_opts(o: dict) -> dict:
    """검색 설정에서 감시가 쓸 것만. 기간은 감시가 정한다(그날 하루)."""
    return {k: o[k] for k in WATCH_KEYS}


def watch_label(o: dict) -> str:
    """알림 머리말 — 검색식 이름 + 기본값과 다른 조건."""
    extra = [t for k, t in (("flow", "수급 조건"), ("frgn", "외국인"), ("orgn", "기관")) if o.get(k)]
    return label_of(o["kind"]) + (f" [{' · '.join(extra)}]" if extra else "")


def watch_hits(wo: dict, day) -> list[dict]:
    """감시 항목 하나의 설정으로 day 하루에 걸린 종목. 화면 검색과 같은 경로(scan)다 — 기간만 그날 하루로 좁힌다.
    상태 검색식(양봉 상태)은 그 상태가 **시작된 날**만 알린다(양봉인 동안 매일 알리면 쓸모가 없다)."""
    d = f"{pd.Timestamp(day):%Y-%m-%d}"
    o = dict(DEFAULTS, **wo, start=d, end=d)
    o["kind"] = core.OSC_STATE.get(o["kind"], o["kind"])
    win, _, cand = scan(o)
    if cand.empty:
        return []
    cand = cand[cand.발생일 == pd.Timestamp(day)]
    ret = win.ret.reindex(cand.index)
    return [{"code": str(r.종목코드), "name": str(r.종목명), "close": float(r.종가),
             "ret": float(ret.loc[r.Index]) if r.Index in ret.index else None,
             "ind": float(r.개인분산) if "개인분산" in cand.columns else None}
            for r in cand.itertuples()]


def watch_refresh(now: datetime):
    """오늘 봉을 (다시) 받아 패널에 붙이고 (그 날짜, 종가 확정인가, 시각 글, 안내)를 돌려준다. 오늘 장이 없으면 날짜가 None.
    저장 자료에 이미 오늘이 있으면(18시 뒤 「최신 데이터 받기」) 받지 않고 그 날짜를 쓴다."""
    panel("d")
    stored = _PANEL["stored"]
    if stored.date() >= now.date():
        return (stored, True, "", "") if stored.date() == now.date() else (None, False, "", "저장 자료가 오늘보다 뒤입니다")
    with _LIVE_LOCK:
        busy = _LIVE["running"]
    if not busy:
        live_start()
    end = time.time() + WATCH_REFRESH_TIMEOUT
    while time.time() < end:
        with _LIVE_LOCK:
            if not _LIVE["running"]:
                break
        time.sleep(0.5)
    with _LIVE_LOCK:
        st = dict(_LIVE)
    if st["running"]:
        return None, False, "", "오늘 봉을 만드는 데 너무 오래 걸립니다"
    if st["day"] is None or st["day"].date() != now.date():
        return None, False, "", st["msg"] or "오늘 시세가 없습니다"
    return st["day"], st["at"].time() >= LIVE_FINAL, f"{st['at']:%H:%M}", ""


def _watch_form(fields: dict, inner: str) -> str:
    """감시 화면의 단추 하나(POST /api/watch). 보기 전용 접속에는 그리지 않는다."""
    if not is_owner():
        return ""
    hid = "".join(f'<input type="hidden" name="{k}" value="{html.escape(str(v))}">' for k, v in fields.items())
    return f'<form class="inline" method="post" action="/api/watch">{hid}{inner}</form>'


_WATCHER: dict = {"w": None}


def watcher() -> watch.Watcher:
    if _WATCHER["w"] is None:
        _WATCHER["w"] = watch.Watcher(watch_hits, watch_refresh)
    return _WATCHER["w"]


def watch_action(form: dict) -> tuple[str, str]:
    """감시 화면의 단추들. 반환 (돌아갈 주소, 안내·오류 글). 토큰은 어디에도 되돌려 적지 않는다."""
    get = lambda k: (form.get(k) or [""])[-1].strip()
    act, w, back = get("act"), watcher(), "/watch"
    try:
        if act == "add":
            o = opts({k: v for k, v in form.items() if k in DEFAULTS})
            if o["kind"] in osc60.KINDS:
                return back, "60분봉 검색식은 감시할 수 없습니다(종목별로 분봉을 새로 받는 경로라 주기적으로 돌리기에 너무 느립니다)."
            return back, ("감시 목록에 넣었습니다." if w.add(watch_opts(o), watch_label(o)) else f"감시 목록은 {watch.MAX_ITEMS}개까지입니다.")
        if act == "remove":
            w.remove(get("id"))
            return back, "뺐습니다."
        if act == "interval":
            return back, f"{w.set_interval(int(get('min') or watch.INTERVAL_DEFAULT))}분마다 확인합니다."
        if act == "token":
            tok = get("token")
            if not watch.valid_token(tok):
                return back, "봇 토큰 모양이 아닙니다 — BotFather 가 준 「숫자:영문…」 줄을 그대로 붙여 넣으십시오."
            me = watch.tg_call(tok, "getMe")["result"]
            watch.save_config(token=tok)
            return back, f"봇 @{me.get('username', '')} 에 연결했습니다. 이제 텔레그램에서 그 봇에게 아무 말이나 보낸 뒤 「대화방 찾기」를 누르십시오."
        if act == "detect":
            cfg = watch.load_config()
            chat, name = watch.detect_chat(cfg["token"])
            watch.save_config(chat=chat)
            watch.send("✅ 수급분석 스캐너 감시 알림이 이 대화방으로 옵니다.")
            return back, f"대화방을 찾았습니다({name or chat}) — 확인 메시지를 보냈습니다."
        if act == "test":
            watch.send(f"✅ 시험 발송 {datetime.now():%m-%d %H:%M:%S}")
            return back, "시험 메시지를 보냈습니다."
        if act == "start":
            cfg = watch.load_config()
            if not cfg["token"] or not cfg["chat"]:
                return back, "먼저 텔레그램을 연결하십시오(아래 「텔레그램 연결」)."
            if not w.items():
                return back, "감시할 검색식이 없습니다 — 종목 찾기 화면에서 「이 검색식 감시」를 누르십시오."
            return back, ("감시를 시작했습니다." if w.start() else "이미 감시 중입니다.")
        if act == "stop":
            w.stop()
            return back, "감시를 멈춥니다(돌고 있던 한 바퀴가 끝나면 멈춥니다)."
        if act == "once":
            p = panel()
            r = w.cycle(record=False, day=p.dt.max(), final=True)
            return back, (f"지금 걸린 종목을 보냈습니다 — 자료의 마지막 날 {r['day']} 기준 {r['new']}종목 · 발신 {r['sent']}건."
                          + (f" ⚠ {'; '.join(r['errors'])}" if r["errors"] else ""))
    except watch.TgError as e:
        return back, f"⚠ {e}"
    return back, "알 수 없는 요청입니다."


def watch_html(o: dict, msg: str = "") -> bytes:
    w, cfg = watcher(), watch.load_config()
    st = w.status()
    ready = bool(cfg["token"] and cfg["chat"])
    rows = "".join(
        f'<tr><td class="k">{html.escape(i["label"])}<span class="c2">거래대금 {i["o"]["minval"]:g}억↑'
        f'{" · 급등일 제외" if i["o"].get("surge") and i["o"]["kind"] not in core.NO_SURGE_EXCLUDE else ""}</span></td>'
        f'<td><a href="/?{qs(dict(DEFAULTS, **i["o"]))}">검색 화면</a></td>'
        f'<td>{_watch_form({"act": "remove", "id": i["id"]}, "<button>빼기</button>")}</td></tr>'
        for i in w.items())
    table = (f'<div class="wrap"><table><thead><tr><th class="k">감시하는 검색식</th><th></th><th></th></tr></thead><tbody>{rows}</tbody></table></div>'
             if rows else '<div class="empty">감시할 검색식이 없습니다 — <a href="/"><b>종목 찾기</b></a>에서 검색식과 조건을 고른 뒤 「이 검색식 감시」를 누르십시오.</div>')
    run = ('<span class="badge good">감시 중</span>' if st["running"] else '<span class="badge weak">꺼짐</span>')
    ctl = (_watch_form({"act": "stop"}, "<button>감시 중지</button>") if st["running"]
           else _watch_form({"act": "start"}, '<button class="go">감시 시작</button>'))
    iv = _watch_form({"act": "interval"},
                    f'<label>확인 주기(분) <input type="number" name="min" value="{st["interval"]}" min="{watch.INTERVAL_MIN}" max="{watch.INTERVAL_MAX}" style="width:5em"></label> <button>바꾸기</button>')
    tg = (f'<p class="note">텔레그램 연결됨 — 봇 토큰 저장됨 · 대화방 {html.escape(cfg["chat"])}</p>'
          + _watch_form({"act": "test"}, "<button>시험 메시지 보내기</button>") if ready else
          '<ol class="note"><li>텔레그램에서 <b>@BotFather</b> 를 찾아 <code>/newbot</code> → 이름을 정하면 「숫자:영문…」 모양의 <b>토큰</b>을 줍니다.</li>'
          '<li>그 토큰을 아래에 붙여 넣고 「토큰 저장」.</li><li>텔레그램에서 방금 만든 봇을 열어 <b>아무 말이나</b> 보냅니다.</li>'
          '<li>「대화방 찾기」를 누르면 확인 메시지가 옵니다.</li></ol>')
    tg += (_watch_form({"act": "token"},
                      '<label>봇 토큰 <input type="password" name="token" autocomplete="off" style="width:26em" placeholder="123456789:AA…"></label> <button>토큰 저장</button>')
           + (_watch_form({"act": "detect"}, "<button>대화방 찾기</button>") if cfg["token"] else ""))
    log = "".join(f"<li>{html.escape(x)}</li>" for x in reversed(st["log"])) or "<li>아직 기록이 없습니다.</li>"
    body = f"""{topbar("watch", o)}
<div class="watch"><h1>실시간 감시 {run}</h1>
<p class="lead">장중에 {st["interval"]}분마다 전 종목의 지금 시세로 오늘 봉을 만들어 아래 검색식을 돌리고, <b>그날 새로 걸린 종목</b>을 텔레그램으로 보냅니다.
「감시 시작」을 눌렀을 때만 돌고, 15:40 뒤 종가로 한 번 더 확인해 마감 정리를 보낸 다음 스스로 끝납니다.</p>
{f'<p class="stale-note" role="status">{html.escape(msg)}</p>' if msg else ""}
<section class="sig"><div class="row">{ctl}{iv}{_watch_form({"act": "once"}, "<button>지금 걸린 종목 한 번 보내기</button>")}</div>
{table}</section>
<section class="sig"><h2>텔레그램 연결</h2>{tg}</section>
<section class="sig"><h2>최근 기록</h2><ul class="note" id="watch-log">{log}</ul></section>
<p class="note"><b>장중 잠정</b> — 장중 봉은 미완성이라 신고가·양봉 전환 신호가 마감 때 사라질 수 있습니다(마감 정리에 사라진 종목을 적습니다).
수급(개인 분산비율·외국인·기관)은 장 마감 뒤에 확정되므로 장중에는 직전 거래일 값으로 판정합니다. 서버를 다시 띄우면 감시는 꺼진 상태로 시작합니다.
「지금 걸린 종목 한 번 보내기」는 자료의 마지막 날 기준으로 전부 보내며, 알린 기록에는 남기지 않습니다.</p>
</div><script>if({str(st["running"]).lower()}) setTimeout(function(){{location.replace('/watch')}}, 30000);</script>"""
    return page("실시간 감시", body)


# ─────────────────────────── 시간별 추천 (picks.py) — 사용자 요청 2026-10-06, 지인 공유용

PICK_KINDS = ("win_combo", "win_tight", "best_combo", "bb40_combo", "lowvol_breakout", "oscw_trend")   # 추천 후보를 뽑는 검색식(★ 중 모델이 아는 것)
PICK_BASE = {"win_combo": "승률형", "win_tight": "승률형", "lowvol_breakout": "저변동", "oscw_trend": "저변동",   # 검색식 → 모델이 학습한 검색식 묶음
             "best_combo": "조합1위", "bb40_combo": "상한선", "bb_break": "볼린저"}
MODEL_FILE = core.CACHE / "winmodel_final.pkl"
MODEL_MAX_AGE = 45                      # 일. 이보다 오래된 모델은 「모델 다시 학습」을 권한다(해마다 다시 학습하는 것이 검정 방식이었다)
_MODEL: dict = {"m": None, "err": "", "training": False}
_MODEL_LOCK = threading.Lock()
_MKT: dict = {}


def model_load() -> dict | None:
    """저장된 확률 모델 {'model','cols','ref'(검색식 묶음별 과거 확률 분포),'trained'}. 없으면 None."""
    with _MODEL_LOCK:
        if _MODEL["m"] is None and MODEL_FILE.exists():
            try:
                _MODEL["m"] = pd.read_pickle(MODEL_FILE)
            except Exception as e:                           # noqa: BLE001
                _MODEL["err"] = f"모델 파일을 못 읽었다 — {repr(e)[:60]}"
        return _MODEL["m"]


def model_train() -> str:
    """결과(40일 뒤)가 난 사건 전부로 다시 학습해 저장한다(약 1분). 사건표가 없으면 그 이유를 돌려준다."""
    import winmodel
    if not strategy_events_exist():
        return "사건표가 없습니다 — 이 PC 에서 `python strategy.py --build` 를 먼저 돌리십시오(상폐 포함 marcap 자료 필요)."
    with _MODEL_LOCK:
        if _MODEL["training"]:
            return "이미 학습 중입니다."
        _MODEL["training"] = True
    try:
        E, X, y = winmodel.build_xy()
        m = winmodel.fit(X, y)
        p = m.predict_proba(X)[:, 1]
        ref = {short: np.sort(p[(E.base == b).to_numpy()]) for b, short in winmodel.SHORT.items()}
        obj = {"model": m, "cols": list(X.columns), "ref": ref, "trained": f"{datetime.now():%Y-%m-%d}", "n": int(len(E)),
               "last": f"{E.dt.max():%Y-%m-%d}"}
        core.CACHE.mkdir(parents=True, exist_ok=True)
        pd.to_pickle(obj, MODEL_FILE)
        with _MODEL_LOCK:
            _MODEL["m"], _MODEL["err"] = obj, ""
        return f"학습 끝 — 사건 {len(E):,}건({obj['last']} 까지)."
    except Exception as e:                                   # noqa: BLE001
        with _MODEL_LOCK:
            _MODEL["err"] = f"학습 실패 — {repr(e)[:80]}"
        return _MODEL["err"]
    finally:
        with _MODEL_LOCK:
            _MODEL["training"] = False


def strategy_events_exist() -> bool:
    import strategy
    return strategy.EVENTS.exists() and strategy.PATHS.exists()


def market_row(day) -> dict:
    """그날의 지수 상태(코스피·코스닥) — winmodel.market_features. 하루에 한 번만 받는다."""
    key = f"{pd.Timestamp(day):%Y-%m-%d}"
    if key not in _MKT:
        import winmodel
        try:
            _MKT[key] = winmodel.market_features(pd.DatetimeIndex([pd.Timestamp(day)])).iloc[0].to_dict()
        except Exception:                                    # noqa: BLE001 — 지수를 못 받으면 빈 값(모델은 결측을 다룬다)
            _MKT[key] = {}
    return _MKT[key]


def price_with_today(code: str, day) -> pd.DataFrame | None:
    """저장 일봉 + (오늘이면) 메모리의 오늘 봉."""
    px = core.read_price(code)
    if px is None:
        return None
    px = px[["open", "high", "low", "close", "volume"]].astype(float)
    day = pd.Timestamp(day)
    if day > px.index[-1]:
        with _LIVE_LOCK:
            b = (_LIVE.get("bars") or {}).get(code)
        if not b:
            return None
        px.loc[day] = [float(b[0]), float(b[1]), float(b[2]), float(b[3]), float(b[4])]
    return px


def pick_candidates(day) -> list[dict]:
    """오늘(day) ★ 검색식에 걸린 종목마다 카드 재료 — 모델 백분위(pct)·지표·수급·테마·글. flowapp.scan 과 같은 경로(수급 조건 켬)."""
    import strategy
    day = pd.Timestamp(day)
    d = f"{day:%Y-%m-%d}"
    hits: dict[str, list[str]] = {}
    for kind in PICK_KINDS:
        o = dict(DEFAULTS, **VALIDATED, kind=kind, start=d, end=d)
        try:
            win, _, cand = scan(o)
        except Exception:                                    # noqa: BLE001 — 검색식 하나가 터져도 나머지는 본다
            continue
        if cand.empty:
            continue
        for code in cand[cand.발생일 == day].종목코드.astype(str):
            hits.setdefault(code, []).append(kind)
    if not hits:
        return []
    mdl = model_load()
    mkt = market_row(day)
    up = up_days(dict(DEFAULTS))
    regime = float(bool(up.get(day, False)))
    uni = core.universe().set_index("Code")
    last = panel()
    out = []
    for code, kinds in hits.items():
        px = price_with_today(code, day)
        if px is None or len(px) < 300 or px.index[-1] != day:
            continue
        f = strategy.feats(px).iloc[-1]
        row = last[(last.Code.astype(str) == code) & (last.dt == day)]
        flow = {c: (float(row[c].iloc[0]) if (len(row) and c in row and pd.notna(row[c].iloc[0])) else float("nan")) for c in ("ind_disp", "frgn5", "orgn5")}
        primary = kinds[0]
        base = PICK_BASE.get(primary, "승률형")
        pct = None
        if mdl is not None:
            X = {}
            for c in mdl["cols"]:
                if c in f.index:
                    X[c] = float(f[c])
                elif c.startswith("b_"):
                    X[c] = 1.0 if c == f"b_{base}" else 0.0
                elif c == "n_day":
                    X[c] = float(np.log1p(sum(1 for k in hits.values() if primary in k)))
                elif c == "regime":
                    X[c] = regime
                else:
                    X[c] = float(mkt.get(c, np.nan))
            p = float(mdl["model"].predict_proba(pd.DataFrame([X])[mdl["cols"]])[0, 1])
            ref = mdl["ref"].get(base)
            pct = float(100 * np.searchsorted(ref, p) / len(ref)) if ref is not None and len(ref) else None
        name = str(uni.Name.get(code, code))
        info = SIGNAL_INFO.get(primary)
        atr, g20, near, wosc = 100 * float(f.atr), 100 * float(f.g20), float(f.near52), float(f.wosc)
        th = ", ".join(n for _, n in themes.of(code)[:2])
        final_bar = (day <= _PANEL.get("stored", day)) or bool(live_status().get("final"))
        price, low = float(px.close.iloc[-1]), float(px.low.iloc[-1])
        entry = ((f"오늘 15:20 이후 종가 무렵에 매수(마감까지 신호가 남아 있을 때). 못 사면 내일 09:30 이후 — 시가 갭이 +1% 넘으면 패스. "
                  f"지정가를 낮게 걸고 기다리지 않는다(체결되는 쪽이 약한 쪽).") if not final_bar else
                 ("내일 09:30 이후 매수 — 시가 갭이 +1% 넘으면 패스. 종가가 오늘 종가 아래로 내려와 있으면 사지 않는다. 지정가로 기다리지 않는다."))
        hold = (f"40거래일(약 2개월) 뒤 종가 매도 · 손절·익절 없음 · 계좌의 1/10 이내(최대 10종목 분산). "
                f"단기로 보면 1주(5거래일) 보유, 무효선 = 오늘 저가 {low:,.0f}원 종가 이탈.")
        why = [f"{' / '.join(label_of(k) for k in kinds)} 에 걸림",
               (f"오를 확률 모델 백분위 {pct:.0f} — 과거 같은 검색식 신호 가운데 상위 {100 - pct:.0f}% 안" if pct is not None else "모델 없음"),
               f"ATR {atr:.1f}% · 20일선 이격 {g20:+.1f}% · 52주 최고 대비 {100 * near:.0f}% · 주봉 오실레이터 {'음봉(아직 안 돌았다)' if wosc < 0 else '양봉'}"]
        if flow["ind_disp"] == flow["ind_disp"]:
            why.append(f"수급: 개인 분산 {100 * flow['ind_disp']:.0f}% · 외인 5일 {100 * flow['frgn5']:+.0f}% · 기관 5일 {100 * flow['orgn5']:+.0f}% (직전 거래일 기준)")
        if info:
            why.append(f"검색식 성적: {info[4][:90]}")
        if th:
            why.append(f"테마: {th}")
        warn = []
        if not final_bar:
            warn.append("장중 잠정 신호 — 마감 때 사라질 수 있음(15:41 에 확정 여부를 다시 적는다)")
        warn.append("시장 상승국면" if regime else "시장 하락국면(승률형은 하락국면에서도 통했지만 2016·2018년 같은 해는 크게 졌다)")
        if atr > 4.0:
            warn.append(f"변동성 높음(ATR {atr:.1f}% — 과거 승률형 신호 중앙 3.4%)")
        if g20 > 0.10 * 100:
            warn.append("20일선 이격 10% 초과 — 이미 많이 오른 자리")
        if flow["ind_disp"] == flow["ind_disp"] and flow["ind_disp"] > core.IND_DISP_MAX:
            warn.append("개인 분산 30% 초과")
        out.append({"code": code, "name": name, "price": price, "low": low, "ret": float(f.ret), "kinds": [label_of(k) for k in kinds],
                    "kind_codes": list(kinds),                                   # top3 가 WIN_RATES 를 찾는 열쇠(2026-10-07)
                    "primary": primary, "pct": pct, "atr": atr, "g20": g20, "near52": near, "wosc": wosc, "regime": regime,
                    "clv": float(f.clv) if pd.notna(f.clv) else None, "vr": float(f.vr) if pd.notna(f.vr) else None,
                    "osc_d": float(f["osc_d"]) if pd.notna(f["osc_d"]) else None, "align": float(f["align"]) if pd.notna(f["align"]) else 0.0,   # f.align 은 Series 메서드다
                    "flow": flow, "theme": th, "entry": entry, "hold": hold, "why": why, "warn": warn})
    return out


def pick_refresh(now: datetime):
    return watch_refresh(now)


def pick_prices(day, codes) -> dict:
    """마감 확정용: 코드 → (종가, 그날 ★ 검색식 신호가 종가로도 남았는가)."""
    still = {c["code"] for c in pick_candidates(day)}
    out = {}
    for code in codes:
        px = price_with_today(code, day)
        if px is not None and px.index[-1] == pd.Timestamp(day):
            out[code] = (float(px.close.iloc[-1]), code in still)
    return out


_PICKER: dict = {"p": None}


def picker() -> picks.Picker:
    if _PICKER["p"] is None:
        _PICKER["p"] = picks.Picker(pick_candidates, pick_refresh, watch.send, prices=pick_prices,
                                    on_final=lambda: top3_run(send_tg=bool(_PICKER["p"].state.get("telegram")))[1])   # 15:41 마감 뒤 오늘의 3종목 기록
    return _PICKER["p"]


def picks_action(form: dict) -> tuple[str, str]:
    get = lambda k: (form.get(k) or [""])[-1].strip()
    act, pk, back = get("act"), picker(), "/picks"
    try:
        if act == "start":
            if model_load() is None:
                return back, "모델이 없습니다 — 먼저 「모델 학습」을 누르십시오."
            return back, ("추천을 시작했습니다(매 정시 + 15:41 확정)." if pk.start() else "이미 돌고 있습니다.")
        if act == "stop":
            pk.stop()
            return back, "추천을 멈춥니다."
        if act == "now":
            r = pk.pick(None)
            if r["pick"]:
                return back, f"지금 뽑음 — {r['pick']['name']} (백분위 {r['pick']['pct']:.0f})."
            return back, f"추천 없음 — {r['note'] or '오늘 걸린 종목이 없거나 백분위가 기준 아래'}"
        if act == "telegram":
            pk.set_telegram(get("on") == "1")
            return back, "텔레그램 전송을 " + ("켰습니다." if get("on") == "1" else "껐습니다.")
        if act == "train":
            threading.Thread(target=model_train, daemon=True).start()
            return back, "모델 학습을 시작했습니다(약 1분). 잠시 뒤 새로고침하십시오."
        if act == "confirm":
            p = panel()
            day = p.dt.max()
            n = pk.confirm(day, pick_prices(day, [c["code"] for c in pk.picks() if c["day"] == f"{day:%Y-%m-%d}"]))
            return back, f"{day:%m-%d} 카드 {n}개의 마감 확정 여부를 적었습니다."
    except watch.TgError as e:
        return back, f"⚠ {e}"
    return back, "알 수 없는 요청입니다."


def liquid_codes() -> list[str]:
    """지금 패널 마지막 날 기준 유동 종목(20일 거래대금 ≥ 기본 하한) 코드 — 거래대금 증분 수집 범위."""
    p = panel()
    last = p[p.dt == p.dt.max()]
    return sorted(last[last.val20 >= VALIDATED["minval"] * 1e8].Code.astype(str).unique().tolist())


def top3_run(send_tg: bool = False, last_stored: bool = False) -> tuple[str, str]:
    """오늘의 3종목을 지금 뽑아 기록한다. (날짜, 메모). last_stored=True 면 오늘 봉 대신 저장된 마지막 거래일(확정 봉)로 뽑는다."""
    now = datetime.now()
    if last_stored:
        day, final, at, note = panel().dt.max(), True, "마감", ""
    else:
        day, final, at, note = watch_refresh(now)
    if day is None:
        return "", note or "오늘 시세 없음(휴장이거나 장 시작 전)"
    key = f"{day:%Y-%m-%d}"
    cands = pick_candidates(day)
    picks_ = top3.choose(cands, WIN_RATES, bool(final))
    at = at or f"{now:%H:%M}"
    top3.record(key, picks_, bool(final), at, len(cands))
    got = [h for h, _, _ in top3.HORIZONS if picks_.get(h)]
    msg = f"{key} {at} · 후보 {len(cands)} · 뽑힘 {len(got)}/3" + ("" if final else " · 장중 잠정")
    if send_tg and got:
        try:
            watch.send(top3.render_text(key, picks_))
            msg += " · 텔레그램 보냄"
        except Exception as e:                                   # noqa: BLE001
            msg += f" · 텔레그램 실패({repr(e)[:40]})"
    return key, msg


def top3_action(form: dict) -> tuple[str, str]:
    get = lambda k: (form.get(k) or [""])[-1].strip()
    act, back = get("act"), "/top3"
    if act in ("pick", "pick_send", "pick_last"):
        try:
            _, msg = top3_run(send_tg=(act == "pick_send"), last_stored=(act == "pick_last"))
        except Exception as e:                                   # noqa: BLE001
            msg = f"뽑지 못했다 — {repr(e)[:100]}"
        return back, msg
    return back, "알 수 없는 요청입니다."


def _top3_form(fields: dict, inner: str) -> str:
    if not is_owner():
        return ""
    hid = "".join(f'<input type="hidden" name="{k}" value="{html.escape(str(v))}">' for k, v in fields.items())
    return f'<form class="inline" method="post" action="/api/top3">{hid}{inner}</form>'


def top3_card(h: str, label: str, c: dict | None, day: str, show_result: bool) -> str:
    if not c:
        return f'<article class="card dim"><header><span class="when">{h} · {label}</span><span class="badge weak">해당 없음</span></header><p class="note">점수 {top3.MIN_SCORE:.0f} 넘는 후보가 없었습니다.</p></article>'
    res = ""
    if show_result:
        r = top3.realized(c, core.read_price(c["code"]), day)
        if r:
            res = (f'<p class="res {"pos" if r["ret"] > 0 else "neg"}">{"확정" if r["done"] else "진행 중"} · {r["entry_day"]} 시가 {r["entry"]:,.0f} → '
                   f'{"기한" if r["done"] else "현재"} 종가 {r["exit"]:,.0f} · <b>{100 * r["ret"]:+.1f}%</b> ({r["held"]}/{c["days"]}거래일)</p>')
    th = f' · 테마 {html.escape(c["theme"])}' if c.get("theme") else ""
    return (f'<article class="card"><header><span class="when">{h} · {label}</span><span class="badge good">점수 {c["score"]:+.1f}</span></header>'
            f'<h3><a href="/stock/{c["code"]}">{html.escape(c["name"])}</a> <span class="c2">{c["code"]}</span> · {c["price"]:,.0f}원 ({100 * c["ret"]:+.1f}%)</h3>'
            f'<p class="kinds">{html.escape(" / ".join(c["kinds"]))}{th}</p>{res}'
            f'<dl><dt>매수타점</dt><dd>{html.escape(c["entry"])}</dd><dt>보유·청산</dt><dd>{html.escape(c["hold"])}' + (f'<br>{html.escape(c["stop"])}' if c.get("stop") else "") + '</dd>'
            f'<dt>점수 근거</dt><dd><ul>{"".join(f"<li>{html.escape(w)}</li>" for w in c["why"])}</ul></dd>'
            + (f'<dt>주의</dt><dd><ul>{"".join(f"<li>{html.escape(w)}</li>" for w in c["warn"])}</ul></dd>' if c.get("warn") else "") + '</dl>'
            + (f'<p class="note">같은 기간의 다음 후보: {html.escape(", ".join(c["others"]))}</p>' if c.get("others") else "") + '</article>')


_BENCH: dict = {}


def bench_curve() -> pd.Series:
    """유동 유니버스(20일 거래대금 ≥ 기본 하한)의 날짜별 평균 일수익률을 누적한 곡선 — 패널이 같으면 한 번만 센다.
    (예전엔 카드마다 600만 행을 걸러 0.2초씩 들었다 — 추적 카드 30장이면 6초.)"""
    p = panel()
    key = (id(p), len(p))
    if _BENCH.get("key") != key:
        liq = p[p.val20 >= VALIDATED["minval"] * 1e8]
        daily = liq.groupby("dt").ret.mean().fillna(0).sort_index()
        _BENCH["key"], _BENCH["curve"] = key, (1 + daily).cumprod()
    return _BENCH["curve"]


def bench_ret(d0: str, d1: str) -> float | None:
    """같은 기간 시장 수익률 — d0 다음 날부터 d1 까지 유동 유니버스 평균 일수익률의 누적(검정의 시장초과와 같은 뜻)."""
    cv = bench_curve()
    a, b = pd.Timestamp(d0), pd.Timestamp(d1)
    if b <= a or cv.empty:
        return None
    i0, i1 = cv.index.searchsorted(a, side="right") - 1, cv.index.searchsorted(b, side="right") - 1
    if i1 <= i0 or i1 < 0:
        return None
    base = float(cv.iloc[i0]) if i0 >= 0 else 1.0
    return float(cv.iloc[i1] / base - 1)


def track_html(o: dict) -> bytes:
    st = top3.load_state()
    today = datetime.now().date()
    bars_fn = lambda code: price_with_today(code, pd.Timestamp(today))     # 저장 일봉 + (장중이면) 오늘 봉 — 오늘 산 카드도 바로 따라간다  # noqa: E731
    rows = top3.track_rows(st, bars_fn, bench_ret)
    val = top3.validation(rows, today)
    pct = lambda x, d=1: "—" if x is None else f"{100 * x:+.{d}f}%"
    vrows = []
    for h, _, label in top3.HORIZONS:
        v = val[h]
        if not v.get("start"):
            vrows.append(f"<tr><td>{h} · {label}</td><td>{v['window']}일</td><td colspan=8>아직 카드 없음</td></tr>")
            continue
        prog = min(100, int(100 * v["elapsed"] / v["window"]))
        state_txt = "검증 끝" if v["elapsed"] >= v["window"] else f"{v['elapsed']}/{v['window']}일"
        win_txt = "—" if v["win"] is None else f"{100 * v['win']:.0f}%"
        vrows.append(f"<tr><td>{h} · {label}</td><td>{v['start']} ~ {v['end']}<br><span class=\"prog\"><i style=\"width:{prog}%\"></i></span> {state_txt}</td>"
                     f"<td>{v['n']}</td><td>{v['n_done']}</td><td>{win_txt}</td>"
                     f"<td>{pct(v['avg'], 2)}</td><td>{pct(v['bench'], 2)}</td><td>{'—' if v['avg'] is None or v['bench'] is None else pct(v['avg'] - v['bench'], 2)}</td>"
                     f"<td>{pct(v['max_avg'])} / {pct(v['min_avg'])}</td><td>{v['n_live']}건 {pct(v['live_avg'])}</td></tr>")
    trs = []
    for r in rows:
        if "ret" not in r:
            res = "<td colspan=7 class=note>다음 거래일 봉 대기(매수 전)</td>"
        else:
            cls = "pos" if r["ret"] > 0 else "neg" if r["ret"] < 0 else ""
            res = (f"<td>{r['entry_day']}<br>{r['entry']:,.0f}</td><td>{r['held']}/{r['days']}</td><td>{r['exit_day']}<br>{r['exit']:,.0f}</td>"
                   f"<td class=\"{cls}\"><b>{pct(r['ret'])}</b></td><td>{pct(r.get('bench'))}</td><td>{pct(r['max_ret'])} / {pct(r['min_ret'])}</td>"
                   f"<td>{'<span class=\"badge good\">확정</span>' if r['done'] else '<span class=\"badge weak\">진행 중</span>'}</td>")
        trs.append(f"<tr><td>{r['day']}{'' if r['final'] else '<br><small>장중 잠정</small>'}</td><td>{r['horizon']}</td>"
                   f"<td><a href=\"/stock/{r['code']}\">{html.escape(r['name'])}</a> <span class=c2>{r['code']}</span><br><small>신호일 종가 {r['price']:,.0f} · 점수 {r['score']:+.1f}</small></td>{res}</tr>")
    body = f"""{topbar("track", o)}
<div class="picks top3"><h1>추천 추적 <span class="badge weak">검증 중</span></h1>
<p class="lead">「오늘의 3종목」 카드를 <b>다음 거래일 시가에 사서 기한(단기 3·스윙 5·중기 20거래일) 종가에 판다</b>는 규칙으로 매일 따라갑니다.
검증 창은 단기·스윙 <b>30일</b>, 중기 <b>90일</b>(첫 카드 날부터, 달력일). 시장 대비 = 같은 기간 유동 유니버스 평균 수익률을 뺀 값(검정의 시장초과와 같은 뜻).
시세는 저장 일봉이라 「최신 데이터 받기」를 돌린 날까지 반영됩니다.</p>
<section class="sig"><h2>검증 집계</h2><table class="perf wide"><thead><tr><th>기간</th><th>검증 창</th><th>카드</th><th>확정</th><th>승률</th><th>평균</th><th>시장</th><th>시장 대비</th><th>최고/최저 평균</th><th>진행 중</th></tr></thead>
<tbody>{"".join(vrows)}</tbody></table></section>
<section class="sig"><h2>건별 추적</h2>{'<table class="perf wide"><thead><tr><th>추천일</th><th>기간</th><th>종목</th><th>매수(다음날 시가)</th><th>보유</th><th>청산/현재</th><th>수익률</th><th>시장</th><th>최고/최저</th><th>상태</th></tr></thead><tbody>' + "".join(trs) + "</tbody></table>" if trs else '<div class="empty">아직 카드가 없습니다 — 「오늘의 3종목」에서 뽑으면 여기 쌓입니다.</div>'}</section>
</div>
<style>.top3 .perf.wide{{width:100%;font-size:13px}} .top3 .perf.wide td,.top3 .perf.wide th{{padding:6px 8px;text-align:right;vertical-align:top;border-bottom:1px solid var(--line)}}
.top3 .perf.wide td:nth-child(-n+3),.top3 .perf.wide th:nth-child(-n+3){{text-align:left}} .top3 .perf td.pos{{color:var(--pos)}} .top3 .perf td.neg{{color:var(--neg)}}
.prog{{display:inline-block;width:90px;height:6px;background:var(--line);border-radius:3px;vertical-align:middle;margin-right:4px}} .prog i{{display:block;height:6px;background:var(--neg);border-radius:3px}}</style>"""
    return page("추천 추적", body)


def top3_html(o: dict, msg: str = "") -> bytes:
    st = top3.load_state()
    days = sorted(st["days"], reverse=True)
    owner = is_owner()
    ctl = ('<div class="row">' + _top3_form({"act": "pick"}, '<button class="go">지금 뽑기</button>')
           + _top3_form({"act": "pick_send"}, "<button>뽑고 텔레그램으로 보내기</button>")
           + _top3_form({"act": "pick_last"}, "<button>지난 거래일(확정 봉)로 뽑기</button>") + "</div>") if owner else ""
    sections = []
    for i, d in enumerate(days[:30]):
        e = st["days"][d]
        head = f'<h2>{d} <span class="when">{html.escape(str(e.get("at", "")))} · 후보 {e.get("n", 0)}개 · {"종가 확정" if e.get("final") else "장중 잠정"}</span></h2>'
        cards = "".join(top3_card(h, label, (e.get("picks") or {}).get(h), d, show_result=True) for h, _, label in top3.HORIZONS)
        sections.append(f'<section class="day{" first" if i == 0 else ""}">{head}<div class="cards3">{cards}</div></section>')
    # 실적표: 확정된 카드의 기간별 평균
    rows = []
    for h, _, label in top3.HORIZONS:
        rets = []
        for d in days:
            c = (st["days"][d].get("picks") or {}).get(h)
            if c:
                r = top3.realized(c, core.read_price(c["code"]), d)
                if r and r["done"]:
                    rets.append(r["ret"])
        if rets:
            rows.append(f"<tr><td>{h} · {label}</td><td>{len(rets)}</td><td>{100 * sum(rets) / len(rets):+.2f}%</td><td>{100 * sum(1 for x in rets if x > 0) / len(rets):.0f}%</td></tr>")
    table = (f'<table class="perf"><thead><tr><th>기간</th><th>확정 건수</th><th>평균 수익률</th><th>오른 비율</th></tr></thead><tbody>{"".join(rows)}</tbody></table>'
             if rows else '<p class="note">아직 기한이 지나 확정된 카드가 없습니다 — 쌓이면 여기에 기간별 평균이 적힙니다.</p>')
    body = f"""{topbar("top3", o)}
<div class="picks top3"><h1>오늘의 3종목 <span class="badge weak">단기 · 스윙 · 중기</span></h1>
<p class="lead">★ 검색식에 오늘 걸린 종목을 모아, <b>보유 기간마다 점수가 가장 높은 1종목</b>을 고릅니다 — 단기 3거래일 · 스윙 5거래일 · 중기 20거래일.
점수는 이 저장소에서 검정한 수치로만 매깁니다: 검색식의 그 기간 오른 비율(2025년 이후) · 확률 모델 백분위 · ATR · 20일선 이격 · 종가 위치 · 거래량 · 수급(개인 분산·외인/기관) · 시장 국면. 한 종목은 한 기간에만 들어갑니다.</p>
{f'<p class="stale-note" role="status">{html.escape(msg)}</p>' if msg else ""}
{ctl}
<p class="note"><b>규칙이 고른 종목과 근거이지 매수 추천이 아닙니다.</b> 가장 좋은 검색식도 2개월 뒤 오른 비율이 53~58%이고 해마다 편차가 큽니다. 지난 카드는 다음 거래일 시가 매수 → 기한 종가 매도로 실적을 적어 규칙 자체를 검증합니다.</p>
<section class="sig"><h2>실적표(확정 카드)</h2>{table}<p class="note">건별 매일 추적과 검증 집계(단기·스윙 30일, 중기 90일)는 <a href="/track">추천 추적</a>에 있습니다. 「시간별 추천」이 돌고 있으면 15:41 마감 때 3종목을 자동으로 뽑아 쌓습니다.</p></section>
{"".join(sections) if sections else '<div class="empty">아직 뽑은 날이 없습니다 — 「지금 뽑기」.</div>'}
</div>
<style>.cards3{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px}} .top3 .day{{margin-top:22px}} .top3 .res{{margin:4px 0 8px;font-size:13px}}
.top3 .res.pos{{color:var(--pos)}} .top3 .res.neg{{color:var(--neg)}} .top3 .perf td,.top3 .perf th{{padding:4px 10px;text-align:right}} .top3 .perf td:first-child,.top3 .perf th:first-child{{text-align:left}}</style>"""
    return page("오늘의 3종목", body)


def picks_html(o: dict, msg: str = "") -> bytes:
    pk = picker()
    st = pk.status()
    mdl = model_load()
    owner = is_owner()
    if mdl:
        age = (datetime.now().date() - datetime.strptime(mdl["trained"], "%Y-%m-%d").date()).days
        mline = f"확률 모델: {mdl['trained']} 학습 · 사건 {mdl['n']:,}건({mdl['last']} 까지)" + (f' <b class="warn">· {age}일 지남 — 다시 학습을 권함</b>' if age > MODEL_MAX_AGE else "")
    else:
        mline = "확률 모델이 아직 없습니다" + (f" — {html.escape(_MODEL['err'])}" if _MODEL["err"] else "") + (" (학습 중…)" if _MODEL["training"] else "")
    ctl = ""
    if owner:
        ctl = ('<div class="row">' + (_pick_form({"act": "stop"}, "<button>추천 중지</button>") if st["running"] else _pick_form({"act": "start"}, '<button class="go">추천 시작</button>'))
               + _pick_form({"act": "now"}, "<button>지금 뽑기</button>") + _pick_form({"act": "confirm"}, "<button>마감 확정 적기</button>")
               + _pick_form({"act": "train"}, "<button>모델 학습</button>")
               + _pick_form({"act": "telegram", "on": "0" if st["telegram"] else "1"}, f"<button>텔레그램 전송 {'끄기' if st['telegram'] else '켜기'}</button>")
               + f'</div><p class="note">{mline}</p>')
    cards = []
    for c in pk.picks():
        cls = "card" + (" dim" if c.get("status") == "마감 때 신호 소멸" else "")
        close = f' · 종가 {c["close"]:,.0f}원' if c.get("close") else ""
        cards.append(
            f'<article class="{cls}"><header><span class="when">{c["day"]} {c["at"]} · {html.escape(c["slot"])}</span>'
            f'<span class="badge {"good" if c["status"] == "종가 확정" else "weak" if c["status"] == "장중 잠정" else "bad"}">{html.escape(c["status"])}</span></header>'
            f'<h3><a href="/stock/{c["code"]}">{html.escape(c["name"])}</a> <span class="c2">{c["code"]}</span> · {c["price"]:,.0f}원 ({100 * c["ret"]:+.1f}%){close}</h3>'
            f'<p class="kinds">{html.escape(" / ".join(c["kinds"]))}</p>'
            f'<dl><dt>매수타점</dt><dd>{html.escape(c["entry"])}</dd><dt>보유전략</dt><dd>{html.escape(c["hold"])}</dd>'
            f'<dt>매수근거</dt><dd><ul>{"".join(f"<li>{html.escape(w)}</li>" for w in c["why"])}</ul></dd>'
            f'<dt>주의</dt><dd><ul>{"".join(f"<li>{html.escape(w)}</li>" for w in c["warn"])}</ul></dd></dl>'
            + (f'<p class="note">그 시각의 다른 후보: {html.escape(", ".join(c["others"]))}</p>' if c.get("others") else "") + '</article>')
    body = f"""{topbar("picks", o)}
<div class="picks"><h1>시간별 추천 <span class="badge {"good" if st["running"] else "weak"}">{"돌고 있음" if st["running"] else "꺼짐"}</span></h1>
<p class="lead">장중 매 정시에 ★ 검색식을 오늘 봉으로 돌려, 걸린 종목 가운데 <b>오를 확률 모델 백분위가 가장 높은 1종목</b>을 고릅니다(백분위 {picks.PICK_MIN} 미만이면 그 시간은 추천 없음).
15:41 에 종가로 다시 돌려 장중 신호가 남았는지 적습니다. 같은 날 같은 종목은 한 번만 고릅니다.</p>
{f'<p class="stale-note" role="status">{html.escape(msg)}</p>' if msg else ""}
{ctl}
<p class="note"><b>이 카드는 매수 추천이 아니라 규칙이 고른 종목과 그 근거입니다.</b> 가장 좋은 검색식도 2개월 뒤 오른 비율이 53~58%이고, 2016·2018년 같은 해에는 크게 졌습니다.
투자 판단과 책임은 보는 사람에게 있습니다. 근거 수치는 전부 이 프로그램의 과거 검정에서 나온 것입니다(README).</p>
{"".join(cards) if cards else '<div class="empty">아직 카드가 없습니다.</div>'}
<section class="sig"><h2>최근 기록</h2><ul class="note">{"".join(f"<li>{html.escape(x)}</li>" for x in reversed(st["log"])) or "<li>—</li>"}</ul></section></div>
<script>if({str(st["running"]).lower()}) setTimeout(function(){{location.replace('/picks')}}, 60000);</script>"""
    return page("시간별 추천", body)


def _pick_form(fields: dict, inner: str) -> str:
    if not is_owner():
        return ""
    hid = "".join(f'<input type="hidden" name="{k}" value="{html.escape(str(v))}">' for k, v in fields.items())
    return f'<form class="inline" method="post" action="/api/picks">{hid}{inner}</form>'


# ─────────────────────────── 관심 그룹 (groups.py)

def _pct(v: float, digits: int = 1) -> str:
    if v != v:
        return "—"
    cls = "pos" if v > 0 else "neg" if v < 0 else ""
    return f'<span class="{cls}">{v:+.{digits}f}%</span>'


def _post_form(action: str, fields: dict, inner: str, cls: str = "inline") -> str:
    if not is_owner():                 # 보기 전용에는 그룹을 바꾸는 단추를 그리지 않는다(눌러도 403)
        return ""
    hid = "".join(f'<input type="hidden" name="{k}" value="{html.escape(str(v))}">' for k, v in fields.items())
    return f'<form class="{cls}" method="post" action="/api/group"><input type="hidden" name="action" value="{action}">{hid}{inner}</form>'


def groups_html(o: dict, err: str = "") -> bytes:
    cards = []
    for g in groups.load_all():
        idx = groups.register(g["id"], g["codes"])[1] if g["codes"] else None     # 상세 화면과 같은 기준(차트 첫날=1,000)
        if idx is not None and len(idx):
            r = groups.returns(idx.close)
            val = (f'<span class="v">{idx.close.iloc[-1]:,.0f}</span>'
                   f'<span class="r">1일 {_pct(r[1])} · 1달 {_pct(r[20])}</span>')
        else:
            val = '<span class="m">종목을 담으면 지수가 만들어집니다</span>'
        cards.append(f'<a class="gcard" href="/group/{g["id"]}?{qs(o)}"><b>{html.escape(g["name"])}</b>'
                     f'<span class="m">{len(g["codes"])}종목</span>{val}</a>')
    body = (topbar("groups", o)
            + '<h1>관심 그룹</h1><p class="lead">종목을 묶어 두면 그 그룹만의 지수와 수급 차트가 만들어집니다. '
              '예: 반도체 · 내 보유 종목 · 금융주</p>'
            + (f'<div class="msg">{html.escape(err)}</div>' if err else "")
            + _post_form("create", {}, '<input type="text" name="name" maxlength="40" required '
                         'placeholder="새 그룹 이름 — 예: 반도체"><button class="btn1" type="submit">+ 그룹 만들기</button>',
                         cls="inline group-create")
            + (f'<div class="gcards">{"".join(cards)}</div>' if cards else
               '<div class="empty">아직 그룹이 없습니다.'
               + (" 위에 이름을 적고 「그룹 만들기」를 누르세요." if is_owner() else "") + '</div>'))
    return page("관심 그룹", body)


GROUP_ADD_JS = """
<script>
(function(){
  var box=document.getElementById('gq'), hits=document.getElementById('ghits'), form=document.getElementById('gadd'), t, rev=0, i=-1, enterGo=false;
  if(!box) return;
  function close(){hits.replaceChildren();i=-1;}
  function go(){
    var v=box.value.trim(), req=++rev;
    if(!v){close();return;}
    fetch('/api/search?q='+encodeURIComponent(v)).then(r=>r.json()).then(function(rows){
      if(req!==rev) return; hits.replaceChildren();
      rows.forEach(function(r){var a=document.createElement('a'), s=document.createElement('span');
        a.href='#';a.className='add';a.textContent=r.name;s.className='c';s.textContent=r.code+' · '+r.market;a.appendChild(s);
        a.addEventListener('click',function(e){e.preventDefault();form.code.value=r.code;form.submit();});
        hits.appendChild(a);});
      if(!rows.length){var d=document.createElement('div');d.className='search-status';d.textContent='찾은 종목이 없습니다.';hits.appendChild(d);}
      i=-1; if(enterGo&&rows.length){enterGo=false;hits.querySelector('a').click();} enterGo=false;
    }).catch(function(){enterGo=false;});
  }
  box.addEventListener('input',function(){enterGo=false;clearTimeout(t);close();t=setTimeout(go,150);});
  box.addEventListener('keydown',function(e){
    if(e.isComposing||e.keyCode===229) return;
    var as=hits.querySelectorAll('a');
    if(e.key==='ArrowDown'||e.key==='ArrowUp'){                     // 키보드로 고르는 종목을 강조(사용자 요청 2026-10-08)
      if(!as.length) return; e.preventDefault();
      if(i>=0&&as[i]) as[i].classList.remove('sel');
      i = e.key==='ArrowDown' ? (i+1)%as.length : (i<=0?as.length-1:i-1);
      as[i].classList.add('sel'); as[i].scrollIntoView({block:'nearest'});
    } else if(e.key==='Enter'){e.preventDefault();
      if(as.length) (as[i]||as[0]).click();
      else if(box.value.trim()){enterGo=true;clearTimeout(t);go();}}
    else if(e.key==='Escape'){close();}});
  document.addEventListener('click',function(e){if(!hits.contains(e.target)&&e.target!==box)close();});
})();
</script>
"""


def group_html(gid: str, o: dict, err: str = "") -> bytes | None:
    g = groups.get(gid)
    if g is None:
        return None
    name, codes = g["name"], g["codes"]
    vc, idx, flow = groups.register(gid, codes)
    head = (topbar("groups", o) + f'<a class="back" href="/groups?{qs(o)}">← 관심 그룹 목록</a>'
            + (f'<div class="msg">{html.escape(err)}</div>' if err else ""))
    add = ('<form id="gadd" method="post" action="/api/group" style="display:none"><input type="hidden" name="action" value="add">'
           f'<input type="hidden" name="gid" value="{gid}"><input type="hidden" name="code"></form>'
           '<div class="srch"><input id="gq" autocomplete="off" placeholder="이 그룹에 담을 종목 찾기 — 이름이나 코드를 치고 고르면 바로 담깁니다">'
           '<div class="hits" id="ghits"></div></div>') if is_owner() else ""
    rows = []
    for m in groups.members(codes):
        r = m["ret"]
        rows.append(
            f'<tr><td class="k"><a href="/stock/{m["code"]}?{qs(o)}">{html.escape(m["name"])}</a>'
            f'<span class="c2">{m["code"]}</span></td>'
            f'<td>{m["close"]:,.0f}</td><td>{_pct(r[1])}</td><td>{_pct(r[20])}</td><td class="hide-m">{_pct(r[60])}</td>'
            f'<td>{_post_form("remove", {"gid": gid, "code": m["code"]}, "<button class=xbtn type=submit title=그룹에서_빼기>빼기</button>", cls="")}</td></tr>')
    table = ('<div class="wrap wide"><table><thead><tr><th class="k">종목</th><th>현재가</th><th>1일</th><th>1달</th>'
             f'<th class="hide-m">3달</th><th></th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
             if rows else '<div class="empty">아직 담은 종목이 없습니다.'
             + (" 위 검색창에서 종목을 찾아 담으세요." if is_owner() else "") + '</div>')
    manage = ('<details style="margin:18px 0"><summary class="note" style="cursor:pointer">그룹 이름 바꾸기 · 삭제</summary>'
              + _post_form("rename", {"gid": gid}, f'<input type="text" name="name" maxlength="40" required value="{html.escape(name)}">'
                           '<button class="btn2" type="submit">이름 바꾸기</button>')
              + _post_form("delete", {"gid": gid}, '<button class="xbtn" type="submit" '
                           "onclick=\"return confirm('이 그룹을 삭제할까요? 종목 자료는 지워지지 않습니다.')\">그룹 삭제</button>")
              + '</details>')
    if not is_owner():
        manage = ""
    if idx is None or not len(idx):
        chart, stats = "", ""
    else:
        r = groups.returns(idx.close, (1, 20, 60, 240))
        stats = ('<div class="stats">'
                 f'<div class="stat"><span class="l">그룹 지수</span><span class="n">{idx.close.iloc[-1]:,.0f}</span></div>'
                 + "".join(f'<div class="stat"><span class="l">{lab}</span><span class="n">{_pct(r[n])}</span></div>'
                           for n, lab in ((1, "1일"), (20, "1달"), (60, "3달"), (240, "1년")))
                 + f'<div class="stat"><span class="l">기준일</span><span class="n" style="font-size:15px">{idx.index[-1]:%Y-%m-%d}</span></div></div>')
        if flow is None or len(flow) < 60:
            chart = '<div class="empty">수급 자료가 60거래일 이상인 종목이 있어야 차트를 그릴 수 있습니다.</div>'
        else:
            doc = viewer.build(vc, name, None, inline_js="/static/", sig_dates=None, sig_label="", body_only=True)
            inner = doc[doc.index("<body>") + 6:doc.index("</body>")]
            # 종목용 문구를 그룹에 맞게 바꾼다 — 차트 자체는 그대로다
            inner = re.sub(r"<h1>.*?</h1>", "", inner, count=1)
            inner = re.sub(r"종가 ([\d,]+)원", r"그룹 지수 \1", inner, count=1)
            inner = inner.replace("<h2>기간별 순매수 (수량)</h2>", "<h2>기간별 순매수 (금액, 백만원)</h2>")
            inner = inner.replace("<h2>주체별 보유 요약</h2>", "<h2>주체별 보유 요약 (금액, 백만원)</h2>")
            inner = re.sub(r'<p class="note">데이터: .*?</p>', "", inner, count=1)
            chart = ('<div class="stk-main">' + inner
                     + '<p class="note"><b>그룹 지수</b> = 담은 종목의 하루 수익률을 같은 비중으로 평균해 이은 값(차트 첫날 = 1,000). '
                       '고가·저가는 종목별 고점·저점의 평균이라 실제 장중 지수보다 넓게 잡힙니다. '
                       '<b>그룹 수급</b>은 주체별 순매수 수량 × 그날 종가를 더한 <b>금액(백만원)</b> 기준입니다 — '
                       '위 표의 「수량」「평균단가」는 그룹에서는 각각 금액, 매집 구간의 평균 지수를 뜻합니다.</p></div>')
    body = (head + f'<h1>{html.escape(name)} <span class="chip">{len(codes)}종목</span></h1>' + stats + add
            + '<h2>담은 종목</h2>' + table + manage + chart + GROUP_ADD_JS)
    return page(f"{name} — 관심 그룹", body)


def group_action(form: dict) -> tuple[str, str]:
    """POST /api/group 처리 → (돌아갈 주소, 오류 문구)."""
    f = {k: v[-1] for k, v in form.items()}
    act, gid = f.get("action", ""), f.get("gid", "")
    try:
        if act == "create":
            return f"/group/{groups.create(f.get('name', ''))}", ""
        if act == "rename":
            groups.rename(gid, f.get("name", ""))
        elif act == "delete":
            groups.delete(gid)
            return "/groups", ""
        elif act == "add":
            groups.add(gid, f.get("code", ""))
        elif act == "remove":
            groups.remove(gid, f.get("code", ""))
        else:
            return "/groups", "알 수 없는 요청입니다."
    except ValueError as e:
        return (f"/group/{gid}" if groups.get(gid) else "/groups"), str(e)
    back = f.get("back", "")
    return (back if back.startswith("/stock/") else f"/group/{gid}"), ""


def group_picker(code: str) -> str:
    """종목 화면의 「관심 그룹에 담기」."""
    if not is_owner():
        return ""
    gs = groups.load_all()
    if not gs:
        return '<p class="note"><a href="/groups">관심 그룹을 만들면</a> 이 종목을 담아 그룹 지수·차트를 볼 수 있습니다.</p>'
    opt = "".join(f'<option value="{g["id"]}">{html.escape(g["name"])}{" ✓" if code in g["codes"] else ""}</option>' for g in gs)
    return _post_form("add", {"code": code, "back": f"/stock/{code}"},
                      f'<select name="gid" aria-label="관심 그룹">{opt}</select>'
                      '<button class="btn2" type="submit">+ 관심 그룹에 담기</button>')


def stock_html(code: str, o: dict) -> bytes:
    uni = core.universe().set_index("Code")
    name = str(uni.Name.get(code, code))
    if o["kind"] in osc60.KINDS:          # 60분봉 검색식은 일봉 차트에 찍을 신호일이 없다
        sd = []
    else:
        sd = signal_dates(o, code)        # 양봉 검색식은 양봉이 시작된 날만 찍는다(full_mask)
    label = label_of(o["kind"], o.get("tf", "d"))

    # 차트 캐싱 (Phase 4)
    # 캐시 키에 검색식·설정을 넣는다 — 코드만 쓰면 다른 검색식으로 들어와도 옛 신호 마커·범례가 1시간 남는다
    #   기간(days·unit)은 차트와 무관하므로 뺀다 — 넣으면 기간만 바꿔도 같은 차트를 다시 만든다(0.4초)
    chart_o = {k: v for k, v in o.items() if k not in ("days", "unit", "start", "end")}
    ckey = f"{code}_{hashlib.sha1(qs(chart_o).encode()).hexdigest()[:8]}"
    cached = viewer_cache.ChartCache.get(ckey)
    if cached:
        inner = cached
        cache_note = " (캐시)"
    else:
        doc = viewer.build(code, name, None, inline_js="/static/",
                           sig_dates=sd, sig_label=label, body_only=True,
                           minute_panel=minute.panel(code))
        inner = doc[doc.index("<body>") + 6:doc.index("</body>")]
        viewer_cache.ChartCache.set(ckey, inner)
        cache_note = ""

    nav = (topbar("stock", o) + '<div class="stock-nav">' + f'<a class="back" href="/?{qs(o)}">← 후보 목록</a>'
           '<div class="srch"><input id="q" autocomplete="off" '
           'placeholder="다른 종목 찾기 — 예: 삼성전자 · 005930"><div class="hits" id="hits"></div></div>'
           + '</div><div class="stock-meta">' + group_picker(code) + theme_line(code)
           + ('<button class="btn2 theme-expand" id="theme-expand" aria-expanded="false">테마 더 보기</button>' if themes.of(code) else '')
           + defense_line(code) + '</div>')
    note = (f'<p class="note">▲ 는 <b>{html.escape(label)}</b> 신호가 난 날입니다'
            f'({len(sd)}회, 현재 설정 기준). <b>일봉에만 찍습니다</b> — 주·월봉은 한 봉이 여러 날을 '
            f'덮어 발생일을 가리키지 못합니다.{cache_note}</p>')
    dart_section = dartreport_v3.render_dart_section_v3(code, name)
    ana_section = analysis.render_section(code, name)
    return page(f"{name} 수급분석",
                nav + STOCK_GRID_CSS
                + '<div class="stk"><div class="stk-main">' + inner + note
                + '</div><aside class="stk-side"><p class="side-title">실시간 호가</p>' + realtime.section(code) + '</aside></div>'
                # 오늘 봉 스크립트는 호가창(#rt, 날짜를 싣는다) **뒤에** 둬야 한다 — 앞에 두면 못 찾고 멈춘다
                + realtime.DAILY_JS
                + ana_section + dart_section
                + SEARCH_JS)


# 차트 오른쪽에 호가창(사용자 요청 2026-09-29). 좁은 화면에선 차트 아래로 내려간다.
STOCK_GRID_CSS = """<style>
.stk{display:grid;grid-template-columns:minmax(0,1fr) 290px;gap:var(--card-gap);align-items:start}
.stk-side{min-width:0;position:sticky;top:10px;background:var(--surface);
  border:1px solid var(--box);border-radius:12px;padding:14px;box-shadow:var(--shadow)}
.stk-side .rt-ob tfoot td{font-size:12px}
@media(max-width:900px){.stk{grid-template-columns:minmax(0,1fr)}.stk-side{position:static}}
</style>"""


_DARTLINES: dict[str, tuple[float, bytes]] = {}
_DARTLINES_LOCK = threading.Lock()


def dartlines_json(code: str) -> bytes:
    """차트 재무 박스용 — 기간마다 연결·별도 **둘 다**(dart_collector both=True). 10분 캐시:
    한 종목에 최대 48회(6년×4분기×2) 호출이라 새로고침마다 다시 받지 않는다."""
    now = time.time()
    with _DARTLINES_LOCK:
        hit = _DARTLINES.get(code)
        if hit and now - hit[0] < 600:
            return hit[1]
    try:
        d = dart_collector.collect_dart_financials(code, both=True)
        keep = ("year", "quarter", "fs_div", "revenue", "op_profit", "net_income", "cash")
        body = json.dumps({"records": [{k: r.get(k) for k in keep} for r in d["records"]]},
                          ensure_ascii=False).encode()
    except Exception as e:                                  # noqa: BLE001
        return json.dumps({"error": str(e)[:120]}, ensure_ascii=False).encode()   # 실패는 캐시하지 않는다
    with _DARTLINES_LOCK:
        _DARTLINES[code] = (now, body)
    return body


def search_json(q: str) -> bytes:
    found = core.search_stocks(q, limit=20)
    rows = []
    for r in found.itertuples():
        cap = f"{r.marcap_eok/10000:,.1f}조" if r.marcap_eok == r.marcap_eok else ""
        rows.append({"code": str(r.Code), "name": str(r.Name), "market": str(r.Market),
                     "cap": cap, "url": f"/stock/{r.Code}"})
    return json.dumps(rows, ensure_ascii=False).encode("utf-8")


# ─────────────────────────── 서버

def same_origin(headers) -> bool:
    """브라우저가 보낸 Origin(없으면 Referer)이 이 앱(localhost·127.0.0.1)인가. 둘 다 없으면(브라우저 밖 도구) 통과."""
    src = headers.get("Origin") or headers.get("Referer") or ""
    if not src:
        return True
    host = urllib.parse.urlparse(src).hostname or ""
    return host in ("localhost", "127.0.0.1")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):        # 요청마다 stderr 로 쏟지 않는다
        pass

    def send(self, body: bytes, ctype="text/html; charset=utf-8", code=200,
             cache_control="no-store", etag: str | None = None):
        # HTML contains three chart datasets. Compress the wire payload, not the data.
        enc = self.headers.get("Accept-Encoding", "")
        accepts_gzip = any(p.strip().split(";")[0] == "gzip" and
                           not re.search(r";\s*q=0(?:\.0*)?(?:\s|;|$)", p)
                           for p in enc.split(","))
        compressed = len(body) > 4096 and accepts_gzip
        if compressed:
            body = gzip.compress(body, compresslevel=3)
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("Vary", "Accept-Encoding")
        if compressed:
            self.send_header("Content-Encoding", "gzip")
        if etag:
            self.send_header("ETag", etag)
        self.end_headers()
        self.wfile.write(body)

    def send_static(self, path: Path, versioned: bool):
        st = path.stat()
        etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
        cache = "private, max-age=31536000, immutable" if versioned else "private, no-cache"
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", cache)
            self.send_header("Vary", "Accept-Encoding")
            self.end_headers()
            return
        ctype = "text/css; charset=utf-8" if path.suffix == ".css" else "application/javascript; charset=utf-8"
        self.send(path.read_bytes(), ctype, cache_control=cache, etag=etag)

    def authorized(self) -> bool:
        """FLOWAPP_PASSWORD 가 있으면 Basic 인증. 실패 시 401 을 보내고 False.
        통과하면 이 요청의 권한을 적어 둔다: 관리자 비밀번호 = owner, 보기 전용 비밀번호 = viewer."""
        _REQ.role = "owner"
        if urllib.parse.urlparse(self.path).path == "/healthz":
            _REQ.role = "viewer"
            return True
        if not PASSWORD:
            return True
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                pw = base64.b64decode(h[6:]).decode("utf-8").partition(":")[2]
            except (ValueError, UnicodeDecodeError):
                pw = ""
            if hmac.compare_digest(pw.encode(), PASSWORD.encode()):
                return True
            if VIEW_PASSWORD and hmac.compare_digest(pw.encode(), VIEW_PASSWORD.encode()):
                _REQ.role = "viewer"
                return True
        body = "로그인이 필요합니다".encode()
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="flowapp", charset="UTF-8"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return False

    def stream_rt(self, code: str) -> None:
        """실시간 현재가·호가를 SSE 로 흘린다. 브라우저가 떠나면(쓰기 실패) 구독을 푼다.
        화면 갱신은 초당 최대 4회로 묶는다 — 호가는 초당 수십 건 온다(실측 10초 83건)."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        hub = realtime.HUB
        hub.subscribe(code)
        try:
            ver, last, seq = -1, 0.0, None       # seq: 이미 보낸 마지막 체결 순번(처음엔 최근 30건)
            while True:
                snap = hub.wait(code, ver, 15, seq)
                if snap["ver"] == ver:
                    self.wfile.write(b": ping\n\n")      # 연결 확인(끊겼으면 여기서 예외)
                    self.wfile.flush()
                    continue
                gap = 0.25 - (time.monotonic() - last)
                if gap > 0:
                    time.sleep(gap)
                    snap = hub.snapshot(code, seq)   # 쉬는 동안 온 체결까지 한꺼번에
                ver, last, seq = snap["ver"], time.monotonic(), snap["seq"]
                self.wfile.write(b"data: " + json.dumps(snap, ensure_ascii=False).encode() + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            hub.unsubscribe(code)

    def do_POST(self):                # noqa: N802
        if not self.authorized():
            return
        u = urllib.parse.urlparse(self.path)
        if not is_owner():             # 보기 전용: 바꾸는 요청(데이터 받기·그룹 편집)은 전부 막는다
            self.send("보기 전용 접속에서는 할 수 없습니다".encode(), "text/plain; charset=utf-8", 403)
            return
        try:
            if u.path == "/api/update":
                with _UPDATE_LOCK:
                    if not _UPDATE["running"]:
                        threading.Thread(target=update_data_async, daemon=True).start()
                    status = _UPDATE["log"]
                    is_done = _UPDATE["done"]

                steps, total = steps_view()
                resp = json.dumps({"status": status, "done": is_done, "steps": steps, "total": total}, ensure_ascii=False)
                self.send(resp.encode("utf-8"), "application/json; charset=utf-8")
            elif u.path == "/api/picks":
                n = min(int(self.headers.get("Content-Length") or 0), 10_000)
                form = urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
                to, msg = picks_action(form)
                self.send_response(303)
                self.send_header("Location", to + "?" + urllib.parse.urlencode({"msg": msg}))
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif u.path == "/api/top3":
                n = min(int(self.headers.get("Content-Length") or 0), 10_000)
                form = urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
                to, msg = top3_action(form)
                self.send_response(303)
                self.send_header("Location", to + "?" + urllib.parse.urlencode({"msg": msg}))
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif u.path == "/api/watch":
                n = min(int(self.headers.get("Content-Length") or 0), 10_000)
                form = urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
                to, msg = watch_action(form)
                self.send_response(303)
                self.send_header("Location", to + "?" + urllib.parse.urlencode({"msg": msg}))
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif u.path in ("/api/settings", "/api/app") and appsettings.enabled():
                if not same_origin(self.headers):       # 다른 사이트가 이 PC 의 앱으로 키를 바꾸거나 끄게 하지 않는다
                    self.send("다른 사이트에서 온 요청은 받지 않습니다".encode(), "text/plain; charset=utf-8", 403)
                    return
                n = min(int(self.headers.get("Content-Length") or 0), 10_000)
                form = urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
                act = (form.get("do") or [""])[-1]
                if u.path == "/api/app" and act in ("update", "quit"):
                    note = ("새 버전을 받고 다시 시작합니다. 30초쯤 뒤 이 창을 새로 고치세요." if act == "update"
                            else "프로그램을 종료했습니다. 이 창은 닫아도 됩니다.")
                    self.send(page("flowapp", f'<h1>{note}</h1>'))
                    appsettings.exit_later(appsettings.EXIT_UPDATE if act == "update" else 0)
                    return
                msg = appsettings.save(form) if u.path == "/api/settings" else ""
                if act == "test":
                    ok, m = appsettings.test_kiwoom()
                    msg = f"{msg} {m}".strip()
                self.send_response(303)
                self.send_header("Location", "/settings?" + urllib.parse.urlencode({"msg": msg}))
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif u.path == "/api/group":
                n = min(int(self.headers.get("Content-Length") or 0), 10_000)
                form = urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
                to, err = group_action(form)
                if err:
                    to += ("&" if "?" in to else "?") + urllib.parse.urlencode({"err": err})
                self.send_response(303)          # 새로고침해도 다시 제출되지 않게 GET 으로 돌려보낸다
                self.send_header("Location", to)
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self.send(page("없음", '<h1>404</h1>'), code=404)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.send(page("오류", f"<h1>오류</h1><pre>{html.escape(repr(e))}</pre>"), code=500)

    def do_GET(self):                 # noqa: N802
        if not self.authorized():
            return
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        _REQ.tf = (q.get("tf") or ["d"])[-1] if (q.get("tf") or ["d"])[-1] in core.TFS else "d"      # 봉 단위(2026-10-06) — opts 보다 먼저
        try:
            if u.path == "/healthz":
                self.send(b"ok", "text/plain; charset=utf-8")
            elif u.path.startswith("/api/rt/"):
                code = u.path.removeprefix("/api/rt/")
                if not valid_code(code):
                    self.send(b"bad code", "text/plain; charset=utf-8", 400)
                    return
                self.stream_rt(code)
            elif u.path.startswith("/api/dartlines/"):
                code = u.path.removeprefix("/api/dartlines/")
                if not valid_code(code):
                    self.send(b'{"error":"bad code"}', "application/json; charset=utf-8", 400)
                    return
                self.send(dartlines_json(code), "application/json; charset=utf-8")
            elif u.path.startswith("/api/today/"):
                code = u.path.removeprefix("/api/today/")
                if not valid_code(code):
                    self.send(b'{"error":"bad code"}', "application/json; charset=utf-8", 400)
                    return
                self.send(json.dumps(today_bar(code)).encode("utf-8"), "application/json; charset=utf-8")
            elif u.path.startswith("/api/minute/"):
                code = u.path.removeprefix("/api/minute/")
                try:
                    m = int((q.get("m") or ["5"])[0])
                except ValueError:
                    m = 0
                if not valid_code(code) or m not in minute.SCOPES:
                    self.send(json.dumps({"error": "잘못된 종목코드 또는 주기"}, ensure_ascii=False).encode(),
                              "application/json; charset=utf-8", 400)
                    return
                self.send(minute.payload(code, m), "application/json; charset=utf-8")
            elif u.path.startswith("/static/") and u.path[8:] in STATIC_FILES:
                asset = STATIC_FILES[u.path[8:]]
                self.send_static(asset, q.get("v") == [str(asset.stat().st_mtime_ns)])
            elif u.path == "/plotly.js":
                import plotly.offline as po
                self.send(po.get_plotlyjs().encode("utf-8"),
                          "application/javascript; charset=utf-8")
            elif u.path == "/api/update":
                steps, total = steps_view()
                with _UPDATE_LOCK:
                    resp = json.dumps({"status": _UPDATE["log"], "done": _UPDATE["done"], "steps": steps, "total": total},
                                    ensure_ascii=False)
                self.send(resp.encode("utf-8"), "application/json; charset=utf-8")
            elif u.path == "/api/search":
                self.send(search_json((q.get("q") or [""])[0]),
                          "application/json; charset=utf-8")
            elif u.path.startswith("/api/opinion/"):
                code = u.path.removeprefix("/api/opinion/")
                if not valid_code(code):
                    self.send(json.dumps({"error": "종목코드는 6자리(숫자·대문자)여야 합니다."}, ensure_ascii=False).encode(),
                              "application/json; charset=utf-8", 400)
                    return
                uni = core.universe().set_index("Code")
                nm = str(uni.Name.get(code, code))
                try:
                    dart = dart_collector.collect_dart_financials(code)
                except Exception:      # noqa: BLE001 — DART 가 없어도 나머지 근거로 쓴다
                    dart = None
                data = analysis.gather(code, nm, dart)
                # 견해는 유료 API 를 부른다 — 보기 전용은 관리자가 이미 만들어 둔 것만 본다
                out = opinion.generate(data) if is_owner() else (opinion.peek(data) or {
                    "model": opinion.MODEL, "text": "", "engine": "",
                    "error": "이 종목의 견해는 아직 없습니다 — 보기 전용 접속에서는 새로 만들 수 없습니다"})
                self.send(json.dumps({"html": opinion.render(out)},
                                     ensure_ascii=False).encode(),
                          "application/json; charset=utf-8")
            elif u.path.startswith("/api/analysis/"):
                code = u.path.removeprefix("/api/analysis/")
                if not valid_code(code):
                    self.send(json.dumps({"error": "종목코드는 6자리(숫자·대문자)여야 합니다."}, ensure_ascii=False).encode(),
                              "application/json; charset=utf-8", 400)
                    return
                uni = core.universe().set_index("Code")
                nm = str(uni.Name.get(code, code))
                self.send(json.dumps(analysis.fetch_payload(code, nm), ensure_ascii=False).encode(),
                          "application/json; charset=utf-8")
            elif u.path.startswith("/api/dart/"):
                code = u.path.removeprefix("/api/dart/")
                if not valid_code(code):
                    self.send(json.dumps({"error": "종목코드는 6자리(숫자·대문자)여야 합니다."}, ensure_ascii=False).encode(),
                              "application/json; charset=utf-8", 400)
                    return
                try:
                    payload = dartreport_v3.fetch_payload(code)
                    self.send(json.dumps(payload, ensure_ascii=False).encode(),
                              "application/json; charset=utf-8")
                except dart_collector.DartError as exc:
                    self.send(json.dumps({"error": str(exc)}, ensure_ascii=False).encode(),
                              "application/json; charset=utf-8", 502)
            elif u.path == "/picks":
                self.send(picks_html(opts(q), (q.get("msg") or [""])[0][:400] if is_owner() else ""))
            elif u.path == "/top3":
                self.send(top3_html(opts(q), (q.get("msg") or [""])[0][:400] if is_owner() else ""))
            elif u.path == "/track":
                self.send(track_html(opts(q)))
            elif u.path == "/api/top3":
                self.send(json.dumps(top3.load_state(), ensure_ascii=False, default=str).encode(), "application/json; charset=utf-8")
            elif u.path == "/api/picks":
                self.send(json.dumps(picker().status(), ensure_ascii=False).encode(), "application/json; charset=utf-8")
            elif u.path == "/settings" and appsettings.enabled():
                body = appsettings.SETTINGS_CSS + appsettings.page_body((q.get("msg") or [""])[0][:300])
                self.send(page("설정", topbar("settings", opts(q)) + body))
            elif u.path == "/api/app-version" and appsettings.enabled():
                self.send(json.dumps(appsettings.check_update(), ensure_ascii=False).encode(), "application/json; charset=utf-8")
            elif u.path == "/watch":
                if not is_owner():
                    self.send("보기 전용 접속에서는 볼 수 없습니다".encode(), "text/plain; charset=utf-8", 403)
                    return
                self.send(watch_html(opts(q), (q.get("msg") or [""])[0][:400]))
            elif u.path == "/api/watch":
                self.send(json.dumps(watcher().status() if is_owner() else {}, ensure_ascii=False).encode(),
                          "application/json; charset=utf-8")
            elif u.path == "/api/live":
                self.send(json.dumps(live_status(), ensure_ascii=False).encode(), "application/json; charset=utf-8")
            elif u.path == "/api/osc60":
                self.send(json.dumps(osc60.status(), ensure_ascii=False).encode(), "application/json; charset=utf-8")
            elif u.path == "/api/backtest":
                o = opts(q)
                if o["kind"] in osc60.KINDS:
                    self.send(json.dumps({"html": "<p class='lead'><b>60분봉 검색식은 과거 성적을 낼 수 없습니다.</b> "
                                          "분봉 이력을 저장해 두지 않기 때문입니다. 일봉·주봉 오실레이터 검색식의 성적을 참고하십시오.</p>"},
                                         ensure_ascii=False).encode(), "application/json; charset=utf-8")
                    return
                d = research.backtest(panel(), o)
                self.send(json.dumps({"html": research.render_backtest(d)},
                                     ensure_ascii=False, default=float).encode(),
                          "application/json; charset=utf-8")
            elif u.path == "/api/compare":
                o = opts(q)
                if o["kind"] in osc60.KINDS:       # 비교표는 패널 신호만 돌린다
                    o = dict(o, kind=DEFAULTS["kind"])
                d = research.compare(panel(), o)
                self.send(json.dumps({"html": research.render_compare(d)},
                                     ensure_ascii=False, default=float).encode(),
                          "application/json; charset=utf-8")
            elif u.path in ("/backtest", "/compare"):
                o = opts(q)
                if u.path == "/backtest":
                    title, api, tab = "이 검색식 성적", f"/api/backtest?{qs(o)}", "backtest"
                else:
                    title, api, tab = "검색식 비교", f"/api/compare?{qs(o)}", "compare"
                self.send(page(title, topbar(tab, o) + research.section(title, api)),
                          "text/html; charset=utf-8")
            elif u.path.startswith("/stock/"):
                code = u.path.split("/stock/", 1)[1].strip("/")[:6]
                if not valid_code(code):
                    self.send(page("오류", "<h1>잘못된 종목코드</h1>"), code=400)
                    return
                try:
                    _REQ.tf = "d"
                    self.send(stock_html(code, dict(opts(q), tf="d")))
                except SystemExit as e:
                    self.send(page("데이터 부족", f"<h1>수급 데이터 없음</h1><p>{code}는 수급 데이터가 없거나 부족합니다.</p><a href='/'>← 처음으로</a>"), code=404)
            elif u.path == "/groups":
                self.send(groups_html(opts(q), q.get("err", [""])[-1]))
            elif u.path.startswith("/group/"):
                body = group_html(u.path.split("/group/", 1)[1].strip("/")[:12], opts(q), q.get("err", [""])[-1])
                if body is None:
                    self.send(page("없음", '<h1>없는 그룹입니다</h1><a href="/groups">← 관심 그룹</a>'), code=404)
                else:
                    self.send(body)
            elif u.path in ("/", "/index.html"):
                o = opts(q)
                self.send(index_html(o))
                if o["kind"] not in osc60.KINDS:      # 결과에서 종목을 누르면 바로 뜨게, 차트 신호일을 미리 만들어 둔다
                    threading.Thread(target=full_mask, args=(o,), daemon=True).start()
            elif u.path == "/api/form":               # 검색식을 바꿀 때 폼만 다시 그린다(검색은 하지 않는다)
                self.send(cfg_form(opts(q)).encode("utf-8"))
            elif u.path == "/favicon.ico":
                self.send(b"", "image/x-icon", 404)
            else:
                self.send(page("없음", '<h1>404</h1><a href="/">← 처음으로</a>'), code=404)
        except BrokenPipeError:
            pass
        except Exception as e:        # noqa: BLE001
            import traceback
            traceback.print_exc()
            self.send(page("오류", f"<h1>오류</h1><pre>{html.escape(repr(e))}</pre>"
                                   '<a href="/">← 처음으로</a>'), code=500)


def open_browser(url: str) -> None:
    """WSL 이라 리눅스 브라우저가 없다. Windows 쪽으로 넘긴다. 윈도우 배포판은 기본 브라우저로 바로 연다."""
    if sys.platform == "win32":
        os.startfile(url)                                        # noqa: S606
        return
    for cmd in (["explorer.exe", url], ["xdg-open", url], ["open", url]):
        try:
            subprocess.run(cmd, timeout=15, capture_output=True)
            return
        except Exception:  # noqa: BLE001
            continue


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(prog="flowapp", description="수급분석 스캐너 (로컬 앱)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8700)))
    ap.add_argument("--host", default=os.environ.get("FLOWAPP_HOST", "127.0.0.1"),
                    help="0.0.0.0 이면 외부에 연다(서버). 그때는 FLOWAPP_PASSWORD 필수")
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--update", action="store_true",
                    help="서버를 띄우지 않고 화면의 '업데이트' 버튼과 같은 수집(시세→수급→패널)만 하고 끝낸다")
    a = ap.parse_args()
    if a.update:
        update_data_async()
        print(_UPDATE["log"])
        sys.exit(0 if _UPDATE["log"].startswith("✓") else 1)
    if a.host not in ("127.0.0.1", "localhost") and not PASSWORD:
        sys.exit("✗ 외부에 열려면 FLOWAPP_PASSWORD 를 설정하세요(잠금 없이 공개하지 않습니다).")

    global LIVE_ON
    LIVE_ON = os.environ.get("FLOWAPP_LIVE", "1") != "0"     # 오늘 봉(잠정) 붙이기 — 끄려면 FLOWAPP_LIVE=0
    appsettings.apply_env()            # 윈도우 배포판: 설정 화면에 저장한 DART·Anthropic 키
    tune_malloc()                      # 패널을 읽기 전에 — 놓은 큰 배열이 OS 로 돌아가게
    panel()                            # 첫 요청이 30초 걸리지 않도록 미리 만든다
    threading.Thread(target=board.data, daemon=True).start()       # 전광판 집계(수 초)를 첫 화면 전에 미리
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    url = f"http://localhost:{a.port}"
    print(f"■ flowapp → {url}   (Ctrl+C 로 종료)")
    if not a.no_open:
        threading.Timer(0.6, open_browser, args=(url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n종료합니다.")
        srv.shutdown()


if __name__ == "__main__":
    main()
