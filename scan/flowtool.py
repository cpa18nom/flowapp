"""영웅문4 수급분석툴 재현 — 13주체 매집/분산 엔진과 수급분석표.

원본 해부와 검증 근거는 `FLOWTOOL_SPEC.md`. 요약하면 원본은 투자자 13주체마다 같은 5열을 둔다.

    누적합계 = 최초일부터 당일까지의 순매수 누적
    최고저점 = 누적합계의 당일까지 최저      (expanding min)
    매집수량 = 누적합계 − 최고저점
    매집고점 = 매집수량의 당일까지 최고      (expanding max)
    분산비율 = 매집수량 / 매집고점           (1=재고 참 · 0=재고 빔)

**원본 그대로 쓰지 않은 것은 '창(window)' 하나뿐이다.** 원본은 MIN/MAX 범위가 주체마다
다르고(개인은 1258행 고정=확장창, 세력은 3행→1242·4행→1243 으로 양끝이 미는 이동창),
SUM 도 입력 1300행에서 끊겨 2870행까지 있는 데이터의 절반을 버린다. 복붙 중 앵커($)가
섞여 생긴 템플릿 사고라 판단해 **전구간 확장창으로 통일**했다. 알고리즘 자체는 그대로다.
원본의 창을 그대로 재현하면 캐시값과 소수 17자리까지 일치한다 — `tests/test_flowtool.py`.

세력 = 외국인 + 금융투자·보험·투신·기타금융·은행·연기금·사모펀드·국가·기타법인 (10주체).
원본 `수급분석표` 시트만 국가를 빼고 9주체로 더하는데(결함 #3), 엔진 시트·차트가 쓰는
10주체 정의를 채택했다. 시장 순매수 합=0 항등식 `-(개인+내외국인)` 과 2,865/2,869행 일치.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import core

# ─────────────────────────── 주체 정의 (원본 블록 순서 그대로)

FORCE_PARTS = ["frgn", "fnnc", "insrnc", "invtrt", "etcfnnc",
               "bank", "penfnd", "samo", "natn", "etccorp"]

BLOCKS: list[tuple[str, list[str]]] = [
    ("개인", ["ind"]),
    ("세력", FORCE_PARTS),
    ("외국인", ["frgn"]),
    ("기관", ["fnnc", "insrnc", "invtrt", "etcfnnc", "bank", "penfnd", "samo"]),  # 금융투자·보험·투신·기타금융·은행·연기금·사모펀드
    ("금융투자", ["fnnc"]),
    ("보험", ["insrnc"]),
    ("투신", ["invtrt"]),
    ("기타금융", ["etcfnnc"]),
    ("은행", ["bank"]),
    ("연기금", ["penfnd"]),
    ("사모펀드", ["samo"]),
    ("국가", ["natn"]),
    ("기타법인", ["etccorp"]),
    ("내외국인", ["natfor"]),
]

# 수급분석표·보유수량 차트가 쓰는 12주체 (세력 합계를 뺀 나머지)
PARTS12 = [n for n, _ in BLOCKS if n != "세력"]

COLS5 = ["누적합계", "최고저점", "매집수량", "매집고점", "분산비율"]

# 원본 수급분석표 7~24행. (몇 거래일 전부터, 몇 거래일 전까지) — 최신이 0.
PERIODS: list[tuple[str, int, int]] = [
    ("1주", 0, 5), ("2주", 5, 10), ("3주", 10, 15), ("4주", 15, 20),
    ("1달", 0, 20), ("2달", 20, 40), ("3달", 40, 60),
    ("1분기", 0, 60), ("2분기", 60, 120), ("3분기", 120, 180), ("4분기", 180, 240),
    ("1년", 0, 240), ("2년", 240, 480), ("3년", 480, 720), ("4년", 720, 960),
    ("5년", 960, 1200), ("6년", 1200, 1440), ("7년", 1440, 1680),
]

CHART_DAYS = 400          # 원본 차트 3개 모두 BU3:BU402 = 최근 400거래일


# ─────────────────────────── 엔진

def block(v: np.ndarray) -> dict[str, np.ndarray]:
    """한 주체의 5열. v 는 **오래된 것부터** 정렬된 순매수."""
    v = np.asarray(v, dtype=float)
    cum = np.cumsum(v)
    low = np.minimum.accumulate(cum)
    acc = cum - low
    pk = np.maximum.accumulate(acc)
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(pk > 0, acc / pk, np.nan)
    return dict(zip(COLS5, (cum, low, acc, pk, ratio)))


def load(code: str) -> pd.DataFrame | None:
    """flow 원본을 읽어 오래된 것부터 정렬. px_raw 는 전일대비 부호가 붙어 오므로 abs()."""
    if code in core.VIRTUAL_FLOW:          # 관심 그룹 수급(금액 합) — groups.register 가 등록한다
        return core.VIRTUAL_FLOW[code].copy()
    p = core.FLOW / f"{code}.csv.gz"
    if not p.exists():
        return None
    d = pd.read_csv(p, dtype={"dt": str})
    if d.empty:
        return None
    d["dt"] = pd.to_datetime(d.dt, format="%Y%m%d")
    d = d.sort_values("dt").set_index("dt")
    d["px"] = d.px_raw.abs()
    # 키움 수량은 그날의 주식 수 그대로다 — 병합·증자 전 수량을 지금 주식 수 기준으로 맞춘다(core.flow_factor).
    # 수량에는 배율을 곱하고 가격은 나눠 금액(수량×가격)은 그대로 둔다. 바뀐 적 없는 종목은 배율이 전부 1 이다.
    px = core.read_price(code)
    k = core.flow_factor(d.index, d.px_raw.to_numpy(), None if px is None else px.close)
    if (k != 1).any():
        qty = [c for c in d.columns if c not in ("px_raw", "pred_pre", "flu_rt", "px")]
        d[qty] = d[qty].to_numpy(dtype=float) * k[:, None]
        d["px"] = d.px / k
    return d


def engine(d: pd.DataFrame) -> pd.DataFrame:
    """원본 `자동 수급계산` 시트. 13주체 × 5열 + 세력 매집수량의 5·20·60일 평균."""
    out = {"현재가": d.px.to_numpy(dtype=float)}
    for name, parts in BLOCKS:
        v = d[parts].sum(axis=1).to_numpy(dtype=float)
        out[f"{name}_순매수"] = v
        for k, arr in block(v).items():
            out[f"{name}_{k}"] = arr
    f = pd.Series(out["세력_매집수량"], index=d.index)
    for n in (5, 20, 60):
        out[f"세력_{n}일추세"] = f.rolling(n, min_periods=1).mean().to_numpy()
    return pd.DataFrame(out, index=d.index)


# ─────────────────────────── 수급분석표

def report(d: pd.DataFrame, eng: pd.DataFrame, daily: int = 5) -> pd.DataFrame:
    """원본 `수급분석표` 시트. 최근 daily 일 + 기간버킷 + 현재/최대 보유량.

    원본은 3달·4분기의 평균단가만 한 행 더 당겨 쓴다(결함 #4). 같은 구간을 쓰도록 고쳤다.
    """
    n = len(d)
    rows: list[dict] = []

    def line(label, sl):
        r = {"구간": label,
             "평균단가": float(d.px.iloc[sl].mean()),
             "거래량": float(d.vol.iloc[sl].sum())}
        for name, parts in BLOCKS:
            r[name if name != "세력" else "세력합"] = float(
                d[parts].iloc[sl].to_numpy(dtype=float).sum())
        return r

    for i in range(min(daily, n)):                      # 최근 일자 (원본 2~6행)
        j = n - 1 - i
        r = line(d.index[j].strftime("%y/%m/%d"), slice(j, j + 1))
        rows.append(r)

    for label, a, b in PERIODS:                         # 기간버킷 (원본 7~24행)
        if a >= n:
            continue
        lo, hi = max(0, n - b), n - a
        rows.append(line(label, slice(lo, hi)))

    cur = {"구간": "현재 보유량", "평균단가": np.nan, "거래량": np.nan}
    mx = {"구간": "최대 보유량", "평균단가": np.nan, "거래량": np.nan}
    for name, _ in BLOCKS:
        key = name if name != "세력" else "세력합"
        cur[key] = float(eng[f"{name}_매집수량"].iloc[-1])
        mx[key] = float(eng[f"{name}_매집고점"].iloc[-1])
    rows += [cur, mx]
    return pd.DataFrame(rows).set_index("구간")


def positions(d: pd.DataFrame, eng: pd.DataFrame) -> pd.DataFrame:
    """HTS 수급분석 화면 상단 블록 — 주체별 보유 요약.

    행: 최대보유수량 · 현재보유수량 · 매집가능수량 · 분산비율 · 평균단가 · 승패
    평균단가는 **매집 구간의 거래대금 가중평균**이다. 매집 구간은 누적순매수가
    역대최저(최고저점)를 찍은 날 다음부터 오늘까지 — 그 구간 순매수 합이 곧 현재보유수량이다.
    그래서 Σ(순매수×종가) / 현재보유수량 이 그 구간에 실어 놓은 평균 매입가가 된다.
    """
    px = d.px.to_numpy(dtype=float)
    last_px = float(px[-1])
    out: dict[str, dict] = {}
    for name, _ in BLOCKS:
        v = eng[f"{name}_순매수"].to_numpy(dtype=float)
        cum = eng[f"{name}_누적합계"].to_numpy(dtype=float)
        low = eng[f"{name}_최고저점"].to_numpy(dtype=float)
        cur = float(eng[f"{name}_매집수량"].iloc[-1])
        mx = float(eng[f"{name}_매집고점"].iloc[-1])
        # 매집 시작 = 마지막으로 누적이 역대최저에 닿은 날. 그 다음날부터가 매집 구간.
        at_low = np.flatnonzero(cum <= low + 1e-9)
        start = int(at_low[-1]) + 1 if len(at_low) else 0
        seg_v, seg_p = v[start:], px[start:]
        avg = float((seg_v * seg_p).sum() / cur) if cur > 0 else np.nan
        out[name if name != "세력" else "세력합"] = {
            "최대보유수량": mx,
            "현재보유수량": cur,
            "매집가능수량": mx - cur,
            "분산비율": cur / mx if mx > 0 else np.nan,
            "평균단가": avg,
            "승패": ("승리" if last_px > avg else "패배") if np.isfinite(avg) else "—",
        }
    return pd.DataFrame(out)


def snapshot(eng: pd.DataFrame) -> pd.DataFrame:
    """13주체 현재 상태 한 장 — 분산비율·매집수량·매집고점·자기이력 백분위."""
    rows = []
    for name, _ in BLOCKS:
        s = eng[f"{name}_분산비율"].dropna()
        cur = float(s.iloc[-1]) if len(s) else np.nan
        rows.append({"주체": name,
                     "분산비율": cur,
                     "백분위": float((s < cur).mean() * 100) if len(s) else np.nan,
                     "매집수량": float(eng[f"{name}_매집수량"].iloc[-1]),
                     "매집고점": float(eng[f"{name}_매집고점"].iloc[-1]),
                     "누적순매수": float(eng[f"{name}_누적합계"].iloc[-1])})
    return pd.DataFrame(rows).set_index("주체")
