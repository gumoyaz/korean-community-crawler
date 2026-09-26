"""
이슈링크(issuelink.co.kr) 2차 소스.

TodayBestStory(TBS)와 운영자·서버가 다른 모음 사이트라, TBS에서 갱신이 멈췄거나(degraded·missing)
평소 글이 아주 적은 커뮤니티(오유 등)를 채우는 데 쓴다. 어떤 커뮤니티에 쓸지는 호출하는 쪽(crawler.py)이 정한다.

- 목록: /community/listview/{site}/{hours}/adj/_self/blank/blank/blank
  커뮤니티별 '인기순' 100행(와이고수는 80행 안팎), 최근 {hours}시간 글. 약 15~25분 늦게 반영된다.
  공식 베스트가 아니라 일반 게시판의 조회수 상위라 에펨·더쿠·보배·SLR은 TBS 목록과 겹치는 글이 거의 없다.
  crawler의 날짜 창(당일, 02시 전에는 어제도)에 맞춰 그 날짜 글만 고르고, 기간(hours)도 자정 이후를 덮는
  가장 짧은 것(3·6·12·24시간)을 골라 100행 안에 당일 글이 많이 들어오게 한다.
- 글 링크는 이슈링크를 거친다(/community/go/{site}/{id} → HEAD 303 · GET 307).
  Location 헤더로 원본 URL을 풀고, 목록 쪽 쿼리(page·po·od 등)는 지운다.
- 조회수(span.hit)는 원본 조회수, 댓글 수는 제목 뒤 <small>[n]</small>, 추천 수는 없다(0).
  시각('2026-09-24 09:17:33')은 KST다.
- robots.txt는 'User-agent: * / Allow:/'(2026-09 확인). 실행 중에도 프로세스마다 한 번 확인해서 막혀 있으면 쓰지 않는다.

fetch()는 실패해도 예외를 올리지 않고 [](또는 받은 만큼)와 로그를 남긴다.
"""

import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

KST = timezone(timedelta(hours=9))

BASE = 'https://www.issuelink.co.kr'
LIST_URL = BASE + '/community/listview/{site}/{hours}/adj/_self/blank/blank/blank'   # adj = 인기순
GO_URL = BASE + '/community/go/{site}/{pid}'
ROBOTS_URL = BASE + '/robots.txt'

# 이슈링크 커뮤니티 → 우리 source id (crawler.COMMUNITY_ID_MAP의 source id)
SOURCE_MAP = {
    '82cook': 'cook82',
    'bobae': 'bobaedream',
    'clien': 'clien',
    'etoland': 'etoland',
    'fmkorea': 'fmkorea',
    'humoruniv': 'humoruniv',
    'instiz': 'instiz',
    'inven': 'inven',
    'mlbpark': 'mlbpark',
    'ppomppu': 'ppomppu',
    'ruliweb': 'ruliweb',
    'slr': 'slrclub',
    'theqoo': 'theqoo',
    'todayhumor': 'todayhumor',
    'ygosu': 'ygosu',
}
SITE_BY_SOURCE = {v: k for k, v in SOURCE_MAP.items()}

# 원본 URL이 이 도메인(또는 하위 도메인)이 아니면 버린다 (삭제된 글이 이슈링크·다른 곳으로 돌려보내는 경우)
ORIGIN_DOMAINS = {
    '82cook': '82cook.com',
    'bobae': 'bobaedream.co.kr',
    'clien': 'clien.net',
    'etoland': 'etoland.co.kr',
    'fmkorea': 'fmkorea.com',
    'humoruniv': 'humoruniv.com',
    'instiz': 'instiz.net',
    'inven': 'inven.co.kr',
    'mlbpark': 'donga.com',
    'ppomppu': 'ppomppu.co.kr',
    'ruliweb': 'ruliweb.com',
    'slr': 'slrclub.com',
    'theqoo': 'theqoo.net',
    'todayhumor': 'todayhumor.co.kr',
    'ygosu': 'ygosu.com',
}

# 원본 URL에서 지우는 쿼리 — 이슈링크가 붙인 목록 위치(page·po·pg·p)·정렬(od)·보기 옵션.
# 글 번호·게시판(id·no·num·number·table·code·No·b·bn·m)은 남긴다 (crawler._url_key 중복 제거가 그대로 맞물린다)
DROP_QUERY_KEYS = frozenset({
    'page', 'p', 'po', 'pg', 'od', 'category', 'groupCd', 'bm', 'iskin', 'kind',
    'select', 'query', 'subselect', 'subquery', 'user', 'site',
})

