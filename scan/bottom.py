"""바닥 탈출 — "바닥권에서 하루 거래대금이 시가총액의 30% 이상 터진 날" (사용자 제안 2026-09-29).

데이터: `~/market/raw/marcap/marcap-YYYY.parquet` (FinanceData/marcap, KRX 일별 원자료)
  - 거래대금(Amount)·시가총액(Marcap)·상장주식수(Stocks)가 **그날 실제 값**이다. 로컬 시세(FDR)에는
    과거 주식수가 없어 과거 시총을 못 만든다(지금 주식수로 나누면 증자·CB 전환 종목에서 틀린다).
  - **상장폐지 종목이 들어 있다.** 바닥 매수는 생존편향에 가장 취약한 전략이다 — 바닥에서 못 나오고
    사라진 종목이 빠지면 성과가 부풀려진다. 그래서 검정은 로컬 시세가 아니라 이것으로 한다.
  - 가격은 **수정 전**이다. 일수익률은 KRX 전일대비(Changes = 종가 − 기준가)로 구한다 — 권리락·분할·병합이
    반영된 공식 값이다(2026-09-30). Changes 가 없는 행만 '주식수가 크게 바뀌고 가격이 반비례로 움직인 날'
    추정식으로 보정한다.

정의
  바닥권(전날 기준) : 종가 ≤ 1년(252일) 최저 저가 × (1+ZONE_UP)  그리고  1년 고점 ≥ 1년 저점 × DROP
  탈출 신호(오늘)   : 바닥권 & 거래대금/시총 ≥ 기준(기본 30%). 같은 종목은 COOLDOWN 거래일 안에 한 번.
  바닥권은 **전날**로 본다 — 신호일 종가는 이미 튀어 바닥권을 벗어났을 수 있다(룩어헤드 방지).

검정: 신호일 종가 진입 → H 거래일 뒤 수익. 같은 날 유니버스 평균을 빼 초과수익, 날짜별 평균의 t.
  상폐로 H 전에 끝나면 마지막 거래가(정리매매 포함)로 청산. 표본 끝에 걸려 H 를 못 채운 신호는 뺀다.
"""
from __future__ import annotations

import glob
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

MARCAP = core.ROOT / "raw" / "marcap"
ZONE_UP = 0.15       # 1년 저점 대비 +15% 이내
DROP = 1.5           # 1년 고점이 저점의 1.5배 이상(=고점에서 33% 이상 하락)
TURNOVER = 0.30      # 거래대금 / 시총
MAX_DAY_RET = 2.0    # 하루 수익률 상한(+200%) — 넘으면 데이터 오류로 본다
COOLDOWN = 20        # 같은 종목 재신호 금지 거래일
HORIZONS = (20, 60, 120)
COLS = ["Date", "Code", "Name", "Market", "Open", "High", "Low", "Close", "Amount", "Marcap", "Stocks", "Changes", "Volume"]


def load(since: str = "2006-01-01") -> pd.DataFrame:
    """marcap 전 연도 → 보통주(KOSPI·KOSDAQ, 우선주·스팩·리츠 제외), 코드·날짜 정렬."""
    fs = sorted(glob.glob(str(MARCAP / "marcap-*.parquet")))
    fs = [f for f in fs if int(Path(f).stem[-4:]) >= int(since[:4])]
    if not fs:
        raise FileNotFoundError(f"marcap 파일 없음: {MARCAP}")
    d = pd.concat([pd.read_parquet(f, columns=COLS) for f in fs], ignore_index=True)
    d = d[d.Market.isin(("KOSPI", "KOSDAQ", "KOSDAQ GLOBAL"))
          & d.Code.str.endswith("0") & ~d.Name.str.contains("스팩|리츠", na=False)]
    d = d[(d.Close > 0) & (d.Marcap > 0)]
    d = d.sort_values(["Code", "Date"]).reset_index(drop=True)
    d = repair_tail(d)
    for c in ("Open", "High", "Low", "Close"):
        d[c] = d[c].astype("float64")
    return d


