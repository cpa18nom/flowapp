"""보조지표 관계 → 승률 좋은 매수 시점 전수 탐색 (사용자 요청 2026-10-07).

사용자 말: 차트의 보조지표(볼린저 220 상한 · 일목 기준선 26 · 엔벨로프 60 상단 · VWAP 120 · MA20/60/120 · 스토캐스틱 오실레이터)끼리의 돌파,
캔들의 위치, 선끼리의 위치, 오실레이터를 총망라해 모든 시나리오를 세우고, 신호에 샀으면 얼마가 언제 났는지 추적. 60분봉 1년 · 일봉 2년.

방법
  조건(atomic) 약 36개 → 단일 · 2개 조합 · 3개 조합 전부(지지 건수가 모자란 가지는 친다).
  측정 = 신호 봉 종가에 사서 h봉 뒤 종가(수익률) · 시장초과(같은 봉 시각의 유동 유니버스 평균 수익률 차감) · 오른 비율.
  '언제' = 지평별 평균 곡선의 최고점 + 신호마다 (저장한 지평 중) 최고 수익이 난 지평의 분포.
  과최적화 방지 = 기간을 앞 절반(IS)·뒤 절반(OOS)으로 나눠 **양쪽 다** 기준보다 나은 것만 올린다. 그래도 2년·1년은 짧다 — 결과는 '후보'다.
  유동성 = 하루 거래대금 20일 평균 ≥ 10억(화면 기본값). 상장폐지 종목은 없다(최근 2년 생존 종목 — 생존편향 약간).

    python indsetup.py d  > cache/indsetup_d.md     # 일봉 2년(앱 시세, 생존 종목)
    python indsetup.py h  > cache/indsetup_h.md     # 60분봉 1년
    python indsetup.py dfull > cache/indsetup_dfull.md   # ★ 일봉 전 기간 2007~(marcap, 상장폐지 포함, IS 2007~2019 · OOS 2020~) — 사용자 요청 "전기간"
"""
from __future__ import annotations

import itertools
import sys
import time
import warnings
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

MIN_VAL = 10e8
H = {"d": [1, 2, 3, 5, 7, 10, 15, 20, 30, 40], "h": [1, 3, 7, 14, 21, 35, 70, 105, 140]}      # 60분봉 7개 = 하루
H["dfull"] = H["d"]
HL = {"d": {1: "1일", 2: "2일", 3: "3일", 5: "1주", 7: "7일", 10: "2주", 15: "3주", 20: "1달", 30: "6주", 40: "2달"},
      "h": {1: "1시간", 3: "3시간", 7: "1일", 14: "2일", 21: "3일", 35: "1주", 70: "2주", 105: "3주", 140: "1달"}}
RANK_H = {"d": (5, 10, 20), "h": (21, 35, 70), "dfull": (5, 10, 20)}       # 순위를 매기는 지평(중간 보유)
YEARS = {"d": 2, "h": 1}
MIN_N = {"d": 150, "h": 300, "dfull": 500}           # 조합이 넘어야 하는 건수(IS·OOS 각각)
BARS_PER_DAY = {"d": 1, "h": 7, "dfull": 1}
HL["dfull"] = HL["d"]
IS_END_FULL = np.datetime64("2019-12-31")
TOP = 25


# ────────────────────────────── 조건

