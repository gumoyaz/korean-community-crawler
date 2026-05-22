"""
Korean internet community trend crawler.
Sources: FMKorea, 오늘의유머, 루리웹, 클리앙 + Instagram fallback.
Trend scoring: keyword frequency + velocity (rate of change between rounds).
"""

import os
import requests
from bs4 import BeautifulSoup
import re
import json
import time
import random
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15',
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

COMMUNITY_SOURCES = [
    {
        'id': 'fmkorea',
        'label': 'FMKorea 베스트',
        'pages': [
            'https://www.fmkorea.com/index.php?mid=best&listStyle=list&page=1',
            'https://www.fmkorea.com/index.php?mid=best&listStyle=list&page=2',
        ],
        'title_sel': 'td.title a',
        'view_sel': None,
        'base_url': 'https://www.fmkorea.com',
        'color': '#ff6b35',
        'emoji': '🔥',
    },
    {
        'id': 'todayhumor',
        'label': '오늘의유머 베스트',
        'pages': [
            'https://www.todayhumor.co.kr/board/list.php?table=bestofbest&page=1',
            'https://www.todayhumor.co.kr/board/list.php?table=bestofbest&page=2',
        ],
        'title_sel': '.subject a',
        'view_sel': '.hits',
        'date_sel': 'td.date',
        'base_url': 'https://www.todayhumor.co.kr',
        'color': '#2ecc71',
        'emoji': '😂',
    },
    {
        'id': 'ruliweb',
        'label': '루리웹 베스트',
        'pages': [
            'https://bbs.ruliweb.com/community/board/300143',
            'https://bbs.ruliweb.com/community/board/300143?page=2',
        ],
        'title_sel': '.subject a',
        'view_sel': 'td.hit',
        'date_sel': 'td.time',
        'base_url': 'https://bbs.ruliweb.com',
        'color': '#3498db',
        'emoji': '🎮',
    },
    {
        'id': 'clien',
        'label': '클리앙 인기',
        'pages': [
            'https://www.clien.net/service/board/park?od=T31&po=0',
            'https://www.clien.net/service/board/park?od=T31&po=1',
        ],
        'title_sel': '.list_subject',
        'view_sel': '.hit',
        'date_sel': '.list_time span.timestamp',
        'base_url': 'https://www.clien.net',
        'color': '#9b59b6',
        'emoji': '💻',
    },
    {
        'id': 'theqoo',
        'label': '더쿠 핫',
        'pages': [
            'https://theqoo.net/hot',
            'https://theqoo.net/hot?page=2',
        ],
        'title_sel': '.title a:not(.replyNum)',
        'view_sel': None,
        'date_sel': '.time',
        'base_url': 'https://theqoo.net',
        'color': '#e91e8c',
        'emoji': '💗',
    },
    {
        'id': 'mlbpark',
        'label': 'MLB파크',
        'pages': [
            'https://mlbpark.donga.com/mp/b.php?b=bullpen&m=search&k=&s=&sport=&select=1&query=',
        ],
        'title_sel': '.tit a',
        'view_sel': None,
        'date_sel': None,
        'base_url': 'https://mlbpark.donga.com',
        'color': '#1565c0',
        'emoji': '⚾',
    },
    {
        'id': 'instiz',
        'label': '인스티즈',
        'pages': [
            'https://www.instiz.net/pt',
            'https://www.instiz.net/pt?page=2',
        ],
        'title_sel': '.listsubject a',
        'title_inner_sel': '.sbj',
        'view_sel': None,
        'date_sel': None,
        'date_sibling': 'listno',
        'date_inner': 'div.listno.regdate',
        'base_url': 'https://www.instiz.net',
        'color': '#ff7675',
        'emoji': '💬',
    },
    {
        'id': 'bobaedream',
        'label': '보배드림',
        'pages': [
            'https://www.bobaedream.co.kr/list?code=freeb',
            'https://www.bobaedream.co.kr/list?code=freeb&page=2',
        ],
        'title_sel': 'a.bsubject',
        'view_sel': None,
        'date_sel': 'td.date',
        'base_url': 'https://www.bobaedream.co.kr',
        'color': '#fdcb6e',
        'emoji': '🚗',
    },
    {
        'id': 'natekorea',
        'label': '네이트판',
        'pages': [
            'https://pann.nate.com/talk/ranking/d',
        ],
        'title_sel': 'dl dt h2 a[href^="/talk/"]',
        'view_sel': 'dd.info span.count',
        'date_sel': None,
        'default_date': 'today',
        'base_url': 'https://pann.nate.com',
        'color': '#e17055',
        'emoji': '💁',
    },
    {
        'id': 'humoruniv',
        'label': '웃긴대학',
        'pages': [
            'https://web.humoruniv.com/board/humor/list.html?table=pds',
            'https://web.humoruniv.com/board/humor/list.html?table=pds&page=2',
        ],
        'title_sel': 'td.li_sbj a[href*="read.html"]',
        'view_sel': None,
        'date_sel': 'td.li_date',
        'base_url': 'https://web.humoruniv.com/board/humor',
        'color': '#f9ca24',
        'emoji': '🤣',
    },
    {
        'id': 'arcalive',
        'label': '아카라이브',
        'pages': [
            'https://arca.live/b/live?sort=recommend',
            'https://arca.live/b/live?sort=recommend&p=2',
        ],
        'title_sel': 'a.vrow.column:not(.notice)',
        'title_text_sel': '.col-title .title',
        'view_sel': '.col-view',
        'date_sel': 'time[datetime]',
        'date_attr': 'datetime',
        'base_url': 'https://arca.live',
        'color': '#00b4d8',
        'emoji': '🌊',
    },
    {
        'id': 'ddanzi',
        'label': '딴지일보',
        'pages': [
            'https://www.ddanzi.com/free',
            'https://www.ddanzi.com/free?page=2',
        ],
        'title_sel': 'table tr td a[href*="/free/"]',
        'view_sel': 'table tr td:last-child',
        'date_sel': None,
        'default_date': 'today',
        'base_url': 'https://www.ddanzi.com',
        'color': '#6c5ce7',
        'emoji': '📰',
    },
]