# marcap 최근 구간 보정은 core 에 있다(앱 패널의 turnover 도 같은 보정을 쓴다)
app_tail, repair_tail = core.app_tail, core.repair_tail


def adjust(d: pd.DataFrame) -> pd.DataFrame:
    """액면분할·병합 보정 수익률(ret)과 수정 OHLC(aO·aH·aL·aC)."""
    g = d.groupby("Code", sort=False)
    rs = d.Stocks / g.Stocks.shift(1)
    rp = d.Close / g.Close.shift(1)
    split = ((rs > 1.4) | (rs < 0.7)) & (np.abs(np.log((rs * rp).clip(lower=1e-9))) < 0.35)
    d["split"] = split.fillna(False)
    d["ret"] = np.where(d.split, rp * rs, rp) - 1
    if "Changes" in d:
        # ★ KRX 전일대비(Changes)는 **기준가** 대비다 — 권리락(무상·유상증자)·분할·병합·감자가 이미 반영돼 있다.
        #   위의 '같은 날 주식수·가격 반비례' 추정은 무상증자를 놓친다: 권리락일(가격 하락)과 신주 상장일(주식수
        #   증가)이 몇 주 어긋나서다(알테오젠 2020-07-23: 종가 308,700→185,000 이 −40% 로 찍혔다, 실제 +19.8%).
        #   앱 수정주가(FDR)와 일수익률이 3%p↑ 어긋나는 날: 추정식 3,364일 → Changes 25일(2015~2025 실측).
        #   Changes 가 없거나 기준가가 0 이하인 행만 추정식으로 남긴다.
        base = d.Close - d.Changes
        ok = d.Changes.notna() & (base > 0)
        d["ret"] = np.where(ok, d.Close / base.where(ok, 1.0) - 1, d.ret)
    # 하루 +200% 초과는 제도상 나올 수 없다(가격제한 ±30%, 감자 후 재상장 첫날도 기준가 대비 최대 +160%).
    # 실측 20년에 25일 안팎 — 전일 종가가 1원으로 찍힌 오류(에스와이코퍼레이션 2013-09-11: +6,699,900%)나
    # 감자·증자가 겹쳐 기준가가 반영 안 된 재상장일이다. 하나가 그 시기 시장 평균을 통째로 부풀리므로
    # 진짜 값을 알 수 없는 이런 날은 0%(중립)로 두고 bad 로 표시한다.
    d["bad"] = (d.ret > MAX_DAY_RET).fillna(False)
    d.loc[d.bad, "ret"] = 0.0
    d.loc[g.cumcount() == 0, "ret"] = 0.0
    lvl = (1 + d.ret.fillna(0)).groupby(d.Code, sort=False).cumprod()
    f = lvl * d.groupby("Code", sort=False).Close.transform("first") / d.Close
    for c in ("Open", "High", "Low", "Close"):
        d["a" + c[0]] = d[c] * f
    return d


def features(d: pd.DataFrame, turnover: float = TURNOVER, zone_up: float = ZONE_UP,
             drop: float = DROP) -> pd.DataFrame:
    g = d.groupby("Code", sort=False)
    d["low252"] = g.aL.transform(lambda s: s.rolling(252, min_periods=252).min())
    d["high252"] = g.aH.transform(lambda s: s.rolling(252, min_periods=252).max())
    d["zone"] = (d.aC <= d.low252 * (1 + zone_up)) & (d.high252 >= d.low252 * drop)
    d["zone_prev"] = g.zone.shift(1).astype("boolean").fillna(False).astype(bool)
    d["to"] = d.Amount / d.Marcap                     # 그날 실제 거래대금 ÷ 실제 시총
    d["val20_prev"] = g.Amount.transform(lambda s: s.rolling(20).mean().shift(1))
    d["i"] = g.cumcount()
    return d


