"""검색식 조합 검정 — 지금까지의 신호·조건을 조합해 '최고의 검색식'을 찾는다 (사용자 요청 2026-09-30).

데이터: marcap(KRX 일별, **상장폐지 포함**) → bottom.load/adjust 로 분할 보정한 OHLC.
        신호 정의는 스캐너(core)의 함수를 **그대로** 호출한다 — 검정한 것과 화면이 같은 식이다.
측정:   신호일 종가 진입 → 5·20·40·120·250 거래일(1주·1달·2달·반년·1년) 뒤 종가.
        상폐로 그 전에 끝나면 마지막 거래가로 청산(정리매매 포함). 표본 끝에 걸린 신호는 뺀다.
        절대수익 · 상승확률 · **시장초과**(같은 날 유동 유니버스 평균 차감) · t(월 단위 군집).
유니버스: 20일 평균 거래대금 ≥ 10억(스캐너 기본값과 같다). 횡단면 순위도 그 안에서만 매긴다.
과최적화 방지: 조합은 **2007~2019(IS)** 성적으로만 고르고, **2020~(OOS)** 는 고른 뒤에 한 번 본다.

    python combo.py            # 패널 생성(캐시) → 단독 신호표 → 조합 탐색 → OOS 확인
"""
from __future__ import annotations

import hashlib
import itertools
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bottom  # noqa: E402
import core  # noqa: E402

HORIZONS = {5: "1주", 20: "1달", 40: "2달", 120: "반년", 250: "1년"}
MIN_VAL = 10e8
IS_END, OOS_START = "2019-12-31", "2020-01-01"
CACHE = core.CACHE / "combo_panel_{tag}.pkl"
COMBO_VERSION = 6          # 6: 전사본 신호 5종(bb40_break·gap_neg_reclaim·tail_reclaim·drop_recover·ema_seize, 2026-10-04) · 5: 하루 +200% 초과 중립 처리 · 4: 수익률을 KRX Changes 로(무상증자 보정) · 3: f_osc·f_regime 를 core.osc_ok·market_up 로 · 2: stoch_osc·f_osc 열 — 열 구성을 바꾸면 올린다(캐시 무효화)

SIGNAL_COLS = {            # 진입 사건(그날 하루)
    "매일(필터만)": None,
    "120일 신고가": "nh120",
    "기준봉 눌림": "sik_pullback",
    "BB상단+직전언덕": "bb_break",
    "EMA 수렴→발산": "ema_div",
    "기준선26 눌림": "kijun_pull",
    "골든크로스": "gc",
    "일봉양전": "stoch_turn_up",
    "카페주력": "cafe_juryeok",
    "알까기": "algkkagi",
    "바닥권 거래대금폭발": "bottom_spike",
    # 식이형 전사본(2026-10-04)에서 뽑은 다섯
    "상한선(대표가40) 첫 돌파": "bb40_break",
    "갭상승 음봉 시가 돌파": "gap_neg_reclaim",
    "위꼬리 고가 장악": "tail_reclaim",
    "장대음봉 하락폭 회복": "drop_recover",
    "장대양봉 장기선 장악": "ema_seize",
}
FILTERS = {                # 상태 조건(그날 참/거짓)
    "저변동30%": "f_lowvol",
    "모멘텀상위30%": "f_mom",
    "52주고가90%↑": "f_near52",
    "기준봉 생존중": "f_base",
    "시장 상승국면": "f_regime",
    "1년범위 상단30%": "f_recover",
    "EMA정배열": "f_ema",
    "소형주(하위50%)": "f_small",
    "급등일 아님": "f_nosurge",
    # 사용자 선호 타점(2026-09-30): 차트 스토캐스틱 칸의 osc 막대가 빨간 양봉이거나, 음봉이 줄어드는 날
    "오실레이터 양봉/음봉축소": "f_osc",
    "오실레이터 양봉만": "f_osc_red",
}