INSTAGRAM_HASHTAGS = [
    '맛스타그램', '카페스타그램', '오오티디', '뷰티', '여행스타그램', '일상', '먹스타그램'
]

FOOD_WORDS = {
    '맛집', '카페', '음식', '요리', '디저트', '한식', '브런치', '레스토랑', '맛있',
    '맛스타그램', '먹스타그램', '밥집', '식당', '점심', '저녁', '베이커리', '케이크',
    '커피', '라멘', '파스타', '초밥', '치킨', '피자', '샐러드', '맥주', '술집', '고기'
}
BEAUTY_WORDS = {
    '뷰티', '화장품', '피부', '메이크업', '스킨케어', '립', '아이섀도', '마스크팩',
    '헤어', '네일', '향수', '세럼', '선크림', '파운데이션', '쿠션', '틴트', '다이어트', '운동'
}
FASHION_WORDS = {
    '패션', '코디', '스타일', '아우터', '원피스', '청바지', '신발', '가방', '악세사리',
    '데일리룩', '오오티디', '하울', '쇼핑', '브랜드', '명품', '옷'
}
TRAVEL_WORDS = {
    '여행', '관광', '호텔', '항공', '비행기', '유럽', '일본', '동남아', '제주', '부산',
    '강릉', '속초', '해외여행', '국내여행', '캠핑', '드라이브'
}
GAME_WORDS = {
    '게임', '롤', '리그오브레전드', '배그', '배틀그라운드', '오버워치', '마인크래프트',
    '스팀', '닌텐도', '플스', 'PS5', '엑박', '피파', '디아블로', '로스트아크',
    '메이플', '던파', '와우', '포트나이트', '엘든링', '사이버펑크', '발로란트',
    '애플', '삼성', '아이폰', '갤럭시', '인텔', '엔비디아', 'CPU', 'GPU',
    '노트북', '태블릿', '스마트폰', '컴퓨터', '프로그래밍', 'AI', '인공지능',
    '유튜브', '넷플릭스', '앱', '소프트웨어', '하드웨어', '리뷰',
}
CELEB_WORDS = {
    '아이돌', '연예인', '가수', '배우', '드라마', '영화', '콘서트', '팬미팅',
    '앨범', '컴백', 'BTS', '블랙핑크', '뉴진스', '아이브', '에스파', '방탄',
    '케이팝', '엔터', '연예계', '스타', '팬', '직캠', '뮤직비디오', '티저',
    '오디션', '데뷔', '소속사', '음원', '멜론', '스트리밍', '시상식',
}
HUMOR_WORDS = {
    '개그', '코미디', '유머', '웃음', '병맛', '드립', '개드립', '짤', '밈',
    '움짤', '레전드짤', '웃대', '에펨코리아', '기묘한', '황당', '충격',
}
CAR_WORDS = {
    '자동차', '차량', '주행', '연비', '엔진', '수입차', '국산차',
    '현대차', '기아차', '제네시스', '테슬라', '아우디', '벤츠', 'BMW',
    '볼보', '포르쉐', '전기차', '하이브리드', '중고차', '신차', '튜닝',
    '보배드림', '자동차사고', '교통사고',
}