PER_SOURCE = 25     # 커뮤니티당 원본 URL을 푸는 글 수 (crawler._assign_ranks의 MAX_PER_SOURCE와 같게)
HOURS = 24          # 날짜 창을 쓰지 않을 때의 목록 기간(시간)
LIST_HOURS = (3, 6, 12, 24)   # 자동으로 고르는 목록 기간 (이슈링크 선택지 3·6·12·24·48·72·96·120·168·336 중)
DEADLINE = 20       # 초. 목록 + 원본 URL 풀기 전체 상한 — 넘으면 푼 글까지만 돌려준다
LIST_SHARE = 0.55   # 전체 상한 중 목록 받기에 쓰는 몫 (나머지는 원본 URL 풀기)
LIST_TIMEOUT = 8
GO_TIMEOUT = 5
ROBOTS_TIMEOUT = 5
LIST_WORKERS = 3    # 동시 연결 수 (이슈링크 서버 예의)
GO_WORKERS = 4
LIST_GAP = (0.3, 0.6)    # 같은 작업자의 목록 요청 사이 간격(초)
GO_GAP = (0.05, 0.15)    # 같은 작업자의 원본 URL 풀기 요청 사이 간격(초)
ROBOTS_TTL = 6 * 3600    # robots.txt 확인 결과를 프로세스 안에서 재사용하는 시간(초)
CACHE_MAX = 1000         # 원본 URL 캐시 항목 상한 (항목당 약 100바이트, crawler.ISSUELINK_CACHE_MAX와 같게)

REDIRECT_CODES = (301, 302, 303, 307, 308)
DATE_RE = re.compile(r'(\d{4})-(\d{2})-(\d{2})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?')
COUNT_RE = re.compile(r'\[(\d[\d,]*)\]')

# 'rel' 속성의 이슈링크 글 id → 원본 URL. cache를 넘기지 않으면 이 모듈 캐시를 프로세스 안에서 재사용한다.
# build.py(GitHub Actions)는 실행마다 프로세스가 새로 뜨므로 crawler가 fetch(cache=state 안의 dict)로 넘겨 이어 쓴다
_CACHE: dict = {}
_ROBOTS = {'at': None, 'allowed': True}
_ROBOTS_LOCK = threading.Lock()

# 마지막 fetch()의 결과 (source_health·로그용).
# {'at': ISO, 'hours': 목록 기간, 'dates': [날짜 창], 'robots': bool, 'elapsed': 초,
#  'sources': {source id: {'site', 'status': ok|empty|error|skipped, 'error'(있을 때),
#              'rows': 날짜 창 안 목록 행 수, 'tried': 풀려고 한 글 수, 'resolved': 돌려준 글 수,
#              'newest': 날짜 창 안 인기글 중 가장 새 글 시각 ISO|None}}}
# newest는 인기순 100행 안에서의 최신이라 이슈링크 수집 지연을 재는 값으로는 느슨하다
LAST_RUN: dict = {}

_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'


def _log(msg: str) -> None:
    print(f'[IssueLink] {msg}')


def _crawler():
    """crawler 모듈 (사용 지점에서 import — crawler.py가 이 모듈을 import해도 순환이 생기지 않게)"""
    import crawler
    return crawler


def _headers(referer: str = BASE + '/') -> dict:
    try:
        return _crawler()._random_headers({'Referer': referer})
    except Exception:
        return {'User-Agent': _UA, 'Accept-Language': 'ko-KR,ko;q=0.9',
                'Accept': 'text/html,application/xhtml+xml,*/*;q=0.8', 'Referer': referer}


# ── 파싱 ──────────────────────────────────────────────────────────────────────

