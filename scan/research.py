"""전략 검정 · 신호 비교 — 삭제된 scan 탭 두 개를 flowapp 으로 옮긴 것.

계산은 **core 가 한다.** 여기서 평균·승률을 다시 구하지 않는다 — 화면이 자체 계산을
가지면 검정과 화면이 갈라진다(scan 시절 실제로 겪은 사고).

지켜야 하는 두 가지 (둘 다 과거에 낸 버그다)
  1. 횡단면 연산은 **유동성으로 좁힌 검정 유니버스 안에서만.** 모집단 전체로 중립화하면
     기준선이 0 이 아니게 된다(실측 −1.2048%).
  2. 화면의 거래대금 입력은 **억**, `val20` 은 **원**. `* 1e8` 을 빼먹으면 "10원 이상"이
     되어 유동성 필터가 통째로 꺼진다.
"""
from __future__ import annotations

import threading
import time

import numpy as np

import core

HORIZON_DEFAULT = 20
STOP_DEFAULT = 20.0
CACHE_TTL = 1800          # 30분. 패널 기준일이 바뀌면 키가 달라져 저절로 무효가 된다
_CACHE: dict[tuple, tuple[float, dict]] = {}
_CACHE_LOCK = threading.Lock()


def _cached(key: tuple, build):
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < CACHE_TTL:
            return hit[1]
    value = build()
    with _CACHE_LOCK:
        _CACHE[key] = (now, value)
        if len(_CACHE) > 24:                      # 오래된 것부터 버린다
            for k in sorted(_CACHE, key=lambda k: _CACHE[k][0])[:8]:
                _CACHE.pop(k, None)
    return value


def universe(panel, minval_eok: float):
    """검정 유니버스. **억 → 원** 환산을 여기 한 곳에서만 한다."""
    return core.apply_liquidity(panel, minval_eok * 1e8)


def _signal(sub, kind: str, o: dict, panel=None):
    """panel(걸러지기 전 패널)은 재진입 검색식만 쓴다 — 신호 뒤 며칠을 종목의 연속 시계열에서 센다."""
    return core.make_signal(sub, kind, vol_q=o["volq"], hh_days=o["hh"],
                            cafe_decline=o["cafe"], alg_rise=o["algr"],
                            alg_pullback=o["algp"], exclude_surge=bool(o["surge"]), full=panel,
                            reclaim_window=int(o.get("rcw", core.RECLAIM_WINDOW)),
                            reclaim_dip=float(o.get("rcd", core.RECLAIM_DIP)),
                            reclaim_vmult=float(o.get("rcv", core.RECLAIM_VMULT)))


def _key(panel, o: dict, extra: tuple) -> tuple:
    """패널 기준일을 키에 넣는다 — 시세를 갱신하면 옛 검정 결과가 남지 않는다."""
    return (str(panel.dt.max())[:10], len(panel), o["minval"], o["volq"], o["hh"],
            o["cafe"], o["algr"], o["algp"], o["surge"], o.get("rcw"), o.get("rcd"), o.get("rcv")) + extra


def backtest(panel, o: dict, horizon: int = HORIZON_DEFAULT,
             stop: float | None = STOP_DEFAULT, take: float | None = None) -> dict:
    """현재 설정 그대로 검정(앱 시세는 2014년~ — 약 12년). 숫자는 전부 core 가 낸 것을 옮기기만 한다."""
    def build():
        sub = universe(panel, o["minval"])
        mask = _signal(sub, o["kind"], o, panel)
        # ★ 수익은 걸러지기 전 패널(panel)의 연속 시계열에서 잰다 — core.forward_return
        es = core.event_study(sub, mask, horizon, full=panel)
        bt = core.backtest(sub, mask, horizon, stop_loss=stop, take_profit=take, full=panel)
        st = core.stability(sub, mask, horizon, full=panel)
        return {
            "신호": next(k for k, v in core.SIGNALS.items() if v == o["kind"]),
            "유니버스": int(len(sub)), "발생": int(mask.sum()),
            "보유일": horizon, "손절": stop, "익절": take,
            "요약": {k: bt.get(k) for k in
                   ("n", "연환산초과", "시장대비승률", "초과수익", "승률", "평균수익",
                    "중앙수익", "연환산", "시장수익", "손익비", "최대낙폭_평균",
                    "MDD_20퍼센트이하비율", "참고_사후꼭지", "회전수")},
            "검정": {k: es.get(k) for k in
                   ("n", "초과평균", "초과중앙", "시장대비승률", "t",
                    "기준선승률", "기준선평균")},
            "구간": st.replace({np.nan: None}).to_dict("records"),
        }
    return _cached(_key(panel, o, ("bt", o["kind"], horizon, stop, take)), build)


