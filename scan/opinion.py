"""종목 견해 — 모아둔 근거를 읽고 **판단을 문장으로** 쓴다.

`analysis.py` 는 근거를 세워 놓는 데까지만 한다. 그 다음 한 걸음(리포트·뉴스·수치를 읽고
"그래서 어떻게 보는가")은 사람 아니면 모델이 해야 하는 일이라 여기서 Claude 를 부른다.

**전송 경로 두 가지 — 자격증명이 있는 쪽을 쓴다**
  1. `anthropic` SDK (`ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` 이 있을 때). 공식 경로다.
  2. `claude -p` 헤드리스 (키가 없을 때). 이 PC 에는 키가 없고 Claude Code 로그인만 있다
     — 실측 2026-09-20: SDK 는 "Could not resolve authentication method" 로 죽고,
     `claude -p` 는 exit 0 으로 답한다. 그래서 기본은 CLI 다.

**화면이 지키는 것**: 이 글은 모델이 쓴 것이고 매매권유가 아니다. 근거로 준 자료 밖의
사실을 지어내지 말라고 프롬프트가 못박고, 화면에도 그렇게 적는다.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import time

MODEL = os.environ.get("FLOWAPP_OPINION_MODEL", "claude-opus-5")
TIMEOUT = int(os.environ.get("FLOWAPP_OPINION_TIMEOUT", "240"))
CACHE_TTL = 3600
_CACHE: dict[tuple, tuple[float, dict]] = {}
_LOCK = threading.Lock()

SYSTEM = """당신은 한국 주식을 보는 애널리스트입니다. 아래 '자료'만 근거로 판단하십시오.

지켜야 할 것
- 자료에 없는 사실(수치·사건·인물)을 지어내지 마십시오. 모르면 모른다고 쓰십시오.
- 확인된 수치는 단정하고, 추정·전망은 "~로 보입니다 / ~일 가능성이 있습니다"로 구분하십시오.
- 목표주가는 작성 시점의 값입니다. 오래된 목표주가를 현재 의견처럼 쓰지 마십시오.
- 강세 논거와 약세 논거가 엇갈리면 **엇갈린 채로** 제시하십시오. 억지로 하나로 합치지 마십시오.
- "매수/매도" 같은 권유 대신, 무엇을 보고 판단해야 하는지를 쓰십시오.
- **리포트·뉴스 본문이 주어지면 거기서 구체적인 것을 꺼내 쓰십시오** — 애널리스트의 추정치와
  가정(환율·ASP·capex), 소송·규제·경쟁 같은 사건. 제목만 훑은 듯한 일반론은 쓰지 마십시오.
- 증권사 추정치는 **회사가 확정한 실적과 구분**해서 "◯◯증권 추정"이라고 밝히십시오.

