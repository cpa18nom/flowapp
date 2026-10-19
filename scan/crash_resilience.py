"""폭락 주간에 버틴 종목의 사전 특징 + 그 대가 (사용자 질문 2026-10-15 "폭락장에서도 살아남는 종목의 공통점은? 미리 알 수 있나?").

자료: raw/price(**현재 상장 종목만 — 생존편향**) · 하루 평균 거래대금(20일) 10억↑ · 유동 종목 300개↑ 날만(2014-07~).
시장 = 유동 종목 일수익률 **중앙값**(폭락 주간 판정·순위상관용). 40일 대가 비교는 **평균**을 쓴다 —
중앙값 누적은 아래로 치우쳐 전 분위가 시장 위로 보인다(실측 +11%).
폭락 주간 = 시장 5일 수익률 하위 3%, 사건끼리 20거래일 이상 떨어뜨림. 특징은 전부 폭락 직전 날 값(미리 알 수 있는 값).

    ~/.venvs/npl/bin/python scan/crash_resilience.py [이번주시작 이번주끝]   # 기본 2026-10-08 2026-10-14
"""
import glob, os, numpy as np, pandas as pd
from scipy.stats import spearmanr

import sys
os.chdir(os.path.expanduser("~/market"))
W0, W1 = (sys.argv[1:3] if len(sys.argv) > 2 else ("2026-10-08", "2026-10-14"))
cl, vo = {}, {}
for f in glob.glob("raw/price/*.csv.gz"):
    d = pd.read_csv(f, header=None, names="dt o h l c v".split(), usecols=[0, 4, 5])
    d = d[d.dt.str.match(r"\d{4}-") & (d.dt >= "2006-01-01")]
    d[["c", "v"]] = d[["c", "v"]].apply(pd.to_numeric, errors="coerce")
    if len(d) < 300: continue
    k = os.path.basename(f)[:6]
    cl[k] = d.set_index("dt").c; vo[k] = d.set_index("dt").v
C = pd.DataFrame(cl).sort_index(); V = pd.DataFrame(vo).reindex(C.index)
C.index = pd.to_datetime(C.index); V.index = C.index
C = C.where(C > 0)
R = C.pct_change(fill_method=None).clip(-0.31, 0.31)
val = (C * V).rolling(20).mean()
liq = val >= 1e9                                        # 하루 평균 거래대금 10억↑만
Rl = R.where(liq.shift(1, fill_value=False))
mkt = Rl.median(axis=1).where(Rl.notna().sum(axis=1) >= 300)
print("유동 종목 300개↑ 첫날", mkt.first_valid_index())

H = 5
fwd = C.shift(-H) / C - 1
mfwd = (1 + mkt).rolling(H).apply(np.prod, raw=True).shift(-H) - 1

feat = {
    "베타(60일)": (R.mul(mkt, axis=0).rolling(60).mean() - R.rolling(60).mean().mul(mkt.rolling(60).mean(), axis=0)).div(mkt.rolling(60).var(ddof=0), axis=0),
    "변동성(20일)": R.rolling(20).std(),
    "수익률 20일": C / C.shift(20) - 1,
    "수익률 60일": C / C.shift(60) - 1,
    "수익률 250일": C / C.shift(250) - 1,
    "52주 고가 대비": C / C.rolling(250).max(),
    "20일선 이격": C / C.rolling(20).mean() - 1,
    "거래대금(규모)": np.log(val),
    "거래량 급증(20/120)": V.rolling(20).mean() / V.rolling(120).mean(),
    "하락일 방어(60일)": R.where(pd.DataFrame(np.repeat(mkt.lt(0).values[:, None], R.shape[1], 1), index=R.index, columns=R.columns)).rolling(60, min_periods=10).mean().sub(
        mkt.where(mkt < 0).rolling(60, min_periods=10).mean(), axis=0),
}

# 폭락 주간: 시장 5일 수익률 하위 3%, 서로 20거래일 이상 떨어진 사건
cand = mfwd.dropna().sort_values()
cand = cand[cand.index >= mkt.first_valid_index() + pd.Timedelta(days=400)]
thr = cand.quantile(0.03)
events = []
for d, v in cand.items():
    if v > thr: break
    if all(abs(C.index.get_loc(d) - C.index.get_loc(e)) >= 20 for e in events): events.append(d)
