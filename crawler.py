"""
Korean internet community trend crawler.
주 경로: TodayBestStory API(21개 커뮤니티 베스트 전량).
예비 경로(커뮤니티 단위): TBS에서 빠졌거나(missing) 갱신이 멈춘(degraded) 커뮤니티만
  직접 스크래핑(fallback)과 이슈링크(sources_issuelink.py, 2차 소스)로 채운다.
  TBS 글이 아주 적은 커뮤니티(오유 등)는 평소에도 이슈링크로 보탠다.
커뮤니티별 수집 상태는 source_health로 남기고, KST 02~12시에는 당일 글이 모자란 칸만 전날 상위 글로 채운다.
랭킹한 글을 issues.py가 '지금 뜨는 이슈'(여러 커뮤니티에 퍼진 이야기)로 묶는다.
"""

import hashlib
import inspect
import json
import math
import re
import time
import random
import threading
import requests
from bs4 import BeautifulSoup
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, urlparse

KST = timezone(timedelta(hours=9))

# Mac UA는 딴지일보가 연결을 끊어서 뺐다 (Windows/Linux UA는 모든 소스에서 200)
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0',
]

def _random_headers(extra=None):
    h = {
        'User-Agent': random.choice(USER_AGENTS),
        'Accept-Language': 'ko-KR,ko;q=0.9',
        'Accept': 'text/html,application/xhtml+xml,*/*;q=0.8',
    }
    if extra:
        h.update(extra)
    return h

HEADERS = _random_headers()


def _is_http_url(url) -> bool:
    return isinstance(url, str) and url.lower().startswith(('http://', 'https://'))


def _fmt_dt(dt: datetime) -> str:
    """시각을 아는 경우의 계약 형식: 'YYYY-MM-DDTHH:MM:SS+09:00'"""
    return dt.astimezone(KST).strftime('%Y-%m-%dT%H:%M:%S+09:00')


