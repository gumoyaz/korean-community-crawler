"""
Korean internet community trend crawler.
Sources: FMKorea, 오늘의유머, 루리웹, 클리앙 + Instagram fallback.
Trend scoring: keyword frequency + velocity (rate of change between rounds).
"""

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

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept-Language': 'ko-KR,ko;q=0.9',
    'Accept': 'text/html,application/xhtml+xml,*/*;q=0.8',
}

# ── 커뮤니티 소스 정의 ────────────────────────────────────────────────────────

COMMUNITY_SOURCES = [
    {
        'id': 'fmkorea',
        'label': 'FMKorea 베스트',
        'pages': [
            'https://www.fmkorea.com/index.php?mid=best&listStyle=list&page=1',
            'https://www.fmkorea.com/index.php?mid=best&listStyle=list&page=2',
        ],
        'title_sel': 'td.title a',
        'view_sel': None,          # rate-limit으로 접근 불안정 → 위치 점수만 사용
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
        'title_sel': '.title a',
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
        'view_sel': None,
        'date_sel': '.date',
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
        'id': 'miznet',
        'label': '미즈넷',
        'pages': [
            'https://www.miznet.net/bbs/board.php?bo_table=free',
            'https://www.miznet.net/bbs/board.php?bo_table=free&page=2',
        ],
        'title_sel': 'td.td_subject .bo_tit',
        'view_sel': None,
        'date_sel': 'td.td_datetime',
        'base_url': 'https://www.miznet.net',
        'color': '#a29bfe',
        'emoji': '👩',
    },
    {
        'id': 'humoruniv',
        'label': '웃긴대학',
        'pages': [
            'https://web.humoruniv.com/board/humor/board_best.html',
        ],
        'title_sel': 'td a[href*="read.html"]',
        'view_sel': None,
        'date_sel': None,
        'base_url': 'https://web.humoruniv.com/board/humor',
        'color': '#f9ca24',
        'emoji': '🤣',
    },
]

INSTAGRAM_HASHTAGS = [
    '맛스타그램', '카페스타그램', '오오티디', '뷰티', '여행스타그램', '일상', '먹스타그램'
]

