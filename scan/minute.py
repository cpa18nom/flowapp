"""분봉 차트 (5·20·60분) — 키움 ka10080 + 실시간 체결로 마지막 봉을 이어 그린다.

실측(2026-09-29, 005930)
  - tic_scope 5·20·60 모두 **기본 지원**(20분도 키움이 직접 준다). 한 번에 900봉:
    5분≈2주 · 20분≈2개월 · 60분≈6개월.
  - 봉 시각 cntr_tm 은 **시작 시각**(09:00 봉이 첫 봉, 60분은 0900·1000…).
  - 코드 그대로 = KRX. 09:00 5분봉 거래량 KRX 159.9만 + NXT(_NX) 22.5만 = 통합(_AL) 182.4만.
    실시간·일봉이 KRX 라 맞춘다.
  - KRX 도 15:30 뒤에 시간외 봉이 온다(16:00 8천주 등 — 정규장의 1% 미만). 정규장만 그린다:
    **시작 시각 ≤ 15:30** 인 봉(5분의 15:30 봉 = 종가 단일가).
  - 가격은 등락 부호가 붙어 온다('+272500') → abs.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime

import core
import realtime

SCOPES = (5, 20, 60)
SHOW = 160            # 처음 보이는 봉 수. 확대·축소(2026-09-29)로 더 넓게 볼 수 있어 데이터는 ZOOM_BARS 만큼 싣는다
ZOOM_BARS = 600       # 브라우저에 싣는 봉 수(5분≈1주 반 · 60분≈4달). 900봉 중 앞은 EMA 워밍업용으로만 쓴다
MAS = (20, 60)
SESSION_END = "1530"
CACHE_TTL = 20        # 초. 같은 종목·주기를 여러 창이 열어도 키움을 반복 호출하지 않는다
_CACHE: dict[tuple, tuple[float, dict]] = {}
_LOCK = threading.Lock()


def parse_rows(rows: list[dict]) -> list[dict]:
    """ka10080 행 → 과거→최신 봉 목록. 정규장 밖·빈 값은 버린다."""
    out = []
    for r in rows:
        tm = str(r.get("cntr_tm") or "")
        if len(tm) < 12 or tm[8:12] > SESSION_END:
            continue
        o, h, l, c = (realtime._abs(r.get(k)) for k in ("open_pric", "high_pric", "low_pric", "cur_prc"))
        if not all((o, h, l, c)):
            continue
        out.append({"t": tm[:12], "o": o, "h": h, "l": l, "c": c,
                    "v": abs(realtime._int(r.get("trde_qty")) or 0)})
    out.sort(key=lambda b: b["t"])
    return out


def add_ma(bars: list[dict]) -> list[dict]:
    closes = [b["c"] for b in bars]
    for n in MAS:
        s = 0.0
        for i, c in enumerate(closes):
            s += c
            if i >= n:
                s -= closes[i - n]
            bars[i][f"ma{n}"] = round(s / n, 2) if i >= n - 1 else None
    return bars


IND_KEYS = ("bb220", "kijun", "env", "vwap")     # 보조지표 4종(viewer.IND 와 같은 정의, 2026-10-07): 볼린저 220·1σ 상한 · 일목 기준선 26 · 엔벨로프 60·5% 상단 · VWAP 120
IND_MAX = 220


def add_ind(bars: list[dict]) -> list[dict]:
    """네 선을 봉마다 적는다(없으면 None). 60분봉 220봉 ≈ 7주 — 900봉을 받으므로 뒤 600봉은 다 채워진다."""
    n = len(bars)
    c = [b["c"] for b in bars]
    for i in range(n):
        b = bars[i]
        if i >= 219:
            w = c[i - 219:i + 1]
            mu = sum(w) / 220
            sd = (sum((x - mu) ** 2 for x in w) / 220) ** 0.5
            b["bb220"] = round(mu + sd, 2)
        else:
            b["bb220"] = None
        b["kijun"] = round((max(x["h"] for x in bars[i - 25:i + 1]) + min(x["l"] for x in bars[i - 25:i + 1])) / 2, 2) if i >= 25 else None
        b["env"] = round(sum(c[i - 59:i + 1]) / 60 * 1.05, 2) if i >= 59 else None
        if i >= 119:
            vv = sum(x["v"] for x in bars[i - 119:i + 1])
            b["vwap"] = round(sum(x["c"] * x["v"] for x in bars[i - 119:i + 1]) / vv, 2) if vv else None
        else:
            b["vwap"] = None
    return bars


def add_stoch(bars: list[dict]) -> list[dict]:
    """「일봉양전」과 같은 식(core.stoch_lines, 12·26·9)을 분봉에 적용해 fd·sd·osc 를 붙인다.
    받은 봉 전체(최대 900)로 계산한다 — EMA 는 앞쪽 봉이 모자라면 값이 달라진다. 워밍업 구간은 None."""
    import math

    import pandas as pd                                    # noqa: PLC0415
    if not bars:
        return bars
    st = core.stoch_lines(pd.Series([b["h"] for b in bars], dtype=float),
                          pd.Series([b["l"] for b in bars], dtype=float),
                          pd.Series([b["c"] for b in bars], dtype=float))
    for b, fd, sd, osc in zip(bars, st.fastD, st.slowD, st.osc):
        ok = not (math.isnan(fd) or math.isnan(sd))
        b["fd"], b["sd"] = (round(fd, 6), round(sd, 6)) if ok else (None, None)
        b["osc"] = round(osc, 6) if ok and not math.isnan(osc) else None
    return bars


def fetch(code: str, m: int) -> dict:
    if m not in SCOPES:
        raise ValueError(f"지원 주기: {SCOPES}")
    key = (code, m)
    now = time.time()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < CACHE_TTL:
            return hit[1]
    import flow                                            # noqa: PLC0415
    for attempt in range(6):
        r, j = flow.kiwoom_post("/api/dostk/chart", "ka10080",             # 8005 면 재발급·재시도
                                {"stk_cd": code, "tic_scope": str(m), "upd_stkpc_tp": "1"}, timeout=15)
        if getattr(r, "status_code", 200) != 429 or attempt == 5:
            break
        time.sleep(flow.RETRY_429 * 2 ** attempt)          # 한도 초과는 1초 안에 풀린다(실측 최대 연속 2회) — 짧게 쉬고 다시
    if j.get("return_code") not in (0, "0"):
        raise RuntimeError(f"분봉 조회 실패: {j.get('return_msg')}")
    bars = add_stoch(add_ind(add_ma(parse_rows(j.get("stk_min_pole_chart_qry") or []))))
    out = {"code": code, "m": m, "today": datetime.now().strftime("%Y%m%d"),
           # 이평 계산에 앞쪽 60봉이 더 필요하다 — 실시간으로 마지막 점을 다시 셀 때도 쓴다
           "bars": bars[-(ZOOM_BARS + max(MAS)):], "show": SHOW, "mas": list(MAS), "ind": list(IND_KEYS),
           "stoch": [core.STOCH_K, core.STOCH_DFAST, core.STOCH_DSLOW]}
    with _LOCK:
        _CACHE[key] = (now, out)
    return out


def payload(code: str, m: int) -> bytes:
    try:
        d = fetch(code, m)
    except Exception as e:                                  # noqa: BLE001
        d = {"error": str(e)[:120]}
    return json.dumps(d, ensure_ascii=False).encode()


# ───────────────────────────── 화면

CSS = """
.mn-note{color:var(--muted);font-size:11.5px;margin:0 0 6px}
#mn-chart{height:640px}
@media(max-width:640px){#mn-chart{height:480px}}
"""


def panel(code: str) -> str:
    """일·주·월봉 차트와 같은 자리에 들어가는 분봉 패널(버튼은 viewer 가 만든다).
    **버튼을 누를 때만** 키움에서 불러온다 — 페이지를 열 때마다 분봉을 조회하지 않는다."""
    return (f'<style>{CSS}</style><div id="mn" data-code="{code}">'
            '<p class="mn-note" id="mn-note">분봉을 고르면 불러옵니다.</p>'
            '<div class="lg" id="lg-mn" role="group" aria-label="범례 — 누르면 선을 숨기거나 표시합니다"></div>'   # 보조지표 켜고 끄기(일봉과 같은 선택, 2026-10-07)
            '<div id="mn-chart" role="img" aria-label="분봉 캔들 차트"></div></div>' + JS)


# 그리기는 static/chart.js 의 FC.minute(캔버스, 2026-10-02 plotly 에서 옮김). 이 섹션은 그 스크립트 **뒤**에 놓인다.
JS = r"""<script>
(function(){
  const box=document.getElementById('mn'); if(!box) return;
  const code=box.dataset.code, el=document.getElementById('mn-chart'), note=document.getElementById('mn-note');
  const css=k=>getComputedStyle(document.documentElement).getPropertyValue(k).trim();
  let D=null, M=5, seen=0, drawn=0, dirty=false, lastSeq=0;
  const lab=t=>t.slice(4,6)+'/'+t.slice(6,8)+' '+t.slice(8,10)+':'+t.slice(10,12);
  function bucket(today, hms, m){                 // 체결시각 → 그 봉의 시작 시각(09:00 기준 m분 단위)
    const mins=(+hms.slice(0,2))*60+(+hms.slice(2,4)), s=9*60;
    if(mins<s || hms.slice(0,4)>'1530') return null;
    const b=s+Math.floor((mins-s)/m)*m, hh=String(Math.floor(b/60)).padStart(2,'0'), mm=String(b%60).padStart(2,'0');
    return today+hh+mm;
  }
  // 마지막 봉의 스토캐스틱을 **직전 봉의 fd·sd 를 이어받아** 같은 식으로 다시 센다(core.stoch_lines 와 동일).
  function llhh(bars,j,K){ if(j<K-1) return null; let lo=Infinity,hi=-Infinity;
    for(let t=j-K+1;t<=j;t++){lo=Math.min(lo,bars[t].l);hi=Math.max(hi,bars[t].h)} return [lo,hi]; }
  function stochAt(bars,i){
    const [K,F,S]=D.stoch, b=bars[i], p=bars[i-1];
    if(!p||p.fd==null||p.sd==null) return;          // 워밍업 구간은 서버 값을 그대로 둔다
    const x=llhh(bars,i,K); if(!x) return;
    const fk=(b.c-x[0])/Math.max(x[1]-x[0],1e-12)*100;
    let num=0,den=0;
    for(let j=i-F+1;j<=i;j++){const y=llhh(bars,j,K); if(!y) return; num+=bars[j].c-y[0]; den+=y[1]-y[0];}
    const sk=den!==0?num/Math.max(den,1e-12)*100:0, af=2/(F+1), as=2/(S+1);
    b.fd=af*fk+(1-af)*p.fd; b.sd=as*sk+(1-as)*p.sd; b.osc=b.fd-b.sd;
  }
  window.__mnStochAt=(bars,i)=>{stochAt(bars,i); return bars[i].osc};   // 검증용
  function ma(bars){
    for(const n of D.mas){
      for(let i=Math.max(0,bars.length-2);i<bars.length;i++){
        if(i<n-1){bars[i]['ma'+n]=null;continue}
        let s=0; for(let j=i-n+1;j<=i;j++) s+=bars[j].c; bars[i]['ma'+n]=s/n;
      }
    }
    ind(bars);
  }
  function ind(bars){                                   // 보조지표 4종 — 마지막 두 봉만(minute.add_ind 와 같은 식)
    for(let i=Math.max(0,bars.length-2);i<bars.length;i++){
      const b=bars[i];
      if(i>=219){let s=0;for(let j=i-219;j<=i;j++)s+=bars[j].c;const mu=s/220;let q=0;for(let j=i-219;j<=i;j++)q+=(bars[j].c-mu)**2;b.bb220=mu+Math.sqrt(q/220)}else b.bb220=null;
      if(i>=25){let hi=-1e18,lo=1e18;for(let j=i-25;j<=i;j++){hi=Math.max(hi,bars[j].h);lo=Math.min(lo,bars[j].l)}b.kijun=(hi+lo)/2}else b.kijun=null;
      if(i>=59){let s=0;for(let j=i-59;j<=i;j++)s+=bars[j].c;b.env=s/60*1.05}else b.env=null;
      if(i>=119){let pv=0,vv=0;for(let j=i-119;j<=i;j++){pv+=bars[j].c*bars[j].v;vv+=bars[j].v}b.vwap=vv?pv/vv:null}else b.vwap=null;
    }
  }
  // 휠=확대·축소 · 드래그=이동 · 더블클릭=처음 창 — FC.minute 가 처리한다. 새 봉이 붙으면 오른쪽 끝에 있을 때만 따라간다.
  let H=null;
  function draw(){                                     // 새로 불러온 자료 — 처음 창으로
    if(!D||!D.bars.length){ if(H) H.c.setData([]); return }
    H=FC.minute(el,D,null);
    drawn=Date.now(); dirty=false;
  }
  function tick(){                                     // 실시간 — 마지막 봉만, 보던 범위는 그대로
    if(!H||!D) return draw();
    if(FC.busy()) return;                              // 휠·드래그 중이면 미룬다(dirty 유지)
    H.tick(D); drawn=Date.now(); dirty=false;
  }
  function load(m){
    M=m; note.textContent='불러오는 중…';
    fetch('/api/minute/'+code+'?m='+m).then(r=>r.json()).then(d=>{
      if(d.error){note.textContent='분봉을 불러오지 못했습니다 — '+d.error; D=null; draw(); return}
      D=d; seen=lastSeq; draw();          // 새 주기는 처음 창으로     // 받아 온 봉에 이미 든 체결은 다시 더하지 않는다
      const last=d.bars.length?d.bars[d.bars.length-1].t:'';
      note.textContent=m+'분봉 · 정규장(09:00~15:30) · 키움 KRX · 마지막 봉 '+(last?lab(last):'없음');
    }).catch(()=>{note.textContent='분봉을 불러오지 못했습니다'});
  }
  window.addEventListener('rt',e=>{            // 실시간 체결로 마지막 봉을 갱신·추가
    const s=e.detail; lastSeq=Math.max(lastSeq,s.seq||0); if(!D||s.src!=='ws') return;
    let changed=false;
    for(const k of (s.ticks||[])){
      if(k.seq<=seen) continue; seen=k.seq;
      const t=bucket(D.today,k.time,M); if(!t||!k.price) continue;
      const bars=D.bars, last=bars[bars.length-1], v=Math.abs(k.vol||0);
      if(last&&last.t===t){last.h=Math.max(last.h,k.price);last.l=Math.min(last.l,k.price);last.c=k.price;last.v+=v}
      else if(!last||t>last.t){bars.push({t,o:k.price,h:k.price,l:k.price,c:k.price,v});
        if(bars.length>D.show+120) bars.splice(0,bars.length-(D.show+120))}
      else continue;
      changed=true;
    }
    if(changed){ma(D.bars); stochAt(D.bars,D.bars.length-1); dirty=true}
  });
  setInterval(()=>{if(dirty&&Date.now()-drawn>500) tick()},250);   // 실시간 봉은 초당 2회까지, 마지막 봉만
  window.loadMinute=function(m){ if(window.FC) load(m); };   // viewer 의 5분·20분·60분 버튼이 부른다
  window.resetMinute=function(){ if(H) H.reset(); };   // 「⟲ 처음 크기」 단추
})();
</script>"""
