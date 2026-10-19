"""시간별 추천 — 장중 매시간 검색식을 돌려 걸린 종목 가운데 **규칙으로 1개**를 골라 알림 카드로 쌓는다 (사용자 요청 2026-10-06, 지인 공유용).

  고르는 규칙   ★ 검색식(flowapp.PICK_KINDS)에 오늘 걸린 종목 → 확률 모델(winmodel) 백분위가 가장 높은 종목. 백분위가 PICK_MIN 미만이면 그 시간은 「추천 없음」.
                같은 날 같은 종목은 한 번만. 수급 조건(개인 분산 ≤30%)은 화면 기본값대로 켠다.
  시각          09:30 뒤 매 정시(10·11·12·13·14·15시) + 15:41 마감 확정(장중 잠정 신호가 종가로도 남았는지 적는다). 「추천 시작」을 눌렀을 때만 돈다.
  카드          매수타점 · 보유전략 · 매수근거 · 주의. 전부 이 저장소에서 검정한 수치로만 쓴다(strategy·strategy_port·winmodel·confirm·minute_study).
  저장          ~/.local/state/flowapp/picks.json — 카드 목록(최근 60일). 텔레그램 전송은 선택(watch.send).

이 모듈은 flowapp 을 모른다. 후보 뽑기(candidates)·오늘 봉 받기(refresh)·보내기(send)를 밖에서 받는다 — 테스트는 가짜로 돈다.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, time as dtime
from pathlib import Path

STATE = Path(os.environ.get("FLOWAPP_PICKS_STATE", Path.home() / ".local" / "state" / "flowapp" / "picks.json"))
HOURS = (10, 11, 12, 13, 14, 15)          # 매 정시(그 시각의 오늘 봉으로)
FINAL = dtime(15, 41)                      # 종가 확정 뒤 한 번 더 — flowapp.LIVE_FINAL(15:40) 뒤여야 받은 봉이 "확정"으로 온다(10/6 15:35 로 돌렸더니 확정 표시가 안 적혔다)
OPEN = dtime(9, 30)
PICK_MIN = 50                              # 모델 백분위 하한(과거 같은 검색식 신호의 중앙 이상) — 아래면 추천하지 않는다
KEEP_DAYS = 60
LOG_MAX = 40


class Picker:
    """candidates(day) -> list[dict]  오늘 걸린 종목(점수·지표 포함, flowapp.pick_candidates)
       refresh(now)    -> (day|None, final, at_text, note)   오늘 봉을 받아 붙인다(flowapp.watch_refresh)
       send(text)      텔레그램(선택)"""

    def __init__(self, candidates, refresh, send=None, state_path: Path | None = None, now=datetime.now, sleep=time.sleep, prices=None,
                 on_final=None):
        self.candidates, self.refresh, self.send, self.now, self.sleep = candidates, refresh, send, now, sleep
        self.on_final = on_final                             # 마감(15:41) 추천 뒤 한 번 부른다 — 오늘의 3종목(top3) 자동 기록용(2026-10-07)
        self.prices = prices                                 # prices(day, codes) -> {코드: (종가, 신호 유지)} — 마감 확정에 쓴다(없으면 안 적는다)
        self.path = state_path or STATE
        self.lock = threading.RLock()
        self.running, self.stop_flag, self.thread = False, threading.Event(), None
        self.log: list[str] = []
        self.state = {"picks": [], "telegram": False, "done": {}}        # done: {날짜: [시각 표식…]}
        self._load()

    def _load(self) -> None:
        try:
            s = json.loads(self.path.read_text(encoding="utf-8"))
            for k in self.state:
                if k in s and isinstance(s[k], type(self.state[k])):
                    self.state[k] = s[k]
        except (OSError, ValueError):
            pass

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        tmp.replace(self.path)

    def _note(self, msg: str) -> None:
        with self.lock:
            self.log.append(f"{self.now():%m-%d %H:%M} {msg}")
            del self.log[:-LOG_MAX]

    def picks(self, days: int = KEEP_DAYS) -> list[dict]:
        with self.lock:
            return [dict(p) for p in reversed(self.state["picks"])][: 500]

    def set_telegram(self, on: bool) -> None:
        with self.lock:
            self.state["telegram"] = bool(on)
            self._save()

    # ── 한 번 고르기
    def pick(self, slot: str | None = None, day=None, final: bool | None = None, at: str = "") -> dict:
        """지금 시각의 추천 하나. slot 은 '10시' 같은 표식(같은 날 같은 표식은 한 번만, None 이면 수동 — 늘 돈다).
        반환: {'day','slot','pick'(카드|None),'note'}."""
        now = self.now()
        note = ""
        if day is None:
            day, final, at, note = self.refresh(now)
        out = {"day": None if day is None else f"{day:%Y-%m-%d}", "slot": slot, "pick": None, "note": note}
        if day is None:
            self._note(f"{slot or '수동'} · 오늘 시세 없음 — {note or '휴장이거나 장 시작 전'}")
            return out
        key = out["day"]
        with self.lock:
            done = self.state["done"].setdefault(key, [])
            if slot and slot in done:
                out["note"] = "이미 뽑은 시각"
                return out
            taken = {p["code"] for p in self.state["picks"] if p["day"] == key}
        try:
            cands = [c for c in self.candidates(day) if c["code"] not in taken]
        except Exception as e:                               # noqa: BLE001
            self._note(f"{slot or '수동'} · 후보를 못 뽑았다 — {repr(e)[:80]}")
            out["note"] = f"후보를 못 뽑았다 — {repr(e)[:80]}"
            return out
        cands = [c for c in cands if c.get("pct") is not None]
        cands.sort(key=lambda c: (c["pct"], len(c.get("kinds", []))), reverse=True)
        card = None
        if cands and cands[0]["pct"] >= PICK_MIN:
            c = cands[0]
            card = {**c, "day": key, "slot": slot or "수동", "at": at or f"{now:%H:%M}", "final": bool(final),
                    "picked_at": f"{now:%Y-%m-%d %H:%M}", "status": "종가 확정" if final else "장중 잠정",
                    "others": [f"{x['name']}({x['pct']:.0f})" for x in cands[1:4]]}
        with self.lock:
            if slot:
                done.append(slot)
            if card:
                self.state["picks"].append(card)
                cutoff = f"{(now.date() - __import__('datetime').timedelta(days=KEEP_DAYS)):%Y-%m-%d}"
                self.state["picks"] = [p for p in self.state["picks"] if p["day"] >= cutoff]
                for k in [k for k in self.state["done"] if k < cutoff]:
                    self.state["done"].pop(k, None)
            self._save()
        out["pick"] = card
        if (final or slot == "마감") and self.prices:        # 종가 확정 봉(또는 마감 슬롯)이면 오늘 장중 카드에 남았는지 적는다
            try:
                with self.lock:
                    codes = [p["code"] for p in self.state["picks"] if p["day"] == key and p.get("status") == "장중 잠정"]
                if codes:
                    self.confirm(day, self.prices(day, codes))
            except Exception as e:                           # noqa: BLE001
                self._note(f"마감 확정을 못 적었다 — {repr(e)[:80]}")
        if card:
            self._note(f"{card['slot']} · {card['name']} {card['code']} · 백분위 {card['pct']:.0f} · 후보 {len(cands)}")
            if self.send and self.state["telegram"]:
                try:
                    self.send(render_text(card))
                except Exception as e:                       # noqa: BLE001
                    self._note(f"텔레그램 발신 실패 — {repr(e)[:80]}")
        else:
            self._note(f"{slot or '수동'} · 추천 없음(후보 {len(cands)}, 최고 백분위 {cands[0]['pct']:.0f})" if cands else f"{slot or '수동'} · 오늘 걸린 종목 없음")
        return out

    def confirm(self, day, prices: dict) -> int:
        """마감 뒤: 그날 장중 잠정 카드에 종가 확정 여부를 적는다. prices: {코드: (종가, 신호가 유지 여부)}. 반환: 갱신 수."""
        key = f"{day:%Y-%m-%d}"
        n = 0
        with self.lock:
            for p in self.state["picks"]:
                if p["day"] == key and p["code"] in prices and p.get("status") != "종가 확정":
                    close, kept = prices[p["code"]]
                    p["close"] = close
                    p["status"] = "종가 확정" if kept else "마감 때 신호 소멸"
                    n += 1
            self._save()
        return n

    # ── 스레드
    def start(self) -> bool:
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.stop_flag.clear()
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()
            return True

    def stop(self) -> None:
        self.stop_flag.set()

    def _wait(self, seconds: float) -> None:
        end = time.time() + seconds
        while not self.stop_flag.is_set() and time.time() < end:
            self.sleep(min(1.0, max(0.0, end - time.time())))

    def _run(self) -> None:
        self._note("추천 시작 · 매 정시 + 15:41 확정")
        try:
            while not self.stop_flag.is_set():
                now = self.now()
                if now.weekday() >= 5:
                    self._note("주말 — 끝")
                    break
                t = now.time()
                if t >= FINAL:
                    r = self.pick("마감")
                    if r["day"] is None:
                        self._note("오늘 장이 없다 — 끝")
                    else:
                        if self.on_final:
                            try:
                                self._note(f"마감 뒤 작업 · {self.on_final()}")
                            except Exception as e:           # noqa: BLE001
                                self._note(f"⚠ 마감 뒤 작업 실패 — {repr(e)[:80]}")
                        self._note("마감 확정까지 끝 — 추천을 끝낸다")
                    break
                if t < OPEN:
                    self._wait(30)
                    continue
                due = [h for h in HOURS if t >= dtime(h, 0)]
                slot = f"{due[-1]}시" if due else None
                if slot:
                    self.pick(slot)
                # 다음 정시(또는 15:41)까지 기다린다
                nxt = next((dtime(h, 0) for h in HOURS if t < dtime(h, 0)), FINAL)
                if nxt < FINAL and nxt <= t:
                    nxt = FINAL
                target = datetime.combine(now.date(), min(nxt, FINAL) if due and due[-1] == 15 else nxt)
                self._wait(max(5.0, (target - now).total_seconds() + 1))
            else:
                self._note("추천 중지")
        except Exception as e:                               # noqa: BLE001
            self._note(f"⚠ 추천이 멈췄다 — {repr(e)[:120]}")
        finally:
            with self.lock:
                self.running = False

    def status(self) -> dict:
        with self.lock:
            return {"running": self.running, "telegram": self.state["telegram"], "n": len(self.state["picks"]), "log": list(self.log[-10:])}


# ────────────────────────────── 카드 글

def render_text(c: dict) -> str:
    """텔레그램용 평문(HTML)."""
    import html
    lines = [f"📌 <b>{html.escape(c['name'])}</b> {c['code']} · {c['day'][5:]} {c['at']} {c['status']}",
             f"현재가 {c['price']:,.0f}원 ({c['ret'] * 100:+.1f}%) · 걸린 검색식: {html.escape(' / '.join(c['kinds']))}",
             f"매수타점: {html.escape(c['entry'])}", f"보유전략: {html.escape(c['hold'])}",
             "매수근거: " + html.escape(" · ".join(c["why"])), "주의: " + html.escape(" · ".join(c["warn"]))]
    return "\n".join(lines)
