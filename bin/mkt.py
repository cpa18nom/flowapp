#!/usr/bin/env python3
"""market — 로컬 시세 raw 저장소.

  mkt backfill [--workers 6] [--only 005930,000660]   전종목 전이력 내려받기(재개 가능)
  mkt update   [--workers 6] [--until Y-M-D] [--from Y-M-D]   증분(--from 은 재방문 교정)
  mkt verify [--fix] [--only 코드,…]                   저장분을 전 이력 재수집본과 대조(수정주가 빠진 옛 봉)
  mkt status                                          커버리지·최종일 점검
  mkt read <코드> [--from YYYY-MM-DD]                 한 종목 읽어 출력(파이프용)

저장 구조
  ~/market/raw/price/{code}.csv.gz   Date,o,h,l,c,v
  ~/market/raw/meta/listing.csv      Code,Name,Market,ListingDate,Sector,Industry
  ~/market/raw/meta/state.json       {code: 최종일}, last_run, failed

설계 메모
  - FDR은 종목당 3,000행이 상한이라 실질 최장 이력은 2014-06 무렵이다.
  - 지표 워밍업(MA480 등)에 2년이 필요하므로 백테스트는 2016-07 이후를 권한다.
  - 현재 상장 종목만 받으므로 **생존편향이 있다**. 상폐 종목은 들어오지 않는다.
    상장일을 메타에 남겨 시점별 유니버스를 재구성해 look-ahead 만큼은 막는다.
  - OneDrive에 두지 않는다(소파일 2,900개 동기화는 느리다). PC 로컬 전용.
"""
from __future__ import annotations

import argparse
import gzip
import functools
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import daily as _daily                                   # noqa: E402

ROOT = Path(os.environ.get("MARKET_HOME", Path.home() / "market"))

# FDR 은 requests 를 timeout 없이 부른다. 응답 없는 연결 하나에 워커가 영원히 묶여 update 가
# 끝나지 않았다(실측 2026-09-23 2600/2788 에서 5분+ 정지, ESTAB 소켓 1개). 걸린 종목은 실패로
# 세어져 다음 실행에서 다시 받는다. socket.setdefaulttimeout 은 **안 먹는다** — requests 가
# timeout=None 을 명시적으로 넘겨 전역 기본값을 덮는다(실측). 그래서 Session.request 를 감싼다.
NET_TIMEOUT = float(os.environ.get("MKT_NET_TIMEOUT", "30"))


def _install_net_timeout():
    import requests
    orig = requests.Session.request
    if getattr(orig, "_mkt_timeout", False):
        return

    def request(self, *a, **kw):
        if kw.get("timeout") is None:
            kw["timeout"] = NET_TIMEOUT
        return orig(self, *a, **kw)
    request._mkt_timeout = True
    requests.Session.request = request


_install_net_timeout()
PRICE = ROOT / "raw" / "price"
META = ROOT / "raw" / "meta"
STATE = META / "state.json"
LISTING = META / "listing.csv"
UNIVERSE = META / "universe.csv"          # 수급 수집·스캐너 패널의 종목 집합
FLOW = ROOT / "raw" / "flow"
UNIVERSE_MARKETS = ("KOSPI", "KOSDAQ", "KOSDAQ GLOBAL")
COLS = ["o", "h", "l", "c", "v"]
SKIP = "__no_new_data__"          # 실패가 아니라 "새 데이터 없음"


def _fdr():
    import warnings
    warnings.filterwarnings("ignore")
    import FinanceDataReader as fdr
    return fdr


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"last": {}, "last_run": None, "failed": []}


def save_state(st: dict) -> None:
    META.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE)


@functools.lru_cache(maxsize=1)
def _krx_live() -> pd.DataFrame:
    """KRX 실시간 목록(코드·이름·시장·시총). KRX-DESC 와 달리 GitHub 캐시를 거치지 않는다."""
    d = _fdr().StockListing("KRX")
    d = d[d.Code.astype(str).str.fullmatch(r"[0-9A-Z]{6}")].drop_duplicates("Code")
    return d


