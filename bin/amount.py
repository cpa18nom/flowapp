"""거래대금 저장소 — 키움 일봉차트(ka10081)의 **실제 거래대금**(trde_prica, 백만원)을 종목별로 받아 둔다 (사용자 요청 2026-10-07).

왜  차트의 「거래대금(억)」은 종가×거래량 근사였다(FDR 일봉에 거래대금 열이 없다). 키움은 체결마다 가격×수량을 더한 값이라
    급등·급락일에 1~5% 어긋났다(솔루엠 10/1 +4.6%). 이 파일이 있으면 viewer 가 이 값을 쓴다(없는 날은 종가×거래량으로 메운다).
저장  ~/market/raw/amount/{code}.csv.gz    Date(YYYY-MM-DD), amt(원, 백만원×1e6), v(키움 거래량 — 대조용)
     금액은 액면분할·증자에 불변이라 FDR 시세가 다시 수정돼도 그대로 쓴다. 시세 파일과 분리해 둔 이유.
속도  ka10081 한 쪽 = 약 600 거래일. 증분(update)은 종목당 1쪽, 백필은 끝까지(2014~ ≈ 5쪽). 요청 간격은 min5 와 같은 규칙(flow.PACE).

    python amount.py backfill [--only 005930,...] [--force]    # 전 이력
    python amount.py update   [--only ...]                     # 최신 1쪽을 받아 덮어쓴다(겹치는 날은 새 값)
    python amount.py status
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import flow  # noqa: E402

DIR = flow.ROOT / "raw" / "amount"
STATE = flow.META / "amount_state.json"
MAX_PAGES = 12            # 백필 쪽 수 상한(600일/쪽 → 12쪽 ≈ 29년)
COLS = ["Date", "amt", "v"]
_limited = {"n": 0}
_last_req = {"t": 0.0}


def _num(x) -> int:
    s = str(x or "").replace(",", "").replace("+", "").replace("-", "").strip()
    return int(float(s)) if s else 0


def parse(rows: list[dict]) -> list[tuple]:
    """ka10081 행 → (Date, amt원, v). 거래대금은 백만원 단위로 온다(실측 2026-10-07: 삼성전자 10/6 '3535154' = 3조5,352억)."""
    out = []
    for r in rows:
        dt = str(r.get("dt") or "")
        if len(dt) != 8:
            continue
        out.append((f"{dt[:4]}-{dt[4:6]}-{dt[6:]}", _num(r.get("trde_prica")) * 1_000_000, _num(r.get("trde_qty"))))
    return out


def _page(code: str, hdr: dict | None, base_dt: str):
    for attempt in range(6):
        try:
            _last_req["t"] = time.monotonic()
            r, j = flow.kiwoom_post("/api/dostk/chart", "ka10081",
                                    {"stk_cd": code, "base_dt": base_dt, "upd_stkpc_tp": "1"},
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
                if "초과" in msg or "제한" in msg:
                    _limited["n"] += 1
                    time.sleep(flow.RETRY_429 * 2 ** attempt)
                    continue
                raise RuntimeError(f"return_code {rc} {msg[:40]}")
            rows = j.get("stk_dt_pole_chart_qry") or []
            nxt = ({"cont-yn": "Y", "next-key": r.headers.get("next-key", "")}
                   if r.headers.get("cont-yn") == "Y" and rows else None)
            return rows, nxt
        except RuntimeError:
            raise
        except Exception as e:                                   # noqa: BLE001
            if attempt == 5:
                raise
            time.sleep(min(30, 1.5 * 2 ** attempt))
    raise RuntimeError("유량 초과가 풀리지 않습니다")


def _pace(pace: float) -> None:
    time.sleep(max(0.0, pace - (time.monotonic() - _last_req["t"])))


def fetch(code: str, pages: int, pace: float = flow.PACE) -> pd.DataFrame | None:
    """최신 → 과거로 pages 쪽까지. 행이 없으면 None."""
    got, hdr = [], None
    base_dt = datetime.now().strftime("%Y%m%d")
    for _ in range(pages):
        _pace(pace)
        rows, hdr = _page(code, hdr, base_dt)
        got += parse(rows)
        if hdr is None:
            break
    if not got:
        return None
    d = pd.DataFrame(got, columns=COLS).drop_duplicates("Date", keep="first").sort_values("Date").reset_index(drop=True)
    # 장중에 받으면 오늘 행은 미완성 거래대금이다 — 15:40 전에는 빼고 저장한다(다음 갱신이 확정값을 덮어쓴다)
    now = datetime.now()
    if now.strftime("%H%M") < "1540":
        d = d[d.Date != now.strftime("%Y-%m-%d")].reset_index(drop=True)
    return d if len(d) else None


def path_of(code: str) -> Path:
    return DIR / f"{code}.csv.gz"


def read(code: str) -> pd.DataFrame | None:
    p = path_of(code)
    if not p.exists():
        return None
    return pd.read_csv(p, dtype={"Date": str})


def write(code: str, d: pd.DataFrame) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = path_of(code).with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as f:
        d.to_csv(f, index=False)
    tmp.replace(path_of(code))


def merge(old: pd.DataFrame | None, new: pd.DataFrame) -> pd.DataFrame:
    """겹치는 날은 새 값. 옛 파일에만 있는 날은 남긴다."""
    if old is None or old.empty:
        return new
    keep = old[~old.Date.isin(set(new.Date))]
    return pd.concat([keep, new], ignore_index=True).sort_values("Date").reset_index(drop=True)


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"done": {}, "failed": [], "empty": []}


def save_state(st: dict) -> None:
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE)


def run(codes: list[str], pages: int, pace: float, label: str) -> tuple[int, int, int]:
    st = load_state()
    ok = fail = empty = 0
    t0 = time.time()
    with ThreadPoolExecutor(1) as writer:
        pending: list = []
        for i, c in enumerate(codes, 1):
            try:
                d = fetch(c, pages, pace)
                if d is None:
                    empty += 1
                    if c not in st["empty"]:
                        st["empty"].append(c)
                else:
                    d = merge(read(c), d)
                    pending.append(writer.submit(write, c, d))
                    st["done"][c] = [str(d.Date.iloc[0]), str(d.Date.iloc[-1]), int(len(d))]
                    ok += 1
                if c in st["failed"]:
                    st["failed"].remove(c)
            except Exception as e:                                   # noqa: BLE001
                fail += 1
                if c not in st["failed"]:
                    st["failed"].append(c)
                print(f"  ✗ {c} {repr(e)[:70]}", file=sys.stderr, flush=True)
            if i % 25 == 0 or i == len(codes):
                for f in pending:
                    f.result()
                pending.clear()
                el = time.time() - t0
                print(f"  {label} {i}/{len(codes)}  성공 {ok} 없음 {empty} 실패 {fail} 한도재시도 {_limited['n']}  "
                      f"경과 {el / 60:.1f}분 남은 {el / i * (len(codes) - i) / 60:.1f}분 · {i / el:.1f}종목/초", file=sys.stderr, flush=True)
                st["last_run"] = datetime.now().isoformat(timespec="seconds")
                save_state(st)
    return ok, fail, empty


def cmd_backfill(a) -> None:
    codes = [c.strip() for c in a.only.split(",")] if a.only else list(flow.universe().Code)
    st = load_state()
    if not a.force:
        codes = [c for c in codes if not (c in st["done"] and path_of(c).exists()) and c not in st["empty"]]
    print(f"■ 거래대금 백필 대상 {len(codes):,}종목 · 간격 {a.sleep}초 · 종목당 최대 {MAX_PAGES}쪽", flush=True)
    ok, fail, empty = run(codes, MAX_PAGES, a.sleep, "amount")
    print(f"■ 완료 성공 {ok:,} 없음 {empty} 실패 {fail}", flush=True)


def cmd_update(a) -> None:
    if a.only_file:                                                  # flowapp 이 넘기는 유동 종목 목록(한 줄에 한 코드)
        codes = [c.strip() for c in Path(a.only_file).read_text(encoding="utf-8").split() if c.strip()]
    else:
        codes = [c.strip() for c in a.only.split(",")] if a.only else list(flow.universe().Code)
    print(f"■ 거래대금 증분 대상 {len(codes):,}종목 (종목당 1쪽 ≈ 600일)", flush=True)
    ok, fail, empty = run(codes, 1, a.sleep, "amount update")
    print(f"■ 완료 성공 {ok:,} 없음 {empty} 실패 {fail}", flush=True)


def cmd_status(a) -> None:
    st = load_state()
    fs = list(DIR.glob("*.csv.gz")) if DIR.exists() else []
    size = sum(f.stat().st_size for f in fs) / 1e6
    done = st["done"]
    print(f"■ 거래대금 {len(fs):,}종목 · {size:,.1f}MB · 마지막 실행 {st.get('last_run')}")
    if done:
        first = pd.Series([v[0][:7] for v in done.values()]).value_counts().sort_index().head(3)
        last = pd.Series([v[1] for v in done.values()]).value_counts().sort_index(ascending=False).head(3)
        print("  시작월:", " · ".join(f"{k} {v:,}종목" for k, v in first.items()))
        print("  마지막:", " · ".join(f"{k} {v:,}종목" for k, v in last.items()))
    if st["failed"]:
        print(f"  ★ 실패 {len(st['failed'])}종목: {st['failed'][:10]}")
    if st["empty"]:
        print(f"  없음 {len(st['empty'])}종목")


def main() -> None:
    ap = argparse.ArgumentParser(prog="amount", description="키움 일봉 거래대금 저장소")
    ap.add_argument("--sleep", type=float, default=flow.PACE, help="요청 시작 간 간격(초)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backfill"); b.add_argument("--only"); b.add_argument("--force", action="store_true")
    b.set_defaults(f=cmd_backfill)
    u = sub.add_parser("update"); u.add_argument("--only"); u.add_argument("--only-file"); u.set_defaults(f=cmd_update)
    s = sub.add_parser("status"); s.set_defaults(f=cmd_status)
    a = ap.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