def features(o: pd.DataFrame, tf: str) -> pd.DataFrame | None:
    c, h, l, op, v = o.close, o.high, o.low, o.open, o.volume
    n = len(o)
    if n < 300:
        return None
    ma = {k: c.rolling(k).mean() for k in (20, 60, 120)}
    bb = c.rolling(220).mean() + c.rolling(220).std(ddof=0)
    kij = (h.rolling(26).max() + l.rolling(26).min()) / 2
    env = ma[60] * 1.05
    vw = (c * v).rolling(120).sum() / v.rolling(120).sum().replace(0, np.nan)
    st = core.stoch_lines(h, l, c)
    osc, fd, sd = st.osc, st.fastD, st.slowD
    bpd = BARS_PER_DAY[tf]
    val = (c * v).rolling(20 * bpd).mean() * bpd                   # 하루 거래대금의 20일 평균
    vr = v / v.rolling(20).mean().shift(1)
    clv = (c - l) / (h - l).replace(0, np.nan)
    pc = c.shift(1)
    X = lambda a, b: (a > b) & (a.shift(1) <= b.shift(1))         # a 가 b 를 위로 돌파  # noqa: E731
    F = pd.DataFrame(index=o.index)
    # 캔들 위치(종가 vs 선)
    F["종가>MA20"], F["종가>MA60"], F["종가>MA120"] = c > ma[20], c > ma[60], c > ma[120]
    F["종가>기준선"], F["종가>VWAP"], F["종가>엔벨상단"], F["종가>볼린저상한"] = c > kij, c > vw, c > env, c > bb
    F["종가<기준선"], F["종가<VWAP"], F["종가<MA20"] = c < kij, c < vw, c < ma[20]
    # 종가가 선을 위로 돌파(이 봉)
    F["돌파↑MA20"], F["돌파↑MA60"], F["돌파↑MA120"] = X(c, ma[20]), X(c, ma[60]), X(c, ma[120])
    F["돌파↑기준선"], F["돌파↑VWAP"], F["돌파↑엔벨상단"], F["돌파↑볼린저상한"] = X(c, kij), X(c, vw), X(c, env), X(c, bb)
    # 선끼리 위치 · 선끼리 돌파
    F["VWAP>기준선"], F["기준선>MA60"], F["MA20>MA60"], F["MA60>MA120"] = vw > kij, kij > ma[60], ma[20] > ma[60], ma[60] > ma[120]
    F["엔벨상단>볼린저상한"], F["VWAP>MA60"] = env > bb, vw > ma[60]
    F["VWAP돌파↑기준선"], F["기준선돌파↑VWAP"], F["MA20돌파↑MA60"] = X(vw, kij), X(kij, vw), X(ma[20], ma[60])
    # 지지 반등(저가는 선 아래, 종가는 위) · 봉 전체가 선 위
    F["기준선지지"], F["VWAP지지"], F["MA20지지"] = (l <= kij) & (c > kij), (l <= vw) & (c > vw), (l <= ma[20]) & (c > ma[20])
    F["저가>기준선"], F["저가>VWAP"] = l > kij, l > vw
    F["양봉"], F["종가고가근처"] = c > op, clv >= 0.7
    # 스토캐스틱
    F["osc양봉"], F["osc전환↑"] = osc > 0, (osc > 0) & (osc.shift(1) <= 0)
    F["osc음봉축소"], F["osc커짐"] = (osc < 0) & (osc > osc.shift(1)), osc > osc.shift(1)
    F["fastD<20"], F["fastD>80"] = fd < 20, fd > 80
    # 거래량
    F["거래량1.5배↑"], F["거래량말라"] = vr >= 1.5, vr < 0.7
    F = F.astype(bool)
    for k in H[tf]:
        F[f"f{k}"] = (c.shift(-k) / c - 1).astype("float32")
    F["liq"] = (val >= MIN_VAL).fillna(False)
    F["ok"] = bb.notna() & vw.notna() & osc.notna()
    return F


def _one(args):
    code, tf = args
    o = core.read_bars(code, tf)
    if o is None:
        return None
    F = features(o, tf)
    if F is None:
        return None
    if tf == "d":                                                  # 오늘 기준 2년(종목의 마지막 봉이 아니라 — 거래정지 종목이 창을 앞으로 늘리지 않게)
        F = F[F.index >= pd.Timestamp.today().normalize() - pd.Timedelta(days=365 * YEARS["d"])]
    F = F[F.ok & F.liq].drop(columns=["ok", "liq"])
    if F.empty:
        return None
    F = F.reset_index(names="dt")
    F["Code"] = code
    return F


