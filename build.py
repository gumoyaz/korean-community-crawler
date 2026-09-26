"""
커트 정적 사이트 빌더 — GitHub Actions가 약 10분마다 한 번 실행한다.

1. 이전 상태 복원(Actions 캐시 → --state-file, 없으면 STATE_URL) → 간격 가드
2. 크롤링 1회 + 데일리 리포트 생성 (KST 12시 이후 오늘 정오본, 다음 날 00:10~02시 전날 최종본)
3. 데일리 음성(Gemini TTS) — TTS_ENABLED일 때 한 실행에 최대 1개
   (데일리 생성을 시도한 실행(성공·실패)과 시작 후 TTS_START_BUDGET_SEC가 지난 실행은 제외)
4. _site/ 에 정적 사이트 렌더링 (index, daily, audio, trends.json, state.json, sitemap, robots, llms.txt, 404)
5. 수집 상태(health) — GITHUB_OUTPUT health·health_detail 등과 Step Summary. pages.yml health 잡이 알림 대상 문제가
   HEALTH_ALERT_STREAK번 연속이면 'source-health' 이슈를 열고, 풀리면 닫는다(연속 횟수는 state.json 'health')

로컬 미리보기:
    python build.py --base "" --out _site --no-crawl   # 운영 state.json으로 렌더링만 (부작용 없음)
    python build.py --base "" --out _site --force      # 직접 크롤까지 (12시 이후면 data/daily에 리포트가 생길 수 있음)
    python -m http.server -d _site 8000
    음성: AUDIO_DIR(기본 <repo>/.audio)에 지금 리포트와 맞는 {date}.mp3·{date}.json이 있으면 싣는다.
          생성은 TTS_ENABLED=true일 때만 한다(로컬 기본값은 생성 안 함).
"""
import argparse
import json
import os
import re
import shutil
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from xml.sax.saxutils import escape as xml_escape

ROOT = Path(__file__).resolve().parent
KST = timezone(timedelta(hours=9))

# Actions의 ${{ vars.X }}는 미설정이면 ''로 들어온다 → 빈 문자열은 미설정으로 취급.
# crawler/daily/gemini가 import 시점에 환경변수를 읽으므로 import 전에 정리한다.
_ENV_KEYS = ('SITE_URL', 'BASE', 'STATE_URL', 'GSC_VERIFICATION', 'GA_MEASUREMENT_ID', 'MIN_INTERVAL_MIN',
             'GOOGLE_API_KEY', 'GEMINI_MODEL', 'GEMINI_FALLBACK_MODEL', 'DAILY_DIR',
             'TTS_ENABLED', 'TTS_MODEL', 'TTS_FALLBACK_MODEL', 'TTS_VOICE', 'AUDIO_DIR')


def _drop_empty_env():
    for k in _ENV_KEYS:
        if k in os.environ and not os.environ[k].strip():
            del os.environ[k]


_drop_empty_env()  # 빈 값이 남아 있으면 load_dotenv가 .env 값으로 채우지 않는다
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env')
except ImportError:
    pass
_drop_empty_env()

import markdown  # noqa: E402
import nh3  # noqa: E402
import requests  # noqa: E402
from jinja2 import Environment, FileSystemLoader, select_autoescape  # noqa: E402

import daily  # noqa: E402
import gemini  # noqa: E402
import tts  # noqa: E402
from crawler import HEALTH_FROM_HOUR, MERGE_PREV_END_HOUR, SOURCE_META, TrendCrawler  # noqa: E402

DEFAULT_SITE_URL = 'https://gumoyaz.github.io/korean-community-crawler'
REFRESH_INTERVAL = 600      # 프론트가 trends.json을 다시 읽는 주기(초)
DEFAULT_MIN_INTERVAL = 7    # 분. cron-job.org(10분) + schedule(15분) 중복 실행 방지
MIN_POSTS_FOR_DAILY = 20
MIN_SOURCES_FOR_DAILY = 10  # 커뮤니티가 이보다 적은 목록(TBS 부분 실패·fallback)으로는 리포트를 만들지 않는다
DAILY_HOUR_KST = 12         # 이 시각 이전에는 오늘 리포트를 만들지 않는다(새벽 임시본 방지)
# 정오본은 그날 오전까지의 인기글만 본다. 다음 날 00:10~01:59(크롤러가 전날 목록을 같이 받는 시간)에
# 전날 리포트를 하루 전체 목록으로 다시 만든다(없으면 백필). generated_at 날짜가 리포트 날짜보다 뒤면 최종본.
DAILY_FINAL_START = (0, 10)   # (시, 분) KST
DAILY_FINAL_END_HOUR = MERGE_PREV_END_HOUR   # 크롤러가 전날 목록을 같이 받는 마지막 시각(이 시각 전까지)과 같게
# 데일리 생성이 실패하면 이 간격(분)을 두고 재시도한다. 매 실행(10분)마다 재시도하면 같은 입력으로
# 계속 실패할 때(SAFETY 차단 등) 크롤러 AI 요약과 같이 쓰는 무료 일일 한도(RPD)를 몇 시간 만에 다 쓴다.
DAILY_RETRY_MIN = 30
# 데일리 음성(Gemini TTS). 한 실행에 최대 1개, 최근 tts.BACKFILL_DAYS일 리포트 중 음성이 없거나 옛 내용인 것을
# 최신 날짜부터. TTS 무료 한도(RPM·RPD)는 문서에 없어(2026-09 기준) 하루 시도 횟수로 막는다.
TTS_RETRY_MIN = 30              # 실패 후 재시도 간격(분). Publish audio 실패가 처음이면 이 간격
TTS_QUOTA_RETRY_MIN = 180       # 일일 한도 소진(429 quota_day)이면 이 간격(분)
TTS_MAX_ATTEMPTS_PER_DAY = 6    # KST 하루 생성 시도 상한 (정오본·최종본 2회 + 백필·재시도)
# build.py 시작 후 이 시간(초)이 지났으면 음성을 만들지 않는다. 잡 타임아웃 15분(900초)에서
# TTS 최악 300초(150초 × 2모델)와 build.py 앞뒤 스텝(설치·업로드·push 약 3분)을 뺀 값에 여유를 둔 것
TTS_START_BUDGET_SEC = 420

# trends.json에 내보내는 키 — get_data()에서 프론트가 쓰는 것만.
# issues는 '지금 뜨는 이슈' 블록(글은 posts[].rank로 참조), trends는 카테고리 글 수만 남은 공개 JSON 호환용
# status: ok | partial | stale | stale-source (crawler.STATUSES), source_health: 커뮤니티별 수집 상태
TREND_KEYS = ('posts', 'issues', 'trends', 'last_updated', 'status', 'total', 'crawl_count',
              'sources', 'source_health', 'ai_summary', 'ai_summary_updated')
# prev_day: 전날 보충 글('어제' 표시, 있을 때만 true), via: 'direct'|'issuelink'(없으면 TBS),
# kept: 이번 수집에서 빠진 커뮤니티라 이전 글을 유지한 것(있을 때만 true)
POST_FIELDS = ('title', 'url', 'source', 'source_label', 'source_emoji', 'source_color',
               'views', 'likes', 'comments', 'date', 'summary', 'author', 'rank',
               'rank_score', 'post_velocity', 'is_food', 'is_beauty', 'is_fashion',
               'is_travel', 'is_game', 'is_celeb', 'is_humor', 'is_car', 'prev_day', 'via', 'kept')
