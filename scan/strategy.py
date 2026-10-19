"""검색식에 걸린 뒤 **어떤 조건이 붙었을 때** 올랐나 → 조건 조합 → 매매 계획(진입·보유·청산) (사용자 요청 2026-10-06).

사용자 생각: 일봉·주봉 검색식에 걸린 종목은 오르기 전에 거래량·보조지표(오실레이터·볼린저밴드·일목균형표·엔벨롭)에 특징이 있을 것이다.
그것을 조합해 우리 전략을 만들고, "무엇을 언제 사서 언제까지 든다"까지 정한다.

자료    combo 패널(marcap, 상장폐지 포함, 2007~) — 신호·시장초과는 combo 것 그대로. 지표는 종목마다 수정 OHLCV 로 새로 낸다(feats).
결정일  D0 = 신호일 종가에 산다 · D3 = 신호 뒤 3거래일째 종가에 산다(그 사이 상승분은 수익에 넣지 않는다).
조건    전부 **그날 장 마감에 알 수 있는 값**(CONDS). 문턱값은 미리 정해 둔다 — 자료를 보고 문턱을 옮기지 않는다.
고르기  **2007~2019(IS)만 보고** 조건 1~3개 조합을 고른다(건수·t 하한을 넘는 것 중 2개월 뒤 오른 비율이 높은 순).
        2020~(OOS)는 고른 뒤에 한 번 본다. IS 상위 10개가 OOS 에서 얼마나 남는지(줄어드는 폭)를 같이 적는다 — 그것이 과최적화의 크기다.
청산    고른 진입 규칙에 청산 규칙(기한·이평선 이탈·기준선 이탈·손절·목표가)을 얹어 건별 수익을 본다. 청산도 IS 로 고른다.

    python strategy.py --build        # 사건표 만들기(약 6분, 최대 6GB) → cache/strategy_events.pkl · strategy_paths.npz
    python strategy.py > cache/strategy_out.md
"""
from __future__ import annotations

import itertools
import sys
import time
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

BASES = {                    # 화면 검색식과 같은 뜻의 combo 조합(신호 열, 필터) → 화면 검색식 kind
    "120일 신고가(필터 없음)": ("nh120", (), None),
    "저변동 신고가": ("nh120", ("f_lowvol",), "lowvol_breakout"),
    "승률형(저변동 신고가+상승률 상위+52주 고가권)": ("nh120", ("f_lowvol", "f_mom", "f_near52"), "win_combo"),
    "조합 1위(저변동 신고가+상승국면+오실레이터)": ("nh120", ("f_lowvol", "f_regime", "f_osc"), "best_combo"),
    "상한선 첫 돌파+52주 고가권+상승국면": ("bb40_break", ("f_near52", "f_regime"), "bb40_combo"),
    "볼린저 상단+직전 고점": ("bb_break", (), "bb_break"),
}
HZ = {20: "4주", 40: "2개월", 120: "반년"}
IS_END = np.datetime64("2019-12-31")
IS_MID = np.datetime64("2013-06-30")     # IS 를 앞뒤로 나누는 날 — 조합은 **양쪽 절반 모두에서** 기준보다 나아야 한다(한 시기에만 통한 조합 걸러내기).
#   ★ 이 요건은 첫 탐색의 OOS 를 본 뒤에 넣었다(2026-10-06): IS 1등 가운데 「시장 하락국면」이 든 조합들이 OOS 에서 무너졌다(건수 100~300, t<1).
#     요건 자체는 IS 자료만 쓰지만, 넣기로 한 판단에 OOS 가 섞였다 — 그래서 아래 OOS 수치는 완전히 깨끗한 사후 확인이 아니다. 진짜 확인은 앞으로의 실전이다.
PATH = 124                   # 신호일부터 저장하는 경로 길이(D3 진입 + 120일)
EVENTS, PATHS = core.CACHE / "strategy_events.pkl", core.CACHE / "strategy_paths.npz"
MIN_N, MIN_SHARE, MIN_T = 300, 0.08, 2.0     # IS 에서 조합이 넘어야 하는 건수·비율·t
TOP_SINGLES, TOP_SHOW = 14, 10