def refresh_listing() -> pd.DataFrame:
    """상장목록 갱신. KRX-DESC → (실패 시) KRX 실시간 + 기존 메타 → (둘 다 실패 시) 로컬.

    ★ KRX-DESC 는 GitHub 캐시의 당일치 CSV 를 받는데, 2026-09-16 부터 13일째 404 였다
      (실측 2026-09-29). '하루이틀 묵어도 된다'던 가정이 깨져 그 뒤 신규상장(네오사피엔스 등)이
      시세·수급에서 통째로 빠졌다. 실시간 KRX 목록으로 대체하고, 거기 없는 상장일·업종은
      기존 파일에서 이어받는다(신규 종목은 공란)."""
    META.mkdir(parents=True, exist_ok=True)
    cols = ["Code", "Name", "Market", "ListingDate", "Sector", "Industry"]
    try:
        d = _fdr().StockListing("KRX-DESC")[cols]
    except Exception as e:  # noqa: BLE001
        try:
            live = _krx_live()
        except Exception as e2:  # noqa: BLE001
            if LISTING.exists():
                print(f"  \u26a0 상장목록 갱신 실패(DESC {repr(e)[:40]} · KRX {repr(e2)[:40]})"
                      f" → 로컬 {LISTING.name} 사용", file=sys.stderr)
                return pd.read_csv(LISTING, dtype={"Code": str})
            raise
        old = (pd.read_csv(LISTING, dtype={"Code": str}).set_index("Code")
               if LISTING.exists() else pd.DataFrame(columns=cols[3:]))
        d = live[["Code", "Name", "Market"]].copy()
        for c in cols[3:]:
            d[c] = d.Code.map(old[c]) if c in old else None
        print(f"  \u26a0 KRX-DESC 실패({repr(e)[:40]}) → KRX 실시간 목록 {len(d):,}종목으로 대체",
              file=sys.stderr)
    # 2024년부터 신규상장은 영문 섞인 코드(0161M0 네오사피엔스)를 받는다. \d{6} 만 받으면
    # 보통주 28종목(삼성에피스홀딩스 등)이 통째로 빠졌다(실측 2026-09-29).
    d = d[d.Code.str.fullmatch(r"[0-9A-Z]{6}")].drop_duplicates("Code")
    d.to_csv(LISTING, index=False, encoding="utf-8")
    return d


def universe_rule(lst: pd.DataFrame) -> pd.DataFrame:
    """유니버스 = KOSPI·KOSDAQ 보통주. 우선주(코드 끝자리≠0)·스팩·리츠 제외.
    (2026-09-14 에 만든 universe.csv 의 규칙을 역산한 것 — 그날 목록에 적용하면 2,555종목이
    정확히 재현된다. tests/test_market.py::test_유니버스_구성 이 같은 규칙을 검사한다.)"""
    m = lst[lst.Market.isin(UNIVERSE_MARKETS)]
    m = m[m.Code.str.endswith("0") & ~m.Name.str.contains("스팩|리츠", na=False)]
    return m.drop_duplicates("Code")


