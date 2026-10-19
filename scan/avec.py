"""AVEC식 차트읽기 가설 검정 — 「급등 뒤 두 번째 증거」「이탈→복귀→매물대 재진입」「돌파 뒤 상단 체류」「매물대(가격대별 거래대금)」
(사용자 요청 2026-10-07 "검색식들에 적용해서 더 완성" · 원문: 위키 80_기업산업분석/AVEC식 차트읽기).

정의(문턱값은 자료를 보기 전에 고정)
  H1 두 번째 증거 : 급등일 E = 종가 +15%↑ · 거래량 ≥ 20일 평균×3. E 뒤 20일 안에서 처음으로
                   ① 종가 ≥ E 시가(급등 시작점 방어, E 뒤 매일) ② 최근 5일 최저가 > E+1~t-5 최저가(저점 상승) ③ 고가 ≥ E 뒤 최고가×0.98 이고
                   그 전에 종가가 최고가×0.95 아래로 눌린 날이 있었다(재터치). 변형: +거래량 재유입(≥1.5배) · +이평 재수렴(|MA5−MA20|/종가 < 2%)
  H2 이탈→복귀→재진입 : 박스 = 15일 전에 끝난 40일 고가·저가. 이탈 = 종가 < 하단×0.97 → 복귀 = 종가 > 하단(첫날) → 재진입 = 복귀 뒤 30일 안 종가 ≥ 박스 중간(첫날).
                   변형: 상단(≥ 박스 고가) 재진입. 비교: 복귀 첫날(shakeout 과 같음).
  H3 돌파 뒤 체류  : 돌파일 = 종가 > 직전 60일 최고가(고가 기준). 유효 = 그 뒤 3일 연속 종가가 돌파 가격(직전 60일 최고가) 위 → 3일째가 신호.
                   가짜 = 3일 안에 종가가 돌파 가격 아래로 → 그날(피해야 할 자리) 성적도 잰다. 비교: 돌파일 당일.
  H4 매물대        : 120일 가격대별 거래대금(대표가 (고+저+종)/3 을 50개 로그 구간) → POC = 최대 구간. 상위 매물대 = 거래대금 ≥ 최대×0.6 인 구간들.
                   A 매물대 진입 = 종가가 POC 가격을 위로 돌파(전날 아래) · 양봉 · 거래량 ≥1.5배. B 돈 벌어가는 구간 = 종가가 상위 매물대 최상단 위로 처음(20일 안 없던) 올라섬.
측정·자료 : indsetup.dfull 과 같다 — marcap 2007~(상폐 포함, 상폐는 마지막가 청산) · 유동 10억↑ · 시장초과 · IS 2007~2019 / OOS 2020~.

    python avec.py --build > cache/avec_out.md
"""
from __future__ import annotations

import sys
import time
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

H = [1, 3, 5, 10, 20, 40, 60]
HL = {1: "1일", 3: "3일", 5: "1주", 10: "2주", 20: "1달", 40: "2달", 60: "3달"}
IS_END = np.datetime64("2019-12-31")
MIN_VAL = 10e8
EVENTS = core.CACHE / "avec_events.pkl"
# 문턱값
SURGE_RET, SURGE_V, SURGE_WIN = 0.15, 3.0, 20
BOX_N, BOX_K, BOX_DIP, REENTRY_WIN = 40, 15, 0.03, 30
BRK_N, HOLD_N = 60, 3
VP_N, VP_BINS, VP_ZONE = 120, 50, 0.6


def _first_after(ev_idx: np.ndarray, cond: np.ndarray, win: int, n: int) -> np.ndarray:
    """사건 ev_idx 마다 그 뒤 1~win 안에서 cond 가 처음 참인 날을 표시한다."""
    out = np.zeros(n, dtype=bool)
    for e in ev_idx:
        seg = cond[e + 1: min(e + 1 + win, n)]
        hit = np.flatnonzero(seg)
        if len(hit):
            out[e + 1 + hit[0]] = True
    return out