def parse_list(html, site: str | None = None) -> list:
    """목록 페이지 HTML → 행 목록(순위순).
    [{'site', 'pid', 'title', 'comments', 'views', 'date', 'author'}]. site를 주면 그 커뮤니티 행만.
    date는 계약 형식('YYYY-MM-DDTHH:MM:SS+09:00') 또는 ''."""
    soup = BeautifulSoup(html, 'html.parser')
    rows, seen = [], set()
    for a in soup.select('span.title > a[rel]'):
        rel = a.get('rel')
        rel = rel[0] if isinstance(rel, list) and rel else (rel or '')
        m = re.fullmatch(r'([a-z0-9]+)-(\d+)', rel.strip())
        if not m or m.group(1) not in SOURCE_MAP or (site and m.group(1) != site):
            continue
        r_site, pid = m.group(1), m.group(2)
        if (r_site, pid) in seen:
            continue
        seen.add((r_site, pid))

        comments = 0
        for small in a.find_all('small'):
            c = COUNT_RE.search(small.get_text())
            if c:
                comments = int(c.group(1).replace(',', ''))
            small.decompose()
        title = re.sub(r'\s+', ' ', a.get_text(' ')).strip()
        title = re.sub(r'\s*[\[(]\d+[\])]$', '', title).strip()   # 끝에 남은 댓글 수 '[13]'

        tr = a.find_parent('tr')
        date_el = tr.select_one('div.second_date span') if tr else None
        hit_el = tr.select_one('span.hit') if tr else None
        nick_el = tr.select_one('span.nick a') if tr else None
        rows.append({
            'site': r_site,
            'pid': pid,
            'title': title,
            'comments': comments,
            'views': _parse_int(hit_el.get_text()) if hit_el else 0,
            'date': _parse_date(date_el.get_text()) if date_el else '',
            'author': nick_el.get_text(' ', strip=True) if nick_el else '',
        })
    return rows


def _parse_int(text: str) -> int:
    m = re.search(r'\d[\d,]*', text or '')
    return int(m.group(0).replace(',', '')) if m else 0


def _parse_date(text: str) -> str:
    m = DATE_RE.search(text or '')
    if not m:
        return ''
    try:
        dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5)),
                      int(m.group(6) or 0), tzinfo=KST)
    except ValueError:
        return ''
    return dt.strftime('%Y-%m-%dT%H:%M:%S+09:00')


