"""종목 분석 — 기술적·정량적·정성적 근거를 한 곳에 모은다.

**앱은 근거를 세워 놓는 데까지 한다. 결론은 사람이 낸다.**
가장 좋은 검색식도 건별로는 절반쯤 진다(승률 50% 안팎, 평균은 소수 대박이 만든다). 화면이 "사라/팔라"를 대신
말하면 그 불확실성이 지워진다. 그래서 여기서는 판정 대신 **관측값과 그 출처**를 낸다.

출처
  기술적  ~/market/raw/price · raw/flow  (core.stock_snapshot / flow_snapshot)
  정량적  OpenDART 분기 재무(dart_collector) · 네이버 투자지표(PER·PBR·컨센서스)
  정성적  로컬 증권리서치 아카이브 색인 · 네이버 종목뉴스

네트워크·로컬 아카이브는 **언제든 없을 수 있다**. 한 축이 비어도 나머지는 그대로
보여주고, 무엇이 왜 비었는지 화면에 적는다(조용히 0 으로 채우지 않는다).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import core

NAVER = "https://m.stock.naver.com/api"
UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://m.stock.naver.com/"}
TIMEOUT = 8


# ──────────────────────────────────────────────────────────────── 공통

def _get_json(url: str):
    """실패하면 None. 분석 화면 한 칸이 비는 것이지 페이지가 죽을 일은 아니다."""
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def _num(text) -> float | None:
    """'11.66배' · '1,668원' · '0.64%' → 숫자. 못 읽으면 None(0 으로 채우지 않는다)."""
    if text is None:
        return None
    m = re.search(r"-?[\d,]+\.?\d*", str(text))
    if not m:
        return None
    try:
        return float(m.group().replace(",", ""))
    except ValueError:
        return None


def report_index() -> Path | None:
    """증권리서치 색인. PC 마다 OneDrive 경로가 달라 **해석기로만** 찾는다(BRAIN 1항)."""
    try:
        sys.path.insert(0, os.path.expanduser("~/.claude/brain/bin"))
        from brainpaths import RAW                     # noqa: PLC0415
        path = Path(RAW) / "03. 투자·시장" / "증권리서치" / "_목록.md"
        return path if path.exists() else None
    except Exception:
        return None


# ──────────────────────────────────────────────────────────── 기술적

def technical(code: str) -> dict:
    """가격·수급 raw 에서 나오는 상태. 계산은 core 가 한다(화면마다 다시 짜지 않는다)."""
    out = {"가격": core.stock_snapshot(code), "수급": core.flow_snapshot(code)}
    snap = out["가격"]
    if snap:
        # 52주 위치·이격은 '지금 어디쯤인가'를 한 줄로 말해 준다. 해석은 화면에서 한다.
        out["요약"] = {
            "52주위치": snap["52주위치"], "고가대비": snap["고가대비"],
            "이평배열": snap["이평배열"], "이격20": snap["이격20"],
            "변동성60일": snap["변동성60일"],
        }
    return out


# ──────────────────────────────────────────────────────────── 정량적

def naver_indicators(code: str) -> dict:
    """네이버 투자지표 + 컨센서스. 재무제표 원본은 DART 를 쓰고 여기서는 **시장가 기준
    지표**(PER·PBR·배당·컨센서스 목표주가)만 가져온다 — 둘의 출처를 섞지 않는다."""
    raw = _get_json(f"{NAVER}/stock/{code}/integration")
    if not raw:
        return {}
    infos = {i.get("key"): i.get("value") for i in (raw.get("totalInfos") or [])}
    con = raw.get("consensusInfo") or {}
    return {
        "종목명": raw.get("stockName"),
        "지표": {k: infos.get(k) for k in
                ("PER", "추정PER", "PBR", "EPS", "BPS", "배당수익률", "시총", "외인소진율")
                if infos.get(k)},
        "컨센서스": {"평균의견": _num(con.get("recommMean")),
                  "목표주가": _num(con.get("priceTargetMean")),
                  "기준일": con.get("createDate")} if con else {},
    }


def quantitative(code: str, dart: dict | None = None) -> dict:
    """DART 분기 재무에서 성장률·마진을 뽑는다.

    ★ 전년 동기(YoY) 로 본다. 반도체·조선처럼 계절성이 큰 업종에서 전분기 대비(QoQ)만
    보면 계절 요인을 실적 변화로 읽는다. 비교 대상이 없으면 **None 으로 남긴다.**
    """
    out = {"지표": naver_indicators(code)}
    records = (dart or {}).get("records") or []
    if not records:
        return out
    rows = []
    by_period = {(r["year"], r["quarter"]): r for r in records}
    for r in records[-8:]:
        prev = by_period.get((r["year"] - 1, r["quarter"]))
        rev, op = r.get("revenue"), r.get("op_profit")
        rows.append({
            "분기": r["period"], "기준": "연결" if r["fs_div"] == "CFS" else "별도",
            "매출": rev, "영업이익": op, "순이익": r.get("net_income"), "현금": r.get("cash"),
            "영업이익률": (op / rev * 100) if rev and op is not None and rev != 0 else None,
            "매출YoY": (_growth(rev, prev.get("revenue")) if prev else None),
            "영업이익YoY": (_growth(op, prev.get("op_profit")) if prev else None),
        })
    out["분기"] = rows
    return out


def _growth(now, before) -> float | None:
    """전년 대비 증감률. 기저가 0 이거나 적자면 배율이 뜻을 잃으므로 내지 않는다."""
    if now is None or before is None or before <= 0:
        return None
    return (now - before) / before * 100


# ──────────────────────────────────────────────────────────── 정성적

REPORT_ROW = re.compile(r"^\|\s*(\d{4}-\d{2}-\d{2})\s*\|([^|]*)\|([^|]*)\|([^|]*)\|"
                        r"([^|]*)\|([^|]*)\|([^|]*)\|([^|]*)\|")


def local_reports(name: str, limit: int = 8) -> list[dict]:
    """로컬 증권리서치 아카이브 색인에서 이 종목 리포트를 최신순으로.

    색인에 **경로 열이 없다**(폴더를 재편해도 색인이 무효화되지 않도록 한 규약).
    그래서 여기서도 파일을 찾지 않고 날짜·증권사·의견·목표주가만 읽는다.
    """
    path = report_index()
    if not path or not name:
        return []
    want = name.replace(" ", "")
    rows = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                m = REPORT_ROW.match(line)
                if not m:
                    continue
                cells = [c.strip() for c in m.groups()]
                if cells[1].replace(" ", "") != want:
                    continue
                rows.append({"날짜": cells[0], "증권사": cells[2], "제목": cells[3],
                             "의견": cells[4] or None,
                             "목표주가": _num(cells[5]), "작성일종가": _num(cells[6])})
    except OSError:
        return []
    rows.sort(key=lambda r: r["날짜"], reverse=True)
    return rows[:limit]


def news(code: str, limit: int = 8) -> list[dict]:
    """네이버 종목뉴스 헤드라인. 본문은 가져오지 않는다 — 제목·언론사·시각만 근거로 쓴다."""
    raw = _get_json(f"{NAVER}/news/stock/{code}?pageSize={max(limit, 5)}&page=1")
    if not isinstance(raw, list):
        return []
    out = []
    for group in raw:
        for it in (group.get("items") or []):
            dt = str(it.get("datetime", ""))
            when = (f"{dt[:4]}-{dt[4:6]}-{dt[6:8]} {dt[8:10]}:{dt[10:12]}"
                    if len(dt) >= 12 else dt)
            office, article = it.get("officeId"), it.get("articleId")
            out.append({
                "일시": when, "언론사": it.get("officeName"),
                "제목": _unescape(str(it.get("title", ""))),
                "url": (f"https://n.news.naver.com/mnews/article/{office}/{article}"
                        if office and article else None),
            })
    out.sort(key=lambda r: r["일시"], reverse=True)
    return out[:limit]


def _unescape(text: str) -> str:
    """네이버 제목에는 &quot; 같은 엔티티가 섞여 온다."""
    import html as _html
    return _html.unescape(text)


def qualitative(code: str, name: str) -> dict:
    return {"리포트": local_reports(name), "뉴스": news(code),
            "리포트출처": str(report_index() or "아카이브 없음")}


# ──────────────────────────────────────────────────────────── 모으기

def gather(code: str, name: str, dart: dict | None = None) -> dict:
    """세 축을 한 번에. 한 축이 실패해도 나머지는 살린다."""
    out = {"code": code, "name": name,
           "collected_at": datetime.now().astimezone().isoformat(timespec="seconds"),
           "결손": []}
    for key, fn in (("기술적", lambda: technical(code)),
                    ("정량적", lambda: quantitative(code, dart)),
                    ("정성적", lambda: qualitative(code, name))):
        try:
            out[key] = fn()
        except Exception as exc:                      # noqa: BLE001
            out[key] = {}
            out["결손"].append(f"{key}: {type(exc).__name__}")
    if not (out.get("정성적") or {}).get("뉴스"):
        out["결손"].append("뉴스: 네이버 응답 없음")
    if not (out.get("정성적") or {}).get("리포트"):
        out["결손"].append("리포트: 이 종목 리포트가 아카이브에 없음")
    if not (out.get("정량적") or {}).get("분기"):
        out["결손"].append("정량: DART 분기 재무 없음")
    return out


# ──────────────────────────────────────────────────────────── 화면

def _pct(v, digits=1, sign=False):
    if v is None:
        return "—"
    return f"{v:+.{digits}f}%" if sign else f"{v:.{digits}f}%"


def _eok(v):
    """원 → 억원. None 은 '—' 로 남긴다(0 으로 채우지 않는다)."""
    return "—" if v is None else f"{v / 100_000_000:,.0f}"


def _tech_html(tech: dict) -> str:
    price, flow = tech.get("가격"), tech.get("수급")
    if not price:
        return "<p>가격 raw 가 없습니다.</p>"
    rows = [
        ("기준일", str(price["기준일"])[:10]),
        ("종가", f"{price['종가']:,.0f}원 ({price['전일대비']:+.2f}%)"),
        ("52주 위치", f"{price['52주위치']:.0f}% (저 {price['52주저가']:,.0f} · "
                   f"고 {price['52주고가']:,.0f}) · 고가대비 {_pct(price['고가대비'], 1, True)}"),
        ("이평 배열", f"{price['이평배열']} · 이격 20일 {_pct(price['이격20'], 1, True)} · "
                   f"60일 {_pct(price['이격60'], 1, True)} · 120일 {_pct(price['이격120'], 1, True)}"),
        ("변동성(60일)", _pct(price["변동성60일"])),
        ("거래대금(20일)", f"{price['거래대금20일'] / 100_000_000:,.0f}억"),
    ]
    if flow:
        rows += [
            ("수급 분산비율", f"개인 {flow['개인분산']:.2f} · 세력 {flow['세력분산']:.2f} · "
                         f"SIG {flow['SIG']:+.2f}"),
            ("자기이력 백분위", f"개인 {flow['개인분산_백분위']:.0f}% · "
                         f"세력 {flow['세력분산_백분위']:.0f}% · SIG {flow['SIG_백분위']:.0f}% "
                         f"({flow['관측일수']}일 중)"),
        ]
    body = "".join(f"<tr><th class='k'>{k}</th><td>{v}</td></tr>" for k, v in rows)
    return (f"<div class='wrap'><table><tbody>{body}</tbody></table></div>"
            "<p class='note'>분산비율의 절대값은 종목마다 기준이 달라 그대로 비교하면 안 됩니다 — "
            "같이 적은 <b>자기이력 백분위</b>로 읽으십시오.</p>")


def _quant_html(quant: dict, price_now: float | None) -> str:
    out = []
    ind = (quant.get("지표") or {}).get("지표") or {}
    con = (quant.get("지표") or {}).get("컨센서스") or {}
    if ind:
        cells = " · ".join(f"<b>{k}</b> {v}" for k, v in ind.items())
        out.append(f"<p>{cells}</p>")
    if con.get("목표주가"):
        gap = ((con["목표주가"] / price_now - 1) * 100) if price_now else None
        out.append(f"<p>증권사 컨센서스({con.get('기준일')}) — 목표주가 "
                   f"<b>{con['목표주가']:,.0f}원</b>"
                   + (f" · 현재가 대비 {_pct(gap, 1, True)}" if gap is not None else "")
                   + (f" · 평균의견 {con['평균의견']:.2f}" if con.get("평균의견") else "")
                   + "</p>")
    rows = quant.get("분기") or []
    if rows:
        head = ("<tr><th>분기</th><th>기준</th><th>매출(억)</th><th>영업이익(억)</th>"
                "<th>영업이익률</th><th>매출 YoY</th><th>영업이익 YoY</th><th>기말현금(억)</th></tr>")
        body = "".join(
            f"<tr><td>{r['분기']}</td><td>{r['기준']}</td><td>{_eok(r['매출'])}</td>"
            f"<td>{_eok(r['영업이익'])}</td><td>{_pct(r['영업이익률'])}</td>"
            f"<td>{_pct(r['매출YoY'], 1, True)}</td><td>{_pct(r['영업이익YoY'], 1, True)}</td>"
            f"<td>{_eok(r['현금'])}</td></tr>" for r in rows)
        out.append(f"<div class='wrap'><table><thead>{head}</thead><tbody>{body}</tbody></table></div>")
        out.append("<p class='note'>증감률은 <b>전년 동기 대비</b>입니다 — 계절성이 큰 업종에서 "
                   "전분기 대비만 보면 계절 요인을 실적 변화로 읽습니다. 적자·기저 0 인 분기는 "
                   "배율이 뜻을 잃어 비워 둡니다.</p>")
    return "".join(out) or "<p>정량 자료가 없습니다.</p>"


def _qual_html(qual: dict, price_now: float | None) -> str:
    from html import escape
    out = []
    reports = qual.get("리포트") or []
    if reports:
        head = ("<tr><th>날짜</th><th>증권사</th><th>제목</th><th>의견</th>"
                "<th>목표주가</th><th>작성일 종가</th><th>지금 대비</th></tr>")
        body = ""
        for r in reports:
            tp, base = r["목표주가"], r["작성일종가"]
            gap = ((tp / price_now - 1) * 100) if (tp and price_now) else None
            body += ("<tr>"
                     f"<td>{escape(r['날짜'])}</td>"
                     f"<td>{escape(r['증권사'])}</td>"
                     f"<td>{escape(r['제목'])}</td>"
                     f"<td>{escape(r['의견'] or '—')}</td>"
                     f"<td>{f'{tp:,.0f}' if tp else '—'}</td>"
                     f"<td>{f'{base:,.0f}' if base else '—'}</td>"
                     f"<td>{_pct(gap, 1, True)}</td></tr>")
        out.append(f"<h4>증권사 리포트 (로컬 아카이브 {len(reports)}건)</h4>"
                   f"<div class='wrap'><table><thead>{head}</thead><tbody>{body}</tbody></table></div>")
        out.append("<p class='note'>목표주가는 <b>작성 시점</b>의 값이고 '지금 대비'는 현재가와의 "
                   "괴리일 뿐 적중률이 아닙니다. 아카이브는 내려받은 리포트만 담고 있어 이 종목을 "
                   "다룬 전부가 아닙니다.</p>")
    news_rows = qual.get("뉴스") or []
    if news_rows:
        items = ""
        for n in news_rows:
            title = escape(n["제목"])
            link = (f"<a href='{escape(n['url'])}' target='_blank' rel='noopener noreferrer'>"
                    f"{title}</a>") if n["url"] else title
            items += (f"<li><span class='muted'>{escape(n['일시'])} · "
                      f"{escape(n['언론사'] or '')}</span> {link}</li>")
        out.append(f"<h4>뉴스 헤드라인 (네이버 {len(news_rows)}건)</h4><ul>{items}</ul>")
        out.append("<p class='note'>제목만 가져옵니다. 제목은 사실이 아니라 <b>보도의 프레임</b>이니 "
                   "판단 근거로 쓰기 전에 원문을 여십시오.</p>")
    return "".join(out) or "<p>정성 자료가 없습니다.</p>"


def render(data: dict) -> str:
    from html import escape
    price_now = ((data.get("기술적") or {}).get("가격") or {}).get("종가")
    missing = data.get("결손") or []
    gap = (f"<p class='note'>비어 있는 칸: {escape(' · '.join(missing))}</p>" if missing else "")
    return (
        f"<p class='note'>수집 {escape(data['collected_at'])} · 종목 {escape(data['code'])} "
        f"· {escape(data['name'])}</p>{gap}"
        f"<h3>① 기술적 — 가격·수급 raw</h3>{_tech_html(data.get('기술적') or {})}"
        f"<h3>② 정량적 — DART 재무 · 시장 지표</h3>{_quant_html(data.get('정량적') or {}, price_now)}"
        f"<h3>③ 정성적 — 리포트 · 뉴스</h3>{_qual_html(data.get('정성적') or {}, price_now)}"
        "<p class='note'><b>이 화면은 결론을 내지 않습니다.</b> 가장 좋은 검색식도 건별 승률은 "
        "50% 안팎이고 평균 수익은 소수의 큰 상승이 만듭니다. 세 축이 엇갈리면 엇갈린 채로 두고 "
        "판단은 사람이 하십시오.</p>")


SECTION_JS = '''<section id="analysis-section" style="margin-top:28px;padding:18px;background:var(--band);border-radius:8px;">
<h2>{name} 종목 분석</h2>
<button type="button" id="ana-refresh">다시 분석</button>
<p id="ana-status" role="status" aria-live="polite">기술적·정량적·정성적 근거를 모으는 중입니다…</p>
<div id="ana-content"></div>
</section>
<script>
(function() {{
 const button = document.getElementById('ana-refresh');
 const status = document.getElementById('ana-status');
 const content = document.getElementById('ana-content');
 async function load() {{
   button.disabled = true;
   status.textContent = '기술적·정량적·정성적 근거를 모으는 중입니다…';
   content.innerHTML = '';
   try {{
     const response = await fetch('/api/analysis/{code}', {{cache: 'no-store'}});
     const result = await response.json();
     if (!response.ok) throw new Error(result.error || '분석 자료를 가져오지 못했습니다.');
     content.innerHTML = result.html;
     status.textContent = '수집 완료';
   }} catch (error) {{
     status.textContent = '수집 실패: ' + error.message + ' 다시 분석 버튼으로 재시도할 수 있습니다.';
   }} finally {{ button.disabled = false; }}
 }}
 button.addEventListener('click', load); load();
}})();
</script>'''


def render_section(code: str, name: str) -> str:
    """페이지에 심을 자리만 만든다. 값은 브라우저가 /api/analysis 로 받아 채운다 —
    네이버·DART 왕복이 있어 페이지 로딩을 붙잡으면 안 된다.

    견해(④)는 **따로 부른다.** 근거 수집은 1~2초인데 견해는 수십 초라, 한 요청에 묶으면
    화면이 그동안 비어 있게 된다."""
    import opinion                                          # noqa: PLC0415
    from html import escape
    if not re.fullmatch(r"[0-9A-Z]{6}", code):
        raise ValueError("Invalid stock code")
    return SECTION_JS.format(code=code, name=escape(name)) + opinion.section(code)


def fetch_payload(code: str, name: str) -> dict:
    import dart_collector                                  # noqa: PLC0415
    try:
        dart = dart_collector.collect_dart_financials(code)
    except Exception:                                      # noqa: BLE001
        dart = None                                        # DART 가 없어도 나머지는 낸다
    data = gather(code, name, dart)
    return {"html": render(data), "data": {"결손": data["결손"]}}


# ──────────────────────────────────────────────────── 본문 읽기 (견해용, 느림)

REPORT_DIR = "02. 종목"
PDF_CHARS = 2600          # 리포트 1건당. 요약·투자포인트는 앞쪽 1~2페이지에 있다
NEWS_CHARS = 1200


def report_files(name: str, limit: int = 3) -> list[Path]:
    """이 종목 리포트 PDF 를 최신순으로. 색인에 경로 열이 없으므로 폴더에서 직접 찾는다."""
    idx = report_index()
    if not idx or not name:
        return []
    folder = idx.parent / REPORT_DIR / name
    if not folder.is_dir():
        return []
    # 파일명이 `YYYY-MM-DD_증권사_종목_제목.pdf` 라 이름 역순 = 최신순이다(mtime 은 OneDrive 동기화 시각이라 못 쓴다)
    return sorted(folder.glob("*.pdf"), key=lambda p: p.name, reverse=True)[:limit]


MIN_PDF_CHARS = 300


def report_bodies(name: str, limit: int = 3) -> list[dict]:
    """PDF 앞부분을 텍스트로. `pdftotext` 가 없으면 빈 목록(견해는 제목만으로 쓰게 된다).

    ★ 텍스트가 거의 안 나오는 PDF 가 섞여 있다(실측: 앞장이 이미지인 리포트에서 57자).
    짧으면 **버리고 다음 리포트로 넘어간다** — 57자를 근거랍시고 넣으면 안 된다.
    """
    exe = shutil.which("pdftotext")
    if not exe:
        return []
    out = []
    for path in report_files(name, limit * 3):
        if len(out) >= limit:
            break
        try:
            # -l 2: 앞 2페이지만. 뒤쪽은 재무제표 표라 텍스트로 뽑으면 숫자만 쏟아진다.
            proc = subprocess.run([exe, "-layout", "-l", "2", str(path), "-"],
                                  capture_output=True, text=True, timeout=30)
            if proc.returncode != 0:
                continue
            text = re.sub(r"[ \t]+", " ", proc.stdout)
            text = re.sub(r"\n{2,}", "\n", text).strip()
            if len(text) >= MIN_PDF_CHARS:
                out.append({"파일": path.stem, "본문": text[:PDF_CHARS]})
        except (OSError, subprocess.SubprocessError):
            continue
    return out


def _article_text(url: str) -> str:
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            html_doc = r.read().decode("utf-8", "ignore")
    except Exception:
        return ""
    m = (re.search(r'id="dic_area".*?>(.*?)</article>', html_doc, re.S)
         or re.search(r'id="dic_area".*?>(.*?)</div>', html_doc, re.S))
    if not m:
        return ""
    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", m.group(1), flags=re.S)
    body = re.sub(r"<[^>]+>", " ", body)
    return re.sub(r"\s+", " ", _unescape(body)).strip()


def news_bodies(rows: list[dict], name: str, limit: int = 3, scan: int = 6) -> list[dict]:
    """헤드라인 목록에서 **본문에 이 회사 이름이 나오는 기사만** 골라 본문을 담는다.

    네이버 종목뉴스는 무관한 기사를 섞어 준다(실측: '에버랜드 3D 호러체험'이 삼성전자
    뉴스로 왔다). 제목만으로는 못 거른다 — 은어를 쓰기 때문이다('삼전닉스').
    본문에는 정식 회사명이 나오므로 본문으로 거른다.

    ★ 최신순으로 앞에서 끊으면 안 된다 — 실측에서 가장 관련 있는 '자사주 매입 10월 종료'가
    더 최신인 국채금리·펀드 기사에 밀려 잘렸다. 훑은 뒤 **언급 횟수로 줄을 세워** 고른다.
    """
    hits = []
    for r in rows[:scan]:
        if not r.get("url"):
            continue
        text = _article_text(r["url"])
        if not text or name not in text:
            continue
        hits.append({"일시": r["일시"], "언론사": r["언론사"], "제목": r["제목"],
                     "본문": text[:NEWS_CHARS], "url": r["url"],
                     "_언급": text.count(name) + (2 if name in r["제목"] else 0)})
    hits.sort(key=lambda h: (h["_언급"], h["일시"]), reverse=True)
    return [{k: v for k, v in h.items() if k != "_언급"} for h in hits[:limit]]