def features(o: pd.DataFrame) -> pd.DataFrame:
    c, h, l, op, v = (o[k] for k in ("close", "high", "low", "open", "volume"))
    n = len(o)
    cv, hv, lv, ov = (x.to_numpy(dtype="float64") for x in (c, h, l, op))
    vr = (v / v.rolling(20).mean().shift(1)).to_numpy()
    F = pd.DataFrame(index=o.index)
    # ── H1 급등 뒤 두 번째 증거
    ret = c / c.shift(1) - 1
    surge = ((ret >= SURGE_RET) & (vr >= SURGE_V)).fillna(False).to_numpy()
    h1 = np.zeros(n, dtype=bool); h1v = np.zeros(n, dtype=bool); h1m = np.zeros(n, dtype=bool)
    ma5, ma20 = c.rolling(5).mean().to_numpy(), c.rolling(20).mean().to_numpy()
    for e in np.flatnonzero(surge):
        start = ov[e]
        hi = hv[e]
        dipped = False
        for t in range(e + 1, min(e + 1 + SURGE_WIN, n)):
            if cv[t] < start:                                   # 시작점 아래로 닫히면 그 급등은 끝
                break
            hi = max(hi, hv[t])
            if cv[t] < hi * 0.95:
                dipped = True
            if t - e >= 6 and dipped and hv[t] >= hi * 0.98 and lv[t - 4:t + 1].min() > lv[e + 1:t - 4].min():
                h1[t] = True
                if vr[t] >= 1.5:
                    h1v[t] = True
                if abs(ma5[t] - ma20[t]) / cv[t] < 0.02:
                    h1m[t] = True
                break
    F["H1_두번째증거"], F["H1_+거래량재유입"], F["H1_+이평재수렴"] = h1, h1v, h1m
    F["H1_급등일(비교)"] = surge
    # ── H2 이탈 → 복귀 → 재진입
    box_lo = l.rolling(BOX_N).min().shift(BOX_K)
    box_hi = h.rolling(BOX_N).max().shift(BOX_K)
    below = (c < box_lo * (1 - BOX_DIP)).fillna(False).to_numpy()
    back = ((c > box_lo) & (c.shift(1) <= box_lo)).fillna(False).to_numpy()
    # 이탈 뒤 30일 안의 복귀 첫날
    ret_idx = np.flatnonzero(_first_after(np.flatnonzero(below), back, 30, n))
    lo_at = box_lo.to_numpy(); hi_at = box_hi.to_numpy()
    h2mid = np.zeros(n, dtype=bool); h2top = np.zeros(n, dtype=bool); h2back = np.zeros(n, dtype=bool)
    last = -10**9
    for r in ret_idx:
        if r - last < REENTRY_WIN:                               # 같은 이탈을 겹쳐 세지 않는다
            continue
        last = r
        h2back[r] = True
        mid, top = (lo_at[r] + hi_at[r]) / 2, hi_at[r]           # 복귀일 기준 박스
        mid_done = False
        for t in range(r + 1, min(r + 1 + REENTRY_WIN, n)):
            if cv[t] < lo_at[r] * (1 - BOX_DIP):                 # 다시 이탈하면 끝
                break
            if not mid_done and cv[t] >= mid:
                h2mid[t] = True; mid_done = True
            if cv[t] >= top:
                h2top[t] = True
                break
    F["H2_복귀첫날(비교)"], F["H2_박스중간재진입"], F["H2_박스상단재진입"] = h2back, h2mid, h2top
    # ── H3 돌파 뒤 상단 체류
    prev_hi = h.rolling(BRK_N).max().shift(1)
    brk = (c > prev_hi).fillna(False).to_numpy()
    lvl = prev_hi.to_numpy()
    h3ok = np.zeros(n, dtype=bool); h3fake = np.zeros(n, dtype=bool); h3day = np.zeros(n, dtype=bool)
    last = -10**9
    for b in np.flatnonzero(brk):
        if b - last < HOLD_N + 1:
            continue
        last = b
        h3day[b] = True
        L = lvl[b]
        held = True
        for k in range(1, HOLD_N + 1):
            t = b + k
            if t >= n:
                held = False
                break
            if cv[t] <= L:
                h3fake[t] = True
                held = False
                break
        if held:
            h3ok[b + HOLD_N] = True
    F["H3_돌파일(비교)"], F["H3_유효돌파(3일체류)"], F["H3_가짜돌파(3일안복귀)"] = h3day, h3ok, h3fake
    # ── H4 매물대 (가격대별 거래대금, 120일 롤링)
    tp = (hv + lv + cv) / 3
    amt = (c * v).to_numpy(dtype="float64")
    lp = np.log(np.where(tp > 0, tp, np.nan))
    lo_, hi_ = np.nanmin(lp), np.nanmax(lp)
    edges = np.linspace(lo_, hi_ + 1e-9, VP_BINS + 1)
    b_idx = np.clip(np.searchsorted(edges, lp, side="right") - 1, 0, VP_BINS - 1)
    M = np.zeros((n, VP_BINS))
    M[np.arange(n), b_idx] = np.where(np.isfinite(amt), amt, 0)
    cs = np.cumsum(M, axis=0)
    roll = cs.copy(); roll[VP_N:] = cs[VP_N:] - cs[:-VP_N]
    roll[:VP_N - 1] = np.nan
    poc_bin = np.nanargmax(np.where(np.isnan(roll), -1, roll), axis=1)
    centers = np.exp((edges[:-1] + edges[1:]) / 2)
    poc = centers[poc_bin]; poc[:VP_N - 1] = np.nan
    mx = np.nanmax(roll, axis=1)
    zone = roll >= VP_ZONE * mx[:, None]                        # 상위 매물대 구간들
    zone_idx = np.where(zone, np.arange(VP_BINS)[None, :], -1).max(axis=1)
    zone_top = np.where(zone_idx >= 0, np.exp(edges[np.clip(zone_idx + 1, 0, VP_BINS)]), np.nan)
    zone_top[:VP_N - 1] = np.nan
    poc_prev = np.r_[np.nan, poc[:-1]]
    c_prev = np.r_[np.nan, cv[:-1]]
    h4a = (cv > poc) & (c_prev <= poc_prev) & (cv > ov) & (vr >= 1.5)
    above_zone = cv > zone_top
    h4b = above_zone & ~pd.Series(above_zone).shift(1, fill_value=False).rolling(20, min_periods=1).max().astype(bool).to_numpy()
    F["H4_매물대진입(POC돌파·거래량)"], F["H4_돈벌어가는구간진입"] = np.nan_to_num(h4a).astype(bool), np.nan_to_num(h4b).astype(bool)
    F["H4_POC위(상태,비교)"] = np.nan_to_num(cv > poc).astype(bool)
    F = F.astype(bool)
    for k in H:
        F[f"f{k}"] = (c.shift(-k) / c - 1).astype("float32")
    F["liq"] = ((c * v).rolling(20).mean() >= MIN_VAL).fillna(False)
    F["ok"] = pd.Series(np.isfinite(poc), index=o.index) & box_lo.notna()
    return F


