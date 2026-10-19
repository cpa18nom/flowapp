#!/usr/bin/env python3
"""core — 전종목 스캐너 엔진 (데이터·지표·신호·검정)

chasamo.exe(차사모 검색기)를 역해석해 얻은 패턴을 비교군으로 싣고,
2,472종목 × 19년 실측으로 살아남은 신호를 주력으로 쓴다.

■ 설계 원칙 두 가지 (둘 다 이번 분석에서 실제로 데인 것)

1) 횡단면 연산은 '검정 유니버스 안에서만' 한다.
   유동성 필터 전 모집단 평균으로 초과수익을 계산하면 기준선이 0이 아니게 되어
   (실측 -1.2048%) 모든 신호가 음수로 보인다. excess() 가 이걸 강제한다.

2) 성과는 '실현수익'으로 본다.
   원본 프로그램의 최고수익 컬럼은 진입 후 120일 중 최고가까지의 수익이다.
   32,011건 실측에서 그 값 평균 +45.82% vs 실제 120일 보유 +5.36%(중앙 -5.51%).
   peak 는 참고로만 같이 보여주고 판단은 실현수익으로 한다.

■ 실측 근거 (2,472종목 · 2016-2026 · 거래대금 10억 이상 · 시장중립)
   저변동30% × 120일신고가 : 20일 승률 48.5% · 비용차감 연 +7.3%p   ← 기본 전략
   급등추격(거래량3배+5%)   : 60일 초과 -2.64%, t=-14.18, 5구간 전부 음 ← 회피
   일봉양전(카페 간판신호)   : 60일 초과 +0.08%, t=-2.36              ← 무효
   알까기                  : 60일 초과 -0.21%, t=-4.04              ← 음수
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(os.environ.get("MARKET_HOME", Path.home() / "market"))
PRICE = ROOT / "raw" / "price"
FLOW = ROOT / "raw" / "flow"
META = ROOT / "raw" / "meta"
CACHE = ROOT / "scan" / "cache"

# ── chasamo.exe 에서 추출한 상수 (260721_stoch_world_v2_baepo) ──
CAFE_DECLINE_LOOKBACK = 60
CAFE_CONSOLIDATION_DAYS = 10
CAFE_CONS_RANGE_MAX_PCT = 18.0
CAFE_BELOW_MA20_RATIO = 0.7
CAFE_TOUCH_TOL_PCT = 2.0
ALG_PRIOR_RISE_LOOKBACK = 60
ALG_PULLBACK_WINDOW = 15
ALG_RECLAIM_WINDOW = 5
STOCH_K, STOCH_DFAST, STOCH_DSLOW = 12, 26, 9   # 원본 기본값 (MACD 주기)

# 지표 정의를 고치면 이 값을 올린다. 캐시 지문에 들어가 옛 패널이 자동 무효화된다.
PANEL_VERSION = 22         # 22: 앱이 안 읽는 열(APP_SKIP)을 혼자 블록으로 — 앱이 복사 없이 지운다(2026-10-19) · 21: crow_star 에 5·20·60일선 정배열(흑삼병 전날~당일) 조건(2026-10-16) · 20: crow_star(흑삼병 뒤 짧은 양봉·도지 + 주봉·월봉 오실레이터 — 사용자 그림 4, 2026-10-16) · 19: stairs_ready(계단식 매집 분출 전 수렴, 2026-10-15) · 18: stairs_break(계단식 매집봉 반복 뒤 1년 고점 돌파 — 사용자 그림 3, 2026-10-15) · 17: accum(매집 의심 — 세력 순매수·횡보·미분산·평단 근처, 2026-10-15) · 16: osd_vol(스토캐스틱 과매도·거래량·VWAP>MA60, indsetup 전 기간 1등, 2026-10-07) · 15: wave_collapse·spike_accum(사용자 그림 검색식, 2026-10-07) · 14: frgn5·orgn5(외국인·기관 5일 순매수 ÷ 거래량) + 수급 주식 수를 수정주가 기준으로 맞춤(flow_factor, 2026-10-05) · 13: ind_disp(개인 분산비율 — 수급 조건, winners.py 2026-10-04) · 12: bb40_break(상한선 대표가 40일·2σ 첫 돌파 — 식이형 전사본, 2026-10-04) · 11: osc_mtf(월봉·주봉 양봉 확대 + 일봉 양봉 전환, 2026-09-30) · 10: 오실레이터 양봉 상태·음봉 3칸 축소(osc_pos_d·osc_pos_w·osc_shrink3_d·osc_shrink3_w, 2026-09-30) · 9: 오실레이터 검색식(osc_shrink_d·osc_up_w·osc_shrink_w, 2026-09-30) · 8: turnover 에 marcap 꼬리 보정(2026-09-30) · 7: mom 열(승률형, 2026-09-30) · 6: bb_break·ema_div·kijun_pull·osc_ok(2026-09-30) · 5: sik_pullback·sik_center·sik_stop 열(2026-09-30) · 4: bottom_spike·turnover 열 추가(2026-09-29) · 3: dtype 압축 — 옛 캐시 무효화

COST_ROUNDTRIP_PCT = 0.4    # 증권거래세 0.18% + 슬리피지


# ────────────────────────────── 지표 (종목별 시계열)

def stoch_lines(h: pd.Series, l: pd.Series, c: pd.Series, k: int = STOCH_K,
                dfast: int = STOCH_DFAST, dslow: int = STOCH_DSLOW) -> pd.DataFrame:
    """'일봉양전' 검색식의 스토캐스틱 — 차사모 검색기 calc_stoch_kiwoom 을 그대로 옮긴 것
    (원본 출력 226/226 재현, reference_chasamo_reverse). **표준 슬로우 스토캐스틱이 아니다.**

      fastK = (C − LL_k) / (HH_k − LL_k) × 100          LL/HH: k일 최저 저가·최고 고가
      fastD = EMA(fastK, dfast)
      slowK = Σ_dfast(C − LL_k) / Σ_dfast(HH_k − LL_k) × 100
      slowD = EMA(slowK, dslow)
      osc   = fastD − slowD        → 음→양 전환일이 '일봉양전'

    기본값 (12, 26, 9) 은 MACD 주기를 스토캐스틱에 넣은 원본 제작자의 변형이다.
    """
    lowest, highest = l.rolling(k).min(), h.rolling(k).max()
    denom = np.maximum(highest - lowest, 1e-12)
    fastK = ((c - lowest) / denom * 100).fillna(0)
    fastD = fastK.ewm(span=dfast, adjust=False).mean()
    num_s = (c - lowest).rolling(dfast).sum()
    den_s = (highest - lowest).rolling(dfast).sum()
    slowK = pd.Series(np.where(den_s != 0, num_s / np.maximum(den_s, 1e-12) * 100, 0), index=c.index)
    slowD = slowK.ewm(span=dslow, adjust=False).mean()
    return pd.DataFrame({"fastK": fastK, "fastD": fastD, "slowK": slowK, "slowD": slowD,
                         "osc": fastD - slowD}, index=c.index)


def add_indicators(d: pd.DataFrame) -> pd.DataFrame:
    """d: index=dt, columns open/high/low/close/volume (한 종목)."""
    o = d.copy()
    c, h, l, v = o.close, o.high, o.low, o.volume
    o["ret"] = c.pct_change()
    o["val20"] = (c * v).rolling(20).mean()
    o["v20"] = v.rolling(20).mean()
    for n in (3, 5, 20, 60, 120, 200):
        o[f"ma{n}"] = c.rolling(n).mean()
    for n in (20, 60, 120, 252):
        o[f"hh{n}"] = c.rolling(n).max()
        # ★ 신고가 돌파는 반드시 '종목의 연속 시계열'에서 판정한다.
        #    유동성으로 걸러낸 뒤 shift(1) 을 하면 걸러져 빠진 날이 '전일'로 둔갑해
        #    신호가 부풀어 오른다(실측: 6,291건 → 16,092건).
        o[f"nh{n}"] = (c >= o[f"hh{n}"]) & (c.shift(1) < o[f"hh{n}"].shift(1))
    o["vol60"] = o.ret.rolling(60).std()
    o["mom"] = c.shift(21) / c.shift(252) - 1          # 모멘텀 12-1: 1년 전 대비, 최근 1달 제외
    o["surge"] = (v >= 3.0 * o.v20) & (o.ret >= 0.05)
    # 카페 검색기 지표 — 계산은 stoch_lines 하나로(차트가 같은 함수를 그린다)
    o["stoch_osc"] = stoch_lines(h, l, c).osc
    o["stoch_turn_up"] = ((o.stoch_osc.shift(1) < 0) & (o.stoch_osc >= 0))
    o["osc_shrink_d"] = osc_shrink_start(o.stoch_osc)
    o["osc_pos_d"] = osc_positive(o.stoch_osc)
    o["osc_shrink3_d"] = osc_shrink_n(o.stoch_osc)
    w = weekly_osc_frame(o)
    o["osc_up_w"], o["osc_shrink_w"], o["osc_pos_w"], o["osc_shrink3_w"] = w.up, w.shrink, w.pos, w.shrink3
    o["osc_mtf"] = o.stoch_turn_up & osc_growing(o, "W-FRI") & osc_growing(o, "M")
    o["gc"] = (((o.ma3.shift(1) <= o.ma20.shift(1)) & (o.ma3 > o.ma20)) |
               ((o.ma5.shift(1) <= o.ma20.shift(1)) & (o.ma5 > o.ma20)))
    return o


# ────────────────────────────── 신호





def sig_cafe_juryeok(o: pd.DataFrame, decline_min_pct: float = 15.0) -> pd.Series:
    """카페주력 — 하락 → 좁은 박스권 → 20일선 아래 체류 → 오늘 첫 20일선 터치."""
    n = CAFE_CONSOLIDATION_DAYS
    swing_high = o.high.shift(n).rolling(CAFE_DECLINE_LOOKBACK).max()
    recent_low = o.low.rolling(n).min()
    cond_decline = (recent_low - swing_high) / swing_high * 100.0 <= -abs(decline_min_pct)
    cons_high = o.high.shift(1).rolling(n).max()
    cons_low = o.low.shift(1).rolling(n).min()
    cond_cons = (cons_high - cons_low) / cons_low.replace(0, np.nan) * 100.0 <= CAFE_CONS_RANGE_MAX_PCT
    below = (o.close < o.ma20).astype(float)
    cond_below = below.shift(1).rolling(n).mean() >= CAFE_BELOW_MA20_RATIO
    cond_touch = (o.close.shift(1) < o.ma20.shift(1)) & (o.high >= o.ma20 * (1 - CAFE_TOUCH_TOL_PCT / 100.0))
    return (cond_decline & cond_cons & cond_below & cond_touch).fillna(False)


def sig_algkkagi(o: pd.DataFrame, rise_min_pct: float = 20.0, pullback_max_pct: float = 15.0) -> pd.Series:
    """알까기 — 1차 상승 → 얕은 눌림 → 20일선 이탈 → 오늘 회복. 실측 -0.21%(t-4.04)."""
    m = ALG_PULLBACK_WINDOW
    swing_low = o.low.shift(m).rolling(ALG_PRIOR_RISE_LOOKBACK).min()
    recent_high = o.high.rolling(m).max()
    cond_rise = (recent_high - swing_low) / swing_low.replace(0, np.nan) * 100.0 >= abs(rise_min_pct)
    pb = (o.close - recent_high) / recent_high.replace(0, np.nan) * 100.0
    cond_shallow = (pb >= -abs(pullback_max_pct)) & (pb <= -3.0)
    cond_under = o.low.shift(1).rolling(ALG_RECLAIM_WINDOW).min() < o.ma20.shift(1)
    cond_reclaim = (o.close >= o.ma20) & (o.close.shift(1) < o.ma20.shift(1))
    return (cond_rise & cond_shallow & cond_under & cond_reclaim).fillna(False)


WIN_MOM_Q, WIN_NEAR52 = 0.70, 0.90     # 승률형: 모멘텀 상위 30% · 종가 ≥ 52주 최고 종가의 90%
SIK_RISE, SIK_VMULT, SIK_LOOKBACK, SIK_TOL = 0.10, 2.0, 20, 0.02


def sig_sik_pullback(o: pd.DataFrame, rise: float = SIK_RISE, vmult: float = SIK_VMULT,
                     lookback: int = SIK_LOOKBACK, tol: float = SIK_TOL) -> pd.DataFrame:
    """기준봉 눌림 — 식이형주식 매매법 §3 「매수=중심값 · 손절=기준봉 시가」(2026-09-30).

      기준봉 : 전일 종가 대비 +rise 이상 · 양봉(종가>시가) · 거래량 ≥ 전날까지 20일 평균 × vmult
      중심값 : (기준봉 시가 + 종가) ÷ 2   — 몸통의 중간, 꼬리 제외
      생존   : 기준봉 이후 **종가**가 한 번도 기준봉 시가 아래로 닫히지 않음
      신호   : 기준봉 1~lookback 거래일 뒤, 저가가 중심값×(1+tol) 이하로 처음 내려온 날(생존 중)

    새 기준봉이 나오면 기준이 그것으로 바뀐다(가장 최근 기준봉 하나만 본다).
    저가권·저평가권 조건은 넣지 않는다 — 9/14 검정에서 역효과(t −9.9 / −7.9)였다.
    반환: sik_pullback(bool) · sik_center(중심값) · sik_stop(기준봉 시가)."""
    c, op, lo, v = o.close, o.open, o.low, o.volume
    base = ((c / c.shift(1) - 1 >= rise) & (c > op)
            & (v >= vmult * v.rolling(20).mean().shift(1))).fillna(False)
    i = pd.Series(np.arange(len(c), dtype=float), index=c.index)
    bidx = i.where(base).ffill()
    age = i - bidx
    center = ((op + c) / 2).where(base).ffill()
    stop = op.where(base).ffill().astype(float)
    alive = c.groupby(bidx).cummin() >= stop               # 기준봉 당일 종가 > 시가 이므로 당일은 생존
    touch = (age >= 1) & (age <= lookback) & alive & (lo <= center * (1 + tol))
    first = touch & (touch.astype(int).groupby(bidx).cumsum() == 1)
    return pd.DataFrame({"sik_pullback": first.fillna(False).astype(bool),
                         "sik_center": center.where(alive & (age <= lookback)),
                         "sik_stop": stop.where(alive & (age <= lookback))}, index=c.index)


def osc_ok(osc: pd.Series) -> pd.Series:
    """사용자 선호 타점(2026-09-30): 스토캐스틱 칸 osc 막대가 빨간 양봉(>0)이거나, 음봉이 줄어드는 날
    (<0 이면서 전날보다 올라옴). 단독으로는 효과가 없고(combo.py), 조합 1위 신호의 95% 가 이미 만족한다."""
    return ((osc > 0) | ((osc < 0) & (osc > osc.shift(1)))).fillna(False)


def market_up(df: pd.DataFrame, n: int = 120) -> pd.Series:
    """시장 상승국면 — 전달받은 유니버스의 동일가중 지수가 n 일 이동평균 위인 날(행 단위 bool).
    횡단면 연산이므로 반드시 검정 유니버스(유동성으로 좁힌 df)로 부른다. 그날 종가까지만 쓴다."""
    return df.dt.map(market_up_days(df, n)).fillna(False).astype(bool)


def market_up_days(df: pd.DataFrame, n: int = 120) -> pd.Series:
    """market_up 의 날짜별 판정(날짜 → bool). df 는 dt·ret 열만 있으면 된다(유동성으로 좁힌 유니버스)."""
    idx = (1 + df.groupby("dt").ret.mean().fillna(0)).cumprod()
    return idx > idx.rolling(n).mean()


def osc_shrink_start(osc: pd.Series) -> pd.Series:
    """오실레이터 음봉이 **줄기 시작한 날** — 음수인 채로 전날보다 올라왔고, 전날은 그렇지 않았던 날.
    (음봉이 계속 줄어드는 동안은 첫날 하루만 잡는다. 양봉 전환일은 stoch_turn_up 이 따로 잡는다.)"""
    shrink = (osc < 0) & (osc > osc.shift(1))
    return (shrink & ~shrink.shift(1, fill_value=False)).fillna(False)


def osc_turn_up(osc: pd.Series) -> pd.Series:
    """오실레이터가 음봉에서 양봉으로 바뀐 날(osc 음 → 0 이상). 일봉의 '일봉양전'과 같은 정의."""
    return ((osc.shift(1) < 0) & (osc >= 0)).fillna(False)


def higher_osc(o: pd.DataFrame, freq: str, k: int = STOCH_K, dfast: int = STOCH_DFAST,
               dslow: int = STOCH_DSLOW) -> pd.DataFrame:
    """일봉 날짜마다 **그날까지의 봉으로 본** 상위 봉(freq: "W-FRI" 주봉 · "M" 월봉) 오실레이터.
      osc  = 이번 주(달)의 봉을 그날 종가까지로 만들어 계산한 값 — 그날 차트에 보이는 마지막 막대
      prev = 직전에 끝난 주(달)의 osc
    미래 참조가 없다(끝난 봉들 + 그날까지의 미완성 봉 하나). 주·월의 마지막 거래일에는 stoch_lines 로 계산한
    완성 봉 값과 같다(테스트로 고정). 식은 stoch_lines 를 한 봉만 이어 붙인 것이다."""
    key = o.index.to_period(freq)
    g = o.groupby(key)
    W = pd.DataFrame({"high": g.high.max(), "low": g.low.min(), "close": g.close.last()})
    done = stoch_lines(W.high, W.low, W.close, k, dfast, dslow)
    lowest, highest = W.low.rolling(k).min(), W.high.rolling(k).max()
    prev = lambda x: pd.Series(x.shift(1).reindex(key).to_numpy(), index=o.index)      # noqa: E731 — 직전 완성 봉의 값을 날짜에
    hh = np.maximum(prev(W.high.rolling(k - 1).max()), o.high.groupby(key).cummax()) if k > 1 else o.high.groupby(key).cummax()
    ll = np.minimum(prev(W.low.rolling(k - 1).min()), o.low.groupby(key).cummin()) if k > 1 else o.low.groupby(key).cummin()
    fastK = ((o.close - ll) / np.maximum(hh - ll, 1e-12) * 100).fillna(0)
    af, a_s = 2 / (dfast + 1), 2 / (dslow + 1)
    pf, ps = prev(done.fastD), prev(done.slowD)
    fastD = (af * fastK + (1 - af) * pf).where(pf.notna(), fastK)
    num = prev((W.close - lowest).rolling(dfast - 1).sum()) + (o.close - ll)
    den = prev((highest - lowest).rolling(dfast - 1).sum()) + (hh - ll)
    slowK = pd.Series(np.where(den != 0, num / np.maximum(den, 1e-12) * 100, 0), index=o.index)
    slowD = (a_s * slowK + (1 - a_s) * ps).where(ps.notna(), slowK)
    return pd.DataFrame({"osc": fastD - slowD, "prev": prev(done.osc)}, index=o.index)


def osc_growing(o: pd.DataFrame, freq: str) -> pd.Series:
    """상위 봉 오실레이터가 **양봉이고 아직 커지는 중**(고점을 찍기 전) — osc ≥ 0 이면서 직전 봉보다 크다.
    양봉이 직전 봉보다 작아지면 고점이 지난 것으로 본다(사용자 정의 2026-09-30 '양봉 추세 · 고점 직전')."""
    h = higher_osc(o, freq)
    return ((h.osc >= 0) & (h.osc > h.prev)).fillna(False)


OSC_SHRINK_N = 3            # '최대 음봉에서 3칸 줄었다' 의 칸 수 (사용자 정의 2026-09-30)


def osc_positive(osc: pd.Series) -> pd.Series:
    """오실레이터가 양봉인 봉(osc ≥ 0) — **상태**다(사건이 아니다). 경계는 양봉 전환(osc_turn_up)과 같게 0 을 양봉에 넣는다.
    화면은 '지금 양봉인 종목'을 고르는 데 쓰고(state_candidates), 양봉이 시작된 날은 양봉 전환 열이 알려 준다."""
    return (osc >= 0).fillna(False)


def osc_shrink_n(osc: pd.Series, n: int = OSC_SHRINK_N) -> pd.Series:
    """**최대 음봉에서 n 칸 줄어든 봉** — 공격적인 추세 전환 자리(사용자 정의 2026-09-30).
      · 이번 음봉 구간(마지막 양봉 뒤)에서 가장 깊은 봉이 n 봉 전이고
      · 그 뒤 n 봉이 한 번도 쉬지 않고 연달아 줄었으며(전 봉보다 위) · 아직 음봉이다.
    n 번째 봉 하루만 잡는다. 반등하다 다시 깊어져 새 최대 음봉이 생기면 거기서부터 다시 센다."""
    neg = osc < 0
    low = osc.where(neg).groupby((~neg).cumsum()).cummin()        # 이번 음봉 구간의 지금까지 최저
    up = neg & (osc > osc.shift(1))
    streak = up.groupby((~up).cumsum()).cumsum()                  # 연달아 줄어든 봉 수
    return (up & (streak == n) & (osc.shift(n) == low)).fillna(False)


def weekly_osc_frame(o: pd.DataFrame, rule: str = "W-FRI") -> pd.DataFrame:
    """주봉 오실레이터의 사건(up 양봉 전환 · shrink 음봉 축소 시작 · shrink3 최대 음봉에서 3칸 축소)과
    상태(pos 양봉)를 **일봉 날짜에** 찍어 돌려준다. 찍는 규칙은 weekly_osc_events 설명과 같다 —
    상태(pos)도 그 주의 마지막 거래일 하루에만 찍는다(주봉 한 개 = 한 줄)."""
    g = o[["high", "low", "close"]].resample(rule)
    w = pd.DataFrame({"high": g.high.max(), "low": g.low.min(), "close": g.close.last()}).dropna(subset=["close"])
    osc = stoch_lines(w.high, w.low, w.close).osc
    last_day = pd.Series(o.index, index=o.index).resample(rule).last().reindex(w.index)
    out = pd.DataFrame(False, index=o.index, columns=["up", "shrink", "pos", "shrink3"])
    for name, ev in (("up", osc_turn_up(osc)), ("shrink", osc_shrink_start(osc)),
                     ("pos", osc_positive(osc)), ("shrink3", osc_shrink_n(osc))):
        out.loc[last_day[ev.to_numpy()].to_numpy(), name] = True
    return out


def weekly_osc_events(o: pd.DataFrame, rule: str = "W-FRI") -> tuple[pd.Series, pd.Series]:
    """주봉 오실레이터의 (양봉 전환, 음봉 축소 시작)을 **일봉 날짜에** 찍어 돌려준다.
    주봉은 금요일 마감(차트의 주봉과 같은 규칙)으로 만들고, 사건은 그 주의 **마지막 거래일**에 찍는다 —
    주봉이 완성되는 날이므로 과거 구간에 미래 참조가 없다. 자료의 마지막 주가 아직 안 끝났으면 그때까지의
    봉으로 판정해 마지막 날에 찍는다(화면의 '이번 주' 표시용)."""
    w = weekly_osc_frame(o, rule)
    return w.up, w.shrink


def _first(cond: pd.Series, gap: int = 1) -> pd.Series:
    """cond 가 참인 날 중 직전 gap 거래일 안에 참이 없던 날만(연속 발생을 첫날 하나로)."""
    cond = cond.fillna(False).astype(bool)
    prior = cond.shift(1, fill_value=False).astype(int).rolling(gap, min_periods=1).max().astype(bool)
    return cond & ~prior


def sig_bb_break(o: pd.DataFrame, n: int = 20, k: float = 2.0, hill: int = 60) -> pd.Series:
    """볼린저 상한선 + 직전 언덕 돌파 — 식이형 「검정 점선(BB 20,2σ 상단) 돌파」 +
    「돌파는 직전 언덕(앞 고점, 꼬리 포함)을 넘어야 한다」. 직전 언덕 = 전날까지 hill 일 최고 고가.
    두 조건을 함께 처음 만족한 날."""
    c = o.close
    up = c.rolling(n).mean() + k * c.rolling(n).std(ddof=0)
    hill_hi = o.high.rolling(hill).max().shift(1)
    return _first((c > up) & (c > hill_hi))


def sig_ema_div(o: pd.DataFrame, conv: float = 0.05, within: int = 60) -> pd.Series:
    """장기 EMA 120/240/480 수렴 → 발산 — 「수렴 = 발산 준비」(식이형, 이평은 지수).
    수렴: 전날까지 within 일 안에 (최대EMA−최소EMA)/종가 ≤ conv 였던 적이 있음.
    발산: 오늘 EMA120 > 240 > 480 정배열 + 종가가 셋 위. 그 상태가 처음 된 날."""
    c = o.close
    e = {n: c.ewm(span=n, adjust=False, min_periods=n).mean() for n in (120, 240, 480)}
    hi = np.maximum(np.maximum(e[120], e[240]), e[480])
    lo = np.minimum(np.minimum(e[120], e[240]), e[480])
    was_conv = ((hi - lo) / c).rolling(within).min().shift(1) <= conv
    div = (e[120] > e[240]) & (e[240] > e[480]) & (c > hi)
    return _first(div, gap=1) & was_conv.fillna(False)


def sig_kijun_pull(o: pd.DataFrame, tol: float = 0.02) -> pd.Series:
    """기준선(일목 26) 눌림 후 재상승 — 식이형 MTS 매매(기준선 위 눌림 → 재상승 자리).
      기준선 = (26일 최고 고가 + 최저 저가)/2, 5일 전보다 낮지 않음(꺾이지 않은 기준선)
      위에 있었음: 전날까지 20일 중 70% 이상 종가 > 기준선
      눌림: 전날까지 3일 안에 저가 ≤ 기준선×(1+tol) 이면서 종가는 기준선 위
      재상승: 오늘 종가 > 전일 고가 · 종가 > 기준선.  5거래일 안 재발생은 첫날만."""
    c, h, l = o.close, o.high, o.low
    kj = (h.rolling(26).max() + l.rolling(26).min()) / 2
    rising = kj >= kj.shift(5)
    above = (c > kj).astype(float).shift(1).rolling(20).mean() >= 0.7
    touch = ((l <= kj * (1 + tol)) & (c >= kj)).astype(float)
    pulled = touch.shift(1).rolling(3).max() == 1
    return _first(rising & above & pulled & (c > h.shift(1)) & (c > kj), gap=5)


# ── 식이형 전사본(차트 그림 1,300편, 2026-10-04 전사 완료)에서 뽑은 신호 — 저자 문장은 README 「전사본에서 뽑은 검색식」 절

def _reclaim(o: pd.DataFrame, ev: pd.Series, level: pd.Series, window: int) -> pd.Series:
    """사건(ev) 뒤 1~window 거래일 안에 종가가 그 사건의 기준가(level)를 **처음** 넘어 닫힌 날.
    새 사건이 나오면 기준이 그것으로 바뀐다(가장 최근 사건 하나만 본다 — sig_sik_pullback 과 같은 약속)."""
    ev = ev.fillna(False).astype(bool)
    i = pd.Series(np.arange(len(o), dtype=float), index=o.index)
    eidx = i.where(ev).ffill()
    age = i - eidx
    lv = level.where(ev).ffill()
    above = ((age >= 1) & (age <= window) & (o.close > lv)).fillna(False)
    # 사건 뒤 window 안에서 '처음' — 넘은 날의 누적 수가 1 인 날(window 밖에서 넘은 날은 세지 않는다)
    return above & (above.astype(int).groupby(eidx).cumsum() == 1)


BB40_N, BB40_K, BB40_QUIET = 40, 2.0, 60


def bb40_upper(o: pd.DataFrame, n: int = BB40_N, k: float = BB40_K) -> pd.Series:
    """저자 화면의 「상한선」 — 대표가 (고+저+종)/3 의 n일 단순평균 + k×모표준편차.
    전사에서 툴팁 값과 소수 셋째 자리까지 맞춘 설정(2026 화면 40일 · 저자 본문 「수치는 40/2」)."""
    tp = (o.high + o.low + o.close) / 3
    return tp.rolling(n).mean() + k * tp.rolling(n).std(ddof=0)


def sig_bb40_break(o: pd.DataFrame, quiet: int = BB40_QUIET) -> pd.Series:
    """상한선(대표가 40일·2σ) 첫 돌파 — 「볼린저밴드 상한선은 저항 … 뚫어 올린다는 건 누군가의 힘이 필요」,
    「하락 후 첫 번째 볼린저밴드 상한선 돌파」. 양봉으로 종가가 상한선 위에서 닫힌 날 중,
    전날까지 quiet 거래일 동안 그런 날이 없던 날."""
    return _first((o.close > bb40_upper(o)) & (o.close > o.open), gap=quiet)


GAPNEG_GAP, GAPNEG_VMULT, GAPNEG_WINDOW = 0.03, 2.0, 60


def sig_gap_neg_reclaim(o: pd.DataFrame, gap: float = GAPNEG_GAP, vmult: float = GAPNEG_VMULT,
                        window: int = GAPNEG_WINDOW) -> pd.Series:
    """갭상승 음봉의 시가 돌파 — 「음봉 매집이란 갭 상승 후 당일 음봉」, 「돌파 타점은 갭 상승 음봉 시가」,
    「시가를 잡지 않으면 주가는 상승하지 않습니다」.
      갭상승 음봉 : 시가 ≥ 전일 종가 × (1+gap) · 종가 < 시가 · 거래량 ≥ 전날까지 20일 평균 × vmult
      신호       : 그 뒤 1~window 거래일 안에 종가가 그 음봉의 시가를 처음 넘은 날"""
    c, op, v = o.close, o.open, o.volume
    ev = (op >= c.shift(1) * (1 + gap)) & (c < op) & (v >= vmult * v.rolling(20).mean().shift(1))
    return _reclaim(o, ev, op, window)


TAIL_MIN, TAIL_VMULT, TAIL_WINDOW = 0.05, 2.0, 60


def sig_tail_reclaim(o: pd.DataFrame, tail: float = TAIL_MIN, vmult: float = TAIL_VMULT,
                     window: int = TAIL_WINDOW) -> pd.Series:
    """긴 위꼬리의 고가를 종가로 장악 — 「위 꼬리를 달았는데 그 위 꼬리를 잡으러 간다? 이건 절대적 세력」,
    「위 꼬리를 다 잡았습니다」.
      위꼬리 봉 : (고가 − 몸통 상단) ÷ 전일 종가 ≥ tail · 거래량 ≥ 전날까지 20일 평균 × vmult · 고가가 20일 최고
      신호     : 그 뒤 1~window 거래일 안에 종가가 그 봉의 고가를 처음 넘은 날"""
    c, op, h, v = o.close, o.open, o.high, o.volume
    body_top = np.maximum(op, c)
    ev = (((h - body_top) / c.shift(1) >= tail) & (v >= vmult * v.rolling(20).mean().shift(1))
          & (h >= h.rolling(20).max()))
    return _reclaim(o, ev, h, window)


DROP_MIN, DROP_WINDOW = -0.07, 20


def sig_drop_recover(o: pd.DataFrame, drop: float = DROP_MIN, window: int = DROP_WINDOW) -> pd.Series:
    """장대음봉의 하락폭 회복 — 「하락폭을 회복했다면 100% 세력」, 「하락을 했던 가격대를 모두 회복한 그림
    (개인이 할 수 없는 그림)」.
      장대음봉 : 전일 종가 대비 drop 이하 · 음봉(종가 < 시가)
      신호     : 그 뒤 1~window 거래일 안에 종가가 '하락 전 가격'(그 봉의 시가와 전일 종가 중 높은 값)을 처음 넘은 날"""
    c, op = o.close, o.open
    ev = (c / c.shift(1) - 1 <= drop) & (c < op)
    return _reclaim(o, ev, np.maximum(op, c.shift(1)), window)


SEIZE_RISE, SEIZE_VMULT = 0.05, 2.0


def sig_ema_seize(o: pd.DataFrame, rise: float = SEIZE_RISE, vmult: float = SEIZE_VMULT) -> pd.Series:
    """장대양봉 하나로 장기선(EMA120·240) 장악 — 「장대양봉 나타났을 때 120일 240을 잡는다면 더 강력함의 신호」,
    「이평선에 수차례 저항 → 종가로 장악」(이평은 지수 — 전사에서 EMA 로 확인).
      전일 종가는 두 선 중 높은 쪽 아래 · 오늘 양봉(+rise 이상, 거래량 ≥ 전날까지 20일 평균 × vmult)의 종가가
      EMA120·EMA240 둘 다 위."""
    c, op, v = o.close, o.open, o.volume
    e120 = c.ewm(span=120, adjust=False, min_periods=120).mean()
    e240 = c.ewm(span=240, adjust=False, min_periods=240).mean()
    top = np.maximum(e120, e240)
    return ((c.shift(1) < top.shift(1)) & (c > top) & (c > op) & (c / c.shift(1) - 1 >= rise)
            & (v >= vmult * v.rolling(20).mean().shift(1))).fillna(False)


# ── 스토캐스틱 과매도 + 거래량 (2026-10-07, indsetup.py 전 기간 탐색 1등 — 사용자 "검색식으로 진행")
OSD_FD, OSD_VMULT, OSD_VWAP_N = 20.0, 1.5, 120
NO_RATES_KINDS = {"osd_vol", "accum", "stairs_ready"}                  # WIN_RATES(winners.py 사건표) 밖의 검색식 — 성적은 SIGNAL_INFO 에 indsetup 수치로 적는다


def vwap_n(o: pd.DataFrame, n: int = OSD_VWAP_N) -> pd.Series:
    """VWAP 기간 n — n봉 (종가×거래량) 합 ÷ 거래량 합(차트 보조지표 VWAP 과 같은 식)."""
    pv = (o.close * o.volume).rolling(n).sum()
    return pv / o.volume.rolling(n).sum().replace(0, np.nan)


def sig_osd_vol(o: pd.DataFrame, fd_max: float = OSD_FD, vmult: float = OSD_VMULT) -> pd.Series:
    """「스토캐스틱 과매도 · 거래량 1.5배 · VWAP > MA60」 — 스토캐스틱(12·26·9) fastD < 20 인 봉에 거래량이 전날까지 20일 평균의 1.5배 이상 실리고,
    VWAP(120) 이 60일선 위인 날. indsetup.py 전 기간(2007~, 상폐 포함): 9,394건 · 2주~1달 오른 비율 53~54%(기준 45%) · 시장초과 +1.2~1.6p · 최고 지평 6주."""
    fd = stoch_lines(o.high, o.low, o.close).fastD
    ma60 = o["ma60"] if "ma60" in o else o.close.rolling(60).mean()
    vr = o.volume / o.volume.rolling(20).mean().shift(1)
    return ((fd < fd_max) & (vr >= vmult) & (vwap_n(o) > ma60)).fillna(False).astype(bool)


# ── 사용자 그림 검색식 (2026-10-07, expand.py·spike.py 검정 — 둘 다 평균은 시장 아래. 사용자 요청으로 '(주의)' 등급으로 화면에 둔다)
PIV_W = 10                                   # 피벗 = 앞뒤 10거래일 극값(10일 뒤 확정)
WC_WIN, WC_RISE, WC_AMP, WC_DROP, WC_STATE, WC_BAND = 250, 0.20, 1.3, 80, 120, 0.15
SA_RUN, SA_RUN_MIN, SA_V, SA_HALF, SA_DROP, SA_STATE, SA_BASE, SA_WICK, SA_ACC_V, SA_ACC_WIN, SA_ACC_N = 40, 0.40, 3.0, 0.55, 120, 250, 1.35, 0.03, 3.0, 40, 2
PATTERN_KINDS = {"wave_collapse", "spike_accum", "stairs_break", "crow_star"}
# 사용자 그림 3(2026-10-15, 에스엠벡셀 010580): 횡보·상승 중 거래량 터진 매집봉이 1년에 여러 번 → 끌다가 1년 고점 종가 돌파(분출)
ST_W, ST_MED, ST_VMULT, ST_PUSH, ST_GAP, ST_NCL = 250, 120, 10.0, 0.10, 20, 3


# ── 매집 의심 (2026-10-15, 사용자 "1년 이내 세력 매집 의심 종목" → "검색식으로 추가") — 국내 수급론 + Wyckoff 흡수 단계를 숫자로.
#   최근 120거래일 창: ① 세력(=−(개인+내외국인)) 순매수 ≥ 거래량 5% · 개인 순매도 ≥ 3% ② 고저폭 ≤ 60% · 120일 변화 −15~+25%(횡보·아직 안 튐)
#   ③ 세력 분산비율 ≥ 0.9 · 개인 분산비율 ≤ 0.3(안 팔았다) ④ 하락일 평균 거래량 ≤ 상승일의 0.9배 ⑤ 종가가 세력 평단(순매수일 가중) 위 0~15%.
#   검정(생존 종목·월 1회 표본, 2021-03~): 2024~ 3달 +2.2%p·6달 +5.6%p(t2.5), 2021~23 은 유의하지 않음. 2배 확률은 기준보다 낮다(급등 신호 아님).
ACC_W, ACC_FNET, ACC_INET, ACC_RNG, ACC_CHG, ACC_FDISP, ACC_IDISP, ACC_DVR, ACC_PREM = 120, 0.05, -0.03, 1.6, (-0.15, 0.25), 0.9, 0.3, 0.9, (0.0, 0.15)


def accum_state(px: pd.DataFrame, fl: pd.DataFrame | None) -> pd.Series:
    """일봉(px: high·low·close·volume)과 수급(read_flow 결과, natfor 포함)으로 매집 의심 상태(bool, index=px 와 수급이 둘 다 있는 날)."""
    if fl is None or "natfor" not in fl or len(fl) <= ACC_W:
        return pd.Series(dtype=bool)
    x = px[["high", "low", "close", "volume"]].join(fl[["vol", "ind", "natfor"]], how="inner")
    if len(x) <= ACC_W:
        return pd.Series(False, index=x.index)
    c, v = x.close, x.volume
    force = -(x.ind + x.natfor)
    rv = x.vol.rolling(ACC_W).sum().replace(0, np.nan)
    fnet, inet = force.rolling(ACC_W).sum() / rv, x.ind.rolling(ACC_W).sum() / rv
    rng = x.high.rolling(ACC_W).max() / x.low.rolling(ACC_W).min()
    chg = c / c.shift(ACC_W) - 1
    up, dn = c > c.shift(1), c < c.shift(1)
    dvr = v.where(dn).rolling(ACC_W, min_periods=20).mean() / v.where(up).rolling(ACC_W, min_periods=20).mean()
    fb = force.clip(lower=0)
    prem = c / ((fb * c).rolling(ACC_W).sum() / fb.rolling(ACC_W).sum()) - 1
    fd = pd.Series(disp(force.to_numpy(dtype=float)), index=x.index)
    idp = pd.Series(disp(x.ind.to_numpy(dtype=float)), index=x.index)
    s = ((fnet >= ACC_FNET) & (inet <= ACC_INET) & (rng <= ACC_RNG) & chg.between(*ACC_CHG)
         & (fd >= ACC_FDISP) & (idp <= ACC_IDISP) & (dvr <= ACC_DVR) & prem.between(*ACC_PREM))
    return s.fillna(False).astype(bool)


def sig_accum(code: str, o: pd.DataFrame) -> pd.Series:
    """봉(o)에 맞춘 매집 의심 상태. 일봉이 아니면(주봉·60분봉) 저장 일봉으로 계산해 그 봉의 날짜에 잇는다.
    수급이 시세보다 늦은 날(오늘 봉)은 직전 값을 IND_DISP_FFILL 거래일까지 이어 쓴다(flow_cols 와 같은 규칙)."""
    idx = pd.DatetimeIndex(o.index)
    days = idx.normalize()
    daily = len(idx) > 1 and (days == idx).all() and np.median(np.diff(idx.asi8)) <= 4 * 86_400 * 10**9
    px = o if daily else read_price(code)
    if px is None:
        return pd.Series(False, index=o.index)
    st = accum_state(px, read_flow(code, px.close))
    if not len(st):
        return pd.Series(False, index=o.index)
    key = idx if daily else days
    both = st.astype(float).reindex(st.index.union(key.unique())).ffill(limit=IND_DISP_FFILL)   # 60분봉은 날짜가 겹친다 — 겹친 채 합치면 reindex 가 죽는다(2026-10-19)
    return pd.Series(both.reindex(key).fillna(0).to_numpy() > 0, index=o.index)


def _pivot_idx(h: pd.Series, l: pd.Series, w: int = PIV_W) -> tuple[np.ndarray, np.ndarray]:
    ph = (h == h.rolling(2 * w + 1, center=True).max()).to_numpy()
    pl = (l == l.rolling(2 * w + 1, center=True).min()).to_numpy()
    return np.flatnonzero(ph), np.flatnonzero(pl)


def wave_collapse_events(o: pd.DataFrame) -> list[tuple[int, float]]:
    """확대 파동 뒤 붕괴(expand.py 2판): 마지막 고점 P3 앞 250일에 확정 고점 2개↑ · P3 가 창의 최고 · 마지막 파동 폭 ≥ 앞 파동 중앙값×1.3
    · H3/바닥 F − 1 ≥ 20% → P3 확정 뒤 80일 안에 종가 < F 가 된 첫날 e. 돌려주는 값 = [(e, F), …]."""
    h, l, c = (o[k].to_numpy(dtype="float64") for k in ("high", "low", "close"))
    n = len(o)
    PH, PL = _pivot_idx(o.high, o.low)
    out, last_e = [], -10**9
    for k in range(2, len(PH)):
        p3 = PH[k]
        prior = PH[(PH < p3) & (PH >= p3 - WC_WIN)]
        if len(prior) < 2 or h[p3] <= h[prior].max():
            continue
        lows_in = PL[(PL > prior[0]) & (PL < p3)]
        if len(lows_in) == 0:
            continue
        F = l[lows_in].min()
        if h[p3] / F - 1 < WC_RISE:
            continue
        amps = [h[b_] - l[a_:b_ + 1].min() for a_, b_ in zip(prior[:-1], prior[1:])]
        last_amp = h[p3] - l[prior[-1]:p3 + 1].min()
        if not amps or last_amp < WC_AMP * float(np.median(amps)):
            continue
        confirm = p3 + PIV_W
        if confirm >= n:
            continue
        seg = c[confirm + 1: min(confirm + 1 + WC_DROP, n)]
        below = np.flatnonzero(seg < F)
        if len(below) == 0:
            continue
        e = confirm + 1 + below[0]
        if e - last_e < WC_STATE:
            continue
        last_e = e
        out.append((e, F))
    return out


def sig_wave_collapse(o: pd.DataFrame) -> pd.Series:
    """「상승 파동이 점점 커지다가 갑자기 붕괴·횡보」(사용자 그림 2) — 붕괴일 뒤 120거래일 안에서 20일 폭 ≤15% 횡보이고
    일봉 오실레이터가 양봉인 날(사용자가 말한 매수 시점). 상태 검색식(매일 날 수 있다). 검정(expand.py): IS +0.8 · OOS −0.8 로 효과 없음."""
    n = len(o)
    state = np.zeros(n, dtype=bool)
    for e, _ in wave_collapse_events(o):
        state[e: min(e + WC_STATE + 1, n)] = True
    band = o.high.rolling(20).max().shift(1) / o.low.rolling(20).min().shift(1) - 1
    osc = o["stoch_osc"] if "stoch_osc" in o else stoch_lines(o.high, o.low, o.close).osc
    return (pd.Series(state, index=o.index) & (band <= WC_BAND) & (osc > 0)).fillna(False).astype(bool)


def _stairs_parts(o: pd.DataFrame) -> dict:
    """계단식 매집(사용자 그림 3) 공통 재료. 창 값(최고 종가·매집봉 묶음 수·1년 저가)은 전날까지로 센다."""
    c, h = o.close, o.high
    amt = c * o.volume
    med = amt.rolling(ST_MED, min_periods=ST_MED // 2).median().shift(1)
    spike = ((amt >= ST_VMULT * med) & (h >= c.shift(1) * (1 + ST_PUSH)) & (med > 0)).fillna(False)
    recent = spike.astype(int).rolling(ST_GAP, min_periods=1).sum().shift(1).fillna(0)
    ncl = (spike & (recent == 0)).astype(int).rolling(ST_W, min_periods=ST_W).sum().shift(1)
    maxc = c.rolling(ST_W, min_periods=ST_W).max().shift(1)
    minl = o.low.rolling(ST_W, min_periods=ST_W).min().shift(1)
    camp = (ncl >= ST_NCL) & (minl >= minl.shift(ST_W))          # 1년 매집봉 묶음 3개↑ · 바닥 유지
    return {"c": c, "amt": amt, "med": med, "spike": spike, "maxc": maxc, "camp": camp}


def sig_stairs_break(o: pd.DataFrame) -> pd.Series:
    """「계단식 매집 뒤 분출」(사용자 그림 3) — 오늘 종가가 직전 250거래일 최고 종가를 넘고 오늘이 매집봉이며,
    직전 250거래일에 매집봉 묶음(20거래일 넘게 떨어진 첫 봉)이 3개 이상, 그 1년 저가가 그 전 1년 저가보다 낮지 않다(바닥 유지).
    매집봉 = 거래대금(종가×거래량)이 직전 120일 중앙값의 10배 이상이고 고가가 전일 종가보다 10% 이상.
    검정(stairs, marcap 상폐 포함 2007~): 1년 고점 돌파 전체보다 나빴다 — 120일 시장초과 IS −1.7%p · OOS −4.5%p(t −2.7)."""
    P = _stairs_parts(o)
    return ((P["c"] > P["maxc"]) & P["spike"] & P["camp"]).fillna(False).astype(bool)


ST_NEAR, ST_QUIET_N, ST_QUIET_X, ST_RNG_N, ST_RNG = 0.85, 10, 2.0, 20, 1.15


def sig_stairs_ready(o: pd.DataFrame) -> pd.Series:
    """「계단식 매집 · 분출 전 수렴」(사용자 그림 3 의 매수 자리, 2026-10-15) — 계단 매집(묶음 3개↑·바닥 유지) 종목이
    아직 1년 최고 종가를 넘지 않았고 그 85% 이상에 있으며, 최근 10일(오늘 포함) 매집봉이 없고 평균 거래대금이 120일 중앙값의 2배 이하(거래 마름),
    20일 고가/저가 ≤ 1.15(수렴)인 날. 상태 검색식. 검정(stairs_pre, 상폐 포함 2007~): 20일 안 분출 12~15%(아무 날 1.2%),
    60일 시장초과 IS +4.1%p(t 5.4) · OOS +3.3%p(t 3.7), 120일 +5.3 · +4.6%p."""
    P = _stairs_parts(o)
    c = P["c"]
    quiet = (P["amt"].rolling(ST_QUIET_N).mean() <= ST_QUIET_X * P["med"]) & (P["spike"].astype(int).rolling(ST_QUIET_N, min_periods=1).sum() == 0)
    tight = o.high.rolling(ST_RNG_N).max() / o.low.rolling(ST_RNG_N).min() <= ST_RNG
    return (P["camp"] & (c <= P["maxc"]) & (c >= ST_NEAR * P["maxc"]) & quiet & tight).fillna(False).astype(bool)


# 사용자 그림 4(2026-10-16, 듀켐바이오 176750): 흑삼병 뒤 짧은 양봉·도지(십자·샛별·짧은 꽉 찬 양봉) + 주봉·월봉 오실레이터 양봉 또는 음봉 축소
CS_CROW_SUM, CS_SHORT, CS_SHORT_REL, CS_DOJI, CS_ALIGN_N = 0.03, 0.03, 0.6, 0.003, 5


def sig_crow_star(o: pd.DataFrame) -> pd.Series:
    """「흑삼병 뒤 짧은 양봉·도지」(사용자 그림 4) — 전날까지 세 봉 연속 음봉(종가가 연달아 낮아지고 세 몸통 합 ≥ 3%) 뒤
    오늘이 짧은 봉(몸통 ≤ 전일 종가 3% · ≤ 흑삼병 가장 큰 몸통의 60%)이며 양봉이거나 도지(몸통 ≤ 0.3%)이고,
    그날까지의 주봉·월봉 오실레이터가 양봉(≥0)이거나 직전 봉보다 올라온(음봉 축소) 날.
    그리고 흑삼병 전날부터 오늘까지 5일 내내 5일선 > 20일선 > 60일선 정배열(사용자 추가 2026-10-16 — 정배열 상태에서 나온 흑삼병).
    문턱은 듀켐바이오 10/14(흑삼병 −1.7·−5.2·−0.6% 뒤 +2.5% 양봉, 10/7~10/14 정배열)가 잡히도록 맞췄다.
    검정(crow.py, marcap 상폐 포함 2007~): README 「흑삼병 뒤 짧은 양봉·도지」 — 시장 아래."""
    c, op = o.close, o.open
    body = (c - op).abs() / c.shift(1)
    bear = c < op
    crow = bear.shift(1, fill_value=False) & bear.shift(2, fill_value=False) & bear.shift(3, fill_value=False) \
        & (c.shift(1) < c.shift(2)) & (c.shift(2) < c.shift(3))
    b3 = pd.concat([body.shift(1), body.shift(2), body.shift(3)], axis=1)
    short = (body <= CS_SHORT) & (body <= CS_SHORT_REL * b3.max(axis=1)) & ((c >= op) | (body <= CS_DOJI))
    m5, m20, m60 = (c.rolling(n).mean() for n in (5, 20, 60))
    align = ((m5 > m20) & (m20 > m60)).astype(int).rolling(CS_ALIGN_N).min() == 1      # 흑삼병 전날(t-4)~오늘(t) 내내 정배열
    ev = (crow & (b3.sum(axis=1) >= CS_CROW_SUM) & short & align).fillna(False)
    if not ev.any():
        return ev.astype(bool)
    w, m = higher_osc(o, "W-FRI"), higher_osc(o, "M")
    ok = lambda h: (h.osc >= 0) | (h.osc > h.prev)      # noqa: E731 — 양봉이거나 음봉이 줄어드는 중
    return (ev & ok(w) & ok(m)).fillna(False).astype(bool)


def spike_accum_events(o: pd.DataFrame) -> list[tuple[int, float]]:
    """급등 시도 → 반토막(spike.py): 피벗 고점 P 가 앞 40일 최저 대비 +40%↑ · P 앞뒤 10일 거래량 ≥ 앞 60일 평균×3 → P 확정 뒤 120일 안
    종가 ≤ H[P]×0.55 가 된 첫날 e. 돌려주는 값 = [(e, H), …]."""
    h, l, c, v = (o[k].to_numpy(dtype="float64") for k in ("high", "low", "close", "volume"))
    n = len(o)
    PH, _ = _pivot_idx(o.high, o.low)
    v60 = pd.Series(v).rolling(60).mean().to_numpy()
    out, last_e = [], -10**9
    for p in PH:
        if p < SA_RUN + 60 + PIV_W:
            continue
        if h[p] / l[p - SA_RUN:p].min() - 1 < SA_RUN_MIN:
            continue
        pre = v60[p - PIV_W - 1]
        if not np.isfinite(pre) or pre <= 0 or v[p - PIV_W:p + PIV_W + 1].mean() < SA_V * pre:
            continue
        confirm = p + PIV_W
        seg = c[confirm + 1: min(confirm + 1 + SA_DROP, n)]
        below = np.flatnonzero(seg <= SA_HALF * h[p])
        if len(below) == 0:
            continue
        e = confirm + 1 + below[0]
        if e - last_e < SA_STATE:
            continue
        last_e = e
        out.append((e, h[p]))
    return out


def sig_spike_accum(o: pd.DataFrame) -> pd.Series:
    """「급등 시도 → 반토막 → 바닥에서 거래량 터진 윗꼬리 양봉 매집」(사용자 그림 3) — 반토막일 뒤 250거래일 안, 종가가 그 뒤 최저가의 1.35배
    이내(바닥권), 최근 40일에 매집 봉(양봉·윗꼬리 ≥3%·거래량 ≥20일 평균×3) 2개↑, 20일선이 20일 전보다 낮지 않은 날. 상태 검색식."""
    n = len(o)
    c, v, h, op = o.close, o.volume, o.high, o.open
    acc = (c > op) & ((h - np.maximum(op, c)) / c >= SA_WICK) & (v >= SA_ACC_V * v.rolling(20).mean().shift(1))
    acc40 = acc.astype(int).rolling(SA_ACC_WIN).sum()
    ma20 = c.rolling(20).mean()
    gentle = ma20 >= ma20.shift(20)
    lo = o.low.to_numpy(dtype="float64")
    cl = c.to_numpy(dtype="float64")
    base = np.zeros(n, dtype=bool)
    for e, _ in spike_accum_events(o):
        t = min(e + SA_STATE + 1, n)
        base[e:t] = cl[e:t] <= SA_BASE * np.minimum.accumulate(lo[e:t])
    return (pd.Series(base, index=o.index) & (acc40 >= SA_ACC_N) & gentle).fillna(False).astype(bool)


# 검색식 이름 = **무엇을 찾는지(로직)**. 출처 별명(카페주력·알까기·일봉양전·식이형·조합 1위·승률형)은 이름에서 빼고
# 화면 설명(flowapp.SIGNAL_INFO)에만 남긴다 — 사용자 요청 2026-10-02. 순서 = 화면 분류(flowapp.SIGNAL_GROUPS) 순.
# 성적이 나빴던 둘은 이름에 '주의'를 둔다 — 종목 화면 표·비교표에는 등급 배지가 없어 매수 신호로 읽힌다.
SIGNALS = {
    # 이름 규칙(2026-10-06 사용자 "이름들을 전체적으로 손봐줘"): 핵심 조건 · 부가 조건 을 「·」로 잇는다. 봉 단위 말(일봉)은 쓰지 않는다 —
    # 화면의 「봉 단위」 단추가 정한다(60분봉 패널이면 '오실레이터' = 60분봉 오실레이터). 「주봉」은 상위 봉이라 그대로 적는다.
    # 순서 = 화면 분류(flowapp.SIGNAL_GROUPS) 순. 코드 키(kind)는 바꾸지 않는다.
    # ── 신고가 돌파
    "저변동 신고가 · 상승률 상위 · 52주 고가권": "win_combo",                       # 옛 이름 승률형 (OOS 2달 승률 53%·반년 57%)
    "저변동 신고가 · 상승국면 · 오실레이터 호전": "best_combo",                      # 옛 이름 조합 1위
    "저변동 신고가": "lowvol_breakout",
    "저변동 신고가 · 상승률 상위 · 52주 고가권 · 20일선 근접": "win_tight",         # combo_win.py 2026-10-04
    "상한선(대표가 40) 첫 돌파 · 52주 고가권 · 상승국면": "bb40_combo",              # 식이형 전사본 2026-10-04
    "볼린저 상단 · 직전 고점 동시 돌파": "bb_break",                                # 식이형
    # ── 이동평균선
    "장기 이평(120·240·480) 수렴 뒤 정배열": "ema_div",                              # 식이형
    "단기선(3·5)의 20일선 상향 돌파": "gc",                                          # 골든크로스
    # ── 눌림목 · 반등
    "기준선(26) 눌림 뒤 전일 고가 돌파": "kijun_pull",                               # 식이형
    "상승 뒤 얕은 눌림 · 20일선 회복": "algkkagi",                                   # 카페 '알까기'
    "하락 뒤 좁은 횡보 · 20일선 첫 접촉": "cafe_juryeok",                            # 카페 '카페주력'
    "장대양봉 몸통 중간 눌림 (주의)": "sik_pullback",                                # 식이형 기준봉 눌림 — 19년 검정 약세
    "스토캐스틱 과매도 · 거래량 1.5배 · VWAP > MA60": "osd_vol",                     # indsetup.py 전 기간 1등 (2026-10-07)
    # ── 재진입(reclaim.py 2026-10-02): 신호 뒤 신호가 아래로 눌렸다가 처음 되찾은 날. 신호일 매수보다 낫지 않다 — 놓친 신호의 재진입 자리
    "저변동 신고가 · 상승률 상위 · 52주 고가권 — 재진입(눌림 뒤 회복)": "reclaim_win",
    "저변동 신고가 · 상승국면 · 오실레이터 호전 — 재진입(눌림 뒤 회복)": "reclaim_best",
    "저변동 신고가 — 재진입(눌림 뒤 회복)": "reclaim_lowvol",
    # ── 거래대금 · 수급
    "바닥 횡보 중 거래대금 폭발 (주의)": "bottom_spike",                              # 2026-09-29 검정 약세
    "매집 의심 · 세력 순매수 · 횡보 · 미분산 · 평단 근처": "accum",                    # 2026-10-15 (상태 검색식)
    # ── 사용자 그림(2026-10-07) — 검정에서 평균이 시장 아래였다('주의'). 모양이 맞는 종목을 보는 용도
    "확대 파동 뒤 붕괴 · 바닥 횡보 · 오실레이터 양봉 (주의)": "wave_collapse",
    "급등 뒤 반토막 · 바닥 매집 봉(윗꼬리·거래량) (주의)": "spike_accum",
    "계단식 매집봉 반복 · 분출 전 수렴(고점 근처·거래 마름)": "stairs_ready",            # 사용자 그림 3 의 매수 자리 (2026-10-15)
    "계단식 매집봉 반복 뒤 1년 고점 돌파 (주의)": "stairs_break",                      # 사용자 그림 3 (2026-10-15)
    "정배열 속 흑삼병 뒤 짧은 양봉·도지 · 주봉·월봉 오실레이터 양봉/음봉 축소 (주의)": "crow_star",   # 사용자 그림 4 (2026-10-16)
    # ── 오실레이터(스토캐스틱 12·26·9 의 fastD − slowD 막대) · 이 봉
    "오실레이터 음봉→양봉 전환": "stoch_turn_up",                                     # 카페 '일봉양전'
    "오실레이터 양봉 상태": "osc_pos_d",
    "오실레이터 음봉 축소 시작": "osc_shrink_d",
    "오실레이터 최대 음봉 뒤 3칸 축소": "osc_shrink3_d",
    "월·주봉 양봉 확대 중 · 오실레이터 양봉 전환": "osc_mtf",
    # ── 오실레이터 · 주봉(상위 봉 — 어느 봉 단위에서든 그 주의 봉으로 묶어 계산)
    "주봉 오실레이터 음봉→양봉 전환": "osc_up_w",
    "주봉 오실레이터 양봉 상태": "osc_pos_w",
    "주봉 오실레이터 음봉 축소 시작": "osc_shrink_w",
    "주봉 오실레이터 최대 음봉 뒤 3칸 축소": "osc_shrink3_w",
    "주봉 오실레이터 음봉 축소 · 저변동 · 이평 정배열": "oscw_trend",                # combo_win.py 2026-10-04
}
RECLAIM = {"reclaim_win": "win_combo", "reclaim_best": "best_combo", "reclaim_lowvol": "lowvol_breakout"}   # 재진입 검색식 → 바탕 검색식
RECLAIM_WINDOW, RECLAIM_DIP, RECLAIM_VMULT = 60, 5.0, 0.0   # 신호 뒤 60거래일 안 · 종가가 신호가보다 5%↓ 눌림 · 거래량 조건 없음(reclaim.py 의 기본 줄)
WIN_TIGHT_GAP = 0.05       # win_tight: 종가가 20일선 위로 5% 넘게 벌어지지 않았다(막 벌어진 급등 자리가 아니다)
OSC_STATE = {"osc_pos_d": "stoch_turn_up", "osc_pos_w": "osc_up_w"}
REGIME_KINDS = {"best_combo", "bb40_combo"}          # 시장 상승국면에서만 신호가 나는 검색식 — 하락국면 0건은 정상(화면이 이유를 적는다)
NO_SURGE_EXCLUDE = {"bottom_spike", "best_combo", "win_combo", "win_tight", "bb40_combo", *OSC_STATE, *RECLAIM, "wave_collapse", "spike_accum", "stairs_break", "stairs_ready", "crow_star", "osd_vol", "accum"}   # osd_vol 은 거래량 조건이 핵심 — 검정도 급등일 제외 없이 했다   # 상태 검색식: 오늘이 급등일이라고 '지금 양봉'에서 빠지면 안 된다 ·   # best_combo 는 급등일 제외 없이 검정했다 — 같은 식으로 보여준다   # 거래대금 폭발일은 거의 다 급등일이다 — 급등 제외를 걸면 신호가 사라진다
MARCAP = ROOT / "raw" / "marcap"
BASE_DAYS, BASE_BAND, BASE_TURNOVER = 20, 0.30, 0.30   # 52주 신저가 후 20일↑ · 폭 30% 이내 · 거래대금/시총 30%↑


# ────────────────────────────── 패널

def universe() -> pd.DataFrame:
    return pd.read_csv(META / "universe.csv", dtype={"Code": str})


# 봉 단위(사용자 요청 2026-10-06 "검색식을 60분봉으로도"): d 일봉(저장 시세) · w 주봉(일봉을 금요일 마감으로 합침) · h 60분봉(raw/min5 5분봉을 합침, 약 1년).
TFS = {"d": "일봉", "w": "주봉", "h": "60분봉"}
TF_BARS_PER_DAY = {"d": 1.0, "w": 0.2, "h": 7.0}     # 하루에 봉 몇 개 — 거래대금 하한(20봉 평균)을 '하루치'로 맞추는 데 쓴다
TF_MIN_ROWS = {"d": PANEL_MIN_ROWS if "PANEL_MIN_ROWS" in globals() else 300, "w": 120, "h": 300}
MIN5 = ROOT / "raw" / "min5"
SESSION = ("0900", "1530")                            # 정규장(5분봉 시작 시각 기준 09:00 ≤ t < 15:30)


def _fingerprint(tf: str = "d") -> str:
    """폴더 상태의 지문. 파일을 새로 받거나 기존 파일을 갱신하면 값이 바뀌어
    캐시가 자동 무효화되고 패널이 다시 만들어진다. tf='h' 는 5분봉 저장소(raw/min5)를 본다.

    ★ '최신 mtime' 만 쓰면 안 된다 — 기존 파일이 바뀌어도 그 시각이 최신보다 이르면
      지문이 그대로라 갱신을 놓친다(실측 확인). 전체 (이름, mtime, 크기) 를 해시한다.
      2,789개 stat 에 수십 ms 수준이라 비용은 무시할 만하다."""
    h = hashlib.sha1()
    n = 0
    newest = 0.0
    folder = MIN5 if tf == "h" else PRICE
    for f in sorted(folder.glob("*.csv.gz")) if folder.exists() else []:
        s = f.stat()
        h.update(f"{f.name}:{s.st_mtime_ns}:{s.st_size};".encode())
        newest = max(newest, s.st_mtime)
        n += 1
    for f in sorted(MARCAP.glob("marcap-*.parquet")) if MARCAP.exists() else []:   # 거래대금/시총 원천
        s = f.stat()
        h.update(f"{f.name}:{s.st_mtime_ns}:{s.st_size};".encode())
    return f"v{PANEL_VERSION}:{tf}:{n}:{newest:.0f}:{h.hexdigest()[:12]}"




def _panel_prefix(tf: str) -> str:
    return "panel" if tf == "d" else f"panel{tf}"      # d 는 예전 이름 그대로(panel_*.pkl), 주봉 panelw_*, 60분봉 panelh_*


def prune_cache(keep: int = 1, tf: str = "d") -> int:
    """패널 캐시는 한 개가 800MB 대다. 봉 단위마다 최신 것만 남기고 지운다."""
    if not CACHE.exists():
        return 0
    fs = sorted(CACHE.glob(f"{_panel_prefix(tf)}_*.pkl"), key=lambda p: p.stat().st_mtime, reverse=True)
    removed = 0
    for p in fs[keep:]:
        try:
            p.unlink()
            removed += 1
        except OSError:
            pass
    return removed


# ────────────────────────────── 수급 (개인분산·세력분산·SIG)

def disp(v: np.ndarray) -> np.ndarray:
    """분산비율 = 매집수량 / 매집고점.
    누적순매수 cum, 역대최저 low, 매집수량 acc=cum-low, 매집고점 pk=running_max(acc).
    1.0 = 재고 꽉참(안 팔았다), 0.0 = 재고 비움(다 팔았다). 이름과 방향이 반대라 주의."""
    cum = np.cumsum(v, dtype=float)
    low = np.minimum.accumulate(cum)
    acc = cum - low
    pk = np.maximum.accumulate(acc)
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.where(pk > 0, acc / pk, np.nan)
    return r


IND_DISP_MAX = 0.30        # 수급 조건: 개인 분산비율 ≤ 30% (winners.py — 19개 검색식 중 18개에서 2025~·2021~24 둘 다 개선)
IND_DISP_FFILL = 5         # 수급 자료가 시세보다 늦을 때(오늘 봉·장중) 직전 값을 며칠까지 이어 쓸지
FRGN5_MIN = 0.05           # 외국인 조건: 최근 5일 순매수 ≥ 5일 거래량의 5% (winners.py — 2025~ 15/18 · 2021~24 15/18 검색식에서 개선)
ORGN5_MIN = 0.01           # 기관 조건: 최근 5일 순매수 ≥ 5일 거래량의 1% (2025~ 16/18 · 2021~24 14/18)
FLOW_ADJ_SNAP = 0.005      # 수급 원본가 ÷ 수정종가가 1 에서 이만큼 안 벗어나면 1 로 본다(반올림 잡음)
_FLOW_USE = ("dt", "px_raw", "vol", "ind", "frgn", "orgn", "natfor")      # natfor: 세력 = −(개인+내외국인) — 매집 의심(sig_accum)


def flow_factor(dates, px_raw, close: pd.Series | None) -> np.ndarray:
    """수급 원본의 **주식 수**를 지금 기준(수정주가 기준)으로 맞추는 배율(날짜별).

    키움 투자자별 수량은 그날의 주식 수 그대로다 — 액면병합·무상증자 뒤에도 과거가 다시 매겨지지 않는다
    (실측 2026-10-05: 5:1 병합 종목의 병합 전 수급 거래량 = 수정 거래량의 정확히 5배, 원본가 = 수정종가의 1/5).
    그대로 누적하면 병합 전 수량이 5배로 세어져 분산비율이 틀어진다. 배율 = 원본가(px_raw) ÷ 수정종가.
    바뀐 적이 없는 종목은 전 구간 1 이라 값이 그대로다. 종가가 없으면(close=None) 맞추지 않는다."""
    n = len(px_raw)
    if close is None or not len(close):
        return np.ones(n)
    c = close[~close.index.duplicated()].reindex(pd.DatetimeIndex(dates)).to_numpy(dtype=float)
    raw = np.abs(np.asarray(px_raw, dtype=float))
    with np.errstate(divide="ignore", invalid="ignore"):
        k = np.where((raw > 0) & (c > 0), raw / c, np.nan)
    k = pd.Series(k).ffill().bfill().fillna(1.0).to_numpy()
    k[np.abs(k - 1) < FLOW_ADJ_SNAP] = 1.0
    return k


def read_flow(code: str, close: pd.Series | None = None) -> pd.DataFrame | None:
    """수급 원본에서 패널이 쓰는 열만(index=dt, 날짜순 · 수량은 flow_factor 로 맞춘 값). 없거나 못 읽으면 None."""
    p = FLOW / f"{code}.csv.gz"
    if not p.exists():
        return None
    try:
        d = pd.read_csv(p, usecols=lambda c: c in _FLOW_USE)
    except Exception:
        return None
    if len(d) < 2 or "dt" not in d or "ind" not in d:
        return None
    d["dt"] = pd.to_datetime(d.dt.astype(str))
    d = d.drop_duplicates("dt").sort_values("dt").set_index("dt")
    if "px_raw" in d:
        k = flow_factor(d.index, d.px_raw.to_numpy(), close)
        if (k != 1).any():
            for c in ("vol", "ind", "frgn", "orgn", "natfor"):
                if c in d:
                    d[c] = d[c].to_numpy(dtype=float) * k
    return d


def flow_cols(code: str, index: pd.DatetimeIndex, close: pd.Series | None = None) -> dict:
    """패널의 수급 열(시세 날짜 index 에 맞춘 float32 배열): ind_disp(개인 분산비율 0~1) ·
    frgn5 · orgn5(외국인·기관의 최근 5일 순매수 ÷ 5일 거래량). 수급 원본(raw/flow, 2021-01~)이 없는 종목·날짜는 NaN.
    오늘 봉처럼 수급이 아직 없는 날은 직전 값을 IND_DISP_FFILL 거래일까지 이어 쓴다(장중 검색 — 수급은 장 마감 뒤 확정)."""
    out = {c: np.full(len(index), np.nan, dtype="float32") for c in ("ind_disp", "frgn5", "orgn5")}
    index = pd.DatetimeIndex(index)
    days = index.normalize()
    if len(index) and (days != index).any():               # 60분봉처럼 하루 안의 시각이면 — 수급은 하루 하나라 날짜로 맞춘다
        uday = days.unique()
        dclose = None if close is None else close.groupby(pd.DatetimeIndex(close.index).normalize()).last()
        by_day = flow_cols(code, uday, dclose)
        pos = uday.get_indexer(days)
        return {k: v[pos] for k, v in by_day.items()}
    d = read_flow(code, close)
    if d is None:
        return out
    val = {"ind_disp": pd.Series(disp(d.ind.to_numpy(dtype=float)), index=d.index)}
    if "vol" in d:
        vol = d.vol.astype(float).rolling(5).sum().replace(0, np.nan)
        for name, col in (("frgn5", "frgn"), ("orgn5", "orgn")):
            if col in d:
                val[name] = d[col].astype(float).rolling(5).sum() / vol
    for name, sr in val.items():
        both = sr.reindex(sr.index.union(index)).ffill(limit=IND_DISP_FFILL)
        out[name] = both.reindex(index).to_numpy(dtype="float32")
    return out


def ind_disp(code: str, index: pd.DatetimeIndex, close: pd.Series | None = None) -> np.ndarray:
    """개인 분산비율(0~1, float32)을 시세 날짜(index)에 맞춘 배열 — flow_cols 의 한 열."""
    return flow_cols(code, index, close)["ind_disp"]


def flow_history(code: str) -> pd.DataFrame | None:
    """종목별 수급 재고분석. 개인분산·세력분산·SIG 를 일자별로 낸다.
    세력 ≡ -(개인 + 내외국인)  — 시장 순매수 합이 0인 항등식에서 유도된다
    (전체 320만행 중 위반 0.06%, 25종목뿐)."""
    p = FLOW / f"{code}.csv.gz"
    if not p.exists():
        return None
    try:
        d = pd.read_csv(p, usecols=lambda c: c in ("dt", "ind", "natfor", "px_raw"))
    except Exception:
        return None
    if len(d) < 60:
        return None
    d["dt"] = pd.to_datetime(d.dt.astype(str))
    d = d.sort_values("dt").set_index("dt")
    k = 1.0
    if "px_raw" in d:                           # 병합·증자 전 수량을 지금 주식 수 기준으로(flow_factor)
        px = read_price(code)
        k = flow_factor(d.index, d.px_raw.to_numpy(), None if px is None else px.close)
    ind = d.ind.to_numpy(dtype=float) * k
    force = -(ind + d.natfor.to_numpy(dtype=float) * k)
    out = pd.DataFrame({"개인분산": disp(ind),
                        "세력분산": disp(force)}, index=d.index)
    out["SIG"] = out.개인분산 - out.세력분산      # +1 매수구간 / -1 매도구간
    return out


_INT32_COLS = ("open", "high", "low", "close")
_HH_COLS = ("hh20", "hh60", "hh120", "hh252")


def _compact(o: pd.DataFrame, codes: pd.CategoricalDtype) -> pd.DataFrame:
    """값을 한 자리도 바꾸지 않는 범위에서만 dtype 을 줄인다(패널 1.26GB → 약 0.75GB).

    - Code: 문자열 object(333MB) → 전종목 공통 category. 카테고리 목록이 같아야 concat 후에도
      category 로 남는다(다르면 object 로 풀려 절감이 사라진다).
    - OHLC: int64 → int32. 최대 460만원이라 여유가 크다. 넘치는 종목이 생기면 그 종목만 int64 로 둔다.
    - hh*: 종가의 롤링 최고값이라 정수다. float32 는 2^24(1,677만)까지 정수를 정확히 담는다.
    - 이평·수익률·거래대금 같은 비정수 float 은 **건드리지 않는다** — float32 로 내리면 검정 결과가
      미세하게 바뀐다. 절감보다 검정 재현성이 우선이다.
    """
    o["Code"] = o["Code"].astype(codes)
    for c in _INT32_COLS:
        if o[c].max() < 2**31:
            o[c] = o[c].astype("int32")
    for c in _HH_COLS:
        v = o[c].to_numpy()
        fin = v[np.isfinite(v)]
        if fin.size == 0 or (fin.max() < 2**24 and (fin == np.round(fin)).all()):
            o[c] = o[c].astype("float32")
    for c in ("sik_center", "sik_stop"):      # 정수 또는 정수/2 — 2^22 미만이면 float32 가 정확히 담는다
        if c in o and not (o[c].abs() >= 2**22).any():
            o[c] = o[c].astype("float32")
    return o


def panel_cache_path(since: str | None = None, tf: str = "d") -> Path:
    """현재 데이터 지문에 해당하는 패널 캐시 파일 경로(있는지와 무관)."""
    tag = hashlib.sha1(f"{_fingerprint(tf)}|{since}".encode()).hexdigest()[:16]
    return CACHE / f"{_panel_prefix(tf)}_{tag}.pkl"


def ensure_panel_cache(since: str | None = None, timeout: int = 900, tf: str = "d") -> Path:
    """캐시가 없으면 **별도 프로세스**에서 만든다.

    앱 프로세스 안에서 만들면 생성 순간의 최대 메모리(≈2GB)를 파이썬이 OS 에 돌려주지 않아
    앱이 계속 그만큼 쥐고 있는다. 자식 프로세스는 끝나면 전부 반납하고, 앱은 캐시를 읽기만
    한다(≈0.8GB). 서버 메모리 한도 안에 두기 위한 것이다."""
    cp = panel_cache_path(since, tf)
    if not cp.exists():
        code = ("import sys; sys.path.insert(0, %r); import core; core.build_panel(since=%r, tf=%r)"
                % (str(Path(__file__).resolve().parent), since, tf))
        subprocess.run([sys.executable, "-c", code], check=True, timeout=timeout)
    return cp


def base_state(h: pd.Series, l: pd.Series) -> tuple[pd.Series, pd.Series]:
    """한 종목의 '52주 신저가 후 횡보' 상태 — scan/bottom.base_features 와 같은 정의(테스트가 묶는다).

      since_low  : 마지막 52주 신저가(전날까지 252일 최저보다 **낮아진** 날) 이후 거래일 수, 전날 기준
      base_range : 그 신저가일 ~ **전날** 고가 최고 ÷ 저가 최저 − 1
    오늘 새 신저가면 둘 다 NaN(횡보가 아니다)."""
    prior = l.rolling(252, min_periods=252).min().shift(1)
    new_low = (l < prior) & prior.notna()
    i = pd.Series(np.arange(len(l), dtype=float), index=l.index)
    last = i.where(new_low).ffill()
    hi = h.groupby(last).cummax()
    lo = l.groupby(last).cummin()
    rng = (hi / lo - 1).shift(1)
    since = (i - last).shift(1) + 1
    same = last == last.shift(1)
    return since.where(same), rng.where(same)


TAIL_DAYS, BAD_FRAC, MAX_FIX = 60, 0.30, 0.31      # 마지막 60일만 본다 · 그날 종목 30%↑ 어긋나면 '틀린 날' · 31%↑ 차이는 손대지 않는다


def app_tail(start: pd.Timestamp) -> pd.DataFrame:
    """앱 시세(raw/price 종목별 파일)의 start 이후 행 — Code,Date,o,h,l,c,v. 날짜별 _daily 파일은 빠진 날이 있어 쓰지 않는다
    (2026-09-14~18 은 83종목뿐)."""
    parts = []
    for f in sorted(PRICE.glob("*.csv.gz")):
        try:
            x = pd.read_csv(f, usecols=["Date", "o", "h", "l", "c", "v"], parse_dates=["Date"])
        except Exception:
            continue
        x = x[x.Date > start]
        if len(x):
            parts.append(x.assign(Code=f.name[:6]))
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=["Date", "o", "h", "l", "c", "v", "Code"])


def repair_tail(d: pd.DataFrame, app: pd.DataFrame | None = None, log=print) -> pd.DataFrame:
    """marcap 최근 구간의 틀린 종가를 앱 시세(네이버·다음과 일치 확인)로 바로잡는다. **d 를 제자리에서 고쳐 돌려준다.**

    2026-09-30 실측: upstream(FinanceData/marcap) 의 2026-09-14~09-28 은 종목의 78% 에서 종가가 실제와 달랐다
    (중앙 0.6%, 최대 13% · 거래량·시가는 일치 — 장 마감 전 시점 값으로 보인다. 삼성전자 9/15 248,500 vs 실제 250,500).
    파일은 업데이트 때마다 다시 받으므로 파일을 고치지 않고 **읽을 때** 고친다. upstream 이 바로잡히면 아무 일도 하지 않는다.

      틀린 날   : 마지막 TAIL_DAYS 일 중, 거래량이 같은(=같은 원시 기준) 종목의 BAD_FRAC 이상이 종가가 다른 날
      고치는 행 : 틀린 날 · 앱 시세가 있고 · 거래량이 같고 · 차이가 MAX_FIX 미만인 행 → 종가(있으면 고가·저가)를 앱 값으로,
                  시총은 종가 비율만큼, Changes(있으면)는 '고친 전일 종가 × (원래 기준가/원래 전일 종가)' 기준으로 다시 계산
    평소(틀린 날이 아닌 날)의 불일치는 앱 쪽 수정주가(권리락·병합 소급) 때문이라 건드리지 않는다.
    앱 시세에 없는 종목(그 사이 상폐 등)은 고치지 못한 채 남는다. d 는 Code·Date 순으로 정렬돼 있어야 한다."""
    if "Volume" not in d or d.empty:
        return d
    start = d.Date.max() - pd.Timedelta(days=TAIL_DAYS)
    a = app_tail(start) if app is None else app
    if a.empty:
        return d
    s = d[d.Date > start - pd.Timedelta(days=10)]             # 꼬리(+전일 종가용 여유)만 다룬다 — 전체 1,000만 행을 복사하지 않는다
    j = s.loc[s.Date > start, ["Date", "Code", "Close", "Volume"]].reset_index().merge(a, on=["Date", "Code"], how="inner")
    j = j[(j.Volume == j.v) & (j.c > 0)]
    frac = (j.Close != j.c).groupby(j.Date).mean()
    bad_days = frac.index[frac >= BAD_FRAC]
    if len(bad_days) == 0:
        return d
    fix = j[j.Date.isin(bad_days) & (j.Close != j.c) & ((j.Close / j.c - 1).abs() < MAX_FIX)].set_index("index")
    if fix.empty:
        return d
    prev_old = s.groupby("Code", sort=False, observed=True).Close.shift(1)
    factor = ((s.Close - s.Changes) / prev_old) if "Changes" in s else None
    ratio = pd.Series(1.0, index=s.index)
    ratio.loc[fix.index] = fix.c / s.loc[fix.index, "Close"]
    d.loc[fix.index, "Close"] = fix.c.astype(float)
    for col, src in (("High", "h"), ("Low", "l")):
        if col in d:
            d.loc[fix.index, col] = fix[src].astype(float)
    d.loc[fix.index, "Marcap"] = (s.Marcap * ratio)[fix.index]
    if factor is not None:
        # 틀린 날과 그 다음 거래일의 Changes 를 고친 종가 기준으로 다시 잡는다(기준가 보정 계수는 보존, 2% 미만 차이는 1로 본다)
        touched = ratio != 1.0
        redo = (touched | touched.groupby(s.Code, sort=False, observed=True).shift(1, fill_value=False)) & prev_old.notna()
        fac = factor.where((factor - 1).abs() >= 0.02, 1.0).fillna(1.0)
        s2 = d.loc[s.index]
        prev_new = s2.groupby("Code", sort=False, observed=True).Close.shift(1)
        d.loc[redo[redo].index, "Changes"] = (s2.Close - prev_new * fac)[redo]
    log(f"  marcap 보정: 틀린 날 {len(bad_days)}일({pd.Timestamp(bad_days.min()):%m-%d}~{pd.Timestamp(bad_days.max()):%m-%d}) · "
        f"{len(fix):,}행을 앱 시세로 교체")
    return d


def marcap_turnover(codes) -> dict:
    """종목 → (거래대금/시총 일별 Series, 마지막 상장주식수, 마지막 날짜). marcap 이 없으면 {}.
    KRX 일별 실측(FinanceData/marcap). 수 일 늦게 갱신되므로 그 뒤는 호출한 쪽이 거래량÷주식수로 채운다.

    메모리(2026-10-06 실측 +2.5GB → 아래로 고침): 20년치 880만 행을 7열·Code 문자열(object)로 한 번에 합치면 그것만 2.5GB 다.
    연도 파일마다 걸러 Code 를 범주로 바꾸고 **거래대금/시총(to) 하나로 줄인 뒤** 합친다. 꼬리 종가 보정(repair_tail)은
    마지막 두 해 파일에만 필요하므로(TAIL_DAYS 꼬리 + 전일 여유가 연초에는 전년도에 걸친다) 그 둘만 원래 열을 지닌 채 고친다."""
    fs = sorted(MARCAP.glob("marcap-*.parquet")) if MARCAP.exists() else []
    fs = [f for f in fs if int(f.stem[-4:]) >= 2006]
    if not fs:
        return {}
    want = set(codes)
    cat = pd.CategoricalDtype(sorted(want))
    slim, tail = [], []

    def thin(x):
        return pd.DataFrame({"Date": x.Date.to_numpy(), "Code": x.Code.to_numpy(), "to": (x.Amount / x.Marcap).to_numpy(),
                             "Stocks": x.Stocks.to_numpy()})
    for f in fs:
        x = pd.read_parquet(f, columns=["Date", "Code", "Amount", "Marcap", "Stocks", "Close", "Volume"])
        x = x[x.Code.isin(want) & (x.Marcap > 0)]
        x["Code"] = x.Code.astype(cat)
        if f in fs[-2:]:
            tail.append(x)
        else:
            slim.append(thin(x))
    t = pd.concat(tail, ignore_index=True).sort_values(["Code", "Date"]).reset_index(drop=True)
    tail.clear()
    t = repair_tail(t)            # upstream 최근 구간의 틀린 종가 → 시총 보정(2026-09-30)
    slim.append(thin(t))
    t = None
    d = pd.concat(slim, ignore_index=True)
    slim.clear()
    d = d.sort_values(["Code", "Date"], kind="stable").reset_index(drop=True)
    out = {}
    for code, g in d.groupby("Code", sort=False, observed=True):
        out[str(code)] = (pd.Series(g.to.to_numpy(), index=pd.DatetimeIndex(g.Date)),
                          float(g.Stocks.iloc[-1]), g.Date.iloc[-1])
    return out


def bottom_spike(o: pd.DataFrame, mc: tuple | None) -> tuple[pd.Series, pd.Series]:
    """(거래대금/시총, 바닥권 거래대금폭발 여부). mc = marcap_turnover()[code] 또는 None."""
    if mc is None:
        return pd.Series(np.nan, index=o.index), pd.Series(False, index=o.index)
    ser, stocks, last = mc
    to = ser.reindex(o.index)
    after = o.index > last                       # marcap 이 아직 안 올라온 최근 며칠
    if after.any() and stocks > 0:
        to[after] = o.volume[after] / stocks     # 거래대금/시총 = (종가×거래량)/(종가×주식수)
    since, rng = base_state(o.high, o.low)
    sig = (since >= BASE_DAYS) & (rng <= BASE_BAND) & (to >= BASE_TURNOVER)
    return to, sig.fillna(False).astype(bool)


_PANEL_COLS = ["dt", "Code", "open", "high", "low", "close", "volume", "ret", "val20", "v20",
               "ma20", "ma60", "ma120", "ma200", "hh20", "hh60", "hh120", "hh252",
               "nh20", "nh60", "nh120", "nh252", "surge", "vol60", "mom", "stoch_turn_up", "gc",
               "osc_shrink_d", "osc_up_w", "osc_shrink_w", "osc_pos_d", "osc_pos_w", "osc_shrink3_d", "osc_shrink3_w", "osc_mtf",
               "cafe_juryeok", "algkkagi", "turnover", "bottom_spike", "sik_pullback", "sik_center", "sik_stop",
               "bb_break", "ema_div", "kijun_pull", "osc_ok", "bb40_break", "ind_disp", "frgn5", "orgn5", "wave_collapse", "spike_accum", "osd_vol", "accum", "stairs_break", "stairs_ready", "crow_star"]
# 패널에는 있지만 화면(flowapp)은 읽지 않는 열 — 연구 스크립트(combo·winners 등)는 쓰므로 캐시에는 남기고 앱 메모리에서만 뺀다
# (사용자 "본체 메모리 줄이기", 2026-10-19 — 일봉 기준 약 220MB). 화면이 이 중 하나를 쓰기 시작하면 여기서 빼야 한다.
APP_SKIP = ("ma200", "hh20", "hh60", "hh120", "turnover", "sik_center", "sik_stop")


def slim_for_app(d: pd.DataFrame) -> pd.DataFrame:
    """앱이 읽지 않는 열(APP_SKIP)을 제자리에서 지운다. 캐시가 그 열들을 혼자 블록으로 담고 있어(_assemble) 다른 열은 복사되지 않는다."""
    for c in APP_SKIP:
        if c in d.columns:
            d.pop(c)
    return d


PANEL_MIN_ROWS = 300       # 이보다 짧은 종목은 패널에 넣지 않는다(지표가 안 채워진다)


def read_price(code: str) -> pd.DataFrame | None:
    """저장된 일봉 한 종목(open·high·low·close·volume, index=dt). 없거나 못 읽으면 None."""
    p = PRICE / f"{code}.csv.gz"
    if not p.exists():
        return None
    try:
        d = pd.read_csv(p, usecols=["Date", "o", "h", "l", "c", "v"])
    except Exception:
        return None
    d = d.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    d["dt"] = pd.to_datetime(d.Date)
    return d.set_index("dt").drop(columns=["Date"])


AMOUNT = ROOT / "raw" / "amount"      # 키움 일봉 실제 거래대금(bin/amount.py, 2026-10-07) — 종가×거래량 근사를 대신한다


def read_amount(code: str) -> pd.Series | None:
    """저장된 실제 거래대금(원, index=dt). 파일이 없거나 못 읽으면 None — 호출처는 종가×거래량으로 메운다."""
    p = AMOUNT / f"{code}.csv.gz"
    if not p.exists():
        return None
    try:
        d = pd.read_csv(p, usecols=["Date", "amt"])
    except Exception:
        return None
    s = pd.Series(d.amt.to_numpy(dtype="float64"), index=pd.to_datetime(d.Date), name="amount")
    s.index.name = "dt"
    return s[~s.index.duplicated(keep="last")].sort_index()


def turnover_series(code: str, ohlc: pd.DataFrame) -> pd.Series:
    """차트용 거래대금(원). 키움 실제값이 있는 날은 그것, 없는 날(오늘 봉·미수집)은 종가×거래량."""
    approx = ohlc.close * ohlc.volume
    amt = read_amount(code)
    if amt is None:
        return approx
    real = amt.reindex(ohlc.index)
    return real.where(real.notna() & (real > 0), approx)


_AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}


def weekly_bars(d: pd.DataFrame) -> pd.DataFrame:
    """일봉 → 주봉(금요일 마감, 차트의 주봉과 같은 규칙). 거래가 없는 주는 뺀다."""
    w = d[list(_AGG)].resample("W-FRI").agg(_AGG).dropna(subset=["close"])
    w.index.name = "dt"
    return w


def read_min5(code: str) -> pd.DataFrame | None:
    """5분봉 저장소(raw/min5) 한 종목의 정규장 봉(index=시각, open·high·low·close·volume). 없으면 None."""
    p = MIN5 / f"{code}.csv.gz"
    if not p.exists():
        return None
    try:
        d = pd.read_csv(p, dtype={"t": str})
    except Exception:
        return None
    hm = d.t.str[8:12]
    d = d[(hm >= SESSION[0]) & (hm < SESSION[1])]
    if d.empty:
        return None
    o = pd.DataFrame({"open": d.o.to_numpy(float), "high": d.h.to_numpy(float), "low": d.l.to_numpy(float),
                      "close": d.c.to_numpy(float), "volume": d.v.to_numpy(float)},
                     index=pd.to_datetime(d.t, format="%Y%m%d%H%M"))
    o = o[~o.index.duplicated()].sort_index()
    o.index.name = "dt"
    return o


def hourly_bars(m5: pd.DataFrame) -> pd.DataFrame:
    """5분봉 → 60분봉. 09:00 부터 한 시간씩, 마지막 봉은 15:00~15:30(30분). 봉의 시각 = 시작 시각."""
    h = m5.resample("60min", origin="start_day", label="left", closed="left").agg(_AGG).dropna(subset=["close"])
    h.index.name = "dt"
    return h


def read_bars(code: str, tf: str = "d") -> pd.DataFrame | None:
    """봉 단위별 한 종목 봉. d 저장 일봉 · w 주봉 · h 60분봉."""
    if tf == "h":
        m5 = read_min5(code)
        return None if m5 is None else hourly_bars(m5)
    d = read_price(code)
    if d is None or tf == "d":
        return d
    return weekly_bars(d)


def stock_rows(code: str, d: pd.DataFrame, mc: tuple | None, cat) -> pd.DataFrame:
    """한 종목의 패널 행(지표·신호 열 전부). build_panel 과 오늘 봉(live.py)이 **같은 함수**를 쓴다 — 갈라지면
    저장 자료로 만든 패널과 장중에 붙인 봉의 신호가 어긋난다."""
    o = add_indicators(d)
    o["cafe_juryeok"] = sig_cafe_juryeok(o)
    o["algkkagi"] = sig_algkkagi(o)
    o["turnover"], o["bottom_spike"] = bottom_spike(o, mc)
    o[["sik_pullback", "sik_center", "sik_stop"]] = sig_sik_pullback(o)
    o["bb_break"], o["ema_div"], o["kijun_pull"] = sig_bb_break(o), sig_ema_div(o), sig_kijun_pull(o)
    o["osc_ok"] = osc_ok(o.stoch_osc)
    o["bb40_break"] = sig_bb40_break(o)
    o["wave_collapse"], o["spike_accum"] = sig_wave_collapse(o), sig_spike_accum(o)    # 사용자 그림 둘(2026-10-07)
    o["stairs_break"] = sig_stairs_break(o)                                               # 사용자 그림 3 (2026-10-15)
    o["crow_star"] = sig_crow_star(o)                                                     # 사용자 그림 4 (2026-10-16)
    o["stairs_ready"] = sig_stairs_ready(o)                                               # 그 분출 전 수렴 자리 (2026-10-15)
    o["osd_vol"] = sig_osd_vol(o)                                                         # 스토캐스틱 과매도 + 거래량 (2026-10-07)
    o["accum"] = sig_accum(code, o)                                                       # 매집 의심 (2026-10-15)
    for c, v in flow_cols(code, o.index, o.close).items():
        o[c] = v
    o["Code"] = code
    return _compact(o.reset_index()[_PANEL_COLS], cat)


def marcap_tail_path() -> Path:
    return CACHE / f"mcaptail_{hashlib.sha1(_fingerprint().encode()).hexdigest()[:16]}.pkl"


def save_marcap_tail(mcap: dict) -> None:
    """종목 → (마지막 상장주식수, marcap 마지막 날짜). 오늘 봉의 거래대금/시총을 낼 때 marcap 전체(13초)를
    다시 읽지 않으려고 패널을 만들 때 같이 적어 둔다."""
    CACHE.mkdir(parents=True, exist_ok=True)
    cp = marcap_tail_path()
    for old in CACHE.glob("mcaptail_*.pkl"):
        if old != cp:
            old.unlink(missing_ok=True)
    pd.to_pickle({c: (v[1], v[2]) for c, v in mcap.items()}, cp)


def marcap_tail(codes) -> dict:
    """종목 → bottom_spike 가 받는 모양 (빈 Series, 상장주식수, 마지막 날짜). 적어 둔 것이 없으면 한 번 만든다."""
    cp = marcap_tail_path()
    if not cp.exists():
        save_marcap_tail(marcap_turnover(codes))
    empty = pd.Series(dtype=float, index=pd.DatetimeIndex([]))
    return {c: (empty, stocks, last) for c, (stocks, last) in pd.read_pickle(cp).items()}


# 패널은 종목별 계산의 합이라 종목을 나눠 **여러 프로세스**가 만든다(사용자 요청 2026-10-06 "60분봉 패널 빨리").
# 60분봉 한 종목 ≈ 90ms 는 봉 수(1,750)가 아니라 pandas 연산 횟수(종목당 수백 번)의 고정비라 꼬리만 다시 계산하는
# '증분'으로는 거의 안 줄고(게다가 rolling 252·ewm 240·cummax 가 1년치 전체를 본다 — 꼬리 계산은 값이 어긋난다),
# 종목 병렬이 정확하면서 코어 수만큼 빨라진다. 매일 전 종목 파일이 바뀌므로 '안 바뀐 종목 재사용'도 효과가 없다.
PANEL_WORKERS = int(os.environ.get("FLOWAPP_PANEL_WORKERS", "0")) or max(1, min(8, (os.cpu_count() or 2) // 2))
PANEL_MIN_PARALLEL = 64          # 이보다 적은 종목(테스트)은 한 프로세스에서
PANEL_START = "spawn" if sys.platform == "win32" else "fork"     # 윈도우에는 fork 가 없다(윈도우 배포판, 2026-10-19)
_BUILD: dict = {}                # fork 된 작업 프로세스가 물려받는 공통값(tf·시총·종목 범주) — 작업마다 보내면 범주 2,500개를 매번 피클한다


def _panel_one(code: str):
    d = read_bars(code, _BUILD["tf"])
    if d is None or len(d) < TF_MIN_ROWS[_BUILD["tf"]]:
        return None
    return stock_rows(code, d, _BUILD["mcap"].get(code), _BUILD["cat"])


def trim_heap() -> None:
    """놓은 작은 배열들이 힙에 남아 OS 로 안 돌아가는 것(glibc)을 돌려준다. 패널을 만드는 자식 프로세스의 최대 메모리를 낮춘다."""
    import ctypes
    import gc
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except OSError:
        pass


class _Grow:
    """열 하나를 받아 쌓는 버퍼. 큰 배열 하나를 두 배씩 키우며 채우고(큰 할당은 mmap 이라 놓으면 바로 OS 로 돌아간다),
    끝에 제자리 resize 로 딱 맞게 줄인다. 섞인 dtype(int32·int64 / float32·float64)은 pd.concat 과 같은 상위 dtype 으로 올린다."""
    __slots__ = ("a", "n")

    def __init__(self):
        self.a, self.n = None, 0

    def add(self, x: np.ndarray) -> None:
        if self.a is None:
            self.a = np.empty(max(len(x) * 64, 1 << 16), dtype=x.dtype)
        elif self.a.dtype != x.dtype:
            up = np.result_type(self.a.dtype, x.dtype)
            if up != self.a.dtype:
                self.a = self.a.astype(up)
        if self.n + len(x) > len(self.a):
            self.a.resize(max(len(self.a) * 2, self.n + len(x)), refcheck=False)
        self.a[self.n:self.n + len(x)] = x
        self.n += len(x)

    def done(self) -> np.ndarray:
        a, self.a = self.a, None
        a.resize(self.n, refcheck=False)
        return a


def _concat_rows(rows, cat) -> pd.DataFrame:
    """종목별 패널 행을 하나로. pd.concat(목록) 은 목록(1벌)과 결과(1벌)가 같이 떠 패널의 두 배를 쥐고, 종목별 작은 배열을
    모았다 합치는 방식도 작은 배열이 힙에 남아(glibc) 결국 두 배였다(2026-10-06 실측 +2.1GB/1.05GB 패널). 열마다 큰 버퍼
    하나에 바로 써 넣어 **한 벌**만 쓴다. 결과(순서·dtype·RangeIndex)는 concat 과 같다."""
    buf: dict[str, _Grow] = {}
    cols: list[str] = []
    for i, r in enumerate(rows):
        if r is None:
            continue
        if not cols:
            cols = list(r.columns)
            buf = {c: _Grow() for c in cols}
        for c in cols:
            v = r[c]
            buf[c].add((v.cat.codes if c == "Code" else v).to_numpy())
        if i % 256 == 255:
            trim_heap()
    if not cols:
        return pd.DataFrame(columns=_PANEL_COLS)
    d = _assemble(cols, buf, cat)
    trim_heap()
    return d


def _assemble(cols: list[str], buf: dict, cat) -> pd.DataFrame:
    """열 버퍼들을 dtype 묶음별 2차원 블록으로 **한 번만** 옮겨 담아 프레임을 만든다.

    열마다 블록 하나(50개)인 채로 피클하면 **앱이** 처음 쓰는 순간 dtype 별 블록으로 다시 합치며 한 벌을 더 복사하고 그 힙을
    돌려주지 않는다(실측 2026-10-07: 앱 상주 1.35 → 2.26GB). `_consolidate_inplace()` 로 여기서 합쳐도 되지만 그것은 전체를
    한 번에 복사해 자식 프로세스 순간치가 +1GB 다. 묶음 하나씩 옮기고 옮긴 열 버퍼를 바로 놓으면(mmap → 즉시 반납) 순간치는
    가장 큰 묶음(float64 열 ≈0.7GB)뿐이다. pandas 내부 API(make_block·BlockManager, 2.2.3 에서 확인)라 실패하면 공개 경로로 돌아간다."""
    n = next(iter(buf.values())).n
    groups: dict = {}
    for c in cols:
        key = "cat" if c == "Code" else str(buf[c].a.dtype) + (f"|{c}" if c in APP_SKIP else "")   # 앱이 안 읽는 열은 혼자 블록 — 앱이 지울 때 큰 블록을 복사하지 않는다
        groups.setdefault(key, []).append(c)
    try:
        from pandas.core.internals import BlockManager
        from pandas.core.internals.api import make_block
        blocks = []
        for key, names in groups.items():
            place = np.array([cols.index(c) for c in names])
            if key == "cat":
                blocks.append(make_block(pd.Categorical.from_codes(buf.pop(names[0]).done(), dtype=cat), placement=place))
                continue
            blk = np.empty((len(names), n), dtype=np.dtype(key.split("|")[0]))
            for i, c in enumerate(names):
                blk[i] = buf.pop(c).done()
            blocks.append(make_block(blk, placement=place))
        mgr = BlockManager(tuple(blocks), [pd.Index(cols), pd.RangeIndex(n)])
        return pd.DataFrame._from_mgr(mgr, axes=mgr.axes)
    except Exception as e:                                    # noqa: BLE001 — pandas 가 바뀌어 내부 API 가 다르면
        print(f"  패널 블록 조립 실패({repr(e)[:60]}) → 공개 경로(열별 프레임 + consolidate)", file=sys.stderr, flush=True)
        out = {}
        for c in cols:
            if c in buf:
                a = buf.pop(c).done()
                out[c] = pd.Categorical.from_codes(a, dtype=cat) if c == "Code" else a
        d = pd.DataFrame(out, copy=False)
        d._consolidate_inplace()
        return d


def _spawn_init(tf: str, cat) -> None:
    """윈도우(spawn) 작업 프로세스 시작 — fork 처럼 _BUILD 를 물려받지 못하므로 작은 공통값만 받는다."""
    _BUILD.update(tf=tf, cat=cat, mcap={})


def _panel_one_spawn(item):
    """윈도우용: 시총 Series 는 종목마다 작업과 함께 보낸다(전체 dict 를 작업 프로세스마다 복사하지 않는다)."""
    code, mc = item
    _BUILD["mcap"] = {code: mc} if mc is not None else {}
    return _panel_one(code)


def _panel_rows(codes: list, tf: str, mcap: dict, cat):
    """종목별 패널 행을 차례로 낸다(없는 종목은 None). 종목이 많으면 fork 풀로 나눠 돈다 — 순서는 codes 순서 그대로.
    윈도우에는 fork 가 없어 spawn 풀로 돈다(윈도우 배포판, 2026-10-19)."""
    _BUILD.update(tf=tf, mcap=mcap, cat=cat)
    try:
        if PANEL_WORKERS > 1 and len(codes) >= PANEL_MIN_PARALLEL and PANEL_START == "spawn":
            import multiprocessing as mp
            with mp.get_context("spawn").Pool(PANEL_WORKERS, initializer=_spawn_init, initargs=(tf, cat)) as pool:
                yield from pool.imap(_panel_one_spawn, ((c, mcap.get(c)) for c in codes), chunksize=8)
        elif PANEL_WORKERS > 1 and len(codes) >= PANEL_MIN_PARALLEL:
            import multiprocessing as mp
            with mp.get_context("fork").Pool(PANEL_WORKERS) as pool:
                yield from pool.imap(_panel_one, codes, chunksize=8)
        else:
            for c in codes:
                yield _panel_one(c)
    finally:
        _BUILD.clear()


def build_panel(since: str | None = None, cache: bool = True, tf: str = "d") -> pd.DataFrame:
    """전종목 지표 패널. 종목별 시계열 계산만 한다(횡단면 연산 없음). tf = 봉 단위(TFS) — 주봉·60분봉도 **같은 stock_rows** 로 만든다.

    dtype 은 종목별로 줄인 뒤 합친다(`_compact`). 다 합친 뒤에 줄이면 합치는 순간 float64
    사본이 두 벌 떠서 최대 메모리가 3.7GB 까지 오른다(실측 2026-09-29).
    주봉·60분봉에는 marcap(거래대금/시총)을 붙이지 않는다 — 거래대금 폭발 검색식은 그 봉 단위에서 비어 있다."""
    cp = panel_cache_path(since, tf)
    if cache and cp.exists():
        return pd.read_pickle(cp)
    codes = universe().Code
    cat = pd.CategoricalDtype(sorted(codes.unique()))
    mcap = marcap_turnover(codes) if tf == "d" else {}
    trim_heap()                      # marcap 을 합치고 정렬하며 놓은 힙(≈0.7GB)을 작업 프로세스를 가르기 전에 돌려준다
    mcap_tail = {c: (None, v[1], v[2]) for c, v in mcap.items()}    # save_marcap_tail 이 쓰는 꼬리 요약만 남기고
    rows = _panel_rows(list(codes), tf, mcap, cat)
    mcap = None                      # 일별 Series(880만 행)는 작업 프로세스가 _BUILD 로 물려받아 쓰고, 행을 다 모으면 _BUILD 와 함께 놓인다
    d = _concat_rows(rows, cat)
    if since:
        d = d[d.dt >= pd.Timestamp(since)].reset_index(drop=True)
    if cache:
        CACHE.mkdir(parents=True, exist_ok=True)
        d.to_pickle(cp)
        prune_cache(keep=1, tf=tf)       # 캐시 한 개가 800MB 대라 옛것을 남기지 않는다
        if mcap_tail:
            save_marcap_tail(mcap_tail)
    return d


LIVE_TAIL = 2      # 하루를 붙이면 그 종목의 **직전 행**도 바뀐다 — 주봉 신호(osc_*_w)는 '그 주의 마지막 거래일'에 찍히므로
#                    같은 주에 새 날이 생기면 표시가 새 날로 옮겨 간다(실측 150종목×7일: 바뀌는 것은 직전 한 행·주봉 열 4개뿐).


def merge_live(full: pd.DataFrame, rows: pd.DataFrame, day) -> pd.DataFrame:
    """패널에 오늘 봉(잠정) 행들을 **각 종목의 맨 끝에** 끼워 넣은 새 패널. full 은 건드리지 않는다.

    - rows 는 종목마다 마지막 LIVE_TAIL 행(직전 거래일 + 오늘). 패널의 같은 날짜 행은 빼고 rows 것으로 바꾼다.
      앞서 붙였던 오늘 봉도 그렇게 바뀐다(장중에 여러 번 받는다).
    - 종목별로 날짜순 연속 + 0 부터 RangeIndex 라는 패널 규칙을 지킨다 — forward_close·panel_arrays 가 행 위치로 센다.
      그래서 뒤에 그냥 이어 붙이면 안 된다.
    - 열 하나씩 옮긴다. 통째로 concat 후 take 하면 패널(0.8GB)이 순간 세 벌이 된다."""
    day = pd.Timestamp(day)
    cat = full.Code.dtype
    codes = full.Code.cat.codes.to_numpy()
    rc = pd.Categorical(rows.Code.astype(str), dtype=cat).codes.astype(np.int64)
    has = np.zeros(len(cat.categories), dtype=bool)
    has[np.unique(codes)] = True
    ok = (rc >= 0) & has[np.maximum(rc, 0)] & (rows.dt <= day).to_numpy()       # 패널에 없는 종목의 봉은 붙이지 않는다
    rows, rc = rows[ok], rc[ok]
    by = np.lexsort((rows.dt.to_numpy(), rc))
    rows, rc = rows.iloc[by], rc[by]
    dup = np.r_[False, (rc[1:] == rc[:-1]) & (rows.dt.to_numpy()[1:] == rows.dt.to_numpy()[:-1])]
    rows, rc = rows[~dup], rc[~dup]
    cut = np.full(len(cat.categories), day.to_datetime64(), dtype="datetime64[ns]")   # 새 봉이 없는 종목도 옛 오늘 봉은 뺀다
    first = np.r_[True, rc[1:] != rc[:-1]]
    cut[rc[first]] = rows.dt.to_numpy()[first]                  # 새 행이 오는 종목은 그 첫 날짜부터 갈아 끼운다
    keep = full.dt.to_numpy() < cut[codes]
    cb = codes[keep]
    end = np.flatnonzero(np.r_[cb[1:] != cb[:-1], True]) + 1    # 종목 덩어리가 끝나는 자리(다음 행 위치)
    at = np.full(len(cat.categories), -1, dtype=np.int64)
    at[cb[end - 1]] = end
    ok = at[rc] >= 0                                            # 남은 행이 하나도 없는 종목은 덩어리 자리를 잃는다 — 붙이지 않는다
    rows, rc = rows[ok], rc[ok]
    pos = at[rc]
    by = np.argsort(pos, kind="stable")                         # np.insert 는 자리 순서대로 — 같은 자리는 날짜순 그대로
    pos, rc, rows = pos[by], rc[by], rows.iloc[by]
    out = {}
    for c in full.columns:
        if c == "Code":
            out[c] = pd.Categorical.from_codes(np.insert(cb, pos, rc.astype(cb.dtype)), dtype=cat)
        else:
            a = full[c].to_numpy()
            out[c] = np.insert(a[keep], pos, rows[c].to_numpy().astype(a.dtype))
    return pd.DataFrame(out, copy=False)


def price_axis_range(src: pd.DataFrame, entry: float, hh_col: str,
                     stop_loss: float | None = None, take_profit: float | None = None,
                     pad_ratio: float = 0.04) -> tuple[float, float]:
    """차트 가격축 범위. **절대로 음수를 반환하지 않는다.**

    Plotly 의 휠 확대는 x·y 를 같이 늘려서, 축소하면 0 아래 빈 공간이 화면 절반을 먹는다
    (사용자 신고 2026-09-15). y 를 fixedrange 로 고정하고 이 함수가 준 범위만 쓴다.

    범위는 '보이는 구간(src)' 기준이다. 전체 이력에 맞추면 고점 대비 많이 빠진 종목의
    최근 봉이 눌린다(실측 아시아나항공 020560: 최근 1.5년이 전체 축의 10.2% 뿐).
    """
    seen = [float(src.low.min()), float(src.high.max()), float(entry)]
    for c in ("ma20", "ma60", "ma120", hh_col):
        if c in src and src[c].notna().any():
            seen += [float(src[c].min()), float(src[c].max())]
    if stop_loss:
        seen.append(entry * (1 - stop_loss / 100))
    if take_profit:
        seen.append(entry * (1 + take_profit / 100))
    lo, hi = max(float(min(seen)), 0.0), float(max(seen))
    pad = (hi - lo) * pad_ratio or max(1.0, hi * 0.02)
    return max(0.0, lo - pad), hi + pad


def map_ratio_to_price(ratio: pd.Series, lo: float, hi: float, log: bool = True,
                       band: tuple[float, float] = (0.0, 1.0)) -> pd.Series:
    """0~1 비율을 가격축 좌표로 옮긴다(상대참조).

    별도 y축(0~1 고정)에 그리면 가격축과 따로 놀아 축소 시 캔들만 납작해진다.
    분산의 **절대값은 의미가 없으므로**(사용자 지적 2026-09-15) 가격축 좌표로 바꿔
    같이 움직이게 한다. 로그축에서는 기하보간을 써야 시각적으로 등간격이 된다.

    band 는 패널에서 차지할 세로 구간이다. 기본 (0,1) 은 전체 높이라 분산선이
    화면을 다 먹는다 — HTS·네이버 차트처럼 아래쪽 좁은 띠에 넣으려면 (0, 0.25) 처럼 준다.
    """
    b0, b1 = band
    r = ratio.clip(0.0, 1.0) * (b1 - b0) + b0
    if log and lo > 0:
        return lo * (hi / lo) ** r
    return lo + r * (hi - lo)


def signal_dates(df: pd.DataFrame, mask: pd.Series, code: str) -> list[pd.Timestamp]:
    """차트에 찍을 신호 발생일. 화면의 스캔 결과와 정확히 같은 정의를 쓴다."""
    sub = df[(df.Code == code) & mask.reindex(df.index).fillna(False)]
    return list(pd.to_datetime(sub.dt).sort_values())


# ────────────────────────────── 횡단면 (전달받은 프레임 안에서만)

def xrank(df: pd.DataFrame, col: str) -> pd.Series:
    """날짜별 횡단면 백분위 순위. 반드시 검정 유니버스로 좁힌 뒤 호출할 것."""
    return df.groupby("dt")[col].rank(pct=True)


def excess(df: pd.DataFrame, col: str) -> pd.Series:
    """시장중립 초과수익. 전달받은 프레임(=검정 유니버스) 안에서만 중립화한다.
    이 함수를 거치지 않고 직접 빼면 기준선이 0이 아니게 되는 사고가 난다."""
    return df[col] - df.groupby("dt")[col].transform("mean")


def apply_liquidity(df: pd.DataFrame, min_val: float) -> pd.DataFrame:
    # take 는 행을 한 번만 복사한다. df[mask].copy() 는 불리언 인덱싱 사본을 또 복사해
    # 순간 메모리가 +1.17GB 였다(take 는 +0.40GB, 실측 2026-09-29). 둘 다 원본과 끊긴 독립 사본이고
    # 인덱스 라벨도 같다.
    if not min_val:
        return df.copy()
    return df.take(np.flatnonzero((df.val20 >= min_val).to_numpy()))


# ────────────────────────────── 신호 생성

def panel_codes(full: pd.DataFrame) -> np.ndarray:
    """패널 행마다 종목 번호(같은 종목 = 같은 수)."""
    return (full.Code.cat.codes.to_numpy() if isinstance(full.Code.dtype, pd.CategoricalDtype)
            else pd.factorize(full.Code)[0])


def reclaim_days(codes: np.ndarray, close: np.ndarray, base: np.ndarray, window: int = RECLAIM_WINDOW,
                 dip: float = RECLAIM_DIP / 100, vr: np.ndarray | None = None, vmult: float = 0.0) -> np.ndarray:
    """신호 뒤 눌림 → 회복일(패널 행 순서 bool 배열). reclaim.py 의 검정과 같은 정의다.

    base 사건마다 신호가 P0 = 그날 종가. 그 뒤 window 거래일 안에 종가가 P0×(1−dip) **아래로** 내려갔다가(눌림)
    처음으로 P0 이상에서 닫힌 날 = 회복일. vmult>0 이면 신호일~눌림 사이에 거래량 배수(vr) ≥ vmult 인 날이 있어야 한다.
    codes·close 는 **걸러지기 전 패널**(종목별 날짜순 연속)의 것이어야 한다 — 걸러진 프레임에서 세면 날짜를 건너뛴다."""
    E = np.flatnonzero(base)
    n = len(close)
    out = np.zeros(n, dtype=bool)
    if not len(E):
        return out
    p0 = close[E].astype(float)
    use_v = bool(vmult) and vr is not None
    burst = (vr[E] >= vmult) if use_v else np.ones(len(E), dtype=bool)
    dipped = np.zeros(len(E), dtype=bool)
    done = np.zeros(len(E), dtype=bool)
    for k in range(1, int(window) + 1):
        j = E + k
        ok = j < n
        j = np.minimum(j, n - 1)
        ok &= codes[j] == codes[E]
        c = close[j].astype(float)
        hit = ok & dipped & ~done & (c >= p0)                 # 눌린 뒤 신호가 회복
        out[j[hit]] = True
        done |= hit
        if use_v:
            burst |= ok & ~dipped & (vr[j] >= vmult)
        dipped |= ok & ~done & (c < p0 * (1 - dip)) & burst
    return out


def volume_ratio(full: pd.DataFrame) -> np.ndarray:
    """거래량 배수 = 그날 거래량 ÷ **전날까지** 20일 평균(패널 행 순서). 종목의 첫 행·평균이 없는 날은 0."""
    codes = panel_codes(full)
    v = full.volume.to_numpy(dtype=float)
    prev = np.r_[np.nan, full.v20.to_numpy(dtype=float)[:-1]]
    prev[np.r_[True, codes[1:] != codes[:-1]]] = np.nan
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.nan_to_num(np.where(prev > 0, v / prev, np.nan))


def make_signal(df: pd.DataFrame, kind: str, vol_q: float = 0.30, hh_days: int = 120,
                cafe_decline: float = 15.0, alg_rise: float = 20.0, alg_pullback: float = 15.0,
                exclude_surge: bool = True, up_days: pd.Series | None = None, flow_ok: bool = False,
                frgn_ok: bool = False, orgn_ok: bool = False, full: pd.DataFrame | None = None,
                reclaim_window: int = RECLAIM_WINDOW, reclaim_dip: float = RECLAIM_DIP,
                reclaim_vmult: float = RECLAIM_VMULT) -> pd.Series:
    """df 는 이미 유동성으로 좁혀진 검정 유니버스여야 한다(vol 순위가 그 안에서 매겨진다).
    up_days(날짜 → 상승국면 여부, market_up_days 의 결과)는 df 가 최근 구간만 담고 있을 때 넘긴다 —
    상승국면은 전 구간 지수의 120일 평균으로 정하므로 잘린 df 로는 구할 수 없다(recent_signal 이 넘긴다).
    full(걸러지기 전 패널)은 재진입 검색식(RECLAIM)에만 필요하다 — 신호 뒤 며칠을 **종목의 연속 시계열**에서 세야 한다.
    df.index 는 full 의 행 라벨이어야 하고(apply_liquidity 가 보장), df 가 구간을 잘랐으면 그 앞의 신호에서 나온 회복은 빠진다
    (화면은 recent_signal 이 전 구간으로 계산한다)."""
    if kind == "lowvol_breakout":
        vr = xrank(df, "vol60")                   # 변동성 순위는 '살 수 있는 종목들' 안에서
        s = (vr <= vol_q) & df[f"nh{hh_days}"]    # 신고가는 패널 생성 시 연속 시계열에서 판정
    elif kind == "cafe_juryeok":
        s = df.cafe_juryeok
    elif kind == "algkkagi":
        s = df.algkkagi
    elif kind in ("stoch_turn_up", "osc_shrink_d", "osc_up_w", "osc_shrink_w",
                  "osc_pos_d", "osc_pos_w", "osc_shrink3_d", "osc_shrink3_w", "osc_mtf"):
        s = df[kind]
    elif kind == "gc":
        s = df.gc
    elif kind == "bottom_spike":
        s = df.bottom_spike
    elif kind in ("sik_pullback", "bb_break", "ema_div", "kijun_pull", "wave_collapse", "spike_accum", "osd_vol", "accum", "stairs_break", "stairs_ready", "crow_star"):
        s = df[kind]
    elif kind == "win_combo":
        s = ((xrank(df, "vol60") <= vol_q) & df[f"nh{hh_days}"]
             & (xrank(df, "mom") >= WIN_MOM_Q) & (df.close >= WIN_NEAR52 * df.hh252))
    elif kind == "win_tight":                 # 승률형 + 20일선 이격 ≤ 5% (combo_win.py)
        s = ((xrank(df, "vol60") <= vol_q) & df[f"nh{hh_days}"]
             & (xrank(df, "mom") >= WIN_MOM_Q) & (df.close >= WIN_NEAR52 * df.hh252)
             & (df.close <= (1 + WIN_TIGHT_GAP) * df.ma20))
    elif kind == "oscw_trend":                # 주봉 오실 음봉 축소 시작 + 저변동 + 이평 정배열 (combo_win.py)
        s = df.osc_shrink_w & (xrank(df, "vol60") <= vol_q) & (df.ma20 > df.ma60) & (df.ma60 > df.ma120)
    elif kind == "bb40_combo":                # 급등일 제외 없이 검정했다(돌파일은 흔히 급등일) — NO_SURGE_EXCLUDE
        up = market_up(df) if up_days is None else df.dt.map(up_days).fillna(False).astype(bool)
        s = df.bb40_break & (df.close >= WIN_NEAR52 * df.hh252) & up
    elif kind == "best_combo":
        vr = xrank(df, "vol60")
        up = market_up(df) if up_days is None else df.dt.map(up_days).fillna(False).astype(bool)
        s = (vr <= vol_q) & df[f"nh{hh_days}"] & up & df.osc_ok
    elif kind in RECLAIM:                     # 바탕 검색식의 신호 뒤 눌렸다가 신호가를 되찾은 날 (reclaim.py)
        if full is None:
            raise ValueError(f"{kind}: 재진입 검색식은 걸러지기 전 패널(full)이 있어야 한다")
        base = make_signal(df, RECLAIM[kind], vol_q=vol_q, hh_days=hh_days, exclude_surge=False, up_days=up_days)
        on = np.zeros(len(full), dtype=bool)
        on[df.index.to_numpy()[base.to_numpy()]] = True
        out = reclaim_days(panel_codes(full), full.close.to_numpy(), on, reclaim_window, reclaim_dip / 100,
                           volume_ratio(full) if reclaim_vmult else None, reclaim_vmult)
        s = pd.Series(out[df.index.to_numpy()], index=df.index)       # 회복일도 유동성을 통과한 날이어야 한다
    else:
        raise ValueError(f"알 수 없는 신호: {kind}")
    s = s.fillna(False).astype(bool)
    if exclude_surge and kind not in NO_SURGE_EXCLUDE:
        s = s & ~df.surge.fillna(False)
    # 수급 조건 — 기준에 못 미치는 날만 뺀다(수급 자료가 없는 날·종목은 통과: 2021년 이전의 차트 신호일이 통째로 사라지지 않게)
    if flow_ok and "ind_disp" in df:              # 개인 분산비율 ≤ IND_DISP_MAX
        s = s & ~(df.ind_disp > IND_DISP_MAX).to_numpy()
    if frgn_ok and "frgn5" in df:                 # 외국인 5일 순매수 ≥ 거래량의 FRGN5_MIN
        s = s & ~(df.frgn5 < FRGN5_MIN).to_numpy()
    if orgn_ok and "orgn5" in df:                 # 기관 5일 순매수 ≥ 거래량의 ORGN5_MIN
        s = s & ~(df.orgn5 < ORGN5_MIN).to_numpy()
    return s


# ────────────────────────────── 빠른 검색 (화면용 — 결과는 위의 전 구간 경로와 같다)
#   apply_liquidity 는 전 구간 600만 행 × 45열을 통째로 복사한다(0.2초·0.4GB). 후보 목록에는 최근 구간만,
#   차트 신호일에는 열 몇 개만 있으면 되므로 필요한 만큼만 떼어 make_signal 에 넘긴다.
#   횡단면 순위(xrank)는 같은 날짜의 행끼리만 매기므로 구간을 잘라도 값이 같고, 전 구간이 필요한
#   상승국면만 up_days 로 따로 넘긴다. 두 경로가 같다는 것은 test_flowtool 이 실데이터로 고정한다.

_SIG_COLS = ("dt", "vol60", "mom", "close", "hh252", "ret", "osc_ok", "surge", "bb40_break", "ind_disp", "frgn5", "orgn5", "ma20", "ma60", "ma120", "osc_shrink_w", "wave_collapse", "spike_accum", "osd_vol", "accum", "stairs_break", "stairs_ready", "crow_star")     # make_signal 이 읽는 열(신호 열·nh 는 따로)
_CAND_COLS = ("Code", "val20")                                                       # 후보 목록이 더 읽는 열


def window_cut(dates, n: int, unit: str = "d") -> pd.Timestamp:
    """자료의 거래일(dates)에서 **최근 n 봉이 시작하는 날**. unit: d 일봉 · w 주봉(금요일 마감) · m 월봉.
    주·월은 아직 안 끝난 이번 주(달)도 한 봉으로 센다 — 5주 = 이번 주 + 지난 4주. 자료보다 길면 첫날."""
    dates = pd.DatetimeIndex(np.sort(pd.unique(np.asarray(dates))))
    n = max(int(n), 1)
    days = dates.normalize()                           # 60분봉은 하루에 봉이 여럿 — '거래일' 수로 센다
    uday = days.unique()
    if unit == "d":
        day = uday[-min(n, len(uday))]
    else:
        per = uday.to_period("W-FRI" if unit == "w" else "M")
        bars = per.unique()
        day = uday[per == bars[-min(n, len(bars))]][0]
    return pd.Timestamp(dates[days == day][0])


def liquid_rows(full: pd.DataFrame, min_val: float, cut=None) -> np.ndarray:
    """유동성 하한을 넘는(그리고 cut 이후인) 행의 위치. apply_liquidity 와 같은 기준 — 하한이 0 이면 전부."""
    keep = np.ones(len(full), dtype=bool) if not min_val else (full.val20 >= min_val).to_numpy()
    if cut is not None:
        keep &= full.dt.to_numpy() >= np.datetime64(pd.Timestamp(cut))
    return np.flatnonzero(keep)


def _narrow(full: pd.DataFrame, cols, pos: np.ndarray) -> pd.DataFrame:
    """full 의 pos 행에서 cols 열만 뗀 프레임(행 라벨 유지). full[cols] 는 600만 행을 먼저 복사하므로 열별로 뗀다.
    full 에 없는 열은 건너뛴다 — 그 열이 정말 필요한 검색식이면 make_signal 이 KeyError 로 알린다."""
    def cut(s: pd.Series):                  # 범주형(Code)은 범주형 그대로 — to_numpy 는 문자열 객체 배열로 풀어 버린다
        return s.array.take(pos) if isinstance(s.dtype, pd.CategoricalDtype) else s.to_numpy()[pos]
    return pd.DataFrame({c: cut(full[c]) for c in dict.fromkeys(cols) if c in full.columns}, index=row_labels(full, pos))


def row_labels(full: pd.DataFrame, pos: np.ndarray) -> pd.Index:
    """full 의 pos 행 라벨. 패널은 RangeIndex 라 계산으로 낸다 — full.index[pos] 는 pandas 가 600만 칸짜리 행 번호 배열(48MB)을
    만들어 패널 인덱스에 **캐시해 두고 놓지 않는다**(2026-10-19 실측, 검색 한 번에 상주 +48MB)."""
    ix = full.index
    if isinstance(ix, pd.RangeIndex):
        return pd.Index(ix.start + np.asarray(pos, dtype=np.int64) * ix.step)
    return ix[pos]


def liquid_up_days(full: pd.DataFrame, min_val: float) -> pd.Series:
    """유동성으로 좁힌 유니버스의 날짜별 상승국면 — market_up(apply_liquidity(full, min_val)) 의 날짜별 값."""
    return market_up_days(_narrow(full, ("dt", "ret"), liquid_rows(full, min_val)))


def recent_signal(full: pd.DataFrame, min_val: float, kind: str, cut, up_days: pd.Series | None = None,
                  fullmask: np.ndarray | None = None, **sig) -> tuple[pd.DataFrame, pd.Series]:
    """cut 이후·유동성 통과 행만 떼어 낸 프레임과 그 위의 신호. 행 라벨은 full 의 것 그대로다.
    apply_liquidity(full) 전 구간에 make_signal 을 돌린 뒤 cut 이후만 본 것과 같다.
    프레임에는 신호 판정과 후보 목록(latest_candidates·state_candidates)이 읽는 열만 담는다 — 30개월이면 90만 행이다."""
    cols = _SIG_COLS + _CAND_COLS + (f"nh{sig.get('hh_days', 120)}",) + ((kind,) if kind in full.columns else ())
    win = _narrow(full, cols, liquid_rows(full, min_val, cut))
    if kind in RECLAIM:
        # 회복일은 cut 보다 앞선 신호에서도 나온다 — 구간을 잘라서는 못 구한다. 전 구간 신호(full_signal)에서 떼어 쓴다.
        # fullmask 는 화면이 설정별로 만들어 둔 것(flowapp.full_mask) — 없으면 여기서 만든다.
        m = full_signal(full, min_val, kind, **sig) if fullmask is None else fullmask
        return win, pd.Series(m[win.index.to_numpy()], index=win.index)
    if kind in REGIME_KINDS and up_days is None:
        up_days = liquid_up_days(full, min_val)
    return win, make_signal(win, kind, up_days=up_days, **sig)


def full_signal(full: pd.DataFrame, min_val: float, kind: str, **sig) -> np.ndarray:
    """전 구간 신호를 full 의 행 순서 bool 배열로(차트 신호일용). 필요한 열만 떼어 계산한다."""
    pos = liquid_rows(full, min_val)
    cols = list(_SIG_COLS) + [f"nh{sig.get('hh_days', 120)}"] + ([kind] if kind in full.columns else [])
    sub = _narrow(full, cols, pos)
    out = np.zeros(len(full), dtype=bool)
    if kind in RECLAIM:
        sig = dict(sig, full=full)
    out[pos[make_signal(sub, kind, **sig).to_numpy()]] = True
    return out


# ────────────────────────────── 검정

def forward_return(df: pd.DataFrame, horizon: int, full: pd.DataFrame | None = None) -> pd.Series:
    """df 각 행의 horizon **거래일** 뒤 종가 수익률(df.index 기준 Series).

    ★ df 가 유동성으로 걸러진 프레임이면 반드시 full(걸러지기 전 패널)을 같이 넘긴다.
      걸러진 프레임 안에서 shift(-h) 를 하면 빠진 날을 건너뛰어 '20일 뒤'가 몇 달 뒤가 된다
      (2026-09-30 실측: 120일 시장평균이 +2.3% 가 아니라 +10.3% 로 부풀었다 — 신고가 판정에서
      2026-09-15 에 고친 것과 같은 종류의 버그). full 은 build_panel 의 결과처럼 RangeIndex 이고
      종목별로 날짜순 연속이어야 하며, df.index 는 그 라벨을 그대로 갖고 있어야 한다
      (apply_liquidity 가 보장한다)."""
    if full is None:
        g = df.groupby("Code", sort=False, observed=True)
        return g.close.transform(lambda s: s.shift(-horizon) / s - 1)
    if not isinstance(full.index, pd.RangeIndex) or full.index.start != 0 or full.index.step != 1:
        raise ValueError("full 은 0 부터 시작하는 RangeIndex 패널이어야 한다")
    codes = pd.factorize(full.Code)[0]
    c = full.close.to_numpy(dtype=float)
    i = df.index.to_numpy()
    j = np.minimum(i + horizon, len(full) - 1)
    ok = (i + horizon < len(full)) & (codes[j] == codes[i])
    return pd.Series(np.where(ok, c[j] / c[i] - 1, np.nan), index=df.index)


def panel_arrays(full: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """forward_close 가 쓰는 (종목 번호, 종가) 배열. 패널이 바뀔 때 한 번만 만들어 두고 쓴다 —
    forward_return 은 부를 때마다 600만 행을 factorize 한다(4개 기간에 0.07초)."""
    if not isinstance(full.index, pd.RangeIndex) or full.index.start != 0 or full.index.step != 1:
        raise ValueError("full 은 0 부터 시작하는 RangeIndex 패널이어야 한다")
    # 범주 번호(int16)·패널 종가 dtype(int32) 그대로 — factorize(int64)·float64 사본(97MB)을 상주시키지 않는다(2026-10-19).
    # forward_close 는 번호를 같은지만 보고, 종가는 고른 칸만 float 로 바뀐다(int32 → float64 는 정확하다).
    codes = full.Code.cat.codes.to_numpy() if isinstance(full.Code.dtype, pd.CategoricalDtype) else pd.factorize(full.Code)[0]
    return codes, full.close.to_numpy()


def forward_close(arrays: tuple[np.ndarray, np.ndarray], index, horizon: int) -> np.ndarray:
    """패널 행 라벨(index)마다 horizon **거래일** 뒤의 종가. 그만큼 뒤가 아직 없으면 NaN.
    forward_return(df, h, full) 과 같은 규칙이다 — 수익률은 이 값 ÷ 그날 종가 − 1 (테스트로 고정)."""
    codes, c = arrays
    i = np.asarray(index, dtype=np.int64)
    j = np.minimum(i + horizon, len(c) - 1)
    ok = (i + horizon < len(c)) & (codes[j] == codes[i])
    return np.where(ok, c[j], np.nan)


def forward_paths(df: pd.DataFrame, mask: pd.Series, horizon: int, full: pd.DataFrame | None = None):
    """신호 발생 건별로 이후 horizon 일의 고가·저가·종가 경로를 뽑는다.
    full 을 주면 경로를 걸러지기 전 연속 시계열에서 뽑는다(forward_return 참고)."""
    entries, HI, LO, CL, meta = [], [], [], [], []
    if full is not None:
        mask = mask.reindex(df.index).fillna(False).reindex(full.index, fill_value=False)
        hot = set(full.Code[mask.to_numpy()].unique())
        df = full[full.Code.isin(hot)]
    for code, sub in df.groupby("Code", sort=False, observed=True):
        sub = sub.sort_values("dt")
        m = mask.reindex(sub.index).fillna(False).to_numpy()
        if not m.any():
            continue
        hi, lo, cl = sub.high.to_numpy(), sub.low.to_numpy(), sub.close.to_numpy()
        dts = sub.dt.to_numpy()
        n = len(sub)
        for i in np.flatnonzero(m):
            if i + horizon >= n:
                continue
            entries.append(cl[i])
            HI.append(hi[i + 1:i + 1 + horizon])
            LO.append(lo[i + 1:i + 1 + horizon])
            CL.append(cl[i + 1:i + 1 + horizon])
            meta.append((dts[i], code))
    if not entries:
        return None
    return (np.array(entries), np.array(HI), np.array(LO), np.array(CL),
            pd.DataFrame(meta, columns=["dt", "Code"]))


def simulate(paths, stop_loss: float | None, take_profit: float | None) -> np.ndarray:
    """손절·익절을 적용한 실현수익(%). 같은 날 둘 다 닿으면 보수적으로 손절을 먼저 본다."""
    E, HI, LO, CL, _ = paths
    n, H = len(E), HI.shape[1]
    out = np.empty(n)
    slP = E * (1 - stop_loss / 100) if stop_loss else None
    tpP = E * (1 + take_profit / 100) if take_profit else None
    for j in range(n):
        r = None
        for t in range(H):
            if slP is not None and LO[j, t] <= slP[j]:
                r = -stop_loss
                break
            if tpP is not None and HI[j, t] >= tpP[j]:
                r = take_profit
                break
        out[j] = r if r is not None else (CL[j, -1] / E[j] - 1) * 100
    return out


def event_study(df: pd.DataFrame, mask: pd.Series, horizon: int,
                full: pd.DataFrame | None = None) -> dict:
    """시장중립 초과수익 기준 승률·평균·날짜클러스터 t. 중립화는 df 안에서만 이뤄진다.
    df 가 걸러진 프레임이면 full 을 넘겨 수익을 연속 시계열에서 잰다(forward_return)."""
    fwd = forward_return(df, horizon, full)
    # 쓰는 열은 dt·_f 둘뿐이다. df.assign(...).dropna() 는 28열 전체를 두 번 복사해
    # 순간 메모리가 +0.9GB 였다(실측 2026-09-29). 값·순서는 같다.
    tmp = pd.DataFrame({"dt": df.dt, "_f": fwd}).dropna(subset=["_f"])
    x = excess(tmp, "_f")
    m = mask.reindex(tmp.index).fillna(False).to_numpy()
    e = x[m]
    if len(e) < 30:
        return {"n": int(len(e))}
    by = x[m].groupby(tmp.dt[m]).mean()
    t = by.mean() / (by.std() / np.sqrt(len(by))) if len(by) > 2 else np.nan
    return {"n": int(len(e)), "초과평균": 100 * e.mean(), "초과중앙": 100 * e.median(),
            "시장대비승률": 100 * (e > 0).mean(), "t": t,
            "기준선승률": 100 * (x > 0).mean(), "기준선평균": 100 * x.mean()}


def stability(df: pd.DataFrame, mask: pd.Series, horizon: int, bins=5,
              full: pd.DataFrame | None = None) -> pd.DataFrame:
    """구간별 초과수익. 한 구간만 좋은 신호를 걸러내기 위한 것."""
    years = df.dt.dt.year
    edges = np.array_split(np.array(sorted(years.unique())), bins)
    rows = []
    for ys in edges:
        if len(ys) == 0:
            continue
        sub = df[years.isin(ys)]
        sm = mask.reindex(sub.index).fillna(False)
        r = event_study(sub, sm, horizon, full)
        rows.append({"구간": f"{ys[0]}-{ys[-1]}", "발생수": r.get("n", 0),
                     "초과평균": r.get("초과평균", np.nan), "승률": r.get("시장대비승률", np.nan),
                     "t": r.get("t", np.nan)})
    return pd.DataFrame(rows)


def backtest(df: pd.DataFrame, mask: pd.Series, horizon: int,
             stop_loss: float | None = 20.0, take_profit: float | None = None,
             cost: float = COST_ROUNDTRIP_PCT, full: pd.DataFrame | None = None) -> dict:
    """실현수익 기준 성과. peak 는 원본 프로그램식 '사후 꼭지'로, 비교용으로만 낸다.
    df 가 걸러진 프레임이면 full 을 넘긴다(forward_return)."""
    paths = forward_paths(df, mask, horizon, full)
    if paths is None:
        return {"n": 0}
    E, HI, LO, CL, meta = paths
    r = simulate(paths, stop_loss, take_profit)
    net = r - cost
    peak = (HI.max(axis=1) / E - 1) * 100
    trough = (LO.min(axis=1) / E - 1) * 100
    turns = 250 / horizon

    # ★ 시장중립 기준을 반드시 같이 낸다.
    #   절대수익만 보면 강세장에서는 아무 신호나 좋아 보인다('최고수익'과 같은 종류의 착시).
    #   진입일별 시장수익(검정 유니버스 동일가중 평균)을 빼서 초과분을 함께 보고한다.
    fwd = forward_return(df, horizon, full)
    mkt = (fwd.groupby(df.dt).transform("mean") * 100)
    mkt_by_date = mkt.groupby(df.dt).first()
    bench = meta.dt.map(mkt_by_date).to_numpy(dtype=float)
    ex = net - np.nan_to_num(bench, nan=0.0)

    return {"n": len(r), "승률": 100 * (net > 0).mean(), "평균수익": net.mean(),
            "중앙수익": np.median(net), "연환산": net.mean() * turns,
            "시장대비승률": 100 * (ex > 0).mean(), "초과수익": np.nanmean(ex),
            "연환산초과": np.nanmean(ex) * turns, "시장수익": np.nanmean(bench),
            "손익비": (net[net > 0].mean() / -net[net <= 0].mean()) if (net <= 0).any() else np.nan,
            "최대낙폭_평균": trough.mean(), "MDD_20퍼센트이하비율": 100 * (trough <= -20).mean(),
            "참고_사후꼭지": peak.mean(), "회전수": turns, "meta": meta, "수익배열": net,
            # 건별 배열. 종목 하나를 파볼 때 필요하다(stock_signal_log).
            # 집계값만 돌려주면 개별 신호가 어땠는지 되짚을 수가 없다.
            "초과배열": ex, "꼭지배열": peak, "시장배열": bench}


# 가상 종목(관심 그룹): 코드 → 시세 프레임(open·high·low·close·volume, index=dt) / 수급 프레임(flowtool.load 와 같은 모양).
# 차트(viewer)는 코드로 자료를 읽으므로, 여기에 등록하면 그룹도 종목과 똑같은 차트가 된다(groups.py, 2026-09-30).
VIRTUAL_PRICE: dict[str, pd.DataFrame] = {}
VIRTUAL_FLOW: dict[str, pd.DataFrame] = {}


def stock_history(code: str, since: str | None = None) -> pd.DataFrame | None:
    """차트용 단일 종목 OHLCV + 지표. 패널과 같은 정의를 쓰므로 화면과 검정이 어긋나지 않는다."""
    if code in VIRTUAL_PRICE:              # 관심 그룹 지수(groups.register) — 종목과 같은 지표를 입힌다
        d = VIRTUAL_PRICE[code][["open", "high", "low", "close", "volume"]].copy()
    else:
        p = PRICE / f"{code}.csv.gz"
        if not p.exists():
            return None
        d = pd.read_csv(p, usecols=["Date", "o", "h", "l", "c", "v"])
        d = d.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
        d["dt"] = pd.to_datetime(d.Date)
        d = d.set_index("dt").drop(columns=["Date"])
    o = add_indicators(d)
    o["cafe_juryeok"] = sig_cafe_juryeok(o)
    o["algkkagi"] = sig_algkkagi(o)
    if since:
        o = o[o.index >= pd.Timestamp(since)]
    return o


def latest_candidates(df: pd.DataFrame, mask: pd.Series, days: int = 5, cut=None, rank: bool = True) -> pd.DataFrame:
    """최근 며칠 안에 신호가 난 종목. cut(시작일)을 주면 days 대신 그날부터 본다(주·월 단위 기간 — window_cut).
    rank=False 면 변동성순위를 매기지 않고 비워 둔다 — 화면은 그 열을 쓰지 않는데, 30개월 구간이면 순위에만 0.1초가 든다."""
    # 거래일로 센다 — 예전엔 달력 days×2 일이라 '20거래일'이 실제로는 28거래일쯤이었다(2026-09-30 수리)
    cut = window_cut(df.dt.unique(), days) if cut is None else pd.Timestamp(cut)
    inwin = (df.dt >= cut).to_numpy()
    hit = inwin & mask.reindex(df.index).fillna(False).to_numpy(dtype=bool)
    if not hit.any():
        return pd.DataFrame()
    flow = [c for c in ("ind_disp",) if c in df.columns]             # 검색일의 개인 분산비율(화면 표의 한 칸)
    sub = df[["dt", "Code", "close", "val20", "vol60"] + flow][hit]        # 필요한 열만 — 넓은 프레임을 통째로 고르지 않는다
    vr = xrank(df[["dt", "vol60"]][inwin], "vol60").reindex(sub.index) if rank else np.nan
    out = sub.assign(변동성순위=vr).sort_values("dt", ascending=False)
    uni = universe().set_index("Code")
    out["종목명"] = out.Code.map(uni.Name)
    out["시장"] = out.Code.map(uni.Market)
    if not flow:
        out["ind_disp"] = np.nan
    return out[["dt", "Code", "종목명", "시장", "close", "val20", "vol60", "변동성순위", "ind_disp"]].rename(
        columns={"dt": "발생일", "Code": "종목코드", "close": "종가", "val20": "거래대금20일", "ind_disp": "개인분산"})


def state_candidates(df: pd.DataFrame, mask: pd.Series, full: pd.DataFrame, start_col: str, cut=None) -> pd.DataFrame:
    """상태 검색식의 후보, 종목당 한 줄(latest_candidates 와 같은 열 + 상태끝).
      cut 없음: **자료의 마지막 거래일에 상태가 참인 종목**.
      cut 있음: **cut 이후 하루라도 상태가 참이었던 종목**(기간 검색 — 지금은 상태가 끝났어도 나온다).
    발생일·종가는 그 상태가 시작된 날(마지막으로 참이던 날 이전의 마지막 start_col 사건)의 것이고, 상태끝은 마지막으로
    참이던 날이다(지금도 그 상태면 자료의 마지막 거래일). 시작일은 걸러지기 전 패널(full)에서 찾는다 — 유동성으로 걸러진
    df 에는 그날 행이 없을 수 있다. 시작 사건이 없으면(자료 첫머리부터 그 상태) 마지막으로 참이던 날 행을 쓴다.
    index 는 full 의 행 라벨이라 forward_return(cand, h, full) 에 그대로 넘길 수 있다."""
    end = df.dt.max()
    cut = end if cut is None else pd.Timestamp(cut)
    hit = df[(df.dt >= cut) & mask.reindex(df.index).fillna(False)]
    on = hit.groupby("Code", observed=True, sort=False).tail(1)            # 종목별 마지막으로 참이던 날
    if on.empty:
        return pd.DataFrame()
    last_on = pd.Series(on.dt.to_numpy(), index=on.Code.astype(str))
    keep = ["dt", "Code", "close", "vol60"]                                # 600만 행 패널에서 사건 행의 네 열만 뗀다(넓은 행 선택은 0.05초)
    ev = _narrow(full, keep, np.flatnonzero(full[start_col].fillna(False).to_numpy(dtype=bool)
                                            & full.Code.isin(on.Code).to_numpy()))
    ev = ev[ev.dt.to_numpy() <= ev.Code.astype(str).map(last_on).to_numpy()]
    start = ev.groupby("Code", observed=True, sort=False).tail(1)          # 패널은 종목별 날짜순 — 마지막 사건 = 그 상태의 시작
    out = pd.concat([start, on.loc[~on.Code.isin(start.Code).to_numpy(), keep]])
    key = out.Code.astype(str)
    out["상태끝"] = key.map(last_on)
    out["val20"] = key.map(pd.Series(on.val20.to_numpy(), index=on.Code.astype(str)))      # 거래대금은 마지막으로 참이던 날 것
    out["개인분산"] = (key.map(pd.Series(on.ind_disp.to_numpy(), index=on.Code.astype(str)))    # 개인 분산비율도 마지막으로 참이던 날 것
                   if "ind_disp" in on else np.nan)
    last = df[df.dt == end]
    out["변동성순위"] = key.map(pd.Series(xrank(last, "vol60").to_numpy(), index=last.Code.astype(str)))
    uni = universe().set_index("Code")
    out["종목명"] = out.Code.map(uni.Name)
    out["시장"] = out.Code.map(uni.Market)
    out = out.sort_values("dt", ascending=False, kind="stable")
    return out[["dt", "Code", "종목명", "시장", "close", "val20", "vol60", "변동성순위", "개인분산", "상태끝"]].rename(
        columns={"dt": "발생일", "Code": "종목코드", "close": "종가", "val20": "거래대금20일"})


# ────────────────────────────── 종목 검색 · 개별 분석

def search_stocks(query: str, limit: int = 50) -> pd.DataFrame:
    """종목명·코드로 찾는다. 이름은 부분일치(공백 무시), 코드는 앞자리 일치.

    시가총액 큰 순으로 돌려준다 — 이름 조각을 공유하는 종목이 워낙 많아
    ('삼성' 만 해도 십수 개) 순서가 없으면 목록이 쓸모없어진다.
    이름이 정확히 일치하면 시총과 무관하게 맨 위로 올린다('한국전력' vs '한국전력기술').
    """
    u = universe()
    q = str(query or "").strip()
    if not q:
        return u.head(0)
    key = q.replace(" ", "").lower()
    name = u.Name.astype(str).str.replace(" ", "", regex=False).str.lower()
    hit = name.str.contains(key, regex=False) | u.Code.astype(str).str.startswith(key)
    out = u[hit].copy()
    if out.empty:
        return out.reset_index(drop=True)
    out["_exact"] = (name[hit].to_numpy() == key).astype(int)
    out = out.sort_values(["_exact", "marcap_eok"], ascending=[False, False])
    return out.drop(columns=["_exact"]).head(limit).reset_index(drop=True)


def stock_snapshot(code: str) -> dict | None:
    """마지막 거래일 기준 한 종목의 상태.

    ★ 종목의 연속 시계열에서만 계산한다. 변동성·거래대금의 '순위'는 유니버스가
      정해져야 의미가 생기므로 여기 넣지 않고 stock_cross_rank() 로 분리했다.
    """
    o = stock_history(code)
    if o is None or len(o) < 60:
        return None
    last = o.iloc[-1]
    c = float(last.close)
    prev = float(o.close.iloc[-2])
    w = o.tail(252)
    hi, lo = float(w.close.max()), float(w.close.min())
    ma = {n: float(last[f"ma{n}"]) if pd.notna(last[f"ma{n}"]) else np.nan
          for n in (20, 60, 120, 200)}
    trio = [ma[20], ma[60], ma[120]]
    if all(np.isfinite(trio)) and trio[0] > trio[1] > trio[2]:
        align = "정배열 (20>60>120)"
    elif all(np.isfinite(trio)) and trio[0] < trio[1] < trio[2]:
        align = "역배열 (20<60<120)"
    else:
        align = "혼조"
    gap = lambda n: (c / ma[n] - 1) * 100 if np.isfinite(ma[n]) else np.nan
    return {"기준일": o.index[-1], "종가": c, "전일대비": (c / prev - 1) * 100,
            "거래대금20일": float(last.val20) if pd.notna(last.val20) else np.nan,
            "변동성60일": float(last.vol60) * 100 if pd.notna(last.vol60) else np.nan,
            "52주고가": hi, "52주저가": lo,
            "52주위치": (c - lo) / (hi - lo) * 100 if hi > lo else np.nan,
            "고가대비": (c / hi - 1) * 100 if hi > 0 else np.nan,
            "이평배열": align, "이격20": gap(20), "이격60": gap(60), "이격120": gap(120),
            "데이터시작": o.index[0], "행수": int(len(o))}


def flow_snapshot(code: str) -> dict | None:
    """수급 재고 상태 + 자기이력 백분위.

    분산비율의 절대값은 종목마다 기준이 달라 그대로 비교할 수 없다.
    '이 종목의 과거 대비 지금 어디냐'(백분위)가 읽을 수 있는 형태다.
    """
    f = flow_history(code)
    if f is None or f.empty:
        return None
    cur = f.iloc[-1]

    def pct(s, v):
        s = s.dropna()
        return float((s <= v).mean() * 100) if len(s) and np.isfinite(v) else np.nan

    return {"기준일": f.index[-1], "관측일수": int(len(f)),
            "개인분산": float(cur.개인분산), "세력분산": float(cur.세력분산), "SIG": float(cur.SIG),
            "개인분산_백분위": pct(f.개인분산, cur.개인분산),
            "세력분산_백분위": pct(f.세력분산, cur.세력분산),
            "SIG_백분위": pct(f.SIG, cur.SIG)}


def stock_cross_rank(df: pd.DataFrame, code: str) -> dict | None:
    """검정 유니버스 안에서 이 종목의 마지막 날 횡단면 위치.

    ★ df 는 유동성으로 좁힌 유니버스여야 한다. 주력 신호의 변동성 컷이 그 안에서
      매겨지므로, 화면에 띄우는 순위도 같은 모집단에서 매겨야 신호 판정과 어긋나지 않는다.
    """
    if df.empty:
        return None
    last = df.dt.max()
    day = df[df.dt == last]
    idx = day.index[day.Code == code]
    if len(idx) == 0:
        return None
    i = idx[0]
    vr = day.vol60.rank(pct=True)
    lr = day.val20.rank(pct=True)
    return {"기준일": last, "유니버스종목수": int(day.Code.nunique()),
            "변동성순위": float(vr.loc[i]) * 100 if pd.notna(vr.loc[i]) else np.nan,
            "거래대금순위": float(lr.loc[i]) * 100 if pd.notna(lr.loc[i]) else np.nan}


def stock_signal_log(df: pd.DataFrame, mask: pd.Series, code: str, horizon: int,
                     stop_loss: float | None = None, take_profit: float | None = None,
                     cost: float = COST_ROUNDTRIP_PCT) -> pd.DataFrame:
    """이 종목에서 신호가 난 날과 그 뒤 실현수익.

    ★ 시장 기준선은 반드시 df(=검정 유니버스) 전체에서 온다. 종목 하나로 좁힌 프레임에
      backtest 를 걸면 '시장'이 자기 자신이 되어 초과수익이 구조적으로 0 이 된다.
    ★ horizon 만큼의 미래가 아직 없는 최근 신호도 행으로 남기고 수익 칸만 비운다.
      빼버리면 '며칠 전에 신호가 났다'는 가장 쓸모 있는 사실이 화면에서 사라진다.
    """
    cols = ["발생일", "실현수익", "초과수익", "사후꼭지", "측정"]
    m = mask.reindex(df.index).fillna(False) & (df.Code == code)
    dates = signal_dates(df, m, code)
    if not dates:
        # 빈 결과도 컬럼은 갖춰 돌려준다 — 호출부가 dropna(subset=...) 로 바로 이어붙일 수 있어야 한다
        return pd.DataFrame(columns=cols)
    rows = {pd.Timestamp(d): {"발생일": pd.Timestamp(d), "실현수익": np.nan,
                              "초과수익": np.nan, "사후꼭지": np.nan} for d in dates}
    bt = backtest(df, m, horizon, stop_loss=stop_loss, take_profit=take_profit, cost=cost)
    if bt.get("n", 0):
        meta = bt["meta"]
        for i in range(len(meta)):
            d = pd.Timestamp(meta.dt.iloc[i])
            if d in rows:
                rows[d].update({"실현수익": float(bt["수익배열"][i]),
                                "초과수익": float(bt["초과배열"][i]),
                                "사후꼭지": float(bt["꼭지배열"][i])})
    out = pd.DataFrame(list(rows.values())).sort_values("발생일", ascending=False)
    out["측정"] = np.where(out.실현수익.notna(), f"{horizon}일 실현", "측정불가(기간부족)")
    return out.reset_index(drop=True)


def stock_all_signals(df: pd.DataFrame, code: str, horizon: int,
                      stop_loss: float | None = None, take_profit: float | None = None,
                      cost: float = COST_ROUNDTRIP_PCT, **sig_kwargs) -> pd.DataFrame:
    """이 종목에서 SIGNALS 의 신호 각각이 몇 번 났고 성적이 어땠는지.

    ★ 표본이 종목 하나치라 대개 한 자릿수다. 전략 판정이 아니라 이력 확인용이다
      (전략 판정은 전종목이 모인 '전략 검정' 탭에서만 의미가 있다).
    """
    rows = []
    if "full" not in sig_kwargs and isinstance(df.index, pd.RangeIndex) and df.index.start == 0:
        sig_kwargs["full"] = df                 # 걸러지지 않은 패널을 그대로 받았다 — 재진입 검색식이 그 위에서 센다
    for label, kind in SIGNALS.items():
        m = make_signal(df, kind, **sig_kwargs)
        log = stock_signal_log(df, m, code, horizon, stop_loss, take_profit, cost)
        if log.empty:
            rows.append({"신호": label, "발생수": 0, "측정가능": 0, "최근발생": pd.NaT,
                         "평균실현": np.nan, "평균초과": np.nan, "승률": np.nan,
                         "참고_사후꼭지": np.nan})
            continue
        meas = log.dropna(subset=["실현수익"])
        rows.append({"신호": label, "발생수": int(len(log)), "측정가능": int(len(meas)),
                     "최근발생": log.발생일.iloc[0],
                     "평균실현": float(meas.실현수익.mean()) if len(meas) else np.nan,
                     "평균초과": float(meas.초과수익.mean()) if len(meas) else np.nan,
                     "승률": 100 * float((meas.실현수익 > 0).mean()) if len(meas) else np.nan,
                     "참고_사후꼭지": float(meas.사후꼭지.mean()) if len(meas) else np.nan})
    return pd.DataFrame(rows)


def cross_bands(a: pd.Series, b: pd.Series) -> tuple[pd.Series, pd.Series]:
    """두 선 사이를 **어느 쪽이 위인지에 따라 다른 색으로** 칠하기 위한 보조 경계.

    plotly 의 fill='tonexty' 는 색이 하나뿐이라, 교차하는 두 선 사이를 부호별로 칠하려면
    구간을 둘로 쪼개야 한다.
        a 가 위인 구간 : min(a,b) → a   (높이 = a−b, b 가 위면 0)
        b 가 위인 구간 : a → max(a,b)   (높이 = b−a, a 가 위면 0)
    반환은 (min, max). 각각을 바닥 트레이스로 깔고 그 위에 a 를 얹으면 두 색이 완성된다.
    """
    lo = pd.Series(np.minimum(a.to_numpy(dtype=float), b.to_numpy(dtype=float)), index=a.index)
    hi = pd.Series(np.maximum(a.to_numpy(dtype=float), b.to_numpy(dtype=float)), index=a.index)
    return lo, hi


def cross_points(a: pd.Series, b: pd.Series) -> pd.DatetimeIndex:
    """a 와 b 가 자리를 바꾼 날. X 의 교차점이 곧 수급 주도권이 바뀐 지점이다.

    ★ 부호가 0 인 날(완전히 같은 값)은 건너뛴다. 0 을 부호 변화로 세면 한 번의 교차가
      두 번으로 잡힌다. 실측 개인/세력 분산은 1,400일에 23~26회 교차한다.
    """
    d = (a - b).dropna()
    if d.empty:
        return pd.DatetimeIndex([])
    nz = d[d != 0]
    if len(nz) < 2:
        return pd.DatetimeIndex([])
    sign = np.sign(nz.to_numpy())
    flip = np.flatnonzero(np.diff(sign) != 0) + 1
    return pd.DatetimeIndex(nz.index[flip])