# 수집 상태 알림: 알림 대상 문제가 이 횟수만큼 연속이면 pages.yml health 잡이 이슈를 연다.
# 알림 대상 = health stale, status partial, 또는 커뮤니티의 missing·blocked·'채우는 경로가 없는' degraded.
# 이슈링크·직접 수집 글이 들어오고 있는 degraded(TBS 갱신만 멈춤)는 사이트에 글이 계속 들어오므로 칩 표시만 하고
# 알리지 않는다 — 인스티즈는 TBS 갱신이 거의 매일 낮·저녁부터 자정 뒤까지 멈춰서(2026-09-22~25) 매일 이슈가 열리고 닫힌다.
# 하루 넘게 멈추면 날이 바뀐 뒤 당일 글이 없어 missing이 되므로 그때 알린다. 크롤러가 판정을 쉬는 KST 00~06시에는
# 알림 상태를 그대로 두므로(_health의 held) 자정 전부터 이어진 장애는 같은 이슈에 코멘트로 이어진다
HEALTH_ALERT_STREAK = 3
# 알림 중이던(연속 HEALTH_ALERT_STREAK번 이상) 문제가 없어진 실행부터 이 횟수의 실행 동안 health 잡이 열린 이슈 닫기를
# 시도한다(없으면 그냥 끝). 닫는 실행 한 번이 실패해도(gh API 오류, build 잡 실패로 health 잡이 건너뜀) 이슈가 남지 않게
HEALTH_CLOSE_RUNS = 3
HEALTH_LABEL = {'degraded': '갱신 멈춤', 'missing': '빠짐', 'blocked': '차단'}
# source_health.via 중 TBS 대신 채운 경로 — (이름, 뒤에 붙는 조사). 조사는 마지막 이름에 맞춘다(프론트 VIA_NAME과 같다)
HEALTH_FILL_VIA = {'direct': ('직접 수집', '으로'), 'issuelink': ('이슈링크', '로')}

# robots.txt에서 명시적으로 허용을 선언하는 AI 크롤러 ('*'와 같은 그룹)
AI_BOTS = ('GPTBot', 'OAI-SearchBot', 'ChatGPT-User', 'ClaudeBot', 'Claude-SearchBot',
           'Claude-User', 'anthropic-ai', 'PerplexityBot', 'Google-Extended')

# 데일리 마크다운 → HTML 정화 규칙 (LLM 출력에 섞인 raw HTML·javascript: 링크 제거)
_ALLOWED_TAGS = {'h1', 'h2', 'h3', 'h4', 'p', 'ul', 'ol', 'li', 'strong', 'em',
                 'blockquote', 'a', 'br', 'code', 'pre', 'hr'}
_HTTP_URL = re.compile(r'https?://', re.I)


# ── helpers ───────────────────────────────────────────────────────────────────

def _log(msg: str):
    print(msg, flush=True)


def _keep_http_href(tag: str, attr: str, value: str):
    """a[href]는 http/https 절대 URL만 남긴다(상대경로·//host·기타 스킴 제거)."""
    if tag == 'a' and attr == 'href':
        return value if _HTTP_URL.match(value.strip()) else None
    return value


def _md_to_html(text: str) -> str:
    """마크다운 → 정화된 HTML. 템플릿에서 |safe로 출력해도 되는 결과만 돌려준다."""
    raw = markdown.markdown(text or '', extensions=['nl2br', 'sane_lists', 'fenced_code'])
    return nh3.clean(
        raw,
        tags=_ALLOWED_TAGS,
        attributes={'a': {'href'}},
        url_schemes={'http', 'https'},
        attribute_filter=_keep_http_href,
        link_rel='noopener',
    )


def _daily_seo_desc(record: dict, date_kr: str) -> str:
    """데일리 상세의 meta description — 날마다 같은 문장이 되지 않게 그날 요약의 소제목(최대 3개)을 넣는다.
    글 제목은 쓰지 않는다(욕설·자극적인 표현이 검색 결과 설명에 그대로 나간다)."""
    topics = [re.sub(r'\s+', ' ', h).strip()
              for h in re.findall(r'^##\s+(.+)$', record.get('summary_md') or '', re.M)][:3]
    head = f'{date_kr} 커뮤니티 인기글 요약'
    return f'{head}: {", ".join(topics)} 등 그날의 화제를 한 번에.' if topics else f'{head}. 그날의 화제를 한 번에.'


def _format_date_kr(date_str: str) -> str:
    """'2025-04-26'  →  '2025년 4월 26일'"""
    try:
        dt = datetime.strptime(date_str, '%Y-%m-%d')
        return f'{dt.year}년 {dt.month}월 {dt.day}일'
    except (TypeError, ValueError):
        return date_str


