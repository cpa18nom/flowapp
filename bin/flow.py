#!/usr/bin/env python3
"""flow — 키움 ka10059 투자자별 매매동향 raw 저장소.

  flow backfill [--pages 14] [--workers 1] [--only 005930]   유니버스 전이력(재개 가능)
  flow update [--until YYYYMMDD]                             마지막 저장일 다음날부터 증분
  flow status                                                커버리지 점검
  flow universe                                              현재 유니버스 요약

저장
  ~/market/raw/flow/{code}.csv.gz
    dt,cur_prc,pred_pre,flu_rt,acc_trde_qty,ind,frgn,orgn,fnnc,insrnc,invtrt,
    etcfnnc,bank,penfnd,samo,natn,etccorp,natfor      (투자자 수치 단위: **주**)

주의
  - 요청 간격 PACE=0.143초(초당 7건). 실측 2026-10-01, 순차·429 는 0.3초 뒤 재시도, 300~500종목씩:
      간격 0.25 → 4.1종목/초 · 429 0%  (전종목 10.6분)      0.2  → 4.9종목/초 · 429 0%  (8.8분)
      간격 0.143 → 6.6종목/초 · 429 1% (6.4분)  ← 채택      0.125 → 6.9종목/초 · 429 5% (6.2분)
      간격 0.1  → 7.9종목/초 · 429 7%  (5.4분)              0.08 → 7.9종목/초 · 429 12% (5.4분)
    성공 처리량의 천장은 초당 약 8건이다 — 0.1초 아래로는 거절만 늘고 빨라지지 않는다. 동시 요청으로
    초당 24건을 쏴도 성공은 초당 9~11건(나머지 54% 는 429)이라 워커를 늘려도 소용없다(한도는 키 단위).
    9/29 의 "한도 ≈ 5건/초"는 20건짜리 표본으로 잰 값이라 낮게 잡혀 있었다. 더 당기려면 --sleep 0.1.
    429 재시도는 짧게(0.3초부터) 쉰다 — 1.5초씩 쉬면 거절 7% 만으로 지금보다 느려진다.
    1페이지 ≈ 100거래일. 예전 1.2초는 실측 없는 과잉 대기였다(전종목 51분).
  - 종목 단위 요청(ka10059)을 대신할 일괄 조회는 없다(실측 2026-10-01): ka10058 은 투자자별 **상위 100종목**만
    주고(cont-yn=N, 수량 100주 단위), KRX 정보데이터시스템은 로그인 없이는 `LOGOUT`(400)이다.
  - cur_prc 는 전일대비 부호가 붙어 오므로 읽을 때 abs() 한다.
  - unit_tp=1(단주)로 받는다. 1000(천주)로 받으면 거래가 얇은 종목은 전 항목이 0으로
    반올림돼 사라진다(실측: 태광산업 거래량 716주 → 천주 단위에서 개인/외인/기관 전부 0).
    반올림은 대형주에도 영향이 있다 — 삼성전자 60일 중 28일에서 기관계 ≠ 세부8항목 합이었고,
    단주로 받으면 0일이 된다.
  - 장중에는 거래량은 있는데 투자자 항목이 전부 0인 행이 오므로 걸러낸다.
  - 키움 원주가 ≠ FDR 수정주가. 합병·액면분할 종목은 수급 수량의 연속성도 의심해야 한다.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import daily as _daily                                   # noqa: E402

ROOT = Path(os.environ.get("MARKET_HOME", Path.home() / "market"))
FLOW = ROOT / "raw" / "flow"
META = ROOT / "raw" / "meta"
STATE = META / "flow_state.json"
PACE = 0.143         # 요청 시작 간 간격(초) = 초당 7건, 429 약 1% — 모듈 docstring 의 실측표 참고
RETRY_429 = 0.3      # 429 뒤 첫 대기(초), 이후 2배씩. 한도는 1초 안에 풀린다(실측 최대 연속 2회)
_limited = {"n": 0}  # 이번 실행에서 429 로 다시 보낸 횟수 — 진행 줄에 낸다(늘면 PACE 가 한도를 넘은 것)
UNIVERSE = META / "universe.csv"

FIELDS = ["dt", "cur_prc", "pred_pre", "flu_rt", "acc_trde_qty", "ind_invsr", "frgnr_invsr",
          "orgn", "fnnc_invt", "insrnc", "invtrt", "etc_fnnc", "bank", "penfnd_etc",
          "samo_fund", "natn", "etc_corp", "natfor"]
OUT = ["dt", "px_raw", "pred_pre", "flu_rt", "vol", "ind", "frgn", "orgn", "fnnc", "insrnc",
       "invtrt", "etcfnnc", "bank", "penfnd", "samo", "natn", "etccorp", "natfor"]

_http = {"c": None}


def _client():
    """연결 재사용. 요청마다 TLS 를 새로 맺으면 0.2초, 재사용하면 0.03초(실측 2026-09-23)."""
    import httpx
    if _http["c"] is None:
        _http["c"] = httpx.Client(timeout=30)
    return _http["c"]


_tok = {"t": None, "exp": 0.0, "base": None}
TOKEN_FILE = Path(os.environ.get("KIWOOM_TOKEN_FILE", Path.home() / ".cache" / "kiwoom" / "token.json"))
AUTH_CODES = {"8005", "8001", "8002", "8003"}   # 키움 인증 계열 코드(8005 = Token이 유효하지 않습니다)


def auth_rejected(j: dict, status: int = 200) -> bool:
    """토큰 거부인가. ★ 실측 2026-09-29: 8005 는 return_code 가 **아니라 메시지 안**에 온다.
      REST : HTTP 200 · return_code 3 · return_msg "인증에 실패했습니다[8005:Token이 유효하지 않습니다]"
      WS   : return_code 805004 · return_msg "토큰 인증에 실패했습니다. … [CODE=8005, MESSAGE=…]"
    그래서 코드 필드만 보면 못 잡는다 — 메시지의 80xx 를 같이 본다."""
    if status == 401:
        return True
    rc = str(j.get("return_code")); msg = str(j.get("return_msg") or "")
    if rc in AUTH_CODES:
        return True
    return bool(re.search(r"(?:\[|CODE=)\s*80\d\d\b", msg)) or (rc not in ("0", "None") and "인증" in msg)


def _parse_exp(r: dict) -> float:
    """expires_dt('YYYYMMDDHHMMSS', KST) → epoch. 없으면 6시간. 만료 1분 전에 갱신한다."""
    try:
        return time.mktime(time.strptime(str(r["expires_dt"]), "%Y%m%d%H%M%S")) - 60
    except (KeyError, ValueError, TypeError):
        return time.time() + 6 * 3600


def invalidate_token() -> None:
    """서버가 토큰을 거부(8005)했을 때. 메모리·파일 캐시를 지워 다음 token() 이 새로 발급받게 한다."""
    _tok.update(t=None, exp=0.0)
    try:
        TOKEN_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def kiwoom_credentials() -> dict:
    """키움 자격. 환경변수(서버: fly secrets) → 파일(KIWOOM_CREDENTIALS 또는 로컬 기본) 순.
    값을 출력·로그에 남기지 않는다."""
    if os.environ.get("KIWOOM_APPKEY") and os.environ.get("KIWOOM_SECRETKEY"):
        return {"appkey": os.environ["KIWOOM_APPKEY"], "secretkey": os.environ["KIWOOM_SECRETKEY"],
                "is_mock": os.environ.get("KIWOOM_MOCK", "") in ("1", "true", "True")}
    p = Path(os.environ.get("KIWOOM_CREDENTIALS",
                            Path.home() / ".config" / "kiwoom" / "credentials.json"))
    return json.loads(p.read_text("utf-8"))


def token():
    """접근토큰. 프로세스 안 캐시 → 파일 캐시(앱·CLI 공유) → 발급.

    실측 2026-09-29: 같은 키로 다시 발급하면 **같은 토큰**이 오고 이전 것도 살아 있다(폐기 아님).
    만료는 응답의 expires_dt(발급 후 24시간). 그래도 만료 전에 8005 가 온 사례가 있어 — 원인 미확인 —
    거부를 받으면 kiwoom_post/웹소켓이 invalidate_token() 뒤 한 번 다시 발급받아 재시도한다."""
    import httpx
    now = time.time()
    if _tok["t"] and now < _tok["exp"]:
        return _tok["t"], _tok["base"]
    try:
        c = json.loads(TOKEN_FILE.read_text("utf-8"))
        if c.get("t") and now < float(c.get("exp", 0)) and c.get("base"):
            _tok.update(t=c["t"], exp=float(c["exp"]), base=c["base"])
            return _tok["t"], _tok["base"]
    except (OSError, ValueError):
        pass
    cr = kiwoom_credentials()
    base = "https://mockapi.kiwoom.com" if cr.get("is_mock") else "https://api.kiwoom.com"
    r = httpx.post(f"{base}/oauth2/token",
                   headers={"Content-Type": "application/json;charset=UTF-8"},
                   json={"grant_type": "client_credentials",
                         "appkey": cr["appkey"], "secretkey": cr["secretkey"]}, timeout=30).json()
    t = r.get("token") or r.get("access_token")
    if not t:
        # sys.exit 를 쓰면 재시도 루프 안에서 프로세스가 통째로 죽는다.
        # 장시간 무인 실행에서는 예외로 올려 상위 백오프가 다시 시도하게 한다.
        raise RuntimeError(f"키움 토큰 발급 실패: {sorted(r.keys())} {str(r.get('return_msg'))[:60]}")
    _tok.update(t=t, exp=_parse_exp(r), base=base)
    try:
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = TOKEN_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"t": t, "exp": _tok["exp"], "base": base}), "utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(TOKEN_FILE)
    except OSError:
        pass
    return t, base


def kiwoom_post(path: str, api_id: str, body: dict, timeout: float = 15, headers: dict | None = None):
    """키움 REST 한 번. 토큰 거부(HTTP 401 또는 본문 return_code 8005 계열)면 **재발급 후 한 번 더**.
    (resp, json) 을 돌려준다. 호출처는 return_code 를 스스로 판정한다."""
    for attempt in (0, 1):
        t, base = token()
        r = _client().post(f"{base}{path}", headers={"Content-Type": "application/json;charset=UTF-8",
                                                     "authorization": f"Bearer {t}", "api-id": api_id,
                                                     **(headers or {})}, json=body, timeout=timeout)
        try:
            j = r.json()
        except ValueError:
            j = {}
        if attempt == 0 and auth_rejected(j, r.status_code):
            print(f"  ↻ 키움 토큰 거부({str(j.get('return_msg'))[:50]}) → 재발급", file=sys.stderr)
            invalidate_token()
            continue
        return r, j
    return r, j


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"last": {}, "first": {}, "last_run": None, "failed": []}


def save_state(st: dict) -> None:
    META.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE)


def universe() -> pd.DataFrame:
    if not UNIVERSE.exists():
        sys.exit(f"유니버스 없음: {UNIVERSE}")
    return pd.read_csv(UNIVERSE, dtype={"Code": str})


def path_of(code: str) -> Path:
    return FLOW / f"{code}.csv.gz"


def fetch(code: str, pages: int, dt: str | None = None, sleep: float = PACE,
          until: str | None = None):
    t, base = token()
    rows, cont, nk = [], "N", ""
    dt = dt or time.strftime("%Y%m%d")
    for page in range(pages):
        if page:
            time.sleep(sleep)             # 다음 페이지를 받을 때만 쉰다
        # 429·5xx·네트워크 오류에 지수 백오프. 429 는 키 단위 한도를 넘을 때 온다
        # 12시간짜리 장시간 실행에서는 재시도가 없으면 한 번의 스파이크로 전체가 죽는다.
        r = None
        for attempt in range(6):
            limited = False
            try:
                r = _client().post(f"{base}/api/dostk/stkinfo",
                               headers={"Content-Type": "application/json;charset=UTF-8",
                                        "authorization": f"Bearer {t}", "api-id": "ka10059",
                                        "cont-yn": cont, "next-key": nk},
                               json={"dt": dt, "stk_cd": code, "amt_qty_tp": "2",
                                     "trde_tp": "0", "unit_tp": "1"}, timeout=30)
                try:
                    jb = r.json()
                except ValueError:
                    jb = {}
                if auth_rejected(jb, r.status_code):           # 토큰 거부 → 재발급 후 재시도
                    invalidate_token()
                    t, base = token()
                    raise RuntimeError(f"토큰 거부({str(jb.get('return_msg') or r.status_code)[:40]}) 재인증")
                limited = r.status_code == 429
                if limited or r.status_code >= 500:
                    raise RuntimeError(f"HTTP {r.status_code}")
                r.raise_for_status()
                break
            except Exception as e:               # noqa: BLE001
                if attempt == 5:
                    raise
                if limited:                      # 한도 초과는 금방 풀린다 — 짧게 쉬고, 횟수만 진행 줄에 낸다
                    _limited["n"] += 1
                    time.sleep(RETRY_429 * 2 ** attempt)
                    continue
                wait = min(60, 2 ** attempt * 1.5)
                print(f"    ↻ {code} 재시도 {attempt+1}/5 ({repr(e)[:40]}) {wait:.0f}s", file=sys.stderr)
                time.sleep(wait)
        b = r.json().get("stk_invsr_orgn") or []
        rows += b
        cont, nk = r.headers.get("cont-yn", "N"), r.headers.get("next-key", "")
        if cont != "Y" or not b:
            break
    seen, out = set(), []
    for x in rows:
        d = x.get("dt")
        if not d or d in seen:
            continue
        # 장중 실행이면 당일 행은 전 항목 0 이 아닌 '부분 체결' 로 와서 아래 필터를 통과한다
        # (실측 2026-09-18 삼성전자: 거래량 313만·개인 0·전 항목 천주 반올림).
        # 다음 거래일 실행이 100일을 다시 받아 덮어쓰므로 영구 오염은 아니지만,
        # 그 사이에는 확정치인 척 남는다. --until 로 애초에 안 받는다.
        if until and d > until:
            continue
        if all(str(x.get(f, "0")).strip() in ("0", "") for f in FIELDS[5:]):
            continue                      # 장중 미확정(전 항목 0)
        seen.add(d)
        out.append([str(x.get(k, "0")).replace("+", "").strip() or "0" for k in FIELDS])
    if not out:
        # 응답은 정상인데 모든 행의 거래량이 0 = 거래정지 종목이다(실측 2026-09-29 금양 001570:
        # return_code 0 · 100행 · acc_trde_qty 전부 0). 실패로 세면 state 의 failed 에 71종목이
        # 상주해 진짜 실패가 묻힌다(mkt.py 의 SKIP 과 같은 이유). 빈 프레임으로 구분해 돌려준다.
        # 판정은 until 창 안의 행으로만 한다. 창 밖 당일 행에 거래가 있어도(정리매매 첫날 —
        # 실측 2026-09-29 부산주공 005030) 창 안이 전부 0 이면 그 기간은 거래정지였다.
        win = [x for x in rows if x.get("dt") and not (until and x["dt"] > until)]
        if win and all(str(x.get("acc_trde_qty", "0")).strip().lstrip("+-") in ("0", "")
                       for x in win):
            return pd.DataFrame(columns=OUT)
        return None
    d = pd.DataFrame(out, columns=OUT)
    for c in OUT[1:]:
        d[c] = pd.to_numeric(d[c], errors="coerce").fillna(0).astype("int64")
    return d.sort_values("dt").reset_index(drop=True)


def write(code: str, d: pd.DataFrame, append: bool):
    """종목 파일을 쓰고 (첫날, 마지막날, **이번에 새로 들어온 행**) 을 돌려준다.

    1페이지(=100일) 를 받아 99일은 버리고 하루만 남는 게 보통이다. 그 하루를 따로
    돌려주는 이유는 날짜 파일(daily.py)에 그것만 남기기 위해서다.
    """
    FLOW.mkdir(parents=True, exist_ok=True)
    p = path_of(code)
    fresh = d
    if append and p.exists():
        old = pd.read_csv(p, dtype={"dt": str})
        fresh = d[~d.astype({"dt": str}).dt.isin(set(old.dt.astype(str)))]
        d = pd.concat([old, d.astype({"dt": str})])
        d = d.drop_duplicates("dt", keep="last").sort_values("dt").reset_index(drop=True)
    with gzip.open(p, "wt", encoding="utf-8", compresslevel=6) as f:
        d.to_csv(f, index=False)
    return str(d.dt.iloc[0]), str(d.dt.iloc[-1]), fresh


def run(codes, pages, append, sleep, label, until=None):
    st = load_state()
    ok = fail = halt = 0
    t0 = time.time()
    writer = _daily.DailyWriter("flow", ROOT / "raw", date_col="dt")
    for i, c in enumerate(codes, 1):
        t_req = time.monotonic()
        try:
            d = fetch(c, pages, sleep=sleep, until=until)
            if d is None:
                raise RuntimeError("빈 응답")
            if d.empty:                   # 거래정지 — 실패가 아니다. 파일·최종일은 그대로 둔다
                halt += 1
            else:
                first, last, fresh = write(c, d, append)
                writer.add(c, fresh)
                st["first"][c], st["last"][c] = first, last
                ok += 1
            if c in st["failed"]:
                st["failed"].remove(c)
        except Exception as e:  # noqa: BLE001
            fail += 1
            if c not in st["failed"]:
                st["failed"].append(c)
            print(f"  ✗ {c} {repr(e)[:60]}", file=sys.stderr)
        if i % 25 == 0 or i == len(codes):
            el = time.time() - t0
            eta = el / i * (len(codes) - i)
            print(f"  {label} {i}/{len(codes)}  성공 {ok} 거래없음 {halt} 실패 {fail} 한도재시도 {_limited['n']}  "
                  f"경과 {el/60:.0f}분 남은 {eta/60:.0f}분", file=sys.stderr)
        if i % 25 == 0:
            st["last_run"] = datetime.now().isoformat(timespec="seconds")
            save_state(st)
        time.sleep(max(0.0, sleep - (time.monotonic() - t_req)))
    st["last_run"] = datetime.now().isoformat(timespec="seconds")
    save_state(st)
    print(_daily.report(writer.flush(), ROOT / "raw", "flow"), file=sys.stderr)
    return ok, fail, time.time() - t0


def cmd_backfill(a):
    uni = universe()
    codes = [c.strip() for c in a.only.split(",")] if a.only else list(uni.Code)
    st = load_state()
    if not a.force:
        codes = [c for c in codes if c not in st["last"] or not path_of(c).exists()]
    est = len(codes) * a.pages * a.sleep / 3600
    print(f"■ 수급 백필 {len(codes):,}종목 × {a.pages}페이지 · 예상 {est:.1f}시간")
    if not codes:
        print("  이미 전부 받아져 있습니다. --force 로 강제.")
        return
    ok, fail, el = run(codes, a.pages, False, a.sleep, "backfill", getattr(a, "until", None))
    print(f"■ 완료 성공 {ok:,} 실패 {fail} · {el/60:.0f}분")


def cmd_update(a):
    uni = universe()
    st = load_state()
    # 유니버스에서 빠진 종목(상폐 — mkt.refresh_universe 가 뺀다)은 실패 목록에서도 뺀다.
    # 안 빼면 다시 요청하지도 않는 종목이 '실패'로 영구히 남아 진짜 실패를 가린다.
    live = set(uni.Code)
    if any(c not in live for c in st["failed"]):
        st["failed"] = [c for c in st["failed"] if c in live]
        save_state(st)
    until = getattr(a, "until", None)
    cutoff = until or date.today().strftime("%Y%m%d")
    codes = [c for c in uni.Code
             if not path_of(c).exists() or st["last"].get(c, "") < cutoff]
    print(f"■ 수급 증분 {len(codes):,}종목 · 예상 {len(codes)*a.sleep/60:.0f}분")
    if not codes:
        print("  최신 상태입니다.")
        return
    ok, fail, el = run(codes, 1, True, a.sleep, "update", until)
    print(f"■ 완료 성공 {ok:,} 실패 {fail} · {el/60:.0f}분")


def cmd_status(a):
    uni = universe()
    st = load_state()
    files = list(FLOW.glob("*.csv.gz")) if FLOW.exists() else []
    size = sum(f.stat().st_size for f in files) / 1e6
    print(f"\n■ 수급 저장소 {FLOW}")
    print(f"  종목 {len(files):,} / 유니버스 {len(uni):,}  ({len(files)/max(len(uni),1)*100:.1f}%)")
    print(f"  용량 {size:,.0f}MB · 마지막 실행 {st.get('last_run')}")
    if st["last"]:
        s = pd.Series(st["last"])
        print("  최종일 분포:", " · ".join(f"{k} {v:,}" for k, v in s.value_counts().head(3).items()))
    if st["failed"]:
        print(f"  ★ 실패 {len(st['failed'])}: {st['failed'][:8]}")
    if files:
        import random
        for f in random.sample(files, min(3, len(files))):
            d = pd.read_csv(f, dtype={"dt": str})
            print(f"  샘플 {f.name[:6]}: {d.dt.iloc[0]}~{d.dt.iloc[-1]} {len(d):,}행")


def cmd_universe(a):
    uni = universe()
    print(f"■ 유니버스 {len(uni):,}종목  ({UNIVERSE})")
    for m, g in uni.groupby("Market"):
        print(f"  {m:<8} {len(g):>4}종목 · 시총 {g.marcap_eok.max():>10,.0f}억 ~ {g.marcap_eok.min():>8,.0f}억")
        print(f"           상위: {', '.join(g.nlargest(5,'marcap_eok').Name)}")


def main():
    ap = argparse.ArgumentParser(prog="flow", description="키움 투자자별 매매동향 raw")
    ap.add_argument("--sleep", type=float, default=PACE, help="요청 시작 간 간격(초)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backfill"); b.add_argument("--pages", type=int, default=14)
    b.add_argument("--only"); b.add_argument("--force", action="store_true")
    b.add_argument("--until", help="이 날짜(YYYYMMDD)까지만 저장. 장중 실행 시 필수")
    b.set_defaults(f=cmd_backfill)
    u = sub.add_parser("update")
    u.add_argument("--until", help="이 날짜(YYYYMMDD)까지만 저장. 장중 실행 시 필수")
    u.set_defaults(f=cmd_update)
    s = sub.add_parser("status"); s.set_defaults(f=cmd_status)
    v = sub.add_parser("universe"); v.set_defaults(f=cmd_universe)
    a = ap.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