# ────────────────────────────── 패널

def _fingerprint() -> str:
    h = hashlib.sha1(f"v{core.PANEL_VERSION}.{COMBO_VERSION}".encode())
    for f in sorted(core.MARCAP.glob("marcap-*.parquet")):
        st = f.stat()
        h.update(f"{f.name}:{st.st_size}:{st.st_mtime_ns}".encode())
    return h.hexdigest()[:12]


def _one(g: pd.DataFrame, last_day: pd.Timestamp) -> pd.DataFrame:
    """한 종목. core 의 신호 함수를 그대로 부른다."""
    o = pd.DataFrame({"open": g.aO.to_numpy(), "high": g.aH.to_numpy(), "low": g.aL.to_numpy(),
                      "close": g.aC.to_numpy(), "volume": (g.Amount / g.aC).to_numpy()},
                     index=pd.DatetimeIndex(g.Date))
    o = core.add_indicators(o)
    o["cafe_juryeok"] = core.sig_cafe_juryeok(o)
    o["algkkagi"] = core.sig_algkkagi(o)
    sik = core.sig_sik_pullback(o)
    o["sik_pullback"], o["f_base"] = sik.sik_pullback, sik.sik_stop.notna()
    o["bb_break"], o["ema_div"], o["kijun_pull"] = core.sig_bb_break(o), core.sig_ema_div(o), core.sig_kijun_pull(o)
    o["bb40_break"], o["gap_neg_reclaim"], o["tail_reclaim"] = core.sig_bb40_break(o), core.sig_gap_neg_reclaim(o), core.sig_tail_reclaim(o)
    o["drop_recover"], o["ema_seize"] = core.sig_drop_recover(o), core.sig_ema_seize(o)
    since, rng = core.base_state(o.high, o.low)
    to = pd.Series((g.Amount / g.Marcap).to_numpy(), index=o.index)
    o["bottom_spike"] = ((since >= core.BASE_DAYS) & (rng <= core.BASE_BAND) & (to >= core.BASE_TURNOVER)).fillna(False)
    c = o.close
    o["mom"] = c.shift(21) / c.shift(252) - 1
    o["near52"] = c / c.rolling(252).max()
    lo, hi = o.low.rolling(252).min(), o.high.rolling(252).max()
    o["pos252"] = (c - lo) / (hi - lo)
    e = {n: c.ewm(span=n, adjust=False, min_periods=n).mean() for n in (120, 240, 480)}
    o["f_ema"] = (e[120] > e[240]) & (e[240] > e[480])
    o["f_osc"] = core.osc_ok(o.stoch_osc)
    o["mcap"] = g.Marcap.to_numpy()
    o["val20"] = pd.Series(g.Amount.to_numpy(), index=o.index).rolling(20).mean()
    delisted = o.index[-1] < last_day - pd.Timedelta(days=10)
    for h in HORIZONS:
        f = c.shift(-h) / c - 1
        if delisted:                                   # 상폐: h 전에 끝나면 마지막 거래가로 청산
            f = f.fillna(c.iloc[-1] / c - 1)
        o[f"fwd{h}"] = f.astype("float32")
    keep = (["close", "ret", "surge", "stoch_osc", "f_osc", "vol60", "mom", "near52", "pos252", "f_ema", "f_base", "mcap", "val20"]
            + [c for c in SIGNAL_COLS.values() if c] + [f"fwd{h}" for h in HORIZONS])
    out = o[keep].copy()
    out["delisted"] = delisted
    for col in ("stoch_osc", "ret", "vol60", "mom", "near52", "pos252", "close"):
        out[col] = out[col].astype("float32")
    return out