def clean_url(site: str, location: str):
    """이슈링크 /go/ 의 Location → 원본 URL(https, 목록 쿼리 제거). 원본 도메인이 아니면 None."""
    if not location:
        return None
    u = urlparse(urljoin(GO_URL.format(site=site, pid=0), location.strip()))
    host = (u.hostname or '').lower()
    if u.scheme not in ('http', 'https') or not host or host.endswith('issuelink.co.kr'):
        return None
    dom = ORIGIN_DOMAINS.get(site)
    if dom and host != dom and not host.endswith('.' + dom):
        return None
    query = urlencode([(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if k not in DROP_QUERY_KEYS])
    # 15곳 모두 https를 받는다 (오유·웃대·SLR은 이슈링크가 http로 돌려준다)
    return urlunparse(('https', u.netloc, u.path, u.params, query, ''))


# ── 네트워크 ──────────────────────────────────────────────────────────────────

class _Sessions:
    """작업자 스레드마다 Session 하나 (Session은 스레드끼리 나눠 쓰지 않는다).
    keep-alive로 이슈링크 TLS 연결을 재사용하고, 끝나면 close()로 한꺼번에 닫는다."""

    def __init__(self):
        self._tls = threading.local()
        self._all = []
        self._lock = threading.Lock()

    def get(self) -> requests.Session:
        s = getattr(self._tls, 's', None)
        if s is None:
            s = requests.Session()
            self._tls.s = s
            with self._lock:
                self._all.append(s)
        return s

    def close(self) -> None:
        with self._lock:
            for s in self._all:
                s.close()
            self._all.clear()


def _robots_allowed() -> bool:
    """robots.txt가 목록·/go/ 경로를 허용하는지. 결과는 ROBOTS_TTL 동안 재사용.
    RFC 9309: 4xx면 제한 없음, 5xx·연결 실패면 이번에는 쓰지 않는다(이 결과는 캐시하지 않는다)."""
    with _ROBOTS_LOCK:
        at = _ROBOTS['at']
        if at is not None and time.monotonic() - at < ROBOTS_TTL:
            return _ROBOTS['allowed']
    h = _headers()
    try:
        r = requests.get(ROBOTS_URL, headers=h, timeout=ROBOTS_TIMEOUT)
    except requests.RequestException as e:
        _log(f'robots.txt 확인 실패 ({type(e).__name__}) - 이번 실행은 건너뜀')
        return False
    if r.status_code >= 500:
        _log(f'robots.txt HTTP {r.status_code} - 이번 실행은 건너뜀')
        return False
    head = r.text[:2000].lower() if r.status_code < 400 else ''
    if '<html' in head or 'cupid.js' in head or 'tonumbers(' in head:
        # 해외 IP(GitHub Actions 러너)에는 robots.txt 대신 JS 쿠키 봇 확인 페이지가 온다(2026-09-25 Source probe, 목록도
        # 같은 이유로 0행). 이 확인을 풀지 않는다 — 봇 차단 우회라서. 이 HTML을 robots로 읽으면 규칙이 없어 '허용'이 된다
        _log('robots.txt 대신 봇 확인 페이지를 받음(해외 IP 차단으로 보임) - 이번 실행은 건너뜀')
        return False
    if r.status_code >= 400:
        allowed = True
    else:
        rp = RobotFileParser()
        rp.parse(r.text.splitlines())
        ua = h.get('User-Agent') or _UA
        allowed = (rp.can_fetch(ua, LIST_URL.format(site='clien', hours=HOURS))
                   and rp.can_fetch(ua, GO_URL.format(site='clien', pid=1)))
    with _ROBOTS_LOCK:
        _ROBOTS['at'], _ROBOTS['allowed'] = time.monotonic(), allowed
    if not allowed:
        _log('robots.txt가 목록·/go/ 경로를 막음 - 쓰지 않는다')
    return allowed


def _fetch_list(session: requests.Session, site: str, hours: int, deadline: float):
    """(행 목록, 오류 문자열|None). 연결 오류·5xx만 1초 뒤 1회 재시도. deadline이 지나면 새 요청을 시작하지 않는다."""
    url = LIST_URL.format(site=site, hours=hours)
    err = None
    for attempt in range(2):
        if time.monotonic() > deadline:
            return [], err or '시간 초과'
        try:
            r = session.get(url, headers=_headers(), timeout=LIST_TIMEOUT)
        except requests.RequestException as e:
            err = type(e).__name__
        else:
            if r.status_code == 200:
                try:
                    return parse_list(r.content, site), None
                except Exception as e:
                    return [], f'파싱 오류 {type(e).__name__}'
            err = f'HTTP {r.status_code}'
            if r.status_code < 500:
                break
        if attempt == 0:
            time.sleep(1)
    return [], err


def _resolve(session: requests.Session, site: str, pid: str, deadline: float):
    """이슈링크 /go/ 의 Location으로 원본 URL을 푼다(리다이렉트는 따라가지 않아 원본 사이트에는 요청하지 않는다).
    실패·시간 초과면 None."""
    if time.monotonic() > deadline:
        return None
    time.sleep(random.uniform(*GO_GAP))
    go = GO_URL.format(site=site, pid=pid)
    try:
        r = session.head(go, headers=_headers(), allow_redirects=False, timeout=GO_TIMEOUT)
        if r.status_code in (405, 501):   # HEAD를 안 받으면 GET (본문은 읽지 않는다)
            r = session.get(go, headers=_headers(), allow_redirects=False, timeout=GO_TIMEOUT, stream=True)
            r.close()
    except requests.RequestException:
        return None
    if r.status_code not in REDIRECT_CODES:
        return None
    return clean_url(site, r.headers.get('Location', ''))


# ── 공개 함수 ─────────────────────────────────────────────────────────────────

def fetch(sources=None, now=None, *, per_source: int = PER_SOURCE, hours: int | None = None, dates=None,
          deadline: float = DEADLINE, cache: dict | None = None) -> list:
    """이슈링크에서 커뮤니티별 인기글을 받아 crawler의 글 스키마로 돌려준다 (via='issuelink').

    sources     우리 source id 집합 (예: {'todayhumor', 'fmkorea'}). None이면 매핑된 15곳 전부.
                이슈링크에 없는 커뮤니티(dcinside 등)는 무시한다. 해당 커뮤니티가 없으면 요청 없이 [].
    now         기준 시각. None이면 지금(KST). 날짜 창·목록 기간을 여기서 정한다.
    per_source  커뮤니티당 원본 URL을 푸는 최대 글 수 (날짜 창 안의 글 중 목록 순위 앞쪽부터)
    hours       목록 기간(시간). None이면 날짜 창 시작(자정)부터 now까지를 덮는 LIST_HOURS 중 가장 짧은 것.
                직접 줄 때는 이슈링크 선택지 3·6·12·24·48·72·96·120·168·336 중 하나
    dates       남길 글 날짜('YYYY-MM-DD') 모음. None이면 crawler와 같은 창(당일, KST 02시 전에는 어제도).
                빈 모음이면 날짜로 거르지 않고 목록 기간+1시간 안의 글을 모두 쓴다.
                원본 URL을 풀기 전에 거르므로 쓰지 않을 글에 /go/ 요청을 하지 않는다
    deadline    전체 시간 상한(초). 넘으면 그때까지 푼 글만 돌려준다
    cache       '{site}-{id}' → 원본 URL dict. 넘기면 읽고 쓴다(실행 사이에 state로 이어 쓰기).
                None이면 모듈 캐시(프로세스 수명)를 쓴다. CACHE_MAX개로 자른다.

    원본 URL을 풀지 못한 글은 넣지 않는다. 같은 글(crawler._url_key)은 한 번만. 어떤 실패도 예외로 올리지 않는다.
    커뮤니티별 결과는 LAST_RUN에 남긴다."""
    started = time.monotonic()
    LAST_RUN.clear()
    try:
        return _fetch(sources, now, per_source, hours, dates, deadline, cache, started)
    except Exception as e:   # 어떤 실패도 크롤을 막지 않는다
        _log(f'예외 - 이번 실행은 건너뜀: {type(e).__name__}: {e}')
        return []
    finally:
        LAST_RUN['elapsed'] = round(time.monotonic() - started, 2)


def _fetch(sources, now, per_source, hours, dates, deadline, cache, started) -> list:
    now = now or datetime.now(KST)
    now = (now if now.tzinfo else now.replace(tzinfo=KST)).astimezone(KST)
    window = _date_window(now) if dates is None else {str(d)[:10] for d in dates}
    hours = int(hours) if hours else _auto_hours(now, window)
    report: dict = {}
    LAST_RUN.update({'at': now.isoformat(timespec='seconds'), 'hours': hours, 'dates': sorted(window),
                     'sources': report})
    if sources is None:
        sites = list(SOURCE_MAP)
    else:
        wanted = {sources} if isinstance(sources, str) else set(sources)
        sites = [site for site, sid in SOURCE_MAP.items() if sid in wanted]
    if not sites:
        return []
    for site in sites:
        report[SOURCE_MAP[site]] = {'site': site, 'status': 'skipped', 'rows': 0, 'tried': 0, 'resolved': 0,
                                    'newest': None}
    cache = _CACHE if cache is None else cache
    per_source = max(0, int(per_source))
    budget = max(1.0, float(deadline))
    end_at = started + budget
    list_end = started + budget * LIST_SHARE   # 이 시각 뒤로는 목록 요청을 새로 시작하지 않는다

    LAST_RUN['robots'] = _robots_allowed()
    if not LAST_RUN['robots']:
        for rec in report.values():
            rec['error'] = 'robots.txt'
        return []

    oldest = now - timedelta(hours=hours + 1)

    def keep(row: dict) -> bool:
        if not row['date']:
            return True
        if window:
            return row['date'][:10] in window
        return datetime.fromisoformat(row['date']) >= oldest

    lists: dict = {}
    resolved: dict = {}
    n_cached = 0
    pool = _Sessions()
    try:
        # 1) 커뮤니티별 목록 — 동시 LIST_WORKERS개, 작업자마다 간격을 둔다
        def one_list(site: str):
            time.sleep(random.uniform(*LIST_GAP))
            return _fetch_list(pool.get(), site, hours, list_end)

        ex = ThreadPoolExecutor(max_workers=LIST_WORKERS)
        try:
            futs = {ex.submit(one_list, site): site for site in sites}
            done, _ = wait(futs, timeout=max(0.0, end_at - time.monotonic()))
            for f in done:
                site = futs[f]
                rows, err = f.result()
                rows = [r for r in rows if keep(r)]
                lists[site] = rows
                rec = report[SOURCE_MAP[site]]
                rec['rows'] = len(rows)
                row_dates = [r['date'] for r in rows if r['date']]
                rec['newest'] = max(row_dates) if row_dates else None
                if err:
                    rec['status'], rec['error'] = 'error', err
                elif not rows:
                    rec['status'] = 'empty'
        finally:
            ex.shutdown(wait=False, cancel_futures=True)
        for site in sites:
            if site not in lists:
                rec = report[SOURCE_MAP[site]]
                rec['status'], rec['error'] = 'error', '시간 초과'

        # 2) 원본 URL 풀기 — 순위를 커뮤니티끼리 번갈아(1위들, 2위들, …) 넣어 시간이 모자라도 커뮤니티마다 상위 글이 남게
        todo = []
        for i in range(per_source):
            for site in sites:
                rows = lists.get(site) or []
                if i >= len(rows):
                    continue
                report[SOURCE_MAP[site]]['tried'] += 1
                key = f'{site}-{rows[i]["pid"]}'
                if cache.get(key):
                    resolved[(site, i)] = cache[key]
                else:
                    todo.append((site, i, key))
        n_cached = len(resolved)
        if todo and time.monotonic() < end_at:
            def one_go(site: str, pid: str):
                return _resolve(pool.get(), site, pid, end_at)

            ex = ThreadPoolExecutor(max_workers=GO_WORKERS)
            try:
                futs = {ex.submit(one_go, site, lists[site][i]['pid']): (site, i, key) for site, i, key in todo}
                done, _ = wait(futs, timeout=max(0.0, end_at - time.monotonic()))
                for f in done:
                    site, i, key = futs[f]
                    url = f.result()
                    if url:
                        resolved[(site, i)] = url
                        cache[key] = url
            finally:
                ex.shutdown(wait=False, cancel_futures=True)
    finally:
        pool.close()
    _trim_cache(cache)

    # 3) 글 스키마로
    cr = _crawler()
    posts, seen = [], set()
    for site in sites:
        sid = SOURCE_MAP[site]
        label, emoji, color = cr.SOURCE_META.get(sid, (sid, '📝', '#7c6cff'))
        rec = report[sid]
        for i, row in enumerate((lists.get(site) or [])[:per_source]):
            url = resolved.get((site, i))
            title = row['title']
            if not url or len(title) < 4 or cr.NOTICE_TITLE_RE.match(title):
                continue
            key = cr._url_key(url)
            if key in seen:
                continue
            seen.add(key)
            rec['resolved'] += 1
            posts.append({
                'source': sid,
                'source_label': label,
                'source_emoji': emoji,
                'source_color': color,
                'title': title,
                'summary': '',
                'board': '',
                'url': url,
                'author': row['author'],
                'date': row['date'],
                **_classify(cr, title),
                'views': row['views'],
                'likes': 0,
                'comments': row['comments'],
                'position_score': max(0.0, 100 - i * 1.5),   # 이슈링크 인기순 안에서의 순번
                'rank_score': 0,
                'rank': 0,
                'via': 'issuelink',
            })
        if rec['status'] == 'skipped' and rec['rows']:
            if rec['resolved']:
                rec['status'] = 'ok'
            else:
                rec['status'], rec['error'] = 'error', '원본 URL을 풀지 못함'

    parts = ', '.join(f'{sid} {r["resolved"]}/{r["tried"]}' + (f'({r["error"]})' if r.get('error') else '')
                      for sid, r in report.items())
    _log(f'{len(posts)}건 ({len(sites)}곳, 최근 {hours}시간·{",".join(sorted(window)) or "날짜 무관"}, '
         f'캐시 {n_cached}, 새로 푼 URL {len(resolved) - n_cached}, '
         f'{time.monotonic() - started:.1f}초) — {parts}')
    return posts


def _date_window(now: datetime) -> set:
    """crawler와 같은 날짜 창 — KST 02시 전에는 어제·오늘, 그 뒤에는 오늘 (TBS range 요청과 같다)"""
    today = now.strftime('%Y-%m-%d')
    return {today, (now - timedelta(days=1)).strftime('%Y-%m-%d')} if now.hour < 2 else {today}


def _auto_hours(now: datetime, window: set) -> int:
    """날짜 창 시작(자정)부터 now까지를 덮는 가장 짧은 목록 기간. 창이 없거나 24시간을 넘으면 HOURS"""
    if not window:
        return HOURS
    try:
        start = datetime.strptime(min(window), '%Y-%m-%d').replace(tzinfo=KST)
    except ValueError:
        return HOURS
    need = (now - start).total_seconds() / 3600
    return next((h for h in LIST_HOURS if h >= need), HOURS)


def _classify(cr, title: str) -> dict:
    """crawler의 카테고리 판정(TrendCrawler._classify)을 그대로 쓴다. 인스턴스 상태를 쓰지 않는 메서드라
    __init__ 없이 만든 인스턴스로 부른다. 실패하면 카테고리 없음('일반')."""
    try:
        return cr.TrendCrawler.__new__(cr.TrendCrawler)._classify(title)
    except Exception:
        return {'keyword': '일반', **{flag: False for flag, _, _ in getattr(cr, 'CATEGORIES', [])}}


def _trim_cache(cache: dict) -> None:
    """먼저 넣은 항목부터 지워 CACHE_MAX개로 (dict는 넣은 순서를 지킨다)"""
    extra = len(cache) - CACHE_MAX
    if extra > 0:
        for k in list(cache)[:extra]:
            cache.pop(k, None)
