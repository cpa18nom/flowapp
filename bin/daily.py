"""하루치 날짜 파일 — 새로 들어온 행만 날짜별로 따로 남긴다.

**왜 필요한가.** 종목별 파일은 1행을 더하려고 1,400행을 통째로 다시 압축한다. gz 는 부분
수정이 안 되는 포맷이라 append 가 원리적으로 불가능하다. ext4 에서는 그게 빠르지만,
그 폴더를 OneDrive 에 두는 순간 **매일 5,000개 파일·175MB 가 재업로드**된다
(실측 2026-09-20: 하루 갱신에 시세 2,675개 + 수급 2,484개 파일이 바뀌었다).

날짜 파일은 하루에 **딱 하나** 새로 생기고 기존 파일을 건드리지 않는다. 실측 크기는
시세 41KB · 수급 67KB — OneDrive 가 나르기에 부담이 없다.

    ~/market/raw/_daily/price/20260921.csv.gz
    ~/market/raw/_daily/flow/20260921.csv.gz

종목별 파일을 대신하는 것이 아니다. **작업본은 종목별 파일, 운반본은 날짜 파일**이다 —
앱은 종목 시계열을 읽어 패널을 만들기 때문에 종목별 파일이 있어야 빠르다.
"""
from __future__ import annotations

import gzip
import threading
from pathlib import Path

import pandas as pd


class DailyWriter:
    """수집 중에 '새로 들어온 행'만 모았다가 날짜별 파일로 떨군다.

    한 번 실행에 여러 날짜가 들어올 수 있다(연휴 뒤 갱신). 날짜마다 파일을 나눈다.
    이미 있는 날짜 파일과는 **합치고 중복을 지운다** — 같은 날 두 번 돌려도 깨지지 않는다.
    """

    def __init__(self, kind: str, root: Path, date_col: str, code_col: str = "Code"):
        self.dir = Path(root) / "_daily" / kind
        self.date_col, self.code_col = date_col, code_col
        self._rows: list[pd.DataFrame] = []
        self._lock = threading.Lock()          # mkt 은 워커 6개로 병렬 수집한다

    def add(self, code: str, rows: pd.DataFrame | None) -> None:
        if rows is None or not len(rows):
            return
        d = rows.copy()
        if self.code_col not in d.columns:
            d.insert(0, self.code_col, code)
        with self._lock:
            self._rows.append(d)

    def flush(self) -> dict[str, int]:
        """날짜별로 써서 {날짜: 행수}. 쓸 게 없으면 빈 dict."""
        with self._lock:
            rows, self._rows = self._rows, []
        if not rows:
            return {}
        all_rows = pd.concat(rows, ignore_index=True)
        all_rows[self.date_col] = all_rows[self.date_col].astype(str)
        self.dir.mkdir(parents=True, exist_ok=True)
        out = {}
        for day, part in all_rows.groupby(self.date_col):
            stamp = day.replace("-", "")[:8]
            path = self.dir / f"{stamp}.csv.gz"
            if path.exists():                  # 같은 날 두 번째 실행 — 합치고 중복 제거
                part = pd.concat([pd.read_csv(path, dtype={self.code_col: str}), part],
                                 ignore_index=True)
                part[self.date_col] = part[self.date_col].astype(str)
            part = part.drop_duplicates([self.code_col, self.date_col], keep="last")
            part = part.sort_values(self.code_col)
            with gzip.open(path, "wt", encoding="utf-8", compresslevel=6) as f:
                part.to_csv(f, index=False)
            out[stamp] = len(part)
        return out


def report(written: dict[str, int], root: Path, kind: str) -> str:
    if not written:
        return "  날짜 파일: 새로 들어온 행이 없습니다"
    lines = []
    for stamp, n in sorted(written.items()):
        p = Path(root) / "_daily" / kind / f"{stamp}.csv.gz"
        size = p.stat().st_size / 1024 if p.exists() else 0
        lines.append(f"  날짜 파일 {stamp}: {n:,}행 · {size:,.0f}KB  ({p})")
    return "\n".join(lines)
