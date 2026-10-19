"""수급분석 뷰어 — Streamlit 없이 혼자 도는 HTML 한 장을 만든다.

서버도 포트도 없다. `flowchart 삼성전자` 하면 파일 하나가 나오고 브라우저가 그걸 연다.
화면 구성은 HTS 수급분석 창을 따랐다.

  상단  캔들 + MA20/60/120
  하단  주체별 **매집수량**(현재보유수량) 추이 — 원본 차트의 아래 패널
  표①  주체별 보유 요약 (최대보유·현재보유·매집가능·분산비율·평균단가·승패)
  표②  기간별 순매수 (일별·주별·월별·분기별·년별) — 원본 `수급분석표` 시트

분산비율 = 현재보유수량 / 최대보유수량. HTS 화면의 숫자를 역산해 같은 식임을 확인했다
(HMM 2024-05-31: 개인 56,734,783/113,160,546 = 50%, 세력합 93%, 외국인 84%, 연기금 100%).
"""
from __future__ import annotations

import html
import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

import core
import flowtool as ft

# 매집수량 계열 — 명확한 색상 구분
# 매집주체 색 — 서로 멀리 떨어진 진한 색(2026-10-06 사용자 "주체 구분이 어렵다"): 파랑·빨강·갈색빛 주황·초록·보라. 선 끝에 이름표도 붙인다(chart.js).
PANEL = [
    ("개인", "#1d4ed8"),           # 파랑
    ("세력합", "#dc2626"),         # 빨강
    ("외국인", "#b45309"),         # 진한 주황(갈색빛)
    ("연기금", "#15803d"),         # 진한 초록
    ("기관", "#7e22ce"),           # 보라
]
# 분산선 색상 (더 명확하게 구분)
BLUE, RED = "#0043b8", "#d62728"
BLUE_F, RED_F = "rgba(0,67,184,0.22)", "rgba(214,39,40,0.22)"
# 이평선 색상 (분산·매집수량과 구별)
MA = [(20, "#ff7f0e"), (60, "#8c564b"), (120, "#666")]
BB40_COLOR = "#0a8f8f"       # 상한선(대표가 40일·2σ) — 저자 화면처럼 점선으로 그린다(chart.js)
# 가격 칸 보조지표 4종(사용자 요청 2026-10-07, 일봉·주봉·60분봉 공통). 이름 → (색: 형광색, 설명). 값은 indicators() 가 낸다.
IND = {"볼린저상한": ("#c026ff", "볼린저밴드 Period 220 · D1 1 — 상한선만(종가 220봉 평균 + 1σ)"),
       "일목기준선": ("#39ff14", "일목균형표 9·26·52 — 기준선만(26봉 최고·최저의 중간)"),
       "엔벨로프상단": ("#ff2d2d", "Envelope Period 60 · 5% — 저항선만(60봉 평균 × 1.05)"),
       "VWAP": ("#1e90ff", "VWAP 기간 120 — 120봉 (종가×거래량) 합 ÷ 거래량 합")}
IND_BB_N, IND_BB_K, IND_KIJUN, IND_ENV_N, IND_ENV_PCT, IND_VWAP_N = 220, 1.0, 26, 60, 0.05, 120


def indicators(full: pd.DataFrame) -> dict[str, pd.Series]:
    """IND 의 네 선. full = 그 봉 단위의 전 구간 OHLCV(일·주봉은 리샘플한 봉, 60분봉은 분봉). 봉이 모자라면 NaN."""
    c, h, l = full.close, full.high, full.low
    out = {"볼린저상한": c.rolling(IND_BB_N).mean() + IND_BB_K * c.rolling(IND_BB_N).std(ddof=0),
           "일목기준선": (h.rolling(IND_KIJUN).max() + l.rolling(IND_KIJUN).min()) / 2,
           "엔벨로프상단": c.rolling(IND_ENV_N).mean() * (1 + IND_ENV_PCT)}
    if "volume" in full:
        v = full.volume
        out["VWAP"] = (c * v).rolling(IND_VWAP_N).sum() / v.rolling(IND_VWAP_N).sum().replace(0, np.nan)
    return out