def _parse_iso(value) -> datetime | None:
    """오프셋 포함 ISO 8601 → aware datetime. 오프셋이 없으면 KST로 본다."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=KST)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec='seconds')


def _gh_output(**values):
    """GitHub Actions 스텝 출력(GITHUB_OUTPUT)에 key=value를 기록. 로컬에서는 무시."""
    path = os.environ.get('GITHUB_OUTPUT')
    if not path:
        return
    with open(path, 'a', encoding='utf-8') as f:
        for k, v in values.items():
            f.write(f'{k}={v}\n')


def _write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)


def _write_json(path: Path, obj):
    _write(path, json.dumps(obj, ensure_ascii=False, separators=(',', ':')))


# ── 설정 · 상태 ──────────────────────────────────────────────────────────────

def _parse_args(argv):
    p = argparse.ArgumentParser(description='커트 정적 사이트 빌드')
    p.add_argument('--out', default=str(ROOT / '_site'), help='출력 폴더 (기본: <repo>/_site)')
    p.add_argument('--base', default=None,
                   help="내부 링크 접두 경로. 기본은 SITE_URL의 path. 루트 배포/로컬 미리보기는 --base ''")
    p.add_argument('--no-crawl', action='store_true',
                   help='크롤링·데일리 생성 없이 복원한 상태로 렌더링만 (간격 가드 미적용)')
    p.add_argument('--force', action='store_true', help='간격 가드 무시')
    p.add_argument('--state-file', help='상태를 이 JSON 파일에서 먼저 읽는다 (없거나 깨졌으면 STATE_URL)')
    p.add_argument('--templates', default=str(ROOT / 'templates'), help='템플릿 폴더')
    p.add_argument('--now', help='현재 시각 ISO 8601 (테스트용, 오프셋 없으면 KST)')
    return p.parse_args(argv)


def _settings(args) -> dict:
    site_url = os.environ.get('SITE_URL', DEFAULT_SITE_URL).strip().rstrip('/')
    if not _HTTP_URL.match(site_url):
        raise SystemExit(f'SITE_URL은 http(s)://로 시작해야 합니다: {site_url!r}')

    base = args.base if args.base is not None else os.environ.get('BASE', urlsplit(site_url).path)
    base = base.strip().strip('/')
    base = f'/{base}' if base else ''

    try:
        min_interval = float(os.environ.get('MIN_INTERVAL_MIN', DEFAULT_MIN_INTERVAL))
    except ValueError:
        min_interval = DEFAULT_MIN_INTERVAL

    ga_id = os.environ.get('GA_MEASUREMENT_ID', '').strip().upper()
    return {
        'site_url': site_url,
        'base': base,
        'state_url': os.environ.get('STATE_URL', f'{site_url}/data/state.json').strip(),
        'gsc_verification': os.environ.get('GSC_VERIFICATION', '').strip(),
        # GA4 측정 ID('G-XXXXXXXXXX'). 형식이 아니면 태그를 넣지 않는다(스크립트 URL·JS에 그대로 들어가서)
        'ga_id': ga_id if re.fullmatch(r'G-[A-Z0-9]{4,16}', ga_id) else '',
        'min_interval': min_interval,
    }


def _now(arg: str | None) -> datetime:
    if not arg:
        return datetime.now(KST)
    dt = _parse_iso(arg)
    if dt is None:
        raise SystemExit(f'--now 형식이 잘못됐습니다: {arg!r}')
    return dt.astimezone(KST)


STATE_FETCH_TRIES = 3


def _load_state(state_file: str | None, state_url: str) -> dict:
    """이전 실행 상태.
    - --state-file(Actions가 actions/cache로 넘긴 직전 state, 또는 로컬 지정)을 먼저 읽는다.
      없거나 깨졌으면 STATE_URL로 넘어간다(캐시가 비었거나 만료된 경우).
    - STATE_URL이 404면 첫 배포로 보고 빈 dict (크롤은 빈 이력으로 시작).
    - 그 밖의 실패(연결 오류·5xx·깨진 JSON)는 몇 번 재시도한 뒤 SystemExit(1) — 빈 상태로 배포하면
      사이트가 비고 이력·AI 요약·재시도 기록이 지워지므로, 배포를 막아 기존 사이트와 state를 지킨다.
    """
    state = None
    if state_file:
        try:
            with open(state_file, encoding='utf-8') as f:
                state = json.load(f)
            src = state_file
        except (OSError, ValueError) as e:
            _log(f'[State] {state_file} 읽기 실패 ({type(e).__name__}) — {state_url} 에서 받는다')
    if state is None:
        # Pages CDN은 쿼리스트링을 캐시 키에 넣지 않아 ?t=로는 캐시(max-age=600)를 피할 수 없다(2026-09 실측).
        # 그래서 Actions는 직전 state를 actions/cache로 넘기고, 이 경로는 캐시가 없을 때만 쓴다.
        for attempt in range(1, STATE_FETCH_TRIES + 1):
            try:
                r = requests.get(state_url, headers={'Cache-Control': 'no-cache'}, timeout=10)
                if r.status_code == 404:
                    _log(f'[State] {state_url} → HTTP 404 (첫 배포) — 빈 상태로 시작')
                    return {}
                r.raise_for_status()
                state = r.json()
                break
            except (ValueError, requests.RequestException) as e:
                _log(f'[State] 복원 실패 {attempt}/{STATE_FETCH_TRIES} ({type(e).__name__}: {e})')
                if attempt < STATE_FETCH_TRIES:
                    time.sleep(3 * attempt)
        if state is None:
            raise SystemExit('[State] 이전 상태를 받지 못함 — 빈 상태로 배포하지 않도록 중단 (기존 사이트 유지)')
        src = state_url
    if not isinstance(state, dict) or state.get('version') != 1:
        _log('[State] 형식·버전 불일치 — 빈 상태로 시작')
        return {}
    _log(f'[State] 복원: {src} (last_run={state.get("last_run")})')
    return state


# ── 크롤 · 데일리 ────────────────────────────────────────────────────────────

def _crawl(state: dict, no_crawl: bool, now: datetime | None = None) -> TrendCrawler:
    """now: --now로 준 시각(테스트). 크롤러의 커뮤니티 판정·오전 보충·TBS 날짜가 이 시각을 따른다. 없으면 실제 시각."""
    crawler = TrendCrawler()
    try:
        crawler.import_state(state.get('crawler'))
    except Exception as e:
        _log(f'[State] 크롤러 상태 복원 오류: {e} — 빈 상태로 크롤')
        crawler = TrendCrawler()
    if no_crawl:
        _log('[Crawl] --no-crawl — 복원한 데이터로 렌더링만 한다')
        return crawler
    t0 = time.time()
    try:
        crawler.refresh(now=now)
    except Exception as e:
        # 크롤이 실패해도 이전 데이터로 사이트는 계속 띄운다
        _log(f'[Crawl] 오류: {type(e).__name__}: {e} — 이전 데이터로 렌더링')
    d = crawler.get_data()
    _log(f'[Crawl] status={d.get("status")} posts={len(d.get("posts") or [])} '
         f'({time.time() - t0:.1f}s)')
    return crawler


def _is_final(record: dict | None, date: str) -> bool:
    """리포트 날짜가 끝난 뒤(다음 날 이후)에 만든 최종본인지 — generated_at의 KST 날짜로 판단."""
    dt = _parse_iso((record or {}).get('generated_at'))
    return bool(dt) and dt.astimezone(KST).strftime('%Y-%m-%d') > date


def _maybe_generate_daily(posts: list, now: datetime,
                          failed_at: datetime | None) -> tuple[str | None, datetime | None]:
    """조건을 만족하면 리포트를 만든다.
    - KST 12시 이후: 오늘 리포트가 없으면 만든다(오전까지의 인기글 기준).
    - 다음 날 00:10~01:59: 전날 리포트가 최종본이 아니면(정오본이거나 없으면) 하루 전체 목록으로 다시 만든다.
    전날 보충 글(prev_day — 크롤러가 02~12시에 당일 글이 모자란 칸을 채운 '어제' 글)은 입력에서 뺀다.
    반환: (만든 날짜 또는 None, 마지막 생성 실패 시각 — state.json에 남겨 재시도 간격 계산에 쓴다)"""
    posts = [p for p in posts if not p.get('prev_day')]
    today = now.strftime('%Y-%m-%d')
    if DAILY_FINAL_START <= (now.hour, now.minute) and now.hour < DAILY_FINAL_END_HOUR:
        target = (now - timedelta(days=1)).strftime('%Y-%m-%d')
        if _is_final(daily.get_summary(target), target):
            _log(f'[Daily] {target} 최종본 이미 있음')
            return None, failed_at
        # 크롤러가 전날+오늘 목록을 같이 받는 시간 → 오늘 날짜 글은 뺀다
        posts = [p for p in posts if (p.get('date') or '')[:10] != today]
        kind = '최종본(하루 전체 목록)'
    elif now.hour >= DAILY_HOUR_KST:
        target = today
        if daily.has_summary(today):
            _log(f'[Daily] {today} 이미 있음')
            return None, failed_at
        kind = '정오본'
    else:
        _log(f'[Daily] {today} KST {DAILY_HOUR_KST}시 이전 — 생성하지 않음')
        return None, failed_at

    n_src = len({p.get('source') for p in posts})
    if len(posts) < MIN_POSTS_FOR_DAILY or n_src < MIN_SOURCES_FOR_DAILY:
        _log(f'[Daily] {target} skip: 게시글 {len(posts)}개/커뮤니티 {n_src}곳 '
             f'(최소 {MIN_POSTS_FOR_DAILY}개/{MIN_SOURCES_FOR_DAILY}곳) — 다음 실행에 재시도')
        return None, failed_at
    if not gemini.available():
        _log(f'[Daily] {target} skip: GOOGLE_API_KEY 없음')
        return None, failed_at
    if failed_at and failed_at.astimezone(KST).strftime('%Y-%m-%d') == today:
        waited = (now - failed_at).total_seconds() / 60
        if 0 <= waited < DAILY_RETRY_MIN:
            _log(f'[Daily] {target} skip: {waited:.0f}분 전 생성 실패 — {DAILY_RETRY_MIN}분 간격으로 재시도')
            return None, failed_at
    _log(f'[Daily] {target} {kind} 생성 시작 (게시글 {len(posts)}개, 커뮤니티 {n_src}곳)')
    try:
        md = daily.generate_deep_summary(posts, target)
        if not md:
            _log(f'[Daily] {target} 생성 실패 — {DAILY_RETRY_MIN}분 뒤 재시도')
            return None, now
        # generated_at은 판단에 쓴 시각(now)으로 — 최종본 여부(_is_final)가 이 값의 날짜로 정해진다
        path = daily.save_summary(target, md, posts, generated_at=_iso(now))
    except Exception as e:
        _log(f'[Daily] {target} 오류: {type(e).__name__}: {e} — {DAILY_RETRY_MIN}분 뒤 재시도')
        return None, now
    _log(f'[Daily] {target} 저장: {path}')
    return target, None


def _tts_state(raw) -> dict:
    """state.json 'tts' 값 정리 — {failed_at, error, day, attempts, date, pending, publish_fails}."""
    s = raw if isinstance(raw, dict) else {}
    attempts = s.get('attempts')
    pending = s.get('pending')
    fails = s.get('publish_fails')
    return {
        'failed_at': s.get('failed_at') if isinstance(s.get('failed_at'), str) else None,
        'error': str(s.get('error') or ''),          # 마지막 실패 종류(tts.last_error 또는 'publish_failed')
        'day': str(s.get('day') or ''),              # attempts를 센 KST 날짜
        'attempts': attempts if isinstance(attempts, int) and attempts >= 0 else 0,
        'date': str(s.get('date') or ''),            # 마지막으로 시도한 리포트 날짜
        # 직전에 만든 음성 {date, sha} — 다음 실행의 AUDIO_DIR(audio 브랜치)에 없으면 Publish audio가 실패한 것
        'pending': ({'date': pending['date'], 'sha': pending['sha']}
                    if isinstance(pending, dict) and isinstance(pending.get('date'), str)
                    and isinstance(pending.get('sha'), str) else None),
        'publish_fails': fails if isinstance(fails, int) and fails >= 0 else 0,   # Publish audio 연속 실패 수
    }


def _tts_targets(today: str) -> list[tuple[str, str]]:
    """음성이 없거나 옛 내용(source_sha 불일치)인 최근 tts.BACKFILL_DAYS일 리포트 [(날짜, summary_md)] — 최신순."""
    oldest = (datetime.strptime(today, '%Y-%m-%d')
              - timedelta(days=tts.BACKFILL_DAYS - 1)).strftime('%Y-%m-%d')
    out = []
    for s in daily.list_summaries(limit=None):   # 최신순
        d = s.get('date', '')
        if d > today or not daily.is_valid_date(d):
            continue
        if d < oldest:
            break
        record = daily.get_summary(d)
        if not record:
            continue
        md = record['summary_md']
        if tts.is_current(tts.read_meta(d), md):
            continue
        if len(tts.speech_script(md, d)['text']) > tts.MAX_SCRIPT_CHARS:
            _log(f'[TTS] {d} 대본이 {tts.MAX_SCRIPT_CHARS}자를 넘음 — 음성 생략(브라우저 음성)')
            continue
        out.append((d, md))
    return out


def _maybe_generate_audio(now: datetime, created: str | None,
                          tts_state: dict, busy: str = '') -> tuple[list[str], dict]:
    """데일리 음성 — 조건을 만족하면 최신 대상 1개를 만든다.
    순서: tts.enabled() → tts.prune(today) → 이번 실행에서 데일리를 만들었거나 busy면 생략
    → 직전 실행 음성의 publish 실패 확인 → 재시도 간격·하루 상한 → 최신순 첫 대상 1개 tts.generate().
    busy: created 말고 이번 실행에서 음성을 만들지 않을 이유(데일리 생성 실패로 시간을 씀, 시간 예산 초과 등).
    반환: (audio 브랜치에서 바뀐 것 ['2026-09-24', 'prune:2026-09-10', ...], 새 tts_state)"""
    if not tts.enabled():
        _log('[TTS] 꺼짐 (TTS_ENABLED 아님 또는 GOOGLE_API_KEY 없음) — 음성 생성·정리 안 함')
        return [], tts_state
    state = _tts_state(tts_state)
    today = now.strftime('%Y-%m-%d')
    changed = [f'prune:{d}' for d in tts.prune(today)]
    if changed:
        _log(f'[TTS] {tts.KEEP_DAYS}일 지난 음성 삭제: {" ".join(changed)}')
    if created or busy:
        # 한 실행에 Gemini 긴 작업은 하나만(15분 타임아웃). 음성은 다음 실행(약 10분 뒤)에 만든다
        _log(f'[TTS] 이번 실행에서 {busy or f"{created} 데일리를 만듦"} — 음성은 다음 실행에서')
        return changed, state

    if state['day'] != today:
        state['day'], state['attempts'] = today, 0
    targets = _tts_targets(today)

    # 직전 실행이 만든 음성(같은 내용)이 아직 대상이면 audio 브랜치에 올라가지 않은 것 — Publish audio 실패
    # (브랜치 규칙의 강제 push 금지·lease 거부 등). 그대로 두면 매 실행 같은 음성을 다시 합성해 하루 상한을 다 쓴다
    pend, state['pending'] = state['pending'], None
    if pend:
        if any(d == pend['date'] and tts.source_sha(md) == pend['sha'] for d, md in targets):
            state['publish_fails'] += 1
            state.update(failed_at=_iso(now), error='publish_failed', date=pend['date'])
            _log(f'[TTS] 직전 실행에서 만든 {pend["date"]} 음성이 audio 브랜치에 없음 — Publish audio 실패로 봄 '
                 f'(연속 {state["publish_fails"]}번, pages.yml 로그·브랜치 규칙 확인)')
        else:
            state['publish_fails'] = 0
    if not targets:
        _log(f'[TTS] 최근 {tts.BACKFILL_DAYS}일 리포트 음성 모두 최신')
        return changed, state
    # 직전 실패가 일시적이지 않으면(차단·음성 이상 등 같은 입력으로 또 실패할 수 있는 것)
    # 그 날짜를 뒤로 미뤄 다른 날짜가 막히지 않게 한다. publish 실패는 날짜 탓이 아니므로 최신순 그대로
    if state['error'] not in ('', 'transient', 'quota_day', 'publish_failed') and len(targets) > 1:
        targets.sort(key=lambda t: t[0] == state['date'])   # 안정 정렬 — 나머지는 최신순 유지

    failed_at = _parse_iso(state['failed_at'])
    if (state['error'] == 'publish_failed' and state['publish_fails'] >= 2 and failed_at
            and failed_at.astimezone(KST).strftime('%Y-%m-%d') == today):
        # 한 번 다시 만들어도 또 못 올렸으면 설정 문제일 가능성이 크다 → 그날(KST)은 더 부르지 않는다
        _log(f'[TTS] skip: Publish audio 연속 {state["publish_fails"]}번 실패 — 오늘(KST)은 다시 만들지 않음 '
             f'(대기 {len(targets)}개)')
        return changed, state
    wait = TTS_QUOTA_RETRY_MIN if state['error'] == 'quota_day' else TTS_RETRY_MIN
    if failed_at:
        waited = (now - failed_at).total_seconds() / 60
        if 0 <= waited < wait:
            _log(f'[TTS] skip: {waited:.0f}분 전 실패({state["error"]}) — {wait}분 간격으로 재시도 '
                 f'(대기 {len(targets)}개)')
            return changed, state
    if state['attempts'] >= TTS_MAX_ATTEMPTS_PER_DAY:
        _log(f'[TTS] skip: 오늘(KST) 시도 {state["attempts"]}회 — 하루 상한 {TTS_MAX_ATTEMPTS_PER_DAY}회 '
             f'(대기 {len(targets)}개)')
        return changed, state

    date, md = targets[0]
    state['attempts'] += 1
    _log(f'[TTS] {date} 음성 생성 (오늘 {state["attempts"]}/{TTS_MAX_ATTEMPTS_PER_DAY}번째 시도, '
         f'대기 {len(targets)}개: {" ".join(d for d, _ in targets)})')
    try:
        meta = tts.generate(date, md, now)
        err = tts.last_error
    except Exception as e:
        _log(f'[TTS] {date} 오류: {type(e).__name__}: {e}')
        meta, err = None, 'exception'
    if meta:
        # pending: 다음 실행이 이 음성이 audio 브랜치에 올라갔는지 확인한다(publish_fails는 확인될 때까지 유지)
        state.update(failed_at=None, error='', date=date, pending={'date': date, 'sha': tts.source_sha(md)})
        changed.append(date)
    else:
        err = err or 'unknown'
        state.update(failed_at=_iso(now), error=err, date=date)
        _log(f'[TTS] {date} 음성 생성 실패({err}) — '
             f'{TTS_QUOTA_RETRY_MIN if err == "quota_day" else TTS_RETRY_MIN}분 뒤 재시도, 그동안 브라우저 음성')
    return changed, state


# ── 수집 상태 (health) ────────────────────────────────────────────────────────

def _health(data: dict, prev, now: datetime) -> tuple[dict, dict]:
    """이번 크롤 결과의 수집 상태와 연속 횟수.
    health: ok | degraded(status partial이거나 커뮤니티 하나라도 degraded·missing·blocked) | stale(stale·stale-source)
    streak: 알림 대상 문제(HEALTH_ALERT_STREAK 위 주석)가 이어진 횟수 — 알림 대상이 아니면 0 (health가 degraded여도)
    반환: (state.json 'health' {status, streak, key, since, detail, close_left}, GITHUB_OUTPUT 값)
    key는 알림 대상 문제(health·status·커뮤니티 목록), 없으면 'ok'.
    - health_notify: 열린 이슈에 코멘트할 실행 — 알림 중(streak ≥ HEALTH_ALERT_STREAK)이고 key가 바뀌었거나 이번이
      그 장애의 첫 알림 실행(연속 횟수가 막 HEALTH_ALERT_STREAK에 닿음, 이전 이슈가 닫히지 않고 남아 있을 때도 알리게)
    - health_close: 열린 이슈를 닫을 실행 — 알림 중이던 문제가 없어진 실행과 그 뒤 HEALTH_CLOSE_RUNS-1번(close_left)
    - KST 00~06시(crawler.HEALTH_FROM_HOUR 전)에는 크롤러가 degraded·stale-source를 판정하지 않고 00~02시에는 전날 목록도
      같이 본다. 알림 중(연속 HEALTH_ALERT_STREAK번 이상)이던 상태에서 이 시간의 '알림 대상 없음'은 회복으로 세지 않고
      직전 연속 횟수·key를 그대로 둔다(held) — 저녁부터 이어진 장애의 이슈가 00시대에 닫혔다가 02시 뒤 missing으로
      새로 열리지 않게. 이 시간에 실제로 회복했으면 06시 뒤에 닫힌다."""
    prev = prev if isinstance(prev, dict) else {}
    status = str(data.get('status') or '')
    sh = data.get('source_health') if isinstance(data.get('source_health'), dict) else {}
    problems = {s: e for s, e in sh.items() if isinstance(e, dict) and e.get('status') in HEALTH_LABEL}
    if status in ('stale', 'stale-source'):
        health = 'stale'
    elif status == 'partial' or problems:
        health = 'degraded'
    else:
        health = 'ok'

    def filled(e) -> list:
        # source_health.via 순서(crawler.VIA_ORDER: 직접 수집 → 이슈링크)대로 — 프론트 안내 문구와 같은 순서
        return [v for v in dict.fromkeys(e.get('via') or []) if v in HEALTH_FILL_VIA]

    # 갱신 멈춤(degraded)은 알리지 않고 칩에만 표시한다. 원본(TBS)의 커뮤니티 수집이 멈춘 것이라 우리가 고칠 수 없고,
    # 인스티즈는 거의 매일 몇 시간씩 멈춰서(09-25~26 하루에 이슈 4번) 알림이 소음이 됐다. 해외 러너에서는 이슈링크가
    # 봇 확인 페이지를 받아 채울 경로도 없다. 하루 넘게 멈추면 날이 바뀐 뒤 기대 시각(EXPECTED_BY_HOUR)에 missing이 되어
    # 알림 대상이 된다. 여러 곳이 한꺼번에 멈추면 status partial로 알린다
    alert_problems = {s: e for s, e in problems.items() if e['status'] != 'degraded'}
    alerting = health == 'stale' or status == 'partial' or bool(alert_problems)
    key = (f'{health}|{status}|' + ','.join(f'{s}={e["status"]}' for s, e in sorted(alert_problems.items()))
           if alerting else 'ok')
    prev_streak = prev.get('streak') if isinstance(prev.get('streak'), int) and prev['streak'] >= 0 else 0
    prev_key = prev.get('key') if isinstance(prev.get('key'), str) else None
    prev_close = prev.get('close_left') if isinstance(prev.get('close_left'), int) and prev['close_left'] > 0 else 0
    # 알림 중(이슈가 열렸을 연속 횟수)일 때만 이어 둔다 — 아직 알리기 전(1~2번)이면 닫을 이슈가 없어 이어 둘 까닭이 없고,
    # 이어 두면 밤사이 새로 생긴 다른 문제가 한 번만 나와도 곧바로 알림 횟수에 닿는다
    held = (not alerting and prev_streak >= HEALTH_ALERT_STREAK and prev_key is not None
            and now.hour < HEALTH_FROM_HOUR)
    if held:
        streak, key = prev_streak, prev_key
    else:
        streak = prev_streak + 1 if alerting else 0
    changed = key != prev_key
    notify = streak >= HEALTH_ALERT_STREAK and (changed or prev_streak < HEALTH_ALERT_STREAK)
    if streak:
        close, close_left = False, 0
    elif prev_streak >= HEALTH_ALERT_STREAK:   # 알림 중이던 문제가 막 없어진 실행 (알리기 전 1~2번이면 열린 이슈가 없다)
        close, close_left = True, HEALTH_CLOSE_RUNS - 1
    else:
        close, close_left = prev_close > 0, max(0, prev_close - 1)
    since = prev.get('since') if prev.get('status') == health and isinstance(prev.get('since'), str) else _iso(now)

    parts = [f'status {status or "?"}']
    for s, e in sorted(problems.items(), key=lambda kv: (kv[1]['status'], kv[0])):
        label = SOURCE_META.get(s, (s,))[0]
        last = _parse_iso(e.get('last_update'))
        ago = f', 마지막 갱신 {(now - last).total_seconds() / 3600:.1f}시간 전' if last else ''
        note = f', {e["note"]}' if isinstance(e.get('note'), str) and e.get('note') else ''
        fv = filled(e)
        fill = (f', {"·".join(HEALTH_FILL_VIA[v][0] for v in fv)}{HEALTH_FILL_VIA[fv[-1]][1]} 보충' if fv else '')
        parts.append(f'{HEALTH_LABEL[e["status"]]}: {label}({s}{ago}{note}{fill})')
    if held:
        parts.append(f'KST {HEALTH_FROM_HOUR:02d}시 전이라 판정을 쉬는 시간 - 직전 알림 상태 유지(연속 {streak}회)')
    # GITHUB_OUTPUT은 한 줄 key=value — 줄바꿈이 들어가면 다음 출력이 깨진다
    detail = re.sub(r'[\r\n]+', ' ', ' · '.join(parts))[:1500]
    state = {'status': health, 'streak': streak, 'key': key, 'since': since, 'detail': detail,
             'close_left': close_left}
    # held 실행은 판정을 쉬는 것이라 health 잡을 돌리지 않는다(alert false) — 06시 뒤 판정으로 이어서 알리거나 닫는다
    outputs = {'health': health, 'health_detail': detail, 'health_streak': str(streak),
               'health_alert': 'true' if streak >= HEALTH_ALERT_STREAK and not held else 'false',
               'health_changed': 'true' if changed else 'false',
               'health_notify': 'true' if notify else 'false',
               'health_close': 'true' if close else 'false'}
    return state, outputs


def _health_summary(data: dict, hstate: dict, now: datetime) -> str:
    """GITHUB_STEP_SUMMARY용 마크다운 — 커뮤니티별 상태 표 (문제 있는 곳 먼저)."""
    sh = data.get('source_health') if isinstance(data.get('source_health'), dict) else {}
    lines = [f'### 수집 상태: {hstate["status"]} (status {data.get("status")}, 알림 대상 연속 {hstate["streak"]}회)', '',
             f'{hstate["detail"]}', '',
             '| 커뮤니티 | 상태 | 마지막 갱신 | 마지막 새 글 | 글 | 경로 | 비고 |',
             '|---|---|---|---|---:|---|---|']

    def when(v):
        dt = _parse_iso(v)
        if not dt:
            return '-'
        return f'{dt.astimezone(KST):%m-%d %H:%M} ({(now - dt).total_seconds() / 3600:.1f}시간 전)'

    for s, e in sorted(sh.items(), key=lambda kv: (kv[1].get('status') == 'ok', kv[0])):
        if not isinstance(e, dict):
            continue
        label = SOURCE_META.get(s, (s,))[0]
        note = str(e.get('note') or '').replace('|', '/')
        lines.append(f'| {label} ({s}) | {e.get("status")} | {when(e.get("last_update"))} | '
                     f'{when(e.get("last_new"))} | {e.get("n", 0)} | {"·".join(e.get("via") or []) or "-"} | {note} |')
    return '\n'.join(lines) + '\n'


def _write_step_summary(text: str):
    path = os.environ.get('GITHUB_STEP_SUMMARY')
    if not path:
        return
    try:
        with open(path, 'a', encoding='utf-8') as f:
            f.write(text)
    except OSError as e:
        _log(f'[Health] Step Summary 기록 실패: {e}')


# ── 렌더링 ────────────────────────────────────────────────────────────────────

def _prepare_out(out: Path):
    """출력 폴더를 비운다. 이전 빌드 결과(.nojekyll 있음)나 빈 폴더만 지운다."""
    out = out.resolve()
    if out == ROOT or out in ROOT.parents:
        raise SystemExit(f'--out 이 리포 루트(또는 그 상위)입니다: {out}')
    if out.exists():
        if any(out.iterdir()) and not (out / '.nojekyll').exists():
            raise SystemExit(f'{out} 는 비어 있지 않고 이전 빌드 결과도 아닙니다 — 다른 --out 을 지정하세요')
        shutil.rmtree(out)
    out.mkdir(parents=True)
    return out


def _public_data(data: dict, checked_at: str | None = None) -> dict:
    """get_data() → trends.json. 프론트가 쓰는 키·필드만, http(s) 링크 글만.
    checked_at: 마지막 수집 실행 시각. last_updated(데이터가 바뀐 시각)는 원본 목록이 약 1시간마다
    바뀌어서 수십 분 전이 정상이므로, 프론트는 이 값으로 수집 지연을 판단하고 다음 갱신 시각을 잡는다."""
    posts = []
    for p in data.get('posts') or []:
        if not str(p.get('url') or '').lower().startswith(('http://', 'https://')):
            continue
        posts.append({k: p[k] for k in POST_FIELDS if k in p})
    public = {k: data.get(k) for k in TREND_KEYS}
    public['posts'] = posts
    public['total'] = len(posts)
    public['checked_at'] = checked_at
    return public


def _load_summaries(today: str) -> list[dict]:
    """데일리 목록(최신순). 형식이 이상하거나 미래 날짜인 항목은 뺀다(경로·sitemap 오염 방지)."""
    result = []
    for s in daily.list_summaries(limit=None):
        d = s.get('date', '')
        if not daily.is_valid_date(d) or d > today:
            _log(f'[Daily] 목록에서 제외: {d!r}')
            continue
        s = dict(s)
        s['date_kr'] = _format_date_kr(d)
        result.append(s)
    return result


def _sitemap(site_url: str, build_time: str, summaries: list) -> str:
    def lastmod(s):
        dt = _parse_iso(s.get('generated_at'))
        return _iso(dt) if dt else s['date']

    urls = [(f'{site_url}/', build_time),
            (f'{site_url}/daily/', lastmod(summaries[0]) if summaries else build_time)]
    urls += [(f"{site_url}/daily/{s['date']}/", lastmod(s)) for s in summaries]

    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for loc, mod in urls:
        parts.append(f'  <url><loc>{xml_escape(loc)}</loc><lastmod>{xml_escape(mod)}</lastmod></url>')
    parts.append('</urlset>')
    return '\n'.join(parts) + '\n'


def _robots_txt(site_url: str, base: str) -> str:
    # 봇별 그룹을 따로 두면 그 봇은 '*' 그룹 규칙을 무시한다(RFC 9309) → 한 그룹에 모은다.
    # 주의: 프로젝트 페이지(서브경로)에서는 크롤러가 호스트 루트의 robots.txt만 읽는다.
    agents = '\n'.join(f'User-agent: {ua}' for ua in ('*',) + AI_BOTS)
    return (f'# 커트 — 검색·AI 크롤러 모두 허용\n'
            f'{agents}\n'
            f'Allow: /\n'
            f'Disallow: {base}/data/state.json\n'
            f'\n'
            f'Sitemap: {site_url}/sitemap.xml\n')


def _llms_txt(site_url: str, summaries: list, data: dict) -> str:
    """AI 크롤러용 사이트 안내(llms.txt). 실제로 있는 페이지·데이터만 안내한다."""
    recent = '\n'.join(
        f"- [{s['date_kr']} 데일리 리포트]({site_url}/daily/{s['date']}/)"
        for s in summaries[:5]
    ) or '- (아직 생성된 리포트 없음)'

    # 소스 수를 하드코딩하지 않고 최근 수집 결과에서 센다
    counts = Counter(p.get('source_label') or p.get('source') for p in data.get('posts') or [])
    counts.pop(None, None)
    counts.pop('', None)
    if counts:
        labels = ', '.join(label for label, _ in counts.most_common())
        sources = f'최근 수집 기준 {len(counts)}곳: {labels}'
    else:
        sources = 'FM코리아, 디시인사이드, 루리웹, 더쿠, 클리앙 등 한국 주요 커뮤니티'

    ai_line = ('- **AI 트렌드 요약** — 메인 상단에 지금 화제인 주제를 Gemini가 짧게 정리\n'
               if data.get('ai_summary') else '')

    return f"""# 커트 (커뮤니티 트렌드)

