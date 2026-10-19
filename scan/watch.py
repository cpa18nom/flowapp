"""실시간 감시 — 고른 검색식에 **새로 걸린 종목**을 텔레그램으로 알린다 (사용자 요청 2026-10-05).

  동작      화면에서 「감시 시작」을 눌렀을 때만 돈다(상시 스케줄러가 아니다 — 서버를 다시 띄우면 꺼진 상태로 시작한다).
            장중(09:01~) 몇 분마다 전 종목 오늘 봉을 다시 받아(live.py · 약 24초) 감시 목록의 검색식을 돌리고,
            그날 아직 알리지 않은 종목만 보낸다. 15:40 뒤 종가 확정 봉으로 한 번 더 돌려 **마감 정리**를 보내고 스스로 끝난다.
  한계      · 장중 봉은 미완성이다 — 신고가·양봉 전환은 마감 때 사라질 수 있다. 알림에 「장중 잠정」을 붙이고, 마감 정리에 사라진 종목을 적는다.
            · 주기는 분 단위다(계산이 24초). 초 단위 체결 감시가 아니다.
            · 수급(개인 분산비율·외국인·기관)은 장 마감 뒤에 확정된다 — 장중에는 직전 거래일 값으로 판정한다(core.IND_DISP_FFILL).
            · 60분봉 검색식은 감시하지 않는다(종목별로 분봉을 새로 받는 다른 경로다).
  설정      텔레그램 봇 토큰·대화방 번호는 저장소 밖 `~/.config/flowapp/telegram.json`(권한 600) 또는 환경변수
            FLOWAPP_TG_TOKEN · FLOWAPP_TG_CHAT. **토큰은 화면·로그에 내지 않는다.**
  상태      감시 목록·주기·그날 알린 종목은 `~/.local/state/flowapp/watch.json` — 서버를 다시 띄워도 같은 종목을 두 번 알리지 않는다.

이 모듈은 flowapp 을 모른다. 검색(hits)·오늘 봉 받기(refresh)·보내기(send)를 밖에서 받는다 — 테스트는 그 셋을 가짜로 바꿔 돈다.
"""
from __future__ import annotations

import html
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, time as dtime
from pathlib import Path

CONFIG = Path(os.environ.get("FLOWAPP_TG_CONFIG", Path.home() / ".config" / "flowapp" / "telegram.json"))
STATE = Path(os.environ.get("FLOWAPP_WATCH_STATE", Path.home() / ".local" / "state" / "flowapp" / "watch.json"))
API = "https://api.telegram.org/bot{token}/{method}"
OPEN = dtime(9, 1)            # 이 시각 전에는 오늘 체결이 없다(flowapp.LIVE_OPEN)
FINAL = dtime(15, 40)         # 이 뒤에 받은 봉은 종가 확정(flowapp.LIVE_FINAL)
INTERVAL_MIN, INTERVAL_MAX, INTERVAL_DEFAULT = 2, 30, 5      # 분. 오늘 봉 계산이 24초라 2분 밑으로는 의미가 없다
MAX_ITEMS = 12                # 감시 목록 상한 — 검색식마다 주기당 한 번씩 돈다
MAX_LINES = 40                # 한 알림에 적는 종목 수(넘으면 '외 N종목')
TG_LIMIT = 3900               # 텔레그램 한 메시지 4,096자 — 여유를 둔다
KEEP_DAYS = 5                 # 알린 종목 기록을 며칠치 남길지
LOG_MAX = 60


class TgError(RuntimeError):
    """텔레그램 호출 실패. 메시지에 토큰이 들어가지 않는다."""


# ────────────────────────────── 텔레그램

def load_config() -> dict:
    """{'token','chat'} — 환경변수가 파일을 이긴다. 없으면 빈 문자열."""
    cfg = {"token": "", "chat": ""}
    try:
        cfg.update({k: str(v) for k, v in json.loads(CONFIG.read_text(encoding="utf-8")).items() if k in cfg})
    except (OSError, ValueError):
        pass
    cfg["token"] = os.environ.get("FLOWAPP_TG_TOKEN") or cfg["token"]
    cfg["chat"] = os.environ.get("FLOWAPP_TG_CHAT") or cfg["chat"]
    return cfg