def compare(panel, o: dict, horizon: int = HORIZON_DEFAULT,
            stop: float | None = STOP_DEFAULT) -> dict:
    """5개 신호를 **같은 유니버스·같은 조건**으로 나란히. 조건이 갈리면 비교가 아니다."""
    def build():
        sub = universe(panel, o["minval"])       # 유니버스는 한 번만 만들어 공유한다
        rows = []
        for label, kind in core.SIGNALS.items():
            mask = _signal(sub, kind, o, panel)
            es = core.event_study(sub, mask, horizon, full=panel)
            bt = core.backtest(sub, mask, horizon, stop_loss=stop, full=panel)
            rows.append({"신호": label, "kind": kind, "발생": int(mask.sum()),
                         "n": es.get("n", 0), "초과평균": es.get("초과평균"),
                         "t": es.get("t"), "시장대비승률": es.get("시장대비승률"),
                         "연환산초과": bt.get("연환산초과"), "손익비": bt.get("손익비"),
                         "참고_사후꼭지": bt.get("참고_사후꼭지")})
        return {"유니버스": int(len(sub)), "보유일": horizon, "손절": stop, "행": rows,
                "기준선승률": core.event_study(sub, _signal(sub, o["kind"], o, panel),
                                          horizon, full=panel).get("기준선승률")}
    return _cached(_key(panel, o, ("cmp", horizon, stop)), build)


# ──────────────────────────────────────────────────────────── 화면

def _f(v, digits=2, suffix="", sign=False):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "—"
    return (f"{v:+.{digits}f}{suffix}" if sign else f"{v:,.{digits}f}{suffix}")


def _verdict(t) -> str:
    """t 값을 말로 옮긴다. **유의하지 않으면 유의하지 않다고 적는다.**"""
    if t is None or (isinstance(t, float) and np.isnan(t)):
        return "판정 불가 (표본 부족)"
    if t >= 2:
        return "통계적으로 유의 (t ≥ 2)"
    if t <= -2:
        return "<b>역방향으로</b> 유의 (t ≤ −2) — 이 신호는 반대로 간다"
    return "<b>유의하지 않음</b> (|t| &lt; 2) — 단독 매매근거로 쓰지 마십시오"


def plain_summary(d: dict) -> str:
    """검정 결과를 초보가 읽는 한 문단으로. 숫자는 표의 것을 그대로 쓴다(새로 계산하지 않는다)."""
    es, s = d["검정"], d["요약"]
    t, ex, win = es.get("t"), es.get("초과평균"), es.get("시장대비승률")
    if t is None or ex is None or (isinstance(t, float) and np.isnan(t)):
        return "<p class='lead'><b>한 줄 요약</b> · 신호가 너무 적어 성적을 말할 수 없습니다.</p>"
    if t >= 2:
        verdict = "과거에 <b>시장 평균보다 나았습니다</b>. 우연으로 보기 어려운 차이입니다."
    elif t <= -2:
        verdict = "과거에 <b>시장 평균보다 나빴습니다</b>. 매수 신호로 쓰면 안 됩니다."
    else:
        verdict = "과거 성적이 <b>시장 평균과 구별되지 않습니다</b>. 이 검색식만 보고 사지 마십시오."
    return (f"<p class='lead'><b>한 줄 요약</b> · 이 검색식은 {verdict} "
            f"신호가 난 날 사서 {d['보유일']}거래일 들고 있었다면 시장보다 평균 {_f(ex, 2, '%p', True)}, "
            f"시장을 이긴 비율은 {_f(win, 1, '%')}였습니다(2014년 이후 {s['n']:,}건). "
            "아래 표는 그 근거입니다.</p>")