def refresh_universe(lst: pd.DataFrame, marcap: pd.Series | None = None,
                     min_keep: float = 0.9) -> dict:
    """상장목록에서 유니버스를 다시 만든다. 빠진 종목(상폐)의 수급 파일은 flow/_delisted/ 로 옮긴다.

    목록이 비정상적으로 작으면(기존의 90% 미만) 덮어쓰지 않는다 — 소스가 일부만 준 날
    유니버스가 반토막 나고 수급 파일이 대량 이동되는 사고를 막는다."""
    new = universe_rule(lst)[["Code", "Name", "Market"]].copy()
    old = pd.read_csv(UNIVERSE, dtype={"Code": str}) if UNIVERSE.exists() else None
    if old is not None and len(new) < min_keep * len(old):
        print(f"  \u26a0 유니버스 갱신 보류: 새 목록 {len(new):,} < 기존 {len(old):,}의 "
              f"{min_keep:.0%}", file=sys.stderr)
        return {"added": [], "removed": [], "kept": True}
    prev_cap = old.set_index("Code").marcap_eok if old is not None and "marcap_eok" in old else None
    cap = new.Code.map(marcap) if marcap is not None else pd.Series(float("nan"), index=new.index)
    if prev_cap is not None:
        cap = cap.fillna(new.Code.map(prev_cap))      # 장전·휴장엔 실시간 시총이 비어 온다
    new["marcap_eok"] = cap
    new = new.sort_values("marcap_eok", ascending=False, na_position="last")
    added = sorted(set(new.Code) - set(old.Code)) if old is not None else list(new.Code)
    removed = sorted(set(old.Code) - set(new.Code)) if old is not None else []
    tmp = UNIVERSE.with_suffix(".tmp")
    new.to_csv(tmp, index=False, encoding="utf-8")
    tmp.replace(UNIVERSE)
    gone = FLOW / "_delisted"
    for c in removed:                                  # 지우지 않는다 — 옮겨 둔다
        f = FLOW / f"{c}.csv.gz"
        if f.exists():
            gone.mkdir(parents=True, exist_ok=True)
            f.replace(gone / f.name)
    if added or removed:
        name = dict(zip(pd.concat([new.Code, old.Code if old is not None else new.Code]),
                        pd.concat([new.Name, old.Name if old is not None else new.Name])))
        print(f"  유니버스 {len(new):,}종목 · 편입 {len(added)} {[name[c] for c in added][:8]}"
              f" · 제외 {len(removed)} {[name[c] for c in removed][:8]}", file=sys.stderr)
    return {"added": added, "removed": removed, "kept": False}


def _live_marcap_eok() -> pd.Series | None:
    try:
        live = _krx_live()
    except Exception:  # noqa: BLE001
        return None
    return (pd.to_numeric(live.set_index("Code").Marcap, errors="coerce") / 1e8)


def listing() -> pd.DataFrame:
    if not LISTING.exists():
        return refresh_listing()
    return pd.read_csv(LISTING, dtype={"Code": str})


def path_of(code: str) -> Path:
    return PRICE / f"{code}.csv.gz"


def read_one(code: str, since: str | None = None) -> pd.DataFrame | None:
    p = path_of(code)
    if not p.exists():
        return None
    d = pd.read_csv(p, parse_dates=["Date"], dtype={c: "float64" for c in COLS})
    if since:
        d = d[d.Date >= pd.Timestamp(since)]
    return d.set_index("Date")


def _fetch(code: str, start: str | None, until: str | None = None):
    fdr = _fdr()
    d = fdr.DataReader(code, start) if start else fdr.DataReader(code)
    if d is None or len(d) == 0:
        return None
    # 장중에 돌리면 FDR 이 오늘의 미확정 봉을 준다. 다음 실행은 last+1 부터 받으므로
    # 그 봉은 다시 방문되지 않는다 = 영구 오염. --until 로 잘라 막는다.
    if until:
        d = d[d.index <= pd.Timestamp(until)]
        if len(d) == 0:
            return None
    d = d.rename(columns={"Open": "o", "High": "h", "Low": "l", "Close": "c", "Volume": "v"})
    d = d[[c for c in COLS if c in d.columns]].dropna(how="all")
    d = d[d.c > 0]
    # ── 정합성 정리 ──
    # (1) o/h/l 중 하나라도 0인 행은 '봉이 없는 날'(거래정지·결손)이다.
    #     종가만 있는 행을 남기면 ATR·기준봉·매물대가 통째로 오염된다. 제거한다.
    d = d[(d.o > 0) & (d.h > 0) & (d.l > 0)]
    # (2) 수정주가 반올림으로 c 나 o 가 고저를 1~2원 넘는 경우가 있다(실측 0.2%).
    #     봉을 버릴 사안이 아니므로 고저를 넓혀 맞춘다.
    d["h"] = d[["h", "o", "c"]].max(axis=1)
    d["l"] = d[["l", "o", "c"]].min(axis=1)
    return d if len(d) else None