def build() -> pd.DataFrame:
    import bottom
    t = time.time()
    d = bottom.adjust(bottom.load("2006-01-01"))
    last_day = d.Date.max()
    parts = []
    for i, (code, g) in enumerate(d.groupby("Code", sort=False), 1):
        if len(g) < 300:
            continue
        o = pd.DataFrame({"open": g.aO.to_numpy(), "high": g.aH.to_numpy(), "low": g.aL.to_numpy(),
                          "close": g.aC.to_numpy(), "volume": (g.Amount / g.aC).to_numpy()}, index=pd.DatetimeIndex(g.Date))
        F = features(o)
        if o.index[-1] < last_day - pd.Timedelta(days=10):
            lastret = (float(o.close.iloc[-1]) / o.close - 1).astype("float32")
            for k in H:
                F[f"f{k}"] = F[f"f{k}"].fillna(lastret)
        F = F[F.ok & F.liq & (F.index >= "2007-01-01")].drop(columns=["ok", "liq"])
        sig_cols = [c_ for c_ in F.columns if not c_.startswith("f")]
        F = F[F[sig_cols].any(axis=1)]                           # 신호가 하나라도 있는 행만 저장(기준은 따로 센다)
        if F.empty:
            continue
        F = F.reset_index(names="dt"); F["Code"] = code
        parts.append(F)
        if i % 500 == 0:
            print(f"  {i} 종목 ({time.time() - t:.0f}초)", file=sys.stderr, flush=True)
    E = pd.concat(parts, ignore_index=True)
    print(f"  신호 행 {len(E):,} · 종목 {E.Code.nunique():,} ({time.time() - t:.0f}초)", file=sys.stderr, flush=True)
    return E