# ────────────────────────────── 지표 (한 종목 · 그날 종가까지의 값만)

def feats(o: pd.DataFrame) -> pd.DataFrame:
    """o: index=날짜, open·high·low·close·volume(수정). 반환: 같은 index 의 지표(전부 그날 장 마감에 알 수 있는 값)."""
    c, h, l, op, v = o.close, o.high, o.low, o.open, o.volume
    f = pd.DataFrame(index=o.index)
    ma = {n: c.rolling(n).mean() for n in (20, 60, 120)}
    sd = c.rolling(20).std()
    v20 = v.rolling(20).mean()
    # 거래량
    f["vr"] = v / v20.shift(1)                              # 그날 거래량 ÷ 전날까지 20일 평균
    f["v5_20"] = v.rolling(5).mean() / v20
    f["v20_60"] = v20 / v.rolling(60).mean()
    # 오실레이터(스토캐스틱 12·26·9 — 화면과 같은 식) · 주봉·월봉은 그날까지의 봉으로
    osc = core.stoch_lines(h, l, c).osc
    f["osc"], f["osc_d"] = osc, osc - osc.shift(1)
    w, m = core.higher_osc(o, "W-FRI"), core.higher_osc(o, "M")
    f["wosc"], f["wosc_d"] = w.osc, w.osc - w.prev
    f["mosc"], f["mosc_d"] = m.osc, m.osc - m.prev
    # 볼린저밴드(20, 2)
    f["pb"] = (c - (ma[20] - 2 * sd)) / (4 * sd).replace(0, np.nan)
    bw = 4 * sd / ma[20]
    f["bwr"] = bw / bw.rolling(120).median()
    # 일목균형표(9, 26, 52)
    ten = (h.rolling(9).max() + l.rolling(9).min()) / 2
    kij = (h.rolling(26).max() + l.rolling(26).min()) / 2
    sa_raw, sb_raw = (ten + kij) / 2, (h.rolling(52).max() + l.rolling(52).min()) / 2
    sa, sb = sa_raw.shift(26), sb_raw.shift(26)
    f["cloud"] = c / np.maximum(sa, sb) - 1               # 구름 위쪽 대비
    f["tk"] = ten / kij - 1
    f["chikou"] = c / c.shift(26) - 1                     # 후행스팬이 26일 전 종가 위인가
    f["kij"] = c / kij - 1
    f["cloud_fut"] = sa_raw / sb_raw - 1                  # 앞으로 그려질 구름(양운·음운)
    # 엔벨롭 = 이동평균 이격
    f["g20"], f["g60"], f["g120"] = c / ma[20] - 1, c / ma[60] - 1, c / ma[120] - 1
    f["align"] = ((ma[20] > ma[60]) & (ma[60] > ma[120])).astype("float32")
    f["near52"] = c / c.rolling(252).max()
    for n in (5, 20, 60):
        f[f"r{n}"] = c / c.shift(n) - 1
    # 캔들
    f["ret"] = c / c.shift(1) - 1
    f["body"] = c / op - 1
    f["clv"] = (c - l) / (h - l).replace(0, np.nan)
    # RSI(14) · ATR(14)
    d = c.diff()
    up, dn = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean(), (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    f["rsi"] = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    f["atr"] = tr.rolling(14).mean() / c
    # 신호 뒤 3일(결정일 D3 에서 읽는다): 최근 3일의 종가·저가 최저, 평균 거래량
    f["cmin3"], f["lmin3"], f["vavg3"] = c.rolling(3).min(), l.rolling(3).min(), v.rolling(3).mean()
    f["close"], f["low"], f["volume"] = c, l, v
    f["ma20"], f["kijun"] = ma[20], kij
    return f.astype("float32")


# 조건 — (묶음, 이름, 식). X 는 결정일의 지표 프레임. D3 전용 조건은 신호일 값(c0·l0·v0)을 쓴다.
CONDS = (
    ("거래량", "거래량 20일 평균의 2배 이상", lambda X: X.vr >= 2.0),
    ("거래량", "거래량 20일 평균의 1.2배 미만(조용함)", lambda X: X.vr < 1.2),
    ("거래량", "5일 거래량 > 20일 거래량", lambda X: X.v5_20 > 1.0),
    ("거래량", "20일 거래량이 60일의 1.2배 이상(늘어나는 중)", lambda X: X.v20_60 >= 1.2),
    ("거래량", "20일 거래량이 60일의 0.8배 미만(마른 거래량)", lambda X: X.v20_60 < 0.8),
    ("오실레이터", "일봉 오실레이터 양봉", lambda X: X.osc >= 0),
    ("오실레이터", "일봉 오실레이터 상승 중", lambda X: X.osc_d > 0),
    ("오실레이터", "주봉 오실레이터 양봉", lambda X: X.wosc >= 0),
    ("오실레이터", "주봉 오실레이터 커지는 중", lambda X: X.wosc_d > 0),
    ("오실레이터", "주봉 오실레이터 음봉(아직 안 돌았다)", lambda X: X.wosc < 0),
    ("오실레이터", "월봉 오실레이터 양봉", lambda X: X.mosc >= 0),
    ("오실레이터", "월봉 오실레이터 커지는 중", lambda X: X.mosc_d > 0),
    ("오실레이터", "주봉·월봉 둘 다 양봉이고 커지는 중", lambda X: (X.wosc >= 0) & (X.wosc_d > 0) & (X.mosc >= 0) & (X.mosc_d > 0)),
    ("볼린저", "볼린저 상단 위(%b > 1)", lambda X: X.pb > 1.0),
    ("볼린저", "볼린저 상단 아래(%b ≤ 1)", lambda X: X.pb <= 1.0),
    ("볼린저", "밴드폭 수축(120일 중앙의 0.8배 미만)", lambda X: X.bwr < 0.8),
    ("볼린저", "밴드폭 확장(120일 중앙의 1.3배 이상)", lambda X: X.bwr >= 1.3),
    ("일목", "구름 위", lambda X: X.cloud > 0),
    ("일목", "구름 위 10% 이내", lambda X: (X.cloud > 0) & (X.cloud <= 0.10)),
    ("일목", "구름 위로 20% 넘게 떠 있다", lambda X: X.cloud > 0.20),
    ("일목", "전환선 > 기준선", lambda X: X.tk > 0),
    ("일목", "후행스팬이 26일 전 종가 위", lambda X: X.chikou > 0),
    ("일목", "기준선 이격 5% 이내", lambda X: (X.kij >= 0) & (X.kij <= 0.05)),
    ("일목", "앞 구름이 양운", lambda X: X.cloud_fut > 0),
    ("일목", "삼역호전(구름 위·전환선>기준선·후행스팬 위)", lambda X: (X.cloud > 0) & (X.tk > 0) & (X.chikou > 0)),
    ("엔벨롭", "20일선 이격 3% 이내", lambda X: (X.g20 >= 0) & (X.g20 <= 0.03)),
    ("엔벨롭", "20일선 이격 3~8%", lambda X: (X.g20 > 0.03) & (X.g20 <= 0.08)),
    ("엔벨롭", "20일선 이격 8% 초과(과열)", lambda X: X.g20 > 0.08),
    ("엔벨롭", "60일선 이격 10% 이내", lambda X: (X.g60 >= 0) & (X.g60 <= 0.10)),
    ("엔벨롭", "120일선 이격 15% 이내", lambda X: (X.g120 >= 0) & (X.g120 <= 0.15)),
    ("추세", "이평 정배열(20>60>120)", lambda X: X["align"] > 0.5),
    ("추세", "52주 최고 종가의 95% 이상", lambda X: X.near52 >= 0.95),
    ("추세", "최근 20일 상승 10% 이하", lambda X: X.r20 <= 0.10),
    ("추세", "최근 60일 상승 20% 이상", lambda X: X.r60 >= 0.20),
    ("추세", "최근 5일 상승 5% 이하", lambda X: X.r5 <= 0.05),
    ("캔들", "고가권 마감(종가가 그날 범위의 위 20%)", lambda X: X.clv >= 0.8),
    ("캔들", "그날 +5% 이상 상승", lambda X: X.ret >= 0.05),
    ("캔들", "그날 +2% 미만 상승", lambda X: X.ret < 0.02),
    ("RSI", "RSI 70 이상", lambda X: X.rsi >= 70),
    ("RSI", "RSI 50~70", lambda X: (X.rsi >= 50) & (X.rsi < 70)),
    ("시장", "시장 상승국면", lambda X: X.regime > 0.5),
    ("시장", "시장 하락국면", lambda X: X.regime < 0.5),
)
CONDS_D3 = (
    ("신호 뒤", "3일째 종가가 신호가 위", lambda X: X.close > X.c0),
    ("신호 뒤", "3일 동안 종가가 신호가를 안 깼다", lambda X: X.cmin3 >= X.c0),
    ("신호 뒤", "3일 동안 신호일 저가를 안 깼다", lambda X: X.lmin3 >= X.l0),
    ("신호 뒤", "거래량 줄며(3일 평균 < 신호일의 70%) 신호가 위", lambda X: (X.vavg3 < 0.7 * X.v0) & (X.close >= X.c0)),
)


# ────────────────────────────── 사건표

def build() -> None:
    import bottom
    import combo
    t = time.time()
    p = combo.build()
    codes = p.Code.cat.codes.to_numpy()
    n = len(p)
    last = np.flatnonzero(np.r_[codes[1:] != codes[:-1], True])
    end = np.empty(n, dtype=np.int64)
    for s, e in zip(np.r_[0, last[:-1] + 1], last):
        end[s:e + 1] = e
    liq = p.liq.to_numpy()
    L = p[p.liq]
    mkt = {h: p.dt.map(L.groupby("dt")[f"fwd{h}"].mean()).to_numpy() for h in HZ}
    fwd = {h: p[f"fwd{h}"].to_numpy(dtype="float64") for h in HZ}
    regime = p.f_regime.to_numpy(dtype="float32")
    ev = []
    for b, (sig, flt, _) in BASES.items():
        E = np.flatnonzero(combo.mask_of(p, sig, flt) & liq)
        j3 = np.minimum(E + 3, end[E])
        e = pd.DataFrame({"base": b, "Code": p.Code.to_numpy()[E].astype(str), "dt": p.dt.to_numpy()[E],
                          "delisted": p.delisted.to_numpy()[E], "alive3": (E + 3) <= end[E],
                          "regime0": regime[E], "regime3": regime[j3]})
        for h in HZ:
            e[f"f0_{h}"], e[f"m0_{h}"] = fwd[h][E], mkt[h][E]
            e[f"f3_{h}"], e[f"m3_{h}"] = fwd[h][j3], mkt[h][j3]
        ev.append(e)
        print(f"  {b}: {len(E):,}건", file=sys.stderr)
    E = pd.concat(ev, ignore_index=True)
    del p, L, fwd, mkt
    print(f"사건 {len(E):,}건 · combo {time.time() - t:.0f}초", file=sys.stderr)
    d = bottom.adjust(bottom.load("2006-01-01"))
    by = {c: g.index.to_numpy() for c, g in E.groupby("Code", sort=False)}
    F0 = F3 = None
    cols = None
    P = {k: np.full((len(E), PATH), np.nan, dtype="float32") for k in ("close", "ma20", "kijun")}
    left = np.zeros(len(E), dtype=np.int32)
    x0, x3 = None, None
    for code, g in d.groupby("Code", sort=False):
        rows = by.get(code)
        if rows is None:
            continue
        o = pd.DataFrame({"open": g.aO.to_numpy(), "high": g.aH.to_numpy(), "low": g.aL.to_numpy(),
                          "close": g.aC.to_numpy(), "volume": (g.Amount / g.aC).to_numpy()}, index=pd.DatetimeIndex(g.Date))
        f = feats(o)
        if cols is None:
            cols = list(f.columns)
            x0 = np.full((len(E), len(cols)), np.nan, dtype="float32")
            x3 = np.full((len(E), len(cols)), np.nan, dtype="float32")
        pos = o.index.get_indexer(E.dt.to_numpy()[rows])
        ok = pos >= 0
        rows, pos = rows[ok], pos[ok]
        a = f.to_numpy()
        m = len(o)
        x0[rows] = a[pos]
        p3 = pos + 3
        has3 = p3 < m
        x3[rows[has3]] = a[p3[has3]]
        idx = np.minimum(pos[:, None] + np.arange(PATH)[None, :], m - 1)
        for k in P:
            P[k][rows] = f[k].to_numpy()[idx]
        left[rows] = m - 1 - pos
    del d
    X0 = pd.DataFrame(x0, columns=[f"{c}" for c in cols])
    X3 = pd.DataFrame(x3, columns=[f"{c}" for c in cols])
    E["left"] = left                                            # 신호일 뒤에 남은 봉 수(자료 끝·상폐까지)
    pd.to_pickle({"E": E, "X0": X0, "X3": X3}, EVENTS)
    np.savez_compressed(PATHS, **P)
    print(f"사건표 저장 {EVENTS.name} · {time.time() - t:.0f}초", file=sys.stderr)


def load():
    z = pd.read_pickle(EVENTS)
    E, X0, X3 = z["E"], z["X0"], z["X3"]
    X0["regime"], X3["regime"] = E.regime0.to_numpy(), E.regime3.to_numpy()
    for c, src in (("c0", "close"), ("l0", "low"), ("v0", "volume")):
        X3[c] = X0[src].to_numpy()
    return E, X0, X3


# ────────────────────────────── 측정

def measure(f: np.ndarray, m: np.ndarray, dt: np.ndarray) -> dict | None:
    ok = np.isfinite(f) & np.isfinite(m)
    k = int(ok.sum())
    if k < 30:
        return None
    x = pd.Series(f[ok] - m[ok])
    by = x.groupby(dt[ok].astype("datetime64[M]")).mean()
    t = by.mean() / (by.std(ddof=1) / np.sqrt(len(by))) if len(by) > 2 else np.nan
    return {"n": k, "ex": 100 * float(x.mean()), "t": float(t), "up": 100 * float((f[ok] > 0).mean()),
            "abs": 100 * float(f[ok].mean()), "med": 100 * float(np.median(f[ok]))}


def cond_masks(X: pd.DataFrame, d3: bool) -> dict:
    out = {}
    for grp, name, fn in CONDS + (CONDS_D3 if d3 else ()):
        out[name] = np.asarray(fn(X), dtype=bool)
    return out


def search(E: pd.DataFrame, X: pd.DataFrame, base: str, dec: int) -> tuple[list[dict], dict]:
    """한 검색식·한 결정일에서 IS 로 조합을 고르고 OOS 를 붙인다. 반환 (고른 조합들, 기준 성적)."""
    sel = (E.base == base).to_numpy() & (E.alive3.to_numpy() if dec == 3 else True)
    e, x = E[sel], X[sel]
    dt = e.dt.to_numpy()
    f = {h: e[f"f{dec}_{h}"].to_numpy(dtype=float) for h in HZ}
    m = {h: e[f"m{dec}_{h}"].to_numpy(dtype=float) for h in HZ}
    IS = dt <= IS_END
    M = cond_masks(x, dec == 3)

    def st(mask, per):
        k = mask & per
        return {h: measure(f[h][k], m[h][k], dt[k]) for h in HZ}

    ref = {"IS": st(np.ones(len(e), bool), IS), "OOS": st(np.ones(len(e), bool), ~IS), "n_is": int(IS.sum()), "n_oos": int((~IS).sum())}
    halves = [IS & (dt <= IS_MID), IS & (dt > IS_MID)]
    href = [st(np.ones(len(e), bool), hf)[40] for hf in halves]

    def stable(mask) -> bool:
        """IS 앞뒤 절반 모두에서 2개월 오른 비율과 시장초과가 기준보다 낫다."""
        for hf, r in zip(halves, href):
            s = st(mask, hf)[40]
            if r is None or s is None or not (s["up"] > r["up"] and s["ex"] > r["ex"]):
                return False
        return True

    if ref["IS"][40] is None:
        return [], ref
    floor = max(MIN_N, MIN_SHARE * IS.sum())
    singles = []
    for name, mk in M.items():
        s = st(mk, IS)[40]
        if s and s["n"] >= floor and s["up"] > ref["IS"][40]["up"] and s["ex"] > ref["IS"][40]["ex"] and stable(mk):
            singles.append((s["up"] + s["ex"], name))
    names = [n for _, n in sorted(singles, reverse=True)[:TOP_SINGLES]]
    cand = []
    for r in (1, 2, 3):
        for combo_ in itertools.combinations(names, r):
            mk = np.logical_and.reduce([M[c] for c in combo_])
            s = st(mk, IS)
            if s[40] is None or s[40]["n"] < floor or not (s[40]["t"] >= MIN_T) or not stable(mk):
                continue
            cand.append({"conds": combo_, "mask": mk, "is": s})
    cand.sort(key=lambda c: (c["is"][40]["up"], c["is"][40]["ex"]), reverse=True)
    out = []
    for c in cand[:TOP_SHOW]:
        c["oos"] = st(c["mask"], ~IS)
        out.append(c)
    return out, ref


# ────────────────────────────── 청산

EXITS = (
    ("20일 뒤 매도", {"T": 20}),
    ("40일 뒤 매도", {"T": 40}),
    ("60일 뒤 매도", {"T": 60}),
    ("120일 뒤 매도", {"T": 120}),
    ("20일선 아래로 닫히면 매도(최장 120일)", {"T": 120, "trail": "ma20"}),
    ("기준선(26) 아래로 닫히면 매도(최장 120일)", {"T": 120, "trail": "kijun"}),
    ("20일선 이탈 또는 −8% 손절(최장 120일)", {"T": 120, "trail": "ma20", "stop": 0.08}),
    ("60일 보유 · −10% 손절", {"T": 60, "stop": 0.10}),
    ("+15% 익절 · −8% 손절 · 최장 60일", {"T": 60, "stop": 0.08, "take": 0.15}),
    ("+20% 익절 · −10% 손절 · 최장 120일", {"T": 120, "stop": 0.10, "take": 0.20}),
    ("+30% 익절 · 20일선 이탈 매도 · 최장 120일", {"T": 120, "trail": "ma20", "take": 0.30}),
)


def exit_offsets(c: np.ndarray, line: dict, rule: dict) -> np.ndarray:
    """c: (사건, 0~T) 종가 경로(0 = 산 날). 규칙에 처음 걸린 날의 오프셋(없으면 T). 전부 종가 판정·그날 종가 매도."""
    T = rule["T"]
    c = c[:, :T + 1]
    hit = np.zeros(c.shape, dtype=bool)
    if "stop" in rule:
        hit |= c < c[:, :1] * (1 - rule["stop"])
    if "take" in rule:
        hit |= c >= c[:, :1] * (1 + rule["take"])
    if "trail" in rule:
        ln = line[rule["trail"]][:, :T + 1]
        tr = c < ln
        tr[:, :3] = False                                   # 산 직후 사흘은 이탈로 치지 않는다(돌파 직후 흔들림)
        hit |= tr
    hit[:, 0] = False
    off = np.where(hit.any(axis=1), hit.argmax(axis=1), T)
    return off


def exit_stats(c: np.ndarray, line: dict, left: np.ndarray, delisted: np.ndarray) -> list[dict]:
    out = []
    for name, rule in EXITS:
        T = rule["T"]
        ok = (left >= T) | delisted                         # 기한까지 자료가 있거나 상폐로 끝난 것
        cc = c[ok]
        if len(cc) < 30:
            continue
        off = exit_offsets(cc, {k: v[ok] for k, v in line.items()}, rule)
        held = np.minimum(off, np.maximum(left[ok], 1))
        ret = cc[np.arange(len(cc)), off] / cc[:, 0] - 1
        out.append({"rule": name, "n": len(cc), "mean": 100 * float(ret.mean()), "med": 100 * float(np.median(ret)),
                    "up": 100 * float((ret > 0).mean()), "p5": 100 * float(np.quantile(ret, 0.05)),
                    "p95": 100 * float(np.quantile(ret, 0.95)), "days": float(held.mean()),
                    "perday": 100 * float(ret.mean()) / max(float(held.mean()), 1.0)})
    return out


# ────────────────────────────── 보고

def cell(s: dict | None) -> str:
    return "—" if s is None else f"{s['up']:.0f}% · {s['ex']:+.2f} (t{s['t']:+.1f}) · {s['n']:,}건"


def main() -> None:
    if "--build" in sys.argv or not EVENTS.exists():
        build()
        if "--build" in sys.argv:
            return
    E, X0, X3 = load()
    Pz = np.load(PATHS)
    P = {k: Pz[k] for k in ("close", "ma20", "kijun")}
    dt = E.dt.to_numpy()
    print(f"# 검색 뒤 조건 조합 → 매매 계획 ({pd.Timestamp(dt.min()):%Y-%m-%d} ~ {pd.Timestamp(dt.max()):%Y-%m-%d}, 상장폐지 포함)\n")
    print("칸 = **2개월 뒤 오른 비율** · 시장초과 평균 %p (월 군집 t) · 건수. 조합은 IS(2007~2019)만 보고 골랐다 — OOS(2020~)는 그 뒤에 본 것.\n")
    print("조합은 IS 를 앞뒤 절반(~2013-06 / 2013-07~)으로 나눠 **양쪽 모두에서** 기준보다 나은 것만 남겼다. "
          "이 요건은 첫 탐색의 OOS 를 본 뒤에 넣은 것이라(「시장 하락국면」 조합이 OOS 에서 무너졌다) OOS 수치가 완전히 깨끗한 확인은 아니다.\n")
    picks, shrink = [], []
    for base in BASES:
        for dec, X in ((0, X0), (3, X3)):
            got, ref = search(E, X, base, dec)
            lab = "신호일 종가 매수" if dec == 0 else "3일째 종가 매수"
            print(f"\n## {base} · {lab}\n")
            if ref["IS"][40] is None:
                print("건수 부족\n")
                continue
            print(f"조건 없이: IS {cell(ref['IS'][40])} · OOS {cell(ref['OOS'][40])}\n")
            if not got:
                print("IS 에서 기준(건수·t)을 넘으며 오른 비율과 초과수익을 함께 올린 조합이 없다.\n")
                continue
            print("| # | 조건(IS 로 고른 순) | IS 2개월 | OOS 2개월 | OOS 4주 | OOS 반년 |")
            print("|---:|---|---|---|---|---|")
            for i, c in enumerate(got, 1):
                print(f"| {i} | {' + '.join(c['conds'])} | {cell(c['is'][40])} | {cell(c['oos'][40])} | {cell(c['oos'][20])} | {cell(c['oos'][120])} |")
                if c["oos"][40] and ref["OOS"][40]:
                    shrink.append({"base": base, "dec": dec, "rank": i,
                                   "is_up": c["is"][40]["up"] - ref["IS"][40]["up"], "oos_up": c["oos"][40]["up"] - ref["OOS"][40]["up"],
                                   "is_ex": c["is"][40]["ex"] - ref["IS"][40]["ex"], "oos_ex": c["oos"][40]["ex"] - ref["OOS"][40]["ex"]})
            picks.append({"base": base, "dec": dec, "top": got[0], "ref": ref, "all": got})
    S = pd.DataFrame(shrink)
    S.to_csv(core.CACHE / "strategy_shrink.csv", index=False)
    print("\n## 과최적화의 크기 — IS 로 고른 조합이 OOS 에서 얼마나 남았나\n")
    print("| 묶음 | 조합 수 | IS 오른 비율 개선(중앙) | OOS 오른 비율 개선(중앙) | OOS 에서도 오른 비율이 나아진 조합 | IS 초과 개선 | OOS 초과 개선 | OOS 초과도 나아진 조합 |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|")
    for lab, g in [("전체", S)] + [(f"{'신호일' if d == 0 else '3일째'} 매수", g) for d, g in S.groupby("dec")] + [("1등 조합만", S[S["rank"] == 1])]:
        if len(g):
            print(f"| {lab} | {len(g)} | {g.is_up.median():+.1f}%p | {g.oos_up.median():+.1f}%p | {int((g.oos_up > 0).sum())}/{len(g)} | "
                  f"{g.is_ex.median():+.2f}%p | {g.oos_ex.median():+.2f}%p | {int((g.oos_ex > 0).sum())}/{len(g)} |")

    print("\n## 청산 규칙 — 검색식마다 IS 1등 조합에 얹어서 (건별 수익, 시장 차감 없음)\n")
    print("산 날 = 결정일 종가. 전부 종가로 판정하고 그날 종가에 판다. 청산 규칙도 IS 열만 보고 고른다.\n")
    for pk in picks:
        base, dec, top = pk["base"], pk["dec"], pk["top"]
        sel = np.flatnonzero((E.base == base).to_numpy() & (E.alive3.to_numpy() if dec == 3 else True))[top["mask"]]
        c = P["close"][sel][:, dec:]
        line = {"ma20": P["ma20"][sel][:, dec:], "kijun": P["kijun"][sel][:, dec:]}
        left = E.left.to_numpy()[sel] - dec
        dl = E.delisted.to_numpy()[sel]
        IS = dt[sel] <= IS_END
        print(f"\n### {base} · {'신호일' if dec == 0 else '3일째'} 매수 · {' + '.join(top['conds'])}\n")
        print("| 청산 규칙 | IS 평균 | IS 오른 비율 | IS 하위 5% | IS 보유일 | IS 1일당 | OOS 평균 | OOS 중앙 | OOS 오른 비율 | OOS 하위 5% | OOS 상위 5% | OOS 보유일 | OOS 1일당 | OOS 건수 |")
        print("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        a = {r["rule"]: r for r in exit_stats(c[IS], {k: v[IS] for k, v in line.items()}, left[IS], dl[IS])}
        b = {r["rule"]: r for r in exit_stats(c[~IS], {k: v[~IS] for k, v in line.items()}, left[~IS], dl[~IS])}
        for name, _ in EXITS:
            if name in a and name in b:
                x, y = a[name], b[name]
                print(f"| {name} | {x['mean']:+.2f}% | {x['up']:.0f}% | {x['p5']:+.1f}% | {x['days']:.0f} | {x['perday']:+.3f}% | "
                      f"{y['mean']:+.2f}% | {y['med']:+.2f}% | {y['up']:.0f}% | {y['p5']:+.1f}% | {y['p95']:+.1f}% | {y['days']:.0f} | {y['perday']:+.3f}% | {y['n']:,} |")


if __name__ == "__main__":
    main()