STOP_WORDS = {
    '이', '그', '저', '것', '수', '이것', '그것', '저것', '이게', '그게', '저게',
    '이거', '그거', '저거', '여기', '거기', '저기', '이쪽', '그쪽', '저쪽',
    '우리', '저희', '나', '너', '자기', '본인', '누구', '아무', '모두',
    '같은', '다른', '새로운', '좋은', '나쁜', '많은', '적은', '큰', '작은',
    '높은', '낮은', '빠른', '느린', '넓은', '좁은', '오래된', '다양한', '이런',
    '저런', '그런', '어떤', '무슨', '어느', '모든', '각각', '일부', '전체',
    '정말', '진짜', '너무', '매우', '완전', '엄청', '굉장', '되게', '엄청나',
    '조금', '약간', '살짝', '아주', '더', '덜', '가장', '제일', '그냥',
    '요즘', '이제', '이미', '아직', '계속', '다시', '또', '또한', '먼저',
    '나중', '항상', '자주', '가끔', '별로', '거의', '특히', '보통', '주로',
    '바로', '갑자기', '드디어', '역시', '원래', '사실', '당연', '물론',
    '오히려', '한편', '분명', '확실', '아마', '혹시', '결국', '여전히',
    '마침내', '겨우', '벌써', '이미', '먼저', '다시', '혼자', '함께',
    '그리고', '하지만', '근데', '그래서', '그러나', '그래도', '그러면',
    '따라서', '게다가', '다만', '단지', '즉', '또는', '혹은', '반면',
    '그러므로', '왜냐면', '이처럼', '이렇게', '저렇게', '어쨌든',
    '있는', '없는', '하는', '되는', '되어', '이다', '한다', '됐다',
    '있네', '없네', '좋네', '했네', '왔네', '봤네', '같네', '됐네',
    '하게', '되게', '이게', '없이', '있어', '없어', '좋아', '싫어',
    '않는', '않고', '않아', '못하', '못해',
    '나온', '보인', '받은', '된다', '한다', '간다', '온다', '본다',
    '알고', '알아', '알지', '알면', '알던', '몰랐', '몰라', '모름',
    '오늘', '어제', '내일', '지금', '이번', '지난', '다음', '이후', '현재',
    '최근', '요즘', '하루', '이틀', '일주일', '한달', '올해', '작년', '내년',
    '오전', '오후', '저녁', '시간', '날짜', '당시', '올초', '연초', '연말',
    '것', '수', '때', '곳', '점', '듯', '뿐', '채', '중', '후', '전',
    '때문', '위해', '통해', '관련', '대한', '위한', '인해', '따른',
    '경우', '정도', '생각', '이유', '방법', '결과', '내용', '부분',
    '상황', '기준', '의미', '느낌', '차이', '종류', '형태', '방식',
    '가지', '번째', '나머지', '마지막', '처음', '기존', '해당', '전반',
    '사람', '사람들', '분들', '여러분', '친구', '가족',
    '선택', '근황', '소식', '사건', '이슈모음',
    '유튜버', '중갤', '무료', '반응', '민원',
    '말', '얘기', '이야기', '말씀', '대화', '주제', '질문', '답변',
    '남자', '여자', '남성', '여성', '남편', '아내', '부인', '와이프',
    '엄마', '아빠', '어머니', '아버지', '부모', '자녀', '아이', '아들', '딸',
    '집', '방', '직장', '회사', '학교', '나라', '세상', '사회', '현실',
    '공지', '안내', '필독', '운영', '이용', '규칙', '게시판', '갤러리',
    '댓글', '답글', '글쓴이', '작성자', '조회수', '추천수', '비추천',
    '로그인', '회원가입', '신고',
    '이슈', '베스트', '인기자료', '실시간', '카테고리', '게시글',
    '자유게시판', '유머게시판', '정보게시판', '기타게시판',
    '최고조회', '베스트글', '인기글', '핫게시물', '급상승',
    '유머', '정보', '기타', '자유', '일반', '종합', '통합',
    '인기', '추천글', '명예글', '베오베', '베스트오브베스트',
    '라이브', '화제글', '실시간인기', '추천인기글', '라이브화제',
    '기사', '뉴스기사', '인기기사', '오늘기사', '최신기사',
    '포텐', '터짐', '포텐터짐', '힛갤', '개념글', '핫딜', '딜', '익게',
    '싱갤', '에펨', '에펨코리아', '클리앙', '루리웹', '더쿠', '인스티즈',
    '불펜', '모공', '명예의전당', '명예전당', '명예의', '전당', '화제톡', '톡커', '톡커들',
    '자게', '익명', '썸네일', '프사', '닉네임', '아이디', '계정',
    '디시', '디씨', '디시인사이드', '보배드림', '보배', '에펨',
    '아카라이브', '아카', '도그드립', '뽐뿌', '가생이', '이토랜드',
    '밝혔다', '종합', '기자', '연합뉴스', '뉴스', '속보', '단독',
    '주장', '발언', '발표', '보도', '취재', '입장', '해명', '논란',
    '충격', '경악', '황당', '황당함', '어이없', '충격적', '화제',
    '일상', '추천', '공유', '소통', '팔로우', '좋아요', '해시태그',
    '스타그램', '맞팔', '데일리', '일상글', '소통해요', '팔로잉',
    '선팔', '맞팔환영', '인친', '핫플', '핫하', '핫해',
    'ㅋㅋㅋ', 'ㅎㅎㅎ', '레알', '개웃', '개쩐', '레전드', '역대급',
    '인정', '공감', '동의', '맞아요', '맞음', '틀림', '아님',
    '진행', '완료', '시작', '마무리', '정리', '업데이트', '확인',
    '출처', '펌', '퍼온', '짤', '움짤', '사진', '영상', '동영상',
    '글쓰', '글올', '올려', '올림', '질문있', '도와주',
    '대박', '쩔어', '미쳤다', '실화냐', '레알', '헐', '와우',
    '좋았', '최고', '최악', '별로', '그냥저냥', '그저그래',
    '궁금', '신기', '흥미', '재미', '웃긴', '슬픈',
    '대단', '놀랍', '신선', '어메이징', '굿', '쩐다',
    '가봤', '해봤', '먹었', '봤어', '했어', '왔어', '갔어',
    '핫하', '맛있', '귀엽', '예쁜', '멋진', '이쁜',
    '잘했', '못했', '해서', '하면서', '하니까', '하더니',
    '다들', '어떻게', '왜이렇', '어디서', '뭐하', '뭔데',
    '어디가', '언제부터', '얼마나', '어디까지', '어디에',
    '어떡해', '어떡하', '어쩌라', '어쩌지', '어쩌면',
    '그러게', '그렇구나', '그렇지', '맞지', '맞죠', '그쵸',
    '아니지', '아니죠', '아닌가', '모르겠', '모르지',
    '뭐야', '뭔가요', '뭔지', '뭔데', '뭔일', '웬일',
    '이게뭐', '저게뭐', '그게뭐',
}

HISTORY_SIZE = 6

SUMMARY_SELECTORS = {
    'todayhumor': '.viewContent p, .memo_content p',
    'ruliweb':    '.rd_body p, .article_box p',
    'clien':      '.post_article p',
    'theqoo':     '.xe_content p',
    'mlbpark':    '.view_content p, .bd_body p',
}

SUMMARY_MAX_POSTS = 25