def save_config(token: str | None = None, chat: str | None = None) -> None:
    """준 값만 바꿔 저장한다. 파일은 본인만 읽게(600) 둔다."""
    cfg = {"token": "", "chat": ""}
    try:
        cfg.update({k: str(v) for k, v in json.loads(CONFIG.read_text(encoding="utf-8")).items() if k in cfg})
    except (OSError, ValueError):
        pass
    if token is not None:
        cfg["token"] = token.strip()
    if chat is not None:
        cfg["chat"] = str(chat).strip()
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    os.chmod(tmp, 0o600)
    tmp.replace(CONFIG)


def valid_token(token: str) -> bool:
    """봇 토큰 모양(숫자:영숫자·_·-). 주소에 그대로 넣으므로 다른 글자는 받지 않는다."""
    import re
    return bool(re.fullmatch(r"\d{5,12}:[A-Za-z0-9_-]{30,60}", token.strip()))


def tg_call(token: str, method: str, params: dict | None = None, timeout: float = 15) -> dict:
    """텔레그램 Bot API 한 번. 실패는 TgError(설명만 — 주소·토큰은 싣지 않는다)."""
    if not valid_token(token):
        raise TgError("봇 토큰 모양이 아닙니다")
    data = urllib.parse.urlencode(params or {}).encode()
    req = urllib.request.Request(API.format(token=token.strip(), method=method), data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            j = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            desc = json.loads(e.read().decode("utf-8")).get("description", "")
        except Exception:                                        # noqa: BLE001
            desc = ""
        raise TgError(f"텔레그램 {e.code} {desc}".strip()) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise TgError(f"텔레그램에 닿지 못했습니다({type(e).__name__})") from None
    except ValueError:
        raise TgError("텔레그램 응답을 읽지 못했습니다") from None
    if not j.get("ok"):
        raise TgError(f"텔레그램 오류 {j.get('description', '')}".strip())
    return j


def split_text(text: str, limit: int = TG_LIMIT) -> list[str]:
    """줄 단위로 limit 안에 들게 나눈다(한 줄이 넘치면 그 줄은 자른다)."""
    out, cur = [], ""
    for ln in text.split("\n"):
        ln = ln[:limit]
        if cur and len(cur) + 1 + len(ln) > limit:
            out.append(cur)
            cur = ln
        else:
            cur = f"{cur}\n{ln}" if cur else ln
    if cur:
        out.append(cur)
    return out


def send(text: str, cfg: dict | None = None) -> None:
    """설정된 대화방으로 보낸다(HTML). 설정이 없으면 TgError."""
    cfg = cfg or load_config()
    if not cfg["token"] or not cfg["chat"]:
        raise TgError("텔레그램 설정이 없습니다(봇 토큰·대화방)")
    for part in split_text(text):
        tg_call(cfg["token"], "sendMessage", {"chat_id": cfg["chat"], "text": part, "parse_mode": "HTML",
                                              "disable_web_page_preview": "true"})


def detect_chat(token: str) -> tuple[str, str]:
    """봇에게 온 가장 최근 메시지의 (대화방 번호, 이름). 아직 아무도 말을 걸지 않았으면 TgError."""
    ups = tg_call(token, "getUpdates", {"limit": 20}).get("result", [])
    for u in reversed(ups):
        msg = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
        chat = msg.get("chat")
        if chat and "id" in chat:
            name = chat.get("title") or " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x) or chat.get("username") or ""
            return str(chat["id"]), name
    raise TgError("봇이 받은 메시지가 없습니다 — 텔레그램에서 봇에게 아무 말이나 보낸 뒤 다시 누르십시오")


# ────────────────────────────── 알림 글

def fmt_hits(label: str, rows: list[dict], when: str, tag: str) -> str:
    """알림 한 건. rows: {'code','name','close','ret'(비율)|None,'ind'(0~1)|None}."""
    head = f"🔔 <b>{html.escape(label)}</b>\n{html.escape(when)} · {html.escape(tag)} · {len(rows)}종목"
    lines = []
    for r in rows[:MAX_LINES]:
        ret = f" ({r['ret'] * 100:+.1f}%)" if r.get("ret") is not None and r["ret"] == r["ret"] else ""
        ind = f" · 개인분산 {r['ind'] * 100:.0f}%" if r.get("ind") is not None and r["ind"] == r["ind"] else ""
        lines.append(f"• <a href=\"https://m.stock.naver.com/domestic/stock/{r['code']}\">{html.escape(r['name'])}</a> "
                     f"{r['code']} · {r['close']:,.0f}원{ret}{ind}")
    if len(rows) > MAX_LINES:
        lines.append(f"… 외 {len(rows) - MAX_LINES}종목")
    return head + "\n" + "\n".join(lines)


def fmt_close(label: str, kept: list[str], gone: list[str], when: str) -> str:
    """마감 정리 — 장중에 알린 종목 중 종가로도 남은 것 / 사라진 것."""
    out = [f"🏁 <b>{html.escape(label)}</b>", f"{html.escape(when)} 마감 확정 · 오늘 알린 {len(kept) + len(gone)}종목 중 남은 {len(kept)} · 사라진 {len(gone)}"]
    if kept:
        out.append("남음: " + html.escape(", ".join(kept[:MAX_LINES])) + (f" 외 {len(kept) - MAX_LINES}" if len(kept) > MAX_LINES else ""))
    if gone:
        out.append("사라짐: " + html.escape(", ".join(gone[:MAX_LINES])) + (f" 외 {len(gone) - MAX_LINES}" if len(gone) > MAX_LINES else ""))
    return "\n".join(out)


# ────────────────────────────── 감시

class Watcher:
    """감시 목록 + 도는 스레드 하나.

    hits(o, day)  -> list[dict]      그 검색 설정(o)으로 day 에 걸린 종목(fmt_hits 의 rows)
    refresh(now)  -> (day|None, final, at_text, note)   오늘 봉을 받아 붙이고 그 날짜를 돌려준다(오늘 장이 없으면 None)
    send(text)                        텔레그램 발신(실패는 TgError)
    """

    def __init__(self, hits, refresh, send_fn=send, state_path: Path | None = None,
                 now=datetime.now, sleep=time.sleep):
        self.hits, self.refresh, self.send, self.now, self.sleep = hits, refresh, send_fn, now, sleep
        self.path = state_path or STATE
        self.lock = threading.RLock()
        self.thread: threading.Thread | None = None
        self.stop_flag = threading.Event()
        self.running = False
        self.log: list[str] = []
        self.last: dict = {}
        self.state = {"items": [], "interval": INTERVAL_DEFAULT, "sent": {}, "names": {}}
        self._load()

    # ── 상태 파일
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
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def _note(self, msg: str) -> None:
        with self.lock:
            self.log.append(f"{self.now():%m-%d %H:%M:%S} {msg}")
            del self.log[:-LOG_MAX]

    # ── 목록
    def items(self) -> list[dict]:
        with self.lock:
            return [dict(i) for i in self.state["items"]]

    def add(self, o: dict, label: str) -> str:
        """감시 목록에 넣는다. 같은 설정이 이미 있으면 그 번호를 돌려준다. 반환: 항목 번호('' = 가득 참)."""
        key = json.dumps(o, sort_keys=True, ensure_ascii=False)
        with self.lock:
            for i in self.state["items"]:
                if json.dumps(i["o"], sort_keys=True, ensure_ascii=False) == key:
                    return i["id"]
            if len(self.state["items"]) >= MAX_ITEMS:
                return ""
            iid = f"w{int(time.time() * 1000):x}{len(self.state['items'])}"
            self.state["items"].append({"id": iid, "o": o, "label": label})
            self._save()
            return iid

    def remove(self, iid: str) -> None:
        with self.lock:
            self.state["items"] = [i for i in self.state["items"] if i["id"] != iid]
            self._save()

    def set_interval(self, minutes: int) -> int:
        with self.lock:
            self.state["interval"] = max(INTERVAL_MIN, min(INTERVAL_MAX, int(minutes)))
            self._save()
            return self.state["interval"]

    # ── 한 바퀴
    def cycle(self, record: bool = True, day=None, final: bool | None = None, at: str = "") -> dict:
        """감시 목록을 한 번 돈다. record=False 면 '지금 걸린 종목 전부'를 보내고 기록하지 않는다(화면의 「지금 한 번 보내기」).
        day 를 주면 오늘 봉을 받지 않고 그 날짜로 돈다. 반환: {'day','sent','new','errors','note'}."""
        now = self.now()
        note = ""
        given = day is not None
        if day is None:
            day, final, at, note = self.refresh(now)
        out = {"day": f"{day:%Y-%m-%d}" if day is not None else None, "sent": 0, "new": 0, "errors": [], "note": note,
               "at": f"{now:%H:%M:%S}", "final": bool(final)}
        if day is None:
            self._note(f"오늘 시세 없음 — {note or '휴장이거나 장 시작 전'}")
            self.last = out
            return out
        key = f"{day:%Y-%m-%d}"
        tag = "종가 확정" if final else "장중 잠정 — 마감 때 사라질 수 있음"
        when = f"{day:%m-%d}" if given and not at else f"{day:%m-%d} {at or f'{now:%H:%M}'}"     # 날짜를 받아 돈 것은 지금 시각이 그 봉의 시각이 아니다
        with self.lock:
            sent_day = self.state["sent"].setdefault(key, {}) if record else {}
            names = self.state["names"].setdefault(key, {}) if record else {}
            items = [dict(i) for i in self.state["items"]]
        for it in items:
            try:
                rows = self.hits(it["o"], day)
            except Exception as e:                               # noqa: BLE001 — 검색식 하나가 터져도 나머지는 돈다
                out["errors"].append(f"{it['label']}: {repr(e)[:80]}")
                continue
            already = set(sent_day.get(it["id"], []))
            fresh = rows if not record else [r for r in rows if r["code"] not in already]
            texts = []
            if fresh:
                first = record and it["id"] not in sent_day
                texts.append(fmt_hits(it["label"], fresh, when, tag + (" · 감시 시작 시점에 이미 걸려 있던 종목 포함" if first else "")))
            if record and final and already | {r["code"] for r in fresh}:
                now_codes = {r["code"] for r in rows}
                told = already | {r["code"] for r in fresh}
                nm = {**names, **{r["code"]: r["name"] for r in rows}}
                if not sent_day.get(f"{it['id']}:closed"):
                    texts.append(fmt_close(it["label"], sorted(nm.get(c, c) for c in told & now_codes),
                                           sorted(nm.get(c, c) for c in told - now_codes), f"{day:%m-%d}"))
            try:
                for t in texts:
                    self.send(t)
                    out["sent"] += 1
            except TgError as e:
                out["errors"].append(f"발신 실패: {e}")
                continue                                          # 못 보냈으면 알린 것으로 적지 않는다 — 다음 바퀴에 다시 보낸다
            out["new"] += len(fresh)
            if record:
                with self.lock:
                    sent_day[it["id"]] = sorted(already | {r["code"] for r in fresh})
                    names.update({r["code"]: r["name"] for r in fresh})
                    if final and texts:
                        sent_day[f"{it['id']}:closed"] = True
        if record:
            with self.lock:
                for k in sorted(self.state["sent"])[:-KEEP_DAYS]:
                    self.state["sent"].pop(k, None)
                    self.state["names"].pop(k, None)
                self._save()
        self._note(f"{key} {'마감' if final else '장중'} · 검색식 {len(items)}개 · 새 종목 {out['new']} · 발신 {out['sent']}건"
                   + (f" · ⚠ {'; '.join(out['errors'])[:160]}" if out["errors"] else ""))
        self.last = out
        return out

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
        """멈춤 신호를 보면서 기다린다."""
        end = time.time() + seconds
        while not self.stop_flag.is_set() and time.time() < end:
            self.sleep(min(1.0, max(0.0, end - time.time())))

    def _run(self) -> None:
        self._note(f"감시 시작 · {self.state['interval']}분마다")
        try:
            while not self.stop_flag.is_set():
                now = self.now()
                if now.weekday() >= 5:
                    self._note("주말입니다 — 감시를 끝냅니다")
                    break
                if now.time() < OPEN:
                    self._wait(min(60.0, (datetime.combine(now.date(), OPEN) - now).total_seconds() + 1))
                    continue
                if not self.items():
                    self._note("감시할 검색식이 없습니다 — 감시를 끝냅니다")
                    break
                r = self.cycle()
                if r["final"] or self.now().time() >= FINAL and r["day"] is None:
                    self._note("장 마감 — 감시를 끝냅니다" if r["day"] else "오늘 장이 없습니다 — 감시를 끝냅니다")
                    break
                self._wait(self.state["interval"] * 60)
            else:
                self._note("감시 중지")
        except Exception as e:                                   # noqa: BLE001
            self._note(f"⚠ 감시가 멈췄습니다 — {repr(e)[:120]}")
        finally:
            with self.lock:
                self.running = False

    def status(self) -> dict:
        with self.lock:
            return {"running": self.running, "interval": self.state["interval"], "items": len(self.state["items"]),
                    "log": list(self.log[-12:]), "last": dict(self.last)}
