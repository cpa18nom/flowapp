"""종목 화면의 「폭락 방어력」 줄 — 60일 베타 · 하락일 방어 (사용자 요청 2026-10-15).

근거는 `crash_resilience.py`(README 「폭락 주간에 버틴 종목의 사전 특징」 절): 2016~2026 폭락 주간 24건에서
베타가 낮을수록(24건 전부)·하락일 방어가 높을수록(23/24) 그 주에 덜 빠졌다. 정의도 그 스크립트와 같다.
  시장   = 전 영업일 20일 평균 거래대금 10억↑ 종목의 일수익률 중앙값(±31% 로 자름, 300종목↑ 날만)
  베타   = 최근 60거래일 cov(종목, 시장) / var(시장) — 60일이 다 있어야 낸다
  하락일 방어 = 최근 60거래일 중 시장이 내린 날의 (종목 평균 − 시장 평균), 그런 날이 10일↑ 있어야 낸다
저장된 봉만 쓴다(장중 잠정 봉 제외). 패널이 바뀔 때 한 번 전 종목을 계산해 둔다(≈1초).
"""
from __future__ import annotations

import threading

import numpy as np
import pandas as pd

WIN = 60
MIN_DOWN = 10
LIQ = 1e9
MIN_MKT = 300

_CACHE: dict = {"key": None, "v": None}
_LOCK = threading.Lock()


def compute(df: pd.DataFrame, stored) -> dict:
    """전 종목 {code: {beta, dfn, beta_pct, dfn_pct, liquid}} + 기준일. pct 는 유동 종목 안에서의 백분위(0~100)."""
    # 마지막 WIN+2 거래일 행만 위치로 뗀다 — 전 구간을 불리언 .loc 로 거르면 pandas 가 600만 칸 행 번호(48MB)를 패널 인덱스에
    # 캐시해 놓지 않는다(2026-10-19). 기준일까지의 마지막 날들이라 'stored 이하 · 그 첫날 이상' = 'days 안'이다.
    dt = df.dt.to_numpy()
    ok = dt <= np.datetime64(pd.Timestamp(stored))
    days = np.sort(pd.unique(dt[ok]))[-(WIN + 2):]
    pos = np.flatnonzero(ok & (dt >= days[0]))
    s = pd.DataFrame({c: df[c].to_numpy()[pos] for c in ("dt", "ret", "val20")}).assign(
        Code=np.asarray(df.Code.array.take(pos)).astype(str))[["dt", "Code", "ret", "val20"]]
    R = s.pivot(index="dt", columns="Code", values="ret").clip(-0.31, 0.31)
    L = s.pivot(index="dt", columns="Code", values="val20").reindex(columns=R.columns) >= LIQ
    Rl = R.where(L.shift(1, fill_value=False))
    mkt = Rl.median(axis=1).where(Rl.notna().sum(axis=1) >= MIN_MKT)
    W, m = R.iloc[-WIN:], mkt.iloc[-WIN:]
    if m.isna().any():
        return {"day": pd.Timestamp(days[-1]), "rows": {}}
    beta = (W.mul(m, axis=0).mean() - W.mean() * m.mean()) / m.var(ddof=0)
    beta = beta.where(W.notna().sum() == WIN)
    down = (m < 0).to_numpy()
    Wd = W[down]
    dfn = (Wd.mean() - m[down].mean()).where(Wd.notna().sum() >= MIN_DOWN)
    liq = L.iloc[-1].fillna(False)
    pb = beta[liq].rank(pct=True) * 100
    pd_ = dfn[liq].rank(pct=True) * 100
    rows = {}
    for c in R.columns:
        b, f = beta.get(c), dfn.get(c)
        if pd.isna(b) and pd.isna(f):
            continue
        rows[c] = {"beta": None if pd.isna(b) else float(b), "dfn": None if pd.isna(f) else float(f),
                   "beta_pct": None if pd.isna(pb.get(c, np.nan)) else float(pb[c]),
                   "dfn_pct": None if pd.isna(pd_.get(c, np.nan)) else float(pd_[c]),
                   "liquid": bool(liq.get(c, False))}
    return {"day": pd.Timestamp(days[-1]), "rows": rows, "n_down": int(down.sum())}


def get(df: pd.DataFrame, stored, key) -> dict:
    """패널 시각(key)이 같으면 계산해 둔 것을 쓴다."""
    with _LOCK:
        if _CACHE["key"] != key:
            _CACHE["v"], _CACHE["key"] = compute(df, stored), key
        return _CACHE["v"]


def _beta_word(b: float) -> str:
    if b < 0.5:
        return "방어형"
    if b < 0.9:
        return "시장보다 덜 출렁임"
    if b <= 1.1:
        return "시장과 비슷"
    return "시장보다 크게 출렁임"


def _rank(p: float) -> str:
    """유동 종목 안의 위치 — 반보다 아래면 하위, 위면 상위."""
    return f"하위 {max(p, 1):.0f}%" if p <= 50 else f"상위 {max(100 - p, 1):.0f}%"


def line(st: dict, code: str) -> str:
    """종목 화면 머리 한 줄(HTML). 값이 없으면 빈 문자열."""
    import html
    r = st["rows"].get(code)
    if not r:
        return ""
    parts = []
    if r["beta"] is not None:
        pct = f" · {_rank(r['beta_pct'])}" if r["beta_pct"] is not None else ""
        parts.append(f'<span class="chip" title="시장이 1% 움직일 때 이 종목이 움직인 정도(최근 60거래일). '
                     f'낮을수록 폭락 주간에 덜 빠졌다 — 2016~2026 폭락 24주 전부">'
                     f'베타 <b>{r["beta"]:.2f}</b> ({_beta_word(r["beta"])}{pct})</span>')
    if r["dfn"] is not None:
        pct = f" · {_rank(r['dfn_pct'])}" if r["dfn_pct"] is not None else ""
        parts.append(f'<span class="chip" title="최근 60거래일 중 시장이 내린 {st.get("n_down", 0)}일만 골라, '
                     f'이 종목이 시장보다 하루 평균 몇 %p 덜 빠졌나. 높을수록 폭락 주간에 덜 빠졌다 — 24주 중 23주">'
                     f'하락일 방어 <b>{100 * r["dfn"]:+.2f}%p</b>/일{pct}</span>')
    note = ("폭락 그 주에만 통하는 보험 — 두 달 보유로는 저베타 −0.7%p·방어 상위 −1.0%p(평소 대가). "
            f"순위는 거래대금 10억↑ 종목 안 · {st['day']:%m-%d} 종가 기준" + ("" if r["liquid"] else " · 거래대금 10억 미만이라 순위 없음"))
    return ('<p class="defense"><span class="lab">폭락 방어력</span>' + "".join(parts)
            + f'<span class="note">{html.escape(note)}</span></p>')