> 한국 주요 인터넷 커뮤니티의 인기글을 주기적으로 모아 보여 주는 트렌드 집계 사이트.

커트(KEOT)는 여러 한국 커뮤니티의 베스트·인기 게시글을 주기적으로 수집해 한 화면에 보여 줍니다. 정적 사이트로 운영되며 약 10분마다 새 인기글을 확인합니다. 원본 인기글 목록이 대략 1시간 단위로 바뀌므로 실제 내용도 그 주기로 갱신됩니다.

## 주요 기능

- **지금 뜨는 이슈** — 여러 커뮤니티에 같이 올라온 인기글을 이야기 단위로 묶어, 이슈마다 대표 글·퍼진 커뮤니티·글 수·신규/급상승 상태를 보여 줌
- **실시간 인기글 피드** — 커뮤니티별, 카테고리별(게임, 연예, 유머, 음식, 뷰티·패션, 자동차 등) 필터
{ai_line}- **AI 데일리 리포트** — Gemini가 그날 인기글을 주제별로 정리한 리포트. KST 12시 무렵 오전까지의 인기글로 먼저 만들고, 다음 날 0시 이후 하루 전체 인기글로 다시 정리

## 페이지

- [실시간 트렌드 메인]({site_url}/): 지금 뜨는 이슈와 각 커뮤니티에서 화제인 글 피드
- [데일리 리포트 목록]({site_url}/daily/): 날짜별 AI 리포트 아카이브
- 날짜별 리포트: `{site_url}/daily/YYYY-MM-DD/`