def base_features(d: pd.DataFrame) -> pd.DataFrame:
    """'52주 신저가 후 횡보' 판정용 (사용자 정의 2026-09-29).

      new_low     : 오늘 저가가 전날까지 52주(252일) 최저보다 낮다 → 그날이 새 바닥
      since_low   : 마지막 52주 신저가 이후 지난 거래일 수(전날 기준)
      base_range  : 마지막 신저가일 ~ **전날** 고가 최고 ÷ 저가 최저 − 1 (횡보 폭 — 아직 안 튀었나)
    신호일 t 의 판정은 전부 t−1 까지로 한다(그날 튄 것으로 횡보를 판정하지 않는다)."""
    g = d.groupby("Code", sort=False)
    # '신저가 갱신' = 전날까지 252일 최저보다 **낮아진** 날. 같은 값(재차 터치)까지 넣으면(<=) 바닥 재확인이
    # 나올 때마다 횡보가 0 부터 다시 세어진다(회귀 테스트가 잡았다 2026-09-29).
    prior = g.aL.transform(lambda s: s.rolling(252, min_periods=252).min().shift(1))
    d["new_low"] = (d.aL < prior) & prior.notna()
    li = d.i.where(d.new_low)
    d["last_low_i"] = li.groupby(d.Code, sort=False).ffill()
    seg = [d.Code, d.last_low_i]
    hi = d.aH.groupby(seg, sort=False).cummax()
    lo = d.aL.groupby(seg, sort=False).cummin()
    d["base_range"] = (hi / lo - 1).groupby(d.Code, sort=False).shift(1)
    d["since_low"] = (d.i - d.last_low_i).groupby(d.Code, sort=False).shift(1) + 1
    same_seg = d.last_low_i == d.last_low_i.groupby(d.Code, sort=False).shift(1)
    d.loc[~same_seg, ["base_range", "since_low"]] = np.nan   # 오늘 새 신저가면 횡보가 아니다
    return d


def base_signal(d: pd.DataFrame, days: int, band: float, turnover: float) -> pd.Series:
    """52주 신저가 후 days 거래일 이상 새 신저가 없이, 폭 band 이내로 횡보하던 종목에서 거래대금/시총 ≥ turnover."""
    return (d.since_low >= days) & (d.base_range <= band) & (d.to >= turnover)


def forward(d: pd.DataFrame, horizons=HORIZONS) -> pd.DataFrame:
    """H 거래일 뒤 수익. 상폐로 끝나면 마지막 가격으로 청산, 표본 끝에 걸리면 NaN."""
    last_all = d.Date.max()
    g = d.groupby("Code", sort=False)
    n = g.i.transform("max")
    last_date = g.Date.transform("max")
    delisted = last_date < last_all - pd.Timedelta(days=10)
    last_px = g.aC.transform("last")
    for h in horizons:
        fwd = g.aC.shift(-h) / d.aC - 1
        short = d.i + h > n                            # H 를 못 채움
        fwd = np.where(short & delisted, last_px / d.aC - 1, fwd)
        d[f"f{h}"] = np.where(short & ~delisted, np.nan, fwd)
    d["delisted"] = delisted
    return d


def dedupe(ev: pd.DataFrame, cooldown: int = COOLDOWN) -> pd.DataFrame:
    keep, last = [], {}
    for idx, code, i in zip(ev.index, ev.Code, ev.i):
        if code in last and i - last[code] < cooldown:
            continue
        last[code] = i
        keep.append(idx)
    return ev.loc[keep]


