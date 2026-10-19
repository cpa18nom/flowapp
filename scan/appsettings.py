"""윈도우 배포판 전용 — 각자의 API 키 설정 · 프로그램 업데이트 · 종료 (사용자 요청 2026-10-19).

배포판 런처(win/launcher.py)가 FLOWAPP_DIST=1 과 아래 경로들을 환경변수로 넘겨 띄울 때만 켜진다.
로컬(WSL) 실행에서는 꺼져 있다 — 그쪽 키 파일(~/.config/kiwoom/credentials.json)을 덮어쓰지 않기 위해서다.

  키움 키   KIWOOM_CREDENTIALS 가 가리키는 JSON {appkey, secretkey, is_mock} — bin/flow.py 가 그대로 읽는다
  그 밖     FLOWAPP_CONFIG_DIR/keys.json {dart, anthropic} — 이 프로세스의 환경변수로 옮겨 DART·견해 기능이 쓴다
  버전      FLOWAPP_VERSION_FILE {sha, date} — 빌드·런처의 업데이트가 코드 폴더에 적는다

프로그램을 바꾸는 일(업데이트·종료)은 이 프로세스가 하지 않는다. 종료 코드로 런처에 알리고 끝난다:
  EXIT_UPDATE(42) = 새 코드를 받아 다시 띄워 달라 · 0 = 그냥 끝.
키 값은 화면·로그에 다시 내보내지 않는다(끝 4자리만).
"""
from __future__ import annotations

import html
import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

REPO = os.environ.get("FLOWAPP_REPO", "cpa18nom/flowapp")
EXIT_UPDATE = 42


def enabled() -> bool:
    return os.environ.get("FLOWAPP_DIST") == "1" and bool(os.environ.get("KIWOOM_CREDENTIALS"))


def _config_dir() -> Path:
    return Path(os.environ.get("FLOWAPP_CONFIG_DIR") or Path(os.environ["KIWOOM_CREDENTIALS"]).parent)


def _cred_path() -> Path:
    return Path(os.environ["KIWOOM_CREDENTIALS"])


def _keys_path() -> Path:
    return _config_dir() / "keys.json"