ADJ_TOL = 0.0005       # 겹치는 날 종가가 이보다 크게(그리고 1원 넘게) 다르면 수정주가가 바뀐 것으로 본다
ADJ_OVERLAP_DAYS = 10  # 증분을 받을 때 저장분 마지막 날에서 이만큼 앞부터 받는다 — 겹치는 봉이 있어야 비교할 수 있다
ADJ_BACKUP = ROOT / "raw" / "_backup_adj"


def readjusted(old: pd.DataFrame, new: pd.DataFrame) -> bool:
    """새로 받은 봉(new)이 저장분(old)과 **겹치는 가장 이른 날**의 종가가 다른가 = 과거 봉이 다시 수정됐는가.

    액면병합·무상증자·권리락이 생기면 FDR 은 그 전의 봉 전체를 새 비율로 다시 준다. 증분은 뒤에만 붙이므로
    옛 봉이 예전 비율로 남아 병합일에 가짜 급등락이 생긴다(실측 2026-10-05: 28종목, 유진로봇 ×5 등).
    가장 이른 겹침만 본다 — 장중에 굳은 미확정 봉은 저장분의 **끝**에 있고, 그것은 재방문이 덮어쓰면 된다."""
    both = old.index.intersection(new.index)
    if not len(both):
        return False
    day = both.min()
    a, b = float(old.c[day]), float(new.c[day])
    return abs(a - b) > max(1.0, ADJ_TOL * a)


def splice(old: pd.DataFrame, full: pd.DataFrame) -> pd.DataFrame:
    """전 이력을 다시 받은 것(full)으로 바꾸되, full 이 시작하기 전의 옛 봉은 이음매 비율로 맞춰 남긴다.

    FDR 은 종목당 3,000행쯤이 상한이고 이전상장 종목은 그 뒤만 준다(유비온 084440) — 통째로 덮으면 앞 이력이 사라진다.
    비율 = 겹치는 첫날의 (새 종가 ÷ 옛 종가). 가격에는 곱하고 거래량은 나눈다(5:1 병합이면 가격 ×5, 거래량 ÷5)."""
    head = old[old.index < full.index[0]]
    both = old.index.intersection(full.index)
    if not len(head) or not len(both):
        return full
    day = both.min()
    r = float(full.c[day]) / float(old.c[day])
    head = head.copy()
    for c in ("o", "h", "l", "c"):
        head[c] = (head[c] * r).round()
    head["v"] = (head["v"] / r).round()
    return pd.concat([head, full[COLS]]).sort_index()


def _backup(code: str) -> None:
    """덮어쓰기 전 원본을 날짜 폴더에 남긴다(같은 날 이미 있으면 처음 것을 지킨다)."""
    src = path_of(code)
    dst = ADJ_BACKUP / date.today().strftime("%Y%m%d") / src.name
    if src.exists() and not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src.read_bytes())


def _write(code: str, d: pd.DataFrame, append: bool):
    """종목 파일을 쓰고 **이번에 새로 들어온 행**을 돌려준다.

    gz 는 부분 수정이 안 돼 통째로 다시 쓴다(그래서 append=True 여도 파일 전체가 바뀐다).
    새 행을 따로 돌려주는 이유는 날짜 파일(daily.py)에 그것만 남기기 위해서다.
    """
    PRICE.mkdir(parents=True, exist_ok=True)
    p = path_of(code)
    fresh = d
    if append and p.exists():
        old = pd.read_csv(p, parse_dates=["Date"]).set_index("Date")
        fresh = d[~d.index.isin(old.index)]          # 옛 파일에 없던 날짜만
        d = pd.concat([old, d])
        d = d[~d.index.duplicated(keep="last")].sort_index()
    out = d.reset_index().rename(columns={"index": "Date"})
    with gzip.open(p, "wt", encoding="utf-8", compresslevel=6) as f:
        out.to_csv(f, index=False)
    return len(d), fresh.reset_index().rename(columns={"index": "Date"})