def render_backtest(d: dict) -> str:
    from html import escape
    s, es = d["요약"], d["검정"]
    head = (f"<h3>{escape(d['신호'])} · 보유 {d['보유일']}일 · "
            f"손절 {_f(d['손절'], 0, '%')}{' · 익절 ' + _f(d['익절'], 0, '%') if d['익절'] else ''}</h3>"
            f"<p class='note'>검정 유니버스 {d['유니버스']:,}행 · 신호 발생 {d['발생']:,}건 · "
            f"성과 측정 가능 {s['n']:,}건 (보유기간이 모자란 최근 신호는 측정에서 빠집니다)</p>")
    head = plain_summary(d) + head
    rows = [
        ("연환산 <b>초과</b>수익", _f(s["연환산초과"], 2, "%p", True), "시장(유니버스 동일가중)을 뺀 값"),
        ("시장대비 승률", _f(s["시장대비승률"], 1, "%"), f"기준선 {_f(es['기준선승률'], 1, '%')}"),
        ("t 값", _f(es["t"], 2), _verdict(es["t"])),
        ("건당 초과수익", _f(es["초과평균"], 2, "%", True), f"중앙 {_f(es['초과중앙'], 2, '%', True)}"),
        ("연환산 절대수익", _f(s["연환산"], 2, "%p", True), f"그중 시장분 {_f(s['시장수익'], 2, '%', True)}"),
        ("손익비", _f(s["손익비"], 2), "이긴 거래 평균 ÷ 진 거래 평균"),
        ("평균 최대낙폭", _f(s["최대낙폭_평균"], 2, "%"), f"−20% 이하 비율 {_f(s['MDD_20퍼센트이하비율'], 1, '%')}"),
        ("참고: 사후 꼭지", _f(s["참고_사후꼭지"], 2, "%"), "보유 중 최고가 기준 — <b>실현할 수 없는 수치</b>"),
    ]
    body = "".join(f"<tr><th class='k'>{k}</th><td>{v}</td><td class='muted'>{n}</td></tr>"
                   for k, v, n in rows)
    st = "".join(
        f"<tr><td>{escape(str(r['구간']))}</td><td>{int(r['발생수'] or 0):,}</td>"
        f"<td>{_f(r['초과평균'], 2, '%', True)}</td><td>{_f(r['승률'], 1, '%')}</td>"
        f"<td>{_f(r['t'], 2)}</td></tr>" for r in d["구간"])
    return (head
            + f"<div class='wrap'><table><tbody>{body}</tbody></table></div>"
            + "<h4>구간 안정성 — 한 구간이 전부를 끌고 가는지 본다</h4>"
            + "<div class='wrap'><table><thead><tr><th>구간</th><th>발생</th>"
              f"<th>초과평균</th><th>승률</th><th>t</th></tr></thead><tbody>{st}</tbody></table></div>"
            + "<p class='note'>구간 하나만 좋고 나머지가 0 이거나 음수면, 그 신호는 "
              "'그 시기에 맞았던 것'이지 전략이 아닙니다.</p>")


def render_compare(d: dict) -> str:
    from html import escape
    head = (f"<p class='note'>같은 유니버스({d['유니버스']:,}행)·같은 보유 {d['보유일']}일·"
            f"손절 {_f(d['손절'], 0, '%')} 조건으로 {len(d['행'])}개 검색식을 나란히 돌린 결과입니다. "
            f"조건이 갈리면 비교가 아닙니다.</p>")
    rows = ""
    for r in sorted(d["행"], key=lambda r: (r["t"] is None, -(r["t"] or -99))):
        mark = "★" if (r["t"] or 0) >= 2 else ""
        rows += (f"<tr><td>{mark} {escape(r['신호'])}</td><td>{r['발생']:,}</td>"
                 f"<td>{_f(r['초과평균'], 2, '%', True)}</td><td>{_f(r['t'], 2)}</td>"
                 f"<td>{_f(r['시장대비승률'], 1, '%')}</td>"
                 f"<td>{_f(r['연환산초과'], 2, '%p', True)}</td><td>{_f(r['손익비'], 2)}</td>"
                 f"<td class='muted'>{_f(r['참고_사후꼭지'], 2, '%')}</td></tr>")
    return (head + "<div class='wrap'><table><thead><tr><th>신호</th><th>발생</th>"
            "<th>건당 초과</th><th>t</th><th>시장대비 승률</th><th>연환산 초과</th>"
            "<th>손익비</th><th class='muted'>참고: 사후꼭지</th></tr></thead>"
            f"<tbody>{rows}</tbody></table></div>"
            + f"<p class='note'>기준선(아무 날이나 샀을 때) 승률 {_f(d['기준선승률'], 1, '%')}. "
              "★ 는 t ≥ 2 인 신호입니다 — 지금 자료에서 별이 없으면 "
              "<b>통계적으로 살아남은 검색식이 없다</b>는 뜻입니다. "
              "'사후 꼭지'는 보유 중 최고가라 실현할 수 없는 수치이며, 원본 검색기가 "
              "'최고수익'으로 광고하던 값과 같은 종류입니다.</p>")


PAGE_JS = '''<section id="res-section">
<h2>{title}</h2>
<p id="res-status" role="status" aria-live="polite">2014년 이후 자료로 계산하는 중입니다… (수 초 걸립니다)</p>
<div id="res-content"></div>
</section>
<script>
(function() {{
 const status = document.getElementById('res-status');
 const content = document.getElementById('res-content');
 (async function load() {{
   try {{
     const response = await fetch('{api}', {{cache: 'no-store'}});
     const result = await response.json();
     if (!response.ok) throw new Error(result.error || '계산에 실패했습니다.');
     content.innerHTML = result.html;
     status.textContent = '계산 완료';
   }} catch (error) {{
     status.textContent = '계산 실패: ' + error.message;
   }}
 }})();
}})();
</script>'''


def section(title: str, api: str) -> str:
    return PAGE_JS.format(title=title, api=api)
