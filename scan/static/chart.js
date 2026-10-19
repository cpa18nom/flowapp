/* flowapp 차트 — lightweight-charts(캔버스)로 일·주·월봉과 분봉을 그린다 (2026-10-02, plotly 대체).
 *
 * 왜 바꿨나: plotly 는 확대·이동 한 번에 SVG 봉 3천 개를 두 번(가로, 그다음 세로 맞춤) 다시 그려 초당 약 10장이었다.
 * 캔버스는 보이는 봉만 그리고, 세로축이 보이는 봉에 맞춰 같은 프레임에 같이 움직인다(비율 유지).
 *
 * 자료는 서버 viewer.chart_data() 가 만든다. 여기서는 그리기만 한다.
 *   칸: 0 가격(+아래 띠: 개인·세력 분산) · 1 거래대금 · 2 스토캐스틱 · 3 매집수량 · 4~7 재무(현금·매출·영업이익·순이익)
 *   보조지표를 접으면 0·1 칸만 만든다.
 * 십자선: 점선 가로·세로, 날짜는 차트 **위**에(사용자 요청 2026-10-02), 그 봉의 시·고·저·종은 위 정보줄에.
 * 휠 = 커서 기준 확대·축소, 드래그 = 이동(관성), 더블클릭·「처음 크기」 = 전 구간.
 */
