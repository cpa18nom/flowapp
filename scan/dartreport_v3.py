"""Live OpenDART section: browser fetches from the local server after stock selection."""
from html import escape
import re

import dart_collector


def _money(value):
    return '미제공' if value is None else f'{value / 100_000_000:,.1f}'


def _link(receipt, title):
    if not re.fullmatch(r'\d{14}', str(receipt)):
        return escape(str(title))
    return (f'<a href="https://dart.fss.or.kr/dsaf001/main.do?rcpNo={receipt}" '
            f'target="_blank" rel="noopener noreferrer">{escape(str(title))}</a>')


def render_payload(data: dict) -> dict:
    records = data['records']
    summary = ''
    chart = None
    if records:
        latest = records[-1]
        basis = '연결' if latest['fs_div'] == 'CFS' else '별도'
        summary = f'''<h3>{escape(data['name'])} · {latest['period']}까지 · {basis}재무제표</h3>
<p class="note">단위 억원 · 사업연도 기준 분기 · 손익은 <b>해당 분기 3개월 발생액</b>, 현금은 <b>분기말 잔액</b>입니다.
4분기 손익은 연간 누적에서 같은 기준의 3분기 누적을 차감하며, 값이 없는 분기는 막대를 비웁니다.
 · {_link(latest['rcept_no'], '최신 재무제표 공시 원문')}</p>'''
        chart_rows = []
        lookup = {(r['year'], r['quarter']): r for r in records}
        for year in range(records[0]['year'], latest['year'] + 1):
            for quarter in range(1, 5):
                if (year, quarter) > (latest['year'], latest['quarter']):
                    break
                chart_rows.append(lookup.get((year, quarter), {'period': f'{year}Q{quarter}'}))
        # 막대 하나가 분기의 한 항목. 같은 항목의 막대 꼭대기를 선으로 잇는다.
        # 연결·별도를 한 선으로 이으면 안 되므로(기준이 다른 값이다) 기준별로 계열을 나눈다.
        palette = [('revenue', '매출액', '#487cd0'), ('op_profit', '영업이익', '#c0392b'),
                   ('net_income', '순이익', '#2b8b68'), ('cash', '기말 현금', '#c58b22')]
        # 값이 하나도 없는 항목은 계열을 아예 만들지 않는다 — 빈 범례가 뜨면
        # "그려졌는데 안 보이는" 것처럼 읽힌다(실측: 금융지주는 매출액 계정이 없다).
        series = [(key, label, color, basis_code, basis_label)
                  for key, label, color in palette
                  for basis_code, basis_label in [('CFS', '연결'), ('OFS', '별도')]
                  if any(r.get('fs_div') == basis_code and r.get(key) is not None for r in chart_rows)]
        missing = [label for key, label, _ in palette
                   if not any(r.get(key) is not None for r in chart_rows)]
        spots = list(range(len(chart_rows)))
        periods = [r['period'] for r in chart_rows]

        def _typical(key, basis_code):
            """그 계열이 보통 얼마나 큰가 — 폭·그리는 순서를 정하는 기준(중앙값)."""
            vals = sorted(abs(r[key]) for r in chart_rows
                          if r.get('fs_div') == basis_code and r.get(key) is not None)
            return vals[len(vals) // 2] if vals else 0

        # 한 분기에 막대 자리는 하나. 같은 x 에 포개되 **폭을 달리해** 뒤엣것이 가려지지 않게 한다.
        # 쌓지 않는 이유: 매출 ⊃ 영업이익 ⊃ 순이익은 포함관계고 현금은 잔액이라, 쌓으면 합계가
        # 원문에 없는 수치가 된다.
        # 큰 값일수록 넓게, 먼저 그린다(뒤로 깔린다). 어느 항목이 큰지는 회사마다 다르므로
        # (금융지주는 현금 35조 > 영업이익 2.7조) 고정 순서가 아니라 실제 크기로 정한다.
        # ★ 현금은 막대로 그리지 않는다. 손익 셋은 매출 ⊃ 영업이익 ⊃ 순이익 의 포함관계라
        # 포개면 큰 것 안에 작은 것이 들어가 항상 다 보이지만, 현금은 잔액이라 크기가 제멋대로다
        # (실측: 삼성 현금 53조 vs 매출 76조 → 주황이 파랑을 거의 덮었다). 성격도 유량 vs 잔액이라
        # 다르므로 선으로만 얹는다.
        bar_idx = [i for i in range(len(series)) if series[i][0] != 'cash']
        order = sorted(bar_idx, key=lambda i: -_typical(series[i][0], series[i][3]))
        span = 0.54 / max(len(order) - 1, 1)
        widths = {i: 0.78 - rank * span for rank, i in enumerate(order)}
        traces = []
        for i in order:                                    # 막대: 넓은 것부터 = 뒤에서부터
            key, label, color, basis_code, basis_label = series[i]
            ys = [r[key] / 100_000_000 if r.get('fs_div') == basis_code and r.get(key) is not None else None
                  for r in chart_rows]
            name = f'{label} ({basis_label})'
            traces.append({'x': spots, 'y': ys, 'name': name, 'type': 'bar', 'width': widths[i],
                           'marker': {'color': color, 'opacity': 0.75 if basis_code == 'CFS' else 0.45,
                                      'line': {'color': color, 'width': 1}},
                           'legendgroup': name, 'legendrank': 1000 + i,
                           'hovertemplate': f'{name}  %{{y:,.1f}}억원<extra></extra>'})
        # 선: 막대를 다 그린 뒤 그 위에 얹는다. 현금은 막대가 없으므로 선이 본체다.
        for i in order + [i for i in range(len(series)) if series[i][0] == 'cash']:
            key, label, color, basis_code, basis_label = series[i]
            solo = key == 'cash'   # 막대 없이 선만 그리는 항목(잔액)
            ys = [r[key] / 100_000_000 if r.get('fs_div') == basis_code and r.get(key) is not None else None
                  for r in chart_rows]
            name = f'{label} ({basis_label}, 우측축)' if solo else f'{label} ({basis_label})'
            trace = {'x': spots, 'y': ys, 'type': 'scatter', 'mode': 'lines+markers',
                     'name': name,
                     'line': {'color': color, 'width': 3 if solo else 2,
                              'dash': 'solid' if basis_code == 'CFS' else 'dot'},
                     'marker': {'color': color, 'size': 7 if solo else 5,
                                'symbol': 'diamond' if solo else 'circle'},
                     'connectgaps': False, 'showlegend': solo, 'legendrank': 1000 + i,
                     'legendgroup': name}
            if solo:
                # 현금은 **잔액**이라 분기 손익과 축을 공유할 이유가 없다. 같이 두면 스케일이 큰 쪽이
                # 작은 쪽을 눌러버린다(실측 KB금융: 현금 26~35조 vs 분기이익 2~3조 → 막대가 바닥에 깔림).
                trace['yaxis'] = 'y2'
                trace['hovertemplate'] = f'{name}  %{{y:,.1f}}억원<extra></extra>'
            else:
                # 막대 쪽에서 이미 값을 띄운다 — 선까지 띄우면 툴팁이 항목마다 두 줄이 된다.
                trace['hoverinfo'] = 'skip'
            traces.append(trace)
        chart = {'data': traces,
                 # 막대가 한 자리에 모이니 분기 하나를 통째로 보여주는 통합 툴팁이 쓸 수 있다.
                 'layout': {'height': 460, 'barmode': 'overlay', 'bargap': 0.15,
                            'hovermode': 'x unified',
                            'margin': {'t': 30, 'b': 80, 'l': 72, 'r': 78},
                            'xaxis': {'tickmode': 'array', 'tickvals': spots, 'ticktext': periods,
                                      'tickangle': -45, 'range': [-0.7, len(spots) - 0.3]},
                            'yaxis': {'title': {'text': ''}, 'tickformat': ',.0f',
                                      'zeroline': True, 'zerolinecolor': '#9aa0a6'},
                            'yaxis2': {'title': {'text': ''},
                                       'tickformat': ',.0f', 'tickfont': {'color': '#c58b22'},
                                       'overlaying': 'y', 'side': 'right', 'showgrid': False,
                                       'rangemode': 'tozero'},
                            'legend': {'orientation': 'h', 'y': 1.12},
                            'plot_bgcolor': 'rgba(0,0,0,0)', 'paper_bgcolor': 'rgba(0,0,0,0)'}}
        if missing:
            summary += (f'<p class="note">이 회사 공시에서 찾지 못해 <b>표시하지 않은 항목: '
                        f'{escape(", ".join(missing))}</b>. 금융지주·보험사처럼 단일 매출 계정을 '
                        f'공시하지 않는 회사가 있습니다(이자수익·수수료수익 등으로 나뉩니다). '
                        f'없는 값을 합쳐서 만들지 않습니다 — 공시 원문으로 확인하십시오.</p>')
        history = ''.join('<tr>' + ''.join(f'<td>{cell}</td>' for cell in [
            _link(r['rcept_no'], r['period']), '연결' if r['fs_div'] == 'CFS' else '별도',
            *[_money(r[k]) for k in ('revenue', 'op_profit', 'net_income', 'cash')]]) + '</tr>' for r in reversed(records))
        summary += f'''<details><summary>분기별 수치와 공시 원문 ({len(records)}개 보고기간)</summary>
<div class="wrap"><table><thead><tr><th>사업연도·분기</th><th>기준</th><th>매출액</th><th>영업이익</th><th>순이익</th><th>기말 현금</th></tr></thead><tbody>{history}</tbody></table></div></details>'''
    else:
        summary = ('<p>조회 기간에 API로 제공되는 정기보고서 재무제표가 없습니다. '
                   'DART 원문에서 직접 확인해주세요.</p>')
    markup = (f'<p class="note">OpenDART 조회 완료: {escape(data["collected_at"])} · '
              f'종목 {escape(data["code"])} · {escape(data["name"])}</p>' + summary)
    return {'html': markup, 'chart': chart, 'data': data}


def fetch_payload(code: str) -> dict:
    return render_payload(dart_collector.collect_dart_financials(code))


def render_dart_section_v3(code: str, name: str) -> str:
    if not re.fullmatch(r'[0-9A-Z]{6}', code):
        raise ValueError('Invalid stock code')
    return f'''<section id="dart-section" style="margin-top:28px;padding:18px;background:var(--band);border-radius:8px;">
<h2>{escape(name)} DART 재무정보</h2>
<button type="button" id="dart-refresh">최신 정보 다시 조회</button>
<p id="dart-status" role="status" aria-live="polite">DART에서 재무정보를 가져오는 중입니다…</p>
<div id="dart-content"></div><div id="dart-chart"></div>
</section>
<script>
(function() {{
 const button = document.getElementById('dart-refresh');
 const status = document.getElementById('dart-status');
 const content = document.getElementById('dart-content');
 const chart = document.getElementById('dart-chart');
 // 분기 값을 분기말 날짜에 찍는다 — 수급 차트의 x 는 날짜축이다.
 const QEND = {{1: '-03-31', 2: '-06-30', 3: '-09-30', 4: '-12-31'}};
 const DART_SERIES = [['revenue', '매출액', '#487cd0'], ['op_profit', '영업이익', '#c0392b'],
                      ['net_income', '순이익', '#2b8b68'], ['cash', '기말 현금', '#c58b22']];
 async function load() {{
   button.disabled = true;
   status.textContent = 'DART에서 재무정보를 가져오는 중입니다…';
   content.innerHTML = ''; chart.replaceChildren();
   try {{
     const response = await fetch('/api/dart/{code}', {{cache: 'no-store'}});
     const result = await response.json();
     if (!response.ok) throw new Error(result.error || 'DART 조회에 실패했습니다.');
     content.innerHTML = result.html;
     if (result.chart) {{
       if (!window.Plotly) {{
         await new Promise((resolve, reject) => {{
           const script = document.createElement('script'); script.src = '/plotly.js';
           script.onload = resolve; script.onerror = () => reject(new Error('차트 라이브러리를 불러오지 못했습니다.'));
           document.head.appendChild(script);
         }});
       }}
       await Plotly.newPlot(chart, result.chart.data, result.chart.layout, {{responsive: true, displayModeBar: false}});
     }}
     status.textContent = '조회 완료';
   }} catch (error) {{
     status.textContent = '조회 실패: ' + error.message + ' 다시 조회 버튼으로 재시도할 수 있습니다.';
   }} finally {{ button.disabled = false; }}
 }}
 button.addEventListener('click', load); load();
}})();
</script>'''