def _parse_iso_dt(value):
    """ISO 8601 → aware datetime. 'Z'는 UTC, 오프셋이 없으면 KST로 본다. 형식이 틀리면 None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=KST)


def _url_key(url: str) -> str:
    """같은 글의 URL 변형(?page=2, &p=1, 게시판 경로 차이)을 하나로 묶는 중복 제거 키."""
    u = urlparse(url)
    host = u.netloc.lower()
    if host.startswith('www.'):
        host = host[4:]
    m = re.search(r'(?:^|&)(?:document_srl|No|no|number|num|wr_id|id)=(\d+)', u.query)
    if m:
        # 게시판마다 번호가 따로 매겨지는 곳(id=freeboard&no=… 등)은 게시판 이름까지 넣어 다른 글끼리 겹치지 않게
        b = re.search(r'(?:^|&)(?:id|table|code|bo_table)=([A-Za-z]\w*)', u.query)
        return f'{host}#{b.group(1) + ":" if b else ""}{m.group(1)}'
    # 마지막 숫자 경로(게시판 번호가 아니라 글 번호). 이토랜드는 '/view/제목-슬러그-9447051'처럼 슬러그 끝에 붙는다
    nums = re.findall(r'[/-](\d{4,})(?=/|$)', u.path)
    if nums:
        return f'{host}#{nums[-1]}'
    return f'{host}{u.path}?{u.query}'


TITLE_KEY_MIN = 5   # 공백·기호를 뺀 제목이 이보다 짧으면 제목으로 같은 글을 찾지 않는다 (짧은 제목은 다른 글끼리 겹친다)


def _title_key(p: dict) -> str:
    """같은 커뮤니티의 같은 글을 제목으로 알아보는 키 ('source|공백·기호를 뺀 소문자 제목', 짧으면 '').
    경로마다 글 번호가 다른 곳용 — SLR·보배드림은 TBS(베스트 게시판 번호: id=best_article&no=673740)와
    이슈링크(원래 게시판 번호: id=free&no=41756037)가 같은 글을 다른 URL로 준다"""
    t = re.sub(r'[\W_]+', '', str(p.get('title') or '')).lower()
    return f"{p.get('source')}|{t}" if len(t) >= TITLE_KEY_MIN else ''


# ── TodayBestStory API ─────────────────────────────────────────────────────────

TBS_API_URL = 'https://todaybeststory.com/api/v2/communities/posts/range'
TBS_PAGE_LIMIT = 100   # API 최대값 (그 이상은 400)
# 실행마다 limit을 100/99로 번갈아 쓴다(분÷10 홀짝). TBS 응답은 URL별로 약 10분 캐시돼서, 같은 URL이면
# HH:10 실행이 직전(HH:00) 실행이 만든 캐시를 다시 읽고 끝난다(2026-09-24 측정: 처음 쓴 limit URL은 새 값)
TBS_PAGE_LIMITS = (TBS_PAGE_LIMIT, TBS_PAGE_LIMIT - 1)
TBS_MAX_PAGES = 30     # 안전 상한 (하루 1300~2000건 → 13~20페이지)
# 초. 넘으면 페이지 넘기기를 멈추고 받은 만큼 쓴다. 마감 직전 페이지가 최악(15초×2회+대기 2초)으로 걸려도
# 호출부 hard timeout 90초 안에 끝나야 받은 페이지를 잃지 않는다 (평소 16페이지 약 8초)
TBS_DEADLINE = 55
# KST 이 시각 전에는 TBS를 전날·오늘 같이 받는다(자정 직후 오늘 목록이 작아서). 전날 상위 글 저장(오전 보충)과
# build.py의 전날 데일리 최종본(DAILY_FINAL_END_HOUR)이 이 시간대를 같이 쓴다
MERGE_PREV_END_HOUR = 2
TBS_MIN_POSTS = 50     # 끝까지 받았어도 TBS 결과가 이보다 적거나
TBS_MIN_SOURCES = 8    # 커뮤니티 수가 이보다 적으면 25개를 못 채운 커뮤니티 전부를 예비 경로로 보탠다 (보조 조건)

# communityId → (source id, 라벨, 이모지, 색).
# 일베(ILB)는 2024-08 이후 TBS 전체 DB에 글이 0건이라 뺐다(2026-09 확인) — 수집 커뮤니티는 21곳.
# 다시 나오면 아래 '새로 추가된 커뮤니티' 분기가 API 이름으로 받는다
COMMUNITY_ID_MAP = {
    'FMK': ('fmkorea', 'FMKorea', '🔥', '#ff6b35'),
    'DCI': ('dcinside', '디시인사이드', '🎭', '#0066cc'),
    'RUL': ('ruliweb', '루리웹', '🎮', '#3498db'),
    'CLI': ('clien', '클리앙', '💻', '#9b59b6'),
    'QOO': ('theqoo', '더쿠', '💗', '#e91e8c'),
    'MLB': ('mlbpark', 'MLB파크', '⚾', '#1565c0'),
    'INS': ('instiz', '인스티즈', '💬', '#ff7675'),
    'BOB': ('bobaedream', '보배드림', '🚗', '#fdcb6e'),
    'HUM': ('humoruniv', '웃긴대학', '🤣', '#f9ca24'),
    'ARC': ('arcalive', '아카라이브', '🌊', '#00b4d8'),
    'NAT': ('natekorea', '네이트판', '💁', '#e17055'),
    'DDA': ('ddanzi', '딴지일보', '📰', '#6c5ce7'),
    'DOG': ('dogdrip', '개드립', '🐶', '#00b894'),
    'ETO': ('etoland', '이토랜드', '🎯', '#fd79a8'),
    'GAS': ('gasengi', '가생이닷컴', '🌏', '#e84393'),
    'INV': ('inven', '인벤', '⚔️', '#d35400'),
    'PPO': ('ppomppu', '뽐뿌', '💰', '#27ae60'),
    'SLR': ('slrclub', 'SLR클럽', '📷', '#2980b9'),
    'TOD': ('todayhumor', '오늘의유머', '😂', '#2ecc71'),
    'YGO': ('ygosu', '와이고수', '🎲', '#8e44ad'),
    '82C': ('cook82', '82쿡', '👩‍🍳', '#e74c3c'),
}
SOURCE_META = {v[0]: v[1:] for v in COMMUNITY_ID_MAP.values()}   # source id → (라벨, 이모지, 색)

# TBS readCount가 실제 조회수가 아닌 소스 (FMK는 37만~95만 1만 단위 계단값) → 조회수 0으로 두고 순위·추천·댓글로만 평가
SYNTHETIC_VIEW_SOURCES = {'FMK'}
# 같은 커뮤니티 source id. 이슈링크 글(실제 조회수)도 조회수를 0으로 둔다 — 한 커뮤니티에 TBS 글(0)과 섞이면
# _assign_ranks가 그 커뮤니티를 '조회수 있음'으로 보고 TBS 글을 조회수 0점으로 뒤로 민다
SYNTHETIC_VIEW_SOURCE_IDS = {COMMUNITY_ID_MAP[c][0] for c in SYNTHETIC_VIEW_SOURCES if c in COMMUNITY_ID_MAP}


# ── 커뮤니티별 수집 상태 (source_health) ──────────────────────────────────────
# status: ok | degraded(TBS 갱신 멈춤) | missing(있어야 할 시각인데 TBS에 당일 글 없음) | blocked(TBS가 못 주고 직접 수집도 차단)
HEALTH_STALE_MIN = 90     # TBS에서 이 시간(분) 넘게 갱신(updateDatetime)이 없는 커뮤니티는 degraded
HEALTH_FROM_HOUR = 6      # degraded·stale-source는 KST 06~24시에만 판정한다
# '이 시각(KST)부터는 TBS에 당일 글이 있어야 정상'인 고정 기대치. 2026-09-22~25 커뮤니티별 TBS 첫 수집 시각
# (creationDatetime 최솟값)의 가장 늦은 날 기준. 날마다 거의 같은 곳(에펨·클리앙 등 00시대, 디시·뽐뿌 01시대)은 2~3시간,
# 날마다 크게 흔들리는 곳은 4시간 여유를 둔다(인스티즈 01:11~06:11, 와이고수 00:06~06:06, 보배 01~05시, SLR 07~11시,
# 웃대 10~14시 — 3일치 + 2시간으로 잡았던 06시는 09-25 인스티즈·와이고수 06시대 첫 수집에서 missing 오탐이 났다).
# 02시 전(전날 목록을 같이 받는 시간)에는 None이 아닌 곳 전부.
# None: 하루 글이 1~14건뿐이라(오유·가생이·인벤) 당일 글이 없어도 정상 — missing으로 보지 않는다
EXPECTED_BY_HOUR = {
    'fmkorea': 2, 'mlbpark': 2, 'clien': 2, 'etoland': 2, 'dogdrip': 2, 'natekorea': 2, 'arcalive': 2, 'cook82': 2,
    'dcinside': 4, 'ppomppu': 4,
    'ruliweb': 6, 'theqoo': 6,
    'bobaedream': 9, 'instiz': 10, 'ygosu': 10, 'ddanzi': 13, 'slrclub': 15, 'humoruniv': 18,
    'todayhumor': None, 'gasengi': None, 'inven': None,
}
PARTIAL_MIN_PROBLEMS = 3  # TBS에서 빠지거나(missing) 멈춘(degraded) 기대 커뮤니티가 이만큼 이상이면 status partial
# TBS가 온전하지 않을 때(장애·중간 끊김) 새 결과를 채택하는 절대 기준. 못 넘으면 이전 목록을 유지(stale).
# 넘으면 채택하고, 이번에 빠진 커뮤니티는 이전 글을 유지해 병합한다(kept)
FALLBACK_MIN_SOURCES = 6
FALLBACK_MIN_POSTS = 150
ISSUELINK_THIN = 5              # 결정 2-b: TBS 글이 이보다 적은 커뮤니티(오유 등)는 평소에도 이슈링크로 보탠다
                                # (KST 02~12시 오전 보충 중에는 어제 글이 넉넉했던 커뮤니티를 빼고 — 그 칸은 전날 상위 글이 채운다)
ISSUELINK_THIN_EVERY_MIN = 30   # 2-b 커뮤니티를 이슈링크에서 다시 받는 간격(분, 2-a가 같이 있어도). 그 사이에는 직전에 받은 글을 다시 쓴다
ISSUELINK_TIMEOUT = 45          # 이슈링크 호출 hard timeout(초). 모듈 자체 상한(약 20초) 위의 안전망
ISSUELINK_CACHE_MAX = 1000      # state에 남기는 이슈링크 원본 URL 캐시 항목 상한
VIA_ORDER = ('tbs', 'direct', 'issuelink')
MAX_PER_SOURCE = 25             # 커뮤니티당 게시 글 상한 (_assign_ranks)

# 오전 보충(결정 1): 00~02시에 받은 전날 커뮤니티별 상위 글을 state에 두고, 02시 이후 당일 글이
# MAX_PER_SOURCE개에 못 미치는 커뮤니티 칸만 채운다('어제' 표시, prev_day). 당일 글만으로
# 19곳·350건이 되거나 KST 12시가 되면 그날은 끝낸다
PREV_DAY_DONE_SOURCES = 19
PREV_DAY_DONE_POSTS = 350
PREV_DAY_END_HOUR = 12
PREV_DAY_FIELDS = ('title', 'url', 'source', 'source_label', 'source_emoji', 'source_color', 'views', 'likes',
                   'comments', 'date', 'summary', 'author', 'rank_score', 'via', 'is_food', 'is_beauty',
                   'is_fashion', 'is_travel', 'is_game', 'is_celeb', 'is_humor', 'is_car')

# 전체 status — ok | partial(TBS가 일부만 옴: 끊김·장애·기대 커뮤니티 여럿 빠짐/멈춤, 예비 경로로 채우고 이전 글 병합)
# | stale(수집 실패로 이전 목록 유지) | stale-source(TBS 전체 갱신이 HEALTH_STALE_MIN분 넘게 멈춤)
STATUSES = ('ok', 'partial', 'stale', 'stale-source')
HEALTH_STATUSES = ('ok', 'degraded', 'missing', 'blocked')


# ── 직접 스크래핑 (TBS에서 빠졌거나 멈춘 커뮤니티만) ──────────────────────────
# 행(row_sel) 단위로 파싱해서 제목·조회수·날짜가 항상 같은 글에서 나오게 한다.
# 인스티즈(Cloudflare 챌린지 'Just a moment')·FMKorea(430 보안 페이지)는 해외 IP(GitHub Actions 등) 기준으로
# requests로 못 받아서 TBS·이슈링크에 맡긴다. 오유·더쿠·웃대도 해외 IP에서는 막히는 것으로 측정됐지만
# (403·'보안 검사중'·msg.html, 2026-09-24 GCP·Hetzner 측정) 결정 3(되는 동안은 쓴다)에 따라 남겨 두고,
# 막히면 source_health에 blocked로 남긴다.
#   title_sel  행 안의 글 링크(href 사용)      text_sel   링크 안의 제목 요소(없으면 링크 텍스트)
#   strip_sel  제목에서 지울 요소(댓글 수 등)   date_attr  날짜를 텍스트 대신 속성에서 읽기
#   date_from_title  날짜 칸이 없어 페이지 <title>의 'YYYY.MM.DD'를 쓴다
#   row_is_link  행 요소가 곧 글 링크(title_sel 없이 행의 href를 쓴다)
COMMUNITY_SOURCES = [
    {
        'id': 'todayhumor',
        'pages': [
            'https://www.todayhumor.co.kr/board/list.php?table=bestofbest&page=1',
            'https://www.todayhumor.co.kr/board/list.php?table=bestofbest&page=2',
        ],
        'row_sel': 'table.table_list tr.view',
        'title_sel': 'td.subject > a',
        'view_sel': 'td.hits',
        'like_sel': 'td.oknok',
        'date_sel': 'td.date',
    },
    {
        'id': 'ruliweb',
        'pages': [
            'https://bbs.ruliweb.com/best/humor_only',
            'https://bbs.ruliweb.com/best/humor_only?page=2',
        ],
        'row_sel': 'table.board_list_table tr.table_body',
        'title_sel': 'a.subject_link[href*="/best/board/"]',   # 핫딜 광고 행(/market/) 제외
        'text_sel': '.text_over',                               # 순위 숫자·댓글 수 빼고 제목만
        'view_sel': 'td.hit',
        'like_sel': 'td.recomd',
        'date_sel': 'td.time',
    },
    {
        'id': 'clien',
        'pages': [
            'https://www.clien.net/service/board/park?od=T33&po=0',   # T33 = 인기순 (T31은 최신순)
            'https://www.clien.net/service/board/park?od=T33&po=1',
        ],
        'row_sel': 'div.list_item:not(.notice)',
        'title_sel': 'a.list_subject',
        'text_sel': 'span.subject_fixed',
        'view_sel': '.list_hit .hit',
        'like_sel': '.list_symph span',
        'date_sel': '.list_time span.timestamp',
    },
    {
        'id': 'theqoo',
        'pages': [
            'https://theqoo.net/hot',
            'https://theqoo.net/hot?page=2',
        ],
        'row_sel': 'table tbody tr:not(.notice):not(.notice_expand)',   # 공지·이벤트 광고 행 제외
        'title_sel': 'td.title > a:not(.replyNum)',
        'view_sel': 'td.m_no',
        'date_sel': 'td.time',
    },
    {
        'id': 'mlbpark',
        'pages': [
            'https://mlbpark.donga.com/mp/best.php?b=bullpen&m=view',   # 불펜 일간 베스트 (조회수 칸 없음)
        ],
        'row_sel': 'table.tbl_type01 tbody tr',
        'title_sel': 'td.t_left a.txt',
        'date_sel': 'span.date',
    },
    {
        'id': 'bobaedream',
        'pages': [
            'https://www.bobaedream.co.kr/list?code=best',
            'https://www.bobaedream.co.kr/list?code=best&page=2',
        ],
        'row_sel': 'table tbody tr',
        'title_sel': 'a.bsubject',
        'view_sel': 'td.count',
        'like_sel': 'td.recomm',
        'date_sel': 'td.date',
    },
    {
        'id': 'natekorea',
        'pages': [
            # '톡커들의 선택'(최근 인기글). 일간 랭킹(/talk/ranking/d)은 끝난 날짜만 있어 전날 랭킹을 주고,
            # ?stdt=<오늘>은 빈 목록이다(2026-09-25 오전 확인). 날짜 칸이 없어 date는 ''
            'https://pann.nate.com/talk/ranking',
        ],
        'row_sel': 'div.cntList ul li',
        'title_sel': 'dt h2 a[href^="/talk/"]',
        'view_sel': 'dd.info span.count',
        'like_sel': 'dd.info span.rcm',
        'summary_sel': 'dd.txt',
    },
    {
        'id': 'humoruniv',
        'pages': [
            'https://web.humoruniv.com/board/humor/list.html?table=pds&pg=0',   # 페이지 파라미터는 pg(0부터)
            'https://web.humoruniv.com/board/humor/list.html?table=pds&pg=1',
        ],
        'row_sel': 'tr[id^="li_chk_"]',
        'title_sel': 'td.li_sbj a[href*="read.html"]',
        'text_sel': 'span[id^="title_chk_"]',
        'view_sel': 'td.li_und',
        'like_sel': 'td.li_und span.o',
        'date_sel': 'td.li_date',
    },
    {
        'id': 'arcalive',
        'pages': [
            'https://arca.live/b/live?sort=recommend',
            'https://arca.live/b/live?sort=recommend&p=2',
        ],
        'row_sel': 'div.vrow.hybrid:not(.notice)',
        'title_sel': 'a.title.hybrid-title',
        'strip_sel': '.info, .media-icon',
        'view_sel': '.col-view',
        'like_sel': '.col-rate',
        'date_sel': 'time[datetime]',
        'date_attr': 'datetime',
    },
    {
        'id': 'ddanzi',
        'pages': [
            'https://www.ddanzi.com/index.php?mid=free&statusList=BEST%2CHOTBEST%2CBESTAC%2CHOTBESTAC',
            'https://www.ddanzi.com/index.php?mid=free&statusList=BEST%2CHOTBEST%2CBESTAC%2CHOTBESTAC&page=2',
        ],
        'row_sel': 'table.fz_change tr:not(.notice)',
        'title_sel': 'td.title > a:first-of-type',   # 두 번째 링크는 댓글 수('[19]', #comment)
        'view_sel': 'td.readNum',
        'like_sel': 'td.voteNum',
        'date_sel': 'td.time',
    },
    # 아래 7곳은 2026-09-26 추가. Source probe(러너 미국 IP)에서 접속되는 것을 확인했다
    {
        'id': 'dcinside',
        'pages': [
            'https://gall.dcinside.com/board/lists/?id=dcbest',   # 실시간 베스트
            'https://gall.dcinside.com/board/lists/?id=dcbest&page=2',
        ],
        'row_sel': 'table.gall_list tbody tr.ub-content.us-post',   # 공지·광고 행은 us-post가 아니다
        'title_sel': 'td.gall_tit > a:not(.reply_numbox)',
        'view_sel': 'td.gall_count',
        'like_sel': 'td.gall_recommend',
        'date_sel': 'td.gall_date',
        'date_attr': 'title',                                       # 'YYYY-MM-DD HH:MM:SS' (글자는 'HH:MM'·'YY.MM.DD')
    },
    {
        'id': 'ppomppu',
        'pages': [
            'https://www.ppomppu.co.kr/hot.php',   # HOT 게시글 (여러 게시판)
        ],
        'row_sel': 'tr.baseList',
        'title_sel': 'a.baseList-title',
        'strip_sel': '.list_comment2',
        'date_sel': 'td.board_date',                                           # 날짜·추천-반대·조회 순
        'like_sel': 'td.board_date + td.board_date',
        'view_sel': 'td.board_date + td.board_date + td.board_date',
    },
    {
        'id': 'inven',
        'pages': [
            'https://www.inven.co.kr/board/webzine/2097?my=chuchu',   # 오픈이슈갤러리 추천 글
        ],
        'row_sel': 'table.thumbnail tbody tr',
        'title_sel': 'a.subject-link',
        'strip_sel': 'span.category',
        'view_sel': 'td.view',
        'like_sel': 'td.reco',
        'date_sel': 'td.date',
    },
    {
        'id': 'cook82',
        'pages': [
            'https://www.82cook.com/entiz/enti.php?bn=15',   # 자유게시판의 '많이 읽은 글' 10개 (조회수·날짜 칸 없음)
        ],
        'row_sel': 'div.Best ul.most li',
        'title_sel': 'a[href*="read.php"]',
    },
    {
        'id': 'etoland',
        'pages': [
            'https://www.etoland.co.kr/hit/list',   # 인기 순위 30개. /b/hit/list는 07-25에 멈춘 옛 목록이다
        ],
        'row_sel': 'a[class*="grid-cols-[21px"]',   # 순위 행이 링크 자체
        'row_is_link': True,
        'text_sel': 'span.subject',
    },
    {
        'id': 'ygosu',
        'pages': [
            'https://ygosu.com/board/real_article',   # 실시간 인기
        ],
        'row_sel': 'table.bd_list tbody tr:not(:has(td.bdname))',   # 공지·AD 행은 td.bdname이 있다
        'title_sel': 'td.tit a',
        'strip_sel': 'span.category, span.reply_cnt',
        'view_sel': 'td.read',
        'like_sel': 'td.vote',
        'date_sel': 'td.date',
    },
    {
        'id': 'dogdrip',
        'pages': [
            'https://www.dogdrip.net/dogdrip?sort_index=popular',   # 개드립 인기순 (TBS와 같은 목록)
        ],
        'row_sel': 'li.webzine',
        'title_sel': 'a.title-link',
        'like_sel': 'div.list-meta span.margin-right-xsmall > span.text-primary:last-child',
        'date_sel': 'div.list-meta span.text-muted',   # 'N 시간 전'
    },
]

# 제목 맨 앞 말머리([공지]·[🚨필독🚨]·(광고) 등)만 공지로 본다. '…긴급공지'처럼 제목 속 단어는 제외
NOTICE_TITLE_RE = re.compile(r'^\s*[\[【(<]\W{0,3}(공지|필독|이벤트|광고|홍보|AD)')
NOTICE_URL_RE = re.compile(r'/event/|/annonce/|/rule/|[?&]b=notice|event_notice|[?&]id=sponsor')
AD_TITLE_RE = re.compile(r'^\s*(AD\s|하루특가\))')   # 목록에 섞인 광고 행 (뽐뿌 'AD […]', 이토랜드 '하루특가)')
COMMENT_LINK_RE = re.compile(r'^[\[(]?\d+[\])]?$')   # 댓글 수 링크('[19]', '(5)')

# 직접 스크래핑 차단 페이지 표시 (HTTP 200으로 온다). 정상 목록에도 챌린지 스크립트 흔적이 섞여 있어(오유 국내 응답)
# 행을 하나도 못 읽었을 때만 이유를 붙이는 데 쓴다
BLOCK_MARKERS = ('Just a moment', '보안 검사', '보안 시스템')
BLOCK_REDIRECT_RE = re.compile(rb"""location\.(?:replace|href)\s*\(?\s*=?\s*['"][^'"]*msg\.html""")


def detect_block(content: bytes, url: str = '') -> str:
    """차단·챌린지 페이지로 보이면 그 이유, 아니면 ''. (Cloudflare 'Just a moment', 더쿠 '보안 검사중',
    웃대 msg.html 리다이렉트 등 — 행 0개와 같이 쓴다)"""
    if 'msg.html' in (url or ''):
        return 'msg.html 리다이렉트'
    head = (content or b'')[:60000]
    if BLOCK_REDIRECT_RE.search(head):
        return 'msg.html 리다이렉트'
    for marker in BLOCK_MARKERS:
        for enc in ('utf-8', 'cp949'):
            if marker.encode(enc) in head:
                return marker
    return ''

FOOD_WORDS = {
    '맛집', '카페', '음식', '요리', '디저트', '한식', '브런치', '레스토랑', '맛있',
    '맛스타그램', '먹스타그램', '밥집', '식당', '점심', '저녁', '베이커리', '케이크',
    '커피', '라멘', '파스타', '초밥', '치킨', '피자', '샐러드', '맥주', '술집', '고기'
}
BEAUTY_WORDS = {
    '뷰티', '화장품', '피부', '메이크업', '스킨케어', '립스틱', '립밤', '아이섀도', '마스크팩',
    '헤어', '네일', '향수', '세럼', '선크림', '파운데이션', '쿠션', '틴트', '다이어트', '운동'
}
FASHION_WORDS = {
    '패션', '코디', '스타일', '아우터', '원피스', '청바지', '신발', '가방', '악세사리',
    '데일리룩', '오오티디', '하울', '쇼핑', '브랜드', '명품', '옷'
}
TRAVEL_WORDS = {
    '여행', '관광', '호텔', '항공', '비행기', '유럽', '동남아', '제주',
    '강릉', '속초', '해외여행', '국내여행', '캠핑', '드라이브'
}
GAME_WORDS = {
    '게임', '롤', '리그오브레전드', '배그', '배틀그라운드', '오버워치', '마인크래프트',
    '스팀', '닌텐도', '플스', 'PS5', '엑박', '피파', '디아블로', '로스트아크',
    '메이플', '던파', '와우', '포트나이트', '엘든링', '사이버펑크', '발로란트',
    '애플', '아이폰', '갤럭시', '인텔', '엔비디아', 'CPU', 'GPU',
    '노트북', '태블릿', '스마트폰', '컴퓨터', '프로그래밍', 'AI', '인공지능',
    '유튜브', '넷플릭스', '앱', '소프트웨어', '하드웨어', '리뷰',
}
CELEB_WORDS = {
    '아이돌', '연예인', '가수', '배우', '드라마', '영화', '콘서트', '팬미팅',
    '앨범', '컴백', 'BTS', '블랙핑크', '뉴진스', '아이브', '에스파', '방탄',
    '케이팝', '엔터', '연예계', '팬', '직캠', '뮤직비디오', '티저',
    '오디션', '데뷔', '소속사', '음원', '멜론', '스트리밍', '시상식',
}
HUMOR_WORDS = {
    '개그', '코미디', '유머', '웃음', '병맛', '드립', '개드립', '짤', '밈',
    '움짤', '레전드짤', '웃대', '에펨코리아', '기묘한',
}
CAR_WORDS = {
    '자동차', '차량', '주행', '연비', '엔진', '수입차', '국산차',
    '현대차', '기아차', '제네시스', '테슬라', '아우디', '벤츠', 'BMW',
    '볼보', '포르쉐', '전기차', '하이브리드', '중고차', '신차', '튜닝',
    '보배드림', '자동차사고', '교통사고',
}

# (플래그, 대표 카테고리, 단어) — 대표 카테고리는 처음 걸리는 것
CATEGORIES = [
    ('is_food', '음식', FOOD_WORDS), ('is_beauty', '뷰티', BEAUTY_WORDS),
    ('is_fashion', '패션', FASHION_WORDS), ('is_travel', '여행', TRAVEL_WORDS),
    ('is_game', '게임/IT', GAME_WORDS), ('is_celeb', '연예', CELEB_WORDS),
    ('is_humor', '유머', HUMOR_WORDS), ('is_car', '자동차', CAR_WORDS),
]

# 부분 문자열 매칭 오탐을 부르는 합성어 — 카테고리 판정 전에 제목에서 지운다
CATEGORY_EXCLUDE_RE = re.compile('|'.join(map(re.escape, [
    '아시안게임', '아시안 게임', '파인애플', '컨트롤', '롤렉스', '롤러', '롤모델', '캐롤', '패트롤',
    '헤어지', '헤어진', '헤어졌', '헤어짐', '프라이팬', '팬티', '팬데믹', '팬서비스',
    '검색엔진', '고기압', '물고기', '짤렸', '짤린', '짤림', '짤리', '네이버카페', '다음카페',
    '배우자', '배우고', '배우는', '배우러', '배우면', '배우기',
    '운동권', '시민운동', '학생운동', '독립운동', '노동운동',
])))

HISTORY_SIZE = 6   # post_score_history 라운드 수 상한 (이슈 신규·급상승·+N 판정, post_velocity)
# 점수 이력은 TBS를 끝까지 받은 라운드만 넣고, 직전 칸이 이 시간(분) 안에 시작했으면 새 칸 대신 덮어쓴다.
# TBS는 커뮤니티마다 매시 :00~:12에 한 번 수집해서, 한 시간에 '몇 곳만 바뀐 라운드 + 전체가 바뀐 라운드'가
# 연달아 온다. 이를 한 칸으로 묶어 칸 하나 ≈ TBS 갱신 한 번(약 1시간)이 되게 한다
HISTORY_MERGE_MIN = 40
HISTORY_MAX_AGE_MIN = 240   # 이보다 오래된 칸은 버린다 (장애 뒤 몇 시간 전 라운드와 비교하지 않게)
# 튜닝 로그: 결과가 바뀐 빌드의 이슈 요약을 state에 남겨 며칠간 품질을 본다.
# 한 번에 8개면 약 0.9KB라 개수(72)보다 크기 상한이 먼저 걸린다
ISSUE_LOG_SIZE = 72
ISSUE_LOG_MAX_BYTES = 30_000
LOW_DATA_POSTS = 350   # 원본 글이 이보다 적으면 이슈 보드에 '글이 적은 시간' 안내 (issues.py도 이 값을 쓴다)
STATE_VERSION = 1
# 복원 후 쓰지 않는 필드 - state 크기를 줄인다 (position_score는 이전 글을 유지할 때 순서로 다시 매긴다)
STATE_DROP_FIELDS = ('board', 'keyword', 'position_score')
MIN_OK_POSTS = 20   # 이전 목록이 있는데 이보다 적게 모이면 수집 실패로 보고 이전 목록을 유지

SUMMARY_SELECTORS = {
    'todayhumor': '.viewContent p, .memo_content p',
    'ruliweb':    '.view_content p',
    'clien':      '.post_article p',
    'theqoo':     '.xe_content p',
    'mlbpark':    '#contentDetail',
}

SUMMARY_MAX_POSTS = 25
ZERO_WIDTH_RE = re.compile('[​-‍⁠﻿]')   # 본문 앞에 붙는 제로폭 문자
SUMMARY_TAIL_RE = re.compile(r'\s*추천\s*\d+\s*공유\s*$')     # 본문 끝 버튼 글자 ('추천 9 공유')


def _empty_issues(posts: list, now) -> dict:
    """이슈 계산 전·실패 시의 빈 블록 (프론트는 items가 비면 '퍼진 이슈 없음'을 보여 준다).
    계산 실패의 대비책이므로 글 필드를 직접 읽지 않는다 (source 없는 글 때문에 실패했을 수도 있다).
    전날 보충 글(prev_day)은 이슈 보드 대상이 아니라 세지 않는다"""
    posts = [p for p in posts if not (isinstance(p, dict) and p.get('prev_day'))]
    return {'as_of': now.isoformat(timespec='minutes') if now else None, 'posts': len(posts),
            'communities': len({p['source'] for p in posts if p.get('source')}), 'in_issues': 0,
            'low_data': len(posts) < LOW_DATA_POSTS, 'badges': False, 'items': [], 'hot': []}


class TrendCrawler:
    def __init__(self):
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()   # refresh() 동시 실행 방지
        self._posts: list = []
        self._post_score_history: list = []
        self._issues: dict = _empty_issues([], None)
        self._issue_log: list = []          # 튜닝용 이슈 요약 (결과가 바뀐 빌드만)
        self._last_updated = None
        self._status = 'idle'
        self._crawl_count = 0
        self._ai_summary: str = ''
        self._ai_summary_updated = None
        self._ai_summary_attempted = None   # 마지막 Gemini 호출 시각 (실패 시 재시도 간격 계산용)
        self._ai_summary_transient = False  # 마지막 실패가 일시적(503·타임아웃 등)이었는지 → 짧은 간격으로 재시도
        self._snapshot = None               # 직전에 반영한 원본 목록의 서명 (같은 목록이면 이력에 넣지 않는다)
        self._tbs_truncated = False         # 이번 TBS 수집이 다음 페이지를 남기고 끊겼는지
        self._collect_complete = False      # 이번 수집에서 TBS 목록을 끝까지 받았는지
        self._tbs_stats = None              # 이번 TBS 응답의 커뮤니티별 집계 (_fetch_todaybeststory)
        self._plan = None                   # 이번 수집의 커뮤니티 판정·예비 경로 계획 (_plan_fill)
        self._direct_status: dict = {}      # 이번 직접 스크래핑 결과 {source: {status, why, n}}
        self._page_errors: dict = {}        # _get_page가 None을 돌려준 이유 {url: (status, why)} (스레드마다 다른 url)
        self._history_times: list = []      # post_score_history 칸마다 시작 시각 (ISO, 모르면 None)
        self._source_health: dict = {}      # 커뮤니티별 수집 상태 (trends.json source_health)
        self._prev_day = None               # 전날 커뮤니티별 상위 글 {date, posts, done} (오전 보충)
        self._il_at = None                  # 이슈링크에서 2-b 커뮤니티를 마지막으로 새로 받은 시각 (2-b 간격용)
        self._il_sources: set = set()       # 그때 받은 2-b 커뮤니티
        self._il_cache: dict = {}           # 이슈링크 원본 URL 캐시 (모듈이 cache 인자를 받으면 넘긴다)

    def get_data(self) -> dict:
        with self._lock:
            return {
                'posts': list(self._posts),
                # 옛 키워드 트렌드(rising·top·keywords)는 이슈 보드로 바뀌었다. 카테고리 글 수는 공개 JSON 호환용으로 남긴다
                'trends': {'categories': self._category_counts(self._posts)},
                'issues': self._issues,
                'last_updated': self._last_updated,
                'status': self._status,
                'total': len(self._posts),
                'crawl_count': self._crawl_count,
                'sources': list(dict.fromkeys(v[1] for v in COMMUNITY_ID_MAP.values())),
                'source_health': {s: {**e, 'via': list(e.get('via') or [])} for s, e in self._source_health.items()},
                'ai_summary': self._ai_summary,
                'ai_summary_updated': self._ai_summary_updated.isoformat() if self._ai_summary_updated else None,
            }

    # ── 상태 저장/복원 (GitHub Actions 실행 사이에 이어받기) ──────────────────

    def export_state(self) -> dict:
        """다음 실행에서 이어받을 상태. JSON으로 바로 직렬화할 수 있는 값만 담는다."""
        with self._lock:
            prev_day = None
            if self._prev_day:
                prev_day = {'date': self._prev_day.get('date'), 'done': self._prev_day.get('done'),
                            'posts': [{k: p[k] for k in PREV_DAY_FIELDS if k in p}
                                      for p in self._prev_day.get('posts') or []]}
            il_cache = dict(list(self._il_cache.items())[-ISSUELINK_CACHE_MAX:])
            return {
                'version': STATE_VERSION,
                'post_score_history': [dict(h) for h in self._post_score_history],   # _url_key(url) → rank_score
                'post_score_times': list(self._history_times),   # 칸마다 시작 시각 (칸 묶기·오래된 칸 버리기)
                'issue_log': list(self._issue_log),
                # 다음 크롤이 실패하면 이걸 계속 보여준다
                'posts': [{k: v for k, v in p.items() if k not in STATE_DROP_FIELDS} for p in self._posts],
                'ai_summary': self._ai_summary,
                'ai_summary_updated': self._ai_summary_updated.isoformat() if self._ai_summary_updated else None,
                'ai_summary_attempted': self._ai_summary_attempted.isoformat() if self._ai_summary_attempted else None,
                'ai_summary_transient': self._ai_summary_transient,
                'snapshot': self._snapshot,
                'crawl_count': self._crawl_count,
                'last_updated': self._last_updated,
                # 크롤 없이 다시 렌더링할 때(build.py --no-crawl) 직전 상태를 그대로 보여 주려고
                'status': self._status if self._status in STATUSES else None,
                'source_health': {s: {**e, 'via': list(e.get('via') or [])} for s, e in self._source_health.items()},
                'prev_day': prev_day,
                'issuelink': {'at': self._il_at.isoformat(timespec='seconds') if self._il_at else None,
                              'sources': sorted(self._il_sources), 'cache': il_cache},
            }

    def import_state(self, state) -> None:
        """export_state() 결과를 복원한다. None·빈 dict·버전 불일치·형식 오류면 조용히 무시."""
        if not isinstance(state, dict) or state.get('version') != STATE_VERSION:
            return
        # 옛 state의 단어 이력('history')은 더 쓰지 않으므로 읽지 않는다
        try:
            score_history = [{str(u): float(s) for u, s in h.items()}
                             for h in state.get('post_score_history') or [] if isinstance(h, dict)][-HISTORY_SIZE:]
            posts = [p for p in state.get('posts') or []
                     if isinstance(p, dict) and _is_http_url(p.get('url')) and p.get('title') and p.get('source')]
            crawl_count = int(state.get('crawl_count') or 0)
        except (TypeError, ValueError, AttributeError) as e:
            print(f'[State] 크롤러 상태 형식 오류 - 무시: {e}')
            return
        # 칸마다 시작 시각. 옛 state(시각 없음)나 길이가 안 맞으면 모름(None)으로 채운다
        raw_times = state.get('post_score_times')
        times = [t if isinstance(t, str) and _parse_iso_dt(t) else None
                 for t in (raw_times if isinstance(raw_times, list) else [])]
        times = times[-len(score_history):] if score_history else []
        times = [None] * (len(score_history) - len(times)) + times
        log = state.get('issue_log')
        issue_log = [e for e in log if isinstance(e, dict)][-ISSUE_LOG_SIZE:] if isinstance(log, list) else []
        ai_summary = state.get('ai_summary') if isinstance(state.get('ai_summary'), str) else ''
        ai_updated = self._parse_iso(state.get('ai_summary_updated'))
        ai_attempted = self._parse_iso(state.get('ai_summary_attempted'))
        ai_transient = state.get('ai_summary_transient') is True
        snapshot = state.get('snapshot') if isinstance(state.get('snapshot'), str) else None
        last_updated = state.get('last_updated') if isinstance(state.get('last_updated'), str) else None
        # status가 없는 옛 state는 글이 있으면 ok로 본다 (idle은 '아직 수집 전'이라는 뜻)
        status = state.get('status') if state.get('status') in STATUSES else ('ok' if posts else 'idle')
        health = self._import_health(state.get('source_health'))
        prev_day = self._import_prev_day(state.get('prev_day'))
        il = state.get('issuelink') if isinstance(state.get('issuelink'), dict) else {}
        il_at = self._parse_iso(il.get('at'))
        il_sources = ({s for s in il['sources'] if isinstance(s, str)}
                      if isinstance(il.get('sources'), list) else set())
        il_cache = {k: v for k, v in il['cache'].items() if isinstance(k, str) and isinstance(v, str)} \
            if isinstance(il.get('cache'), dict) else {}
        # 이슈 블록은 state에 없으므로 같은 입력(posts, 점수 이력)과 그때 시각(last_updated)으로 다시 계산한다
        issues = self._build_issues(posts, score_history, self._parse_iso(last_updated) or datetime.now(KST))
        with self._lock:
            self._post_score_history = score_history
            self._history_times = times
            self._posts = posts
            self._issues = issues
            self._issue_log = issue_log
            self._ai_summary = ai_summary
            self._ai_summary_updated = ai_updated if ai_summary else None
            self._ai_summary_attempted = ai_attempted
            self._ai_summary_transient = ai_transient
            self._snapshot = snapshot if posts else None
            self._crawl_count = crawl_count
            self._last_updated = last_updated
            self._status = status
            self._source_health = health
            self._prev_day = prev_day
            self._il_at = il_at
            self._il_sources = il_sources
            self._il_cache = il_cache
        n_prev = sum(1 for p in posts if p.get('prev_day'))
        print(f'[State] 복원: posts {len(posts)}(전날 보충 {n_prev}), 점수 이력 {len(score_history)}, '
              f'이슈 {len(issues["items"])}, 이슈 로그 {len(issue_log)}, crawl_count {crawl_count}, status {status}, '
              f'source_health {len(health)}곳, 전날 상위 글 {len((prev_day or {}).get("posts") or [])}')

    @staticmethod
    def _import_health(raw) -> dict:
        """state의 source_health 정리 — 형식이 틀린 항목은 버린다."""
        out = {}
        if not isinstance(raw, dict):
            return out
        for s, e in raw.items():
            if not isinstance(s, str) or not isinstance(e, dict) or e.get('status') not in HEALTH_STATUSES:
                continue
            entry = {'status': e['status'],
                     'last_update': e.get('last_update') if isinstance(e.get('last_update'), str) else None,
                     'last_new': e.get('last_new') if isinstance(e.get('last_new'), str) else None,
                     'n': e.get('n') if isinstance(e.get('n'), int) and e.get('n') >= 0 else 0,
                     'since': e.get('since') if isinstance(e.get('since'), str) else None,
                     'via': [v for v in e.get('via') or [] if v in VIA_ORDER] if isinstance(e.get('via'), list) else []}
            if isinstance(e.get('note'), str) and e.get('note'):
                entry['note'] = e['note']
            out[s] = entry
        return out

    @staticmethod
    def _import_prev_day(raw):
        """state의 prev_day 정리 — {date, done, posts}. 형식이 틀리면 None."""
        if not isinstance(raw, dict) or not isinstance(raw.get('date'), str):
            return None
        raw_posts = raw.get('posts') if isinstance(raw.get('posts'), list) else []
        posts = [dict(p) for p in raw_posts
                 if isinstance(p, dict) and _is_http_url(p.get('url')) and p.get('title') and p.get('source')]
        return {'date': raw['date'], 'done': raw.get('done') if isinstance(raw.get('done'), str) else None,
                'posts': posts}

    @staticmethod
    def _parse_iso(value):
        if not isinstance(value, str) or not value:
            return None
        try:
            dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=KST)

    # ── TodayBestStory ────────────────────────────────────────────────────────

    def _fetch_todaybeststory(self, now=None) -> list:
        now = now or datetime.now(KST)
        end = now.strftime('%Y-%m-%d')   # TBS targetDate는 KST 기준
        # KST 자정 직후에는 오늘 목록이 아직 작으므로 02시 전까지는 어제 목록도 같이 받는다
        start = (now - timedelta(days=1)).strftime('%Y-%m-%d') if now.hour < MERGE_PREV_END_HOUR else end
        limit = TBS_PAGE_LIMITS[(now.minute // 10) % 2]
        raw_posts = []
        pages_ok = 0
        totals = []   # 페이지마다 total — 받는 도중 TBS 목록이 바뀌면 달라진다(페이지 경계 중복·누락)
        self._tbs_stats = None
        # 다음 페이지가 남았는데 멈췄는지. 응답이 조회수순이라 앞 몇 페이지만 받으면 일부 커뮤니티만 남는다
        self._tbs_truncated = True
        deadline = time.monotonic() + TBS_DEADLINE
        for page in range(1, TBS_MAX_PAGES + 1):
            if time.monotonic() > deadline:
                print(f'[TodayBestStory] {TBS_DEADLINE}초 초과 - {pages_ok}페이지까지만 사용')
                break
            data = None
            for attempt in range(2):
                try:
                    headers = _random_headers({'Referer': 'https://todaybeststory.com/communities'})
                    r = requests.get(TBS_API_URL, headers=headers,
                        params={'startDate': start, 'endDate': end, 'page': page, 'limit': limit},
                        timeout=15)
                    r.raise_for_status()
                    data = r.json()
                    break
                except Exception as e:
                    print(f'[TodayBestStory] 오류 (page {page}, 시도 {attempt + 1}): {e}')
                    time.sleep(1)
            if data is None:
                break   # 받은 페이지까지만 쓴다
            pages_ok += 1
            raw_posts.extend(data.get('items') or [])
            if isinstance(data.get('total'), int):
                totals.append(data['total'])
            if not data.get('hasNext'):
                self._tbs_truncated = False
                break
            time.sleep(random.uniform(0.2, 0.4))
        else:
            print(f'[TodayBestStory] 안전 상한 {TBS_MAX_PAGES}페이지 도달 - 나머지 생략')
        if len(set(totals)) > 1:
            print(f'[TodayBestStory] 페이지별 total 불일치 {min(totals)}~{max(totals)} '
                  f'(받는 도중 목록이 갱신됨 - 페이지 경계 중복·누락 가능)')
        if not raw_posts:
            return []
        self._tbs_stats = self._tbs_source_stats(raw_posts, limit, pages_ok, len(set(totals)) > 1)

        items = []
        seen = set()
        src_pos = Counter()   # position_score는 커뮤니티 안에서의 순번 (응답이 조회수순이라 전체 순번을 쓰면 뒤쪽 커뮤니티가 0점)
        for post in raw_posts:
            cid = post.get('communityId') or ''
            src_info = COMMUNITY_ID_MAP.get(cid)
            if not src_info:
                if not re.fullmatch(r'[A-Z0-9]{2,5}', cid):
                    continue
                # 새로 추가된 커뮤니티는 API의 이름으로 받는다
                src_info = (cid.lower(), post.get('communityName') or cid, '📝', '#7c6cff')
            src_id, src_label, src_emoji, src_color = src_info

            title = re.sub(r'\s+', ' ', post.get('postTitle') or '').strip()
            url = (post.get('postUrl') or '').strip()
            if len(title) < 4 or not _is_http_url(url) or url in seen:
                continue
            seen.add(url)

            views = 0 if cid in SYNTHETIC_VIEW_SOURCES else (post.get('readCount') or 0)
            # postDesc는 본문이 아니라 게시판 이름 → summary에 넣지 않고 board로만 둔다.
            # postContent(네이트판 등)는 실제 본문 앞부분이라 요약으로 쓴다.
            content = re.sub(r'\s+', ' ', post.get('postContent') or '').strip()

            n = src_pos[cid]
            src_pos[cid] += 1
            items.append({
                'source': src_id,
                'source_label': src_label,
                'source_emoji': src_emoji,
                'source_color': src_color,
                'title': title,
                'summary': content[:130] + ('…' if len(content) > 130 else ''),
                'board': (post.get('postDesc') or '').strip(),
                'url': url,
                'author': post.get('postWriterName') or '',
                'date': self._tbs_date(post),
                **self._classify(title),
                'views': views,
                'likes': post.get('upvoteCount') or 0,
                'comments': post.get('commentCount') or 0,
                'position_score': max(0.0, 100 - n * 1.5),
                'rank_score': 0,
                'rank': 0,
                # 베스트에 오른 날짜(KST). 작성일(date)이 전날인 글도 당일 목록에 많다 — 전날 상위 글 저장·이전 글 유지 판단용
                # (state에는 남기고 공개 JSON에는 안 낸다)
                'target_date': post.get('targetDate') or '',
            })
        print(f'[TodayBestStory] {start}~{end} limit {limit} {pages_ok}페이지, {len(raw_posts)}개 원본 → '
              f'{len(items)}개 파싱 완료 ({len(src_pos)}개 커뮤니티)')
        return items

    @staticmethod
    def _tbs_source_stats(raw_posts: list, limit: int, pages: int, mismatch: bool) -> dict:
        """TBS 원본 응답의 커뮤니티별 집계 — 마지막 갱신(updateDatetime 최댓값), 마지막 새 글(creationDatetime 최댓값),
        글 수. TBS는 커뮤니티마다 매시 한 번 목록을 다시 받아 updateDatetime을 고친다."""
        by_src: dict = {}
        max_upd = None
        seen = set()
        for post in raw_posts:
            if not isinstance(post, dict):
                continue
            pid = post.get('id') or post.get('postUrl')
            if pid in seen:
                continue
            seen.add(pid)
            cid = post.get('communityId') or ''
            info = COMMUNITY_ID_MAP.get(cid)
            if not info and not re.fullmatch(r'[A-Z0-9]{2,5}', cid):
                continue
            sid = info[0] if info else cid.lower()
            st = by_src.setdefault(sid, {'upd': None, 'new': None, 'n': 0})
            st['n'] += 1
            upd = _parse_iso_dt(post.get('updateDatetime'))
            new = _parse_iso_dt(post.get('creationDatetime'))
            if upd and (st['upd'] is None or upd > st['upd']):
                st['upd'] = upd
            if new and (st['new'] is None or new > st['new']):
                st['new'] = new
            if upd and (max_upd is None or upd > max_upd):
                max_upd = upd
        return {'sources': by_src, 'max_upd': max_upd, 'limit': limit, 'pages': pages, 'mismatch': mismatch}

    def _tbs_date(self, post: dict) -> str:
        raw = post.get('postDatetime') or ''
        if raw:
            try:
                dt = datetime.fromisoformat(raw.replace('Z', '+00:00'))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt.year >= 2000:   # 1969-12-31T15:00Z 같은 가짜 값 제외
                    k = dt.astimezone(KST)
                    # 시각을 모르는 글(인스티즈 대부분)은 KST 자정(15:00:00Z)으로 온다 → 날짜만
                    if (k.hour, k.minute, k.second, k.microsecond) == (0, 0, 0, 0):
                        return k.strftime('%Y-%m-%d')
                    return _fmt_dt(dt)
            except ValueError:
                pass
        # 작성 시각이 없으면(네이트판 전부 등) 베스트에 오른 날짜(KST)만 쓴다
        target = post.get('targetDate') or ''
        return target if re.fullmatch(r'\d{4}-\d{2}-\d{2}', target) else ''

    AI_SUMMARY_INTERVAL = 21600   # 성공 후 다음 요약까지 (6시간)
    AI_SUMMARY_RETRY = 7200       # 실패 후 재시도까지 (2시간). 10분마다 재시도하면 데일리와 같이 쓰는 무료 일일 한도를 다 쓴다
    AI_SUMMARY_RETRY_TRANSIENT = 1800   # 503·타임아웃 같은 일시적 실패 후 재시도까지 (30분)

    def _collect_all_posts(self, sink: list, now=None) -> None:
        """포스트 수집 전체 단계. 결과를 sink에 바로 넣어서 master timeout이 나도 모은 만큼은 살린다.
        TBS를 받은 뒤 커뮤니티 단위로 판정(_plan_fill)해서, 빠졌거나 멈춘 커뮤니티만 직접 스크래핑·이슈링크로 채운다."""
        now = now or datetime.now(KST)
        # TodayBestStory API - 90초 inner hard timeout (DNS/TCP hang 방지)
        api_posts = []
        _tbs_ex = ThreadPoolExecutor(max_workers=1)
        try:
            api_posts = _tbs_ex.submit(self._fetch_todaybeststory, now).result(timeout=90)
        except Exception as e:
            print(f'[Refresh] TodayBestStory inner timeout/오류: {e}')
        finally:
            _tbs_ex.shutdown(wait=False)
        sink.extend(api_posts)

        # 끝까지 받아야 TBS 목록이 온전하다 (몇 페이지만 받고 끊기면 조회수 상위 커뮤니티만 남아 한쪽으로 쏠린다)
        truncated = bool(api_posts) and getattr(self, '_tbs_truncated', False)
        complete = bool(api_posts) and not truncated
        self._collect_complete = complete
        plan = self._plan_fill(api_posts, complete, now)
        self._plan = plan
        direct = [src for src in COMMUNITY_SOURCES if src['id'] in plan['direct']]
        il_want = plan['il_urgent'] | plan['thin']
        n_src = len({p['source'] for p in api_posts})
        if not direct and not il_want:
            print(f'[Refresh] TodayBestStory {len(api_posts)}개/{n_src}개 커뮤니티 - 예비 경로 생략')
            return

        why = '장애' if not api_posts else '중간에 끊김' if truncated else '온전'
        print(f'[Refresh] TodayBestStory {len(api_posts)}개/{n_src}개 커뮤니티({why}) — '
              f'갱신 멈춤 {sorted(plan["degraded"]) or "-"}, 빠짐 {sorted(plan["missing"]) or "-"}'
              f'{", 25개 미만 " + str(sorted(plan["incomplete"])) if plan["incomplete"] else ""} → '
              f'직접 스크래핑 {[s["id"] for s in direct] or "-"}, 이슈링크 {sorted(il_want) or "-"}')
        # 이슈링크는 직접 스크래핑과 동시에 (서로 다른 서버)
        il_ex = ThreadPoolExecutor(max_workers=1)
        il_future = il_ex.submit(self._issuelink_posts, plan, now) if il_want else None
        try:
            # 소스끼리는 병렬, 한 소스 안의 페이지는 순차(간격을 둔다)
            if direct:
                with ThreadPoolExecutor(max_workers=5) as ex:
                    for items in ex.map(self._scrape_source, direct):
                        for p in items:
                            p['via'] = 'direct'
                        sink.extend(items)
            if il_future is not None:
                try:
                    sink.extend(il_future.result(timeout=ISSUELINK_TIMEOUT + 5))
                except Exception as e:
                    print(f'[IssueLink] 호출 timeout/오류: {type(e).__name__}: {e}')
        finally:
            il_ex.shutdown(wait=False)

    def _expected_sources(self, now: datetime) -> set:
        """지금 시각 TBS에 당일 글이 있어야 정상인 커뮤니티 (EXPECTED_BY_HOUR 고정 기대치)."""
        if now.hour < MERGE_PREV_END_HOUR:   # 전날 목록을 같이 받는 시간
            return {s for s, h in EXPECTED_BY_HOUR.items() if h is not None}
        return {s for s, h in EXPECTED_BY_HOUR.items() if h is not None and now.hour >= h}

    @staticmethod
    def _is_stalled(upd, now: datetime) -> bool:
        """TBS 갱신이 HEALTH_STALE_MIN분 넘게 멈췄나 (KST 06~24시에만 판정)"""
        return (upd is not None and now.hour >= HEALTH_FROM_HOUR
                and (now - upd).total_seconds() > HEALTH_STALE_MIN * 60)

    def _plan_fill(self, api_posts: list, complete: bool, now: datetime) -> dict:
        """TBS 결과로 커뮤니티를 판정하고 예비 경로 대상을 정한다.
        - degraded: TBS에 글은 있는데 갱신이 멈춤 / missing: 기대 시각이 지났는데 TBS에 없음
        - incomplete: TBS가 온전하지 않을 때(장애·끊김·결과 너무 적음) 25개를 못 채운 커뮤니티
        - 직접 스크래핑: degraded·missing·incomplete 중 COMMUNITY_SOURCES에 있는 곳
        - 이슈링크: (a) degraded·missing·incomplete, (b) TBS 글이 ISSUELINK_THIN개 미만인 곳(thin).
          오전 보충 중(02~12시)에는 전날 상위 글이 빈 칸을 다 채울 커뮤니티(_prev_day_covered)를 (b)에서 뺀다 — 새벽에는
          큰 커뮤니티도 TBS 첫 수집(01~06시) 전이라 당일 글이 몇 건뿐인데, (b)는 평소에도 글이 아주 적은 곳(오유 등)용이다"""
        stats = (getattr(self, '_tbs_stats', None) or {}) if api_posts else {}
        src_stats = stats.get('sources') or {}
        n_by = Counter(p['source'] for p in api_posts)
        expected = self._expected_sources(now)
        degraded = {s for s, st in src_stats.items() if n_by.get(s) and self._is_stalled(st.get('upd'), now)}
        missing = expected - set(n_by)
        weak = not complete or len(api_posts) < TBS_MIN_POSTS or len(n_by) < TBS_MIN_SOURCES
        incomplete = ({s for s in set(SOURCE_META) | set(n_by) if n_by.get(s, 0) < MAX_PER_SOURCE} - missing
                      if weak else set())
        problems = degraded | missing | incomplete
        max_upd = stats.get('max_upd')
        thin = {s for s in SOURCE_META if n_by.get(s, 0) < ISSUELINK_THIN} - problems
        return {
            'expected': expected, 'degraded': degraded, 'missing': missing, 'incomplete': incomplete,
            'direct': problems & {src['id'] for src in COMMUNITY_SOURCES},
            'il_urgent': problems,
            'thin': thin - self._prev_day_covered(api_posts, now),
            'tbs_n': dict(n_by), 'stats': src_stats, 'complete': complete,
            'tbs_stale': bool(api_posts) and self._is_stalled(max_upd, now), 'max_upd': max_upd,
        }

    # ── 이슈링크 (2차 소스, sources_issuelink.py) ─────────────────────────────

    def _issuelink_posts(self, plan: dict, now: datetime) -> list:
        """결정 2의 (a) TBS에서 빠졌거나 멈춘 커뮤니티, (b) TBS 글이 아주 적은 커뮤니티만 이슈링크로 받는다.
        (a)는 매 실행 새로 받는다. (b)는 (a)가 같이 있어도 ISSUELINK_THIN_EVERY_MIN분 간격으로만 새로 받고, 그 사이에는
        직전 목록의 이슈링크 글을 다시 쓴다(인스티즈는 거의 매일 저녁 내내 (a)라 (b)까지 10분마다 받게 된다).
        _il_at·_il_sources는 (b)를 새로 받은 시각·그때의 (b) 커뮤니티다. 모듈이 없거나 실패해도 [] (크롤은 계속)."""
        try:
            import sources_issuelink as il   # 사용 지점에서 import - 모듈이 없거나 깨져도 크롤은 계속
        except Exception as e:
            print(f'[IssueLink] 모듈 없음/import 실패 - 건너뜀: {type(e).__name__}: {e}')
            return []
        mapped = set(getattr(il, 'SOURCE_MAP', {}).values())
        urgent = plan['il_urgent'] & mapped if mapped else set(plan['il_urgent'])
        thin = (plan['thin'] & mapped if mapped else set(plan['thin'])) - urgent
        if not urgent and not thin:
            return []
        window = self._date_window(now)
        reused = []
        reuse = (bool(thin) and self._il_at is not None and thin <= self._il_sources
                 and 0 <= (now - self._il_at).total_seconds() < ISSUELINK_THIN_EVERY_MIN * 60)
        if reuse:
            with self._lock:
                prev = [p for p in self._posts if p.get('via') == 'issuelink' and p['source'] in thin
                        and not p.get('prev_day')]
            reused = self._clean_external(prev, thin, 'issuelink', window)
            print(f'[IssueLink] 적음 {sorted(thin)} — {ISSUELINK_THIN_EVERY_MIN}분 안에 받은 글 {len(reused)}개 다시 사용')
            if not urgent:
                return reused
        want = urgent | (set() if reuse else thin)
        fetch = getattr(il, 'fetch', None)
        if not callable(fetch):
            print('[IssueLink] fetch() 없음 - 건너뜀')
            return reused
        kwargs = {}
        try:
            if 'cache' in inspect.signature(fetch).parameters:
                kwargs['cache'] = self._il_cache   # 원본 URL 캐시를 실행 사이에 이어 쓴다 (모듈이 받을 때만)
        except (TypeError, ValueError):
            pass
        ex = ThreadPoolExecutor(max_workers=1)
        try:
            items = ex.submit(fetch, set(want), now, **kwargs).result(timeout=ISSUELINK_TIMEOUT)
        except Exception as e:
            print(f'[IssueLink] 오류/timeout - 건너뜀: {type(e).__name__}: {e}')
            return reused
        finally:
            ex.shutdown(wait=False)
        if thin and not reuse:
            self._il_at, self._il_sources = now, set(thin)
        out = self._clean_external(items, want, 'issuelink', window)
        got = Counter(p['source'] for p in out)
        print(f'[IssueLink] 요청 {len(want)}곳(급함 {sorted(urgent) or "-"}, '
              f'적음 {"-" if reuse else sorted(thin) or "-"}) → '
              f'{len(out)}개 ({", ".join(f"{s} {n}" for s, n in got.most_common()) or "없음"})')
        return out + reused

    @staticmethod
    def _date_window(now: datetime) -> set:
        """지금 수집 목록의 날짜(KST) — 02시 전에는 어제·오늘, 그 뒤에는 오늘"""
        today = now.strftime('%Y-%m-%d')
        return ({today, (now - timedelta(days=1)).strftime('%Y-%m-%d')} if now.hour < MERGE_PREV_END_HOUR
                else {today})

    def _clean_external(self, items, want: set, via: str, window: set) -> list:
        """외부 모듈(이슈링크) 결과를 크롤러 글 형식으로 정리한다 — 요청한 커뮤니티·http(s) 링크·지금 날짜 창의 글만.
        라벨·이모지·색은 SOURCE_META(TBS와 같은 칩)로 맞추고, position_score는 커뮤니티 안 순서로 다시 매긴다."""
        out, pos = [], Counter()
        for it in items or []:
            if not isinstance(it, dict):
                continue
            sid = it.get('source')
            title = re.sub(r'\s+', ' ', str(it.get('title') or '')).strip()
            url = str(it.get('url') or '').strip()
            if sid not in want or len(title) < 4 or not _is_http_url(url):
                continue
            date = it.get('date') if isinstance(it.get('date'), str) else ''
            if date and date[:10] not in window:
                continue
            label, emoji, color = SOURCE_META.get(sid) or (str(it.get('source_label') or sid), '📝', '#7c6cff')
            summary = re.sub(r'\s+', ' ', str(it.get('summary') or '')).strip()
            n = pos[sid]
            pos[sid] += 1
            out.append({
                'source': sid, 'source_label': label, 'source_emoji': emoji, 'source_color': color,
                'title': title,
                'summary': summary[:130] + ('…' if len(summary) > 130 else ''),
                'board': '', 'url': url, 'author': str(it.get('author') or ''), 'date': date,
                **self._classify(title),
                'views': 0 if sid in SYNTHETIC_VIEW_SOURCE_IDS else self._as_count(it.get('views')),
                'likes': self._as_count(it.get('likes')),
                'comments': self._as_count(it.get('comments')),
                'position_score': max(0.0, 100 - n * 1.5), 'rank_score': 0, 'rank': 0, 'via': via,
            })
        return out

    @staticmethod
    def _as_count(v) -> int:
        try:
            return max(0, int(v or 0))
        except (TypeError, ValueError):
            return 0

    def refresh(self, now=None):
        """크롤 1회. now: 판정 기준 시각(테스트·build.py --now). 없으면 실제 시각."""
        # 이미 크롤 중이면 즉시 반환 (동시 실행 시 요청 폭증·history 압축 방지)
        if not self._refresh_lock.acquire(blocking=False):
            print('[Refresh] 이미 크롤 중 - 건너뜀')
            return
        try:
            with self._lock:
                self._status = 'crawling'
            try:
                self._refresh_body(now)
            except Exception as e:
                print(f'[Refresh] 예외: {e}')
            finally:
                with self._lock:
                    if self._status == 'crawling':
                        # 예외로 끝났으면 이전 데이터를 그대로 두고 stale로 표시 (last_updated·crawl_count는 그대로)
                        self._status = 'stale'
                        print('[Refresh] 안전망: 이전 데이터 유지, status stale')
        finally:
            self._refresh_lock.release()

    def _refresh_body(self, now_override=None):
        # 기준 시각은 실행마다 한 번만 정해 수집(TBS 날짜 범위·커뮤니티 판정)·이전 글 유지·전날 보충·저장에 모두 쓴다.
        # 수집 뒤에 시계를 다시 읽으면 01:59에 시작해 TBS를 받는 동안 02시를 넘긴 실행이 전날+오늘 목록을
        # '02시 이후 당일 목록'으로 보고 그날 오전 보충을 끝낸다
        now = (now_override or datetime.now(KST)).replace(microsecond=0)
        # 전체 포스트 수집 - 180초 master hard timeout (DNS/TCP hang 완전 차단)
        sink = []
        self._collect_complete = False
        self._plan = None
        self._tbs_stats = None
        self._direct_status = {}
        _collect_ex = ThreadPoolExecutor(max_workers=1)
        try:
            _collect_ex.submit(self._collect_all_posts, sink, now).result(timeout=180)
        except Exception as e:
            print(f'[Refresh] 포스트 수집 master timeout/오류: {e} - 모은 {len(sink)}개로 진행')
        finally:
            _collect_ex.shutdown(wait=False)
        posts = list(sink)
        complete = self._collect_complete
        # TBS 단계에서 예외로 끝나 계획이 없으면 받은 TBS 글로 다시 판정한다 (온전하지 않은 것으로 본다)
        plan = self._plan or self._plan_fill([p for p in posts if not p.get('via')], False, now)
        complete = complete and plan['complete']

        # 중복 제거 (같은 글의 URL 변형 포함) + http(s) 링크만
        unique = self._dedup(posts, plan['degraded'])
        with self._lock:
            prev_posts = list(self._posts)
            prev_snapshot = self._snapshot
        # 커뮤니티별 상태는 채택 여부와 상관없이 갱신한다 (진단·알림용)
        health = self._update_source_health(plan, unique, now)

        n_src = len({p['source'] for p in unique})
        kept_n, kept_src = len(prev_posts), len({p['source'] for p in prev_posts})
        # 0건이거나, 이전보다 적은 몇 건만 건졌거나, TBS가 온전하지 않은데(장애·끊김) 예비 경로까지 합쳐도
        # 절대 기준(FALLBACK_MIN_SOURCES곳·FALLBACK_MIN_POSTS건)에 못 미치면 수집 실패로 본다
        reject = ''
        if not unique:
            reject = '0건'
        elif len(unique) < MIN_OK_POSTS and kept_n > len(unique):
            reject = f'{MIN_OK_POSTS}건 미만'
        elif not complete and (n_src < FALLBACK_MIN_SOURCES or len(unique) < FALLBACK_MIN_POSTS):
            reject = f'TBS 없이 {FALLBACK_MIN_SOURCES}곳·{FALLBACK_MIN_POSTS}건 미만'
        if reject:
            # 이전 posts·trends 유지, history에도 넣지 않는다 (다음 라운드 가짜 급상승 방지)
            with self._lock:
                self._status = 'stale'
                self._source_health = health
            print(f'[Refresh] 수집 {len(unique)}건/{n_src}개 커뮤니티({reject}) - '
                  f'이전 데이터 {kept_n}개/{kept_src}개 유지 (status stale)')
            return

        # 이번에 빠졌거나 모자란(TBS가 온전하지 않을 때) 커뮤니티는 이전 글을 유지해 병합한다
        kept = self._kept_posts(prev_posts, unique, complete, now)
        ranked = self._assign_ranks(unique + kept)
        # 00~02시: 전날 커뮤니티별 상위 글 저장 / 02~12시: 당일 글이 모자란 칸만 전날 글로 채운다
        self._save_prev_day(posts, complete, now)
        extra = self._prev_day_fill(ranked, now)
        final = ranked + extra
        snapshot = self._snapshot_sig(unique + kept + extra)
        status = self._decide_status(plan, complete)
        same = bool(prev_posts) and snapshot == prev_snapshot

        # TBS는 약 1시간마다 갱신된다. 직전과 같은 목록을 다시 받았으면 이력·갱신 시각·crawl_count를 그대로 둔다
        # (같은 목록을 매번 쌓으면 급상승이 3라운드 뒤 사라지고 'n분 전 갱신'이 실제보다 새것처럼 보인다)
        if same:
            # 글·이력은 그대로 두고 이슈만 지금 시각으로 다시 계산한다 (신규·잠잠 판정이 시간에 따라 바뀐다)
            with self._lock:
                cur_posts, score_history = list(self._posts), list(self._post_score_history)
            cur_today = [p for p in cur_posts if not p.get('prev_day')]
            new_summary, transient = (self._generate_ai_summary(cur_today) if self._ai_summary_due(now)
                                      else (None, False))
            issues = self._build_issues(cur_posts, score_history, now)
            with self._lock:
                self._status = status
                self._source_health = health
                self._set_issues(issues)
                self._apply_ai_summary(new_summary, transient, now)
            print(f'[Refresh] 원본 목록이 직전과 같음 ({len(unique)}건) - 이력·갱신 시각 유지 (status {status})')
            return

        today_posts = [p for p in final if not p.get('prev_day')]
        with self._lock:
            prev_post_history = list(self._post_score_history)
            prev_times = list(self._history_times)
            prev_summary = {_url_key(p['url']): p['summary'] for p in self._posts if p.get('summary')}
        self._compute_post_velocity(today_posts, prev_post_history)
        for p in extra:
            p['post_velocity'] = 0.0
        # 점수 이력은 TBS를 끝까지(멈추지 않은 채로) 받은 라운드만 — 예비 경로·이전 글로 메운 라운드를 넣으면
        # 다음 라운드에 가짜 신규·급상승이 생긴다. 키는 _url_key: 전체 URL보다 짧고(state 크기),
        # ?page= 같은 변형이 바뀌어도 같은 글로 이어진다
        if complete and not plan['tbs_stale']:
            curr_scores = {_url_key(p['url']): p.get('rank_score', 0.0) for p in today_posts}
            score_history, history_times = self._push_history(prev_post_history, prev_times, curr_scores, now)
        else:
            score_history, history_times = prev_post_history, prev_times

        # 직전 라운드에서 받아 둔 본문 요약은 재사용 (같은 글을 매번 다시 요청하지 않게)
        for p in final:
            if not p.get('summary'):
                p['summary'] = prev_summary.get(_url_key(p['url']), '')

        # 상위 포스트 본문 요약 병렬 수집 - 25초 hard timeout
        to_summarize = [p for p in today_posts[:SUMMARY_MAX_POSTS]
                        if p['source'] in SUMMARY_SELECTORS and not p['summary']]
        if to_summarize:
            ex = ThreadPoolExecutor(max_workers=4)
            try:
                list(ex.map(self._fetch_summary, to_summarize, timeout=25))
            except Exception:
                pass
            finally:
                ex.shutdown(wait=False)

        # 이슈 보드는 전날 보충 글을 빼고 계산한다 (issues.build_issues가 prev_day 글을 거른다)
        issues = self._build_issues(final, score_history, now)

        # AI 요약 갱신 (성공 후 6시간, 실패 후 2시간·일시적 실패 후 30분 간격) — 당일 글만
        new_summary, transient = (self._generate_ai_summary(today_posts) if self._ai_summary_due(now)
                                  else (None, False))

        with self._lock:
            self._posts = final
            self._post_score_history = score_history
            self._history_times = history_times
            self._set_issues(issues)
            # 데이터가 바뀐 시각 (같은 목록을 다시 받은 실행에서는 바꾸지 않는다)
            self._last_updated = now.isoformat(timespec='seconds')
            self._crawl_count += 1
            self._snapshot = snapshot
            self._status = status
            self._source_health = health
            self._apply_ai_summary(new_summary, transient, now)
        via = Counter(p.get('via') or 'tbs' for p in today_posts)
        print(f'[Refresh] status {status} — 게시 {len(final)}건/{len({p["source"] for p in final})}곳 '
              f'(당일 {len(today_posts)}건: {", ".join(f"{k} {v}" for k, v in via.most_common())}; '
              f'이전 글 유지 {sum(1 for p in final if p.get("kept"))}, 전날 보충 {len(extra)}), '
              f'점수 이력 {len(score_history)}칸')

    @staticmethod
    def _dedup(posts: list, degraded: set) -> list:
        """중복 제거(같은 글의 URL 변형 포함) + http(s) 링크만. 같은 글이 여러 경로로 오면
        건강한 TBS → 직접 스크래핑 → 이슈링크 → 갱신이 멈춘 TBS 순으로 고른다(멈춘 커뮤니티는 새 경로의 수치가 맞다).
        URL이 달라도 다른 경로에서 먼저 고른 같은 커뮤니티·같은 제목(_title_key) 글이면 같은 글로 보고 뺀다
        (SLR·보배드림은 경로마다 글 번호가 다르다). 한 경로 안에서 제목만 같은 글은 다른 글일 수 있어 둘 다 둔다."""
        order = {'direct': 1, 'issuelink': 2}

        def prio(i):
            p = posts[i]
            via = p.get('via')
            if via:
                return order.get(via, 2), i
            return (3 if p.get('source') in degraded else 0), i

        seen, first_via, unique = set(), {}, []
        for i in sorted(range(len(posts)), key=prio):
            p = posts[i]
            if not _is_http_url(p.get('url')):
                continue
            key = _url_key(p['url'])
            if key in seen:
                continue
            via, tk = p.get('via') or 'tbs', _title_key(p)
            if tk and first_via.setdefault(tk, via) != via:
                continue
            seen.add(key)
            unique.append(p)
        return unique

    def _kept_posts(self, prev_posts: list, unique: list, complete: bool, now: datetime) -> list:
        """이번 수집에서 빠진 커뮤니티의 이전 글을 유지한다(kept 표시).
        - 이번 결과(예비 경로 포함)에 한 글도 없는 커뮤니티 — TBS 당일 목록은 하루 동안 쌓이기만 해서,
          같은 날 있던 커뮤니티가 통째로 사라지면 수집 장애다
        - TBS가 온전하지 않으면(장애·끊김): MAX_PER_SOURCE개를 못 채운 커뮤니티도 (같은 글은 새 값 우선 —
          URL(_url_key)이나 같은 커뮤니티·같은 제목(_title_key)이 이번 결과에 있으면 유지하지 않는다)
        지금 날짜 창(오늘, 02시 전에는 어제도)의 목록에 든 글만(TBS는 베스트 날짜 target_date, 그 밖에는 작성일) —
        날이 바뀌면 저절로 빠진다. 전날 보충 글은 유지하지 않는다."""
        n_new = Counter(p['source'] for p in unique)
        limit = 1 if complete else MAX_PER_SOURCE
        targets = {p['source'] for p in prev_posts if n_new.get(p['source'], 0) < limit}
        if not targets:
            return []
        window = self._date_window(now)
        keys = {_url_key(p['url']) for p in unique}
        titles = {tk for tk in map(_title_key, unique) if tk}   # 이번에 다른 경로(URL)로 들어온 같은 글
        out, pos = [], Counter()
        for p in prev_posts:
            s = p.get('source')
            listed = p.get('target_date') or (p.get('date') or '')[:10]
            if s not in targets or p.get('prev_day') or listed not in window:
                continue
            k, tk = _url_key(p['url']), _title_key(p)
            if k in keys or (tk and tk in titles):
                continue
            keys.add(k)
            q = {f: v for f, v in p.items() if f not in ('rank', 'rank_score', 'post_velocity')}
            q['kept'] = True
            q['position_score'] = max(0.0, 100 - pos[s] * 1.5)   # state에는 없다 — 이전 순위 순서로 다시 매긴다
            pos[s] += 1
            out.append(q)
        if out:
            print(f'[Refresh] 이전 글 유지 {len(out)}개: ' + ', '.join(f'{s} {n}' for s, n in pos.most_common()))
        return out

    def _decide_status(self, plan: dict, complete: bool) -> str:
        if plan.get('tbs_stale'):
            return 'stale-source'
        if not complete or len(plan['degraded'] | plan['missing']) >= PARTIAL_MIN_PROBLEMS:
            return 'partial'
        return 'ok'

    @staticmethod
    def _push_history(hist: list, times: list, curr: dict, now: datetime) -> tuple:
        """점수 이력에 이번 라운드를 넣는다. 직전 칸이 HISTORY_MERGE_MIN분 안에 시작했으면 덮어쓰고(같은 TBS 갱신 주기),
        HISTORY_MAX_AGE_MIN분 넘은 칸은 버린다. 반환: (이력, 칸마다 시작 시각)"""
        hist = list(hist)
        times = ([None] * len(hist) + list(times))[-len(hist):] if hist else []
        last = _parse_iso_dt(times[-1]) if times else None
        if last is not None and 0 <= (now - last).total_seconds() < HISTORY_MERGE_MIN * 60:
            hist[-1] = curr
        else:
            hist.append(curr)
            times.append(now.isoformat(timespec='seconds'))
        keep = []
        for i, t in enumerate(times):
            dt = _parse_iso_dt(t)
            if dt is None or (now - dt).total_seconds() <= HISTORY_MAX_AGE_MIN * 60:
                keep.append(i)
        keep = keep[-HISTORY_SIZE:]
        return [hist[i] for i in keep], [times[i] for i in keep]

    # ── 오전 보충 (전날 상위 글) ──────────────────────────────────────────────

    def _save_prev_day(self, posts: list, complete: bool, now: datetime) -> None:
        """KST 00~02시(TBS를 전날·오늘 같이 받는 시간)에 전날 베스트의 커뮤니티별 상위 MAX_PER_SOURCE개를 state에 둔다.
        추가 호출은 없다. 끊긴 TBS 결과로는 이미 저장한 같은 날 목록을 덮어쓰지 않는다."""
        if now.hour >= MERGE_PREV_END_HOUR:
            return
        yday = (now - timedelta(days=1)).strftime('%Y-%m-%d')
        ys = [dict(p) for p in posts if not p.get('via') and p.get('target_date') == yday]
        if not ys:
            return
        with self._lock:
            cur = self._prev_day
        if not complete and cur and cur.get('date') == yday and cur.get('posts'):
            return
        ranked = self._assign_ranks(ys)
        slim = [{k: p[k] for k in PREV_DAY_FIELDS if k in p} for p in ranked]
        with self._lock:
            self._prev_day = {'date': yday, 'done': None, 'posts': slim}
        print(f'[PrevDay] {yday} 커뮤니티별 상위 글 {len(slim)}개/{len({p["source"] for p in slim})}곳 저장')

    def _prev_day_posts(self, now: datetime):
        """오전 보충에 쓸 전날 상위 글 — KST 02~12시이고 어제 날짜로 저장돼 있을 때만, 아니면 None"""
        with self._lock:
            pd = self._prev_day
        if not pd or not (MERGE_PREV_END_HOUR <= now.hour < PREV_DAY_END_HOUR):
            return None
        if pd.get('date') != (now - timedelta(days=1)).strftime('%Y-%m-%d'):
            return None
        return pd.get('posts') or None

    @staticmethod
    def _tbs_today_size(posts: list, today: str) -> tuple:
        """전날 보충을 멈출지 보는 당일 글 규모 (커뮤니티 수, 커뮤니티당 MAX_PER_SOURCE개까지 센 글 수).
        TBS 당일 글만 센다 — 이슈링크·직접 수집 글(via)은 TBS 글이 모이면 빠지는 일시적인 글이고,
        이전 글 유지(kept)는 지난 목록, target_date가 전날인 글은 02시 전 목록이다"""
        n = Counter(p.get('source') for p in posts
                    if not p.get('via') and not p.get('kept') and not p.get('prev_day')
                    and (p.get('target_date') or today) == today)
        return len(n), sum(min(c, MAX_PER_SOURCE) for c in n.values())

    def _prev_day_covered(self, tbs_posts: list, now: datetime) -> set:
        """이번 실행에서 오전 보충이 빈 칸을 다 채울 수 있는 커뮤니티 — 저장한 전날 상위 글이 MAX_PER_SOURCE개인 곳,
        곧 어제는 글이 넉넉했던 곳(새벽에 TBS 첫 수집 전이라 당일 글이 몇 건뿐일 뿐이다).
        어제도 글이 적었던 곳(오유·인벤·웃대 등, '평소 글이 적은 곳')은 넣지 않는다 — 결정 2-b대로 이슈링크로 보탠다.
        보충 시간이 아니거나 TBS 당일 글이 이미 멈춤 기준을 넘었으면 빈 집합 (_plan_fill의 (b) 판정용)"""
        pd = self._prev_day_posts(now)
        if not pd:
            return set()
        n_src, n_posts = self._tbs_today_size(tbs_posts, now.strftime('%Y-%m-%d'))
        if n_src >= PREV_DAY_DONE_SOURCES and n_posts >= PREV_DAY_DONE_POSTS:
            return set()
        return {s for s, c in Counter(p.get('source') for p in pd).items() if s and c >= MAX_PER_SOURCE}

    def _prev_day_fill(self, ranked: list, now: datetime) -> list:
        """KST 02~12시: 당일 글이 MAX_PER_SOURCE개에 못 미치는 커뮤니티 칸만 전날 상위 글로 채운다(prev_day: True).
        TBS 당일 글만으로(_tbs_today_size) PREV_DAY_DONE_SOURCES곳·PREV_DAY_DONE_POSTS건이 되면 채우지 않고,
        12시가 되면 저장한 전날 글을 지워 그날은 끝낸다. 12시 전에는 저장한 글을 지우지 않고 실행마다 다시 판정한다
        (한 번 잘못 끝내면 그날 오전은 되돌릴 수 없으므로. TBS 당일 목록은 쌓이기만 해서 평소에는 한 번 넘으면 계속 넘는다).
        반환: 채운 글(순위는 당일 글 뒤에 이어 붙인다)"""
        with self._lock:
            pd = self._prev_day
        if not pd or now.hour < MERGE_PREV_END_HOUR:
            return []
        today = now.strftime('%Y-%m-%d')
        yday = (now - timedelta(days=1)).strftime('%Y-%m-%d')
        if pd.get('date') != yday:
            return []
        if now.hour >= PREV_DAY_END_HOUR:
            if pd.get('posts'):   # 끝났으면 state를 줄인다
                with self._lock:
                    self._prev_day = {'date': yday, 'done': pd.get('done') or today, 'posts': []}
                print(f'[PrevDay] {PREV_DAY_END_HOUR}시 - 전날 보충 끝')
            return []
        if not pd.get('posts'):
            return []
        n_src, n_posts = self._tbs_today_size(ranked, today)
        if n_src >= PREV_DAY_DONE_SOURCES and n_posts >= PREV_DAY_DONE_POSTS:
            if pd.get('done') != today:   # done: 멈춤 기준을 처음 넘은 날 (로그를 한 번만 남기려고)
                with self._lock:
                    self._prev_day = {**pd, 'done': today}
                print(f'[PrevDay] TBS 당일 글 {n_posts}건/{n_src}곳 - 전날 보충 멈춤 '
                      f'({PREV_DAY_END_HOUR}시까지 실행마다 다시 판정)')
            return []
        counts = Counter(p['source'] for p in ranked)
        keys = {_url_key(p['url']) for p in ranked}
        extra = []
        for p in pd.get('posts') or []:
            s = p.get('source')
            if not s or counts[s] >= MAX_PER_SOURCE or not _is_http_url(p.get('url')):
                continue
            k = _url_key(p['url'])
            if k in keys:
                continue
            keys.add(k)
            counts[s] += 1
            q = dict(p)
            q['prev_day'] = True
            q['post_velocity'] = 0.0
            extra.append(q)
        for i, q in enumerate(extra, len(ranked) + 1):
            q['rank'] = i
        if extra:
            print(f'[PrevDay] 당일 {len(ranked)}건/{len({p["source"] for p in ranked})}곳(멈춤 판정용 TBS {n_posts}건/{n_src}곳) '
                  f'+ 전날 상위 글 {len(extra)}개({len({q["source"] for q in extra})}곳)로 빈 칸 보충')
        return extra

    # ── 커뮤니티별 상태 ───────────────────────────────────────────────────────

    def _update_source_health(self, plan: dict, collected: list, now: datetime) -> dict:
        """커뮤니티별 수집 상태 (trends.json source_health).
        {source: {status, last_update, last_new, n, since, via[, note]}}
        - status는 TBS 기준(ok/degraded/missing). TBS가 못 주는데 직접 수집도 차단이면 blocked(다른 경로로 못 채웠을 때)
        - last_update/last_new: TBS의 마지막 갱신·새 글 시각(없으면 이전 값 유지), n: 이번에 모은 글 수(모든 경로),
          since: 지금 status가 시작된 시각, via: 이번에 글을 준 경로"""
        with self._lock:
            prev = dict(self._source_health)
        stats = plan.get('stats') or {}
        n_by = Counter(p['source'] for p in collected)
        via_by = defaultdict(set)
        for p in collected:
            via_by[p['source']].add(p.get('via') or 'tbs')
        sids = set(EXPECTED_BY_HOUR) | set(stats) | set(n_by)
        health = {}
        for s in sorted(sids):
            st = stats.get(s) or {}
            old = prev.get(s) or {}
            if s in plan['degraded']:
                status = 'degraded'
            elif s in plan['missing']:
                status = 'missing'
            else:
                status = 'ok'
            note = ''
            d = self._direct_status.get(s)
            if status != 'ok' and d and d.get('status') != 'ok':
                note = f"직접 수집 {'차단' if d['status'] == 'blocked' else '실패'}({d.get('why') or '?'})"
                if d['status'] == 'blocked' and not (via_by[s] - {'tbs'}):
                    status = 'blocked'
            entry = {
                'status': status,
                'last_update': _fmt_dt(st['upd']) if st.get('upd') else old.get('last_update'),
                'last_new': _fmt_dt(st['new']) if st.get('new') else old.get('last_new'),
                'n': n_by.get(s, 0),
                'since': old.get('since') if old.get('status') == status and old.get('since') else _fmt_dt(now),
                'via': [v for v in VIA_ORDER if v in via_by[s]],
            }
            if note:
                entry['note'] = note
            health[s] = entry
        bad = {s: e['status'] for s, e in health.items() if e['status'] != 'ok'}
        if bad:
            print('[Health] ' + ', '.join(f'{s} {st}' for s, st in sorted(bad.items())))
        return health

    @staticmethod
    def _snapshot_sig(posts: list) -> str:
        """원본 목록 서명 — 글(_url_key)과 조회·추천·댓글 수가 모두 같으면 같은 값.
        실행마다 달라지는 hash() 대신 sha1을 써서 state.json에 남긴다."""
        rows = sorted(f"{_url_key(p['url'])}|{p.get('views') or 0}|{p.get('likes') or 0}|{p.get('comments') or 0}"
                      for p in posts)
        return hashlib.sha1('\n'.join(rows).encode('utf-8')).hexdigest()

    def _ai_summary_due(self, now: datetime) -> bool:
        with self._lock:
            last_sum, last_try = self._ai_summary_updated, self._ai_summary_attempted
            retry = self.AI_SUMMARY_RETRY_TRANSIENT if self._ai_summary_transient else self.AI_SUMMARY_RETRY
        return ((last_sum is None or (now - last_sum).total_seconds() > self.AI_SUMMARY_INTERVAL) and
                (last_try is None or (now - last_try).total_seconds() > retry))

    def _apply_ai_summary(self, new_summary, transient: bool, now: datetime) -> None:
        """_generate_ai_summary 결과 반영. self._lock 안에서 부른다. None이면 호출하지 않은 것."""
        if new_summary is None:
            return
        self._ai_summary_attempted = now
        self._ai_summary_transient = not new_summary and transient
        if new_summary:
            self._ai_summary = new_summary
            self._ai_summary_updated = now

    def _compute_post_velocity(self, posts: list, history: list) -> None:
        if not history:
            for p in posts:
                p['post_velocity'] = 0.0
            return

        url_avg: dict = {}
        for round_scores in history:
            for url, score in round_scores.items():
                entry = url_avg.setdefault(url, [0.0, 0])
                entry[0] += score
                entry[1] += 1
        avg_prev = {url: v[0] / v[1] for url, v in url_avg.items()}

        for p in posts:
            curr = p.get('rank_score', 0.0)
            prev = avg_prev.get(_url_key(p['url']))
            p['post_velocity'] = round(curr - (prev if prev is not None else 0.0), 1)

    def _generate_ai_summary(self, posts: list):
        """(결과, 일시적 실패 여부). 결과는 요약 텍스트, 호출했지만 실패하면 '',
        호출하지 않았으면(키 없음·모듈 오류·글 없음) None."""
        try:
            import gemini   # 사용 지점에서 import - 모듈이 없거나 깨져도 크롤은 계속
        except Exception as e:
            print(f'[Gemini] gemini 모듈 import 실패: {e}')
            return None, False
        if not gemini.available():
            return None, False

        # 커뮤니티마다 가장 순위가 높은 글 하나씩 (posts는 이미 순위순)
        seen_src: set = set()
        top_posts: list = []
        for p in posts:
            if p['source'] not in seen_src:
                top_posts.append(p)
                seen_src.add(p['source'])
            if len(top_posts) >= 20:
                break

        if not top_posts:
            return None, False

        # 조회수는 넣지 않는다 - 커뮤니티마다 규모가 다르고 FMK 조회수는 합성값이라 소스끼리 비교하면 왜곡된다
        lines = [f"- [{p.get('source_label') or p['source']}] {p['title']}" for p in top_posts]

        prompt = (
            "다음은 오늘 한국 주요 인터넷 커뮤니티에서 가장 화제가 된 글 제목들입니다. "
            "커뮤니티마다 자체 반응(조회·추천·댓글)이 가장 큰 글을 하나씩 골라 종합 순위가 높은 순서로 나열했습니다:\n\n"
            + "\n".join(lines)
            + "\n\n위 제목들을 바탕으로 오늘 온라인 커뮤니티의 분위기와 주요 이슈를 요약해주세요.\n"
            "작성 규칙:\n"
            "- 5~7문장으로 작성\n"
            "- 제목에 나와 있는 내용만 언급하고, 제목에 없는 세부 내용은 절대 추측하거나 지어내지 말 것\n"
            "- 목록 앞쪽에 있는 글일수록 더 비중 있게 다루기\n"
            "- 조회수 같은 숫자로 커뮤니티끼리 인기를 비교하지 말 것\n"
            "- 주제별로 자연스럽게 묶어서 흐름이 느껴지게\n"
            "- 커뮤니티 이름은 나열하지 말 것"
        )

        # gemini.generate 자체 timeout(60초) 위에 130초 hard timeout (DNS hang 방지).
        # generate는 503 재시도·예비 모델 시도까지 합쳐 약 2×timeout(120초) 안에 끝난다.
        ex = ThreadPoolExecutor(max_workers=1)
        try:
            text = ex.submit(gemini.generate, prompt, timeout=60).result(timeout=130)
            if text:
                print('[Gemini] 요약 생성 완료')
            return (text or '').strip(), gemini.last_error == 'transient'
        except Exception as e:
            print(f'[Gemini] 요약 생성 오류: {e}')
            return '', True   # hard timeout 등
        finally:
            ex.shutdown(wait=False)

    # ── 직접 스크래핑 ─────────────────────────────────────────────────────────

    def _scrape_source(self, src: dict) -> list:
        """한 소스의 페이지를 순서대로 받는다. 한 페이지라도 차단되면 나머지 페이지는 건너뛴다.
        결과(ok·blocked·error와 이유)는 self._direct_status[source]에 남긴다 (source_health의 blocked 판정).
        HTTP 200이어도 행을 하나도 못 읽으면 차단(챌린지·보안 검사·msg.html) 또는 선택자 고장으로 본다."""
        items = []
        status, why = 'ok', ''
        for i, url in enumerate(src['pages']):
            time.sleep(random.uniform(0.5, 1.5) if i == 0 else random.uniform(1.0, 2.0))
            html = self._get_page(src, url)
            if html is None:
                status, why = self._page_errors.get(url) or ('error', '응답 없음')
                break
            try:
                rows = self._parse_rows(src, url, html, len(items))
            except Exception as e:
                print(f'[{src["id"]}] {url} 파싱 오류: {e}')
                rows = []
            if not rows:
                reason = detect_block(html)
                status, why = 'blocked', reason or '0행(차단 또는 선택자 고장)'
                print(f'[{src["id"]}] {url} 행 0개 - {why}')
                break
            items.extend(rows)
        if items and status != 'ok':
            status, why = 'ok', f'일부 페이지만({why})'
        self._direct_status[src['id']] = {'status': status, 'why': why, 'n': len(items)}
        print(f'[{src["id"]}] 직접 스크래핑 {len(items)}개' + (f' ({status}: {why})' if status != 'ok' or why else ''))
        return items

    def _get_page(self, src: dict, url: str):
        """HTML bytes 또는 None. 4xx(403 Cloudflare, 429, 430 보안 페이지 등)는 차단으로 보고 재시도하지 않는다.
        연결 오류·타임아웃·5xx만 2초 뒤 1회 재시도. None이면 이유를 self._page_errors[url]에 남긴다."""
        u = urlparse(url)
        err = ''
        for attempt in range(2):
            try:
                r = requests.get(url, headers=_random_headers({'Referer': f'{u.scheme}://{u.netloc}/'}), timeout=12)
            except requests.RequestException as e:
                err = str(e)
            else:
                if r.status_code < 400:
                    if 'msg.html' in (r.url or ''):   # 리다이렉트로 안내 페이지에 떨어짐 (웃대)
                        self._page_errors[url] = ('blocked', 'msg.html 리다이렉트')
                        print(f'[{src["id"]}] {url} → {r.url} - 차단으로 보고 건너뜀')
                        return None
                    return r.content
                if r.status_code < 500:
                    self._page_errors[url] = ('blocked', f'HTTP {r.status_code}')
                    print(f'[{src["id"]}] {url} HTTP {r.status_code} - 차단/거부로 보고 건너뜀')
                    return None
                err = f'HTTP {r.status_code}'
            if attempt == 0:
                time.sleep(2)
        self._page_errors[url] = ('error', err[:80])
        print(f'[{src["id"]}] {url} 오류: {err}')
        return None

    def _parse_rows(self, src: dict, page_url: str, html: bytes, start_pos: int) -> list:
        label, emoji, color = SOURCE_META[src['id']]
        soup = BeautifulSoup(html, 'html.parser')

        page_date = ''
        if src.get('date_from_title') and soup.title:
            m = re.search(r'\d{4}\.\d{1,2}\.\d{1,2}', soup.title.get_text())
            page_date = self._parse_date(m.group(0)) if m else ''

        items = []
        for row in soup.select(src['row_sel']):
            a = row if src.get('row_is_link') else row.select_one(src['title_sel'])
            if not a or not a.get('href'):
                continue
            href = a['href'].strip()
            if src.get('strip_sel'):
                for junk in a.select(src['strip_sel']):
                    junk.decompose()
            text_el = a.select_one(src['text_sel']) if src.get('text_sel') else None
            title = self._clean_title((text_el or a).get_text(' '))
            full_url = urljoin(page_url, href)
            if (len(title) < 4 or COMMENT_LINK_RE.match(title) or '#comment' in href
                    or not _is_http_url(full_url) or self._is_notice(title, full_url)):
                continue

            view_el = row.select_one(src['view_sel']) if src.get('view_sel') else None
            like_el = row.select_one(src['like_sel']) if src.get('like_sel') else None
            date_el = row.select_one(src['date_sel']) if src.get('date_sel') else None
            sum_el = row.select_one(src['summary_sel']) if src.get('summary_sel') else None
            if date_el is not None:
                raw = date_el.get(src['date_attr']) if src.get('date_attr') else date_el.get_text(' ', strip=True)
                date_val = self._parse_date(raw or '')
            else:
                date_val = page_date
            summary = re.sub(r'\s+', ' ', sum_el.get_text(' ')).strip() if sum_el else ''

            n = start_pos + len(items)
            items.append({
                'source': src['id'],
                'source_label': label,
                'source_emoji': emoji,
                'source_color': color,
                'title': title,
                'summary': summary[:130] + ('…' if len(summary) > 130 else ''),
                'board': '',
                'url': full_url,
                'author': '',
                'date': date_val,
                **self._classify(title),
                'views': self._parse_count(view_el.get_text(' ', strip=True)) if view_el else 0,
                'likes': self._parse_count(like_el.get_text(' ', strip=True)) if like_el else 0,
                'comments': 0,
                'position_score': max(0.0, 100 - n * 1.5),
                'rank_score': 0,
                'rank': 0,
            })
        return items

    @staticmethod
    def _clean_title(text: str) -> str:
        title = re.sub(r'\s+', ' ', text).strip()
        return re.sub(r'\s*[\[(]\d+[\])]$', '', title).strip()   # 끝에 붙은 댓글 수 '[13]', '(5)'

    # ── 날짜 (계약: 'YYYY-MM-DDTHH:MM:SS+09:00' / 'YYYY-MM-DD' / '') ─────────

    def _parse_date(self, text: str) -> str:
        """사이트마다 다른 날짜 표기를 KST 기준 계약 형식으로 바꾼다. 사이트 표기 시각은 KST로 본다."""
        now = datetime.now(KST)
        text = (text or '').strip()
        text = re.sub(r'\s*[lL│|ㅣ]\s*조회.*$', '', text).strip()
        if not text:
            return ''
        try:
            # ISO 8601 (아카라이브 time[datetime] '2026-09-23T02:57:19.000Z' 등)
            if re.match(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}', text):
                dt = datetime.fromisoformat(text.replace('Z', '+00:00'))
                return _fmt_dt(dt if dt.tzinfo else dt.replace(tzinfo=KST))
            if text in ('방금', '방금 전') or re.match(r'^\d+\s*초\s*전', text):
                return _fmt_dt(now)
            m = re.match(r'^(\d+)\s*분\s*전', text)
            if m:
                return _fmt_dt(now - timedelta(minutes=int(m.group(1))))
            m = re.match(r'^(\d+)\s*시간\s*전', text)
            if m:
                return _fmt_dt(now - timedelta(hours=int(m.group(1))))
            m = re.match(r'^(\d+)\s*일\s*전', text)
            if m:
                return (now - timedelta(days=int(m.group(1)))).strftime('%Y-%m-%d')
            m = re.match(r'^어제(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?$', text)
            if m:
                y = now - timedelta(days=1)
                if not m.group(1):
                    return y.strftime('%Y-%m-%d')
                return _fmt_dt(y.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                                         second=int(m.group(3) or 0), microsecond=0))
            # 'HH:MM' / 'HH:MM:SS' = 오늘 글. 지금보다 10분 넘게 뒤면 어제 글 (KST 자정 직후 목록)
            m = re.match(r'^(\d{1,2}):(\d{2})(?::(\d{2}))?$', text)
            if m:
                dt = now.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                                 second=int(m.group(3) or 0), microsecond=0)
                if dt > now + timedelta(minutes=10):
                    dt -= timedelta(days=1)
                return _fmt_dt(dt)
            # 'MM.DD HH:MM' / 'MM/DD' 등 연도 없는 표기
            m = re.match(r'^(\d{1,2})[./-](\d{1,2})\.?(?:\s+(\d{1,2}):(\d{2}))?$', text)
            if m:
                d = self._infer_year(int(m.group(1)), int(m.group(2)), now)
                if not m.group(3):
                    return d.strftime('%Y-%m-%d')
                return _fmt_dt(d.replace(hour=int(m.group(3)), minute=int(m.group(4))))
            # 'YY/MM/DD HH:MM' (오유) / 'YY.MM.DD'
            m = re.match(r'^(\d{2})[./-](\d{2})[./-](\d{2})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?$', text)
            if m:
                return self._fmt_parts(2000 + int(m.group(1)), m.group(2), m.group(3), m.group(4), m.group(5), m.group(6))
            # 'YYYY-MM-DD[ HH:MM[:SS]]' / 'YYYY.MM.DD'
            m = re.match(r'^(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})\.?(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?', text)
            if m:
                return self._fmt_parts(int(m.group(1)), m.group(2), m.group(3), m.group(4), m.group(5), m.group(6))
        except (ValueError, OverflowError):
            pass
        return ''

    @staticmethod
    def _fmt_parts(year, month, day, hour=None, minute=None, second=None) -> str:
        dt = datetime(int(year), int(month), int(day), tzinfo=KST)   # 잘못된 날짜면 ValueError
        if hour is None:
            return dt.strftime('%Y-%m-%d')
        return _fmt_dt(dt.replace(hour=int(hour), minute=int(minute), second=int(second or 0)))

    @staticmethod
    def _infer_year(month: int, day: int, now: datetime) -> datetime:
        """연도 없는 'MM.DD'는 올해로 보되, 하루 넘게 미래면 작년 (1월 초의 '12.31')"""
        d = datetime(now.year, month, day, tzinfo=KST)
        if d > now + timedelta(days=1):
            d = d.replace(year=now.year - 1)
        return d

    @staticmethod
    def _to_datetime(date_str: str):
        if not date_str:
            return None
        try:
            if len(date_str) == 10:
                return datetime.strptime(date_str, '%Y-%m-%d').replace(tzinfo=KST)
            dt = datetime.fromisoformat(date_str)
            return dt if dt.tzinfo else dt.replace(tzinfo=KST)
        except ValueError:
            return None

    def _parse_count(self, text: str) -> int:
        text = (text or '').replace(',', '').strip().lower()
        m = re.search(r'(\d+(?:\.\d+)?)\s*(k|m|만)?', text)
        if not m:
            return 0
        try:
            return int(float(m.group(1)) * {'k': 1_000, 'm': 1_000_000, '만': 10_000}.get(m.group(2) or '', 1))
        except ValueError:
            return 0

    # ── 랭킹 ──────────────────────────────────────────────────────────────────

    MAX_AGE_DAYS = 90   # 이보다 오래된 글은 제외
    DECAY_TAU    = 90   # 나이 감쇠 e^(-t/τ) 시간 상수 (반감기 약 62일)

    def _age_days(self, date_str: str) -> float:
        dt = self._to_datetime(date_str)
        if dt is None:
            return 0.0
        return max(0.0, (datetime.now(KST) - dt).total_seconds() / 86400)

    def _assign_ranks(self, posts: list) -> list:
        posts = [p for p in posts if self._age_days(p.get('date', '')) <= self.MAX_AGE_DAYS]

        src_max = {}
        src_has_views = {}
        for p in posts:
            src = p['source']
            if src not in src_max:
                src_max[src] = {'views': 1, 'likes': 1, 'comments': 1}
                src_has_views[src] = False
            if p['views'] > 0:
                src_has_views[src] = True
                src_max[src]['views']    = max(src_max[src]['views'],    p['views'])
            if p.get('likes', 0) > 0:
                src_max[src]['likes']    = max(src_max[src]['likes'],    p['likes'])
            if p.get('comments', 0) > 0:
                src_max[src]['comments'] = max(src_max[src]['comments'], p['comments'])

        for p in posts:
            src = p['source']
            mx  = src_max[src]

            def norm(val, mx_val):
                return (math.log1p(val) / math.log1p(mx_val)) * 100 if val > 0 else 0

            view_score    = norm(p['views'],              mx['views'])
            like_score    = norm(p.get('likes', 0),      mx['likes'])
            comment_score = norm(p.get('comments', 0),   mx['comments'])

            position = p.get('position_score', 50.0)   # 외부·복원 글에 없으면 중간값
            if src_has_views.get(src):
                engagement  = view_score * 0.60 + like_score * 0.25 + comment_score * 0.15
                base_score  = position * 0.35 + engagement * 0.65
            else:
                base_score  = position

            decay = math.exp(-self._age_days(p.get('date', '')) / self.DECAY_TAU)
            p['rank_score'] = round(base_score * decay, 1)

        sorted_posts = sorted(posts, key=lambda x: x['rank_score'], reverse=True)

        seen_src: set = set()
        for p in sorted_posts:
            if p['source'] not in seen_src:
                p['rank_score'] = round(p['rank_score'] * 1.10, 1)
                seen_src.add(p['source'])
        sorted_posts = sorted(sorted_posts, key=lambda x: x['rank_score'], reverse=True)

        DAMPEN = 0.65
        src_counts: dict = {}
        diversity: dict = {}
        for p in sorted_posts:
            n = src_counts.get(p['source'], 0)
            diversity[id(p)] = p['rank_score'] * (DAMPEN ** n)
            src_counts[p['source']] = n + 1

        src_included: dict = {}
        capped: list = []
        for p in sorted(sorted_posts, key=lambda x: diversity[id(x)], reverse=True):
            cnt = src_included.get(p['source'], 0)
            if cnt < MAX_PER_SOURCE:
                capped.append(p)
                src_included[p['source']] = cnt + 1

        for i, p in enumerate(capped):
            p['rank'] = i + 1
        return capped

    # ── 이슈 · 카테고리 ───────────────────────────────────────────────────────

    @staticmethod
    def _build_issues(posts: list, score_history: list, now: datetime) -> dict:
        """'지금 뜨는 이슈' 블록 (issues.py). 계산이 실패해도 크롤·빌드는 계속되고 빈 블록을 둔다."""
        try:
            import issues   # 사용 지점에서 import - 모듈이 깨져도 크롤은 계속 (issues가 이 모듈의 _url_key를 쓴다)
            return issues.build_issues(posts, score_history, now)
        except Exception as e:
            print(f'[Issues] 이슈 계산 오류 - 빈 블록으로: {type(e).__name__}: {e}')
            return _empty_issues(posts, now)

    def _set_issues(self, block: dict) -> None:
        """이슈 블록 반영 + 튜닝 로그. self._lock 안에서 부른다.
        결과(시각 제외)가 직전 기록과 같으면 로그에 넣지 않는다 — 같은 목록이 이어지는 실행이 대부분이다."""
        self._issues = block
        entry = {'t': block['as_of'], 'posts': block['posts'], 'communities': block['communities'],
                 'items': [{k: it[k] for k in ('id', 'name', 'kind', 'status', 'n', 'c')} for it in block['items']]}
        last = self._issue_log[-1] if self._issue_log else {}
        if {k: v for k, v in last.items() if k != 't'} == {k: v for k, v in entry.items() if k != 't'}:
            return
        self._issue_log.append(entry)
        del self._issue_log[:-ISSUE_LOG_SIZE]
        sizes = [len(json.dumps(e, ensure_ascii=False, separators=(',', ':')).encode()) for e in self._issue_log]
        while len(self._issue_log) > 1 and sum(sizes) + len(sizes) + 1 > ISSUE_LOG_MAX_BYTES:   # 괄호·쉼표 포함
            self._issue_log.pop(0)
            sizes.pop(0)

    @staticmethod
    def _category_counts(posts: list) -> dict:
        return {
            '음식/카페':  sum(1 for p in posts if p.get('is_food')),
            '뷰티/패션':  sum(1 for p in posts if p.get('is_beauty') or p.get('is_fashion')),
            '여행':       sum(1 for p in posts if p.get('is_travel')),
            '게임/IT':    sum(1 for p in posts if p.get('is_game')),
            '연예/아이돌': sum(1 for p in posts if p.get('is_celeb')),
            '유머':       sum(1 for p in posts if p.get('is_humor')),
            '자동차':     sum(1 for p in posts if p.get('is_car')),
        }

    def _matches(self, text: str, word_set: set) -> bool:
        return any(w in text for w in word_set)

    def _classify(self, title: str) -> dict:
        """제목으로 대표 카테고리(keyword)와 is_* 플래그를 판정한다 (게시판 이름은 넣지 않는다)."""
        text = CATEGORY_EXCLUDE_RE.sub(' ', title)
        result = {'keyword': '일반'}
        for flag, label, words in CATEGORIES:
            result[flag] = self._matches(text, words)
            if result[flag] and result['keyword'] == '일반':
                result['keyword'] = label
        return result

    def _is_notice(self, title: str, url: str = '') -> bool:
        """직접 스크래핑 전용. 말머리가 [공지]·[필독]·(광고) 등이거나 이벤트/공지 URL이면 공지로 본다."""
        return bool(NOTICE_TITLE_RE.match(title) or AD_TITLE_RE.match(title) or (url and NOTICE_URL_RE.search(url)))

    def _fetch_summary(self, post: dict) -> None:
        sel = SUMMARY_SELECTORS.get(post['source'])
        if not sel:
            return
        try:
            headers = {**HEADERS, 'Referer': post['url']}
            r = requests.get(post['url'], headers=headers, timeout=6)
            r.raise_for_status()
            soup = BeautifulSoup(r.content, 'html.parser')
            for el in soup.select(sel):
                text = el.get_text(' ', strip=True)
                text = re.sub(r'\s+', ' ', ZERO_WIDTH_RE.sub('', text)).strip()
                # MLB파크 #contentDetail에는 추천·공유 버튼 글자가 같이 들어 있다
                text = SUMMARY_TAIL_RE.sub('', text).strip()
                if len(text) >= 15 and not any(w in text for w in ['광고', '제휴', 'AD']):
                    post['summary'] = text[:130] + ('…' if len(text) > 130 else '')
                    return
        except Exception:
            pass