def study(d: pd.DataFrame, mask: pd.Series, horizons=HORIZONS, universe: pd.Series | None = None,
          cooldown: int = COOLDOWN) -> dict:
    """시장중립 초과수익(같은 날 유니버스 평균 대비) · 날짜 묶음 t."""
    uni = universe if universe is not None else pd.Series(True, index=d.index)
    ev = dedupe(d[mask & uni], cooldown)
    out = {"n": len(ev), "종목": ev.Code.nunique()}
    for h in horizons:
        col = f"f{h}"
        base = d.loc[uni, col].groupby(d.loc[uni, "Date"]).mean()
        e = ev[[col, "Date"]].dropna()
        x = e[col] - e.Date.map(base)
        by = x.groupby(e.Date).mean()
        t = by.mean() / (by.std() / np.sqrt(len(by))) if len(by) > 2 else np.nan
        out[h] = {"n": len(x), "초과평균": 100 * x.mean(), "초과중앙": 100 * x.median(),
                  "시장대비승률": 100 * (x > 0).mean(), "t": t, "절대평균": 100 * e[col].mean(),
                  "반토막비율": 100 * (e[col] <= -0.5).mean()}
    out["상폐비율"] = 100 * ev.delisted.mean() if len(ev) else np.nan
    return out


def report(res: dict, label: str) -> str:
    rows = [f"■ {label}  신호 {res['n']:,}건 · {res['종목']:,}종목 · 이후 상폐 {res['상폐비율']:.1f}%"]
    for h in HORIZONS:
        r = res[h]
        rows.append(f"   {h:>3}일  초과 {r['초과평균']:+6.2f}% (중앙 {r['초과중앙']:+6.2f}%)  "
                    f"시장대비승률 {r['시장대비승률']:5.1f}%  t={r['t']:+5.2f}  "
                    f"절대 {r['절대평균']:+6.2f}%  −50%이하 {r['반토막비율']:4.1f}%  (n={r['n']:,})")
    return "\n".join(rows)


def main() -> None:
    import time
    t0 = time.time()
    d = forward(features(adjust(load())))
    d = d[d.Date >= "2007-01-01"].copy()
    print(f"데이터 {len(d):,}행 · {d.Code.nunique():,}종목(상폐 포함 "
          f"{d.loc[d.delisted, 'Code'].nunique():,}) · {d.Date.min():%Y-%m-%d}~{d.Date.max():%Y-%m-%d} · "
          f"분할 보정 {int(d.split.sum()):,}건 · {time.time()-t0:.0f}초", flush=True)
    liq = d.val20_prev >= 1e9
    yang = d.Close > d.Open
    for x in (0.10, 0.20, 0.30, 0.50):
        print(report(study(d, d.zone_prev & (d.to >= x)), f"바닥권 + 거래대금/시총 ≥ {x:.0%}"))
    print(report(study(d, d.zone_prev & (d.to >= 0.30) & yang), "바닥권 + ≥30% + 양봉"))
    print(report(study(d, d.zone_prev & (d.to >= 0.30), universe=liq), "바닥권 + ≥30% (유동성 10억+ 유니버스)"))
    print(report(study(d, (d.to >= 0.30)), "비교: 바닥권 없이 ≥30% 만"))
    base_grid(d)


def base_grid(d: pd.DataFrame) -> None:
    """52주 신저가 후 횡보 + 거래대금 폭발 — 기간·폭·기준 조합 전부."""
    d = base_features(d)
    print("\n■ 52주 신저가 후 횡보 D일↑ · 폭 B 이내 · 거래대금/시총 ≥ X   (초과수익 %, t)")
    print(f"{'D':>3} {'B':>4} {'X':>4} {'n':>6}   {'20일':>15} {'60일':>15} {'120일':>15}  승률120")
    for days in (20, 40, 60):
        for band in (0.20, 0.30):
            for x in (0.10, 0.20, 0.30):
                r = study(d, base_signal(d, days, band, x))
                if not r["n"]:
                    continue
                cells = "  ".join(f"{r[h]['초과평균']:+6.2f}({r[h]['t']:+5.2f})" for h in HORIZONS)
                print(f"{days:>3} {band:>4.0%} {x:>4.0%} {r['n']:>6,}   {cells}  {r[120]['시장대비승률']:5.1f}%")


if __name__ == "__main__":
    main()
