"""종목 찾기 화면 전광판 — 외국인·기관 5일 순매수대금 상위(코스피·코스닥 따로, 사용자 요청 2026-10-16).

대금 = Σ(최근 5거래일 순매수 수량 × 그날 종가). 수급 원본(raw/flow, 키움 ka10059)은 수량만 저장하므로 종가로 근사한다
(px_raw 는 수정 전 원주가 — 같은 날의 원 수량과 짝이 맞다). 수급 마지막 날이 전체 최신일이 아닌 종목은 뺀다.
집계는 raw/flow 지문(flow_state.json mtime)이 바뀔 때만 다시 한다 — 2,500여 파일을 읽는 데 수 초 걸린다."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

import core

N_DAYS, TOP = 5, 3
STATE = core.ROOT / "raw" / "meta" / "flow_state.json"
UNIVERSE = core.ROOT / "raw" / "meta" / "universe.csv"
_lock = threading.Lock()
_cache: dict = {"key": None, "data": None}


def _one(path) -> tuple | None:
    try:
        d = pd.read_csv(path, usecols=["dt", "px_raw", "frgn", "orgn"]).tail(N_DAYS)
    except Exception:
        return None
    if len(d) < N_DAYS:
        return None
    px = d.px_raw.abs().to_numpy(dtype=float)
    return (path.name[:6], int(d.dt.iloc[-1]),
            float((d.frgn.to_numpy(dtype=float) * px).sum()), float((d.orgn.to_numpy(dtype=float) * px).sum()))


def compute() -> dict:
    files = sorted(core.FLOW.glob("*.csv.gz"))
    with ThreadPoolExecutor(8) as ex:
        rows = [r for r in ex.map(_one, files) if r]
    df = pd.DataFrame(rows, columns=["Code", "dt", "frgn", "orgn"])
    if df.empty:
        return {"day": None, "boards": {}}
    last = df.dt.max()
    df = df[df.dt == last]
    uni = pd.read_csv(UNIVERSE, dtype={"Code": str}).set_index("Code")
    df["Name"], df["Market"] = df.Code.map(uni.Name), df.Code.map(uni.Market)
    boards = {}
    for mk in ("KOSPI", "KOSDAQ"):
        g = df[df.Market == mk]
        boards[mk] = {who: [(r.Code, r.Name, getattr(r, who)) for r in g.nlargest(TOP, who).itertuples()] for who in ("frgn", "orgn")}
    return {"day": pd.Timestamp(str(last)), "boards": boards}


def data() -> dict:
    key = STATE.stat().st_mtime if STATE.exists() else None
    with _lock:
        if _cache["key"] != key or _cache["data"] is None:
            _cache["data"], _cache["key"] = compute(), key
        return _cache["data"]


def eok(v: float) -> str:
    """원 → '1,234억' (1억 미만은 소수 한 자리)."""
    x = v / 1e8
    return f"{x:,.0f}억" if abs(x) >= 10 else f"{x:,.1f}억"