def build(force: bool = False) -> pd.DataFrame:
    cp = Path(str(CACHE).format(tag=_fingerprint()))
    if cp.exists() and not force:
        return pd.read_pickle(cp)
    t = time.time()
    d = bottom.adjust(bottom.load("2006-01-01"))
    last_day = d.Date.max()
    parts = []
    for code, g in d.groupby("Code", sort=False):
        if len(g) < 300:
            continue
        x = _one(g, last_day)
        x["Code"] = code
        parts.append(x.reset_index(names="dt"))
    del d
    p = pd.concat(parts, ignore_index=True)
    p["Code"] = p.Code.astype("category")
    p = p[p.dt >= "2007-01-01"].reset_index(drop=True)
    p = _cross(p)
    for f in core.CACHE.glob("combo_panel_*.pkl"):
        f.unlink()
    p.to_pickle(cp)
    print(f"  combo 패널 {len(p):,}행 · {p.Code.nunique():,}종목(상폐 {p.drop_duplicates('Code').delisted.sum():,}) "
          f"· {time.time() - t:.0f}초")
    return p


def _cross(p: pd.DataFrame) -> pd.DataFrame:
    """유동 유니버스 안에서만: 초과수익·순위·시장국면."""
    p["liq"] = (p.val20 >= MIN_VAL).to_numpy()
    L = p[p.liq]
    for h in HORIZONS:
        m = L.groupby("dt")[f"fwd{h}"].transform("mean")
        p[f"ex{h}"] = np.float32(np.nan)
        p.loc[L.index, f"ex{h}"] = (L[f"fwd{h}"] - m).astype("float32")
    for col, name in (("vol60", "r_vol"), ("mom", "r_mom"), ("mcap", "r_mcap")):
        p[name] = np.float32(np.nan)
        p.loc[L.index, name] = L.groupby("dt")[col].rank(pct=True).astype("float32")
    p["f_regime"] = False
    p.loc[L.index, "f_regime"] = core.market_up(L).to_numpy()     # 스캐너와 같은 함수·같은 유니버스
    p["f_lowvol"] = p.r_vol <= 0.30
    p["f_mom"] = p.r_mom >= 0.70
    p["f_near52"] = p.near52 >= 0.90
    p["f_recover"] = p.pos252 >= 0.70
    p["f_small"] = p.r_mcap <= 0.50
    p["f_nosurge"] = ~p.surge.fillna(False).astype(bool)
    p["f_osc_red"] = (p.stoch_osc > 0).to_numpy()
    return p


# ────────────────────────────── 측정

def measure(p: pd.DataFrame, mask: np.ndarray, horizons=HORIZONS) -> dict:
    """mask 행(유동 유니버스 안)의 지평별 성적. t 는 월 단위 군집 — 같은 달 신호는 한 표로 본다."""
    out = {"n": 0}
    base = mask & p.liq.to_numpy()
    for h in horizons:
        f = p[f"fwd{h}"].to_numpy()
        x = p[f"ex{h}"].to_numpy()
        m = base & np.isfinite(f) & np.isfinite(x)
        k = int(m.sum())
        out["n"] = max(out["n"], k)
        if k < 30:
            continue
        ex = pd.Series(x[m])
        by = ex.groupby(p.dt.to_numpy()[m].astype("datetime64[M]")).mean()
        t = by.mean() / (by.std(ddof=1) / np.sqrt(len(by))) if len(by) > 2 else np.nan
        out[h] = {"n": k, "abs": 100 * float(np.mean(f[m])), "up": 100 * float(np.mean(f[m] > 0)),
                  "ex": 100 * float(ex.mean()), "exmed": 100 * float(ex.median()),
                  "win": 100 * float((ex > 0).mean()), "t": float(t), "months": len(by),
                  "stocks": int(pd.unique(p.Code.cat.codes.to_numpy()[m]).size)}
    return out


def mask_of(p: pd.DataFrame, sig: str | None, filters: tuple[str, ...]) -> np.ndarray:
    m = np.ones(len(p), dtype=bool) if sig is None else p[sig].fillna(False).to_numpy(dtype=bool)
    for f in filters:
        m &= p[f].fillna(False).to_numpy(dtype=bool)
    return m


