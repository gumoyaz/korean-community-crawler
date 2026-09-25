"""
크롤링 소스 실측 프로브.

GitHub Actions 러너(Azure 해외 IP)에서 각 소스 URL의 HTTP 상태·바이트·<title>·차단 여부·파싱 행 수를 잰다.
.github/workflows/source-probe.yml(수동 실행 전용)이 돌린다. 크롤 결과·state·배포는 건드리지 않는다.

대상 그룹
  tbs         TodayBestStory API(오늘 1페이지)·health
  fallback    crawler.COMMUNITY_SOURCES의 직접 스크래핑 URL 전부 (행 수는 crawler._parse_rows)
  issuelink   이슈링크 robots.txt·커뮤니티 15곳 목록(행 수는 sources_issuelink.parse_list)·곳마다 /go/ 1건
  candidates  후보 URL (분석 C urls_overseas.txt 기반 + 보고서의 추가 후보). fallback과 같은 URL은 뺀다

판정(verdict)
  ok       2xx·3xx이고 차단 흔적이 없다 (파서가 있으면 1행 이상)
  blocked  401·403·429·430·451·503, cf-mitigated 헤더, 또는 200이지만 챌린지·보안 페이지
           ('Just a moment'·'보안 검사'·'보안 시스템'·captcha 등)나 msg.html 리다이렉트이고 글이 없다
  empty    200인데 파서 행이 0 (선택자가 깨졌거나 내용 없는 차단 페이지)
  http-NNN 그 밖의 4xx·5xx
  error    연결 오류·타임아웃

결과: 마크다운 표를 표준 출력과 GITHUB_STEP_SUMMARY(있으면)에, 전체 결과를 JSON(--out)으로.

사용법:
  python tools/probe_sources.py --out probe/probe_sources.json
  python tools/probe_sources.py --only issuelink,tbs      # 그룹만
  python tools/probe_sources.py --no-ipinfo               # 러너 국가·망 조회(ipinfo.io) 생략
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import requests  # noqa: E402

import crawler  # noqa: E402
import sources_issuelink as issuelink  # noqa: E402

KST = timezone(timedelta(hours=9))
GROUPS = ('tbs', 'fallback', 'issuelink', 'candidates')
TIMEOUT = 15
BODY_SCAN = 200_000   # 차단 흔적·<title>은 본문 앞부분에서만 찾는다

TBS_HEALTH_URL = 'https://todaybeststory.com/api/v2/health'

# 후보 URL: (id, URL, 파서로 쓸 COMMUNITY_SOURCES id 또는 None). '{stdt}'는 오늘(KST) YYYYMMDD.
# 지금 fallback에 있는 베스트 목록(오유·루리웹·클리앙·더쿠 등)은 fallback 그룹이 재므로 여기 두지 않는다
CANDIDATES = [
    ('natekorea', 'https://pann.nate.com/talk/ranking/d?stdt={stdt}', 'natekorea'),   # 오늘 날짜 일간 랭킹
    ('natekorea', 'https://pann.nate.com/talk/ranking', 'natekorea'),
    ('dcinside', 'https://gall.dcinside.com/board/lists/?id=dcbest', None),
    ('fmkorea', 'https://www.fmkorea.com/best', None),
    ('instiz', 'https://www.instiz.net/pt', None),
    ('ppomppu', 'https://www.ppomppu.co.kr/hot.php', None),
    ('slrclub', 'https://www.slrclub.com/bbs/zboard.php?id=best_article', None),
    ('inven', 'https://www.inven.co.kr/board/webzine/2097', None),
    ('cook82', 'https://www.82cook.com/entiz/enti.php?bn=15', None),
    ('etoland', 'https://www.etoland.co.kr/bbs/hit.php', None),
    ('etoland', 'https://www.etoland.co.kr/b/hit/list', None),
    ('ygosu', 'https://ygosu.com/board/real_article', None),
    ('dogdrip', 'https://www.dogdrip.net/dogdrip', None),
    ('gasengi', 'https://www.gasengi.com/main/board.php?bo_table=commu', None),
    ('nunting', 'https://nunting.kr/', None),
    ('nunting', 'https://nunting.kr/site/theqoo', None),
    ('moamoa', 'https://moamoa.kr/api?scope=3&sort=popular', None),
    ('jamnanda', 'https://www.jamnanda.com/commbest/list/', None),
    ('mlbpark-rss', 'https://mlbpark.donga.com/mp/rss.php?b=bullpen', None),
    ('ruliweb-rss', 'https://bbs.ruliweb.com/community/board/300143/rss', None),
    ('ppomppu-rss', 'https://www.ppomppu.co.kr/rss.php?id=freeboard', None),
]

BLOCK_STATUS = {401, 403, 429, 430, 451, 503}
CHALLENGE_RE = re.compile(r'Just a moment|Attention Required|Checking your browser|cf-chl|_cf_chl|challenge-platform'
                          r'|보안 검사|보안 시스템|captcha', re.I)
MSG_REDIRECT_RE = re.compile(r'''location\.(?:replace|href)\s*\(?\s*=?\s*['"][^'"]*msg\.html''', re.I)
TITLE_RE = re.compile(r'<title[^>]*>(.*?)</title>', re.I | re.S)
ARTICLE_HREF_RE = re.compile(r'''href\s*=\s*["']([^"'#]*?\d{5,}[^"'#]*)["']''', re.I)


def _headers(url: str, referer: str | None = None) -> dict:
    """crawler._get_page와 같은 헤더 (브라우저 UA + 같은 사이트 Referer)"""
    u = urlparse(url)
    return crawler._random_headers({'Referer': referer or f'{u.scheme}://{u.netloc}/'})


def _decode(body: bytes, encoding: str | None) -> str:
    """본문 앞부분을 문자열로. 헤더에 charset이 없으면 requests가 ISO-8859-1로 두므로 그때는 UTF-8·CP949 순으로"""
    head = body[:BODY_SCAN]
    declared = encoding if encoding and encoding.lower() not in ('iso-8859-1', 'latin-1') else None
    for enc in filter(None, (declared, 'utf-8', 'cp949')):
        try:
            return head.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return head.decode('utf-8', errors='replace')


def probe(group: str, sid: str, url: str, *, parse=None, method: str = 'GET', referer: str | None = None,
          params: dict | None = None) -> tuple[dict, bytes | None]:
    """URL 하나를 받아 결과 레코드와 본문을 돌려준다. parse(body) → 행 수(int) 또는 (행 수, 메모)."""
    rec = {'group': group, 'id': sid, 'url': url}
    t = time.monotonic()
    try:
        if method == 'HEAD':
            r = requests.head(url, headers=_headers(url, referer), allow_redirects=False, timeout=TIMEOUT)
        else:
            r = requests.get(url, headers=_headers(url, referer), params=params, timeout=TIMEOUT)
    except requests.RequestException as e:
        rec.update(sec=round(time.monotonic() - t, 2), verdict='error', error=f'{type(e).__name__}: {str(e)[:160]}')
        return rec, None
    body = r.content or b''
    text = _decode(body, r.encoding)
    m = TITLE_RE.search(text)
    if params:   # 쿼리를 붙인 실제 요청 URL로 남긴다
        rec['url'] = url = r.request.url if r.request is not None else r.url
    rec.update({
        'final_url': r.url if r.url != url else None,
        'status': r.status_code,
        'bytes': len(body),
        'sec': round(time.monotonic() - t, 2),
        'server': r.headers.get('server'),
        'cf_ray': bool(r.headers.get('cf-ray')),
        'cf_mitigated': r.headers.get('cf-mitigated'),
        'location': r.headers.get('location') if 300 <= r.status_code < 400 else None,
        'title': re.sub(r'\s+', ' ', m.group(1)).strip()[:80] if m else None,
        'article_links': len(set(ARTICLE_HREF_RE.findall(text))),
    })
    challenge = bool(CHALLENGE_RE.search(rec['title'] or '') or CHALLENGE_RE.search(text[:50_000]))
    msg_redirect = bool(MSG_REDIRECT_RE.search(text[:20_000]) or 'msg.html' in (r.url or ''))
    rec['challenge'] = challenge
    rec['msg_redirect'] = msg_redirect
    if parse is not None and r.status_code < 400 and body:
        try:
            out = parse(body)
            rec['rows'], rec['note'] = out if isinstance(out, tuple) else (out, rec.get('note'))
        except Exception as e:
            rec['rows'] = 0
            rec['note'] = f'파싱 오류 {type(e).__name__}: {str(e)[:120]}'
    rec['verdict'] = _verdict(r, rec)
    return rec, body


def _verdict(r, rec: dict) -> str:
    st = r.status_code
    if st in BLOCK_STATUS or (rec.get('cf_mitigated') or '').lower() == 'challenge':
        return 'blocked'
    if st >= 400:
        return f'http-{st}'
    rows = rec.get('rows')
    no_posts = (rows == 0) if rows is not None else rec['article_links'] < 5
    if (rec['challenge'] or rec['msg_redirect']) and no_posts:
        return 'blocked'
    if rows == 0:
        return 'empty'
    return 'ok'


# ── 그룹별 ────────────────────────────────────────────────────────────────────

def _sleep(gap: float) -> None:
    time.sleep(gap * random.uniform(1.0, 1.5))


def probe_tbs(now: datetime, gap: float) -> list:
    today = now.strftime('%Y-%m-%d')

    def parse_tbs(body: bytes):
        data = json.loads(body)
        items = data.get('items') or []
        comms = {i.get('communityId') for i in items}
        upd = max((i.get('updateDatetime') or '' for i in items), default='') or None
        return len(items), f"hasNext={data.get('hasNext')} 커뮤니티 {len(comms)} 최신 updateDatetime {upd}"

    out = []
    rec, _ = probe('tbs', 'tbs-range', crawler.TBS_API_URL, parse=parse_tbs,
                   referer='https://todaybeststory.com/communities',
                   params={'startDate': today, 'endDate': today, 'page': 1, 'limit': crawler.TBS_PAGE_LIMIT})
    out.append(rec)
    _sleep(gap)
    rec, body = probe('tbs', 'tbs-health', TBS_HEALTH_URL)
    if body and rec.get('status') == 200:
        rec['note'] = _decode(body, 'utf-8')[:120]
    out.append(rec)
    return out


def _source_pages(src: dict, now: datetime) -> list:
    """COMMUNITY_SOURCES의 pages (정적 목록 또는 now를 받는 함수)"""
    pages = src.get('pages') or []
    if callable(pages):
        pages = pages(now)
    return [p(now) if callable(p) else p for p in pages]


def probe_fallback(now: datetime, gap: float) -> list:
    tc = crawler.TrendCrawler()
    out = []
    for src in crawler.COMMUNITY_SOURCES:
        try:
            pages = _source_pages(src, now)
        except Exception as e:
            out.append({'group': 'fallback', 'id': src.get('id'), 'url': '', 'verdict': 'error',
                        'error': f'pages 오류 {type(e).__name__}: {e}'})
            continue
        for url in pages:
            rec, _ = probe('fallback', src['id'], url,
                           parse=lambda body, s=src, u=url: len(tc._parse_rows(s, u, body, 0)))
            out.append(rec)
            print(_line(rec), flush=True)
            _sleep(gap)
    return out


def probe_issuelink(now: datetime, gap: float) -> list:
    out = []
    rec, body = probe('issuelink', 'robots.txt', issuelink.ROBOTS_URL)
    if body and rec.get('status') == 200:
        rec['note'] = ' / '.join(line.strip() for line in _decode(body, 'utf-8').splitlines() if line.strip())[:120]
    out.append(rec)
    il_gap = max(0.4, gap / 2)   # 한 서버라 목록은 짧은 간격으로 (동시 요청 없음)
    for site, sid in issuelink.SOURCE_MAP.items():
        _sleep(il_gap)
        url = issuelink.LIST_URL.format(site=site, hours=issuelink.HOURS)
        rows_box = {}

        def parse_il(body: bytes, site=site, box=rows_box):
            rows = issuelink.parse_list(body, site)
            box['rows'] = rows
            dates = [r['date'] for r in rows if r['date']]
            return len(rows), f"최신 글 {max(dates) if dates else '-'}"

        rec, _ = probe('issuelink', sid, url, parse=parse_il)
        out.append(rec)
        print(_line(rec), flush=True)
        rows = rows_box.get('rows') or []
        if rows:   # 첫 글 하나로 /go/ 리다이렉트(원본 URL 풀기)가 되는지
            _sleep(il_gap)
            go = issuelink.GO_URL.format(site=site, pid=rows[0]['pid'])
            grec, _ = probe('issuelink', f'{sid}/go', go, method='HEAD')
            resolved = issuelink.clean_url(site, grec.get('location') or '')
            grec['note'] = f'원본 {resolved}' if resolved else '원본 URL을 풀지 못함'
            if grec.get('verdict') == 'ok' and not resolved:
                grec['verdict'] = 'empty'
            out.append(grec)
            print(_line(grec), flush=True)
    return out


def probe_candidates(now: datetime, gap: float, skip_urls: set) -> list:
    tc = crawler.TrendCrawler()
    srcs = {s['id']: s for s in crawler.COMMUNITY_SOURCES}
    out, seen = [], set(skip_urls)
    for sid, url, parse_as in CANDIDATES:
        url = url.format(stdt=now.strftime('%Y%m%d'))
        if url in seen:
            continue
        seen.add(url)
        src = srcs.get(parse_as) if parse_as else None
        if src is not None:
            parse = (lambda body, s=src, u=url: len(tc._parse_rows(s, u, body, 0)))
        elif re.search(r'rss|\.xml', url, re.I):
            parse = _count_feed_items
        elif '/api' in url:
            parse = _count_json_items
        else:
            parse = None
        rec, _ = probe('candidates', sid, url, parse=parse)
        out.append(rec)
        print(_line(rec), flush=True)
        _sleep(gap)
    return out


def _count_feed_items(body: bytes) -> int:
    return len(re.findall(rb'<(?:item|entry)[\s>]', body[:BODY_SCAN * 2]))


def _count_json_items(body: bytes) -> int:
    data = json.loads(body)
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        for k in ('items', 'data', 'list', 'posts', 'result'):
            if isinstance(data.get(k), list):
                return len(data[k])
    return 0


def runner_info() -> dict | None:
    """러너의 국가·망(IP는 남기지 않는다)"""
    try:
        d = requests.get('https://ipinfo.io/json', timeout=8).json()
        return {k: d.get(k) for k in ('country', 'region', 'org')}
    except Exception as e:
        return {'error': type(e).__name__}


# ── 출력 ──────────────────────────────────────────────────────────────────────

def _line(rec: dict) -> str:
    return (f"[{rec['group']}] {rec['id']:<14} {rec.get('status', '-')!s:>4} {rec.get('verdict', '-'):<8} "
            f"{rec.get('bytes', '-')!s:>7}B rows={rec.get('rows', '-')} links={rec.get('article_links', '-')} "
            f"{(rec.get('title') or rec.get('error') or '')[:50]}")


def _cell(v, n: int = 60) -> str:
    s = '' if v is None else str(v)
    s = re.sub(r'\s+', ' ', s).replace('|', '\\|')
    return s if len(s) <= n else s[:n - 1] + '…'


def markdown(result: dict) -> str:
    lines = ['## 크롤링 소스 프로브', '']
    env = result.get('runner') or {}
    lines.append(f"- 시각: {result['when']} (KST)")
    if env:
        lines.append(f"- 러너: {env.get('country') or '-'} / {env.get('region') or '-'} / {env.get('org') or '-'}")
    counts = {}
    for r in result['results']:
        counts.setdefault(r['group'], {}).setdefault(r.get('verdict', '-'), 0)
        counts[r['group']][r.get('verdict', '-')] += 1
    for g, c in counts.items():
        lines.append(f"- {g}: " + ', '.join(f'{k} {v}' for k, v in sorted(c.items())))
    lines += ['', '판정: ok · blocked(차단·챌린지) · empty(파서 0행) · http-NNN · error(연결 실패). '
              '행 = 파서가 읽은 글 수, 글 링크 = 글 번호처럼 보이는 링크 수(파서 없는 후보의 참고값).', '']
    for g in GROUPS:
        rows = [r for r in result['results'] if r['group'] == g]
        if not rows:
            continue
        lines += [f'### {g}', '', '| id | 상태 | 판정 | 바이트 | 초 | 행 | 글 링크 | title | 메모 | URL |',
                  '|---|---|---|---:|---:|---:|---:|---|---|---|']
        for r in rows:
            memo = r.get('error') or r.get('note') or ''
            lines.append('| ' + ' | '.join([
                _cell(r['id'], 20), _cell(r.get('status', '-')), _cell(r.get('verdict', '-')),
                _cell(r.get('bytes', '')), _cell(r.get('sec', '')), _cell(r.get('rows', '')),
                _cell(r.get('article_links', '')), _cell(r.get('title'), 40), _cell(memo, 70), _cell(r['url'], 70),
            ]) + ' |')
        lines.append('')
    return '\n'.join(lines) + '\n'


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='크롤링 소스 URL 실측 (상태·바이트·title·차단·파싱 행 수)')
    ap.add_argument('--out', help='결과 JSON 경로 (기본: 임시 디렉터리/probe_sources.json)')
    ap.add_argument('--only', default='', help=f'쉼표로 그룹 선택: {",".join(GROUPS)} (기본: 전부)')
    ap.add_argument('--gap', type=float, default=1.0, help='요청 사이 기본 간격(초). 실제로는 1~1.5배')
    ap.add_argument('--no-ipinfo', action='store_true', help='러너 국가·망 조회(ipinfo.io)를 하지 않는다')
    args = ap.parse_args(argv)

    groups = [g.strip() for g in args.only.split(',') if g.strip()] or list(GROUPS)
    bad = [g for g in groups if g not in GROUPS]
    if bad:
        print(f'[probe] 모르는 그룹: {bad} (가능: {GROUPS})')
        return 2
    now = datetime.now(KST).replace(microsecond=0)
    result = {'when': now.isoformat(), 'groups': groups,
              'runner': None if args.no_ipinfo else runner_info(), 'results': []}
    print(f"[probe] {now.isoformat()} 그룹 {groups} 러너 {result['runner']}", flush=True)

    fallback_urls = set()
    if 'tbs' in groups:
        for rec in probe_tbs(now, args.gap):
            result['results'].append(rec)
            print(_line(rec), flush=True)
    if 'fallback' in groups:
        recs = probe_fallback(now, args.gap)
        result['results'] += recs
    try:
        fallback_urls = {u for s in crawler.COMMUNITY_SOURCES for u in _source_pages(s, now)}
    except Exception:
        pass
    if 'issuelink' in groups:
        result['results'] += probe_issuelink(now, args.gap)
    if 'candidates' in groups:
        result['results'] += probe_candidates(now, args.gap, fallback_urls)

    md = markdown(result)
    print('\n' + md)
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a', encoding='utf-8') as f:
            f.write(md)
    out = Path(args.out) if args.out else Path(tempfile.gettempdir()) / 'probe_sources.json'
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    md_out = out.with_suffix('.md')
    md_out.write_text(md, encoding='utf-8')
    print(f'[probe] 결과 {len(result["results"])}건 → {out} , {md_out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