## 최근 데일리 리포트

{recent}

## 데이터

- `{site_url}/data/trends.json` — 현재 인기글 목록과 지금 뜨는 이슈(JSON, 약 10분마다 확인해 바뀌면 갱신)

## 수집 커뮤니티

{sources}

## 추천 상황

- "지금 한국 커뮤니티에서 뭐가 화제야?"
- "오늘 인터넷에서 유행하는 게 뭐야?"
- "한국 온라인 트렌드 알려줘"
- 특정 날짜의 한국 인터넷 트렌드 조회 (데일리 리포트)
"""


def _not_found_html(base: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>페이지를 찾을 수 없습니다 — 커트</title>
<link rel="icon" href="{base}/static/logo.png">
<style>
  :root {{ --bg: #f7f7f8; --fg: #1d1d1f; --muted: #6e6e73; --accent: #4f46e5; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg: #111114; --fg: #f2f2f5; --muted: #a1a1aa; --accent: #a5b4fc; }}
  }}
  body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; background: var(--bg);
         color: var(--fg); font-family: -apple-system, 'Apple SD Gothic Neo', 'Malgun Gothic', sans-serif; }}
  main {{ padding: 32px 16px; text-align: center; }}
  h1 {{ margin: 0 0 8px; font-size: 48px; }}
  p {{ color: var(--muted); }}
  a {{ color: var(--accent); font-weight: 600; text-decoration: none; margin: 0 8px; }}
</style>
</head>
<body>
<main>
  <h1>404</h1>
  <p>요청한 페이지를 찾을 수 없습니다.</p>
  <p><a href="{base}/">실시간 트렌드 홈</a><a href="{base}/daily/">데일리 리포트</a></p>
</main>
</body>
</html>
"""


