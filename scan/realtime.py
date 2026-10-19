"""실시간 현재가·호가 — 키움 REST 웹소켓(0B 체결 · 0D 호가잔량)을 종목 화면에 흘려보낸다.

구조
  브라우저 ──SSE(/api/rt/<코드>)──▶ flowapp ──웹소켓 1개──▶ 키움
  토큰은 서버에만 있다(브라우저가 키움에 직접 붙으면 토큰이 노출된다).

지키는 것
  - **보는 동안만 붙는다.** 종목 화면이 열리면 그 종목을 REG, 닫히면 REMOVE. 보는 종목이 하나도
    없으면 IDLE_SEC 뒤 연결을 끊는다 — 상시 백그라운드로 돌지 않는다.
  - 등록 코드는 **KRX**(코드 그대로). `_NX` 는 NXT, `_AL` 은 통합이다(실측 2026-09-29: 같은 순간
    005930 271,750 · _NX 271,500). flowapp 의 일봉이 KRX 라 맞춘다.
  - 처음 열 때 REST(ka10001 현재가 · ka10004 호가)로 먼저 채운다. 장 마감 뒤에도 마지막 값이 보이고,
    화면은 그것이 '스냅샷'임을 표시한다.
  - 키움 값은 등락 부호가 붙어 온다('+271750', '-266000'). 가격은 abs, 부호는 전일대비에서만 쓴다.

필드(실측 2026-09-29)
  0B  10 현재가 · 11 전일대비 · 12 등락률 · 13 누적거래량 · 15 체결량(+매수/-매도) · 20 체결시각
  0D  41~50 매도호가1~10 · 51~60 매수호가1~10 · 61~70 매도잔량 · 71~80 매수잔량 ·
      121 총매도잔량 · 125 총매수잔량 · 21 호가시각
"""
from __future__ import annotations

import asyncio
import collections
import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))

IDLE_SEC = 60          # 보는 종목이 없어진 뒤 연결을 유지하는 시간
RECONNECT_MAX = 30     # 재접속 백오프 상한(초)
TICK_BUF = 2000        # 종목별로 쌓아 두는 최근 체결 수. SSE 는 초당 4회로 묶여 그 사이 체결을
                       # 여기서 **빠짐없이** 꺼내 보낸다 — 분봉의 거래량·고저가 틀어지지 않게.