# ── 키워드/카테고리 ────────────────────────────────────────────────────────────

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
    # 대명사/지시어
    '이', '그', '저', '것', '수', '이것', '그것', '저것', '이게', '그게', '저게',
    '이거', '그거', '저거', '여기', '거기', '저기', '이쪽', '그쪽', '저쪽',
    '우리', '저희', '나', '너', '자기', '본인', '누구', '아무', '모두',
    # 형용사/관형어
    '같은', '다른', '새로운', '좋은', '나쁜', '많은', '적은', '큰', '작은',
    '높은', '낮은', '빠른', '느린', '넓은', '좁은', '오래된', '다양한', '이런',
    '저런', '그런', '어떤', '무슨', '어느', '모든', '각각', '일부', '전체',
    # 부사
    '정말', '진짜', '너무', '매우', '완전', '엄청', '굉장', '되게', '엄청나',
    '조금', '약간', '살짝', '아주', '더', '덜', '가장', '제일', '그냥',
    '요즘', '이제', '이미', '아직', '계속', '다시', '또', '또한', '먼저',
    '나중', '항상', '자주', '가끔', '별로', '거의', '특히', '보통', '주로',
    '바로', '갑자기', '드디어', '역시', '원래', '사실', '당연', '물론',
    '오히려', '한편', '분명', '확실', '아마', '혹시', '결국', '여전히',
    '마침내', '겨우', '벌써', '이미', '먼저', '다시', '혼자', '함께',
    # 접속사/담화표지
    '그리고', '하지만', '근데', '그래서', '그러나', '그래도', '그러면',
    '따라서', '게다가', '다만', '단지', '즉', '또는', '혹은', '반면',
    '그러므로', '왜냐면', '이처럼', '이렇게', '저렇게', '어쨌든',
    # 동사/형용사 어간 (2~3음절로 남는 것들)
    '있는', '없는', '하는', '되는', '되어', '이다', '한다', '됐다',
    '있네', '없네', '좋네', '했네', '왔네', '봤네', '같네', '됐네',
    '하게', '되게', '이게', '없이', '있어', '없어', '좋아', '싫어',
    '않는', '않고', '않아', '못하', '못해',
    # 시간 표현
    '오늘', '어제', '내일', '지금', '이번', '지난', '다음', '이후', '현재',
    '최근', '요즘', '하루', '이틀', '일주일', '한달', '올해', '작년', '내년',
    '오전', '오후', '저녁', '시간', '날짜',
    # 일반명사 (트렌드 가치 없음)
    '것', '수', '때', '곳', '점', '듯', '뿐', '채', '중', '후', '전',
    '때문', '위해', '통해', '관련', '대한', '위한', '인해', '따른',
    '경우', '정도', '생각', '이유', '방법', '결과', '내용', '부분',
    '상황', '기준', '의미', '느낌', '차이', '종류', '형태', '방식',
    '가지', '번째', '나머지', '마지막', '처음', '기존', '해당', '전반',
    '사람', '사람들', '분들', '여러분', '친구', '가족',
    # 게시판/운영 용어
    '공지', '안내', '필독', '운영', '이용', '규칙', '게시판', '갤러리',
    '댓글', '답글', '글쓴이', '작성자', '조회수', '추천수', '비추천',
    '로그인', '회원가입', '신고',
    # 뉴스 클리셰
    '밝혔다', '종합', '기자', '연합뉴스', '뉴스',
    # SNS/인스타 상투어
    '일상', '추천', '공유', '소통', '팔로우', '좋아요', '해시태그',
    '스타그램', '맞팔', '데일리', '일상글', '소통해요', '팔로잉',
    '선팔', '맞팔환영', '인친', '핫플', '핫하', '핫해',
    # 커뮤니티 상투어
    'ㅋㅋㅋ', 'ㅎㅎㅎ', '레알', '개웃', '개쩐', '레전드', '역대급',
    '인정', '공감', '동의', '맞아요', '맞음', '틀림', '아님',
    '진행', '완료', '시작', '마무리', '정리', '업데이트', '확인',
    '출처', '펌', '퍼온', '짤', '움짤', '사진', '영상', '동영상',
    # 막연한 감탄/평가어
    '대박', '쩔어', '미쳤다', '실화냐', '레알', '헐', '와우',
    '좋았', '최고', '최악', '별로', '그냥저냥', '그저그래',
    '궁금', '신기', '흥미', '재미', '웃긴', '슬픈',
    # 동사 어근 (어미 제거 후 남는 의미없는 것들)
    '가봤', '해봤', '먹었', '봤어', '했어', '왔어', '갔어',
    '핫하', '맛있', '귀엽', '예쁜', '멋진', '이쁜',
    # 의문/감탄 표현
    '다들', '어떻게', '왜이렇', '어디서', '뭐하', '뭔데',
    '어디가', '언제부터', '얼마나', '어디까지', '어디에',
    '어떡해', '어떡하', '어쩌라', '어쩌지', '어쩌면',
    # 구어체 반응어
    '그러게', '그렇구나', '그렇지', '맞지', '맞죠', '그쵸',
    '아니지', '아니죠', '아닌가', '모르겠', '모르지',
    '뭐야', '뭔가요', '뭔지', '뭔데', '뭔일', '웬일',
    '이게뭐', '저게뭐', '그게뭐',
}

HISTORY_SIZE = 6

# 포스트 본문 요약용 CSS 셀렉터 (사이트별)
SUMMARY_SELECTORS = {
    'todayhumor': '.viewContent p, .memo_content p',
    'ruliweb':    '.rd_body p, .article_box p',
    'clien':      '.post_article p',
    'theqoo':     '.xe_content p',
    'mlbpark':    '.view_content p, .bd_body p',
}

SUMMARY_MAX_POSTS = 25  # 상위 N개만 요약 수집


# ── Crawler ──────────────────────────────────────────────────────────────────