def _job(code: str, start: str | None, append: bool, until: str | None = None,
         writer=None):
    """반환 (코드, 마지막 날, 오류, 수정주가 재수집 여부)."""
    try:
        d = _fetch(code, start, until)
        if d is None:
            # 빈 응답은 실패가 아니라 새 데이터가 없다는 뜻이다. 주말·휴장일에 돌리면 전 종목이
            # 여기로 온다(실측 2026-09-20 일요일 2,788종목). 실패로 세면 state.json 의 failed 에
            # 전 종목이 쌓여 **진짜 실패가 묻힌다.**
            return code, None, SKIP, False
        redo = False
        if append and start and path_of(code).exists():
            old = read_one(code)
            if old is not None and len(old) and readjusted(old, d):
                # 과거 봉이 다시 수정됐다 — 뒤에 붙이면 이음매가 생긴다. 전 이력을 다시 받아 바꾼다.
                full = _fetch(code, None, until)
                if full is None:
                    return code, None, "수정주가 재수집 실패(빈 응답)", False
                _backup(code)
                merged = splice(old, full)
                _write(code, merged, False)
                fresh = merged[~merged.index.isin(old.index)]      # 날짜 파일(운반본)에는 새 날짜만
                if writer is not None:
                    writer.add(code, fresh.reset_index().rename(columns={"index": "Date"}))
                return code, str(merged.index[-1].date()), None, True
        _, fresh = _write(code, d, append)
        if writer is not None:
            writer.add(code, fresh)
        return code, str(d.index[-1].date()), None, redo
    except Exception as e:  # noqa: BLE001
        return code, None, repr(e)[:80], False


def run(codes, start_of, append, workers, label, until=None):
    st = load_state()
    done = fail = skip = 0
    redone = []
    t0 = time.time()
    writer = _daily.DailyWriter("price", ROOT / "raw", date_col="Date")
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_job, c, start_of(c), append, until, writer): c for c in codes}
        for i, fu in enumerate(as_completed(futs), 1):
            code, last, err, redo = fu.result()
            if redo:
                redone.append(code)
            if err == SKIP:
                skip += 1
            elif err:
                fail += 1
                if code not in st["failed"]:
                    st["failed"].append(code)
            else:
                done += 1
                st["last"][code] = last
                if code in st["failed"]:
                    st["failed"].remove(code)
            if i % 200 == 0 or i == len(futs):
                el = time.time() - t0
                print(f"  {label} {i}/{len(futs)}  성공 {done} 새데이터없음 {skip} 실패 {fail}"
                      f"  {el:.0f}s", file=sys.stderr)
            if i % 500 == 0:
                st["last_run"] = datetime.now().isoformat(timespec="seconds")
                save_state(st)
    st["last_run"] = datetime.now().isoformat(timespec="seconds")
    if redone:
        # 과거 봉이 다시 수정된 종목 — 전 이력을 다시 받아 바꿨다(원본은 raw/_backup_adj/날짜/)
        st.setdefault("readjusted", []).append({"at": st["last_run"], "codes": sorted(redone)})
        st["readjusted"] = st["readjusted"][-50:]
        print(f"  수정주가 재수집 {len(redone)}종목 {sorted(redone)[:12]}{' …' if len(redone) > 12 else ''}"
              f" · 원본 {ADJ_BACKUP.name}/{date.today():%Y%m%d}/", file=sys.stderr)
    save_state(st)
    print(_daily.report(writer.flush(), ROOT / "raw", "price"), file=sys.stderr)
    if skip:
        print(f"  새 데이터 없음 {skip:,}종목 (휴장일이면 정상)", file=sys.stderr)
    return done, fail, time.time() - t0


def cmd_backfill(a):
    lst = refresh_listing()
    codes = [c.strip() for c in a.only.split(",")] if a.only else list(lst.Code)
    st = load_state()
    if not a.force:
        codes = [c for c in codes if c not in st["last"] or not path_of(c).exists()]
    print(f"■ 백필 대상 {len(codes):,}종목 (전체 {len(lst):,}) · workers {a.workers}")
    if not codes:
        print("  이미 전부 받아져 있습니다. --force 로 강제 재수집.")
        return
    done, fail, el = run(codes, lambda c: None, False, a.workers, "backfill", getattr(a, "until", None))
    print(f"■ 완료 성공 {done:,} 실패 {fail} · {el/60:.1f}분")
    cmd_status(a)


