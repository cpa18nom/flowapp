"""flowapp 윈도우 배포판 런처 — 「flowapp 실행.bat」이 부른다.

  폴더 구조 (압축을 푼 곳 = ROOT)
    runtime\\python.exe      같이 넣은 파이썬(설치 불필요)
    app\\                    프로그램 코드 — 업데이트가 통째로 바꾼다
    flowapp 실행.bat
  사용자 자료 (%LOCALAPPDATA%\\flowapp, FLOWAPP_HOME 으로 바꿀 수 있다) — 업데이트·새 압축본이 건드리지 않는다
    market\\                 시세·수급·분봉 데이터와 계산 캐시
    config\\                 키움 키(kiwoom.json)·선택 키(keys.json)
    state\\  logs\\

하는 일
  1. 처음이면 데이터 묶음(flowapp-data.zip)을 받아 푼다 — ROOT 옆에 그 파일이 있으면 그것을 쓴다(USB 등)
  2. 이미 떠 있으면 브라우저만 연다
  3. flowapp 을 띄우고 지켜본다. 종료 코드 42 = 「업데이트 설치」 → 새 코드를 받아 바꾸고 런처째 다시 시작
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

REPO = "cpa18nom/flowapp"
DATA_URL = f"https://github.com/{REPO}/releases/download/data/flowapp-data.zip"
EXIT_UPDATE = 42                  # flowapp → 런처: 새 코드를 받아 달라 · 런처 → bat: 나를 다시 띄워 달라
PORTS = range(int(os.environ.get("FLOWAPP_PORT", 8700)), int(os.environ.get("FLOWAPP_PORT", 8700)) + 10)
NO_OPEN = os.environ.get("FLOWAPP_NO_OPEN") == "1"     # 시험용: 브라우저를 열지 않는다

APP = Path(__file__).resolve().parent.parent          # ROOT\app
ROOT = APP.parent
PY = Path(sys.executable)
HOME = Path(os.environ.get("FLOWAPP_HOME") or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "flowapp")
DATA, CONFIG, STATE, LOGS = HOME / "market", HOME / "config", HOME / "state", HOME / "logs"


def say(msg: str = "") -> None:
    print(msg, flush=True)


def fail(msg: str) -> None:
    say(f"\n✗ {msg}")
    say(f"  로그 폴더: {LOGS}")
    input("\n엔터를 누르면 창을 닫습니다…")
    sys.exit(1)


def env() -> dict:
    """flowapp·수집기가 읽는 경로를 전부 사용자 자료 폴더로 돌린다(코드 폴더에는 아무것도 쓰지 않는다)."""
    for d in (DATA, CONFIG, STATE, LOGS):
        d.mkdir(parents=True, exist_ok=True)
    e = dict(os.environ)
    e.update({
        "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",          # 윈도우 기본(cp949)으로 한글 파일을 읽지 않게
        "FLOWAPP_DIST": "1",
        "MARKET_HOME": str(DATA),
        "FLOWAPP_CONFIG_DIR": str(CONFIG),
        "KIWOOM_CREDENTIALS": str(CONFIG / "kiwoom.json"),
        "KIWOOM_TOKEN_FILE": str(STATE / "kiwoom_token.json"),
        "FLOWAPP_STATE_DIR": str(STATE),
        "FLOWAPP_CACHE_DIR": str(HOME / "cache"),
        "FLOWAPP_TG_CONFIG": str(CONFIG / "telegram.json"),
        "FLOWAPP_WATCH_STATE": str(STATE / "watch.json"),
        "FLOWAPP_PICKS_STATE": str(STATE / "picks.json"),
        "FLOWAPP_TOP3_STATE": str(STATE / "top3.json"),
        "FLOWAPP_VERSION_FILE": str(APP / "VERSION.json"),
        "FLOW_PYTHON": str(PY),
    })
    return e


# ────────────────────────────── 데이터 묶음

def have_data() -> bool:
    return (DATA / "raw" / "meta" / "universe.csv").exists() and any((DATA / "raw" / "price").glob("*.csv.gz"))


def download(url: str, dst: Path, label: str) -> None:
    tmp = dst.with_suffix(dst.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "flowapp"})
    with urllib.request.urlopen(req, timeout=60) as r, tmp.open("wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got, t0, last = 0, time.time(), 0.0
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            f.write(b)
            got += len(b)
            if time.time() - last > 0.5:
                last = time.time()
                speed = got / max(last - t0, 0.1) / 1e6
                pct = f"{got / total * 100:5.1f}%" if total else ""
                size = f"{got / 1e6:,.0f}/{total / 1e6:,.0f}MB" if total else f"{got / 1e6:,.1f}MB"
                print(f"\r  {label} {pct} {size} · {speed:.1f}MB/s   ", end="", flush=True)
    print()
    if total and got != total:
        tmp.unlink(missing_ok=True)
        raise OSError(f"받다가 끊겼습니다({got:,}/{total:,} 바이트)")
    tmp.replace(dst)


def install_data() -> None:
    local = ROOT.parent / "flowapp-data.zip"
    if not local.exists():
        local = ROOT / "flowapp-data.zip"
    say("■ 처음 실행 — 시세·수급 데이터 묶음을 설치합니다 (약 1GB, 한 번만).")
    if local.exists():
        src, temp = local, False
        say(f"  옆에 있는 {local.name} 를 씁니다.")
    else:
        src, temp = HOME / "flowapp-data.zip", True
        try:
            download(DATA_URL, src, "데이터 받는 중")
        except Exception as e:                                       # noqa: BLE001
            fail(f"데이터를 받지 못했습니다: {e}\n  인터넷 연결을 확인하거나, flowapp-data.zip 을 받아 이 폴더 옆에 두고 다시 실행하세요.")
    say("  압축 푸는 중…")
    try:
        with zipfile.ZipFile(src) as z:
            bad = [n for n in z.namelist() if n.startswith(("/", "\\")) or ".." in Path(n).parts]
            if bad:
                raise zipfile.BadZipFile(f"이상한 경로 {bad[:1]}")
            z.extractall(DATA)
    except zipfile.BadZipFile as e:
        if temp:
            src.unlink(missing_ok=True)
        fail(f"데이터 묶음이 손상됐습니다({e}). 다시 실행하면 새로 받습니다.")
    if temp:
        src.unlink(missing_ok=True)
    if not have_data():
        fail("데이터를 풀었지만 필요한 파일이 없습니다.")
    say("  ✓ 데이터 설치 완료")


# ────────────────────────────── 업데이트

def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "flowapp"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else ""


def update() -> bool:
    """GitHub 의 최신 코드로 app 폴더를 바꾼다. 실패하면 지금 코드 그대로 둔다(False)."""
    say("\n■ 업데이트 — 새 버전을 받는 중…")
    try:
        c = _get_json(f"https://api.github.com/repos/{REPO}/commits/main")
        sha, day = c["sha"], c["commit"]["committer"]["date"][:10]
        zpath = HOME / "app-update.zip"
        download(f"https://codeload.github.com/{REPO}/zip/{sha}", zpath, "코드 받는 중")
        new = ROOT / "app.new"
        shutil.rmtree(new, ignore_errors=True)
        with zipfile.ZipFile(zpath) as z:
            top = z.namelist()[0].split("/")[0] + "/"        # flowapp-<sha>/
            for m in z.infolist():
                rel = m.filename[len(top):]
                if not rel or ".." in Path(rel).parts:
                    continue
                out = new / rel
                if m.is_dir():
                    out.mkdir(parents=True, exist_ok=True)
                else:
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_bytes(z.read(m))
        zpath.unlink(missing_ok=True)
        if not (new / "scan" / "flowapp.py").exists() or not (new / "win" / "launcher.py").exists():
            raise RuntimeError("받은 코드에 필요한 파일이 없습니다")
        (new / "VERSION.json").write_text(json.dumps({"sha": sha, "date": day}), encoding="utf-8")
        if _sha(new / "requirements.txt") != _sha(APP / "requirements.txt"):
            say("  필요한 라이브러리가 바뀌어 설치합니다…")
            r = subprocess.run([str(PY), "-m", "pip", "install", "--disable-pip-version-check", "--no-warn-script-location",
                                "-q", "-r", str(new / "requirements.txt")])
            if r.returncode != 0:
                raise RuntimeError("라이브러리 설치 실패")
        old = ROOT / "app.old"
        shutil.rmtree(old, ignore_errors=True)
        for i in range(10):                                  # 백신·탐색기가 잠깐 쥐고 있으면 이름 바꾸기가 실패한다
            try:
                APP.rename(old)
                break
            except OSError:
                time.sleep(1)
        else:
            raise RuntimeError("app 폴더를 바꾸지 못했습니다(다른 프로그램이 사용 중)")
        new.rename(APP)
        shutil.rmtree(old, ignore_errors=True)
        say(f"  ✓ 업데이트 완료 ({sha[:7]} · {day})")
        return True
    except Exception as e:                                       # noqa: BLE001
        shutil.rmtree(ROOT / "app.new", ignore_errors=True)
        say(f"  ✗ 업데이트 실패: {e} — 지금 버전으로 계속합니다.")
        return False


# ────────────────────────────── 실행

def running_at(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as r:
            return r.read() == b"ok"
    except Exception:                                            # noqa: BLE001
        return False


def port_free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def main() -> int:
    for st in (sys.stdout, sys.stderr):                             # 콘솔 기본(cp949)이면 한글·기호 출력에서 죽는다
        try:
            st.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    say("flowapp — 수급분석 스캐너 (윈도우판)")
    say("  이 창을 닫으면 프로그램이 종료됩니다.\n")
    for p in PORTS:
        if running_at(p):
            say(f"이미 실행 중입니다 → http://localhost:{p}")
            if not NO_OPEN:
                os.startfile(f"http://localhost:{p}")            # noqa: S606
            time.sleep(2)
            return 0
    port = next((p for p in PORTS if port_free(p)), None)
    if port is None:
        fail(f"{PORTS.start}~{PORTS.stop - 1} 포트가 모두 사용 중입니다.")
    e = env()
    if not have_data():
        install_data()
    say(f"■ 프로그램 시작 (처음에는 지표 계산에 1~5분 걸립니다) → http://localhost:{port}")
    log = (LOGS / "flowapp.log").open("a", encoding="utf-8")
    log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} port {port}\n")
    log.flush()
    proc = subprocess.Popen([str(PY), str(APP / "scan" / "flowapp.py"), "--port", str(port)] + (["--no-open"] if NO_OPEN else []),
                            env=e, cwd=str(HOME), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", bufsize=1)
    try:
        for line in proc.stdout:                                     # 화면에도 보이고 로그에도 남긴다
            print(line, end="", flush=True)
            log.write(line)
            log.flush()
        rc = proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        return 0
    finally:
        log.close()
    if rc == EXIT_UPDATE:
        update()
        return EXIT_UPDATE                                           # bat 이 (새) 런처를 다시 띄운다
    if rc != 0:
        fail(f"프로그램이 오류로 끝났습니다(exit {rc}).")
    say("종료했습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
