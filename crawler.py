"""
Korean internet community trend crawler.
주 경로: TodayBestStory API(21개 커뮤니티 베스트 전량).
API 결과가 부족하면 일부 커뮤니티 베스트 목록을 직접 스크래핑한다(fallback).
랭킹한 글을 issues.py가 '지금 뜨는 이슈'(여러 커뮤니티에 퍼진 이야기)로 묶는다.
"""

import hashlib
import json
import math
import re
import time
import random
import threading
import requests
from bs4 import BeautifulSoup
from collections import Counter
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


# ── TodayBestStory API ─────────────────────────────────────────────────────────

TBS_API_URL = 'https://todaybeststory.com/api/v2/communities/posts/range'
TBS_PAGE_LIMIT = 100   # API 최대값 (그 이상은 400)
TBS_MAX_PAGES = 30     # 안전 상한 (하루 1300~2000건 → 13~20페이지)
# 초. 넘으면 페이지 넘기기를 멈추고 받은 만큼 쓴다. 마감 직전 페이지가 최악(15초×2회+대기 2초)으로 걸려도
# 호출부 hard timeout 90초 안에 끝나야 받은 페이지를 잃지 않는다 (평소 16페이지 약 8초)
TBS_DEADLINE = 55
TBS_MIN_POSTS = 50     # TBS 결과가 이보다 적거나
TBS_MIN_SOURCES = 8    # 커뮤니티 수가 이보다 적으면 직접 스크래핑을 보탠다

# communityId → (source id, 라벨, 이모지, 색)
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
    'ILB': ('ilbe', '일베', '⚡', '#636e72'),
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


# ── 직접 스크래핑 (TBS 결과가 부족할 때만) ────────────────────────────────────
# 행(row_sel) 단위로 파싱해서 제목·조회수·날짜가 항상 같은 글에서 나오게 한다.
# 인스티즈(Cloudflare 403)·FMKorea(430 보안 페이지)는 requests로 못 받아서 TBS에만 맡긴다.
#   title_sel  행 안의 글 링크(href 사용)      text_sel   링크 안의 제목 요소(없으면 링크 텍스트)
#   strip_sel  제목에서 지울 요소(댓글 수 등)   date_attr  날짜를 텍스트 대신 속성에서 읽기
#   date_from_title  날짜 칸이 없어 페이지 <title>의 'YYYY.MM.DD'를 쓴다
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
            'https://pann.nate.com/talk/ranking/d',
        ],
        'row_sel': 'div.cntList ul li',
        'title_sel': 'dt h2 a[href^="/talk/"]',
        'view_sel': 'dd.info span.count',
        'like_sel': 'dd.info span.rcm',
        'summary_sel': 'dd.txt',
        'date_from_title': True,   # '명예의 전당 일간 랭킹 : 2026.09.22'
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
]

# 제목 맨 앞 말머리([공지]·[🚨필독🚨]·(광고) 등)만 공지로 본다. '…긴급공지'처럼 제목 속 단어는 제외
NOTICE_TITLE_RE = re.compile(r'^\s*[\[【(<]\W{0,3}(공지|필독|이벤트|광고|홍보|AD)')
NOTICE_URL_RE = re.compile(r'/event/|/annonce/|/rule/|[?&]b=notice|event_notice')
COMMENT_LINK_RE = re.compile(r'^[\[(]?\d+[\])]?$')   # 댓글 수 링크('[19]', '(5)')

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