def build_full() -> pd.DataFrame:
    """일봉 전 기간: marcap(KRX 일별, 상장폐지 포함) → bottom.adjust 의 수정 OHLC. 거래량 = Amount ÷ 수정종가(combo.py 와 같다).
    상폐 종목은 h 봉 전에 끝나면 마지막 거래가로 청산(fwd 의 NaN 을 마지막 종가 수익률로 채운다)."""
    import bottom
    t = time.time()
    d = bottom.adjust(bottom.load("2006-01-01"))
    last_day = d.Date.max()
    print(f"  marcap {len(d):,}행 ({time.time() - t:.0f}초)", file=sys.stderr, flush=True)
    parts = []
    for i, (code, g) in enumerate(d.groupby("Code", sort=False), 1):
        if len(g) < 300:
            continue
        o = pd.DataFrame({"open": g.aO.to_numpy(), "high": g.aH.to_numpy(), "low": g.aL.to_numpy(),
                          "close": g.aC.to_numpy(), "volume": (g.Amount / g.aC).to_numpy()}, index=pd.DatetimeIndex(g.Date))
        F = features(o, "d")
        if F is None:
            continue
        if o.index[-1] < last_day - pd.Timedelta(days=10):          # 상장폐지: 마지막 거래가로 청산
            lastret = (float(o.close.iloc[-1]) / o.close - 1).astype("float32")
            for k in H["d"]:
                F[f"f{k}"] = F[f"f{k}"].fillna(lastret)
        F = F[F.ok & F.liq & (F.index >= "2007-01-01")].drop(columns=["ok", "liq"])
        if F.empty:
            continue
        F = F.reset_index(names="dt")
        F["Code"] = code
        parts.append(F)
        if i % 500 == 0:
            print(f"  {i} 종목 ({time.time() - t:.0f}초)", file=sys.stderr, flush=True)
    E = pd.concat(parts, ignore_index=True)
    for k in H["d"]:
        E[f"e{k}"] = (E[f"f{k}"] - E.groupby("dt")[f"f{k}"].transform("mean")).astype("float32")
    E["Code"] = E.Code.astype("category")
    print(f"  일봉 전 기간 행 {len(E):,} · 종목 {E.Code.nunique():,} · {E.dt.min():%Y-%m-%d} ~ {E.dt.max():%Y-%m-%d} ({time.time() - t:.0f}초)", file=sys.stderr, flush=True)
    return E


def build(tf: str) -> pd.DataFrame:
    if tf == "dfull":
        return build_full()
    t = time.time()
    codes = sorted(p.name[:6] for p in core.PRICE.glob("*.csv.gz"))
    parts = []
    with ProcessPoolExecutor(8) as ex:
        for i, F in enumerate(ex.map(_one, [(c, tf) for c in codes], chunksize=16), 1):
            if F is not None:
                parts.append(F)
            if i % 500 == 0:
                print(f"  {i}/{len(codes)} ({time.time() - t:.0f}초)", file=sys.stderr, flush=True)
    E = pd.concat(parts, ignore_index=True)
    # 시장초과: 같은 봉 시각의 유동 유니버스 평균 수익률 차감
    for k in H[tf]:
        E[f"e{k}"] = (E[f"f{k}"] - E.groupby("dt")[f"f{k}"].transform("mean")).astype("float32")
    E["Code"] = E.Code.astype("category")
    print(f"  {core.TFS[tf]} 행 {len(E):,} · 종목 {E.Code.nunique():,} · {E.dt.min():%Y-%m-%d} ~ {E.dt.max():%Y-%m-%d} ({time.time() - t:.0f}초)", file=sys.stderr, flush=True)
    return E


# ────────────────────────────── 측정

def stats(mask: np.ndarray, FW: np.ndarray, EX: np.ndarray) -> dict | None:
    k = int(mask.sum())
    if k == 0:
        return None
    f, e = FW[mask], EX[mask]
    ok = np.isfinite(f)
    n = ok.sum(0)
    with np.errstate(invalid="ignore", divide="ignore"):
        win = np.where(n > 0, (np.where(ok, f > 0, False)).sum(0) / np.maximum(n, 1), np.nan)
        ex = np.where(n > 0, np.nansum(np.where(ok, e, 0), 0) / np.maximum(n, 1), np.nan)
        ret = np.where(n > 0, np.nansum(np.where(ok, f, 0), 0) / np.maximum(n, 1), np.nan)
    return {"n": k, "win": win, "ex": ex, "ret": ret}


def peak_when(mask: np.ndarray, FW: np.ndarray, hz: list[int]) -> tuple[int, dict]:
    """평균 곡선의 최고 지평 · 신호마다 최고 수익 지평의 분포(비율)."""
    f = FW[mask]
    f = f[np.isfinite(f).all(1)]
    if len(f) == 0:
        return hz[0], {}
    curve = f.mean(0)
    am = f.argmax(1)
    dist = {hz[i]: round(100 * float((am == i).mean())) for i in range(len(hz))}
    return hz[int(curve.argmax())], dist