CSS = """
:root{--bg:#fff;--fg:#1a1a1a;--line:#d8dde3;--head:#eef2f7;--muted:#6b7280;
      --pos:#c0392b;--neg:#1f5fd0;--band:#f7f9fc}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){
      --bg:#14171c;--fg:#e6e8eb;--line:#2b313a;--head:#1d222a;--muted:#9aa3ad;
      --pos:#ff6b5e;--neg:#6ba8ff;--band:#181c22}}
*{box-sizing:border-box}
body{margin:0;padding:16px;background:var(--bg);color:var(--fg);
     font:13px/1.5 -apple-system,"Segoe UI","Malgun Gothic",sans-serif}
h1{font-size:17px;margin:0 0 2px}
h2{font-size:14px;margin:22px 0 6px;font-weight:600}
.sub{color:var(--muted);font-size:12px;margin-bottom:12px}
.note{color:var(--muted);font-size:11.5px;margin:6px 0 0}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{border:1px solid var(--line);padding:3px 7px;text-align:right;white-space:nowrap}
th{background:var(--head);font-weight:600;text-align:center;position:sticky;top:0}
td.k,th.k{text-align:left;background:var(--head);font-weight:600;position:sticky;left:0}
tbody tr:nth-child(even){background:var(--band)}
.pos{color:var(--pos)} .neg{color:var(--neg)}
.wrap{overflow-x:auto;border:1px solid var(--line);border-radius:6px}
.tabs{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 8px}
.pbtn.fold{margin-left:auto}
.foldwrap.folded{overflow:hidden}
.pbtn{appearance:none;cursor:pointer;font:inherit;font-size:12.5px;padding:5px 16px;
      border:1px solid var(--line);background:var(--bg);color:var(--muted);border-radius:6px}
.pbtn:hover{border-color:var(--neg);color:var(--fg)}
.pbtn.on{background:var(--neg);border-color:var(--neg);color:#fff;font-weight:600}
.chartbox{display:none}
.chartbox.on{display:block}
/* 범례는 차트 밖 HTML 이다 — 차트 안에 두면 줄이 늘 때 봉 위로 넘쳐 겹친다(2026-09-30) */
.lg{display:flex;flex-wrap:wrap;gap:2px 4px;margin:0 0 2px;position:sticky;left:0}   /* 좁은 화면에서 차트를 옆으로 밀어도 범례는 제자리 */
.lg button{appearance:none;border:0;background:none;cursor:pointer;font:inherit;font-size:11.5px;color:var(--fg);
           padding:3px 8px;border-radius:6px;white-space:nowrap}
.lg button:hover{background:var(--head)}
.lg .sw{display:inline-block;width:18px;border-top:2px solid;vertical-align:middle;margin-right:6px}
.lg .sw.mk{width:0;height:0;border:5px solid transparent;border-top:8px solid;border-bottom:0}
.lg button.off{opacity:.4;text-decoration:line-through}
.lg .lg-cap{font-size:11px;color:var(--muted);margin-right:6px;align-self:center;cursor:help}
@media(max-width:640px){body{padding:16px 16px}}
"""


def _fmt(x, kind="int"):
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return "—"
    if kind == "pct":
        return f"{x*100:,.0f}%"
    if kind == "sign":
        cls = "pos" if x > 0 else "neg" if x < 0 else ""
        return f'<span class="{cls}">{x:+,.0f}</span>' if cls else f"{x:,.0f}"
    return f"{x:,.0f}"


def _table(df: pd.DataFrame, kinds: dict[str, str] | None = None,
           row_kind: dict[str, str] | None = None) -> str:
    """DataFrame → HTML. kinds 는 컬럼별, row_kind 는 행별 표기(sign/pct/int)."""
    kinds, row_kind = kinds or {}, row_kind or {}
    head = "".join(f"<th>{html.escape(str(c))}</th>" for c in df.columns)
    body = []
    for idx, r in df.iterrows():
        cells = []
        for c in df.columns:
            k = row_kind.get(str(idx)) or kinds.get(str(c), "int")
            v = r[c]
            cells.append(f"<td>{v if isinstance(v, str) else _fmt(v, k)}</td>")
        body.append(f'<tr><td class="k">{html.escape(str(idx))}</td>{"".join(cells)}</tr>')
    return (f'<div class="wrap"><table><thead><tr><th class="k">구분</th>{head}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>')