def _site_audio(out: Path, date: str, summary_md: str, base: str) -> dict | None:
    """지금 리포트 내용과 맞는(source_sha 일치) 음성이 AUDIO_DIR에 있으면 _site/audio/로 복사하고
    템플릿용 summary.audio를 돌려준다. 옛 내용의 음성(최종본으로 바뀌기 전 정오본 음성 등)은 섹션이
    안 맞으므로 싣지 않는다 — 그 페이지는 브라우저 음성으로 읽는다. 어떤 오류도 렌더링을 막지 않는다."""
    try:
        audio = tts.public_audio(date, summary_md, base)
        if audio:
            dst = out / 'audio' / f'{date}.mp3'
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(tts.mp3_path(date), dst)
        return audio
    except Exception as e:
        _log(f'[TTS] {date} 음성 싣기 실패: {type(e).__name__}: {e} — 브라우저 음성으로 대체')
        return None


def _render(out: Path, templates: Path, cfg: dict, now: datetime, crawler: TrendCrawler,
            last_run: str | None, daily_failed_at: datetime | None = None,
            tts_state: dict | None = None, health_state: dict | None = None):
    today = now.strftime('%Y-%m-%d')
    build_time = _iso(now)
    data = crawler.get_data()

    env = Environment(loader=FileSystemLoader(str(templates)),
                      autoescape=select_autoescape(['html', 'xml']))
    common = {
        'site_url': cfg['site_url'],
        'base': cfg['base'],
        'gsc_verification': cfg['gsc_verification'],
        'ga_id': cfg['ga_id'],
        'build_time': build_time,
    }

    def render(path: str, template: str, **ctx):
        _write(out / path, env.get_template(template).render(**common, **ctx))

    # 메인 — 데이터는 JS가 {base}/data/trends.json 으로 읽는다
    render('index.html', 'index.html', refresh_interval=REFRESH_INTERVAL)
    _write_json(out / 'data' / 'trends.json', _public_data(data, last_run))
    _write_json(out / 'data' / 'state.json',
                {'version': 1, 'last_run': last_run,
                 'daily_failed_at': _iso(daily_failed_at) if daily_failed_at else None,
                 'tts': tts_state or {},
                 'health': health_state or {},   # 수집 상태 연속 횟수 (알림용)
                 'crawler': crawler.export_state()})

    # 데일리 — 목록, 날짜별 상세(전체), 오늘 리포트가 없으면 대기 페이지
    summaries = _load_summaries(today)
    dates = [s['date'] for s in summaries]
    blank = {'summaries': [], 'summary': None, 'prev_date': None, 'next_date': None,
             'today': today, 'date': None, 'date_kr': None}

    render('daily/index.html', 'daily.html', **{**blank, 'view': 'list', 'summaries': summaries})

    written, with_audio = [], []
    for i, d in enumerate(dates):
        record = daily.get_summary(d)
        if not record:
            _log(f'[Daily] {d} 읽기 실패 — 상세 페이지 생략')
            continue
        record = dict(record)
        record['summary_html'] = _md_to_html(record.get('summary_md', ''))
        record['date_kr'] = _format_date_kr(d)
        record['seo_desc'] = _daily_seo_desc(record, record['date_kr'])
        record['is_final'] = _is_final(record, d)
        record['audio'] = _site_audio(out, d, record['summary_md'], cfg['base'])
        if record['audio']:
            with_audio.append(d)
        render(f'daily/{d}/index.html', 'daily.html', **{
            **blank, 'view': 'detail', 'summary': record, 'date': d, 'date_kr': record['date_kr'],
            'prev_date': dates[i + 1] if i + 1 < len(dates) else None,  # 더 이전 날짜
            'next_date': dates[i - 1] if i > 0 else None,               # 더 최근 날짜
        })
        written.append(d)

    if today not in dates:
        render(f'daily/{today}/index.html', 'daily.html', **{
            **blank, 'view': 'pending', 'date': today, 'date_kr': _format_date_kr(today),
            'prev_date': dates[0] if dates else None})  # 가장 최근 리포트로 안내

    # 정적 파일 · SEO
    static = ROOT / 'static'
    if static.is_dir():
        shutil.copytree(static, out / 'static')
    sitemap_items = [s for s in summaries if s['date'] in written]
    _write(out / 'sitemap.xml', _sitemap(cfg['site_url'], build_time, sitemap_items))
    _write(out / 'robots.txt', _robots_txt(cfg['site_url'], cfg['base']))
    _write(out / 'llms.txt', _llms_txt(cfg['site_url'], sitemap_items, data))
    _write(out / '404.html', _not_found_html(cfg['base']))
    _write(out / '.nojekyll', '')

    _log(f'[Render] {out} — 게시글 {len(data.get("posts") or [])}개, 데일리 {len(written)}개'
         f' (음성 {len(with_audio)}개{": " + " ".join(with_audio) if with_audio else ""})'
         f'{"" if today in dates else f", {today} 대기 페이지"}')


