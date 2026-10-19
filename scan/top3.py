"""오늘의 3종목 — 검색식에 걸린 종목 가운데 보유 기간별로 1개씩(단기 3일 · 스윙 1주 · 중기 1달) 규칙으로 고른다 (사용자 요청 2026-10-07).

  재료   flowapp.pick_candidates(day) 가 주는 카드(검색식·확률 모델 백분위·차트 지표·수급). 이 모듈은 flowapp 을 모른다.
  점수   보유 기간마다 **이 저장소에서 검정한 수치**로만 점수를 매긴다(근거는 카드에 한 줄씩 적는다):
         - 검색식의 그 기간 오른 비율(flowapp.WIN_RATES, 2025년 이후 · 1주/2주/3주/4주/2개월/3개월) — 50% 를 넘는 만큼 가점
         - 확률 모델 백분위(winmodel, 40일 뒤 상승 확률 — 중기에 크게, 단기에 작게)
         - 차트: ATR(낮을수록 — winmodel 핵심 변수) · 20일선 이격 3~8% 좋고 10%↑ 나쁨(strategy) · 종가 위치(윗꼬리 — minute_study) · 거래량 배수 · 이평 정배열 · 52주 고가권
         - 수급: 개인 분산 ≤30%(winners) · 외인/기관 5일 순매수(패널 v14 조건)
         - 시장 상승국면(regime)
  고르기 기간마다 점수 1등. 한 종목은 한 기간에만(단기 → 스윙 → 중기 순으로 뺀다). 점수가 MIN_SCORE 아래면 그 기간은 「해당 없음」.
  청산   검정 결론(strategy·confirm)대로 **기한 매도**: 단기 3거래일 · 스윙 5거래일 · 중기 20거래일 뒤 종가. 손절은 승률을 깎았다 — 무효선만 적는다.
  기록   ~/.local/state/flowapp/top3.json — 날짜별 3장. 지난 카드는 다음 날 시가 진입 → 기한 종가 청산 수익률을 실제 봉으로 적는다(실적표).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

STATE = Path(os.environ.get("FLOWAPP_TOP3_STATE", Path.home() / ".local" / "state" / "flowapp" / "top3.json"))
HORIZONS = (("단기", 3, "3거래일"), ("스윙", 5, "5거래일(1주)"), ("중기", 20, "20거래일(1달)"))
HZ_IDX = {"단기": (0,), "스윙": (0, 1), "중기": (3, 4)}        # WIN_RATES 의 (1주,2주,3주,4주,2개월,3개월) 중 기간에 맞는 칸
MIN_SCORE = 1.0
KEEP_DAYS = 400                                  # 검증(중기 90일 + 보유 20거래일)이 끝날 때까지 지우지 않는다
VALID_DAYS = {"단기": 30, "스윙": 30, "중기": 90}   # 검증 창(달력일, 첫 카드 날부터) — 사용자 요청 2026-10-07 "1·2번 한 달, 3번 세 달"


def _f(x, default=float("nan")) -> float:
    try:
        v = float(x)
        return v if v == v else default
    except (TypeError, ValueError):
        return default


def rates_for(c: dict, win_rates: dict, idx: tuple[int, ...]) -> tuple[float, str] | None:
    """후보가 걸린 검색식들 가운데 그 기간 오른 비율이 가장 높은 것 → (비율, 검색식 코드)."""
    best = None
    for k in c.get("kind_codes") or []:
        wr = win_rates.get(k)
        if not wr:
            continue
        r = max(wr[1][i] for i in idx)
        if best is None or r > best[0]:
            best = (float(r), k)
    return best


def score(c: dict, win_rates: dict) -> dict[str, tuple[float, list[str]]]:
    """기간별 (점수, 근거 줄). 근거 줄은 '+1.0 …' 꼴로 점수와 이유를 같이 적는다."""
    pct, atr, g20 = _f(c.get("pct")), _f(c.get("atr")), _f(c.get("g20"))
    near, align, regime = _f(c.get("near52")), _f(c.get("align"), 0.0), _f(c.get("regime"), 0.0)
    clv, vr, osc_d = _f(c.get("clv")), _f(c.get("vr")), _f(c.get("osc_d"))
    fl = c.get("flow") or {}
    ind, frgn, orgn = _f(fl.get("ind_disp")), _f(fl.get("frgn5")), _f(fl.get("orgn5"))
    out = {}
    for h, _, _ in HORIZONS:
        pts: list[tuple[float, str]] = []
        r = rates_for(c, win_rates, HZ_IDX[h])
        if r:
            pts.append(((r[0] - 50) / 5, f"검색식의 {'1주' if h != '중기' else '1~2달'} 오른 비율 {r[0]:.0f}% (2025년 이후)"))
        if h == "단기":
            if clv == clv:
                pts.append((1.0, f"종가가 당일 고가 근처(위치 {100 * clv:.0f}%) — 윗꼬리 없음") if clv >= 0.7 else
                           (-1.0, f"윗꼬리 봉(종가 위치 {100 * clv:.0f}%) — 신호일 윗꼬리는 뒤가 나빴다") if clv <= 0.4 else (0.0, ""))
            if vr == vr:
                pts.append((1.0, f"거래량 전일까지 20일 평균의 {vr:.1f}배 — 적당히 실림") if 1.0 <= vr <= 3.0 else
                           (-1.0, f"거래량 {vr:.1f}배 — 과열(급등일 뒤는 평균이 나빴다)") if vr > 5 else (0.0, ""))
            if g20 == g20:
                pts.append((1.0, f"20일선 이격 {g20:+.1f}% — 3~8% 구간") if 3 <= g20 <= 8 else
                           (-1.0, f"20일선 이격 {g20:+.1f}% — 10% 넘게 벌어짐") if g20 > 10 else (0.0, ""))
            if osc_d == osc_d and osc_d > 0:
                pts.append((0.5, "일봉 오실레이터가 커지는 중"))
            if pct == pct and pct >= 70:
                pts.append((1.0, f"확률 모델 백분위 {pct:.0f}"))
            if (frgn == frgn and frgn > 0) or (orgn == orgn and orgn > 0):
                pts.append((0.5, "외국인 또는 기관 5일 순매수"))
        elif h == "스윙":
            if ind == ind:
                pts.append((1.0, f"개인 분산 {100 * ind:.0f}% ≤ 30% (수급 조건)") if ind <= 0.30 else (-1.0, f"개인 분산 {100 * ind:.0f}% > 30%"))
            if (frgn == frgn and frgn >= 0.05) or (orgn == orgn and orgn >= 0.01):
                pts.append((0.5, "외국인 5일 순매수 ≥ 거래량 5% 또는 기관 ≥ 1%"))
            if pct == pct and pct >= 60:
                pts.append((1.0, f"확률 모델 백분위 {pct:.0f}"))
            if align:
                pts.append((0.5, "이동평균 정배열(20 > 60 > 120)"))
            if g20 == g20 and g20 > 10:
                pts.append((-1.0, f"20일선 이격 {g20:+.1f}% — 이미 많이 오른 자리"))
            if atr == atr and atr > 4:
                pts.append((-0.5, f"ATR {atr:.1f}% — 변동성 높음"))
        else:
            if pct == pct:
                pts.append(((pct - 50) / 10, f"확률 모델 백분위 {pct:.0f} (40일 뒤 상승 확률 — 과거 같은 검색식 신호 중 위치)"))
            if regime:
                pts.append((1.0, "시장 상승국면(유니버스 지수가 120일 평균 위)"))
            if atr == atr:
                pts.append((1.0, f"ATR {atr:.1f}% — 승률형 신호 중앙(3.4%) 이하") if atr <= 3.4 else
                           (-1.0, f"ATR {atr:.1f}% — 변동성 높음(모델 핵심 변수)") if atr > 4 else (0.0, ""))
            if near == near and near >= 0.95:
                pts.append((0.5, f"52주 최고 대비 {100 * near:.0f}% — 고가권"))
            if align:
                pts.append((0.5, "이동평균 정배열(20 > 60 > 120)"))
            if ind == ind and ind <= 0.30:
                pts.append((0.5, f"개인 분산 {100 * ind:.0f}% ≤ 30%"))
        pts = [(p, w) for p, w in pts if w]
        total = round(sum(p for p, _ in pts), 2)
        out[h] = (total, [f"{p:+.1f} {w}" for p, w in pts])
    return out


def plan(h: str, c: dict, final: bool) -> dict:
    days = {x[0]: x[1] for x in HORIZONS}[h]
    low = _f(c.get("low"))
    entry = ("내일 09:30 이후 매수 — 시가 갭이 +1% 넘으면 패스. 종가가 오늘 종가 아래로 내려와 있으면 사지 않는다." if final else
             "오늘 15:20 이후 종가 무렵 매수(마감까지 신호가 남아 있을 때). 못 사면 내일 09:30 이후 — 갭 +1% 넘으면 패스.")
    if h == "단기":
        hold = f"{days}거래일 뒤 종가 매도(기한 매도). 매수 다음 날 종가가 매수가 아래면 그날 정리(신호 뒤 3일째 신호가 아래는 뒤가 나빴다)."
    elif h == "스윙":
        hold = f"{days}거래일 뒤 종가 매도. 중간에 20일선을 종가로 깨면 그날 정리."
    else:
        hold = f"{days}거래일 뒤 종가 매도 · 손절·익절 없음(검정: 손절은 승률을 깎고 기한 매도가 최선) · 계좌의 1/10 이내."
    stop = f"무효선 = 신호일 저가 {low:,.0f}원 종가 이탈 — 그러면 전제가 깨진 것으로 보고 정리" if low == low else ""
    return {"days": days, "entry": entry, "hold": hold, "stop": stop}


def choose(cands: list[dict], win_rates: dict, final: bool) -> dict[str, dict | None]:
    """기간마다 1등을 고른다(단기→스윙→중기 순, 한 종목은 한 기간에만). 점수가 MIN_SCORE 아래면 None."""
    scored = [(c, score(c, win_rates)) for c in cands]
    taken: set[str] = set()
    out: dict[str, dict | None] = {}
    for h, _, label in HORIZONS:
        fresh = [(s[h][0], _f(c.get("pct"), 0.0), c, s[h][1]) for c, s in scored if c["code"] not in taken]
        pool = fresh if fresh else [(s[h][0], _f(c.get("pct"), 0.0), c, s[h][1]) for c, s in scored]   # 후보가 모자라면 같은 종목을 다른 기간에도 둔다
        pool.sort(key=lambda x: (x[0], x[1]), reverse=True)
        if not pool or pool[0][0] < MIN_SCORE:
            out[h] = None
            continue
        sc, _, c, why = pool[0]
        taken.add(c["code"])
        out[h] = {**{k: c.get(k) for k in ("code", "name", "price", "low", "ret", "kinds", "kind_codes", "pct", "atr", "g20", "near52", "theme", "warn")},
                  "horizon": h, "hold_label": label, "score": sc, "why": why + ([] if fresh else ["(후보가 모자라 다른 기간과 같은 종목)"]),
                  **plan(h, c, final), "others": [f"{x[2]['name']}({x[0]:+.1f})" for x in pool[1:4]]}
    return out


# ────────────────────────────── 기록

def load_state() -> dict:
    try:
        s = json.loads(STATE.read_text(encoding="utf-8"))
        return s if isinstance(s, dict) and isinstance(s.get("days"), dict) else {"days": {}}
    except (OSError, ValueError):
        return {"days": {}}


def save_state(st: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    tmp.replace(STATE)


def record(day: str, picks: dict[str, dict | None], final: bool, at: str, n_cands: int) -> dict:
    st = load_state()
    st["days"][day] = {"at": at, "final": final, "n": n_cands, "picks": picks}
    keys = sorted(st["days"])
    for k in keys[:-KEEP_DAYS]:
        st["days"].pop(k, None)
    save_state(st)
    return st


def realized(card: dict, bars, day: str) -> dict | None:
    """지난 카드의 실적: 신호일 다음 거래일 시가에 사서 기한(days) 뒤 종가에 판 수익률. bars = 그 종목 일봉(DataFrame, index=dt, open/high/low/close).
    아직 기한이 안 됐으면 마지막 종가 기준 '진행 중'. 보유 중 최고·최저(종가 기준)도 적는다."""
    if bars is None or len(bars) == 0:
        return None
    import pandas as pd
    d0 = pd.Timestamp(day)
    after = bars[bars.index > d0]
    if after.empty:
        return None
    entry = float(after.open.iloc[0])
    if entry <= 0:
        return None
    n = int(card.get("days") or 0)
    done = len(after) >= n
    held = min(len(after), n) if n else len(after)
    path = after.close.iloc[:held].astype(float)
    last = float(path.iloc[-1])
    return {"entry": entry, "entry_day": f"{after.index[0]:%Y-%m-%d}", "exit": last, "done": done, "held": held,
            "exit_day": f"{after.index[held - 1]:%Y-%m-%d}", "ret": last / entry - 1,
            "max_ret": float(path.max() / entry - 1), "min_ret": float(path.min() / entry - 1),
            "path": [round(float(x) / entry - 1, 4) for x in path]}


def track_rows(state: dict, bars_fn, bench_fn=None) -> list[dict]:
    """모든 카드의 추적 행(날짜 내림차순). bars_fn(code) → 일봉, bench_fn(entry_day, exit_day) → 같은 기간 시장(유니버스 평균) 수익률 또는 None."""
    rows = []
    for day in sorted(state.get("days", {}), reverse=True):
        e = state["days"][day]
        for h, _, label in HORIZONS:
            c = (e.get("picks") or {}).get(h)
            if not c:
                continue
            r = realized(c, bars_fn(c["code"]), day)
            row = {"day": day, "horizon": h, "label": label, "code": c["code"], "name": c["name"], "price": c["price"], "score": c["score"],
                   "days": c["days"], "final": bool(e.get("final"))}
            if r:
                row.update(r)
                if bench_fn:
                    try:
                        row["bench"] = bench_fn(r["entry_day"], r["exit_day"])
                    except Exception:                        # noqa: BLE001
                        row["bench"] = None
            rows.append(row)
    return rows


def validation(rows: list[dict], today) -> dict[str, dict]:
    """기간별 검증 집계: 창(달력일) · 경과 · 건수 · 확정 · 승률 · 평균 · 시장 대비 · 최고/최저 평균."""
    import pandas as pd
    today = pd.Timestamp(today)
    out = {}
    for h, _, label in HORIZONS:
        hr = [r for r in rows if r["horizon"] == h]
        if not hr:
            out[h] = {"label": label, "window": VALID_DAYS[h], "start": None, "elapsed": 0, "n": 0}
            continue
        start = pd.Timestamp(min(r["day"] for r in hr))
        elapsed = int((today - start).days)
        done = [r for r in hr if r.get("done")]
        live = [r for r in hr if "ret" in r and not r.get("done")]
        rets = [r["ret"] for r in done]
        bench = [r["bench"] for r in done if r.get("bench") is not None]
        out[h] = {"label": label, "window": VALID_DAYS[h], "start": f"{start:%Y-%m-%d}", "elapsed": elapsed,
                  "end": f"{start + pd.Timedelta(days=VALID_DAYS[h]):%Y-%m-%d}",
                  "n": len(hr), "n_done": len(done), "n_live": len(live),
                  "win": (sum(1 for x in rets if x > 0) / len(rets)) if rets else None,
                  "avg": (sum(rets) / len(rets)) if rets else None,
                  "bench": (sum(bench) / len(bench)) if bench else None,
                  "live_avg": (sum(r["ret"] for r in live) / len(live)) if live else None,
                  "max_avg": (sum(r["max_ret"] for r in done) / len(done)) if done else None,
                  "min_avg": (sum(r["min_ret"] for r in done) / len(done)) if done else None}
    return out


def render_text(day: str, picks: dict[str, dict | None]) -> str:
    lines = [f"📌 오늘의 3종목 {day}"]
    for h, _, label in HORIZONS:
        c = picks.get(h)
        if not c:
            lines.append(f"[{h} {label}] 해당 없음")
            continue
        lines.append(f"[{h} {label}] {c['name']} {c['code']} {c['price']:,.0f}원 · 점수 {c['score']:+.1f}")
        lines.append("  " + " / ".join(c["why"][:3]))
        lines.append(f"  매수 {c['entry']}")
        lines.append(f"  보유 {c['hold']}")
    lines.append("규칙이 고른 종목과 근거이지 매수 추천이 아닙니다. 판단과 책임은 보는 사람에게 있습니다.")
    return "\n".join(lines)