# 주봉은 금요일 마감, 월봉은 월말. 한국장 주차 구분이 금요일 기준이다.
PERIOD_RULE = {"일": None, "주": "W-FRI", "월": "ME"}

# 칸 배치(2026-09-29 사용자 요청, 2026-10-02 캔버스로 옮김 — static/chart.js):
# ① 가격(+아래 띠) ② 거래대금 ③ 스토캐스틱 ④ 매집수량 ⑤~⑧ 재무(현금·매출·영업이익·순이익). 재무 선은 브라우저가 /api/dartlines 로 받는다.
DART_PANES = [  # (키, 제목, 색)
    ("cash", "현금 (분기말 잔액)", "#c58b22"),
    ("revenue", "매출", "#487cd0"),
    ("op_profit", "영업이익", "#c0392b"),
    ("net_income", "순이익", "#2b8b68"),
]
PERIODS_UI = [("d", "일봉"), ("w", "주봉"), ("m", "월봉")]
KEY2P = {"d": "일", "w": "주", "m": "월"}







def resample_ohlc(o: pd.DataFrame, rule: str) -> pd.DataFrame:
    """일봉 → 주봉·월봉. 이평선은 **리샘플한 종가 위에서 다시 계산한다**
    (주봉 MA20 = 20주 이동평균). 일봉 MA 를 리샘플하면 20일 평균을 주 단위로
    찍은 것이 되어 HTS 의 주봉 이평과 달라진다."""
    g = o.resample(rule)
    d = pd.DataFrame({"open": g.open.first(), "high": g.high.max(),
                      "low": g.low.min(), "close": g.close.last(),
                      "volume": g.volume.sum()}).dropna(subset=["close"])
    for n, _ in MA:
        d[f"ma{n}"] = d.close.rolling(n).mean()
    d["hh120"] = d.close.rolling(min(120, max(2, len(d) // 2))).max()
    return d


def resample_state(eng: pd.DataFrame, rule: str) -> pd.DataFrame:
    """누적·매집·분산은 **상태량**이라 합계·평균이 아니라 구간의 마지막 값을 쓴다."""
    return eng.resample(rule).last().dropna(how="all")


def _num(a, nd: int = 2) -> list:
    """숫자 배열 → JSON 목록. NaN·inf 는 null(차트에서 빈칸)."""
    a = np.asarray(a, dtype=float)
    r = np.round(a, nd)
    return [None if not np.isfinite(v) else (int(v) if nd == 0 else float(v)) for v in r]


def _days(idx) -> list[str]:
    return [t.strftime("%Y-%m-%d") for t in pd.DatetimeIndex(idx)]


def chart_data(code: str, d: pd.DataFrame, eng: pd.DataFrame, days: int | None = None,
               band_pct: float = 25.0, log_price: bool = True, period: str = "일",
               sig_dates: list | None = None, sig_label: str = "신호", ohlc: pd.DataFrame | None = None) -> dict:
    """일·주·월봉 차트의 **자료만** 만든다. 그리기는 브라우저의 static/chart.js(lightweight-charts, 캔버스)가 한다.
    (2026-10-02 plotly → lightweight-charts: 확대·이동이 프레임마다 SVG 3천 봉을 두 번 다시 그려 초당 약 10장이었다.)

    자료의 뜻은 예전 figure() 와 같다:
      · 자르지 않는다 — 수급 원본(eng)이 시작하는 날부터 전 구간. 주·월봉은 리샘플(W-FRI·ME), 이평선은 리샘플한 종가로 다시 센다.
      · 분산비율 띠(개인·세력)는 0~1 값 그대로 보내고, 가격 칸 아래 band 비율 안에 그린다(가격축과 따로 논다).
      · 신호(sig_dates)는 **일봉에만** — 주·월봉 한 봉은 여러 날을 덮어 발생일을 가리키지 못한다.
      · 스토캐스틱은 전 구간(리샘플한 봉)으로 센 뒤 화면 구간으로 자른다(EMA 워밍업).
    시간축은 봉 순서(거래일)라 주말·휴장일 빈칸이 원래 없다 — 예전의 rangebreaks(휴장일 목록)가 필요 없다."""
    tail = eng if not days else eng.tail(days)
    x0 = tail.index[0]
    ohlc = core.stock_history(code) if ohlc is None else ohlc      # build() 가 세 기간에 한 번만 센 것을 넘긴다(2026-10-07: 0.55→0.4초)
    o = ohlc.loc[ohlc.index >= x0] if ohlc is not None and len(ohlc) else None
    rule = PERIOD_RULE.get(period)
    if rule:
        if o is not None and len(o):
            o = resample_ohlc(o, rule)
        tail = resample_state(tail, rule)
    out: dict = {"code": code, "period": period, "log": bool(log_price),
                 "band": max(5.0, min(band_pct, 60.0)) / 100.0, "sig_label": sig_label,
                 "colors": {"ma": {str(n): c for n, c in MA}, "bb40": BB40_COLOR, "ind": {k: v[0] for k, v in IND.items()}, "flow": dict(PANEL), "blue": BLUE, "red": RED,
                            "blue_f": BLUE_F, "red_f": RED_F},
                 "dart": [] if code in core.VIRTUAL_PRICE else [{"key": k, "title": t, "col": c} for k, t, c in DART_PANES]}   # 그룹 지수엔 재무가 없다
    if o is not None and len(o):
        out["t"] = _days(o.index)
        out["o"], out["h"], out["l"], out["c"] = (_num(o[k], 2) for k in ("open", "high", "low", "close"))
        # 봉 수보다 긴 이평선은 전부 NaN 이다(월봉 70개의 MA120) — 범례만 남아 혼란스러우니 보내지 않는다
        out["ma"] = {str(n): _num(o[f"ma{n}"], 1) for n, _ in MA if o[f"ma{n}"].notna().any()}
        dv = (ohlc.volume * 1e6) if code in core.VIRTUAL_PRICE else core.turnover_series(code, ohlc)   # 그룹 지수 volume = 백만원 · 종목은 키움 실제 거래대금(없는 날은 종가×거래량)
        dv = dv.loc[dv.index >= x0]
        out["val"] = _num((dv.resample(rule).sum() if rule else dv).reindex(o.index) / 1e8, 2)
        full = resample_ohlc(ohlc, rule) if rule else ohlc
        # 상한선(식이형 화면 설정: 대표가 (고+저+종)/3 의 40봉 평균 + 2σ) — 검색식 bb40_combo 와 같은 함수. 주·월봉은 리샘플한 봉으로 센다.
        #   전 구간(full)으로 센 뒤 화면 구간으로 자른다 — 잘린 구간으로 세면 앞 39봉이 비고, 봉이 40개 안 되면 통째로 빈다(그때는 보내지 않는다).
        bb = core.bb40_upper(full).reindex(o.index)
        if bb.notna().any():
            out["bb40"] = _num(bb, 1)
        # 보조지표 4종(IND) — 전 구간(full)으로 센 뒤 화면 구간으로 자른다. 전부 NaN 이면(봉 부족) 보내지 않는다.
        out["ind"] = {k: _num(s.reindex(o.index), 1) for k, s in indicators(full).items() if s.reindex(o.index).notna().any()}
        st = core.stoch_lines(full.high, full.low, full.close).reindex(o.index)
        out["osc"], out["fd"], out["sd"] = _num(st.osc, 3), _num(st.fastD, 2), _num(st.slowD, 2)
        out["turn"] = (_days(o.index[o.stoch_turn_up.fillna(False).astype(bool)])
                       if period == "일" and "stoch_turn_up" in o else [])
        sd = []
        if sig_dates and period == "일":
            have = set(o.index)
            sd = [x for x in (pd.Timestamp(v) for v in sig_dates) if x in have]
        out["sig"] = _days(sd)
    else:                                                   # 시세 원본이 없으면 수급 원본의 종가 선으로
        out["t"], out["line"] = _days(tail.index), _num(tail.현재가, 2)
        out.update(ma={}, ind={}, val=[], osc=[], fd=[], sd=[], turn=[], sig=[])
    f = pd.DataFrame({"p": tail.개인_분산비율, "s": tail.세력_분산비율}, index=tail.index).dropna()
    out["bt"], out["bp"], out["bs"] = _days(f.index), _num(f.p, 4), _num(f.s, 4)
    out["ft"] = _days(tail.index)
    out["flow"] = {name: _num(tail[f"{'세력' if name == '세력합' else name}_매집수량"], 0) for name, _ in PANEL}
    return out


STATIC = Path(__file__).resolve().parent / "static"
CHART_CSS = """
.fc{position:relative;width:100%;min-width:640px}
.fc-grip{height:12px;margin:2px 0 6px;cursor:ns-resize;touch-action:none;border-radius:6px;position:relative}
.fc-grip::after{content:"";position:absolute;left:50%;top:4px;width:56px;height:4px;margin-left:-28px;border-radius:2px;background:var(--line)}
.fc-grip:hover::after,.fc-grip:active::after{background:var(--muted)}
.fc-info{min-height:18px;font-size:12px;color:var(--muted);margin:0 0 2px;font-variant-numeric:tabular-nums}
.fc-info b{color:var(--fg)}
.fc-date{position:absolute;top:2px;z-index:5;pointer-events:none;background:#3a3f47;color:#fff;font-size:11.5px;
         padding:2px 7px;border-radius:4px;white-space:nowrap;font-variant-numeric:tabular-nums}
.fc-flow-item{font-weight:700}.fc-flow-item i{display:inline-block;width:22px;border-top:3px solid;vertical-align:middle;margin-right:5px}
.fc-flow-item.off{opacity:.35}
/* 칸 제목 상자: 칸의 왼쪽 위 모서리에 딱 붙인다 — 바깥 두 변은 각지게, 안쪽 모서리만 둥글게(사용자 요청 2026-10-08) */
.fc-title{position:absolute;left:0;z-index:30;font-size:11px;color:var(--fg);background:color-mix(in srgb,var(--bg) 88%,transparent);
          padding:1px 6px 1px 4px;border-radius:0 0 5px 0;pointer-events:none}
.fc-title .fc-v{color:var(--muted)}
.fc-title .fc-pf{pointer-events:auto;cursor:pointer;font:inherit;font-size:10px;line-height:1;padding:1px 3px;margin-right:4px;border:1px solid var(--line);border-radius:3px;background:var(--surface,#fff);color:var(--fg);vertical-align:1px}
.fc-title .fc-pf:hover{border-color:var(--neg)}
/* 매집수량·분산비율 범례: 작은 한 줄 — 체크·선·이름·값(사용자 요청 2026-10-08) */
.fc-title.fc-flow-legend{display:flex!important;flex-wrap:nowrap;align-items:center;gap:2px;max-width:calc(100% - 84px);overflow:hidden;
  padding:1px 4px 1px 2px!important;border:solid var(--line);border-width:0 1px 1px 0;border-radius:0 0 6px 0;background:color-mix(in srgb,var(--bg) 92%,transparent)}
.fc-flow-legend>strong{display:inline-flex;align-items:center;font-size:11px;margin-right:3px;white-space:nowrap}
.fc-flow-legend .fc-flow-day{font-size:10.5px;margin-left:3px}
.fc-title.fc-flow-legend .fc-flow-item{pointer-events:auto;cursor:pointer;user-select:none;display:inline-flex;align-items:center;gap:3px;white-space:nowrap;
  margin:0;padding:0 5px;border-radius:4px;background:color-mix(in srgb,var(--line) 32%,transparent);font-size:11px;font-weight:600;line-height:18px}
.fc-title.fc-flow-legend .fc-flow-item i{width:12px;margin:0;border-top-width:2px}
.fc-title.fc-flow-legend input{margin:0;width:11px;height:11px;cursor:pointer;accent-color:#334155}
.fc-title.fc-flow-legend .fc-flow-item.off b{visibility:hidden}
.fc-flow-legend .fc-flow-all{pointer-events:auto;cursor:pointer;user-select:none;display:inline-flex;align-items:center;gap:3px}
.fc-title.fc-flow-legend .fc-flow-item b{min-width:3.6em;text-align:right;font-weight:700;font-variant-numeric:tabular-nums}
.fc-grip[hidden]{display:none}
@media(max-width:700px){.fc-title.fc-flow-legend{flex-wrap:wrap}}
"""

# 일·주·월봉 버튼. 차트는 그 탭을 **처음 열 때** 만든다(FC.show) — 안 보는 두 차트를 미리 그리지 않는다.
PERIOD_JS = """<script>
function showPeriod(k){
  document.querySelectorAll('.chartbox').forEach(function(d){d.classList.remove('on');});
  document.querySelectorAll('.pbtn').forEach(function(b){b.classList.remove('on');});
  document.getElementById('box-' + k).classList.add('on');
  document.getElementById('btn-' + k).classList.add('on');
  FC.show(k);
}
function showMinute(m){
  document.querySelectorAll('.chartbox').forEach(function(d){d.classList.remove('on');});
  document.querySelectorAll('.pbtn').forEach(function(b){b.classList.remove('on');});
  document.getElementById('box-mn').classList.add('on');
  document.getElementById('btn-mn' + m).classList.add('on');
  document.querySelectorAll('.chart-range button').forEach(function(b){b.disabled=true;});
  if (window.loadMinute) { window.loadMinute(m); }
}
function resetZoom(){                                  // 「⟲ 처음 크기」: 보고 있는 차트만 (더블클릭과 같음)
  var on=document.querySelector('.chartbox.on'); if(!on) return;
  if(on.id==='box-mn'){ if(window.resetMinute) window.resetMinute(); return; }
  FC.reset(on.id.slice(4));
}
// 보조지표(스토캐스틱·매집·재무) 접기 — 상태는 브라우저가 기억한다(사용자 요청 2026-09-30)
function foldLabel(){ var b=document.getElementById('btn-fold'), on=FC.fold();
  if(b){ b.textContent = on ? '보조지표 펼치기' : '보조지표 접기'; b.setAttribute('aria-pressed', on?'true':'false'); }
  var f=document.getElementById('btn-finance'); if(f){f.setAttribute('aria-pressed',String(FC.finance()));f.textContent=FC.finance()?'재무지표 접기':'재무지표 펼치기';} }
function lockLabel(){ var b=document.getElementById('btn-lock'); if(!b) return; var on=FC.locked();
  b.textContent = on ? '🔒 크기 고정됨' : '크기 고정'; b.setAttribute('aria-pressed', on?'true':'false'); }
function toggleLock(){ FC.toggleLock(); lockLabel(); }
function toggleFold(){ try{ localStorage.setItem('flowapp.fold', FC.fold()?'0':'1'); }catch(e){} foldLabel(); FC.rebuild(); }
function toggleFinance(){ try{ localStorage.setItem('flowapp.finance', FC.finance()?'0':'1');localStorage.setItem('flowapp.fold','0'); }catch(e){} foldLabel(); FC.rebuild(); }
function chartRange(months){
  var on=document.querySelector('.chartbox.on');if(!on||on.id==='box-mn')return;
  FC.range(on.id.slice(4),months);
  document.querySelectorAll('.chart-range button').forEach(function(b){b.setAttribute('aria-pressed',String(+b.dataset.months===months));});
}
</script>"""


def _scripts(inline_js: bool | str) -> str:
    """lightweight-charts + chart.js. True = 파일에 그대로 싣는다(서버 없이 도는 파일), 문자열 = 그 경로에서 받는다."""
    names = ("lwc.js", "chart.js")
    if inline_js is True:
        return "".join(f"<script>{(STATIC / n).read_text(encoding='utf-8')}</script>" for n in names)
    base = inline_js if isinstance(inline_js, str) and inline_js.endswith("/") else "/static/"
    return "".join(f'<script src="{base}{n}?v={(STATIC / n).stat().st_mtime_ns}"></script>' for n in names)


def build(code: str, name: str, out: Path | None = None, days: int | None = None,
          inline_js: bool | str = True, band_pct: float = 25.0, log_price: bool = True,
          period: str = "일", sig_dates: list | None = None,
          sig_label: str = "신호", body_only: bool = False,
          minute_panel: str | None = None) -> Path | str:
    d = ft.load(code)
    if d is None or len(d) < 60:
        raise SystemExit(f"{name}({code}) 수급 원본이 없거나 60행 미만입니다.")
    eng = ft.engine(d)
    pos = ft.positions(d, eng)
    rep = ft.report(d, eng)

    boxes, tabs = [], []
    first = next((k for k, p in PERIODS_UI if KEY2P[k] == period), "d")
    ohlc = core.stock_history(code)                                 # 세 기간이 같은 일봉+지표를 쓴다 — 한 번만 센다(2026-10-07)
    for key, label in PERIODS_UI:
        cd = chart_data(code, d, eng, days, band_pct=band_pct, log_price=log_price,
                        period=KEY2P[key], sig_dates=sig_dates, sig_label=sig_label, ohlc=ohlc)
        blob = json.dumps(cd, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
        on = " on" if key == first else ""
        boxes.append(f'<div class="chartbox{on}" id="box-{key}">'
                     f'<div class="lg" id="lg-{key}" role="group" aria-label="범례 — 누르면 선을 숨기거나 표시합니다"></div>'
                     f'<div class="fc-info" id="info-{key}" aria-live="off"></div>'
                     f'<div class="fc" id="plot-{key}" role="img" aria-label="{html.escape(name)} {label} 차트"></div>'
                     f'<script type="application/json" id="cd-{key}">{blob}</script></div>')
        tabs.append(f'<button class="pbtn{on}" id="btn-{key}" type="button" '
                    f'onclick="showPeriod(\'{key}\')">{label}</button>')
    if minute_panel:                     # 분봉을 같은 자리·같은 버튼 줄에(사용자 요청 2026-09-29)
        for m in (5, 20, 60):
            tabs.append(f'<button class="pbtn" id="btn-mn{m}" type="button" '
                        f'onclick="showMinute({m})">{m}분</button>')
        boxes.append(f'<div class="chartbox" id="box-mn">{minute_panel}</div>')
    tabs.append('<button class="pbtn fold" id="btn-fold" type="button" onclick="toggleFold()" '
                'title="스토캐스틱·매집수량·재무 칸을 접거나 폅니다">▲ 보조지표 접기</button>')
    tabs.append('<button class="pbtn" id="btn-lock" type="button" onclick="toggleLock()" aria-pressed="false" '
                'title="지금 칸 크기를 기본값으로 저장하고 크기 조절을 잠급니다 — 다시 누르면 잠금만 풉니다">크기 고정</button>')
    tabs.append('<button class="pbtn reset" id="btn-reset" type="button" onclick="resetZoom()" '
                'title="확대·이동 전 처음 크기로 (더블클릭과 같음)">⟲ 처음 크기</button>')
    tabs.insert(-1, '<button class="pbtn" id="btn-finance" type="button" onclick="toggleFinance()" aria-pressed="false">재무지표 펼치기</button>')
    ranges = ('<div class="chart-range" role="group" aria-label="차트 표시 기간"><span class="range-label">표시 기간</span>'
              + "".join(f'<button type="button" data-months="{m}" aria-pressed="{str(m == 12).lower()}" onclick="chartRange({m})">{t}</button>'
                        for m, t in ((3, "3개월"), (6, "6개월"), (12, "1년"), (36, "3년"), (0, "전체")))
              + '<span class="chart-hint">휠 확대 · 드래그 이동 · 더블클릭 전체</span></div>')
    chart = (f'<style>{CHART_CSS}</style><div class="tabs">{"".join(tabs)}</div>{ranges}{"".join(boxes)}'
             + _scripts(inline_js) + PERIOD_JS
             + f"<script>foldLabel(); lockLabel(); FC.show('{first}');</script>")

    head = pos.loc[["최대보유수량", "현재보유수량", "매집가능수량",
                    "분산비율", "평균단가", "승패"]]
    t1 = _table(head, row_kind={"분산비율": "pct", "평균단가": "int"})
    # 기간별 표는 순매수만 본다. 보유량 두 행은 표① 과 겹치므로 뺀다.
    per = rep.drop(index=["현재 보유량", "최대 보유량"], errors="ignore").T
    t2 = _table(per, kinds={c: "sign" for c in per.columns},
                row_kind={"평균단가": "int", "거래량": "int"})

    cur = d.px.iloc[-1]
    delta = (cur / d.px.iloc[-2] - 1) * 100 if len(d) > 1 and d.px.iloc[-2] else 0
    # 전일 종가: 저장 자료가 오늘 전까지면 마지막 저장 종가가 곧 전일 종가, 오늘 봉까지 있으면 그 앞날 종가
    prev_close = cur if d.index[-1].date() < date.today() or len(d) < 2 else d.px.iloc[-2]
    title = f'{html.escape(name)}<small>{html.escape(code)}</small>'
    doc = f"""<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(name)} 수급분석</title><style>{CSS}</style></head><body>
<div class="stock-heading" data-stock-code="{html.escape(code)}" data-stock-name="{html.escape(name, quote=True)}"><h1>{title}</h1>
<div class="stock-price" id="stk-price" data-prev="{prev_close:.0f}"><strong id="stk-now">{cur:,.0f}<small>원</small></strong><span id="stk-chg" class="{'pos' if delta >= 0 else 'neg'}">{delta:+.2f}% · 저장 종가</span><span class="stk-prev">전일 종가 <b id="stk-prev">{prev_close:,.0f}</b>원</span></div></div>
<script>
// 우상단 = 실시간 호가 창의 현재가(사용자 요청 2026-10-08) · 그 아래 전일 종가. 실시간이 오기 전엔 저장 종가를 둔다
window.addEventListener('rt', function(e){{
  var t=(e.detail||{{}}).trade||{{}}; if(!t.price) return;
  // 전일 종가는 저장된 KRX 정규장 종가를 쓴다 — 키움 기준가(base)는 넥스트레이드 시간외까지 반영돼 정규장 종가와 다를 수 있다(실측 2026-10-08 테크윙 53,700 vs 53,100)
  var box=document.getElementById('stk-price'), prev=+box.dataset.prev||t.base||(t.change!=null?t.price-t.change:0);
  var r=prev?(t.price/prev-1)*100:0, nf=function(v){{return Number(v).toLocaleString('ko-KR');}};
  document.getElementById('stk-now').innerHTML=nf(t.price)+'<small>원</small>';
  var c=document.getElementById('stk-chg'); c.className=r>0?'pos':(r<0?'neg':'');
  c.textContent=(r>0?'+':'')+r.toFixed(2)+'% · '+((e.detail.src==='ws')?'실시간':'호가 스냅샷');
  if(prev) document.getElementById('stk-prev').textContent=nf(prev);
}});
</script>
<div class="sub">수급 기준 {d.index[-1]:%Y-%m-%d} · {len(d):,}거래일 · 주가와 수급을 같은 날짜로 확인</div>
<p class="note chart-mobile-hint">좁은 화면에서는 차트를 좌우로 밀어 확인하세요. 범례를 누르면 해당 선을 숨기거나 표시할 수 있습니다.</p>
{chart}
<p class="note">차트 — 위 버튼으로 <b>일·주·월봉</b>을 고릅니다. 봉 아래 <b>띠</b>에 개인분산(파란색)·세력분산(빨간색)을 겹쳤습니다. 두 선은 음의 상관이라 엇갈리고, <b>두 선의 간격이 곧 SIG</b>입니다. <b>빨간 화살표</b>는 신호 발생일(일봉에만 표시). <b>휠로 확대·축소, 드래그로 이동, 더블클릭으로 전체 보기</b>. 세로축은 보이는 봉에 자동으로 맞습니다. 재무 칸을 누르면 그 분기 숫자가 칸 제목 옆에 나옵니다.</p>
<details class="stock-details" open><summary>주체별 보유 요약 · 기간별 순매수</summary><h2>주체별 보유 요약</h2>{t1}
<p class="note">분산비율 = 현재보유수량 ÷ 최대보유수량 (1=재고 꽉 참 · 0=재고 비움).
평균단가는 매집 구간(누적순매수가 역대최저를 찍은 다음날부터 오늘까지) 순매수의 거래대금
가중평균 — <b>HTS 화면과 대조해 확인하지 못한 추정 정의</b>입니다. 승패는 종가와 그 평균단가의 비교.</p>
<h2>기간별 순매수 (수량)</h2>{t2}
<p class="note">기간은 거래일 수 기준(1주=5 · 1달=20 · 1분기=60 · 1년=240).
세력합 = 외국인 + 금융투자·보험·투신·기타금융·은행·연기금·사모펀드·국가·기타법인.</p>
<p class="note">데이터: {core.FLOW}</p></details>
</body></html>"""
    if body_only:
        return doc
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(doc, encoding="utf-8")
    return out
