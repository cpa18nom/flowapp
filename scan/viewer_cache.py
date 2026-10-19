"""viewer 캐싱 엔진 — 생성된 차트 HTML 캐시 (1시간)."""
import hashlib
import json
import os
import time
from pathlib import Path
from functools import wraps


CACHE_DIR = Path(os.environ.get("FLOWAPP_CACHE_DIR", Path.home() / ".cache" / "flowapp"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_EXPIRE = 3600  # 1시간

# ★ 차트를 만드는 코드가 바뀌면 캐시는 무효다. 이게 없으면 코드를 고쳐도 최대 1시간 동안
#   옛 HTML 이 그대로 나온다(실측 2026-09-20: 축 여유·DART 오버레이를 고쳤는데 화면이 그대로였다).
CODE_FILES = ("viewer.py", "core.py", "flowtool.py", "minute.py", "static/chart.js", "static/lwc.js")


def _code_fingerprint_now() -> str:
    base = Path(__file__).resolve().parent
    parts = []
    for name in CODE_FILES:
        try:
            st = (base / name).stat()
            parts.append(f"{name}:{st.st_mtime_ns}:{st.st_size}")
        except OSError:
            parts.append(f"{name}:-")
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]


# ★ 지문은 **이 프로세스가 코드를 읽어 들인 시점**에 한 번만 잰다(2026-10-07 실측): 요청마다 디스크를 다시 재면, 파일을 고친 뒤
#   아직 옛 코드로 돌고 있는 서버가 만든 HTML 이 **새 지문**을 달고 저장되고, 재기동한 새 서버가 그 옛 HTML 을 그대로 내보낸다
#   (거래대금을 키움 값으로 바꿨는데 화면은 종가×거래량 그대로였다). 디스크 파일이 바뀌어도 메모리의 코드는 옛것이다.
_CODE_FP = _code_fingerprint_now()


def code_fingerprint() -> str:
    return _CODE_FP


class ChartCache:
    """종목별 차트 캐시."""

    @staticmethod
    def _cache_path(code: str) -> Path:
        """캐시 파일 경로."""
        return CACHE_DIR / f"{code}_chart.json"

    @staticmethod
    def get(code: str) -> str | None:
        """캐시에서 가져오기."""
        path = ChartCache._cache_path(code)
        if not path.exists():
            return None

        try:
            with open(path, 'r') as f:
                data = json.load(f)

            # 만료 확인
            if time.time() - data['timestamp'] > CACHE_EXPIRE:
                path.unlink()  # 만료된 캐시 삭제
                return None

            # 코드가 바뀌었으면 만료와 같다
            if data.get('code_fp') != code_fingerprint():
                path.unlink(missing_ok=True)
                return None

            return data['html']
        except Exception:
            return None

    @staticmethod
    def set(code: str, html: str) -> None:
        """캐시에 저장."""
        path = ChartCache._cache_path(code)
        data = {
            'timestamp': time.time(),
            'html': html,
            'code': code,
            'code_fp': code_fingerprint(),
        }
        try:
            with open(path, 'w') as f:
                json.dump(data, f)
        except Exception as e:
            print(f"캐시 저장 실패: {e}")

    @staticmethod
    def clear(code: str = None) -> None:
        """캐시 지우기."""
        if code:
            ChartCache._cache_path(code).unlink(missing_ok=True)
        else:
            # 모든 캐시 지우기
            for path in CACHE_DIR.glob("*_chart.json"):
                path.unlink(missing_ok=True)

    @staticmethod
    def stats() -> dict:
        """캐시 통계."""
        paths = list(CACHE_DIR.glob("*_chart.json"))
        total_size = sum(p.stat().st_size for p in paths) / 1024 / 1024
        return {
            'cached_codes': len(paths),
            'total_size_mb': round(total_size, 2),
            'expire_seconds': CACHE_EXPIRE
        }


def cached_chart(func):
    """차트 생성 함수 캐싱 데코레이터."""
    @wraps(func)
    def wrapper(code: str, *args, **kwargs):
        # 캐시 확인
        cached = ChartCache.get(code)
        if cached:
            return cached, True  # (html, from_cache)

        # 캐시 미스 — 생성
        html = func(code, *args, **kwargs)
        ChartCache.set(code, html)

        return html, False  # (html, from_cache)

    return wrapper