def fmt_row(name: str, r: dict, horizons=HORIZONS) -> str:
    cells = []
    for h in horizons:
        v = r.get(h)
        cells.append("—" if not v else f"{v['ex']:+.2f} ({v['t']:+.1f}) ↑{v['up']:.0f}%")
    return f"| {name} | {r['n']:,} | " + " | ".join(cells) + " |"


def main() -> None:
    p = build()
    dt = p.dt.to_numpy()
    IS = dt <= np.datetime64(IS_END)
    OOS = dt >= np.datetime64(OOS_START)
    hdr = "| 검색식 | 건수 | " + " | ".join(f"{v}({k}일)" for k, v in HORIZONS.items()) + " |"
    sep = "|---|---:|" + "---|" * len(HORIZONS)
    print("\n## 1. 단독 신호 (전 구간, 상폐 포함) — 시장초과 %p (t) ↑절대상승확률\n")
    print(hdr), print(sep)
    for name, col in SIGNAL_COLS.items():
        if col is None:
            continue
        print(fmt_row(name, measure(p, mask_of(p, col, ()))))
    print(fmt_row("(기준선) 유동 유니버스 전체", measure(p, np.ones(len(p), bool))))

    # 2. 조합 탐색 — IS 에서만. 신호 × 필터 0~2개.
    rows = []
    fkeys = list(FILTERS.items())
    for sname, scol in SIGNAL_COLS.items():
        for k in (0, 1, 2):
            for combo in itertools.combinations(fkeys, k):
                if scol is None and k == 0:
                    continue
                fcols = tuple(c for _, c in combo)
                m = mask_of(p, scol, fcols)
                r = measure(p, m & IS, horizons=(20, 40, 120))
                if any(h not in r for h in (20, 40, 120)) or r[40]["n"] < 300 or r[40]["stocks"] < 50:
                    continue
                ts = [r[h]["t"] for h in (20, 40, 120)]
                exs = [r[h]["ex"] for h in (20, 40, 120)]
                rows.append({"name": " + ".join([sname] + [n for n, _ in combo]), "sig": scol, "f": fcols,
                             "n_is": r[40]["n"], "t_min": min(ts), "ex40": exs[1], "ex_min": min(exs)})
    R = pd.DataFrame(rows)
    print(f"\n## 2. 조합 탐색 — {len(R)}개 조합(IS 2007~2019, 건수 300↑·종목 50↑)")
    # 세 지평 모두 양수이고, 가장 약한 지평의 t 가 큰 순서
    top = R[R.ex_min > 0].sort_values("t_min", ascending=False).head(15)
    print("\n### IS 상위 15 → OOS(2020~) 확인  · 값 = 시장초과 %p (t) ↑절대상승확률\n")
    print("| # | 검색식 | 구간 | 건수 | " + " | ".join(f"{v}" for v in HORIZONS.values()) + " |")
    print("|---|---|---|---:|" + "---|" * len(HORIZONS))
    for i, r in enumerate(top.itertuples(), 1):
        m = mask_of(p, r.sig, r.f)
        for lab, sel in (("IS", IS), ("OOS", OOS)):
            q = measure(p, m & sel)
            cells = ["—" if not q.get(h) else f"{q[h]['ex']:+.2f} ({q[h]['t']:+.1f}) ↑{q[h]['up']:.0f}%"
                     for h in HORIZONS]
            print(f"| {i if lab == 'IS' else ''} | {r.name if lab == 'IS' else ''} | {lab} | {q['n']:,} | "
                  + " | ".join(cells) + " |")
    R.to_csv(core.CACHE / "combo_results.csv", index=False)
    print(f"\n(전체 조합표: {core.CACHE / 'combo_results.csv'})")


if __name__ == "__main__":
    main()