def main() -> None:
    tf = sys.argv[1] if len(sys.argv) > 1 else "d"
    hz = H[tf]
    cache = core.CACHE / f"indsetup_{tf}.pkl"
    if cache.exists() and "--build" not in sys.argv:
        E = pd.read_pickle(cache)
    else:
        E = build(tf)
        E.to_pickle(cache)
    conds = [c for c in E.columns if c not in ("dt", "Code") and not (c[0] in "fe" and c[1:].isdigit())]
    C = {c: E[c].to_numpy(dtype=bool) for c in conds}
    FW = np.column_stack([E[f"f{k}"].to_numpy() for k in hz])
    EX = np.column_stack([E[f"e{k}"].to_numpy() for k in hz])
    dts = E.dt.to_numpy()
    mid = IS_END_FULL if tf == "dfull" else dts.min() + (dts.max() - dts.min()) / 2
    is_, oos = dts <= mid, dts > mid
    rank_i = [hz.index(k) for k in RANK_H[tf]]
    base_is, base_oos = stats(is_, FW, EX), stats(oos, FW, EX)
    base_all = stats(np.ones(len(E), dtype=bool), FW, EX)
    lab = HL[tf]

    def row_html(name, m):
        s_all, s_is, s_oos = stats(m, FW, EX), stats(m & is_, FW, EX), stats(m & oos, FW, EX)
        if not s_all:
            return None
        pk, dist = peak_when(m, FW, hz)
        return {"name": name, "n": s_all["n"], "n_is": s_is["n"] if s_is else 0, "n_oos": s_oos["n"] if s_oos else 0,
                "win": s_all["win"], "ex": s_all["ex"], "ret": s_all["ret"],
                "win_is": s_is["win"] if s_is else None, "win_oos": s_oos["win"] if s_oos else None,
                "ex_is": s_is["ex"] if s_is else None, "ex_oos": s_oos["ex"] if s_oos else None, "peak": pk, "dist": dist}

    def score(r):
        """순위 지평들의 (IS·OOS 각각의) 오른 비율 — 기준보다 얼마나 나은지의 최소값. 양쪽 다 나아야 양수."""
        if r is None or r["n_is"] < MIN_N[tf] or r["n_oos"] < MIN_N[tf]:
            return -9
        d_is = np.nanmean([r["win_is"][i] - base_is["win"][i] for i in rank_i])
        d_oos = np.nanmean([r["win_oos"][i] - base_oos["win"][i] for i in rank_i])
        return float(min(d_is, d_oos))

    title = {"d": "일봉 (2년, 앱 시세·생존 종목)", "h": "60분봉 (1년)", "dfull": "일봉 전 기간 (2007~, marcap·상장폐지 포함)"}[tf]
    print(f"# 보조지표 관계 → 매수 시점 전수 탐색 — {title} (2026-10-07)\n")
    print(f"행 {len(E):,}(유동·지표 유효) · 종목 {E.Code.nunique():,} · {pd.Timestamp(dts.min()):%Y-%m-%d} ~ {pd.Timestamp(dts.max()):%Y-%m-%d} · IS = {pd.Timestamp(mid):%Y-%m-%d} 까지 · OOS = 그 뒤")
    print(f"조건 {len(conds)}개 → 단일 {len(conds)} · 2개 조합 {len(conds) * (len(conds) - 1) // 2:,} · 3개 조합(지지 건수 넘는 것만). 순위 = {'·'.join(lab[k] for k in RANK_H[tf])} 오른 비율이 기준(전 행)보다 IS·OOS **둘 다** 얼마나 높은가(최소값).")
    print("칸 = 오른 비율(절대수익 > 0) · 시장초과 = 같은 봉 시각 유동 유니버스 평균 대비 %p · '최고 지평' = 평균 수익 곡선이 가장 높은 지평 · '언제' = 신호마다 최고 수익이 난 지평의 분포(%)\n")
    print("## 기준(전 행)\n")
    print("| | 건수 | " + " | ".join(lab[k] for k in hz) + " |\n|---|---:|" + "---|" * len(hz))
    for nm, s in (("전 행 오른 비율", base_all), ("IS 오른 비율", base_is), ("OOS 오른 비율", base_oos)):
        print(f"| {nm} | {s['n']:,} | " + " | ".join(f"{100 * w:.0f}%" for w in s["win"]) + " |")
    print(f"| 전 행 평균 수익 | | " + " | ".join(f"{100 * r:+.2f}%" for r in base_all["ret"]) + " |")

    def table(rows, title):
        rows = [r for r in rows if r]
        rows.sort(key=score, reverse=True)
        rows = [r for r in rows if score(r) > 0][:TOP]
        print(f"\n## {title} — 상위 {len(rows)}\n")
        if not rows:
            print("(IS·OOS 둘 다 기준보다 나은 것이 없다)")
            return rows
        print("| 시나리오 | 건수(IS/OOS) | 점수 | " + " | ".join(lab[k] for k in hz) + " | 최고 지평 | 언제(최고 수익 지평 분포) |")
        print("|---|---:|---:|" + "---|" * len(hz) + "---|---|")
        for r in rows:
            cells = [f"{100 * r['win'][i]:.0f}% ({100 * r['ex'][i]:+.1f})" for i in range(len(hz))]
            when = " ".join(f"{lab[k]}:{v}" for k, v in r["dist"].items() if v >= 10)
            print(f"| {r['name']} | {r['n']:,} ({r['n_is']:,}/{r['n_oos']:,}) | +{100 * score(r):.1f}p | " + " | ".join(cells) + f" | {lab[r['peak']]} | {when} |")
        return rows

    t = time.time()
    singles = [row_html(c, C[c]) for c in conds]
    print("\n## 단일 조건 전부 (점수순, 기준 미달 포함)\n")
    print("| 조건 | 건수 | 점수 | " + " | ".join(lab[k] for k in hz) + " |\n|---|---:|---:|" + "---|" * len(hz))
    for r in sorted([r for r in singles if r], key=score, reverse=True):
        print(f"| {r['name']} | {r['n']:,} | {100 * score(r):+.1f}p | " + " | ".join(f"{100 * r['win'][i]:.0f}% ({100 * r['ex'][i]:+.1f})" for i in range(len(hz))) + " |")
    pairs = []
    pair_masks = {}
    for a, b in itertools.combinations(conds, 2):
        m = C[a] & C[b]
        if (m & is_).sum() < MIN_N[tf] or (m & oos).sum() < MIN_N[tf]:
            continue
        pair_masks[(a, b)] = m
        pairs.append(row_html(f"{a} + {b}", m))
    top_pairs = table(pairs, "2개 조합")
    print(f"\n(2개 조합 {len(pairs):,}개 평가 · {time.time() - t:.0f}초)", file=sys.stderr)
    triples = []
    seen = set()
    for (a, b), m2 in pair_masks.items():
        for c_ in conds:
            if c_ in (a, b):
                continue
            key = tuple(sorted((a, b, c_)))
            if key in seen:
                continue
            seen.add(key)
            m = m2 & C[c_]
            if (m & is_).sum() < MIN_N[tf] or (m & oos).sum() < MIN_N[tf]:
                continue
            triples.append(row_html(" + ".join(key), m))
    top_tri = table(triples, "3개 조합")
    print(f"\n(3개 조합 {len(triples):,}개 평가 · {time.time() - t:.0f}초)", file=sys.stderr)
    # 전체 결과 저장
    allrows = [r for r in singles + pairs + triples if r]
    out = pd.DataFrame([{"name": r["name"], "n": r["n"], "n_is": r["n_is"], "n_oos": r["n_oos"], "score": score(r), "peak": r["peak"],
                         **{f"win{k}": r["win"][i] for i, k in enumerate(hz)}, **{f"ex{k}": r["ex"][i] for i, k in enumerate(hz)},
                         **{f"win_is{k}": (r["win_is"][i] if r["win_is"] is not None else np.nan) for i, k in enumerate(hz)},
                         **{f"win_oos{k}": (r["win_oos"][i] if r["win_oos"] is not None else np.nan) for i, k in enumerate(hz)}} for r in allrows])
    out.sort_values("score", ascending=False).to_csv(core.CACHE / f"indsetup_{tf}_all.csv", index=False, encoding="utf-8-sig")
    # 최근 신호(상위 시나리오)
    best = (top_tri or top_pairs)[:5]
    if best:
        names = dict(zip(core.universe().Code, core.universe().Name))
        last = pd.Timestamp(dts.max())
        recent_from = last - pd.Timedelta(days=5 if tf != "h" else 2)
        print(f"\n## 최근 신호 — 상위 시나리오 5개 ({recent_from:%m-%d} 이후)\n")
        for r in best:
            parts = r["name"].split(" + ")
            m = np.logical_and.reduce([C[p] for p in parts]) & (dts >= np.datetime64(recent_from))
            hits = E.loc[m, ["dt", "Code"]].sort_values("dt", ascending=False).head(12)
            txt = ", ".join(f"{names.get(str(c), c)}({pd.Timestamp(d):%m-%d %H:%M})" if tf == "h" else f"{names.get(str(c), c)}({pd.Timestamp(d):%m-%d})" for d, c in zip(hits.dt, hits.Code))
            print(f"- **{r['name']}** ({int(m.sum())}건): {txt or '없음'}")


if __name__ == "__main__":
    main()