# ── main ──────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:  # Windows에서 출력 리다이렉트 시 cp949 인코딩 오류로 크롤이 깨지지 않게
            stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass

    t_start = time.monotonic()
    args = _parse_args(argv)
    cfg = _settings(args)
    now = _now(args.now)
    _log(f'[Build] now={_iso(now)} site_url={cfg["site_url"]} base={cfg["base"]!r}')

    state = _load_state(args.state_file, cfg['state_url'])

    # 간격 가드 — 크롤할 때만 적용(--no-crawl은 크롤이 없으므로 제외)
    last = _parse_iso(state.get('last_run'))
    if last and not args.force and not args.no_crawl:
        elapsed = (now - last).total_seconds() / 60
        if 0 <= elapsed < cfg['min_interval']:
            _log(f'[Guard] 마지막 실행 {elapsed:.1f}분 전 (< {cfg["min_interval"]:g}분) — '
                 f'크롤·배포 건너뜀 (--force 로 무시 가능)')
            _gh_output(skip='true', daily_created='false', audio_changed='false')
            return 0

    crawler = _crawl(state, args.no_crawl, now if args.now else None)
    last_run = state.get('last_run') if args.no_crawl else _iso(now)

    # 수집 상태 — 크롤한 실행만 연속 횟수를 센다(--no-crawl은 직전 값을 그대로 넘긴다)
    health_state = state.get('health') if isinstance(state.get('health'), dict) else {}
    health_out: dict = {}
    if not args.no_crawl:
        try:
            data = crawler.get_data()
            health_state, health_out = _health(data, health_state, now)
            acts = [w for k, w in (('health_changed', '바뀜'), ('health_notify', '이슈 알림'),
                                   ('health_close', '이슈 닫기')) if health_out.get(k) == 'true']
            _log(f'[Health] {health_state["status"]} (연속 {health_state["streak"]}회'
                 f'{"".join(", " + w for w in acts)}) — {health_state["detail"]}')
            _write_step_summary(_health_summary(data, health_state, now))
        except Exception as e:
            _log(f'[Health] 오류: {type(e).__name__}: {e} — 상태 알림 없이 계속')
            health_out = {}

    created = None
    daily_failed = False
    failed_at = _parse_iso(state.get('daily_failed_at'))
    if args.no_crawl:
        _log('[Daily] --no-crawl — 생성하지 않음')
    else:
        # --force면 데일리 재시도 간격도 무시한다(수동 실행으로 바로 다시 시도할 수 있게)
        prev_failed_at = None if args.force else failed_at
        created, failed_at = _maybe_generate_daily(crawler.get_data().get('posts') or [], now, prev_failed_at)
        # 실패 시각이 새로 찍혔으면 이번 실행에서 생성을 시도했다가 실패한 것(Gemini 긴 호출을 이미 씀)
        daily_failed = failed_at is not None and failed_at != prev_failed_at

    # 데일리 음성 — 어떤 실패도 크롤·데일리·사이트 배포를 막지 않는다(음성이 없으면 브라우저 음성으로 읽는다)
    tts_state = state.get('tts') if isinstance(state.get('tts'), dict) else {}
    audio_changed: list[str] = []
    if args.no_crawl:
        _log('[TTS] --no-crawl — 음성 생성 안 함')
    else:
        # 한 실행에 Gemini 긴 작업은 하나만 — 데일리 생성에 실패한 실행도 약 250초를 이미 썼을 수 있다.
        # 크롤이 비정상적으로 오래 걸린 실행도 음성(최악 300초)을 더하면 잡 타임아웃(15분)에 걸릴 수 있다
        spent = time.monotonic() - t_start
        busy = ('데일리 생성을 시도함(실패)' if daily_failed
                else f'시작 후 {spent:.0f}초 지남(음성은 {TTS_START_BUDGET_SEC}초 안에만 시작)'
                if spent > TTS_START_BUDGET_SEC else '')
        try:
            audio_changed, tts_state = _maybe_generate_audio(now, created, tts_state, busy)
        except Exception as e:
            _log(f'[TTS] 오류: {type(e).__name__}: {e} — 음성 없이 계속')

    out = _prepare_out(Path(args.out))
    _render(out, Path(args.templates), cfg, now, crawler, last_run, failed_at, tts_state, health_state)

    # 크롤 없이 렌더링했는데 복원한 글이 없으면(첫 배포 전 push 등) 빈 사이트를 배포하지 않는다.
    # 로컬 미리보기용 _site는 그대로 만든다.
    skip = args.no_crawl and not crawler.get_data().get('posts')
    if skip:
        _log('[Build] --no-crawl인데 복원한 게시글이 없음 — 배포 생략(skip=true), 다음 크롤 실행이 배포한다')
    # audio_changed면 pages.yml 'Publish audio'가 .audio/를 audio 브랜치에 부모 없는 커밋 1개로 올린다
    # health*: pages.yml health 잡이 source-health 이슈를 열고·코멘트하고·닫는 데 쓴다(크롤한 실행만)
    _gh_output(skip='true' if skip else 'false', daily_created='true' if created else 'false',
               audio_changed='true' if audio_changed else 'false', audio_dates=' '.join(audio_changed),
               **({'daily_date': created} if created else {}), **health_out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