HISTORY_SIZE = 6   # post_score_history 라운드 수 (이슈 신규·급상승·+N 판정, post_velocity)
# 튜닝 로그: 결과가 바뀐 빌드의 이슈 요약을 state에 남겨 며칠간 품질을 본다.
# 한 번에 8개면 약 0.9KB라 개수(72)보다 크기 상한이 먼저 걸린다
ISSUE_LOG_SIZE = 72
ISSUE_LOG_MAX_BYTES = 30_000
LOW_DATA_POSTS = 350   # 원본 글이 이보다 적으면 이슈 보드에 '글이 적은 시간' 안내 (issues.py도 이 값을 쓴다)
STATE_VERSION = 1
STATE_DROP_FIELDS = ('board', 'keyword', 'position_score')   # 복원 후 쓰지 않는 필드 - state 크기를 줄인다
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
    계산 실패의 대비책이므로 글 필드를 직접 읽지 않는다 (source 없는 글 때문에 실패했을 수도 있다)"""
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
        self._collect_complete = False      # 이번 수집이 끝까지 받은 TBS 목록만으로 충분했는지

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
                'ai_summary': self._ai_summary,
                'ai_summary_updated': self._ai_summary_updated.isoformat() if self._ai_summary_updated else None,
            }

    # ── 상태 저장/복원 (GitHub Actions 실행 사이에 이어받기) ──────────────────

    def export_state(self) -> dict:
        """다음 실행에서 이어받을 상태. JSON으로 바로 직렬화할 수 있는 값만 담는다."""
        with self._lock:
            return {
                'version': STATE_VERSION,
                'post_score_history': [dict(h) for h in self._post_score_history],   # _url_key(url) → rank_score
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
                # 크롤 없이 다시 렌더링할 때(build.py --no-crawl) 직전 상태(ok/stale)를 그대로 보여 주려고
                'status': self._status if self._status in ('ok', 'stale') else None,
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
        log = state.get('issue_log')
        issue_log = [e for e in log if isinstance(e, dict)][-ISSUE_LOG_SIZE:] if isinstance(log, list) else []
        ai_summary = state.get('ai_summary') if isinstance(state.get('ai_summary'), str) else ''
        ai_updated = self._parse_iso(state.get('ai_summary_updated'))
        ai_attempted = self._parse_iso(state.get('ai_summary_attempted'))
        ai_transient = state.get('ai_summary_transient') is True
        snapshot = state.get('snapshot') if isinstance(state.get('snapshot'), str) else None
        last_updated = state.get('last_updated') if isinstance(state.get('last_updated'), str) else None
        # status가 없는 옛 state는 글이 있으면 ok로 본다 (idle은 '아직 수집 전'이라는 뜻)
        status = state.get('status') if state.get('status') in ('ok', 'stale') else ('ok' if posts else 'idle')
        # 이슈 블록은 state에 없으므로 같은 입력(posts, 점수 이력)과 그때 시각(last_updated)으로 다시 계산한다
        issues = self._build_issues(posts, score_history, self._parse_iso(last_updated) or datetime.now(KST))
        with self._lock:
            self._post_score_history = score_history
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
        print(f'[State] 복원: posts {len(posts)}, 점수 이력 {len(score_history)}, 이슈 {len(issues["items"])}, '
              f'이슈 로그 {len(issue_log)}, crawl_count {crawl_count}, status {status}')

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

    def _fetch_todaybeststory(self) -> list:
        now = datetime.now(KST)
        end = now.strftime('%Y-%m-%d')   # TBS targetDate는 KST 기준
        # KST 자정 직후에는 오늘 목록이 아직 작으므로 02시 전까지는 어제 목록도 같이 받는다
        start = (now - timedelta(days=1)).strftime('%Y-%m-%d') if now.hour < 2 else end
        raw_posts = []
        pages_ok = 0
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
                        params={'startDate': start, 'endDate': end, 'page': page, 'limit': TBS_PAGE_LIMIT},
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
            if not data.get('hasNext'):
                self._tbs_truncated = False
                break
            time.sleep(random.uniform(0.2, 0.4))
        else:
            print(f'[TodayBestStory] 안전 상한 {TBS_MAX_PAGES}페이지 도달 - 나머지 생략')
        if not raw_posts:
            return []

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
            })
        print(f'[TodayBestStory] {start}~{end} {pages_ok}페이지, {len(raw_posts)}개 원본 → {len(items)}개 파싱 완료 '
              f'({len(src_pos)}개 커뮤니티)')
        return items

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

    def _collect_all_posts(self, sink: list) -> None:
        """포스트 수집 전체 단계. 결과를 sink에 바로 넣어서 master timeout이 나도 모은 만큼은 살린다."""
        # TodayBestStory API - 90초 inner hard timeout (DNS/TCP hang 방지)
        api_posts = []
        _tbs_ex = ThreadPoolExecutor(max_workers=1)
        try:
            api_posts = _tbs_ex.submit(self._fetch_todaybeststory).result(timeout=90)
        except Exception as e:
            print(f'[Refresh] TodayBestStory inner timeout/오류: {e}')
        finally:
            _tbs_ex.shutdown(wait=False)
        sink.extend(api_posts)

        # 끝까지 받았고 글 수와 커뮤니티 수가 둘 다 충분해야 TBS만 쓴다
        # (몇 페이지만 받고 끊기면 조회수 상위 커뮤니티만 남아 한쪽으로 쏠린다)
        n_src = len({p['source'] for p in api_posts})
        truncated = bool(api_posts) and getattr(self, '_tbs_truncated', False)
        if len(api_posts) >= TBS_MIN_POSTS and n_src >= TBS_MIN_SOURCES and not truncated:
            print(f'[Refresh] TodayBestStory {len(api_posts)}개/{n_src}개 커뮤니티 - 직접 스크래핑 생략')
            self._collect_complete = True
            return

        why = '중간에 끊김' if truncated else '부족'
        print(f'[Refresh] TodayBestStory {len(api_posts)}개/{n_src}개 커뮤니티로 {why} - 직접 스크래핑 fallback')
        # 소스끼리는 병렬, 한 소스 안의 페이지는 순차(간격을 둔다)
        with ThreadPoolExecutor(max_workers=5) as ex:
            for items in ex.map(self._scrape_source, COMMUNITY_SOURCES):
                sink.extend(items)

    def refresh(self):
        # 이미 크롤 중이면 즉시 반환 (동시 실행 시 요청 폭증·history 압축 방지)
        if not self._refresh_lock.acquire(blocking=False):
            print('[Refresh] 이미 크롤 중 - 건너뜀')
            return
        try:
            with self._lock:
                self._status = 'crawling'
            try:
                self._refresh_body()
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

    def _refresh_body(self):

        # 전체 포스트 수집 - 180초 master hard timeout (DNS/TCP hang 완전 차단)
        sink = []
        self._collect_complete = False
        _collect_ex = ThreadPoolExecutor(max_workers=1)
        try:
            _collect_ex.submit(self._collect_all_posts, sink).result(timeout=180)
        except Exception as e:
            print(f'[Refresh] 포스트 수집 master timeout/오류: {e} - 모은 {len(sink)}개로 진행')
        finally:
            _collect_ex.shutdown(wait=False)
        posts = list(sink)
        complete = self._collect_complete

        # 중복 제거 (같은 글의 URL 변형 포함) + http(s) 링크만
        seen, unique = set(), []
        for p in posts:
            if not _is_http_url(p.get('url')):
                continue
            key = _url_key(p['url'])
            if key not in seen:
                seen.add(key)
                unique.append(p)

        snapshot = self._snapshot_sig(unique)
        unique = self._assign_ranks(unique)

        with self._lock:
            kept = len(self._posts)
            kept_src = len({p['source'] for p in self._posts})
            same = bool(self._posts) and snapshot == self._snapshot
        n_src = len({p['source'] for p in unique})
        # 0건이거나, 이전보다 적은 몇 건만 건졌거나, TBS가 온전하지 않아 커뮤니티 수가 이전의 절반도 안 되면
        # (TBS 장애 + fallback 2곳만 성공 등) 수집 실패로 본다
        if (not unique or (len(unique) < MIN_OK_POSTS and kept > len(unique))
                or (not complete and n_src * 2 < kept_src)):
            # 이전 posts·trends 유지, history에도 넣지 않는다 (다음 라운드 가짜 급상승 방지)
            with self._lock:
                self._status = 'stale'
            print(f'[Refresh] 수집 {len(unique)}건/{n_src}개 커뮤니티 - 이전 데이터 {kept}개/{kept_src}개 유지 (status stale)')
            return

        now = datetime.now(KST).replace(microsecond=0)

        # TBS는 약 1시간마다 갱신된다. 직전과 같은 목록을 다시 받았으면 이력·갱신 시각·crawl_count를 그대로 둔다
        # (같은 목록을 매번 쌓으면 급상승이 3라운드 뒤 사라지고 'n분 전 갱신'이 실제보다 새것처럼 보인다)
        if same:
            new_summary, transient = (self._generate_ai_summary(unique) if self._ai_summary_due(now)
                                      else (None, False))
            # 글·이력은 그대로 두고 이슈만 지금 시각으로 다시 계산한다 (신규·잠잠 판정이 시간에 따라 바뀐다)
            with self._lock:
                posts, score_history = list(self._posts), list(self._post_score_history)
            issues = self._build_issues(posts, score_history, now)
            with self._lock:
                self._status = 'ok'
                self._set_issues(issues)
                self._apply_ai_summary(new_summary, transient, now)
            print(f'[Refresh] 원본 목록이 직전과 같음 ({len(unique)}건) - 이력·갱신 시각 유지')
            return

        with self._lock:
            prev_post_history = list(self._post_score_history)
            prev_summary = {_url_key(p['url']): p['summary'] for p in self._posts if p.get('summary')}
        self._compute_post_velocity(unique, prev_post_history)
        # 키는 _url_key: 전체 URL보다 짧고(state 크기), ?page= 같은 변형이 바뀌어도 같은 글로 이어진다
        curr_scores = {_url_key(p['url']): p.get('rank_score', 0.0) for p in unique}
        score_history = (prev_post_history + [curr_scores])[-HISTORY_SIZE:]

        # 직전 라운드에서 받아 둔 본문 요약은 재사용 (같은 글을 매번 다시 요청하지 않게)
        for p in unique:
            if not p['summary']:
                p['summary'] = prev_summary.get(_url_key(p['url']), '')

        # 상위 포스트 본문 요약 병렬 수집 - 25초 hard timeout
        to_summarize = [p for p in unique[:SUMMARY_MAX_POSTS]
                        if p['source'] in SUMMARY_SELECTORS and not p['summary']]
        if to_summarize:
            ex = ThreadPoolExecutor(max_workers=4)
            try:
                list(ex.map(self._fetch_summary, to_summarize, timeout=25))
            except Exception:
                pass
            finally:
                ex.shutdown(wait=False)

        issues = self._build_issues(unique, score_history, now)

        # AI 요약 갱신 (성공 후 6시간, 실패 후 2시간·일시적 실패 후 30분 간격)
        new_summary, transient = (self._generate_ai_summary(unique) if self._ai_summary_due(now)
                                  else (None, False))

        with self._lock:
            self._posts = unique
            self._post_score_history = score_history
            self._set_issues(issues)
            # 데이터가 바뀐 시각 (같은 목록을 다시 받은 실행에서는 바꾸지 않는다)
            self._last_updated = now.isoformat(timespec='seconds')
            self._crawl_count += 1
            self._snapshot = snapshot
            self._status = 'ok'
            self._apply_ai_summary(new_summary, transient, now)

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
        """한 소스의 페이지를 순서대로 받는다. 한 페이지라도 차단되면 나머지 페이지는 건너뛴다."""
        items = []
        for i, url in enumerate(src['pages']):
            time.sleep(random.uniform(0.5, 1.5) if i == 0 else random.uniform(1.0, 2.0))
            html = self._get_page(src, url)
            if html is None:
                break
            try:
                items.extend(self._parse_rows(src, url, html, len(items)))
            except Exception as e:
                print(f'[{src["id"]}] {url} 파싱 오류: {e}')
        print(f'[{src["id"]}] 직접 스크래핑 {len(items)}개')
        return items

    def _get_page(self, src: dict, url: str):
        """HTML bytes 또는 None. 4xx(403 Cloudflare, 429, 430 보안 페이지 등)는 차단으로 보고 재시도하지 않는다.
        연결 오류·타임아웃·5xx만 2초 뒤 1회 재시도."""
        u = urlparse(url)
        err = ''
        for attempt in range(2):
            try:
                r = requests.get(url, headers=_random_headers({'Referer': f'{u.scheme}://{u.netloc}/'}), timeout=12)
            except requests.RequestException as e:
                err = str(e)
            else:
                if r.status_code < 400:
                    return r.content
                if r.status_code < 500:
                    print(f'[{src["id"]}] {url} HTTP {r.status_code} - 차단/거부로 보고 건너뜀')
                    return None
                err = f'HTTP {r.status_code}'
            if attempt == 0:
                time.sleep(2)
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
            a = row.select_one(src['title_sel'])
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

            if src_has_views.get(src):
                engagement  = view_score * 0.60 + like_score * 0.25 + comment_score * 0.15
                base_score  = p['position_score'] * 0.35 + engagement * 0.65
            else:
                base_score  = p['position_score']

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
        MAX_PER_SOURCE = 25
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
        return bool(NOTICE_TITLE_RE.match(title) or (url and NOTICE_URL_RE.search(url)))

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