def market_mean() -> pd.DataFrame:
    """시장초과용 — combo 패널(같은 marcap)의 유동 유니버스 날짜별 평균 수익률."""
    import combo
    p = combo.build()
    L = p[p.liq]
    out = {}
    for k in H:
        col = f"fwd{k}" if f"fwd{k}" in p else None
        if col:
            out[k] = L.groupby("dt")[col].mean()
    return pd.DataFrame(out)


def main() -> None:
    if EVENTS.exists() and "--build" not in sys.argv:
        E = pd.read_pickle(EVENTS)
    else:
        E = build(); E.to_pickle(EVENTS)
    mm = market_mean()
    import combo
    p = combo.build()
    L = p[p.liq]
    base = {}
    for k in H:
        col = f"fwd{k}"
        if col in L:
            f = L[col].to_numpy(); ok = np.isfinite(f)
            base[k] = (float((f[ok] > 0).mean()), int(ok.sum()))
    E["dt"] = pd.to_datetime(E.dt)
    for k in H:
        if k in mm:
            E[f"e{k}"] = (E[f"f{k}"] - E.dt.map(mm[k])).astype("float32")
    dts = E.dt.to_numpy()
    is_, oos = dts <= IS_END, dts > IS_END
    sig_cols = [c for c in E.columns if c[:2] in ("H1", "H2", "H3", "H4")]
    hz = [k for k in H if f"e{k}" in E]
    print("# AVEC식 가설 검정 — 일봉 전 기간 2007~ (marcap·상장폐지 포함, IS 2007~2019 · OOS 2020~, 2026-10-07)\n")
    print("칸 = 오른 비율 (시장초과 %p). 기준 = combo 패널 유동 유니버스 전 행(1주·2주·1달·2달·3달).")
    print("| 기준(전 행) | " + " | ".join(f"{HL[k]} {100 * base[k][0]:.0f}%" for k in hz if k in base) + " |\n")
    print("| 시나리오 | 건수 IS/OOS | " + " | ".join(HL[k] for k in hz) + " | OOS 1달 | OOS 2달 |")
    print("|---|---:|" + "---|" * len(hz) + "---|---|")
    for s in sig_cols:
        m = E[s].to_numpy(dtype=bool)
        cells, oo = [], []
        for k in hz:
            f, e = E[f"f{k}"].to_numpy(), E[f"e{k}"].to_numpy()
            ok = m & np.isfinite(f) & np.isfinite(e)
            cells.append(f"{100 * (f[ok] > 0).mean():.0f}% ({100 * e[ok].mean():+.1f})" if ok.sum() >= 30 else "—")
        for k in (20, 40):
            f, e = E[f"f{k}"].to_numpy(), E[f"e{k}"].to_numpy()
            ok = m & oos & np.isfinite(f) & np.isfinite(e)
            oo.append(f"{100 * (f[ok] > 0).mean():.0f}% ({100 * e[ok].mean():+.1f})" if ok.sum() >= 30 else "—")
        print(f"| {s} | {int((m & is_).sum()):,}/{int((m & oos).sum()):,} | " + " | ".join(cells) + " | " + " | ".join(oo) + " |")
    # 최근 신호
    names = dict(zip(core.universe().Code, core.universe().Name))
    last = E.dt.max()
    print(f"\n## 최근 신호 ({(last - pd.Timedelta(days=7)):%m-%d} 이후, 유동)\n")
    for s in sig_cols:
        if "비교" in s:
            continue
        hits = E[E[s] & (E.dt >= last - pd.Timedelta(days=7))].sort_values("dt", ascending=False).head(10)
        print(f"- **{s}**: " + (", ".join(f"{names.get(str(c), c)}({d:%m-%d})" for d, c in zip(hits.dt, hits.Code)) or "없음"))


if __name__ == "__main__":
    main()