class TrendCrawler:
    def __init__(self):
        self._lock = threading.Lock()
        self._posts: list = []
        self._history: list = []
        self._trends: dict = {}
        self._last_updated = None
        self._status = 'idle'
        self._crawl_count = 0

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
            }

    def refresh(self):
        with self._lock:
            self._status = 'crawling'

        posts = []

        for src in COMMUNITY_SOURCES:
            for url in src['pages']:
                items = self._scrape_community(src, url)
                posts.extend(items)
                time.sleep(0.5)

        for tag in INSTAGRAM_HASHTAGS:
            posts.extend(self._fetch_instagram(tag))

        # 공지/중복 필터
        seen, unique = set(), []
        for p in posts:
            key = p['url']
            if key not in seen and not self._is_notice(p['title']):
                seen.add(key)
                unique.append(p)

        # 전체 랭킹 점수 계산 (조회수 로그 정규화 + 위치 점수)
        unique = self._assign_ranks(unique)

        # 상위 포스트 본문 요약 병렬 수집 (커뮤니티만, fmkorea 제외)
        to_summarize = [p for p in unique[:SUMMARY_MAX_POSTS]
                        if p['source'] in SUMMARY_SELECTORS and not p['summary']]
        if to_summarize:
            with ThreadPoolExecutor(max_workers=8) as ex:
                ex.map(self._fetch_summary, to_summarize)

        counter = self._word_counter(unique)

        with self._lock:
            self._posts = unique
            self._history.append(counter)
            if len(self._history) > HISTORY_SIZE:
                self._history.pop(0)
            self._trends = self._score_trends(unique, self._history)
            self._last_updated = datetime.now(timezone.utc).isoformat()
            self._crawl_count += 1
            self._status = 'ok'

    # ── community scraper ─────────────────────────────────────────────────────

    def _scrape_community(self, src: dict, url: str) -> list:
        for attempt in range(2):
          try:
            headers = {**HEADERS, 'Referer': src['base_url'] + '/'}
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

            # 조회수 파싱
            view_counts = []
            if src.get('view_sel'):
                for el in soup.select(src['view_sel']):
                    view_counts.append(self._parse_count(el.get_text().strip()))

            # 날짜 파싱
            dates = []
            if src.get('date_sel'):
                for el in soup.select(src['date_sel']):
                    dates.append(self._parse_date(el.get_text().strip()))

            items = []
            for pos, a in enumerate(anchors):
                title = re.sub(r'\d+$', '', a.get_text().strip()).strip()
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

                views = view_counts[pos] if pos < len(view_counts) else 0

                # 위치 점수 (1위 = 100, 아래로 갈수록 감소)
                position_score = max(0, 100 - pos * 3)

                text = title
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
                    'date': dates[pos] if pos < len(dates) else '',
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
                    'rank_score': 0,   # refresh()에서 정규화 후 채움
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
        """다양한 날짜 포맷을 'YYYY-MM-DD HH:MM UTC' ISO 형식으로 변환."""
        now = datetime.now(timezone.utc)
        text = text.strip()
        try:
            # "N분 전"
            m = re.match(r'(\d+)분\s*전', text)
            if m:
                return (now - timedelta(minutes=int(m.group(1)))).strftime('%Y-%m-%d %H:%M')
            # "N시간 전"
            m = re.match(r'(\d+)시간\s*전', text)
            if m:
                return (now - timedelta(hours=int(m.group(1)))).strftime('%Y-%m-%d %H:%M')
            # "HH:MM" (오늘)
            m = re.match(r'^(\d{1,2}):(\d{2})$', text)
            if m:
                return now.strftime('%Y-%m-%d') + f" {m.group(1).zfill(2)}:{m.group(2)}"
            # "MM.DD" 또는 "MM/DD"
            m = re.match(r'^(\d{1,2})[./](\d{1,2})$', text)
            if m:
                return f"{now.year}-{m.group(1).zfill(2)}-{m.group(2).zfill(2)}"
            # "YY/MM/DD HH:MM" (오늘의유머 포맷)
            m = re.match(r'^(\d{2})/(\d{2})/(\d{2})\s+(\d{1,2}):(\d{2})$', text)
            if m:
                return f"20{m.group(1)}-{m.group(2)}-{m.group(3)} {m.group(4).zfill(2)}:{m.group(5)}"
            # "YY/MM/DD"
            m = re.match(r'^(\d{2})/(\d{2})/(\d{2})$', text)
            if m:
                return f"20{m.group(1)}-{m.group(2)}-{m.group(3)}"
            # "YYYY.MM.DD" 또는 "YYYY-MM-DD"
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

    # ── Instagram ─────────────────────────────────────────────────────────────

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

    # ── trend scoring ─────────────────────────────────────────────────────────

    def _assign_ranks(self, posts: list) -> list:
        import math
        max_views = max((p['views'] for p in posts if p['views'] > 0), default=1)
        for p in posts:
            v = p['views']
            view_score = (math.log1p(v) / math.log1p(max_views)) * 60 if v > 0 else 0
            p['rank_score'] = round(p['position_score'] * 0.4 + view_score * 0.6, 1)

        # 전체 통합 랭킹 (rank_score 내림차순)
        sorted_posts = sorted(posts, key=lambda x: x['rank_score'], reverse=True)
        for i, p in enumerate(sorted_posts):
            p['rank'] = i + 1
        return sorted_posts

    # 동사/형용사 어미 패턴 (긴 것부터 순서대로)
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
                # 6음절 이상은 거의 모두 동사구 → 제외
                if len(w) > 5:
                    continue
                stem = self._SUFFIX_RE.sub('', w)
                if len(stem) < 2:
                    continue
                if stem in STOP_WORDS or w in STOP_WORDS:
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

    # ── helpers ───────────────────────────────────────────────────────────────

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
                # 너무 짧거나 광고성 텍스트 제외
                if len(text) >= 15 and not any(w in text for w in ['광고', '제휴', 'AD']):
                    post['summary'] = text[:130] + ('…' if len(text) > 130 else '')
                    return
        except Exception:
            pass
