#!/usr/bin/env python3
"""5분봉 저장소 — 키움 ka10080(주식분봉차트) 으로 종목별 5분봉을 받아 둔다 (사용자 요청 2026-10-06).

  min5 backfill [--only 005930,000660] [--force]     유니버스 전 종목, 키움이 주는 끝(약 1년)까지. 재개 가능
  min5 update                                         저장분 마지막 봉 뒤만 이어 받는다
  min5 status                                         종목 수·기간·용량

저장  ~/market/raw/min5/{code}.csv.gz    t(YYYYMMDDHHMM, 봉 시작 시각),o,h,l,c,v — 수정주가(upd_stkpc_tp=1)
      정규장 밖(시간외·대체거래소 ~20:00) 봉도 **그대로 둔다** — 거르는 것은 읽는 쪽이 한다(09:00~15:30).
실측  (2026-10-06 삼성전자) 한 번에 900봉 · 22회로 2025-10-01 09:00 까지 = 키움이 주는 끝(1년). 60분봉도 같은 날까지.
      1분봉은 60회로 2026-03-19 까지(더 이어진다). 상장폐지 종목은 빈 응답.
속도  수급 수집과 같은 간격(flow.PACE, 초당 7건)으로 2,557종목 × 22회 ≈ 2시간. 429 는 짧게 쉬고 다시 보낸다.
      간격은 **요청 시작 시각 기준**이고 gz 쓰기는 뒷 스레드가 한다 — 처음(10/6)엔 응답·저장이 끝난 뒤에 쉬어서
      429 가 0 인데도 3.3종목/초(증분 12분)밖에 안 나왔다. 응답 ≈0.1초 + 저장 ≈0.05초가 간격에 더해졌던 것.
주의  장중에 받으면 마지막 봉이 미완성이다 — update 가 저장분의 **마지막 봉부터** 다시 받아 덮어쓴다.
"""
from __future__ import annotations

import argparse
import gzip
from concurrent.futures import ThreadPoolExecutor
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import flow  # noqa: E402

DIR = flow.ROOT / "raw" / "min5"
STATE = flow.META / "min5_state.json"
SCOPE = 5
MAX_PAGES = 60            # 한 종목에서 넘기는 쪽 수 상한(1년 = 22쪽). 끝없이 도는 것을 막는다
COLS = ["t", "o", "h", "l", "c", "v"]
_limited = {"n": 0}
_last_req = {"t": 0.0}    # 마지막 요청을 **보낸** 시각(monotonic) — 간격은 여기서부터 잰다


def _num(x) -> int:
    """'+71200' · '-71200' · '71200' → 71200 (키움은 부호로 등락을 싣는다)."""
    s = str(x or "").replace(",", "").replace("+", "").replace("-", "").strip()
    return int(float(s)) if s else 0


def parse(rows: list[dict]) -> list[tuple]:
    out = []
    for r in rows:
        tm = str(r.get("cntr_tm") or "")
        if len(tm) < 12:
            continue
        o, h, l, c = (_num(r.get(k)) for k in ("open_pric", "high_pric", "low_pric", "cur_prc"))
        if not (o and h and l and c):
            continue
        out.append((tm[:12], o, h, l, c, _num(r.get("trde_qty"))))
    return out


def _page(code: str, hdr: dict | None):
    """한 쪽. 429·5xx·네트워크 오류는 지수 백오프로 다시 보낸다. 반환 (행, 다음 쪽 헤더|None)."""
    for attempt in range(6):
        try:
            _last_req["t"] = time.monotonic()
            r, j = flow.kiwoom_post("/api/dostk/chart", "ka10080",
                                    {"stk_cd": code, "tic_scope": str(SCOPE), "upd_stkpc_tp": "1"},
                                    timeout=30, headers=hdr)
            if r.status_code == 429:
                _limited["n"] += 1
                time.sleep(flow.RETRY_429 * 2 ** attempt)
                continue
            if r.status_code >= 500:
                raise RuntimeError(f"HTTP {r.status_code}")
            rc = j.get("return_code")
            if rc not in (0, "0", None):
                msg = str(j.get("return_msg") or "")
                if "초과" in msg or "제한" in msg:              # 본문으로 오는 유량 초과
                    _limited["n"] += 1
                    time.sleep(flow.RETRY_429 * 2 ** attempt)
                    continue
                raise RuntimeError(f"return_code {rc} {msg[:40]}")
            rows = j.get("stk_min_pole_chart_qry") or []
            nxt = ({"cont-yn": "Y", "next-key": r.headers.get("next-key", "")}
                   if r.headers.get("cont-yn") == "Y" and rows else None)
            return rows, nxt
        except RuntimeError:
            raise
        except Exception as e:                                   # noqa: BLE001 — 네트워크
            if attempt == 5:
                raise
            time.sleep(min(30, 1.5 * 2 ** attempt))
    raise RuntimeError("유량 초과가 풀리지 않습니다")


def fetch(code: str, stop_at: str | None = None, pace: float = flow.PACE) -> pd.DataFrame | None:
    """최신 → 과거로 넘기며 받는다. stop_at(YYYYMMDDHHMM) 이하의 봉이 나오면 그 쪽에서 멈춘다. 봉이 없으면 None."""
    got, hdr = [], None
    for page in range(MAX_PAGES):
        _pace(pace)
        rows, hdr = _page(code, hdr)
        bars = parse(rows)
        got += bars
        if hdr is None or (stop_at and bars and min(b[0] for b in bars) <= stop_at):
            break
    if not got:
        return None
    d = pd.DataFrame(got, columns=COLS).drop_duplicates("t", keep="first").sort_values("t").reset_index(drop=True)
    return d