(function () {
  'use strict';
  const LW = window.LightweightCharts;
  const css = k => getComputedStyle(document.documentElement).getPropertyValue(k).trim();
  const WD = '일월화수목금토';
  const UP = '#d64545', DN = '#2f6fd0';
  // 기본 비율 = 사용자가 맞춰 둔 화면(2026-10-08 · 이전 470/64/100/240). 분산 띠를 별도 칸(band)으로 떼면서 가격 칸에서 그 높이(347×25%)를 뺐다
  const HEIGHTS = { price: 260, val: 85, stoch: 92, flow: 125, band: 87, dart: 92 };
  // 칸 크기 설정·고정(사용자 요청 2026-10-08): 「크기 고정」을 누르면 그때의 칸 높이를 저장하고 경계 끌기·전체 손잡이를 잠근다
  const paneSizes = () => { try { return JSON.parse(localStorage.getItem('flowapp.paneH') || '{}') || {}; } catch (e) { return {}; } };
  const paneLocked = () => { try { return localStorage.getItem('flowapp.paneLock') === '1'; } catch (e) { return false; } };
  // 거래대금·스토캐스틱·매집수량 칸을 하나씩 접는다(사용자 요청 2026-10-08) — 접힌 칸은 제목 줄 높이만 남기고, 상태는 브라우저가 기억한다
  const PANE_FOLD_H = 24;
  const paneFold = () => { try { return JSON.parse(localStorage.getItem('flowapp.paneFold') || '{}') || {}; } catch (e) { return {}; } };
  // 차트+보조지표 상자 전체 높이를 아래 손잡이로 한 번에 조절한다(사용자 요청 2026-10-08) — 기본 높이 대비 배율로 기억해서 칸을 접고 펴도 비율이 유지된다
  const chartScale = () => { try { const v = +localStorage.getItem('flowapp.chartScale2'); return v >= 0.4 && v <= 4 ? v : 1; } catch (e) { return 1; } };
  function sizeGrip(box, natural) {
    let g = box.nextElementSibling;
    if (!g || !g.classList.contains('fc-grip')) {
      g = document.createElement('div');
      g.className = 'fc-grip'; g.setAttribute('role', 'separator'); g.setAttribute('aria-orientation', 'horizontal');
      g.title = '끌어서 차트 전체 높이 조절 · 두 번 누르면 기본 높이';
      box.after(g);
    }
    g.hidden = paneLocked();
    const save = v => { try { if (v === 1) localStorage.removeItem('flowapp.chartScale2'); else localStorage.setItem('flowapp.chartScale2', String(v)); } catch (e) {} };
    g.onpointerdown = e => {
      e.preventDefault(); g.setPointerCapture(e.pointerId);
      const y0 = e.clientY, h0 = box.getBoundingClientRect().height;
      g.onpointermove = ev => { box.style.height = Math.max(natural * 0.4, Math.min(natural * 4, h0 + ev.clientY - y0)) + 'px'; };
      g.onpointerup = g.onpointercancel = () => {
        g.onpointermove = g.onpointerup = g.onpointercancel = null;
        save(Math.round(box.getBoundingClientRect().height / natural * 1000) / 1000);
      };
    };
    g.ondblclick = () => { save(1); try { localStorage.removeItem('flowapp.paneH'); } catch (e) {} FC.rebuild(); };   // 칸 크기 설정까지 기본으로
  }
  const legendItem = (name, col) => `<label class="fc-flow-item" data-actor="${name}" title="눌러서 ${name} 선 보이기/숨기기"><input type="checkbox" checked><i style="border-color:${col}"></i>${name} <b>—</b></label>`;
  const foldBtn = (k, pf) => `<button type="button" class="fc-pf" data-pf="${k}" aria-expanded="${!pf[k]}" title="${pf[k] ? '이 칸 펴기' : '이 칸 접기'}">${pf[k] ? '▸' : '▾'}</button>`;
  const FC_BUSY = { until: 0 };                       // 사용자가 휠·드래그 중이면 실시간 다시 그리기를 미룬다

  const NF = new Intl.NumberFormat('ko-KR');          // toLocaleString 은 부를 때마다 포매터를 새로 만든다 — 축 눈금이 프레임마다 수십 번 부른다(휠 확대 프로파일 8.6%)
  const fmtInt = v => v == null ? '—' : NF.format(Math.round(v));
  const fmtQty = v => {                              // 매집수량(주): 만·억 단위
    if (v == null) return '—';
    const a = Math.abs(v);
    return a >= 1e8 ? (v / 1e8).toFixed(1) + '억' : a >= 1e4 ? NF.format(Math.round(v / 1e4)) + '만' : Math.round(v) + '';
  };
  const fmtEok = v => v == null ? '—' : (Math.abs(v) >= 1e4 ? (v / 1e4).toFixed(1) + '조' : NF.format(Math.round(v)) + '억');

  function theme() {
    return { bg: css('--bg') || '#fff', fg: css('--fg') || '#1a1a1a', line: css('--line') || '#d8dde3',
             muted: css('--muted') || '#6b7280' };
  }
  function series(T, Y) {                            // null 은 빈칸(whitespace) — 선이 끊긴다
    const out = new Array(T.length);
    for (let i = 0; i < T.length; i++) out[i] = Y[i] == null ? { time: T[i] } : { time: T[i], value: Y[i] };
    return out;
  }
  function dayLabel(t, period) {                     // 'YYYY-MM-DD' 또는 분봉 숫자 시각
    if (typeof t === 'number') {                     // 분봉: KST 를 UTC 처럼 실어 둔 초
      const d = new Date(t * 1000);
      const p = n => String(n).padStart(2, '0');
      return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} (${WD[d.getUTCDay()]}) ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
    }
    const s = typeof t === 'string' ? t : `${t.year}-${String(t.month).padStart(2, '0')}-${String(t.day).padStart(2, '0')}`;
    if (period === '월') return s.slice(0, 7);
    const wd = WD[new Date(s + 'T00:00:00Z').getUTCDay()];
    return period === '주' ? `${s} (${wd}) 주` : `${s} (${wd})`;
  }
  function tsKey(t) {                                // time 값 → 비교 가능한 문자열/숫자
    if (t == null) return null;
    if (typeof t === 'string' || typeof t === 'number') return t;
    return `${t.year}-${String(t.month).padStart(2, '0')}-${String(t.day).padStart(2, '0')}`;
  }
  function lowerBound(values, target) {
    let lo = 0, hi = values.length;
    while (lo < hi) { const m = (lo + hi) >>> 1; if (values[m] < target) lo = m + 1; else hi = m; }
    return lo;
  }
  function monthsAgo(last, months) {
    const d = new Date(last + 'T00:00:00Z'), day = d.getUTCDate();
    d.setUTCDate(1); d.setUTCMonth(d.getUTCMonth() - months);
    const max = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 0)).getUTCDate();
    d.setUTCDate(Math.min(day, max)); return d.toISOString().slice(0,10);
  }

  // ── 분산 띠 채우기: 개인이 위면 파랑, 세력이 위면 빨강(예전 plotly fill 과 같은 뜻) ──
  class BandFill {
    constructor(T, P, S, cBlue, cRed) { Object.assign(this, { T, P, S, cBlue, cRed }); this._v = [this._view()]; }
    attached(p) { this.chart = p.chart; this.series = p.series; }
    detached() { this.chart = this.series = null; }
    updateAllViews() {}
    paneViews() { return this._v; }
    _view() {
      const self = this;
      return {
        zOrder: () => 'bottom',
        renderer: () => ({
          draw(target) {
            if (!self.chart || !self.series || self.hidden) return;   // 분산 두 선을 다 끄면 채우기도 끈다
            const ts = self.chart.timeScale(), s = self.series, { T, P, S } = self;
            target.useMediaCoordinateSpace(({ context: ctx }) => {
              // ★ 봉마다 사각형 하나씩(3천 개) 넣으면 채우기가 프레임을 다 먹는다 — 휠 확대가 끊기던 원인(실측 2026-10-02:
              //   채우기를 끄면 평균 25ms → 17ms, 최악 117ms → 33ms). 색이 같은 구간을 **다각형 하나**로 잇고(위 선은 앞으로,
              //   아래 선은 거꾸로), 같은 화소 열에 겹치는 점은 건너뛴다. 칠해지는 넓이는 예전과 같다.
              const blue = new Path2D(), red = new Path2D();
              let run = null, up = null, lastX = -1e9;               // run = [x, yp, ys, …] 이어지는 같은 색 구간
              const flush = () => {
                if (run && run.length >= 6) {
                  const path = up ? blue : red, n = run.length / 3;
                  path.moveTo(run[0], run[1]);
                  for (let k = 1; k < n; k++) path.lineTo(run[3 * k], run[3 * k + 1]);
                  for (let k = n - 1; k >= 0; k--) path.lineTo(run[3 * k], run[3 * k + 2]);
                  path.closePath();
                }
                run = null;
              };
              const vr = ts.getVisibleRange();                 // 보이는 구간만 그린다(앞뒤 한 점씩 더)
              let i0 = 0, i1 = T.length - 1;
              if (vr) { const lo = tsKey(vr.from), hi = tsKey(vr.to);
                i0 = Math.max(0, lowerBound(T, lo) - 1);
                i1 = Math.min(T.length - 1, lowerBound(T, hi) + 1); }
              for (let i = i0; i <= i1; i++) {
                const x = ts.timeToCoordinate(T[i]);
                if (x == null || P[i] == null || S[i] == null) { flush(); continue; }
                const yp = s.priceToCoordinate(P[i]), ys = s.priceToCoordinate(S[i]);
                if (yp == null || ys == null) { flush(); continue; }
                const nowUp = P[i] >= S[i];
                if (run && nowUp === up && x - lastX < 1 && i < i1) continue;      // 같은 화소 열 — 색이 그대로면 건너뛴다
                if (run) run.push(x, yp, ys);
                if (!run || nowUp !== up) {                    // 색이 바뀌는 점: 앞 구간은 이 점까지 칠하고, 새 구간이 이 점에서 시작한다
                  flush(); run = [x, yp, ys]; up = nowUp;
                }
                lastX = x;
              }
              flush();
              ctx.fillStyle = self.cBlue; ctx.fill(blue);
              ctx.fillStyle = self.cRed; ctx.fill(red);
            });
          },
        }),
      };
    }
  }

  // ── 공통: 차트 하나 만들기 ──
  function makeChart(el, period) {
    const th = theme();
    const chart = LW.createChart(el, {
      autoSize: true,
      layout: { background: { color: 'transparent' }, textColor: th.fg, fontSize: 11,
                fontFamily: '-apple-system,"Segoe UI","Malgun Gothic",sans-serif',
                panes: { separatorColor: '#aab4c2', separatorHoverColor: th.muted, enableResize: true } },   // 칸 경계를 또렷하게(사용자 요청 2026-10-02)
      grid: { vertLines: { visible: false }, horzLines: { color: th.line, style: LW.LineStyle.Dotted } },
      crosshair: {
        mode: LW.CrosshairMode.Normal,
        vertLine: { style: LW.LineStyle.Dashed, width: 1, color: th.muted, labelVisible: false },   // 날짜는 위에 따로
        horzLine: { style: LW.LineStyle.Dashed, width: 1, color: th.muted, labelBackgroundColor: '#555' },
      },
      rightPriceScale: { borderColor: th.line, minimumWidth: 72 },
      timeScale: { borderColor: th.line, rightOffset: 4, minBarSpacing: 0.3, fixLeftEdge: false,
                   timeVisible: typeof period === 'number', secondsVisible: false,
                   tickMarkFormatter: typeof period === 'number' ? minuteTick : undefined },
      kineticScroll: { mouse: true, touch: true },
      handleScale: { axisPressedMouseMove: { time: true, price: false }, mouseWheel: false, pinch: true },   // 휠은 smoothWheel 이 애니메이션으로
      handleScroll: { mouseWheel: false, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
      localization: { locale: 'ko-KR',
                      timeFormatter: t => dayLabel(t, period) },
    });
    chart.disposeInteractions = smoothWheel(chart, el);
    return chart;
  }

  // ── 부드러운 휠 확대·축소 (사용자 요청 2026-10-02: "렉 걸리듯 버벅인다") ──
  // 라이브러리 기본 휠은 눈금 한 칸마다 한 번에 뛴다. 목표 범위를 쌓아 두고 매 프레임 조금씩(시간 기준) 다가가게 그린다.
  // 커서 아래 봉이 제자리에 남는다(커서 기준 확대). 가로 휠·Shift+휠은 좌우 이동.
  function smoothWheel(chart, el) {
    const ts = chart.timeScale();
    let target = null, raf = 0, bars = 0, prev = 0;
    const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const cancel = () => { if (raf) cancelAnimationFrame(raf); target = null; raf = 0; prev = 0; };
    // 프레임 수가 아니라 **시간**으로 다가간다. 예전엔 프레임마다 35% — 144Hz 화면에서는 60Hz 보다 2.4배 빨리 끝나 '뚝' 끊겨 보였다.
    const TAU = 55;                                         // ms — 남은 거리가 1/e 로 줄어드는 시간(휠 한 칸이 약 0.2초에 걸쳐 풀린다)
    const step = (now) => {
      const cur = ts.getVisibleLogicalRange();
      if (!cur || !target) { raf = 0; prev = 0; return; }
      const k = 1 - Math.exp(-Math.min(now - (prev || now - 16.7), 50) / TAU);
      prev = now;
      const f = cur.from + (target.from - cur.from) * k, t = cur.to + (target.to - cur.to) * k;
      const eps = Math.max(0.02, (target.to - target.from) * 0.0005);   // 화면 폭의 0.05% 안이면 끝(반 화소 미만)
      const done = Math.abs(target.from - f) < eps && Math.abs(target.to - t) < eps;
      ts.setVisibleLogicalRange(done ? target : { from: f, to: t });
      if (done) { target = null; raf = 0; prev = 0; } else raf = requestAnimationFrame(step);
    };
    const wheel = ev => {
      const cur = ts.getVisibleLogicalRange(); if (!cur) return;
      ev.preventDefault();
      FC_BUSY.until = performance.now() + 700;
      const base = target || { from: cur.from, to: cur.to }, w = base.to - base.from;
      if (!bars) bars = (chart.panes()[0].getSeries()[0]?.data().length) || 1000;      // data() 는 배열을 새로 만든다 — 휠 한 칸마다 부르지 않는다
      const n = bars;
      const horiz = Math.abs(ev.deltaX) > Math.abs(ev.deltaY) || ev.shiftKey;
      const d = (ev.deltaMode === 1 ? 33 : 1) * (horiz ? (ev.deltaX || ev.deltaY) : ev.deltaY);
      if (horiz) { const sh = d / 400 * w; target = { from: base.from + sh, to: base.to + sh }; }
      else {
        const rect = el.getBoundingClientRect();
        const a = ts.coordinateToLogical(ev.clientX - rect.left) ?? (base.from + base.to) / 2;
        const k = Math.exp(d * 0.0012);                       // 휠 한 칸(100) ≈ 12.7%
        let nw = Math.max(12, Math.min(w * k, n * 1.15));
        const r = (a - base.from) / w;
        target = { from: a - r * nw, to: a - r * nw + nw };
      }
      if (reduceMotion) { ts.setVisibleLogicalRange(target); target = null; }
      else if (!raf) raf = requestAnimationFrame(step);
    };
    const down = () => { cancel(); FC_BUSY.until = Infinity; };
    const up = () => { if (FC_BUSY.until === Infinity) FC_BUSY.until = performance.now() + 500; };
    el.addEventListener('wheel', wheel, { passive: false });
    el.addEventListener('pointerdown', down);
    window.addEventListener('pointerup', up);
    window.addEventListener('pointercancel', up);
    window.addEventListener('blur', up);
    return () => {
      cancel(); up(); el.removeEventListener('wheel', wheel);el.removeEventListener('pointerdown', down);
      window.removeEventListener('pointerup', up);window.removeEventListener('pointercancel', up);window.removeEventListener('blur', up);
    };
  }
  function minuteTick(t, type) {                     // 분봉 눈금: 날이 바뀌면 월/일, 아니면 시:분
    const d = new Date(t * 1000), p = n => String(n).padStart(2, '0');
    return type <= 2 ? `${p(d.getUTCMonth() + 1)}/${p(d.getUTCDate())}` : `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
  }

  // 차트 위 날짜 꼬리표 + 칸 제목(겹쳐 그린 DOM) — 캔버스를 다시 그리지 않는다
  function overlay(host) {
    host.style.position = 'relative';
    const tag = document.createElement('div');
    tag.className = 'fc-date';
    tag.hidden = true;
    host.appendChild(tag);
    return tag;
  }
  function paneTitles(host, chart, titles) {
    host.querySelectorAll('.fc-title').forEach(e => e.remove());
    const els = titles.map(([i, html]) => {
      const d = document.createElement('div');
      d.className = 'fc-title';
      d.dataset.pane = i;
      d.innerHTML = html;
      host.appendChild(d);
      return d;
    });
    const place = () => {
      const panes = chart.panes();
      let top = 0;
      const tops = panes.map(p => { const t = top; top += p.getHeight() + 1; return t; });
      for (const d of els) {
        // 제목 상자는 칸의 왼쪽 위 모서리에 딱 붙인다(사용자 요청 2026-10-08)
        const i = +d.dataset.pane; d.style.top = (tops[i] ?? 0) + 'px'; d.hidden = i >= panes.length;
      }
    };
    const state = { els, place, raf: 0 };
    state.raf = requestAnimationFrame(() => {state.raf=0;place();});
    // 칸 경계를 끌어 높이를 바꾸면 제목도 따라 내려가야 한다 — 경계 끌기는 ResizeObserver 에 안 잡힌다(상자 크기는 그대로)
    const later = () => { if (!state.raf) state.raf = requestAnimationFrame(() => {state.raf=0;place();}); };
    host.onpointermove = e => { if (e.buttons) later(); };
    host.onpointerup = later;
    return state;
  }

  // ── 일·주·월봉 ──
  const CHARTS = {};                                 // period key(d·w·m) → 핸들
  const DATA = {}, SAVED_RANGES = {}, SAVED_MONTHS = {};
  function dispose(h) {
    h.chart.disposeInteractions(); h.observer.disconnect();
    if (h.hoverRaf) cancelAnimationFrame(h.hoverRaf);
    if (h.titles.raf) cancelAnimationFrame(h.titles.raf);
    h.chart.remove();
  }
  function buildDaily(key, opts) {
    const D = DATA[key] || (DATA[key] = JSON.parse(document.getElementById('cd-' + key).textContent));
    const box = document.getElementById('plot-' + key);
    const old = CHARTS[key];
    const keepRange = old ? old.chart.timeScale().getVisibleLogicalRange() : SAVED_RANGES[key];
    if (old) { dispose(old); box.innerHTML = ''; }
    const fold = !!opts.fold;
    const H = Object.assign({}, HEIGHTS, paneSizes()), nDart = fold || !opts.finance ? 0 : D.dart.length;
    const pf = paneFold(), hp = k => pf[k] ? PANE_FOLD_H : H[k];
    // 칸: 0 가격 · 1 거래대금 · 2 스토캐스틱 · 3 매집수량 · 4 분산비율(개인·세력 분산 띠, 사용자 요청 2026-10-08 — 매집수량 칸 바로 아래) · 그 뒤 재무
    // 「보조지표 접기」로 3·4번 칸이 없을 때만 분산 띠를 예전처럼 가격 칸 아래에 둔다(그만큼 가격 칸을 키워 캔들 영역은 그대로)
    const hasBand = D.bt.length > 1, bandOwn = hasBand && !fold;
    const bandPane = bandOwn ? 4 : 0, DART0 = 4 + (bandOwn ? 1 : 0);
    const hPrice = H.price + (hasBand && fold ? H.band : 0), priceBandFrac = hasBand && fold ? H.band / hPrice : 0;
    const flowTop = () => box.clientWidth < 600 ? 0.45 : 0.2;   // 매집 칸 위쪽 = 한 줄 범례 자리
    const bandMargins = () => bandOwn ? { top: 0.26, bottom: 0.04 } : { top: 1 - priceBandFrac, bottom: 0 };
    const want = [hPrice, hp('val')].concat(fold ? [] : [hp('stoch'), hp('flow')].concat(bandOwn ? [hp('band')] : [], Array(nDart).fill(H.dart)));
    const natural = want.reduce((a, b) => a + b, 0) + 30;
    const locked = paneLocked();
    box.style.height = Math.round(natural * (locked ? 1 : chartScale())) + 'px';
    sizeGrip(box, natural);
    const chart = makeChart(box, D.period);
    if (locked) chart.applyOptions({ layout: { panes: { enableResize: false } } });   // 고정이면 칸 경계 끌기도 막는다
    const th = theme(), T = D.t;
    const S = {};                                    // 이름 → 시리즈 (범례 토글·실시간)

    // 0 가격
    let main;
    if (D.o) {
      main = chart.addSeries(LW.CandlestickSeries, { upColor: UP, downColor: DN, borderUpColor: UP, borderDownColor: DN,
        wickUpColor: UP, wickDownColor: DN, priceLineVisible: false, lastValueVisible: true,
        priceFormat:{type:'custom',formatter:fmtInt,minMove:1} }, 0);
      const bars = new Array(T.length);
      for (let i = 0; i < T.length; i++) bars[i] = D.o[i] == null ? { time: T[i] } :
        { time: T[i], open: D.o[i], high: D.h[i], low: D.l[i], close: D.c[i] };
      main.setData(bars);
    } else {
      main = chart.addSeries(LW.LineSeries, { color: '#888', lineWidth: 1.5, priceLineVisible: false,
        priceFormat:{type:'custom',formatter:fmtInt,minMove:1} }, 0);
      main.setData(series(T, D.line));
    }
    S['주가'] = main;
    for (const [n, col] of Object.entries(D.colors.ma)) {
      if (!D.ma[n]) continue;
      const s = chart.addSeries(LW.LineSeries, { color: col, lineWidth: 1, priceLineVisible: false, lastValueVisible: false,
        crosshairMarkerVisible: false }, 0);
      s.setData(series(T, D.ma[n]));
      S['MA' + n] = s;
    }
    if (D.bb40) {                                     // 상한선(대표가 40봉·2σ) — 저자 화면처럼 점선
      const s = chart.addSeries(LW.LineSeries, { color: D.colors.bb40, lineWidth: 1, lineStyle: LW.LineStyle.Dotted,
        priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false }, 0);
      s.setData(series(T, D.bb40));
      S['상한선'] = s;
    }
    for (const [name, col] of Object.entries(D.colors.ind || {})) {   // 보조지표 4종(2026-10-07): 볼린저상한·일목기준선·엔벨로프상단·VWAP
      if (!D.ind || !D.ind[name]) continue;
      const s = chart.addSeries(LW.LineSeries, { color: col, lineWidth: 1.2, priceLineVisible: false, lastValueVisible: false,
        crosshairMarkerVisible: false }, 0);
      s.setData(series(T, D.ind[name]));
      S[name] = s;
    }
    const sigMarks = (D.sig || []).map(t => ({ time: t, position: 'aboveBar', color: '#e45756', shape: 'arrowDown', size: 1.4 }));
    const markers = LW.createSeriesMarkers(main, sigMarks);

    // 접힌 칸은 선을 아예 넣지 않고 빈 칸만 붙잡아 둔다 — 선을 숨기기(visible:false)만 하면 값 없는 축 때문에 'Value is null' 로 칸 높이가 0 이 된다(실측)
    // 칸을 먼저 만들어 둔다 — 분산 띠가 4번 칸에 바로 들어갈 수 있게
    while (chart.panes().length < want.length) chart.addPane(true);

    // 분산 띠: 개인·세력 분산(0~1 고정 축)
    let bandFill = null;
    if (hasBand && !(bandOwn && pf.band)) {
      const fixed01 = () => ({ priceRange: { minValue: 0, maxValue: 1 } });
      // 제 칸이면 오른쪽 축(0~1 눈금)을 그대로 쓰고, 가격 칸에 얹힐 때만 숨은 'band' 축
      const mk = (col) => chart.addSeries(LW.LineSeries, { color: col, lineWidth: 2, priceScaleId: bandOwn ? 'right' : 'band',
        priceLineVisible: false, lastValueVisible: false, autoscaleInfoProvider: fixed01,
        priceFormat: { type: 'custom', formatter: v => v.toFixed(2) } }, bandPane);
      const sp = mk(D.colors.blue), ss = mk(D.colors.red);
      sp.setData(series(D.bt, D.bp)); ss.setData(series(D.bt, D.bs));
      if (!bandOwn) chart.priceScale('band', 0).applyOptions({ scaleMargins: bandMargins(), visible: false });
      bandFill = new BandFill(D.bt, D.bp, D.bs, D.colors.blue_f, D.colors.red_f); sp.attachPrimitive(bandFill);
      sp.createPriceLine({ price: 1, color: th.line, lineStyle: LW.LineStyle.Dotted, lineWidth: 1, axisLabelVisible: false });
      S['개인분산'] = sp; S['세력분산'] = ss;
    }

    // 1 거래대금(억)
    if (D.val && D.val.length && !pf.val) {
      const v = chart.addSeries(LW.HistogramSeries, { priceLineVisible: false, lastValueVisible: false,
        priceFormat: { type: 'custom', formatter: x => fmtInt(x) + '억', minMove: 0.01 } }, 1);
      const vd = new Array(T.length);
      for (let i = 0; i < T.length; i++) vd[i] = D.val[i] == null ? { time: T[i] } :
        { time: T[i], value: D.val[i], color: (D.c && D.o && D.c[i] >= D.o[i]) ? 'rgba(214,69,69,.7)' : 'rgba(47,111,208,.7)' };
      v.setData(vd);
      S['거래대금'] = v;
    }

    const titles = [[1, foldBtn('val', pf) + '<b>거래대금(억)</b>']];
    if (!fold) {
      // 2 스토캐스틱: fastD·slowD 는 0~100 고정, osc 막대는 0 을 가운데 둔 대칭 축
      if (D.fd && D.fd.length && pf.stoch) titles.push([2, foldBtn('stoch', pf) + '<b>스토캐스틱</b>']);
      else if (D.fd && D.fd.length) {
        const fix100 = () => ({ priceRange: { minValue: 0, maxValue: 100 } });
        const f = chart.addSeries(LW.LineSeries, { color: '#ff7f0e', lineWidth: 1.2, priceLineVisible: false,
          lastValueVisible: false, autoscaleInfoProvider: fix100, priceFormat: { type: 'custom', formatter: x => x.toFixed(0) } }, 2);
        const sl = chart.addSeries(LW.LineSeries, { color: '#7b3fb5', lineWidth: 1.2, priceLineVisible: false,
          lastValueVisible: false, autoscaleInfoProvider: fix100 }, 2);
        f.setData(series(T, D.fd)); sl.setData(series(T, D.sd));
        for (const lv of [20, 80]) f.createPriceLine({ price: lv, color: th.line, lineStyle: LW.LineStyle.Dotted, lineWidth: 1, axisLabelVisible: false });
        const sym = orig => { const r = orig(); if (!r) return r; const m = Math.max(Math.abs(r.priceRange.minValue), Math.abs(r.priceRange.maxValue)) * 1.1 || 1;
          return { priceRange: { minValue: -m, maxValue: m } }; };
        const osc = chart.addSeries(LW.HistogramSeries, { priceScaleId: 'osc', priceLineVisible: false, lastValueVisible: false,
          autoscaleInfoProvider: sym }, 2);
        const od = new Array(T.length);
        for (let i = 0; i < T.length; i++) od[i] = D.osc[i] == null ? { time: T[i] } :
          { time: T[i], value: D.osc[i], color: D.osc[i] >= 0 ? 'rgba(214,69,69,.55)' : 'rgba(47,111,208,.55)' };
        osc.setData(od);
        chart.priceScale('osc', 2).applyOptions({ visible: false });
        if (D.turn && D.turn.length)
          LW.createSeriesMarkers(osc, D.turn.map(t => ({ time: t, position: 'belowBar', color: '#e45756', shape: 'arrowUp', size: 0.6 })));
        titles.push([2, foldBtn('stoch', pf) + '<b>스토캐스틱</b> <span style="color:#ff7f0e">fastD</span> · <span style="color:#7b3fb5">slowD</span> · osc 막대']);
      }
      // 3 매집수량
      for (const [name, col] of Object.entries(pf.flow ? {} : D.colors.flow)) {
        // 굵은 선 + 오른쪽 축에 「이름 값」 꼬리표(title) — 선만으로는 주체가 구분되지 않았다(사용자 2026-10-06)
        const s = chart.addSeries(LW.LineSeries, { color: col, lineWidth: 2.6, priceLineVisible: false, lastValueVisible: true, title: name,
          priceFormat: { type: 'custom', formatter: v => v < 0 ? '' : fmtQty(v) } }, 3);   // 보유수량은 음수가 없다 — 아래 분산 띠 자리에 축 눈금(-1,000만 등)이 찍히지 않게
        s.setData(series(D.ft, D.flow[name]));
        S[name] = s;
      }
      if (pf.flow) titles.push([3, foldBtn('flow', pf) + '<b>매집수량 (주)</b>']);
      else {
        // 「매집수량」 체크 = 주체 다섯 선을 한꺼번에(사용자 요청 2026-10-08) · 한 줄 범례
        titles.push([3, '<strong>' + foldBtn('flow', pf) + '<label class="fc-flow-all" title="주체 선 전부 보이기/숨기기"><input type="checkbox" checked>매집수량</label><span class="fc-flow-day"></span></strong>'
          + Object.entries(D.colors.flow).map(([name, col]) => legendItem(name, col)).join('')]);
      }
      // 4 분산비율 — 띠 시리즈는 위에서 넣었다
      if (bandOwn) titles.push([4, '<strong>' + foldBtn('band', pf) + '분산비율</strong>'
        + (pf.band ? '' : legendItem('개인분산', D.colors.blue) + legendItem('세력분산', D.colors.red))]);
      if (nDart) D.dart.forEach((p, i) => titles.push([DART0 + i, `<b style="color:${p.col}">${p.title}</b><span class="fc-v"></span>`]));
    }

    // 칸 높이
    const panes = chart.panes();
    // 재무 칸은 선이 아직 없다 — 빈 칸이 지워지지 않게 붙잡아 둔다
    if (!fold) for (let i = panes.length; i < want.length; i++) chart.addPane(true);
    chart.panes().forEach((p, i) => { if (want[i]) p.setStretchFactor(want[i]); });
    // 축 설정은 칸을 다 만든 **뒤에** 칸마다 따로 — 나중에 생긴 칸은 0번 칸 설정(로그·아래 띠 여백)을 그대로 복사한다(실측)
    chart.panes().forEach((p, i) => chart.priceScale('right', i).applyOptions(i === 0
      ? { mode: D.log ? LW.PriceScaleMode.Logarithmic : LW.PriceScaleMode.Normal, scaleMargins: { top: 0.06, bottom: priceBandFrac ? priceBandFrac + 0.02 : 0.03 } }
      : { mode: LW.PriceScaleMode.Normal, scaleMargins: i === 4 && bandOwn ? bandMargins() : { top: i === 1 ? 0.08 : i === 3 ? flowTop() : 0.16, bottom: i === 1 ? 0 : 0.06 } }));
    const tt = paneTitles(box, chart, titles);
    tt.els.forEach(el => el.querySelectorAll('.fc-pf').forEach(b => b.onclick = ev => {
      ev.stopPropagation();
      const st = paneFold(); st[b.dataset.pf] = !st[b.dataset.pf];
      try { localStorage.setItem('flowapp.paneFold', JSON.stringify(st)); } catch (e) {}
      FC.rebuild();
    }));
    const flowLegend = pf.flow ? null : tt.els.find(e => +e.dataset.pane === 3);
    const bandLegend = bandOwn && !pf.band ? tt.els.find(e => +e.dataset.pane === 4) : null;
    const legends = [flowLegend, bandLegend].filter(Boolean);
    for (const L of legends) {
      L.classList.add('fc-flow-legend');
      // 체크로 그 선만 켜고 끈다(사용자 요청 2026-10-08) — 위 보조지표 단추와 같은 상태(LEG_OFF)를 쓴다
      L.querySelectorAll('[data-actor] input').forEach(cb => cb.onchange = () => toggleLeg(cb.parentElement.dataset.actor));
    }
    if (flowLegend) {
      const all = flowLegend.querySelector('.fc-flow-all input');
      // 일부만 켜져 있을 때 누르면 전부 켠다(전부 켜져 있을 때만 전부 끈다)
      if (all) all.onchange = () => { const ns = Object.keys(D.colors.flow); setLegs(ns, ns.every(n => !LEG_OFF.has(n))); };
    }
    const flowItems = legends.flatMap(L => Array.from(L.querySelectorAll('[data-actor]')));
    const flowIdx = new Map(D.ft.map((t,i) => [t,i])), bandIdx = new Map(D.bt.map((t,i) => [t,i]));
    let lastFlow = null;
    const showFlow = (i, selectedDate) => {
      const date = selectedDate || D.ft[i] || '';
      if (date === lastFlow || !legends.length) return;
      lastFlow = date;
      if (flowLegend) flowLegend.querySelector('.fc-flow-day').textContent = date ? ' · ' + date : '';
      const bi = bandIdx.get(date);
      flowItems.forEach(e => {
        const a = e.dataset.actor, band = a === '개인분산' ? D.bp : a === '세력분산' ? D.bs : null;
        const v = band ? (bi == null ? null : band[bi]) : (i == null ? null : D.flow[a][i]);
        e.querySelector('b').textContent = v == null ? '미제공' : band ? v.toFixed(2) : fmtQty(v);
      });
    };
    showFlow(D.ft.length - 1);

    // 처음 보기: 전 구간(예전과 같다). 다시 그릴 때는 보던 창을 유지한다.
    if (keepRange) chart.timeScale().setVisibleLogicalRange(keepRange);
    else {
      const from = lowerBound(T, monthsAgo(T[T.length - 1], 12));
      chart.timeScale().setVisibleLogicalRange({from: Math.max(0, from - 1), to: T.length + 3});
    }

    // 십자선: 위 날짜 꼬리표 + 정보줄
    const tag = overlay(box);
    const info = document.getElementById('info-' + key);
    const idx = new Map(T.map((t, i) => [t, i]));
    let lastInfo = null, lastTag = null;
    const showInfo = i => {
      if (i === lastInfo || i == null || !info || !D.o || D.o[i] == null) return;
      lastInfo = i;
      const prev = i > 0 ? D.c[i - 1] : null, chg = prev ? (D.c[i] / prev - 1) * 100 : null;
      info.innerHTML = `시 ${fmtInt(D.o[i])} · 고 ${fmtInt(D.h[i])} · 저 ${fmtInt(D.l[i])} · 종 <b>${fmtInt(D.c[i])}</b>` +
        (chg == null ? '' : ` <span class="${chg >= 0 ? 'pos' : 'neg'}">${chg >= 0 ? '+' : ''}${chg.toFixed(2)}%</span>`) +
        (D.val && D.val[i] != null ? ` · 거래대금 ${fmtInt(D.val[i])}억` : '') +
        (D.bb40 && D.bb40[i] != null && !LEG_OFF.has('상한선') ? ` · 상한선 ${fmtInt(D.bb40[i])}` : '') +
        Object.keys(D.ind || {}).filter(k => D.ind[k][i] != null && !LEG_OFF.has(k)).map(k => ` · ${k} ${fmtInt(D.ind[k][i])}`).join('');
    };
    showInfo(T.length - 1);
    const hover = param => {
      if (!param.time || !param.point || param.point.x < 0) { tag.hidden = true; showInfo(T.length - 1);showFlow(D.ft.length - 1);return; }
      tag.hidden = false;
      const time = tsKey(param.time);
      if (lastTag !== time) { tag.textContent = dayLabel(param.time, D.period); lastTag = time; }
      const w = tag.offsetWidth, x = Math.max(0, Math.min(param.point.x - w / 2, box.clientWidth - w - 74));
      tag.style.left = x + 'px';
      showInfo(idx.get(time)); showFlow(flowIdx.get(time), time);
    };
    box.ondblclick = () => FC.reset(key);
    const observer = new ResizeObserver(() => {
      tt.place();
      if (!fold) chart.priceScale('right',3).applyOptions({scaleMargins:{top:flowTop(),bottom:0.06}});
    });
    observer.observe(box);
    const h = { key, D, chart, S, main, markers, sigMarks, fold, pf, bandFill, dart0: DART0, want, titles: tt, dart: null, observer, hoverRaf:0,
                months:old ? old.months : (SAVED_MONTHS[key] ?? 12) };
    let latestHover;
    chart.subscribeCrosshairMove(param => {
      latestHover = param;
      if (!h.hoverRaf) h.hoverRaf = requestAnimationFrame(() => { h.hoverRaf=0;hover(latestHover); });
    });
    CHARTS[key] = h;
    chart.timeScale().subscribeVisibleLogicalRangeChange(r => {     // 오른쪽 끝으로 돌아오면 미뤄 둔 오늘 봉을 그린다
      if (r && FC.live && r.to >= D.t.length - 1.5 && h.liveSig == null) applyLive(h, FC.live); });
    applyLegendState(h);
    if (nDart) loadDart(h);
    if (FC.live) applyLive(h, FC.live);
    return h;
  }

  // ── 범례(차트 밖 HTML 단추) ──
  function legend(key) {
    const el = document.getElementById('lg-' + key);
    const h = CHARTS[key];
    if (!el || !h) return;
    const items = [['MA20', h.D.colors.ma['20']], ['MA60', h.D.colors.ma['60']], ['MA120', h.D.colors.ma['120']],
                   ['상한선', h.D.colors.bb40], ...Object.entries(h.D.colors.ind || {}),
                   ['개인분산', h.D.colors.blue], ['세력분산', h.D.colors.red]];
    if (h.sigMarks.length) items.push([`${h.D.sig_label || '신호'} (${h.sigMarks.length}회)`, '#e45756', 'sig']);
    for (const [n, c] of Object.entries(h.D.colors.flow)) items.push([n, c]);
    el.innerHTML = '<span class="lg-cap" title="누르면 그 선을 숨기거나 다시 표시합니다. 선택은 저장됩니다.">보조지표</span>' +
      items.filter(([n, , k]) => k || h.S[n] || (!h.fold && h.D.colors.flow[n]))
      .map(([n, c, k]) => `<button type="button" data-n="${n}"${k ? ` data-k="${k}"` : ''} aria-pressed="${!LEG_OFF.has(k || n)}" class="${LEG_OFF.has(k || n) ? 'off' : ''}">` +
        `<span class="sw${k === 'sig' ? ' mk' : ''}" style="border-top-color:${c}"></span>${n}</button>`).join('');
    el.onclick = ev => {
      const b = ev.target.closest('button'); if (!b) return;
      toggleLeg(b.dataset.k || b.dataset.n);
    };
  }
  function setLegs(names, off) {                      // 여러 선을 한꺼번에 켜거나 끈다(매집수량 전체 체크)
    for (const n of names) { if (off) LEG_OFF.add(n); else LEG_OFF.delete(n); }
    saveLeg();
    for (const k of Object.keys(CHARTS)) applyLegendState(CHARTS[k]);
  }
  function toggleLeg(name) {                          // 한 선을 켜고 끈다 — 모든 차트(일·주·월·분봉)에 적용하고 저장한다
    if (LEG_OFF.has(name)) LEG_OFF.delete(name); else LEG_OFF.add(name);
    saveLeg();
    for (const k of Object.keys(CHARTS)) applyLegendState(CHARTS[k]);
    const mn = document.getElementById('mn-chart');
    if (mn && mn.__fcm) applyMinuteLegend(mn.__fcm);
  }
  function applyMinuteLegend(h) {                     // 분봉: MA20·MA60 + 보조지표 4종을 범례 이름으로 켜고 끈다
    h.mas.forEach((s, i) => s.applyOptions({ visible: !LEG_OFF.has('MA' + (h.masN || [20, 60])[i]) }));
    for (const [k, s] of Object.entries(h.inds)) s.applyOptions({ visible: !LEG_OFF.has(IND_NAMES[k]) });
    document.querySelectorAll('#lg-mn button').forEach(b => {
      const off = LEG_OFF.has(b.dataset.n); b.classList.toggle('off', off); b.setAttribute('aria-pressed', String(!off));
    });
  }
  function legendMinute(h, D) {
    const el = document.getElementById('lg-mn'); if (!el) return;
    const IND_COL = { bb220: '#c026ff', kijun: '#39ff14', env: '#ff2d2d', vwap: '#1e90ff' };
    const items = (D.mas || []).map((n, i) => ['MA' + n, ['#ff7f0e', '#8c564b'][i]]).concat(Object.entries(IND_NAMES).map(([k, n]) => [n, IND_COL[k]]));
    el.innerHTML = '<span class="lg-cap" title="누르면 그 선을 숨기거나 다시 표시합니다. 선택은 저장되고 일봉 차트와 같이 갑니다.">보조지표</span>' +
      items.map(([n, c]) => `<button type="button" data-n="${n}" aria-pressed="${!LEG_OFF.has(n)}" class="${LEG_OFF.has(n) ? 'off' : ''}"><span class="sw" style="border-top-color:${c}"></span>${n}</button>`).join('');
    el.onclick = ev => { const b = ev.target.closest('button'); if (b) toggleLeg(b.dataset.n); };
    applyMinuteLegend(h);
  }
  const LEG_KEY = 'flowapp.legoff';
  const LEG_OFF = new Set((() => { try { return JSON.parse(localStorage.getItem(LEG_KEY) || '[]'); } catch (e) { return []; } })());   // 꺼 둔 선 이름 — 일·주·월·분봉이 같이 쓰고 저장된다
  const saveLeg = () => { try { localStorage.setItem(LEG_KEY, JSON.stringify([...LEG_OFF])); } catch (e) { /* 비공개 창 등 */ } };
  const IND_NAMES = { bb220: '볼린저상한', kijun: '일목기준선', env: '엔벨로프상단', vwap: 'VWAP' };   // 분봉 키 → 범례 이름(일봉과 같은 이름으로 같이 켜지고 꺼진다)
  function applyLegendState(h) {
    if (h.bandFill) h.bandFill.hidden = LEG_OFF.has('개인분산') && LEG_OFF.has('세력분산');   // 아래 applyOptions 가 다시 그리게 한다
    for (const [n, s] of Object.entries(h.S)) if (/^(MA\d+|상한선|볼린저상한|일목기준선|엔벨로프상단|VWAP|개인분산|세력분산|개인|세력합|외국인|연기금|기관)$/.test(n))
      s.applyOptions({ visible: !LEG_OFF.has(n) });
    h.markers.setMarkers(LEG_OFF.has('sig') ? [] : h.sigMarks);
    document.querySelectorAll('#lg-' + h.key + ' button').forEach(b => {
      const off=LEG_OFF.has(b.dataset.k || b.dataset.n);b.classList.toggle('off',off);b.setAttribute('aria-pressed',String(!off));
    });
    h.titles.els.forEach(el => el.querySelectorAll('[data-actor]').forEach(item => {
      const off = LEG_OFF.has(item.dataset.actor); item.classList.toggle('off', off);
      const cb = item.querySelector('input'); if (cb) cb.checked = !off;
    }));
    h.titles.els.forEach(el => el.querySelectorAll('.fc-flow-all input').forEach(all => {
      const on = Object.keys(h.D.colors.flow).filter(n => !LEG_OFF.has(n)).length, n = Object.keys(h.D.colors.flow).length;
      all.checked = on > 0; all.indeterminate = on > 0 && on < n;
    }));
  }

  // ── 재무(DART) 분기 선: 연결 = 항목 색 실선, 별도 = 회색 점선. 숫자는 그 칸을 누를 때만 ──
  const QEND = { 1: '-03-31', 2: '-06-30', 3: '-09-30', 4: '-12-31' }, BASIS = { CFS: '연결', OFS: '별도' };
  let DART_REC = null, DART_ERR = null, DART_WAIT = [];
  function fetchDart(code) {
    if (DART_REC || DART_ERR) return Promise.resolve();
    if (DART_WAIT.length) return new Promise(r => DART_WAIT.push(r));
    return new Promise(r => {
      DART_WAIT.push(r);
      fetch('/api/dartlines/' + code).then(r => r.json()).then(d => {
        if (d.error || !d.records || !d.records.length) DART_ERR = '재무정보 없음 — ' + (d.error || '공시 재무제표가 없습니다');
        else DART_REC = d.records;
      }).catch(() => { DART_ERR = '재무정보를 불러오지 못했습니다'; })
        .finally(() => { const w = DART_WAIT; DART_WAIT = []; w.forEach(f => f()); });
    });
  }
  function snapTo(T, x) {                            // 분기말이 휴장일이면 그 전 마지막 봉으로
    let lo = 0, hi = T.length - 1, ans = null;
    while (lo <= hi) { const m = (lo + hi) >> 1; if (T[m] <= x) { ans = T[m]; lo = m + 1; } else hi = m - 1; }
    return ans;
  }
  function loadDart(h) {
    const { D, chart } = h;
    const set = msg => h.titles.els.filter(e => +e.dataset.pane >= h.dart0).forEach(e => { e.querySelector('.fc-v').textContent = msg ? ' · ' + msg : ''; });
    if (typeof location === 'undefined' || location.protocol === 'file:') { set('재무는 앱에서만'); return; }
    set('불러오는 중…');
    fetchDart(D.code).then(() => {
      if (CHARTS[h.key] !== h) return;
      if (DART_ERR) { set(DART_ERR); return; }
      set('');
      h.dart = {};
      D.dart.forEach((p, i) => {
        const pane = h.dart0 + i, mx = Math.max(0, ...DART_REC.map(r => Math.abs((r[p.key] || 0) / 1e8))), div = mx >= 1e4 ? 1e4 : 1;
        const unit = div > 1 ? '조' : '억', miss = [];
        h.dart[pane] = { p, div, pts: {} };
        for (const b of ['CFS', 'OFS']) {
          const recs = DART_REC.filter(r => r.fs_div === b).sort((a, c) => a.year - c.year || a.quarter - c.quarter);
          const pts = [], seen = new Set();
          for (const r of recs) {
            const t = snapTo(D.t, r.year + QEND[r.quarter]);
            if (!t || seen.has(t) || r[p.key] == null) continue;
            seen.add(t);
            pts.push({ time: t, value: r[p.key] / 1e8 / div, label: `${r.year} ${r.quarter}Q` });
          }
          h.dart[pane].pts[b] = pts;
          if (!pts.length) { miss.push(BASIS[b]); continue; }
          const col = b === 'CFS' ? p.col : '#8a94a3';
          const s = chart.addSeries(LW.LineSeries, { color: col, lineWidth: b === 'CFS' ? 2 : 1.6,
            lineStyle: b === 'CFS' ? LW.LineStyle.Solid : LW.LineStyle.Dotted, pointMarkersVisible: true, pointMarkersRadius: 2,
            priceLineVisible: false, lastValueVisible: false, title: BASIS[b],
            priceFormat: { type: 'custom', formatter: v => (div > 1 ? v.toFixed(1) : NF.format(Math.round(v))) + unit } }, pane);
          s.setData(pts.map(q => ({ time: q.time, value: q.value })));
        }
        const e = h.titles.els.find(e => +e.dataset.pane === pane);
        if (e) { const u = e.querySelector('.fc-u') || e.insertBefore(document.createElement('span'), e.querySelector('.fc-v'));
                 u.className = 'fc-u'; u.textContent = ` (${unit}원)`; }       // 축 숫자에는 단위가 안 붙는다 — 제목에 적는다
        if (e && miss.length) e.querySelector('.fc-v').textContent = ' · ' + miss.join('·') + ' 없음';
      });
      h.titles.place();
    });
    chart.subscribeClick(param => {
      if (!h.dart || param.paneIndex == null || !h.dart[param.paneIndex] || !param.time) return;
      const P = h.dart[param.paneIndex], key = tsKey(param.time);
      let best = null;                               // 두 기준을 통틀어 누른 날짜에 가장 가까운 분기
      for (const b of ['CFS', 'OFS']) for (const q of P.pts[b]) {
        const d = Math.abs(new Date(q.time) - new Date(key));
        if (!best || d < best.d) best = { d, time: q.time, label: q.label };
      }
      if (!best) return;
      const parts = ['CFS', 'OFS'].map(b => { const q = P.pts[b].find(q => q.time === best.time);
        return q ? `${BASIS[b]} <b>${fmtEok(q.value * P.div)}</b>` : null; }).filter(Boolean);
      const e = h.titles.els.find(e => +e.dataset.pane === param.paneIndex);
      if (e) e.querySelector('.fc-v').innerHTML = ` · ${best.label} ${parts.join(' · ')}`;
    });
  }

  // ── 실시간 '오늘 봉' (realtime.py 가 체결을 넘긴다) ──
  function periodLabel(today, key) {                 // 주봉 = 그 주 금요일(W-FRI), 월봉 = 말일(ME)
    const [y, m, d] = today.split('-').map(Number), t = new Date(Date.UTC(y, m - 1, d));
    const f = x => x.toISOString().slice(0, 10);
    if (key === 'w') { t.setUTCDate(t.getUTCDate() + ((5 - t.getUTCDay() + 7) % 7)); return f(t); }
    if (key === 'm') return f(new Date(Date.UTC(y, m, 0)));
    return today;
  }
  function applyLive(h, t) {
    const { D, main } = h;
    const sig = `${t.today}|${t.price}|${t.high}|${t.low}|${t.open}`;
    if (h.liveSig === sig) return;                   // 값이 그대로면 다시 그리지 않는다(예전: 초당 2회 전체 다시 그리기 → CPU 56%)
    h.liveSig = sig;
    if (!D.o || !D.t.length) return;
    const L = periodLabel(t.today, h.key), last = D.t[D.t.length - 1];
    if (last > L) return;                            // 이미 이후 자료가 있다
    if (h.key === 'd' && last === L) return;         // 오늘 확정 봉이 이미 있다
    const n = D.t.length - 1;
    let bar;
    if (last === L) bar = { time: L, open: D.o[n], high: Math.max(D.h[n], t.high), low: Math.min(D.l[n], t.low), close: t.price };
    else bar = { time: L, open: t.open, high: t.high, low: t.low, close: t.price };
    if (h.key === 'd') Object.assign(bar, { color: 'rgba(214,69,69,.45)', borderColor: UP, wickColor: UP });   // 장중·미확정은 옅게
    if (h.key === 'd' && t.price < t.open) Object.assign(bar, { color: 'rgba(47,111,208,.45)', borderColor: DN, wickColor: DN });
    main.update(bar);
    const closes = D.c.slice(0, last === L ? n : n + 1).concat([t.price]);
    for (const [k, s] of Object.entries(h.S)) {
      const m = /^MA(\d+)$/.exec(k); if (!m) continue;
      const w = +m[1]; if (closes.length < w) continue;
      let sum = 0; for (let i = closes.length - w; i < closes.length; i++) sum += closes[i];
      s.update({ time: L, value: sum / w });
    }
    if (h.S['상한선'] && D.h && D.l) {                 // 오늘 봉까지 넣은 40봉 대표가 평균 + 2σ(모표준편차) — core.bb40_upper 와 같은 식
      const m = last === L ? n : n + 1, tp = [];
      for (let i = 0; i < m; i++) tp.push((D.h[i] + D.l[i] + D.c[i]) / 3);
      tp.push((bar.high + bar.low + bar.close) / 3);
      if (tp.length >= 40) {
        const w = tp.slice(-40), mu = w.reduce((a, b) => a + b, 0) / 40;
        const sd = Math.sqrt(w.reduce((a, b) => a + (b - mu) * (b - mu), 0) / 40);
        h.S['상한선'].update({ time: L, value: mu + 2 * sd });
      }
    }
    // 보조지표 4종 — 오늘 봉까지 넣은 마지막 점(VWAP 은 오늘 거래량이 없어 그대로 둔다)
    const m2 = last === L ? n : n + 1;
    const cl = D.c.slice(0, m2).concat([bar.close]);
    if (h.S['볼린저상한'] && cl.length >= 220) {
      const w = cl.slice(-220), mu = w.reduce((a, b) => a + b, 0) / 220;
      h.S['볼린저상한'].update({ time: L, value: mu + Math.sqrt(w.reduce((a, b) => a + (b - mu) * (b - mu), 0) / 220) });
    }
    if (h.S['엔벨로프상단'] && cl.length >= 60) h.S['엔벨로프상단'].update({ time: L, value: cl.slice(-60).reduce((a, b) => a + b, 0) / 60 * 1.05 });
    if (h.S['일목기준선'] && D.h && D.l) {
      const hs = D.h.slice(0, m2).concat([bar.high]), ls = D.l.slice(0, m2).concat([bar.low]);
      if (hs.length >= 26) h.S['일목기준선'].update({ time: L, value: (Math.max(...hs.slice(-26)) + Math.min(...ls.slice(-26))) / 2 });
    }
  }

  // ── 분봉 ── 차트는 한 번만 만들고 주기를 바꾸거나 실시간 봉이 붙으면 자료만 바꾼다
  //   (chart.remove() 뒤 다시 만들면 lightweight-charts 가 'Object is disposed' 를 던졌다 — 2026-10-02 실측)
  function buildMinute(el, D, keep) {
    const toT = s => Date.UTC(+s.slice(0, 4), +s.slice(4, 6) - 1, +s.slice(6, 8), +s.slice(8, 10), +s.slice(10, 12)) / 1000;
    let h = el.__fcm;
    if (!h) {
      const chart = makeChart(el, 1);
      const c = chart.addSeries(LW.CandlestickSeries, { upColor: UP, downColor: DN, borderUpColor: UP, borderDownColor: DN,
        wickUpColor: UP, wickDownColor: DN, priceLineVisible: false,
        priceFormat:{type:'custom',formatter:fmtInt,minMove:1} }, 0);
      const mas = ['#ff7f0e', '#8c564b'].map(col => chart.addSeries(LW.LineSeries, { color: col, lineWidth: 1.3,
        priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false }, 0));
      const IND_COL = { bb220: '#c026ff', kijun: '#39ff14', env: '#ff2d2d', vwap: '#1e90ff' };     // viewer.IND 와 같은 색(보조지표 4종)
      const inds = {}; for (const [k, col] of Object.entries(IND_COL)) inds[k] = chart.addSeries(LW.LineSeries, { color: col, lineWidth: 1.2,
        priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false }, 0);
      const v = chart.addSeries(LW.HistogramSeries, { priceLineVisible: false, lastValueVisible: false,
        priceFormat: { type: 'custom', formatter: fmtQty, minMove: 1 } }, 1);
      const sym = orig => { const r = orig(); if (!r) return r; const m = Math.max(Math.abs(r.priceRange.minValue), Math.abs(r.priceRange.maxValue)) * 1.1 || 1;
        return { priceRange: { minValue: -m, maxValue: m } }; };
      const o = chart.addSeries(LW.HistogramSeries, { priceLineVisible: false, lastValueVisible: false, autoscaleInfoProvider: sym,
        priceFormat: { type: 'custom', formatter: x => x.toFixed(1) } }, 2);
      chart.panes()[0].setStretchFactor(60); chart.panes()[1].setStretchFactor(14); chart.panes()[2].setStretchFactor(26);
      chart.panes().forEach((p, i) => chart.priceScale('right', i).applyOptions({ scaleMargins: { top: 0.08, bottom: i === 1 ? 0 : 0.05 } }));
      paneTitles(el, chart, [[1, '<b>거래량</b>'], [2, '<b>스토 osc</b>']]);
      const tag = overlay(el);
      chart.subscribeCrosshairMove(param => {
        if (!param.time || !param.point || param.point.x < 0) { tag.hidden = true; return; }
        tag.hidden = false; tag.textContent = dayLabel(param.time, 1);
        const w = tag.offsetWidth; tag.style.left = Math.max(0, Math.min(param.point.x - w / 2, el.clientWidth - w - 74)) + 'px';
      });
      h = el.__fcm = { chart, c, mas, v, o, inds };
      el.ondblclick = () => h.reset();
    }
    const b = D.bars, T = b.map(r => toT(r.t));
    h.c.setData(b.map((r, i) => ({ time: T[i], open: r.o, high: r.h, low: r.l, close: r.c })));
    h.mas.forEach((s, i) => { const n = D.mas[i];
      s.setData(n == null ? [] : b.map((r, j) => r['ma' + n] == null ? { time: T[j] } : { time: T[j], value: r['ma' + n] })); });
    for (const [k, s] of Object.entries(h.inds)) s.setData(b.map((r, j) => r[k] == null ? { time: T[j] } : { time: T[j], value: r[k] }));
    h.v.setData(b.map((r, i) => ({ time: T[i], value: r.v, color: r.c >= r.o ? 'rgba(214,69,69,.6)' : 'rgba(47,111,208,.6)' })));
    h.o.setData(b.map((r, i) => r.osc == null ? { time: T[i] } : { time: T[i], value: r.osc, color: r.osc >= 0 ? 'rgba(214,69,69,.6)' : 'rgba(47,111,208,.6)' }));
    // 실시간: 마지막 두 봉만 고친다(update). 보이는 범위는 건드리지 않는다 — 예전엔 다시 그릴 때마다 범위를 다시 넣다가
    // 한 봉씩 넓어져 가만히 있어도 차트가 저절로 축소됐다(2026-10-02 실측 20초에 165→191봉).
    h.tick = (D) => {
      const b = D.bars, k = b.length;
      for (let i = Math.max(0, k - 2); i < k; i++) {
        const r = b[i], t = toT(r.t);
        if (h.lastT != null && t < h.lastT) continue;
        h.c.update({ time: t, open: r.o, high: r.h, low: r.l, close: r.c });
        h.mas.forEach((s, j) => { const m = D.mas[j], v = m == null ? null : r['ma' + m]; if (v != null) s.update({ time: t, value: v }); });
        for (const [k, s] of Object.entries(h.inds)) if (r[k] != null) s.update({ time: t, value: r[k] });
        h.v.update({ time: t, value: r.v, color: r.c >= r.o ? 'rgba(214,69,69,.6)' : 'rgba(47,111,208,.6)' });
        if (r.osc != null) h.o.update({ time: t, value: r.osc, color: r.osc >= 0 ? 'rgba(214,69,69,.6)' : 'rgba(47,111,208,.6)' });
        h.lastT = t;
      }
    };
    h.lastT = T.length ? T[T.length - 1] : null;
    h.masN = D.mas; legendMinute(h, D);
    const n = T.length, span = Math.min(n, el.clientWidth < 600 ? Math.round(D.show / 2) : D.show);
    h.reset = () => h.chart.timeScale().setVisibleLogicalRange({ from: n - span, to: n + 2 });
    h.range = () => h.chart.timeScale().getVisibleLogicalRange();
    if (keep) h.chart.timeScale().setVisibleLogicalRange(keep); else h.reset();
    return h;
  }

  const FC = window.FC = {
    charts: CHARTS, live: null,
    fold: () => { try { return localStorage.getItem('flowapp.fold') === '1'; } catch (e) { return false; } },
    finance: () => { try { return localStorage.getItem('flowapp.finance') === '1'; } catch (e) { return false; } },
    show(key) {                                      // 탭을 처음 열 때 만든다(안 보는 차트는 만들지 않는다)
      if (!CHARTS[key]) { buildDaily(key, { fold: FC.fold(), finance: FC.finance() }); legend(key); }
      else if (FC.live) applyLive(CHARTS[key], FC.live);       // 숨어 있던 동안의 오늘 봉
      document.querySelectorAll('.chart-range button').forEach(b => {b.disabled=false;b.setAttribute('aria-pressed',String(+b.dataset.months===CHARTS[key].months));});
    },
    rebuild() {
      const on = document.querySelector('.chartbox.on'), key = on && on.id.slice(4);
      for (const k of Object.keys(CHARTS)) {
        if (k === key) { buildDaily(k, {fold:FC.fold(),finance:FC.finance()});legend(k); }
        else { const h=CHARTS[k];SAVED_RANGES[k]=h.chart.timeScale().getVisibleLogicalRange();SAVED_MONTHS[k]=h.months;dispose(h);delete CHARTS[k];document.getElementById('plot-'+k).innerHTML=''; }
      }
    },
    range(key, months) {
      const h=CHARTS[key];if(!h)return;
      h.months=months;
      h.chart.disposeInteractions(); h.chart.disposeInteractions = smoothWheel(h.chart,document.getElementById('plot-'+key));
      if (!months) h.chart.timeScale().fitContent();
      else { const T=h.D.t, from=lowerBound(T,monthsAgo(T[T.length-1],months));h.chart.timeScale().setVisibleLogicalRange({from:Math.max(0,from-1),to:T.length+3}); }
    },
    reset(key) { FC.range(key,0);document.querySelectorAll('.chart-range button').forEach(b=>b.setAttribute('aria-pressed',String(+b.dataset.months===0))); },
    // 실시간 오늘 봉. 보고 있는 차트에만, 휠·드래그 중이 아닐 때만 그린다 — 못 그렸으면 false(호출 쪽이 다음에 다시 부른다)
    applyLive(t) {
      FC.live = t;
      if (performance.now() < FC_BUSY.until || document.hidden) return false;
      const on = document.querySelector('.chartbox.on'), key = on && on.id.slice(4), h = CHARTS[key];
      const vr = h && h.chart.timeScale().getVisibleLogicalRange();
      if (h && vr && vr.to < h.D.t.length - 1.5) return true;   // 지난 구간을 보는 중 — 오늘 봉이 화면 밖이라 다시 그릴 일이 없다(돌아오면 FC.live 로 그린다)
      if (h) { try { applyLive(h, t); } catch (e) { console.warn('오늘 봉', e); } }
      return true;
    },
    busy: () => performance.now() < FC_BUSY.until,
    locked: paneLocked,
    // 「크기 고정」: 보고 있는 차트의 지금 칸 높이를 기본값으로 저장하고 경계 끌기·전체 손잡이를 잠근다. 다시 누르면 잠금만 푼다(크기는 유지)
    toggleLock() {
      if (paneLocked()) { try { localStorage.removeItem('flowapp.paneLock'); } catch (e) {} FC.rebuild(); return; }
      const on = document.querySelector('.chartbox.on'), h = on && CHARTS[on.id.slice(4)];
      if (h) {
        // 실제 칸 높이는 경계선·시간축 몫만큼 설정값보다 조금 작다 — 비율로 되돌려 저장해야 고정·해제를 반복해도 줄어들지 않는다
        const raw = h.chart.panes().map(p => p.getHeight()), tot = raw.reduce((a, b) => a + b, 0), wt = h.want.reduce((a, b) => a + b, 0);
        const ps = raw.map(x => Math.round(x / tot * wt)), saved = paneSizes(), hasBand = h.D.bt.length > 1;
        const keys = h.fold ? ['price', 'val'] : ['price', 'val', 'stoch', 'flow'].concat(hasBand ? ['band'] : []);
        keys.forEach((k, i) => {
          if (ps[i] == null || h.pf[k]) return;                       // 접힌 칸은 접기 전 크기를 그대로 둔다
          if (k === 'price' && h.fold && hasBand) {                    // 접기 모드에선 가격 칸 안에 분산 띠가 있다
            const b = saved.band || HEIGHTS.band; saved.price = Math.max(80, ps[i] - b);
          } else saved[k] = Math.max(PANE_FOLD_H + 16, ps[i]);
        });
        try { localStorage.setItem('flowapp.paneH', JSON.stringify(saved)); localStorage.removeItem('flowapp.chartScale2'); } catch (e) {}
      }
      try { localStorage.setItem('flowapp.paneLock', '1'); } catch (e) {}
      FC.rebuild();
    },
    minute: buildMinute,
  };
})();
