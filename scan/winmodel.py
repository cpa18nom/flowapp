"""검색식 신호의 '오를 확률'을 한꺼번에 추정하는 모델 — 조건 46개 + 시장 상태를 합쳐 승률을 더 올릴 수 있나 (사용자 요청 2026-10-06).

사용자 요청: "내 검색식 기준으로 승률을 더 높일 방법을 찾아 달라 — 보조지표를 만들든 어떤 방법이든."
조건을 1~3개씩 겹치는 탐색(strategy.py)은 오른 비율 천장이 55% 안팎이었다. 여기서는 **전부 한꺼번에** 넣고(기울기 부스팅 트리),
신호마다 '2개월 뒤 오를 확률'을 매긴 뒤 **확률이 높은 쪽만 고르면** 오른 비율이 얼마나 되는지 본다.

자료    strategy.py 사건표(상폐 포함 2007~, 검색식 6개 22만 건) + 지수 상태(코스피·코스닥의 20·60일 수익, 120일선 이격, 20일 변동성 — FDR) + 그날 같은 검색식 신호 수.
표적    신호일 종가 → 40거래일 뒤 종가가 올랐는가(절대 수익 > 0). 보조로 시장초과 > 0.
검증    **연도별 순차 검증(walk-forward)**: 해마다 그 전 해까지로만 학습하고 그 해를 맞힌다(2012~2026). 한 번도 미래를 보지 않는다.
        고르는 기준(상위 몇 %)은 2007~2019 안의 검증 구간(2016~2019)에서 정하고 2020~ 는 그대로 본다.
출력    확률 상위 10·20·30·50% 만 샀을 때의 오른 비율·시장초과·건수 — 검색식별·해별. 그리고 어떤 변수가 중요했나.

    python winmodel.py > cache/winmodel_out.md        # 약 5분
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
import strategy as S  # noqa: E402

from sklearn.ensemble import HistGradientBoostingClassifier  # noqa: E402

FIRST_TEST = 2012                    # 이 해부터 해마다 맞힌다(그 전 5년은 학습만)
TOPS = (0.10, 0.20, 0.30, 0.50)
DROP = {"close", "low", "volume", "ma20", "kijun", "cmin3", "lmin3", "vavg3", "vwap"}       # 값 자체(단위 있는 것)는 뺀다 — 비율·거리만 쓴다
SHORT = {"120일 신고가(필터 없음)": "신고가", "저변동 신고가": "저변동", "승률형(저변동 신고가+상승률 상위+52주 고가권)": "승률형",
         "조합 1위(저변동 신고가+상승국면+오실레이터)": "조합1위", "상한선 첫 돌파+52주 고가권+상승국면": "상한선", "볼린저 상단+직전 고점": "볼린저"}


def market_features(dates: pd.DatetimeIndex) -> pd.DataFrame:
    """지수 상태(그날 종가까지). 코스피·코스닥 각각 20·60일 수익, 120일선 이격, 20일 변동성, 60일 고점 대비."""
    import FinanceDataReader as fdr
    out = {}
    for name, code in (("ks", "KS11"), ("kq", "KQ11")):
        c = fdr.DataReader(code, "2006-01-01").Close
        c = c[~c.index.duplicated()]
        f = pd.DataFrame({f"{name}_r20": c / c.shift(20) - 1, f"{name}_r60": c / c.shift(60) - 1,
                          f"{name}_ma120": c / c.rolling(120).mean() - 1, f"{name}_vol": c.pct_change().rolling(20).std(),
                          f"{name}_dd60": c / c.rolling(60).max() - 1})
        out[name] = f.reindex(dates, method="ffill")
    return pd.concat(out.values(), axis=1)


def build_xy():
    E, X0, _ = S.load()
    X = X0.drop(columns=[c for c in DROP if c in X0.columns]).copy()
    X["regime"] = E.regime0.to_numpy()
    for b, s in SHORT.items():
        X[f"b_{s}"] = (E.base == b).to_numpy(dtype="float32")
    cnt = E.groupby(["base", "dt"]).size().rename("n_day")                      # 그날 같은 검색식 신호 수(돌파가 몰린 날)
    X["n_day"] = E.set_index(["base", "dt"]).index.map(cnt).to_numpy(dtype="float32")
    X["n_day"] = np.log1p(X.n_day)
    mk = market_features(pd.DatetimeIndex(sorted(E.dt.unique())))
    for c in mk.columns:
        X[c] = mk[c].reindex(pd.DatetimeIndex(E.dt)).to_numpy(dtype="float32")
    y = (E.f0_40 > 0).to_numpy()
    ok = E.f0_40.notna().to_numpy() & E.m0_40.notna().to_numpy()
    return E[ok].reset_index(drop=True), X[ok].reset_index(drop=True), y[ok]


def fit(X: pd.DataFrame, y: np.ndarray) -> HistGradientBoostingClassifier:
    m = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.05, max_leaf_nodes=15, min_samples_leaf=200,
                                       l2_regularization=1.0, random_state=0)
    return m.fit(X, y)


def walk_forward(E, X, y) -> np.ndarray:
    """해마다 그 전 해까지로 학습 → 그 해 확률. 반환: 사건별 확률(학습 전 해는 NaN)."""
    years = E.dt.dt.year.to_numpy()
    p = np.full(len(E), np.nan)
    for yr in range(FIRST_TEST, years.max() + 1):
        tr, te = years < yr, years == yr
        if te.sum() == 0:
            continue
        # 표적이 40거래일 뒤라 학습 끝과 시험 시작이 겹치지 않게 학습에서 마지막 두 달을 뺀다
        cut = E.dt.to_numpy() < np.datetime64(f"{yr - 1}-11-01")
        m = fit(X[tr & cut], y[tr & cut])
        p[te] = m.predict_proba(X[te])[:, 1]
        print(f"  {yr}: 학습 {int((tr & cut).sum()):,} → 시험 {int(te.sum()):,}", file=sys.stderr, flush=True)
    return p


def table(E, p, sel, label: str, tops=TOPS, by_base: bool = True) -> None:
    e = E[sel].assign(p=p[sel])
    e = e[e.p.notna()]
    up = lambda d: 100 * float((d.f0_40 > 0).mean())
    ex = lambda d: 100 * float((d.f0_40 - d.m0_40).mean())
    print(f"\n### {label}\n")
    print("| 검색식 | 전체 오른 비율 · 초과 · 건수 | " + " | ".join(f"상위 {int(t * 100)}%" for t in tops) + " | 하위 50% |")
    print("|---|---|" + "---|" * (len(tops) + 1))
    groups = list(e.groupby("base", sort=False)) if by_base else []
    for b, g in [("**전체**", e)] + groups:
        if len(g) < 100:
            continue
        cells = []
        for t in tops:
            q = g.p.quantile(1 - t)
            s = g[g.p >= q]
            cells.append(f"{up(s):.0f}% · {ex(s):+.1f} · {len(s):,}")
        lo = g[g.p < g.p.quantile(0.5)]
        name = b if b.startswith("**") else SHORT.get(b, b)
        print(f"| {name} | {up(g):.0f}% · {ex(g):+.1f} · {len(g):,} | " + " | ".join(cells) + f" | {up(lo):.0f}% · {ex(lo):+.1f} |")


def main() -> None:
    t = time.time()
    E, X, y = build_xy()
    print(f"# 신호의 오를 확률 모델 — 조건 46개 + 시장 상태 (상장폐지 포함 {E.dt.min():%Y}~{E.dt.max():%Y-%m-%d}, 사건 {len(E):,}건)\n")
    print("표적 = 신호일 종가 → 40거래일 뒤 종가 상승 여부. 해마다 **그 전 해까지로만 학습**해 그 해를 맞혔다(2012~). "
          "칸 = 오른 비율 · 시장초과 %p · 건수. 「상위 N%」 = 그 묶음 안에서 모델 확률이 높은 N% 만 샀을 때.\n")
    p = walk_forward(E, X, y)
    dt = E.dt.to_numpy()
    table(E, p, dt <= S.IS_END, "검증 구간 2012~2019 (학습 자료와 같은 시대 — 기준을 정하는 데만 쓴다)")
    table(E, p, dt > S.IS_END, "**2020~ (한 번도 학습에 안 쓴 해들)**")
    table(E, p, dt >= np.datetime64("2025-01-01"), "2025~ (최근)")
    # 해마다 — 승률형·저변동 신고가의 상위 30% vs 전체
    print("\n### 해마다 — 상위 30% 만 샀을 때 오른 비율 (전체 → 상위 30%)\n")
    print("| 해 | " + " | ".join(SHORT[b] for b in S.BASES) + " |")
    print("|---|" + "---:|" * len(S.BASES))
    e = E.assign(p=p)
    e = e[e.p.notna()]
    for yr, g in e.groupby(e.dt.dt.year):
        cells = []
        for b in S.BASES:
            h = g[g.base == b]
            if len(h) < 30:
                cells.append("—")
                continue
            s = h[h.p >= h.p.quantile(0.7)]
            cells.append(f"{100 * (h.f0_40 > 0).mean():.0f} → {100 * (s.f0_40 > 0).mean():.0f}%")
        print(f"| {yr} | " + " | ".join(cells) + " |")
    # 변수 중요도 — 2019 까지로 한 번 학습해 순열 중요도
    from sklearn.inspection import permutation_importance
    tr = (dt <= np.datetime64("2019-10-31"))
    m = fit(X[tr], y[tr])
    te = (dt > S.IS_END)
    sub = np.flatnonzero(te)
    rng = np.random.default_rng(0)
    sub = rng.choice(sub, size=min(30000, len(sub)), replace=False)
    imp = permutation_importance(m, X.iloc[sub], y[sub], n_repeats=3, random_state=0, scoring="roc_auc")
    order = np.argsort(-imp.importances_mean)[:20]
    print("\n### 어떤 변수가 중요했나 (2020~ 에서 섞었을 때 AUC 가 얼마나 떨어지나 · 상위 20)\n")
    print("| 변수 | 중요도 |")
    print("|---|---:|")
    for i in order:
        print(f"| {X.columns[i]} | {imp.importances_mean[i]:.4f} |")
    from sklearn.metrics import roc_auc_score
    print(f"\n- 2020~ AUC(모델이 오를 것과 안 오를 것을 구별하는 힘, 0.5 = 동전 던지기): {roc_auc_score(y[te], m.predict_proba(X[te])[:, 1]):.3f}")
    pd.DataFrame({"Code": E.Code, "dt": E.dt, "base": E.base, "p": p, "y": y}).to_pickle(core.CACHE / "winmodel_pred.pkl")
    print(f"\n<!-- {time.time() - t:.0f}초 -->")


if __name__ == "__main__":
    main()
