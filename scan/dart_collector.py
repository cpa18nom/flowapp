"""OpenDART HTTP client used directly by the flowapp server (no MCP dependency)."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from io import BytesIO
import json
import os
from pathlib import Path
import re
import threading
import time
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import urlopen
import xml.etree.ElementTree as ET
from zipfile import ZipFile, BadZipFile

KEY_FILE = Path.home() / '.claude/projects/-home-leegarden/memory/api_keys.md'
CORP_CACHE = Path(os.environ.get('FLOWAPP_CACHE_DIR', Path.home() / '.cache/flowapp')) / 'opendart/corporations.json'
_CORP_LOCK = threading.Lock()
REPORTS = {1: '11013', 2: '11012', 3: '11014', 4: '11011'}
METRICS = {
    'revenue': (('ifrs-full_Revenue', 'ifrs_Revenue', 'dart_Revenue', 'dart_OperatingRevenue'),
                ('매출액', '수익(매출액)', '영업수익', '매출')),
    'op_profit': (('dart_OperatingIncomeLoss', 'ifrs-full_ProfitLossFromOperatingActivities'),
                  ('영업이익', '영업이익(손실)', '영업손실')),
    'net_income': (('ifrs-full_ProfitLoss', 'ifrs_ProfitLoss'), ('당기순이익', '당기순이익(손실)', '분기순이익', '반기순이익', '분기순이익(손실)', '반기순이익(손실)')),
    # 현금은 **현금흐름표의 기말 잔액**이 1순위다. 금융사는 재무상태표에 '현금및현금성자산'이
    # 없고 '현금및예치금'만 있어(실측: KB금융) BS 만 보면 통째로 비어버린다.
    # 기초잔액(dart_CashAndCashEquivalentsAtBeginningOfPeriod)을 집으면 안 되므로 id 로만 받는다.
    'cash': (('ifrs-full_CashAndCashEquivalents', 'ifrs_CashAndCashEquivalents',
              'dart_CashAndCashEquivalentsAtEndOfPeriod'),
             ('기말현금및현금성자산', '반기말현금및현금성자산', '분기말현금및현금성자산',
              '기말의현금및현금성자산', '현금및현금성자산')),
}
# 지표별로 뒤질 재무제표. 앞에 오는 것이 우선이다.
STATEMENTS = {'cash': ('CF', 'BS')}
DEFAULT_STATEMENTS = ('IS', 'CIS')


class DartError(Exception):
    """Safe user-facing error; never includes request URLs or credentials."""


def read_api_key() -> str:
    key = os.environ.get('DART_API_KEY', '').strip()
    if key:
        if re.fullmatch(r'[a-zA-Z0-9]{40}', key):
            return key
        raise DartError('DART_API_KEY 인증키 형식을 확인해주세요.')
    path = Path(os.environ.get('DART_API_KEY_FILE', str(KEY_FILE))).expanduser()
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except OSError:
        raise DartError('DART 인증키 파일을 읽을 수 없습니다. 서버의 DART_API_KEY 또는 DART_API_KEY_FILE을 설정해주세요.') from None
    candidates = set()
    for i, line in enumerate(lines):
        if re.search(r'DART|전자공시', line, re.I):
            candidates.update(re.findall(r'\b[a-fA-F0-9]{40}\b', '\n'.join(lines[i:i + 7])))
    if len(candidates) != 1:
        raise DartError('인증키 파일에서 DART 인증키를 하나로 식별하지 못했습니다.')
    return candidates.pop()


class DartClient:
    def __init__(self, key: str | None = None):
        self._key = key if key is not None else read_api_key()

    def _read(self, endpoint: str, **params) -> bytes:
        url = 'https://opendart.fss.or.kr/api/' + endpoint + '?' + urlencode({'crtfc_key': self._key, **params})
        try:
            with urlopen(url, timeout=25) as response:
                return response.read()
        except (URLError, TimeoutError, OSError):
            raise DartError('DART 서버 연결에 실패했습니다. 잠시 후 다시 조회해주세요.') from None

    @staticmethod
    def check_status(status):
        if status in ('000', '013'):
            return
        messages = {'010': '인증키가 등록되지 않았습니다.', '011': '사용 중지된 인증키입니다.',
                    '012': 'DART에서 허용하지 않은 IP입니다.', '020': 'DART API 요청 한도를 초과했습니다.',
                    '800': 'DART 시스템 점검 중입니다.', '901': 'DART 계정의 인증 갱신이 필요합니다.'}
        raise DartError(messages.get(status, 'DART API에서 정상 응답을 받지 못했습니다.'))

    def get(self, endpoint: str, **params) -> dict:
        try:
            data = json.loads(self._read(endpoint, **params))
        except (ValueError, UnicodeError):
            raise DartError('DART 응답을 해석할 수 없습니다.') from None
        self.check_status(data.get('status'))
        return data

    def corporation(self, code: str) -> dict:
        if not re.fullmatch(r'[0-9A-Z]{6}', code):
            raise DartError('종목코드는 6자리(숫자·대문자)여야 합니다.')
        with _CORP_LOCK:
            companies = None
            try:
                cached = json.loads(CORP_CACHE.read_text(encoding='utf-8'))
                if time.time() - cached['at'] < 86400:
                    companies = cached['companies']
            except (OSError, ValueError, KeyError, TypeError):
                pass
            if companies is None:
                raw = self._read('corpCode.xml')
                try:
                    with ZipFile(BytesIO(raw)) as archive:
                        root = ET.fromstring(archive.read('CORPCODE.xml'))
                    companies = {node.findtext('stock_code', '').strip(): {
                        'corp_code': node.findtext('corp_code'), 'name': node.findtext('corp_name')}
                        for node in root.findall('list') if node.findtext('stock_code', '').strip()}
                except (BadZipFile, KeyError, ET.ParseError):
                    try:
                        status = ET.fromstring(raw).findtext('status')
                    except ET.ParseError:
                        status = None
                    self.check_status(status)
                    raise DartError('DART 회사 고유번호 목록을 읽을 수 없습니다.') from None
                try:
                    CORP_CACHE.parent.mkdir(parents=True, exist_ok=True)
                    temp = CORP_CACHE.with_suffix('.tmp')
                    temp.write_text(json.dumps({'at': time.time(), 'companies': companies}, ensure_ascii=False), encoding='utf-8')
                    temp.replace(CORP_CACHE)
                except OSError:
                    pass  # The freshly downloaded map is still usable.
            if code not in companies:
                raise DartError('DART 회사 고유번호가 없는 종목입니다. ETF·ETN 등은 회사 재무제표가 제공되지 않을 수 있습니다.')
            return companies[code]


def amount(value) -> int | None:
    value = str(value or '').replace(',', '').replace(' ', '').strip()
    if not value or value in ('-', 'N/A'):
        return None
    if value.startswith('(') and value.endswith(')'):
        value = '-' + value[1:-1]
    try:
        return int(value)
    except ValueError:
        return None


def parse_statement(rows: list[dict], year: int, quarter: int, fs_div: str) -> dict:
    """IS/CIS values are three-month values; cash is a period-end balance (CF first, then BS)."""
    result = {'year': year, 'quarter': quarter, 'period': f'{year}Q{quarter}',
              'fs_div': fs_div, 'rcept_no': rows[0].get('rcept_no', ''), 'cumulative': {}}
    for metric, (ids, names) in METRICS.items():
        order = STATEMENTS.get(metric, DEFAULT_STATEMENTS)
        eligible = [r for r in rows if r.get('sj_div') in order]
        eligible.sort(key=lambda r: order.index(r.get('sj_div')))   # 앞에 적힌 재무제표가 우선
        selected = next((r for r in eligible if r.get('account_id') in ids), None)
        if selected is None:
            selected = next((r for r in eligible if re.sub(r'\s+', '', r.get('account_nm', '')) in names), None)
        if selected is not None and selected.get('currency', 'KRW') != 'KRW':
            raise DartError('원화 이외 통화의 재무제표는 현재 차트에서 지원하지 않습니다. 공시 원문을 확인해주세요.')
        selected = selected or {}
        result[metric] = amount(selected.get('thstrm_amount'))
        if metric != 'cash':
            result['cumulative'][metric] = (result[metric] if quarter in (1, 4)
                                            else amount(selected.get('thstrm_add_amount')))
    return result


def quarterize(records: list[dict]) -> list[dict]:
    """Derive Q4 only from same-year/same-basis annual minus nine-month totals."""
    by_period = {(r['year'], r['quarter'], r['fs_div']): r for r in records}
    for row in records:
        if row['quarter'] == 4:
            prev = by_period.get((row['year'], 3, row['fs_div']))
            for metric in ('revenue', 'op_profit', 'net_income'):
                total = row['cumulative'][metric]
                nine_month = prev['cumulative'].get(metric) if prev else None
                row[metric] = total - nine_month if total is not None and nine_month is not None else None
    return sorted(records, key=lambda r: (r['year'], r['quarter']))


def collect_dart_financials(stock_code: str, min_year: int | None = None,
                            max_year: int | None = None, client=None, both: bool = False) -> dict:
    """Fetch current API data on every request; legacy/manual financial caches are never read.

    both=False(기본, DART 섹션): 기간마다 연결 → 없으면 별도, 하나만.
    both=True(차트의 재무 박스, 2026-09-29): 기간마다 **연결과 별도를 모두**(있는 것만). 호출이 최대 2배다."""
    client = client or DartClient()
    company = client.corporation(stock_code)
    today = date.today()
    max_year = max_year or today.year
    min_year = min_year or max_year - 5
    corp = company['corp_code']
    disclosures = client.get('list.json', corp_code=corp,
                             bgn_de=f'{today.year - 1}0101', end_de=today.strftime('%Y%m%d'),
                             sort='date', sort_mth='desc', page_count=10).get('list', [])

    def fetch_one(year, quarter, basis):
        data = client.get('fnlttSinglAcntAll.json', corp_code=corp, bsns_year=year,
                          reprt_code=REPORTS[quarter], fs_div=basis)
        rows = data.get('list', [])
        return parse_statement(rows, year, quarter, basis) if rows else None

    def fetch(period):
        year, quarter = period
        if both:
            return [r for r in (fetch_one(year, quarter, b) for b in ('CFS', 'OFS')) if r]
        for basis in ('CFS', 'OFS'):
            r = fetch_one(year, quarter, basis)
            if r:
                return [r]
        return []

    periods = [(year, q) for year in range(min_year, max_year + 1) for q in REPORTS]
    with ThreadPoolExecutor(max_workers=4) as pool:
        records = [r for rs in pool.map(fetch, periods) for r in rs]
    records = quarterize(records)
    return {'code': stock_code, 'name': company['name'], 'corp_code': corp,
            'source': 'OpenDART', 'collected_at': datetime.now().astimezone().isoformat(timespec='seconds'),
            'records': records, 'disclosures': [{k: r.get(k, '') for k in
                ('report_nm', 'rcept_no', 'rcept_dt', 'flr_nm')} for r in disclosures]}