def _pace(pace: float) -> None:
    """다음 요청을 보내기 전에 — 직전 요청을 보낸 뒤 pace 초가 안 지났으면 남은 만큼만 쉰다."""
    time.sleep(max(0.0, pace - (time.monotonic() - _last_req["t"])))


def path_of(code: str) -> Path:
    return DIR / f"{code}.csv.gz"


def read(code: str) -> pd.DataFrame | None:
    p = path_of(code)
    if not p.exists():
        return None
    return pd.read_csv(p, dtype={"t": str})


def write(code: str, d: pd.DataFrame) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = path_of(code).with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as f:
        d.to_csv(f, index=False)
    tmp.replace(path_of(code))


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"done": {}, "failed": [], "empty": []}


def save_state(st: dict) -> None:
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE)


def run(codes: list[str], update: bool, pace: float, label: str) -> tuple[int, int, int]:
    """종목을 차례로 받는다. 요청은 pace 간격으로 **쉬지 않고** 나가고, 받은 것을 gz 로 쓰는 일은 뒷 스레드 하나가 한다
    (쓰기 ≈0.05초/종목이 요청 사이에 끼면 그만큼 느려진다 — 실측 2026-10-06)."""
    st = load_state()
    ok = fail = empty = 0
    t0 = time.time()
    with ThreadPoolExecutor(1) as writer:
        pending: list = []
        for i, c in enumerate(codes, 1):
            try:
                old = read(c) if update else None
                stop = str(old.t.iloc[-1]) if old is not None and len(old) else None
                d = fetch(c, stop, pace)
                if d is None:
                    empty += 1
                    if c not in st["empty"]:
                        st["empty"].append(c)
                else:
                    if old is not None and len(old):
                        d = pd.concat([old[old.t < d.t.iloc[0]], d], ignore_index=True)     # 겹치는 구간은 새로 받은 것으로(마지막 봉이 미완성이었을 수 있다)
                    pending.append(writer.submit(write, c, d))
                    st["done"][c] = [str(d.t.iloc[0]), str(d.t.iloc[-1]), int(len(d))]
                    ok += 1
                if c in st["failed"]:
                    st["failed"].remove(c)
            except Exception as e:                                   # noqa: BLE001
                fail += 1
                if c not in st["failed"]:
                    st["failed"].append(c)
                print(f"  ✗ {c} {repr(e)[:70]}", file=sys.stderr, flush=True)
            if i % 25 == 0 or i == len(codes):
                for f in pending:                                    # 상태 파일은 실제로 저장된 것만 가리켜야 한다
                    f.result()
                pending.clear()
                el = time.time() - t0
                print(f"  {label} {i}/{len(codes)}  성공 {ok} 봉없음 {empty} 실패 {fail} 한도재시도 {_limited['n']}  "
                      f"경과 {el / 60:.1f}분 남은 {el / i * (len(codes) - i) / 60:.1f}분 · {i / el:.1f}종목/초", file=sys.stderr, flush=True)
                st["last_run"] = datetime.now().isoformat(timespec="seconds")
                save_state(st)
    return ok, fail, empty


def cmd_backfill(a) -> None:
    codes = [c.strip() for c in a.only.split(",")] if a.only else list(flow.universe().Code)
    st = load_state()
    if not a.force:
        codes = [c for c in codes if not (c in st["done"] and path_of(c).exists()) and c not in st["empty"]]
    print(f"■ 5분봉 백필 대상 {len(codes):,}종목 · 간격 {a.sleep}초", flush=True)
    ok, fail, empty = run(codes, False, a.sleep, "min5")
    print(f"■ 완료 성공 {ok:,} 봉없음 {empty} 실패 {fail}", flush=True)


def cmd_update(a) -> None:
    codes = [c.strip() for c in a.only.split(",")] if a.only else list(flow.universe().Code)
    print(f"■ 5분봉 증분 대상 {len(codes):,}종목", flush=True)
    ok, fail, empty = run(codes, True, a.sleep, "min5 update")
    print(f"■ 완료 성공 {ok:,} 봉없음 {empty} 실패 {fail}", flush=True)


def cmd_status(a) -> None:
    st = load_state()
    fs = list(DIR.glob("*.csv.gz")) if DIR.exists() else []
    size = sum(f.stat().st_size for f in fs) / 1e6
    done = st["done"]
    print(f"■ 5분봉 {len(fs):,}종목 · {size:,.0f}MB · 마지막 실행 {st.get('last_run')}")
    if done:
        first = pd.Series([v[0][:8] for v in done.values()]).value_counts().sort_index().head(3)
        last = pd.Series([v[1][:8] for v in done.values()]).value_counts().sort_index(ascending=False).head(3)
        print("  시작일:", " · ".join(f"{k} {v:,}종목" for k, v in first.items()))
        print("  마지막:", " · ".join(f"{k} {v:,}종목" for k, v in last.items()))
        print(f"  봉 수 합 {sum(v[2] for v in done.values()):,}")
    if st["failed"]:
        print(f"  ★ 실패 {len(st['failed'])}종목: {st['failed'][:10]}")
    if st["empty"]:
        print(f"  봉 없음 {len(st['empty'])}종목(거래정지·신규 등)")


def main() -> None:
    ap = argparse.ArgumentParser(prog="min5", description="키움 5분봉 저장소")
    ap.add_argument("--sleep", type=float, default=flow.PACE, help="요청 시작 간 간격(초)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backfill"); b.add_argument("--only"); b.add_argument("--force", action="store_true")
    b.set_defaults(f=cmd_backfill)
    u = sub.add_parser("update"); u.add_argument("--only"); u.set_defaults(f=cmd_update)
    s = sub.add_parser("status"); s.set_defaults(f=cmd_status)
    a = ap.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
