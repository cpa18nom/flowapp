"""관심 그룹 — 종목을 묶어 그 그룹만의 지수·수급을 만들고, 기존 차트(viewer)에 그대로 넣는다 (사용자 요청 2026-09-30).

차트는 새로 만들지 않는다. viewer 는 종목코드로 시세(core.stock_history)와 수급(flowtool.load)을 읽으므로,
그룹에 가상 코드를 주고 그 두 자리에 그룹 데이터를 등록하면(core.VIRTUAL_PRICE / VIRTUAL_FLOW) 종목과 똑같은 차트가 나온다.

그룹 지수 (동일가중, 차트 첫날 = 1000)
  각 종목의 그날 시가·고가·저가·종가를 **자기 전일 종가로 나눈 비율**을 내고, 그날 자료가 있는 종목끼리 평균한다.
    종가 = 전일 지수 × 평균(종가비) · 시가·고가·저가 = 전일 지수 × 평균(각 비율)
  종목마다 고점·저점 시각이 다르므로 지수의 고가·저가는 실제 장중 지수보다 넓게 잡힌다(근사). 종가는 정확하다.
  늦게 상장한 종목은 자료가 생긴 다음 날부터 평균에 들어온다(전일 종가가 있어야 비율이 나온다 — 편입으로 지수가 튀지 않는다).
그룹 수급
  종목 수급은 '주식 수'라 종목끼리 더할 수 없다. 주체별 순매수 수량 × 그날 종가 = **금액(백만원)** 으로 바꿔 더한다.
  그래서 그룹 차트의 매집·분산은 금액 기준이다. 분산비율 산식(core.disp)은 종목과 같다.
"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

import numpy as np
import pandas as pd

import core
import flowtool as ft

FILE = Path(os.environ.get("FLOWAPP_GROUPS_FILE", core.ROOT / "user" / "groups.json"))
BASE = 1000.0
MAX_GROUPS, MAX_MEMBERS, MAX_NAME = 50, 60, 40
FLOW_COLS = ["ind", "frgn", "orgn", "fnnc", "insrnc", "invtrt", "etcfnnc", "bank", "penfnd", "samo",
             "natn", "etccorp", "natfor"]
_LOCK = threading.Lock()
_CODE = re.compile(r"^[0-9A-Z]{6}$")


# ────────────────────────────── 저장

def load_all() -> list[dict]:
    try:
        data = json.loads(FILE.read_text(encoding="utf-8"))
        return [g for g in data.get("groups", []) if isinstance(g, dict) and "id" in g]
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save(groups: list[dict]) -> None:
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"groups": groups}, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(FILE)                       # 쓰다 죽어도 옛 파일이 남는다


def get(gid: str) -> dict | None:
    return next((g for g in load_all() if g["id"] == gid), None)


def _clean_name(name: str) -> str:
    name = " ".join(str(name).split())[:MAX_NAME]
    if not name:
        raise ValueError("그룹 이름을 적어 주세요.")
    return name


def create(name: str) -> str:
    name = _clean_name(name)
    with _LOCK:
        gs = load_all()
        if len(gs) >= MAX_GROUPS:
            raise ValueError(f"그룹은 {MAX_GROUPS}개까지 만들 수 있습니다.")
        n = 1 + max([int(g["id"][1:]) for g in gs if re.fullmatch(r"g\d+", g["id"])], default=0)
        gs.append({"id": f"g{n}", "name": name, "codes": []})
        _save(gs)
    return f"g{n}"


def _edit(gid: str, fn) -> None:
    with _LOCK:
        gs = load_all()
        g = next((x for x in gs if x["id"] == gid), None)
        if g is None:
            raise ValueError("없는 그룹입니다.")
        if fn(g, gs) is not False:
            _save(gs)


def rename(gid: str, name: str) -> None:
    name = _clean_name(name)
    _edit(gid, lambda g, gs: g.update(name=name))


def delete(gid: str) -> None:
    _edit(gid, lambda g, gs: gs.remove(g))


def add(gid: str, code: str) -> None:
    code = str(code).strip().upper()
    if not _CODE.match(code) or not (core.PRICE / f"{code}.csv.gz").exists():
        raise ValueError("시세 자료가 없는 종목코드입니다.")

    def fn(g, gs):
        if code in g["codes"]:
            return False
        if len(g["codes"]) >= MAX_MEMBERS:
            raise ValueError(f"한 그룹에는 {MAX_MEMBERS}종목까지 담을 수 있습니다.")
        g["codes"].append(code)
    _edit(gid, fn)


def remove(gid: str, code: str) -> None:
    _edit(gid, lambda g, gs: g["codes"].remove(code) if code in g["codes"] else False)


# ────────────────────────────── 지수

def _price(code: str) -> pd.DataFrame | None:
    p = core.PRICE / f"{code}.csv.gz"
    if not p.exists():
        return None
    d = pd.read_csv(p, usecols=["Date", "o", "h", "l", "c", "v"], parse_dates=["Date"]).set_index("Date")
    return d[(d.c > 0) & (d.o > 0)]


def index_ohlc(codes: list[str]) -> pd.DataFrame | None:
    """동일가중 그룹 지수의 open·high·low·close·volume(거래대금 합, 백만원). 자료가 없으면 None."""
    ro, rh, rl, rc, val = {}, {}, {}, {}, {}
    for code in codes:
        d = _price(code)
        if d is None or len(d) < 2:
            continue
        pc = d.c.shift(1)
        ro[code], rh[code], rl[code], rc[code] = d.o / pc, d.h / pc, d.l / pc, d.c / pc
        val[code] = d.c * d.v / 1e6
    if not rc:
        return None
    RC = pd.DataFrame(rc).mean(axis=1, skipna=True).dropna()
    RO, RH, RL = (pd.DataFrame(x).mean(axis=1, skipna=True).reindex(RC.index) for x in (ro, rh, rl))
    close = BASE * RC.cumprod()
    prev = close.shift(1).fillna(BASE)
    out = pd.DataFrame({"open": prev * RO, "high": prev * RH, "low": prev * RL, "close": close,
                        "volume": pd.DataFrame(val).sum(axis=1, skipna=True).reindex(RC.index).fillna(0.0)})
    out["high"] = out[["open", "high", "close"]].max(axis=1)      # 평균 때문에 고가<종가가 되는 날을 막는다
    out["low"] = out[["open", "low", "close"]].min(axis=1)
    out.index.name = "dt"
    return out


def group_flow(codes: list[str], idx: pd.DataFrame) -> pd.DataFrame | None:
    """flowtool.load 와 같은 모양의 그룹 수급. 주체별 순매수 **금액(백만원)** 의 합, px = 그룹 지수."""
    parts = []
    for code in codes:
        d = ft.load(code)
        if d is None or d.empty:
            continue
        cols = [c for c in FLOW_COLS if c in d]
        m = d[cols].mul(d.px, axis=0) / 1e6
        m["vol"] = d.vol * d.px / 1e6
        parts.append(m)
    if not parts:
        return None
    s = pd.concat(parts).groupby(level=0).sum().sort_index()
    s = s[s.index.isin(idx.index)]
    for c in FLOW_COLS:
        if c not in s:
            s[c] = 0.0
    s["px"] = idx.close.reindex(s.index)
    s["px_raw"], s["pred_pre"], s["flu_rt"] = s.px, 0.0, 0.0
    s.index.name = "dt"
    return s


def vcode(gid: str) -> str:
    """차트에 넘길 가상 종목코드. 실제 코드(6자리)와 겹치지 않는다."""
    return f"GRP-{gid}"


def register(gid: str, codes: list[str]) -> tuple[str, pd.DataFrame | None, pd.DataFrame | None]:
    """그룹의 지수·수급을 만들어 가상 코드로 등록한다. (가상코드, 지수, 수급)"""
    vc = vcode(gid)
    idx = index_ohlc(codes)
    flow = group_flow(codes, idx) if idx is not None else None
    if flow is not None and len(flow):
        # 차트는 수급이 시작되는 날부터 그린다 — 그 첫날이 1,000 이 되도록 다시 맞춘다(모양·수익률은 그대로)
        k = BASE / float(idx.close.loc[flow.index[0]])
        idx[["open", "high", "low", "close"]] *= k
        flow["px"], flow["px_raw"] = flow.px * k, flow.px_raw * k
    if idx is not None:
        core.VIRTUAL_PRICE[vc] = idx
    else:
        core.VIRTUAL_PRICE.pop(vc, None)
    if flow is not None:
        core.VIRTUAL_FLOW[vc] = flow
    else:
        core.VIRTUAL_FLOW.pop(vc, None)
    return vc, idx, flow


def returns(close: pd.Series, spans=(1, 20, 60)) -> dict:
    """마지막 값 기준 n 거래일 수익률(%) — 자료가 모자라면 NaN."""
    out = {}
    for n in spans:
        out[n] = (close.iloc[-1] / close.iloc[-1 - n] - 1) * 100 if len(close) > n else float("nan")
    return out


def members(codes: list[str]) -> list[dict]:
    uni = core.universe().set_index("Code")
    rows = []
    for code in codes:
        d = _price(code)
        name = str(uni.Name.get(code, code)) if code in uni.index else code
        if d is None or d.empty:
            rows.append({"code": code, "name": name, "close": np.nan, "ret": {1: np.nan, 20: np.nan, 60: np.nan}})
            continue
        rows.append({"code": code, "name": name, "close": float(d.c.iloc[-1]), "ret": returns(d.c)})
    return rows
