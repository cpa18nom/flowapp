"""2025년부터 모든 검색식을 돌려, 신호 뒤 수익률로 사건을 나누고 '살아남은 쪽'의 공통 조건을 찾는다 (사용자 요청 2026-10-04).

사용자 절차
  1. 2025년부터 모든 검색식을 돌린다(화면과 같은 식 — core.make_signal, 거래대금 10억↑, 급등일 제외 기본값).
  2. 신호일 종가 기준 1·2·3·4주(5·10·15·20거래일)·2개월(40)·3개월(60) 뒤 수익률.
  3. 음수는 버린다. 단 1주에 음수였어도 2주에 양수면 살린다.
        A = 1주 양수 · B = 1주 음수 → 2주 양수(회복) · C = 1주·2주 모두 음수(버림)      '살림' = A + B
  4. 살린 쪽에 검색식 말고 다른 공통 조건(신호일에 알 수 있는 값)이 있는지 본다.
  5. 있으면 검색식에 반영 — 그 전에 **2025년 이전 구간에서도 같은 방향인지** 확인한다(2025~ 만 보고 고르면 그 장세에 맞춘 것일 뿐이다).

자료: 앱 패널(core.build_panel — 현재 상장 종목만, 상폐 없음). 수급(raw/flow)은 2021-01~ 라 수급 조건의 과거 확인은 2021~2024.
    python winners.py > winners_out.md          # 약 4분
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
import flowtool as ft  # noqa: E402

START = "2025-01-01"
MIN_VAL = 10e8
HZ = {5: "1주", 10: "2주", 15: "3주", 20: "4주", 40: "2개월", 60: "3개월"}
SKIP = set(core.OSC_STATE)          # 상태 검색식(양봉인 날 전부) — 시작일이 stoch_turn_up·osc_up_w 와 같은 사건이라 뺀다
PAST_FROM = "2015-01-01"            # 과거 확인 구간(수급 조건은 2021~)


def past_ret(L: pd.DataFrame, full: pd.DataFrame, n: int) -> np.ndarray:
    """신호일까지의 n거래일 수익률(종목의 연속 시계열에서)."""
    codes = pd.factorize(full.Code)[0]
    c = full.close.to_numpy(dtype=float)
    i = L.index.to_numpy()
    j = np.maximum(i - n, 0)
    ok = (i - n >= 0) & (codes[j] == codes[i])
    return np.where(ok, c[i] / c[j] - 1, np.nan)


def flow_features(codes) -> pd.DataFrame:
    """종목·날짜별 수급 값: 개인·세력 분산비율(0~1), 외국인·기관·개인의 최근 5일 순매수 ÷ 5일 거래량."""
    out = []
    for code in codes:
        d = ft.load(code)
        if d is None or len(d) < 30:
            continue
        d = d.set_index("dt") if "dt" in d.columns else d
        vol = d.vol.rolling(5).sum().replace(0, np.nan)
        x = pd.DataFrame({"Code": code, "dt": d.index,
                          "개인분산": ft.block(d[["ind"]].sum(axis=1))["분산비율"],
                          "세력분산": ft.block(d[ft.FORCE_PARTS].sum(axis=1))["분산비율"],
                          "외인5일": (d.frgn.rolling(5).sum() / vol).to_numpy(),
                          "기관5일": (d.orgn.rolling(5).sum() / vol).to_numpy(),
                          "개인5일": (d.ind.rolling(5).sum() / vol).to_numpy()})
        out.append(x)
    return pd.concat(out, ignore_index=True)


def build() -> pd.DataFrame:
    """전 구간 사건표: 검색식마다 신호일 한 줄 + 수익률 + 신호일 특징."""
    t = time.time()
    full = core.build_panel()
    L = core.apply_liquidity(full, MIN_VAL)
    L = L[L.dt >= PAST_FROM]
    print(f"패널 {len(full):,}행 → 유동 유니버스 {len(L):,}행 ({time.time() - t:.0f}초)", file=sys.stderr)
    L = L.assign(regime=core.market_up(core.apply_liquidity(full, MIN_VAL)).reindex(L.index).to_numpy())
    for h in HZ:
        L[f"r{h}"] = core.forward_return(L, h, full).to_numpy()
        L[f"x{h}"] = core.excess(L, f"r{h}").to_numpy()
    # 신호일 특징(전부 그날 장 마감 시점에 알 수 있는 값)
    c = L.close.astype(float)
    L["변동성순위"] = core.xrank(L, "vol60").to_numpy()            # 0=가장 조용
    L["모멘텀순위"] = core.xrank(L, "mom").to_numpy()              # 1=1년 상승률 최상위
    L["거래대금순위"] = core.xrank(L, "val20").to_numpy()
    L["52주고가비"] = (c / L.hh252).to_numpy()
    L["20일선이격"] = (c / L.ma20 - 1).to_numpy()
    L["60일선이격"] = (c / L.ma60 - 1).to_numpy()
    L["120일선이격"] = (c / L.ma120 - 1).to_numpy()
    L["200일선위"] = (c > L.ma200).to_numpy()
    L["이평정배열"] = ((L.ma20 > L.ma60) & (L.ma60 > L.ma120)).to_numpy()
    L["거래량배수"] = (L.volume / L.v20).to_numpy()
    L["당일등락"] = L.ret.to_numpy()
    L["몸통"] = (c / L.open - 1).to_numpy()
    L["위꼬리"] = ((L.high - np.maximum(L.open, L.close)) / c).to_numpy()
    L["회전율"] = L.turnover.to_numpy()
    L["5일수익"] = past_ret(L, full, 5)
    L["20일수익"] = past_ret(L, full, 20)
    L["60일수익"] = past_ret(L, full, 60)
    L["일봉오실양"] = L.osc_pos_d.fillna(False).to_numpy(dtype=bool)
    L["주봉오실양"] = L.osc_pos_w.fillna(False).to_numpy(dtype=bool)
    L["상승국면"] = L.regime.fillna(False).to_numpy(dtype=bool)
    ev = []
    for name, kind in core.SIGNALS.items():
        if kind in SKIP:
            continue
        m = core.make_signal(L, kind, full=full).to_numpy()
        e = L.loc[m, ["dt", "Code"] + [f"{p}{h}" for h in HZ for p in "rx"] + FEATS].copy()
        e["kind"], e["name"] = kind, name
        ev.append(e)
        print(f"  {kind}: {int(m.sum()):,}건", file=sys.stderr)
    E = pd.concat(ev, ignore_index=True)
    E["Code"] = E.Code.astype(str)
    t = time.time()
    fl = flow_features(sorted(E.Code[E.dt >= "2021-03-01"].unique()))
    E = E.merge(fl, on=["Code", "dt"], how="left")
    print(f"수급 {len(fl):,}행 붙임 ({time.time() - t:.0f}초)", file=sys.stderr)
    return E


NUM = ["변동성순위", "모멘텀순위", "거래대금순위", "52주고가비", "20일선이격", "60일선이격", "120일선이격", "거래량배수",
       "당일등락", "몸통", "위꼬리", "회전율", "5일수익", "20일수익", "60일수익"]
BOOL = ["200일선위", "이평정배열", "일봉오실양", "주봉오실양", "상승국면"]
FEATS = NUM + BOOL
FLOW = ["개인분산", "세력분산", "외인5일", "기관5일", "개인5일"]


def grade(e: pd.DataFrame) -> pd.Series:
    """A=1주 양수 · B=1주 음수→2주 양수 · C=둘 다 음수(버림). 2주 수익률이 아직 없는 사건은 뺀다."""
    g = pd.Series("C", index=e.index)
    g[(e.r5 <= 0) & (e.r10 > 0)] = "B"
    g[e.r5 > 0] = "A"
    return g.where(e.r5.notna() & e.r10.notna())


def stat(e: pd.DataFrame) -> dict:
    g = e.g.dropna()
    return {"n": len(g), "keep": 100 * float((g != "C").mean()) if len(g) else np.nan,
            **{f"r{h}": 100 * float(e[f"r{h}"].mean()) for h in HZ}, **{f"x{h}": 100 * float(e[f"x{h}"].mean()) for h in HZ},
            **{f"up{h}": 100 * float((e[f"r{h}"].dropna() > 0).mean()) for h in HZ}}


def main() -> None:
    cp = core.CACHE / "winners_events.pkl"
    if cp.exists() and "--rebuild" not in sys.argv:
        E = pd.read_pickle(cp)
    else:
        E = build()
        E.to_pickle(cp)
    E["g"] = grade(E)
    new, old = E[E.dt >= START], E[E.dt < START]
    print(f"# 2025년~ 검색식별 신호 뒤 수익률과 살아남은 쪽의 조건 ({new.dt.min():%Y-%m-%d} ~ {new.dt.max():%Y-%m-%d})\n")
    print("거래대금 10억↑ · 신호일 종가 진입 · 절대수익률 · A=1주 양수, B=1주 음수→2주 양수, C=1·2주 모두 음수(버림)\n")

    print("## 1. 검색식별 결과 (2025~)\n")
    print("| 검색식 | 건수 | A | B | C(버림) | " + " | ".join(f"{v} 평균(오른 비율)" for v in HZ.values()) + " | 4주 시장초과 |")
    print("|---|---:|---:|---:|---:|" + "---|" * len(HZ) + "---:|")
    for kind, e in new.groupby("kind", sort=False):
        g = e.g.dropna()
        if len(g) < 30:
            print(f"| {e.name.iloc[0]} | {len(e)} | — | — | — |" + " — |" * (len(HZ) + 1))
            continue
        s = stat(e)
        print(f"| {e.name.iloc[0]} | {len(g):,} | {100 * (g == 'A').mean():.0f}% | {100 * (g == 'B').mean():.0f}% | {100 * (g == 'C').mean():.0f}% | "
              + " | ".join(f"{s[f'r{h}']:+.1f}% ({s[f'up{h}']:.0f}%)" for h in HZ) + f" | {s['x20']:+.2f}%p |")
    u = new.drop_duplicates(["Code", "dt"])
    print(f"\n(참고) 같은 기간 유동 유니버스 아무 날: 사건표에는 신호일만 있어 직접 비교는 '4주 시장초과' 열로 한다.")

    print("\n## 2. 살린 쪽(A+B)과 버린 쪽(C)은 그 뒤 어떻게 됐나 (2025~, 전 검색식 합)\n")
    print("| 묶음 | 건수 | " + " | ".join(HZ.values()) + " |")
    print("|---|---:|" + "---|" * len(HZ))
    for lab, sel in (("A 1주 양수", new.g == "A"), ("B 1주 음수→2주 양수", new.g == "B"), ("C 버림", new.g == "C")):
        e = new[sel]
        print(f"| {lab} | {len(e):,} | " + " | ".join(f"{100 * e[f'r{h}'].mean():+.1f}%" for h in HZ) + " |")

    # 3. 조건 찾기 — 검색식마다, 특징마다 '위쪽 1/3'·'아래쪽 1/3'(참/거짓)로 나눠 살림 비율과 수익률을 본다.
    print("\n## 3. 조건 후보 — 2025~ 에서 좋아 보였고, 과거(2015~2024 · 수급은 2021~2024)에서도 같은 방향인 것\n")
    print("기준: 2025~ 에서 살림 비율 +4%p↑ 그리고 4주·2개월 평균수익 둘 다 개선 · 조건 쪽 100건↑. "
          "과거에서 살림 비율과 4주 시장초과가 둘 다 개선되면 「과거 확인 ○」.\n")
    rows = []
    for kind, e in new.groupby("kind", sort=False):
        e = e[e.g.notna()]
        if len(e) < 300:
            continue
        b = stat(e)
        o = old[(old.kind == kind) & old.g.notna()]
        for f in FEATS + FLOW:
            op = o if f not in FLOW else o[o.dt >= "2021-03-01"]
            ob = stat(op) if len(op) >= 300 else None
            if f in BOOL:
                cuts = [("참", lambda d, f=f: d[f] == True), ("거짓", lambda d, f=f: d[f] == False)]   # noqa: E712
            else:
                v = e[f].dropna()
                if len(v) < 300:
                    continue
                lo, hi = v.quantile(1 / 3), v.quantile(2 / 3)
                cuts = [(f"≥ {hi:.3g}", lambda d, f=f, hi=hi: d[f] >= hi), (f"≤ {lo:.3g}", lambda d, f=f, lo=lo: d[f] <= lo)]
            for lab, fn in cuts:
                s = e[fn(e)]
                if len(s) < 100:
                    continue
                st = stat(s)
                dk, d20, d40 = st["keep"] - b["keep"], st["r20"] - b["r20"], st["r40"] - b["r40"]
                if not (dk >= 4 and d20 > 0 and d40 > 0):
                    continue
                past = "자료 부족"
                pk = px = np.nan
                if ob is not None:
                    ps = op[fn(op)]
                    if len(ps) >= 100:
                        pst = stat(ps)
                        pk, px = pst["keep"] - ob["keep"], pst["x20"] - ob["x20"]
                        past = "○" if (pk > 0.5 and px > 0) else "✕"
                rows.append({"검색식": e.name.iloc[0], "kind": kind, "조건": f"{f} {lab}", "건수": len(s), "전체": len(e),
                             "살림": st["keep"], "d살림": dk, "d4주": d20, "d2개월": d40, "d4주초과": st["x20"] - b["x20"],
                             "과거": past, "과거d살림": pk, "과거d4주초과": px, "과거건수": len(op[fn(op)]) if ob is not None else 0})
    R = pd.DataFrame(rows)
    R.to_csv(core.CACHE / "winners_conditions.csv", index=False)
    if len(R):
        R = R.sort_values(["과거", "d살림"], ascending=[True, False])
        print("| 검색식 | 조건 | 건수/전체 | 살림 비율(차) | 4주 차 | 2개월 차 | 4주 초과 차 | 과거 확인 | 과거 살림 차 | 과거 4주초과 차 | 과거 건수 |")
        print("|---|---|---:|---:|---:|---:|---:|:-:|---:|---:|---:|")
        for r in R[R.과거 == "○"].itertuples():
            print(f"| {r.검색식} | {r.조건} | {r.건수:,}/{r.전체:,} | {r.살림:.0f}% ({r.d살림:+.1f}) | {r.d4주:+.1f}%p | {r.d2개월:+.1f}%p | {r.d4주초과:+.1f}%p | "
                  f"{r.과거} | {r.과거d살림:+.1f}%p | {r.과거d4주초과:+.2f}%p | {r.과거건수:,} |")
        n_o, n_x, n_q = (R.과거 == "○").sum(), (R.과거 == "✕").sum(), (R.과거 == "자료 부족").sum()
        print(f"\n2025~ 에서 기준을 넘은 조건 {len(R)}개 중 과거 확인 ○ {n_o}개 · ✕ {n_x}개 · 자료 부족 {n_q}개 "
              f"(전체 표: {core.CACHE / 'winners_conditions.csv'})")
        # 조건별로 몇 개 검색식에서 통했나
        R["특징"] = R.조건.str.split(" ").str[0] + " " + R.조건.str.split(" ").str[1].map(lambda s: "높음/참" if s in ("≥", "참") else "낮음/거짓")
        t = R.groupby("특징").agg(통과=("과거", lambda s: (s == "○").sum()), 탈락=("과거", lambda s: (s == "✕").sum()),
                                 평균d살림=("d살림", "mean")).sort_values("통과", ascending=False)
        print("\n### 조건별 — 몇 개 검색식에서 통했나\n")
        print("| 조건(방향) | 과거 확인 ○ 검색식 수 | ✕ 수 | 2025~ 살림 비율 차 평균 |")
        print("|---|---:|---:|---:|")
        for k, r in t.iterrows():
            print(f"| {k} | {r.통과} | {r.탈락} | {r.평균d살림:+.1f}%p |")


if __name__ == "__main__":
    main()