class TrendCrawler:
    def __init__(self):
        self._lock = threading.Lock()
        self._posts: list = []
        self._history: list = []
        self._post_score_history: list = []
        self._trends: dict = {}
        self._last_updated = None
        self._status = 'idle'
        self._crawl_count = 0
        self._ai_summary: str = ''
        self._ai_summary_updated = None

    def get_data(self) -> dict:
        with self._lock:
            return {
                'posts': list(self._posts),
                'trends': dict(self._trends),
                'last_updated': self._last_updated,
                'status': self._status,
                'total': len(self._posts),
                'crawl_count': self._crawl_count,
                'sources': [s['label'] for s in COMMUNITY_SOURCES],
                'ai_summary': self._ai_summary,
                'ai_summary_updated': self._ai_summary_updated.isoformat() if self._ai_summary_updated else None,
            }

    def _fetch_todaybeststory(self) -> list:
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        api_url = 'https://todaybeststory.com/api/v2/communities/posts/range'
        raw_posts = []
        try:
            for page in range(1, 11):
                headers = _random_headers({'Referer': 'https://todaybeststory.com/communities'})
                r = requests.get(api_url, headers=headers,
                    params={'startDate': today, 'endDate': today, 'page': page, 'limit': 30},
                    timeout=15)
                r.raise_for_status()
                data = r.json()
                raw_posts.extend(data.get('items', []))
                if not data.get('hasNext'):
                    break
                time.sleep(random.uniform(0.3, 0.8))
        except Exception as e:
            print(f'[TodayBestStory] 오류 (page {page}): {e}')
        if not raw_posts:
            return []

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
            'DOG': ('dogdrip', '도그드립', '🐶', '#00b894'),
            'ETO': ('etoland', '이토랜드', '🎯', '#fd79a8'),
            'GAS': ('gasengi', '가생이닷컴', '🌏', '#e84393'),
            'ILB': ('ilbe', '일베', '⚡', '#636e72'),
            'INV': ('inven', '인벤', '⚔️', '#d35400'),
            'PPO': ('ppomppu', '뽐뿌', '💰', '#27ae60'),
            'SLR': ('slrclub', 'SLR클럽', '📷', '#2980b9'),
            'TOD': ('todayhumor', '오늘의유머', '😂', '#2ecc71'),
            'YGO': ('ygosu', '와고수', '🎲', '#8e44ad'),
            '82C': ('cook82', '82쿡', '👩‍🍳', '#e74c3c'),
        }

        items = []
        for post in raw_posts:
            cid = post.get('communityId', '')
            src_info = COMMUNITY_ID_MAP.get(cid)
            if not src_info:
                continue
            src_id, src_label, src_emoji, src_color = src_info

            title = post.get('postTitle', '').strip()
            if len(title) < 4:
                continue

            url = post.get('postUrl', '')
            if not url:
                continue

            date_raw = post.get('postDatetime', '')
            date_val = self._parse_date(date_raw[:10]) if date_raw else datetime.now(timezone.utc).strftime('%Y-%m-%d')

            views = post.get('readCount', 0) or 0
            text = title + ' ' + post.get('postDesc', '')

            items.append({
                'source': src_id,
                'source_label': src_label,
                'source_emoji': src_emoji,
                'source_color': src_color,
                'title': title,
                'summary': post.get('postDesc', '').strip(),
                'url': url,
                'image': '',
                'author': post.get('postWriterName', ''),
                'date': date_val,
                'keyword': self._main_category(text),
                'is_food':    self._matches(text, FOOD_WORDS),
                'is_beauty':  self._matches(text, BEAUTY_WORDS),
                'is_fashion': self._matches(text, FASHION_WORDS),
                'is_travel':  self._matches(text, TRAVEL_WORDS),
                'is_game':    self._matches(text, GAME_WORDS),
                'is_celeb':   self._matches(text, CELEB_WORDS),
                'is_humor':   self._matches(text, HUMOR_WORDS),
                'is_car':     self._matches(text, CAR_WORDS),
                'views': views,
                'position_score': max(0, 100 - len(items) * 0.5),
                'rank_score': 0,
                'rank': 0,
                'likes': post.get('upvoteCount', 0) or 0,
                'comments': post.get('commentCount', 0) or 0,
                'is_sample': False,
            })
        print(f'[TodayBestStory] {len(raw_posts)}개 원본 → {len(items)}개 파싱 완료')
        return items

    AI_SUMMARY_INTERVAL = 21600

    def _collect_all_posts(self) -> list:
        """포스트 수집 전체 단계. refresh()에서 180초 master hard timeout으로 호출된다."""
        # TodayBestStory API — 90초 inner hard timeout (DNS/TCP hang 방지)
        api_posts = []
        _tbs_ex = ThreadPoolExecutor(max_workers=1)
        try:
            api_posts = _tbs_ex.submit(self._fetch_todaybeststory).result(timeout=90)
        except Exception as e:
            print(f'[Refresh] TodayBestStory inner timeout: {e}')
        finally:
            _tbs_ex.shutdown(wait=False)

        if len(api_posts) >= 50:
            print(f'[Refresh] TodayBestStory {len(api_posts)}개 — 직접 스크래핑 생략')
            return api_posts

        print(f'[Refresh] TodayBestStory {len(api_posts)}개 부족 — 직접 스크래핑 fallback')
        posts = list(api_posts)
        for src in COMMUNITY_SOURCES:
            for url in src['pages']:
                items = self._scrape_community(src, url)
                posts.extend(items)
                time.sleep(0.5)
        return posts

    def refresh(self):
        with self._lock:
            self._status = 'crawling'
            last_sum = self._ai_summary_updated

        # 전체 포스트 수집 — 180초 master hard timeout (DNS/TCP hang 완전 차단)
        posts = []
        _collect_ex = ThreadPoolExecutor(max_workers=1)
        try:
            posts = _collect_ex.submit(self._collect_all_posts).result(timeout=180)
        except Exception as e:
            print(f'[Refresh] 포스트 수집 master timeout: {e}')
        finally:
            _collect_ex.shutdown(wait=False)

        # 공지/중복 필터
        seen, unique = set(), []
        for p in posts:
            key = p['url']
            if key not in seen and not self._is_notice(p['title']):
                seen.add(key)
                unique.append(p)

        unique = self._assign_ranks(unique)

        with self._lock:
            prev_post_history = list(self._post_score_history)
        self._compute_post_velocity(unique, prev_post_history)
        curr_scores = {p['url']: p.get('rank_score', 0.0) for p in unique}

        # 상위 포스트 본문 요약 병렬 수집 — 25초 hard timeout
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

        counter = self._word_counter(unique)

        # AI 요약 갱신 — 35초 hard timeout
        now = datetime.now(timezone.utc)
        needs_summary = (last_sum is None or
                         (now - last_sum).total_seconds() > self.AI_SUMMARY_INTERVAL)
        new_summary = None
        if needs_summary:
            new_summary = self._generate_ai_summary(unique)

        with self._lock:
            self._posts = unique
            self._history.append(counter)
            if len(self._history) > HISTORY_SIZE:
                self._history.pop(0)
            self._post_score_history.append(curr_scores)
            if len(self._post_score_history) > HISTORY_SIZE:
                self._post_score_history.pop(0)
            self._trends = self._score_trends(unique, self._history)
            self._last_updated = now.isoformat()
            self._crawl_count += 1
            self._status = 'ok'
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
            prev = avg_prev.get(p['url'])
            p['post_velocity'] = curr - (prev if prev is not None else 0.0)

    def _generate_ai_summary(self, posts: list) -> str:
        api_key = os.environ.get('GOOGLE_API_KEY', '')
        if not api_key:
            return ''

        seen_src: set = set()
        top_posts: list = []
        for p in posts:
            if p['source'] not in seen_src:
                top_posts.append(p)
                seen_src.add(p['source'])
            if len(top_posts) >= 20:
                break

        if not top_posts:
            return ''

        src_label_map = {s['id']: s['label'] for s in COMMUNITY_SOURCES}
        lines = []
        for p in top_posts:
            label = p.get('source_label') or src_label_map.get(p['source'], p['source'])
            views = p.get('views', 0)
            views_str = f' (조회수 {views:,})' if views else ''
            lines.append(f"- [{label}] {p['title']}{views_str}")

        prompt = (
            "다음은 오늘 한국 주요 인터넷 커뮤니티에서 가장 화제가 된 글 제목들입니다:\n\n"
            + "\n".join(lines)
            + "\n\n위 제목들을 바탕으로 오늘 온라인 커뮤니티의 분위기와 주요 이슈를 요약해주세요.\n"
            "작성 규칙:\n"
            "- 5~7문장으로 작성\n"
            "- 제목에 나와 있는 내용만 언급하고, 제목에 없는 세부 내용은 절대 추측하거나 지어내지 말 것\n"
            "- 조회수가 높은 글일수록 더 비중 있게 다루기\n"
            "- 주제별로 자연스럽게 묶어서 흐름이 느껴지게\n"
            "- 커뮤니티 이름은 나열하지 말 것"
        )

        def _call_gemini():
            _url = (
                'https://generativelanguage.googleapis.com/v1beta/models/'
                'gemini-2.5-flash:generateContent'
            )
            _payload = {'contents': [{'parts': [{'text': prompt}]}]}
            _r = requests.post(_url, json=_payload,
                               headers={'x-goog-api-key': api_key}, timeout=30)
            _r.raise_for_status()
            return _r.json()['candidates'][0]['content']['parts'][0]['text']

        ex = ThreadPoolExecutor(max_workers=1)
        try:
            future = ex.submit(_call_gemini)
            text = future.result(timeout=35)
            print('[Gemini] 요약 생성 완료')
            return text.strip()
        except Exception as e:
            print(f'[Gemini] 요약 생성 오류: {e}')
            return ''
        finally:
            ex.shutdown(wait=False)

    def _scrape_community(self, src: dict, url: str) -> list:
        time.sleep(random.uniform(0.5, 1.5))
        for attempt in range(2):
          try:
            headers = _random_headers({'Referer': src['base_url'] + '/'})
            r = requests.get(url, headers=headers, timeout=12)
            r.raise_for_status()
            break
          except Exception as e:
            if attempt == 1:
                print(f'[{src["label"]}] {url} 오류: {e}')
                return []
            time.sleep(2)
        try:
            soup = BeautifulSoup(r.content, 'html.parser')
            anchors = soup.select(src['title_sel'])

            view_counts = []
            if src.get('view_sel'):
                for el in soup.select(src['view_sel']):
                    view_counts.append(self._parse_count(el.get_text().strip()))

            dates = []
            date_per_anchor = {}
            if src.get('date_sibling'):
                sib_cls = src['date_sibling']
                for a in anchors:
                    parent_td = a.find_parent('td')
                    date_val = ''
                    if parent_td:
                        sib = parent_td.find_next_sibling('td', class_=sib_cls)
                        if sib:
                            date_val = self._parse_date(sib.get_text().strip())
                        elif src.get('date_inner'):
                            inner = a.select_one(src['date_inner'])
                            if inner:
                                date_val = self._parse_date(inner.get_text().strip())
                    date_per_anchor[id(a)] = date_val
            elif src.get('date_sel'):
                raw_dates = [el.get_text().strip() for el in soup.select(src['date_sel'])]
                if not raw_dates:
                    print(f'[{src["label"]}] date_sel="{src["date_sel"]}" 결과 없음')
                dates = [self._parse_date(d) for d in raw_dates]

            anchor_has_all = bool(src.get('title_text_sel'))

            items = []
            item_pos = 0
            for pos, a in enumerate(anchors):
                if anchor_has_all:
                    title_el = a.select_one(src['title_text_sel'])
                    title = title_el.get_text().strip() if title_el else ''
                elif src.get('title_inner_sel'):
                    inner = a.select_one(src['title_inner_sel'])
                    title = inner.get_text().strip() if inner else re.sub(r'(?<!\s)\d+$', '', a.get_text().strip()).strip()
                else:
                    title = re.sub(r'(?<!\s)\d+$', '', a.get_text().strip()).strip()
                if len(title) < 4:
                    continue
                href = a.get('href', '')
                if not href:
                    continue
                if href.startswith('http'):
                    full_url = href
                elif href.startswith('/'):
                    full_url = src['base_url'] + href
                else:
                    full_url = src['base_url'] + '/' + href

                if anchor_has_all:
                    view_el = a.select_one(src['view_sel']) if src.get('view_sel') else None
                    views = self._parse_count(view_el.get_text().strip()) if view_el else 0
                    date_val = ''
                    if src.get('date_sel'):
                        date_el = a.select_one(src['date_sel'])
                        if date_el:
                            raw = date_el.get(src['date_attr']) if src.get('date_attr') else date_el.get_text().strip()
                            date_val = self._parse_date(raw) if raw else ''
                else:
                    views = view_counts[pos] if pos < len(view_counts) else 0

                position_score = max(0, 100 - item_pos * 1.5)

                if not anchor_has_all:
                    if date_per_anchor:
                        date_val = date_per_anchor.get(id(a), '')
                    else:
                        date_val = dates[pos] if pos < len(dates) else ''
                if not date_val and src.get('default_date') == 'today':
                    date_val = datetime.now(timezone.utc).strftime('%Y-%m-%d')

                text = title
                item_pos += 1
                items.append({
                    'source': src['id'],
                    'source_label': src['label'],
                    'source_emoji': src['emoji'],
                    'source_color': src['color'],
                    'title': title,
                    'summary': '',
                    'url': full_url,
                    'image': '',
                    'author': '',
                    'date': date_val,
                    'keyword': self._main_category(text),
                    'is_food':    self._matches(text, FOOD_WORDS),
                    'is_beauty':  self._matches(text, BEAUTY_WORDS),
                    'is_fashion': self._matches(text, FASHION_WORDS),
                    'is_travel':  self._matches(text, TRAVEL_WORDS),
                    'is_game':    self._matches(text, GAME_WORDS),
                    'is_celeb':   self._matches(text, CELEB_WORDS),
                    'is_humor':   self._matches(text, HUMOR_WORDS),
                    'is_car':     self._matches(text, CAR_WORDS),
                    'views': views,
                    'position_score': position_score,
                    'rank_score': 0,
                    'rank': 0,
                    'likes': 0,
                    'comments': 0,
                    'is_sample': False,
                })
            return items
        except Exception as e:
            print(f'[{src["label"]}] {url} 오류: {e}')
            return []

    def _parse_date(self, text: str) -> str:
        now = datetime.now(timezone.utc)
        text = text.strip()
        text = re.sub(r'\s*[lL│|ㅣ]\s*조회.*$', '', text).strip()
        if not text:
            return ''
        try:
            m = re.match(r'(\d+)분\s*전', text)
            if m:
                return (now - timedelta(minutes=int(m.group(1)))).strftime('%Y-%m-%d %H:%M')
            m = re.match(r'(\d+)시간\s*전', text)
            if m:
                return (now - timedelta(hours=int(m.group(1)))).strftime('%Y-%m-%d %H:%M')
            m = re.match(r'(\d+)일\s*전', text)
            if m:
                return (now - timedelta(days=int(m.group(1)))).strftime('%Y-%m-%d %H:%M')
            if text.strip() == '어제':
                return (now - timedelta(days=1)).strftime('%Y-%m-%d')
            m = re.match(r'^(\d{1,2}):(\d{2})$', text)
            if m:
                return now.strftime('%Y-%m-%d') + f" {m.group(1).zfill(2)}:{m.group(2)}"
            m = re.match(r'^(\d{1,2})[.](\d{1,2})\s+(\d{1,2}):(\d{2})$', text)
            if m:
                return f"{now.year}-{m.group(1).zfill(2)}-{m.group(2).zfill(2)} {m.group(3).zfill(2)}:{m.group(4)}"
            m = re.match(r'^(\d{1,2})[./-](\d{1,2})$', text)
            if m:
                return f"{now.year}-{m.group(1).zfill(2)}-{m.group(2).zfill(2)}"
            m = re.match(r'^(\d{2})/(\d{2})/(\d{2})\s+(\d{1,2}):(\d{2})$', text)
            if m:
                return f"20{m.group(1)}-{m.group(2)}-{m.group(3)} {m.group(4).zfill(2)}:{m.group(5)}"
            m = re.match(r'^(\d{2})/(\d{2})/(\d{2})$', text)
            if m:
                return f"20{m.group(1)}-{m.group(2)}-{m.group(3)}"
            m = re.match(r'^(\d{2})[.](\d{2})[.](\d{2})$', text)
            if m:
                return f"20{m.group(1)}-{m.group(2)}-{m.group(3)}"
            m = re.match(r'(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})', text)
            if m:
                base = f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"
                t = re.search(r'(\d{1,2}):(\d{2})', text)
                if t:
                    return base + f" {t.group(1).zfill(2)}:{t.group(2)}"
                return base
        except Exception:
            pass
        return ''

    def _parse_count(self, text: str) -> int:
        text = text.replace(',', '').strip()
        try:
            if 'k' in text.lower():
                return int(float(text.lower().replace('k', '')) * 1000)
            if 'M' in text or 'm' in text:
                return int(float(text.lower().replace('m', '')) * 1_000_000)
            return int(re.sub(r'[^\d]', '', text) or 0)
        except Exception:
            return 0

    def _fetch_instagram(self, hashtag: str) -> list:
        try:
            url = f'https://www.instagram.com/explore/tags/{hashtag}/'
            r = requests.get(url, headers=HEADERS, timeout=8)
            parsed = self._parse_insta_html(r.text, hashtag)
            if parsed:
                return parsed
        except Exception:
            pass
        return self._insta_sample(hashtag)

    def _parse_insta_html(self, html: str, hashtag: str) -> list:
        match = re.search(r'window\._sharedData\s*=\s*({.+?});</script>', html, re.DOTALL)
        if not match:
            return []
        try:
            data = json.loads(match.group(1))
            edges = (data.get('entry_data', {})
                     .get('TagPage', [{}])[0]
                     .get('graphql', {})
                     .get('hashtag', {})
                     .get('edge_hashtag_to_media', {})
                     .get('edges', []))
            items = []
            for edge in edges[:3]:
                node = edge.get('node', {})
                cap_edges = node.get('edge_media_to_caption', {}).get('edges', [])
                caption = cap_edges[0].get('node', {}).get('text', '') if cap_edges else ''
                shortcode = node.get('shortcode', '')
                text = hashtag + ' ' + caption
                items.append({
                    'source': 'instagram',
                    'source_label': '인스타그램',
                    'source_emoji': '📸',
                    'source_color': '#e040fb',
                    'title': f'#{hashtag}',
                    'summary': caption[:150],
                    'url': f'https://www.instagram.com/p/{shortcode}/',
                    'image': node.get('thumbnail_src', ''),
                    'author': '',
                    'date': datetime.fromtimestamp(node.get('taken_at_timestamp', time.time()), tz=timezone.utc).strftime('%Y-%m-%d %H:%M'),
                    'keyword': self._main_category(text),
                    'is_food':   self._matches(text, FOOD_WORDS),
                    'is_beauty': self._matches(text, BEAUTY_WORDS),
                    'is_fashion':self._matches(text, FASHION_WORDS),
                    'is_travel': self._matches(text, TRAVEL_WORDS),
                    'is_game':   self._matches(text, GAME_WORDS),
                    'is_celeb':  self._matches(text, CELEB_WORDS),
                    'is_humor':  self._matches(text, HUMOR_WORDS),
                    'is_car':    self._matches(text, CAR_WORDS),
                    'views': node.get('edge_liked_by', {}).get('count', 0),
                    'likes': node.get('edge_liked_by', {}).get('count', 0),
                    'comments': node.get('edge_media_to_comment', {}).get('count', 0),
                    'position_score': 0,
                    'rank_score': 0,
                    'rank': 0,
                    'is_sample': False,
                })
            return items
        except Exception:
            return []

    def _insta_sample(self, hashtag: str) -> list:
        templates = [
            f'#{hashtag} 요즘 여기 완전 핫하더라구요 ✨ 다들 가보셨어요? #추천 #일상',
            f'#{hashtag} 드디어 가봤는데 소문대로 대박이었어요 💕 #맛집 #데이트',
        ]
        text = hashtag
        return [{
            'source': 'instagram',
            'source_label': '인스타그램 (샘플)',
            'source_emoji': '📸',
            'source_color': '#e040fb',
            'title': f'#{hashtag}',
            'summary': templates[i % 2],
            'url': f'https://www.instagram.com/explore/tags/{hashtag}/',
            'image': '',
            'author': f'user_{random.randint(1000,9999)}',
            'date': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M'),
            'keyword': self._main_category(text),
            'is_food':   self._matches(text, FOOD_WORDS),
            'is_beauty': self._matches(text, BEAUTY_WORDS),
            'is_fashion':self._matches(text, FASHION_WORDS),
            'is_travel': self._matches(text, TRAVEL_WORDS),
            'is_game':   self._matches(text, GAME_WORDS),
            'is_celeb':  self._matches(text, CELEB_WORDS),
            'is_humor':  self._matches(text, HUMOR_WORDS),
            'is_car':    self._matches(text, CAR_WORDS),
            'views': 0,
            'likes': random.randint(300, 9000),
            'comments': random.randint(10, 400),
            'position_score': 0,
            'rank_score': 0,
            'rank': 0,
            'is_sample': True,
        } for i in range(2)]

    MAX_AGE_DAYS = 90
    DECAY_HALF   = 30

    def _age_days(self, date_str: str) -> float:
        if not date_str:
            return 0.0
        try:
            dt = datetime.strptime(date_str[:10], '%Y-%m-%d').replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - dt).total_seconds() / 86400
        except Exception:
            return 0.0

    def _age_decay(self, date_str: str) -> float:
        if not date_str:
            return 1.0
        try:
            age_days = self._age_days(date_str)
            if age_days > self.MAX_AGE_DAYS:
                return 0.0
            return 1.0
        except Exception:
            return 1.0

    def _assign_ranks(self, posts: list) -> list:
        import math
        posts = [p for p in posts if self._age_decay(p.get('date', '')) > 0.0]

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

            age_days = self._age_days(p.get('date', ''))
            decay = math.exp(-age_days / 90) if age_days >= 0 else 1.0
            p['rank_score'] = round(base_score * decay, 1)
            p['age_decay']  = round(decay, 2)

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
        for p in sorted_posts:
            n = src_counts.get(p['source'], 0)
            p['diversity_score'] = p['rank_score'] * (DAMPEN ** n)
            src_counts[p['source']] = n + 1

        src_included: dict = {}
        capped: list = []
        for p in sorted(sorted_posts, key=lambda x: x['diversity_score'], reverse=True):
            cnt = src_included.get(p['source'], 0)
            if cnt < MAX_PER_SOURCE:
                capped.append(p)
                src_included[p['source']] = cnt + 1

        final = sorted(capped, key=lambda x: x['diversity_score'], reverse=True)
        for i, p in enumerate(final):
            p['rank'] = i + 1
        return final

    _SUFFIX_RE = re.compile(
        r'(하더라구요|더라구요|더라고요|가보셨어요|셨어요|았어요|었어요'
        r'|겠어요|겠습니다|합니다|습니다|됩니다|입니다'
        r'|이에요|아요|어요|네요|군요|더라고|더라구'
        r'|는데요|인데요|은데요|했어요|봤어요|왔어요'
        r'|아버린|어버린|라버린|아버려|어버려|버린|버려'
        r'|으려고|려고|으려|려|라고|이라고'
        r'|는지|은지|을지|ㄹ지|다가|면서|으면서'
        r'|이다|하다|됐다|했다|같다|싶다|지만|지도|지는'
        r'|으로|에서|에게|한테|처럼|만큼|보다|까지|부터'
        r'|이나|이며|이고|이랑|랑|이죠|이지|이요'
        r'|는걸|은걸|ㄴ걸|는게|은게|ㄴ게'
        r'|았던|었던|던|았|었)$'
    )

    def _word_counter(self, posts: list) -> Counter:
        c = Counter()
        for p in posts:
            text = p.get('title', '') + ' ' + p.get('summary', '')
            words = re.findall(r'[가-힣]{2,8}', text)
            for w in words:
                if len(w) > 5:
                    continue
                stem = self._SUFFIX_RE.sub('', w)
                if len(stem) < 2:
                    continue
                if stem in STOP_WORDS or w in STOP_WORDS:
                    continue
                if len(stem) > 2 and stem[-1] in '의은는을를도와과' and stem[:-1] in STOP_WORDS:
                    continue
                c[stem] += 1
        return c

    def _score_trends(self, posts: list, history: list) -> dict:
        if not history:
            return {'rising': [], 'top': [], 'keywords': [], 'categories': {}}

        current = history[-1]
        prev    = history[-2] if len(history) >= 2 else Counter()
        older   = history[-4] if len(history) >= 4 else Counter()
        has_history = len(history) >= 2

        total = sum(current.values()) or 1
        scored = []
        for word, cnt in current.most_common(80):
            if cnt < 3:
                continue
            base  = (cnt / total) * 100 * 10
            vel   = (cnt - prev.get(word, 0)) * 2 + (cnt - older.get(word, 0))
            score = min(100, base + max(0, vel) * 3)
            scored.append({
                'word': word,
                'count': cnt,
                'score': round(score, 1),
                'velocity': vel if has_history else 0,
                'is_rising': has_history and vel > 0 and cnt >= 2,
            })

        rising = sorted(
            [s for s in scored if s['is_rising']],
            key=lambda x: x['velocity'], reverse=True
        )[:15]
        top = sorted(scored, key=lambda x: x['score'], reverse=True)[:20]

        src_counts = Counter(p.get('source', '') for p in posts)
        keywords = [{'keyword': k, 'count': c} for k, c in src_counts.most_common()]

        categories = {
            '음식/카페':  sum(1 for p in posts if p.get('is_food')),
            '뷰티/패션':  sum(1 for p in posts if p.get('is_beauty') or p.get('is_fashion')),
            '여행':       sum(1 for p in posts if p.get('is_travel')),
            '게임/IT':    sum(1 for p in posts if p.get('is_game')),
            '연예/아이돌': sum(1 for p in posts if p.get('is_celeb')),
            '유머':       sum(1 for p in posts if p.get('is_humor')),
            '자동차':     sum(1 for p in posts if p.get('is_car')),
        }

        return {'rising': rising, 'top': top, 'keywords': keywords, 'categories': categories}

    def _matches(self, text: str, word_set: set) -> bool:
        return any(w in text for w in word_set)

    def _main_category(self, text: str) -> str:
        for cat, words in [
            ('음식', FOOD_WORDS), ('뷰티', BEAUTY_WORDS), ('패션', FASHION_WORDS),
            ('여행', TRAVEL_WORDS), ('게임/IT', GAME_WORDS), ('연예', CELEB_WORDS),
            ('유머', HUMOR_WORDS), ('자동차', CAR_WORDS),
        ]:
            if self._matches(text, words):
                return cat
        return '일반'

    def _is_notice(self, title: str) -> bool:
        notice_words = ['공지', '안내', '필독', '이용규칙', '운영', '모집', '체험단']
        return any(w in title for w in notice_words) and len(title) < 30

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
                text = re.sub(r'\s+', ' ', text).strip()
                if len(text) >= 15 and not any(w in text for w in ['광고', '제휴', 'AD']):
                    post['summary'] = text[:130] + ('…' if len(text) > 130 else '')
                    return
        except Exception:
            pass