def _int(v) -> int | None:
    try:
        return int(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _float(v) -> float | None:
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _abs(v) -> int | None:
    n = _int(v)
    return abs(n) if n is not None else None


# ───────────────────────────── 파싱 (웹소켓 · REST 를 같은 모양으로)

def parse_trade(v: dict) -> dict:
    return {"price": _abs(v.get("10")), "change": _int(v.get("11")), "rate": _float(v.get("12")),
            "cum_vol": _int(v.get("13")), "vol": _int(v.get("15")), "time": v.get("20") or "",
            "open": _abs(v.get("16")), "high": _abs(v.get("17")), "low": _abs(v.get("18"))}


def parse_book(v: dict) -> dict:
    asks = [[_abs(v.get(str(41 + i))), _int(v.get(str(61 + i)))] for i in range(10)]
    bids = [[_abs(v.get(str(51 + i))), _int(v.get(str(71 + i)))] for i in range(10)]
    return {"asks": asks, "bids": bids, "tot_ask": _int(v.get("121")),
            "tot_bid": _int(v.get("125")), "time": v.get("21") or ""}


def _nth(side: str, i: int) -> str:
    """ka10004 키 이름: 1단계는 'fpr', 2단계부터 '2th_pre'."""
    return f"{side}_fpr" if i == 1 else f"{side}_{i}th_pre"


def parse_rest_book(j: dict) -> dict:
    asks = [[_abs(j.get(f"{_nth('sel', i)}_bid")), _int(j.get(f"{_nth('sel', i)}_req"))]
            for i in range(1, 11)]
    bids = [[_abs(j.get(f"{_nth('buy', i)}_bid")), _int(j.get(f"{_nth('buy', i)}_req"))]
            for i in range(1, 11)]
    return {"asks": asks, "bids": bids, "tot_ask": _int(j.get("tot_sel_req")),
            "tot_bid": _int(j.get("tot_buy_req")), "time": j.get("bid_req_base_tm") or ""}


def parse_rest_quote(j: dict) -> dict:
    return {"price": _abs(j.get("cur_prc")), "change": _int(j.get("pred_pre")),
            "rate": _float(j.get("flu_rt")), "cum_vol": _int(j.get("trde_qty")), "vol": None,
            "time": "", "open": _abs(j.get("open_pric")), "high": _abs(j.get("high_pric")),
            "low": _abs(j.get("low_pric")), "base": _abs(j.get("base_pric")),
            "upper": _abs(j.get("upl_pric")), "lower": _abs(j.get("lst_pric"))}


def _rest(api_id: str, path: str, code: str) -> dict:
    import flow                                            # noqa: PLC0415
    _, j = flow.kiwoom_post(f"/api/dostk/{path}", api_id, {"stk_cd": code}, timeout=10)   # 8005 면 재발급·재시도
    if j.get("return_code") not in (0, "0"):
        raise RuntimeError(f"{api_id} {j.get('return_msg')}")
    return j


def rest_snapshot(code: str) -> dict:
    """장 마감 뒤에도 보이도록 REST 로 한 번 채운다. 실패한 쪽은 비워 둔다."""
    out = {}
    try:
        out["trade"] = parse_rest_quote(_rest("ka10001", "stkinfo", code))
    except Exception as e:                                  # noqa: BLE001
        out["error"] = f"현재가 조회 실패: {str(e)[:60]}"
    try:
        out["book"] = parse_rest_book(_rest("ka10004", "mrkcond", code))
    except Exception as e:                                  # noqa: BLE001
        out["error"] = f"호가 조회 실패: {str(e)[:60]}"
    return out


# ───────────────────────────── 허브

class Hub:
    """종목별 최신 상태 + 구독 카운트 + 웹소켓 1개. 스레드 안전.

    connect/token/snapshot 을 주입받는다 — 테스트는 가짜 소켓으로 돈다."""

    def __init__(self, token=None, connect=None, snapshot=rest_snapshot, idle=IDLE_SEC):
        self._token = token
        self._connect = connect
        self._snapshot = snapshot
        self._idle = idle
        self._cond = threading.Condition()
        self._state: dict[str, dict] = {}
        self._refs: dict[str, int] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ops: asyncio.Queue | None = None
        self.status = "대기"

    # ── 스레드 쪽 API (HTTP 핸들러가 부른다)
    def subscribe(self, code: str) -> None:
        with self._cond:
            first = self._refs.get(code, 0) == 0
            self._refs[code] = self._refs.get(code, 0) + 1
            st = self._state.setdefault(code, {"ver": 0, "src": "", "trade": None, "book": None})
        if first:
            threading.Thread(target=self._seed, args=(code,), daemon=True).start()
            self._ensure_loop()
            self._post(("REG", code))
        return st

    def unsubscribe(self, code: str) -> None:
        with self._cond:
            n = self._refs.get(code, 0) - 1
            if n > 0:
                self._refs[code] = n
                return
            self._refs.pop(code, None)
        self._post(("REMOVE", code))

    def watching(self) -> list[str]:
        with self._cond:
            return sorted(self._refs)

    def snapshot(self, code: str, since: int | None = None) -> dict:
        """since(체결 순번) 이후의 체결을 ticks 로 함께 준다. None 이면 최근 30건."""
        with self._cond:
            st = self._state.get(code) or {}
            buf = st.get("ticks") or ()
            ticks = list(buf)[-30:] if since is None else [t for t in buf if t["seq"] > since]
            return {"code": code, "ver": st.get("ver", 0), "src": st.get("src", ""),
                    "trade": st.get("trade"), "book": st.get("book"), "status": self.status,
                    "error": st.get("error", ""), "seq": st.get("seq", 0), "ticks": ticks}

    def wait(self, code: str, ver: int, timeout: float, since: int | None = None) -> dict:
        """ver 보다 새 상태가 오거나 timeout 이 지나면 스냅샷을 돌려준다."""
        end = time.monotonic() + timeout
        with self._cond:
            while (self._state.get(code) or {}).get("ver", 0) <= ver:
                left = end - time.monotonic()
                if left <= 0:
                    break
                self._cond.wait(left)
        return self.snapshot(code, since)

    def update(self, code: str, kind: str, data: dict, src: str) -> None:
        """kind = trade | book. REST 스냅샷은 실시간 값을 덮지 않는다."""
        with self._cond:
            st = self._state.setdefault(code, {"ver": 0, "src": "", "trade": None, "book": None})
            if src == "rest" and st.get("src") == "ws":
                return
            if kind == "trade" and st.get("trade"):
                keep = {k: v for k, v in st["trade"].items() if k in ("open", "high", "low", "base",
                                                                   "upper", "lower")}
                data = {**keep, **{k: v for k, v in data.items() if v is not None or k not in keep}}
            if kind == "trade" and src == "ws" and data.get("price") and data.get("time"):
                st["seq"] = st.get("seq", 0) + 1
                st.setdefault("ticks", collections.deque(maxlen=TICK_BUF)).append(
                    {"seq": st["seq"], "time": data["time"], "price": data["price"],
                     "vol": data.get("vol"), "change": data.get("change")})
            st[kind] = data
            st["src"] = src
            st["ver"] += 1
            st["at"] = time.time()
            self._cond.notify_all()

    def _seed(self, code: str) -> None:
        snap = self._snapshot(code)
        for kind in ("trade", "book"):
            if snap.get(kind):
                self.update(code, kind, snap[kind], "rest")
        if snap.get("error"):
            with self._cond:
                st = self._state.setdefault(code, {"ver": 0})
                st["error"] = snap["error"]
                st["ver"] = st.get("ver", 0) + 1
                self._cond.notify_all()

    def _set_status(self, s: str) -> None:
        with self._cond:
            self.status = s
            for st in self._state.values():
                st["ver"] = st.get("ver", 0) + 1
            self._cond.notify_all()

    # ── 이벤트 루프 (별도 스레드 하나)
    def _ensure_loop(self) -> None:
        with self._cond:
            if self._thread and self._thread.is_alive():
                return
            self._loop = asyncio.new_event_loop()
            self._ops = asyncio.Queue()
            self._thread = threading.Thread(target=self._loop.run_forever, daemon=True,
                                            name="kiwoom-rt")
            self._thread.start()
            asyncio.run_coroutine_threadsafe(self._run(), self._loop)

    def _post(self, op) -> None:
        loop, ops = self._loop, self._ops
        if loop and ops and loop.is_running():
            loop.call_soon_threadsafe(ops.put_nowait, op)

    async def _run(self) -> None:
        backoff = 1
        idle_since = None
        while True:
            if not self.watching():
                idle_since = idle_since or time.monotonic()
                if time.monotonic() - idle_since >= self._idle:
                    break
                await asyncio.sleep(0.5)
                continue
            idle_since = None
            try:
                await self._session()
                backoff = 1
            except Exception as e:                           # noqa: BLE001
                self._set_status(f"재접속 대기 — {type(e).__name__}")
                await asyncio.sleep(backoff)
                backoff = min(RECONNECT_MAX, backoff * 2)
        self._set_status("대기")
        with self._cond:
            self._thread = None
        asyncio.get_running_loop().stop()

    async def _session(self) -> None:
        token = self._token or _default_token
        connect = self._connect or _default_connect
        t, url = token()
        async with connect(url) as ws:
            await ws.send(json.dumps({"trnm": "LOGIN", "token": t}))
            m = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if str(m.get("return_code")) != "0":
                import flow                                # noqa: PLC0415
                if flow.auth_rejected(m):                  # 실측: return_code 805004 · "[CODE=8005, …]"
                    flow.invalidate_token()                # 다음 시도가 새 토큰으로 LOGIN 한다
                raise RuntimeError(f"LOGIN {m.get('return_code')} {str(m.get('return_msg'))[:60]}")
            reg = self.watching()
            if reg:
                await ws.send(json.dumps(_reg("REG", reg)))
            self._set_status("실시간")
            idle_since = None
            while True:
                while not self._ops.empty():
                    op, code = self._ops.get_nowait()
                    with self._cond:
                        live = code in self._refs
                    if op == "REG" and live:
                        await ws.send(json.dumps(_reg("REG", [code])))
                    elif op == "REMOVE" and not live:
                        await ws.send(json.dumps(_reg("REMOVE", [code])))
                if not self.watching():
                    idle_since = idle_since or time.monotonic()
                    if time.monotonic() - idle_since >= self._idle:
                        return                              # 아무도 안 본다 → 연결을 닫는다
                else:
                    idle_since = None
                try:
                    raw = await asyncio.wait_for(ws.recv(), 0.5)
                except asyncio.TimeoutError:
                    continue
                m = json.loads(raw)
                tr = m.get("trnm")
                if tr == "PING":
                    await ws.send(raw)                      # 받은 그대로 돌려줘야 연결이 유지된다
                elif tr == "REAL":
                    self.handle_real(m)
                elif tr in ("REG", "REMOVE") and str(m.get("return_code")) != "0":
                    self._set_status(f"등록 실패 — {m.get('return_msg')}")

    def handle_real(self, m: dict) -> None:
        for d in m.get("data") or []:
            code, typ, v = d.get("item"), d.get("type"), d.get("values") or {}
            if typ == "0B":
                self.update(code, "trade", parse_trade(v), "ws")
            elif typ == "0D":
                self.update(code, "book", parse_book(v), "ws")


def _reg(trnm: str, codes: list[str]) -> dict:
    return {"trnm": trnm, "grp_no": "1", "refresh": "1",
            "data": [{"item": codes, "type": ["0B", "0D"]}]}


def _default_token():
    import flow                                            # noqa: PLC0415
    t, base = flow.token()
    return t, base.replace("https://", "wss://") + ":10000/api/dostk/websocket"


def _default_connect(url):
    import websockets                                      # noqa: PLC0415
    return websockets.connect(url, ping_interval=None)     # 키움은 자체 PING(trnm) 을 쓴다


HUB = Hub()


# ───────────────────────────── 화면 (종목 페이지 맨 위)

CSS = """
.rt-ob{border:1px solid var(--line);border-radius:8px;padding:8px 10px;background:var(--band)}
.ob{width:100%;font-size:12.5px;table-layout:fixed}
.ob td{border:0;border-bottom:1px solid var(--line);padding:2px 6px;position:relative}
.ob td.q{width:36%} .ob td.p{width:28%;text-align:center;font-weight:600}
.ob .bar{position:absolute;top:2px;bottom:2px;opacity:.16;border-radius:2px}
.ob td.q.ask .bar{right:0;background:var(--neg)} .ob td.q.bid .bar{left:0;background:var(--pos)}
.ob td.q.ask{text-align:right} .ob td.q.bid{text-align:left}
.ob tr.cur td.p{outline:2px solid var(--fg);outline-offset:-2px;border-radius:3px}
.ob tfoot td{border:0;padding-top:6px;color:var(--muted);font-size:11.5px}
.ob span{position:relative}
.rt-err{color:var(--pos);font-size:12px;margin-top:6px}
.rt-head{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;margin:0 0 6px 2px;font-size:12.5px;min-height:1.4em}
.rt-head b{font-size:17px;font-weight:700} .rt-head em{color:var(--muted);font-style:normal;font-size:11px}
.ob td.p small{display:block;font-weight:400;font-size:10px;line-height:1.1;opacity:.75}
"""


def section(code: str) -> str:
    """차트 오른쪽에 붙는 **호가창만**(사용자 요청 2026-09-29: 시가·고가·저가·시각·체결 목록 없이).
    SSE 는 그대로 받는다 — 분봉·오늘 봉이 이 연결의 체결을 'rt' 이벤트로 받아 쓴다."""
    return (f'<style>{CSS}</style>'
            f'<section class="rt-ob" id="rt" data-code="{code}" '
            f'data-today="{time.strftime("%Y-%m-%d")}" aria-label="실시간 10단계 호가">'
            '<div class="rt-head" id="rt-head" aria-live="polite"></div>'        # 현재가 · 전일대비 · 당일 등락률(사용자 요청 2026-10-07)
            '<table class="ob" id="ob"><tbody></tbody><tfoot><tr><td class="q ask" id="ob-ta"></td>'
            '<td class="p">총잔량</td><td class="q bid" id="ob-tb"></td></tr></tfoot></table>'
            '<div class="rt-err" id="rt-err" role="status"></div>'
            '</section>' + JS)


JS = r"""<script>
(function(){
  const box=document.getElementById('rt'); if(!box) return;
  const code=box.dataset.code, $=id=>document.getElementById(id);
  const n=v=>v==null?'—':Number(v).toLocaleString('ko-KR');
  const cls=v=>v>0?'pos':(v<0?'neg':'');
  let base=null;
  function render(s){
    const tr=s.trade||{}, bk=s.book;
    if(tr.base) base=tr.base;
    window.dispatchEvent(new CustomEvent('rt',{detail:s}));   // 분봉·오늘 봉이 받아 쓴다
    $('rt-err').textContent=s.error||'';
    if(tr.price){                                              // 현재가 · 전일대비 · 당일 등락률 (키움 0B 11·12 / REST pred_pre·flu_rt)
      const ch=tr.change, rt=tr.rate, c=cls(ch!=null?ch:(rt||0));
      const sg=v=>v>0?'+':'';
      $('rt-head').innerHTML='<b class="'+c+'">'+n(tr.price)+'</b>'
        +(ch!=null?'<span class="'+c+'">'+sg(ch)+n(ch)+'</span>':'')
        +(rt!=null?'<span class="'+c+'">'+sg(rt)+Number(rt).toFixed(2)+'%</span>':'')
        +(s.src==='ws'?'':'<em>스냅샷</em>');
    }
    if(!bk) return;
    const mx=Math.max(1,...bk.asks.map(a=>a[1]||0),...bk.bids.map(a=>a[1]||0));
    const pc=p=>base&&p?cls(p-base):'';
    const pp=p=>base&&p?'<small>'+(p>base?'+':'')+((p/base-1)*100).toFixed(2)+'%</small>':'';   // 호가 단계마다 전일 종가 대비 %
    const row=(side,p,q)=>{
      const bar='<i class="bar" style="width:'+((q||0)/mx*100).toFixed(1)+'%"></i><span>'+(q?n(q):'')+'</span>';
      const cur=tr.price&&p===tr.price?' class="cur"':'';
      return '<tr'+cur+'>'+(side==='a'?'<td class="q ask">'+bar+'</td>':'<td class="q"></td>')
        +'<td class="p '+pc(p)+'">'+(p?n(p)+pp(p):'')+'</td>'
        +(side==='b'?'<td class="q bid">'+bar+'</td>':'<td class="q"></td>')+'</tr>';
    };
    let h='';
    for(let i=9;i>=0;i--) h+=row('a',bk.asks[i][0],bk.asks[i][1]);
    for(let i=0;i<10;i++) h+=row('b',bk.bids[i][0],bk.bids[i][1]);
    $('ob').tBodies[0].innerHTML=h;
    $('ob-ta').textContent=n(bk.tot_ask); $('ob-tb').textContent=n(bk.tot_bid);
  }
  let es;
  function open(){
    es=new EventSource('/api/rt/'+code);
    es.onmessage=e=>{try{render(JSON.parse(e.data))}catch(_){}};
    es.onerror=()=>{$('rt-err').textContent='재연결 중…'};
  }
  open();
  window.addEventListener('pagehide',()=>es&&es.close());
})();
</script>"""


# ───────────────────────────── 일·주·월봉 차트에 '오늘 봉'을 실시간으로
#
# 그리기는 static/chart.js 의 FC.applyLive 가 한다(2026-10-02 캔버스로 옮김):
#   일봉 = 옅은 '오늘(장중)' 봉을 덧붙인다(확정 봉과 구분) · 주·월봉 = 이번 주·달 봉의 고·저·종에 오늘을 합친다(시가 유지),
#   다음 주·달로 넘어간 첫날이면 새 봉. 이평은 마지막 점만 다시 센다. 세로축은 차트가 보이는 봉에 맞춘다.
# ★ 실시간 체결(ws)·정규장(09:00~15:30) 것만 쓴다. REST 스냅샷(ka10001)에는 날짜가 없어서
#   주말·휴장일에 전 거래일 값을 '오늘 봉'으로 그릴 위험이 있다.
#   첫 값은 날짜가 붙은 네이버 시세(flowapp /api/today/<코드>)로 채운다(2026-10-15) — 장 마감 뒤에 연 화면에도 오늘 봉이 바로 나온다.
DAILY_JS = r"""<script>
(function(){
  const rt=document.getElementById('rt'); if(!rt) return;
  const today=rt.dataset.today;                        // YYYY-MM-DD (서버 KST)
  let last=null, dirty=false;
  window.addEventListener('rt',e=>{
    const s=e.detail, t=s.trade;
    if(s.src!=='ws'||!t||!t.price||!t.open||!t.high||!t.low||!t.time) return;
    if(t.time<'090000'||t.time>'153059') return;        // 정규장 체결만
    last={today,price:t.price,open:t.open,high:t.high,low:t.low}; dirty=true;
  });
  // 첫 값은 날짜가 붙은 네이버 시세로 바로 채운다 — 체결을 기다리지 않는다(장 마감 뒤에 연 화면·체결이 뜸한 종목). 웹소켓 체결이 먼저 왔으면 덮지 않는다
  fetch('/api/today/'+rt.dataset.code).then(r=>r.json()).then(b=>{
    if(!last&&b&&b.today===today&&b.price&&b.open&&b.high&&b.low){ last={today,price:b.price,open:b.open,high:b.high,low:b.low}; dirty=true; }
  }).catch(()=>{});
  setInterval(()=>{ if(dirty&&last&&window.FC&&FC.applyLive(last)) dirty=false; },1000);   // 초당 1회 · 휠·드래그 중이면 미룬다(FC.applyLive 가 false)
})();
</script>"""