def _read(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write(p: Path, d: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def apply_env() -> None:
    """keys.json 의 선택 키를 이 프로세스 환경변수로. 서버 시작 때와 저장 직후에 부른다."""
    if not enabled():
        return
    k = _read(_keys_path())
    for name, env in (("dart", "DART_API_KEY"), ("anthropic", "ANTHROPIC_API_KEY")):
        if k.get(name):
            os.environ[env] = k[name]
        else:
            os.environ.pop(env, None)


def kiwoom_ready() -> bool:
    c = _read(_cred_path()) if enabled() else {}
    return bool(c.get("appkey") and c.get("secretkey"))


def _tail(v: str | None) -> str:
    return f"저장됨 (끝자리 …{html.escape(v[-4:])})" if v else "비어 있음"


def save(form: dict) -> str:
    """설정 저장. 빈 칸은 '그대로 둔다'. 돌려주는 값 = 화면에 띄울 한 줄."""
    def f(k):
        return (form.get(k) or [""])[-1].strip()
    cred = _read(_cred_path())
    changed = False
    for k in ("appkey", "secretkey"):
        if f(k):
            cred[k] = f(k)
            changed = True
    mock = f("server") == "mock"
    if cred.get("is_mock") != mock:
        cred["is_mock"] = mock
        changed = True
    if changed:
        _write(_cred_path(), cred)
        _forget_kiwoom_token()
    keys = _read(_keys_path())
    for k in ("dart", "anthropic"):
        if f(k) == "-":                    # '-' 하나 = 지우기
            keys.pop(k, None)
        elif f(k):
            keys[k] = f(k)
    _write(_keys_path(), keys)
    apply_env()
    return "저장했습니다."


def _flow():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
    import flow                                                  # noqa: PLC0415
    return flow


def _forget_kiwoom_token() -> None:
    """키를 바꾸면 옛 키로 받아 둔 토큰을 버린다(다음 요청이 새 키로 발급받는다)."""
    try:
        _flow().invalidate_token()
    except Exception:                                            # noqa: BLE001
        pass


def test_kiwoom() -> tuple[bool, str]:
    """토큰을 새로 받아 본다. 실패 사유만 보여 주고 키는 보이지 않는다."""
    if not kiwoom_ready():
        return False, "키움 앱키·시크릿키를 먼저 저장하세요."
    flow = _flow()
    flow.invalidate_token()
    try:
        _, base = flow.token()
    except Exception as e:                                       # noqa: BLE001
        msg = str(e)
        hint = " — 키움 OpenAPI 사이트에서 이 PC의 IP 를 허용 IP 로 등록했는지 확인하세요." if "IP" in msg or "ip" in msg else ""
        return False, f"연결 실패: {msg[:160]}{hint}"
    return True, f"연결 성공 ({'모의투자' if 'mock' in base else '실서버'})"


# ────────────────────────────── 프로그램 업데이트

def local_version() -> dict:
    """설치된 코드의 버전 {sha, date}. 런처가 FLOWAPP_VERSION_FILE(코드 폴더의 VERSION.json)을 알려 준다."""
    f = os.environ.get("FLOWAPP_VERSION_FILE")
    return _read(Path(f)) if enabled() and f else {}


def remote_version(timeout: float = 10) -> dict:
    """GitHub 의 최신 커밋 {sha, date, message}. 받지 못하면 예외."""
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/commits/main",
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "flowapp"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        j = json.loads(r.read().decode("utf-8"))
    return {"sha": j["sha"], "date": j["commit"]["committer"]["date"][:10],
            "message": j["commit"]["message"].splitlines()[0][:120]}


def check_update() -> dict:
    """{"current", "latest", "new", "message"} 또는 {"error"}."""
    cur = local_version().get("sha", "")
    try:
        rv = remote_version()
    except Exception as e:                                       # noqa: BLE001
        return {"error": f"업데이트 정보를 받지 못했습니다({repr(e)[:80]})", "current": cur[:7]}
    return {"current": cur[:7], "latest": rv["sha"][:7], "date": rv["date"], "message": rv["message"],
            "new": rv["sha"] != cur}


def exit_later(code: int, delay: float = 1.0) -> None:
    """응답을 보낸 뒤 프로세스를 끝낸다. 런처가 종료 코드를 보고 업데이트·재시작·종료를 한다."""
    def _go():
        time.sleep(delay)
        os._exit(code)
    threading.Thread(target=_go, daemon=True).start()


# ────────────────────────────── 화면

def page_body(msg: str = "") -> str:
    cred = _read(_cred_path())
    keys = _read(_keys_path())
    mock = bool(cred.get("is_mock"))
    v = local_version()
    note = f'<p class="note" role="status"><b>{html.escape(msg)}</b></p>' if msg else ""
    first = "" if kiwoom_ready() else (
        '<p class="note"><b>처음 쓰시나요?</b> 키움 키가 없어도 검색·차트는 같이 받은 데이터로 바로 됩니다. '
        '「최신 데이터 받기」로 매일 새 수급·분봉을 받으려면 아래에 본인 키움 REST API 키를 넣으세요.</p>')
    return f"""<h1>설정</h1>{note}{first}
<form method="post" action="/api/settings" class="settings">
<fieldset><legend>키움 REST API (수급·5분봉·거래대금 받기)</legend>
<p class="note">발급: <a href="https://openapi.kiwoom.com" target="_blank" rel="noopener">키움 OpenAPI 사이트</a> → REST API 신청 → 앱키·시크릿키.
신청 화면에서 <b>이 PC의 공인 IP</b>를 허용 IP 로 등록해야 연결됩니다.</p>
<label>앱키 <input name="appkey" type="password" autocomplete="off" placeholder="{_tail(cred.get('appkey'))}"></label>
<label>시크릿키 <input name="secretkey" type="password" autocomplete="off" placeholder="{_tail(cred.get('secretkey'))}"></label>
<div class="radios">서버
<label><input type="radio" name="server" value="real"{'' if mock else ' checked'}> 실서버</label>
<label><input type="radio" name="server" value="mock"{' checked' if mock else ''}> 모의투자</label></div>
<p class="note">모의투자 키는 요청 한도가 낮아 받기가 3~4배 느립니다.</p>
</fieldset>
<fieldset><legend>선택 기능</legend>
<label>DART 인증키 (종목 화면의 공시·재무) <input name="dart" type="password" autocomplete="off" placeholder="{_tail(keys.get('dart'))}"></label>
<p class="note">발급: <a href="https://opendart.fss.or.kr" target="_blank" rel="noopener">OpenDART</a> → 인증키 신청 (무료)</p>
<label>Anthropic API 키 (AI 견해 · 유료) <input name="anthropic" type="password" autocomplete="off" placeholder="{_tail(keys.get('anthropic'))}"></label>
<p class="note">빈 칸으로 저장하면 기존 값을 그대로 둡니다. 지우려면 <code>-</code> 하나만 넣고 저장하세요.</p>
</fieldset>
<div class="actions"><button type="submit" name="do" value="save">저장</button>
<button type="submit" name="do" value="test">저장하고 키움 연결 시험</button></div>
</form>
<form method="post" action="/api/app" class="settings">
<fieldset><legend>프로그램</legend>
<p>현재 버전: <b>{html.escape(v.get('sha', '')[:7] or '알 수 없음')}</b> {html.escape(v.get('date', ''))}</p>
<p id="upd-info" class="note">「업데이트 확인」을 누르면 새 버전이 있는지 봅니다.</p>
<div class="actions"><button type="button" id="upd-check">업데이트 확인</button>
<button type="submit" name="do" value="update" id="upd-go" hidden>업데이트 설치 (자동 재시작)</button>
<button type="submit" name="do" value="quit" class="ghost">프로그램 종료</button></div>
</fieldset></form>
<script>
document.getElementById('upd-check').onclick = async () => {{
  const info = document.getElementById('upd-info');
  info.textContent = '확인 중…';
  try {{
    const j = await (await fetch('/api/app-version')).json();
    if (j.error) {{ info.textContent = j.error; return; }}
    info.textContent = j.new ? `새 버전 ${{j.latest}} (${{j.date}}) — ${{j.message}}` : `최신 버전입니다 (${{j.current}}).`;
    document.getElementById('upd-go').hidden = !j.new;
  }} catch (e) {{ info.textContent = '확인 실패: ' + e; }}
}};
</script>"""


SETTINGS_CSS = """<style>
form.settings{max-width:720px;margin:0 0 20px}
form.settings fieldset{border:1px solid #d0d7de;border-radius:8px;padding:12px 16px;margin:0 0 14px;background:#fff}
form.settings legend{font-weight:700;padding:0 6px}
form.settings label{display:block;margin:8px 0}
form.settings input[type=password]{display:block;width:100%;max-width:520px;padding:7px 9px;border:1px solid #c4ccd4;border-radius:6px;font-size:14px}
form.settings .radios label{display:inline-block;margin:6px 14px 6px 0}
form.settings .actions{display:flex;gap:10px;flex-wrap:wrap}
form.settings button{padding:8px 16px;border-radius:6px;border:1px solid #1f6feb;background:#1f6feb;color:#fff;font-size:14px;cursor:pointer}
form.settings button.ghost,form.settings button[type=button]{background:#fff;color:#1f6feb}
</style>"""