def cmd_update(a):
    lst = refresh_listing()
    refresh_universe(lst, _live_marcap_eok())
    st = load_state()
    until = getattr(a, "until", None)
    today = date.fromisoformat(until) if until else date.today()
    codes, starts = [], {}
    frm = getattr(a, "from_", None)
    for c in lst.Code:
        if not path_of(c).exists():
            codes.append(c); starts[c] = None; continue
        last = st["last"].get(c)
        # 저장분과 **겹치게** 받는다 — 겹치는 봉의 종가가 달라졌으면 수정주가가 바뀐 것이다(readjusted).
        # 병합으로 몇 주 거래정지였던 종목은 재방문 창(--from) 안에 저장분이 없을 수 있어 종목별 마지막 날에서 다시 잡는다.
        back = ((datetime.fromisoformat(last).date() - timedelta(days=ADJ_OVERLAP_DAYS)).isoformat()
                if last else None)
        if frm:
            # 재방문. 장중에 받힌 미확정 봉은 last+1 규칙 때문에 다시 방문되지 않아
            # 영구히 굳는다(실측 2026-09-14~16 종가·거래량 오염). 이 옵션이 유일한 교정 수단이다.
            codes.append(c); starts[c] = min(frm, back) if back else frm; continue
        if not last:
            codes.append(c); starts[c] = None; continue
        nxt = datetime.fromisoformat(last).date() + timedelta(days=1)
        if nxt <= today:
            codes.append(c); starts[c] = back
    print(f"■ 증분 대상 {len(codes):,}종목 · "
          f"{frm + ' ~ ' if frm else ''}기준일 {today} · workers {a.workers}")
    if not codes:
        print("  최신 상태입니다.")
        return
    done, fail, el = run(codes, lambda c: starts.get(c), True, a.workers, "update", until)
    print(f"■ 완료 성공 {done:,} 실패 {fail} · {el/60:.1f}분")
    cmd_status(a)


def _verify_one(code: str, fix: bool):
    """저장분 전체를 새로 받은 전 이력과 대조. 반환 (코드, 다른 봉 수, 첫 다른 날, 그날 새÷옛 비율, 고쳤는가, 오류)."""
    try:
        old = read_one(code)
        if old is None or not len(old):
            return code, 0, None, None, False, None
        full = _fetch(code, None, str(old.index[-1].date()))      # 저장분 마지막 날까지만 — 새 날짜를 붙이지 않는다
        if full is None:
            return code, 0, None, None, False, "빈 응답"
        both = old.index.intersection(full.index)
        a, b = old.c[both].astype(float), full.c[both].astype(float)
        bad = (a - b).abs() > (ADJ_TOL * a).clip(lower=1.0)
        if not bad.any():
            return code, 0, None, None, False, None
        day = bad[bad].index[0]
        fixed = False
        seen = old[old.index >= full.index[0]]
        if fix and len(full) >= 0.9 * len(seen):                   # 일부만 온 응답으로 이력을 줄이지 않는다
            _backup(code)
            _write(code, splice(old, full), False)
            fixed = True
        return code, int(bad.sum()), str(day.date()), float(b[day] / a[day]), fixed, None
    except Exception as e:  # noqa: BLE001
        return code, 0, None, None, False, repr(e)[:80]