events.sort()
rows = []
for d in events:
    ok = liq.loc[d] & fwd.loc[d].notna()
    y = fwd.loc[d][ok]
    r = {"day": d.date(), "mkt5": mfwd.loc[d], "n": int(ok.sum())}
    for k, F in feat.items():
        x = F.loc[d][ok]; m = x.notna()
        r[k] = spearmanr(x[m], y[m]).statistic if m.sum() > 50 else np.nan
    rows.append(r)
E = pd.DataFrame(rows)
print(f"폭락 주간 {len(E)}건 (시장 5일 ≤ {thr:.1%})")
print(E[["day", "mkt5", "n"]].assign(mkt5=lambda x: x.mkt5.map("{:.1%}".format)).to_string(index=False))
s = E[list(feat)].agg(["mean", lambda x: (x > 0).mean()]).T
s.columns = ["순위상관 평균", "양(+)인 비율"]
s["~2020"] = E[E.day < pd.Timestamp("2021-01-01").date()][list(feat)].mean()
s["2021~"] = E[E.day >= pd.Timestamp("2021-01-01").date()][list(feat)].mean()
print(s.sort_values("순위상관 평균").round(3).to_string())

# 5분위: 가장 잘 갈린 특징 — 분위별 폭락 주간 평균 수익(시장 대비)
for k in ["베타(60일)", "변동성(20일)", "하락일 방어(60일)", "거래대금(규모)", "수익률 20일"]:
    q = []
    for d in events:
        ok = liq.loc[d] & fwd.loc[d].notna() & feat[k].loc[d].notna()
        x, y = feat[k].loc[d][ok], fwd.loc[d][ok] - mfwd.loc[d]
        q.append(y.groupby(pd.qcut(x.rank(method="first"), 5, labels=False)).mean())
    q = pd.concat(q, axis=1)
    print(k, "분위1(낮음)→5(높음) 시장 대비:", " ".join(f"{v:+.1%}" for v in q.mean(axis=1)),
          "| 1분위>5분위 비율", f"{(q.loc[0] > q.loc[4]).mean():.0%}")

# 이번 주(10/8 종가 → 10/14 종가)
d0, d1 = pd.Timestamp(W0), pd.Timestamp(W1)
ok = liq.loc[d0] & C.loc[d1].notna()
wk = (C.loc[d1] / C.loc[d0] - 1)[ok]
mk = (1 + mkt.loc[d0:d1].iloc[1:]).prod() - 1
print(f"\n{W0}→{W1} 시장(중앙값 누적) {mk:+.1%} · 종목 중앙값 {wk.median():+.1%} · 오른 종목 {(wk > 0).mean():.0%} · n={ok.sum()}")
for k in feat:
    x = feat[k].loc[d0][ok]; m = x.notna()
    print(f"  {k}: 순위상관 {spearmanr(x[m], wk[m]).statistic:+.3f}")
for c, n in [("012030", "DB"), ("396300", "HT로보틱스"), ("096530", "씨젠"), ("033780", "KT&G")]:
    print(n, f"이번주 {C.loc[d1, c] / C.loc[d0, c] - 1:+.1%}", " ".join(f"{k}={feat[k].loc[d0, c]:.2f}" for k in ["베타(60일)", "변동성(20일)", "하락일 방어(60일)"]))

# 대가: 평소(40거래일 보유) 저베타·하락일 방어 분위별 성적, 시장 = 유동 종목 평균
H2 = 40
f40 = C.shift(-H2) / C - 1
mmean = Rl.mean(axis=1).where(mkt.notna())
m40 = (1 + mmean).rolling(H2).apply(np.prod, raw=True).shift(-H2) - 1
days = [d for d in C.index[mkt.notna()][300::20] if pd.notna(m40.loc[d])]
for k in ["베타(60일)", "하락일 방어(60일)"]:
    out = {"상승장": [], "하락장": []}
    for d in days:
        ok = liq.loc[d] & f40.loc[d].notna() & feat[k].loc[d].notna()
        if ok.sum() < 200: continue
        x, y = feat[k].loc[d][ok], f40.loc[d][ok] - m40.loc[d]
        q = y.groupby(pd.qcut(x.rank(method="first"), 5, labels=False)).mean()
        out["상승장" if m40.loc[d] > 0 else "하락장"].append(q)
    for reg, qs in out.items():
        q = pd.concat(qs, axis=1).mean(axis=1)
        print(f"{k} {reg}({len(qs)}구간) 40일 시장대비 분위1→5:", " ".join(f"{v:+.1%}" for v in q))
    allq = pd.concat(out["상승장"] + out["하락장"], axis=1).mean(axis=1)
    print(f"{k} 전체 40일:", " ".join(f"{v:+.1%}" for v in allq))