형식 (한국어, 전체 900자 이내, 마크다운 소제목 없이 단락으로)
1) 첫 문장: 지금 이 종목의 핵심 쟁점 한 줄
2) 강세 근거 2~3개 (자료의 수치를 인용)
3) 약세·리스크 근거 2~3개
4) 기술적 지표와 수급이 실적·뉴스와 엇갈리는 지점
5) 마지막 줄: 앞으로 무엇이 확인되면 판단이 바뀌는가"""


def _fmt(v, digits=1, suffix=""):
    return "—" if v is None else f"{v:,.{digits}f}{suffix}"


def build_prompt(data: dict, bodies: dict | None = None) -> str:
    """모아둔 근거를 모델이 읽을 수 있게 정리한다.

    수치·제목만으로는 판단이 얕아진다(실측: 제목만 준 첫 버전은 ITC 소송·관세·HBM 수율 같은
    쟁점을 전혀 못 봤다). 그래서 **리포트 PDF 앞 2페이지와 뉴스 본문**을 같이 넣는다.
    """
    tech = (data.get("기술적") or {})
    price, flow = tech.get("가격") or {}, tech.get("수급") or {}
    quant = data.get("정량적") or {}
    ind = (quant.get("지표") or {}).get("지표") or {}
    con = (quant.get("지표") or {}).get("컨센서스") or {}
    qual = data.get("정성적") or {}

    lines = [f"# 종목: {data.get('name')} ({data.get('code')})", "", "## 가격·수급 (자체 raw)"]
    if price:
        lines += [
            f"- 기준일 {str(price.get('기준일'))[:10]} · 종가 {_fmt(price.get('종가'), 0)}원",
            f"- 52주 위치 {_fmt(price.get('52주위치'), 0, '%')} "
            f"(저 {_fmt(price.get('52주저가'), 0)} · 고 {_fmt(price.get('52주고가'), 0)}) · "
            f"고가대비 {_fmt(price.get('고가대비'), 1, '%')}",
            f"- 이평배열 {price.get('이평배열')} · 이격 20일 {_fmt(price.get('이격20'), 1, '%')} · "
            f"120일 {_fmt(price.get('이격120'), 1, '%')} · 변동성60일 {_fmt(price.get('변동성60일'), 1, '%')}",
        ]
    if flow:
        lines.append(
            f"- 수급 분산비율: 개인 {_fmt(flow.get('개인분산'), 2)} · 세력 {_fmt(flow.get('세력분산'), 2)} "
            f"· SIG {_fmt(flow.get('SIG'), 2)} (자기이력 백분위 개인 {_fmt(flow.get('개인분산_백분위'), 0)}% · "
            f"세력 {_fmt(flow.get('세력분산_백분위'), 0)}%). "
            "주의: 수급 데이터가 2020-12부터라 매집 기준점이 잘려 절대값 신뢰도는 낮다.")

    lines += ["", "## 재무·시장지표"]
    if ind:
        lines.append("- " + " · ".join(f"{k} {v}" for k, v in ind.items()))
    if con.get("목표주가"):
        lines.append(f"- 증권사 컨센서스({con.get('기준일')}) 목표주가 {_fmt(con['목표주가'], 0)}원 "
                     f"· 평균의견 {_fmt(con.get('평균의견'), 2)}")
    for r in (quant.get("분기") or [])[-6:]:
        lines.append(
            f"- {r['분기']}({r['기준']}) 매출 {_fmt((r['매출'] or 0) / 1e8, 0)}억 · "
            f"영업이익 {_fmt((r['영업이익'] or 0) / 1e8, 0)}억 · 영업이익률 {_fmt(r['영업이익률'], 1, '%')} · "
            f"매출YoY {_fmt(r['매출YoY'], 1, '%')} · 영업이익YoY {_fmt(r['영업이익YoY'], 1, '%')}")

    reports = qual.get("리포트") or []
    if reports:
        lines += ["", "## 증권사 리포트 (작성 시점 기준)"]
        for r in reports[:8]:
            lines.append(f"- {r['날짜']} {r['증권사']} 「{r['제목']}」 의견 {r['의견'] or '—'} · "
                         f"목표주가 {_fmt(r['목표주가'], 0)} (작성일 종가 {_fmt(r['작성일종가'], 0)})")
    news = qual.get("뉴스") or []
    if news:
        lines += ["", "## 최근 뉴스 헤드라인"]
        lines += [f"- {n['일시']} {n['언론사']}: {n['제목']}" for n in news[:10]]

    bodies = bodies or {}
    for r in (bodies.get("리포트") or []):
        lines += ["", f"## [리포트 본문] {r['파일']}", r["본문"]]
    for n in (bodies.get("뉴스") or []):
        lines += ["", f"## [뉴스 본문] {n['일시']} {n['언론사']} — {n['제목']}", n["본문"]]
    if bodies.get("리포트") or bodies.get("뉴스"):
        lines += ["", "(본문은 앞부분만 잘라 넣은 것입니다. 잘린 뒤의 내용을 추측하지 마십시오.)"]
    return "\n".join(lines)


def _via_sdk(prompt: str) -> str:
    import anthropic                                        # noqa: PLC0415
    client = anthropic.Anthropic()
    r = client.messages.create(
        model=MODEL, max_tokens=16000,
        thinking={"type": "adaptive"},
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in r.content if b.type == "text").strip()


def _via_cli(prompt: str) -> str:
    exe = shutil.which("claude")
    if not exe:
        raise RuntimeError("claude CLI 가 PATH 에 없습니다")
    # 프롬프트는 **표준입력**으로 준다 — argv 로 넘기면 길이 제한에 걸린다.
    # 도구는 막는다: 견해는 아래 자료만 읽고 쓰는 것이고, 중첩 세션이 파일을 뒤질 이유가 없다.
    proc = subprocess.run(
        [exe, "-p", "--model", MODEL, "--append-system-prompt", SYSTEM,
         "--disallowed-tools", "Bash,Read,Write,Edit,WebFetch,WebSearch,Task"],
        input=prompt, capture_output=True, text=True, timeout=TIMEOUT,
        cwd=tempfile.gettempdir(), env={**os.environ, "CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1"},
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "claude CLI 실패").strip()[:200])
    return proc.stdout.strip()


def available() -> str:
    """쓸 수 있는 전송 경로("sdk"/"cli"), 없으면 "". 서버에는 Claude Code 로그인이 없다."""
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return "sdk"
    return "cli" if shutil.which("claude") else ""


def _key(data: dict) -> tuple:
    price = ((data.get("기술적") or {}).get("가격") or {})
    return (data.get("code"), str(price.get("기준일"))[:10], MODEL)


def peek(data: dict) -> dict | None:
    """이미 만들어 둔 견해(캐시)만 돌려준다 — 새로 만들지 않는다. 보기 전용 접속이 유료 API 를 부르지 않게 하는 문."""
    with _LOCK:
        hit = _CACHE.get(_key(data))
    return hit[1] if hit and time.time() - hit[0] < CACHE_TTL else None


def generate(data: dict) -> dict:
    """견해 한 편. 실패해도 예외를 올리지 않는다 — 화면 한 칸이 비는 것으로 끝낸다."""
    key = _key(data)
    now = time.time()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < CACHE_TTL:
            return hit[1]

    if not available():
        # 엔진이 없는데 본문(PDF·뉴스)부터 수 초 읽고 실패하지 않는다.
        return {"model": MODEL, "text": "", "engine": "",
                "error": "이 환경에는 견해 엔진이 없습니다(ANTHROPIC_API_KEY 또는 claude CLI 필요)"}

    # 본문 읽기는 느리다(PDF 2초 + 뉴스 1초). 견해를 쓸 때만 부른다 — 근거 화면은 이걸 기다리지 않는다.
    import analysis                                         # noqa: PLC0415
    name = data.get("name") or ""
    try:
        bodies = {"리포트": analysis.report_bodies(name),
                  "뉴스": analysis.news_bodies((data.get("정성적") or {}).get("뉴스") or [], name)}
    except Exception:                                       # noqa: BLE001
        bodies = {}
    prompt = build_prompt(data, bodies)
    out = {"model": MODEL, "text": "", "engine": "", "error": ""}
    for engine, fn in (("sdk", _via_sdk), ("cli", _via_cli)):
        try:
            out["text"], out["engine"] = fn(prompt), engine
            break
        except Exception as exc:                            # noqa: BLE001
            out["error"] = f"{engine}: {type(exc).__name__} {str(exc)[:120]}"
    if out["text"]:
        out["error"] = ""
        with _LOCK:
            _CACHE[key] = (now, out)
            if len(_CACHE) > 16:
                for k in sorted(_CACHE, key=lambda k: _CACHE[k][0])[:6]:
                    _CACHE.pop(k, None)
    return out


def render(out: dict) -> str:
    from html import escape
    if not out.get("text"):
        return ("<p class='note'>견해를 생성하지 못했습니다 — "
                f"{escape(out.get('error') or '알 수 없는 이유')}. "
                "위의 근거만으로 직접 판단하십시오.</p>")
    paras = "".join(f"<p>{escape(p.strip())}</p>"
                    for p in out["text"].split("\n") if p.strip())
    via = "Anthropic API" if out.get("engine") == "sdk" else "Claude Code 헤드리스"
    return (paras + f"<p class='note'><b>이 견해는 {escape(out.get('model', ''))} 가 "
            f"위 근거만 보고 쓴 것입니다({escape(via)}).</b> 매매권유가 아니며, "
            "모델이 자료를 잘못 읽었을 수 있습니다 — 숫자는 위 표와 공시 원문으로 대조하십시오.</p>")


SECTION = '''<section id="op-section" style="margin-top:22px">
<h3>④ 견해 — 위 근거를 읽고 쓴 판단</h3>
<p id="op-status" role="status" aria-live="polite">근거를 읽고 견해를 쓰는 중입니다… (수십 초 걸립니다)</p>
<div id="op-content"></div>
</section>
<script>
(function() {{
 const status = document.getElementById('op-status');
 const content = document.getElementById('op-content');
 (async function load() {{
   try {{
     const r = await fetch('/api/opinion/{code}', {{cache: 'no-store'}});
     const result = await r.json();
     if (!r.ok) throw new Error(result.error || '견해 생성에 실패했습니다.');
     content.innerHTML = result.html;
     status.textContent = '작성 완료';
   }} catch (e) {{
     status.textContent = '생성 실패: ' + e.message;
   }}
 }})();
}})();
</script>'''


def section(code: str) -> str:
    import re                                               # noqa: PLC0415
    if not re.fullmatch(r"[0-9A-Z]{6}", code):
        raise ValueError("Invalid stock code")
    return SECTION.format(code=code)