def cmd_verify(a):
    """전 종목의 저장분을 전 이력 재수집본과 대조한다(수정주가가 빠진 옛 봉 찾기). --fix 면 바꾼다.

    update 의 자동 감지는 **이번에 겹친 봉**만 본다 — 감지 장치를 넣기 전에 이미 이음매가 굳은 종목은 이 명령으로만 찾는다."""
    codes = ([c.strip() for c in a.only.split(",")] if a.only
             else sorted(f.name.split(".")[0] for f in PRICE.glob("*.csv.gz")))
    print(f"■ 대조 대상 {len(codes):,}종목 · workers {a.workers}{' · 고침' if a.fix else ' · 보기만'}")
    t0, hits, errs = time.time(), [], []
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for i, r in enumerate(ex.map(lambda c: _verify_one(c, a.fix), codes), 1):
            if r[5]:
                errs.append(r)
            elif r[1]:
                hits.append(r)
            if i % 400 == 0 or i == len(codes):
                print(f"  verify {i}/{len(codes)}  다름 {len(hits)} 오류 {len(errs)}  {time.time() - t0:.0f}s",
                      file=sys.stderr)
    for code, n, day, ratio, fixed, _ in sorted(hits, key=lambda r: -r[1]):
        print(f"  {code}: 다른 봉 {n:,} · 첫 {day} · 새÷옛 {ratio:.4f}{' · 고침' if fixed else ''}")
    if errs:
        print(f"  ★ 오류 {len(errs)}종목(--only 로 다시): {','.join(r[0] for r in errs)} · 예 {errs[0][5]}")
    print(f"■ 완료 다름 {len(hits)} · 고침 {sum(r[4] for r in hits)} · 오류 {len(errs)} · {(time.time() - t0) / 60:.1f}분")
    if a.fix and hits:
        st = load_state()
        st.setdefault("readjusted", []).append({"at": datetime.now().isoformat(timespec="seconds"),
                                                "codes": sorted(r[0] for r in hits if r[4])})
        save_state(st)


def cmd_status(a):
    lst = listing()
    st = load_state()
    files = list(PRICE.glob("*.csv.gz")) if PRICE.exists() else []
    size = sum(f.stat().st_size for f in files) / 1e6
    lasts = pd.Series({k: v for k, v in st["last"].items() if v})
    print(f"\n■ 저장소 {ROOT}")
    print(f"  종목 파일 {len(files):,} / 상장 {len(lst):,}  ({len(files)/max(len(lst),1)*100:.1f}%)")
    print(f"  용량 {size:,.0f}MB · 마지막 실행 {st.get('last_run')}")
    if len(lasts):
        vc = lasts.value_counts().sort_index(ascending=False).head(4)
        print("  최종일 분포:", " · ".join(f"{k} {v:,}종목" for k, v in vc.items()))
    if st["failed"]:
        print(f"  ★ 실패 {len(st['failed'])}종목: {st['failed'][:8]}{' …' if len(st['failed'])>8 else ''}")
    if files:
        import random
        s = random.sample(files, min(3, len(files)))
        for f in s:
            d = pd.read_csv(f, parse_dates=["Date"])
            print(f"  샘플 {f.name.split(chr(46))[0]}: {d.Date.min().date()}~{d.Date.max().date()} {len(d):,}행")


def cmd_read(a):
    d = read_one(a.code.zfill(6), a.__dict__.get("from_"))
    if d is None:
        sys.exit(f"없음: {a.code}")
    print(d.to_csv())


def main():
    ap = argparse.ArgumentParser(prog="mkt", description="로컬 시세 raw 저장소")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backfill"); b.add_argument("--workers", type=int, default=6)
    b.add_argument("--only"); b.add_argument("--force", action="store_true")
    b.add_argument("--until", help="이 날짜까지만 저장(YYYY-MM-DD). 장중 실행 시 필수")
    b.set_defaults(f=cmd_backfill)
    u = sub.add_parser("update"); u.add_argument("--workers", type=int, default=6)
    u.add_argument("--until", help="이 날짜까지만 저장(YYYY-MM-DD). 장중 실행 시 필수")
    u.add_argument("--from", dest="from_",
                   help="저장일 무시하고 이 날짜부터 다시 받아 덮어쓴다(미확정 봉 교정용)")
    u.set_defaults(f=cmd_update)
    s = sub.add_parser("status"); s.set_defaults(f=cmd_status)
    v = sub.add_parser("verify"); v.add_argument("--workers", type=int, default=6)
    v.add_argument("--only"); v.add_argument("--fix", action="store_true", help="다른 종목을 전 이력으로 바꾼다(원본은 _backup_adj)")
    v.set_defaults(f=cmd_verify)
    r = sub.add_parser("read"); r.add_argument("code"); r.add_argument("--from", dest="from_")
    r.set_defaults(f=cmd_read)
    a = ap.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
