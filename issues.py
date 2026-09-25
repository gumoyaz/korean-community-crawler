"""
'지금 뜨는 이슈' 보드 — 인기글을 여러 커뮤니티에 퍼진 이야기(이슈) 단위로 묶는다.

build_issues(posts, ps_history, now) → trends.json 의 issues 블록
  posts       crawler가 랭킹한 글 목록(rank는 1..N 유일). 블록은 제목을 복사하지 않고 rank로 글을 가리킨다.
              전날 보충 글(prev_day)은 보드에 쓰지 않는다.
  ps_history  crawler의 post_score_history(라운드별 _url_key → rank_score). 연속으로 같은 라운드는 하나로 친다.
LLM 호출 없음, 새 의존성 없음(형태소 분석기 대신 규칙 토크나이저).

1. 준비   할인·포인트 글은 뺀다(피드에는 그대로). 묶기용 제목 사본에서 한자 약칭을 푼다(李 대통령 → 이재명 대통령).
2. 토큰   규칙 토크나이저 + '~고' 명사 보호·조사 되돌리기·별칭 통일·숫자+단위·합성어 쪼개기
3. 묶기   글마다 대표 구체어(home) 하나 → 같은 home끼리 잇고, 자주 함께 나오는 home끼리 합치고,
          퍼나르기(제목 글자 2-gram Jaccard ≥ 0.45)를 잇는다. 외톨이 글·한 커뮤니티 묶음은 한 번만 붙인다(사슬 없음).
          너무 크고 흩어진 묶음은 합치기 전으로 되돌린다.
4. 설명   종류(story·topic·repost), 이름, 대표 글('왜'), 시각, 상태(new·rising·steady·quiet)
5. 순위   heat = 커뮤니티별 최고 rank_score 합 + 같은 곳 추가 글(로그) × 신선도. 보드에는 믿을 만한 묶음만 최대 8개
6. 여기서만 뜨거운 글: 이슈에 안 든 글 중 커뮤니티 안 1~2위, 커뮤니티당 1개
"""

import hashlib
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

# LOW_DATA_POSTS(원본 글이 이보다 적으면 '글이 적은 시간' 안내)는 crawler에 둔다 — 이슈 계산이 실패한 빈 블록도 같은 기준을 쓰게
from crawler import LOW_DATA_POSTS, _url_key

KST = timezone(timedelta(hours=9))

RECENT_H = 3              # 이 시간 안에 여러 곳에 올라오기 시작하면 신규
MAX_ITEMS = 8             # 보드에 싣는 이슈 수
DUP_J = 0.45             # 제목 글자 2-gram Jaccard가 이 이상이면 같은 글 퍼나르기
BIG_N, BIG_C, COHESION = 15, 8, 0.5   # 응집도 가드: 글·커뮤니티가 이보다 많으면 1순위 단어가 이 비율을 덮어야 한다
HOT_MAX = 6               # 여기서만 뜨거운 글 수

# ── 손으로 관리하는 목록 ──────────────────────────────────────────────────────

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
    # 서술어·관형형 어그로 표현 (어미 규칙으로 떼면 '재난'·'곤란' 같은 명사가 망가져 목록으로 막는다)
    '있다', '없다', '있음', '없음', '했다', '방금',
    '들통', '들통난', '드러난', '밝혀진', '난리난', '터진', '뒤집힌', '발칵',
    '공개된', '알려진', '논란된', '화제된',
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
    '싱글벙글', '만원',   # 디시 말머리, 금액 표기
}

# 규칙 토크나이저가 '서술어/부사'로 따로 막는 흔한 단어 (분석기 없이 어미로 못 잡는 것)
ADV_STOP = {'제대로', '점점', '보고', '좀', '다시', '많이', '진짜로', '같이', '겨우', '엄청', '아직도', '이제',
            '걍', '딱', '또', '막', '왜', '뭐', '잘', '못', '안', '다', '단', '근데', '나도', '저도', '제가', '내가',
            '너네', '님들', '저희', '이거', '저거', '그짝', '무조건', '솔직한', '정말로', '아주', '결국',
            '수준', '근황', '최신', '실시간', '공식', '실제', '이유', '장면', '상황', '반응', '후기', '결론', '정체',
            '요즘', '오늘자', '전국민이', '유일한', '있었다', '좋다', '싶은', '누가', '누구', '이상한'}

# 제목 태그([속보]·약혐) 등) 중 주제어가 아닌 것 (경고·출처·형식)
TAG_STOP = {'약혐', '혐', '스포', '스포x', '스포?', 'ㅇㅎ', '후방', '펌', '블라', '속보', '단독', '안내', '공지',
            '속보/교도', '정보', '유머', '재업', '포텐대비 재업', '그림', '네이버', '스토브', '네이버페이',
            '한국닌텐도', '애플tv스토어', '추측', '수정', '인터뷰③', '헬시타임', '정치', '펌글'}
LATIN_STOP = {'JPG', 'GIF', 'MP4', 'TXT', 'SNS', 'TOP', 'TMI', 'OK', 'VS', 'FEAT'}

# '맥락이 있어야 뜻이 사는' 일반명사 — 이슈를 잇는 단독 근거로 쓰지 않는다
GENERIC = {
    '한국', '한국인', '대한민국', '미국', '일본', '중국', '국내', '해외', '외국', '외국인', '세계', '전세계',
    '대통령', '정부', '정권', '국회', '의원', '정치', '민주당', '국민', '선수', '감독', '배우', '가수', '아이돌',
    '남친', '여친', '남자친구', '여자친구', '결혼', '이혼', '연애', '회사', '직원', '신입', '사장', '팀장', '엄마', '아빠',
    '음식', '맛집', '게임', '영상', '드라마', '영화', '방송', '사진', '할인', '가격', '돈', '재산', '월급', '연봉',
    '사고', '사건', '논란', '의혹', '사과', '문제', '상태', '수준', '근황', '반응', '후기', '결론', '정체', '이유',
    '전화', '선물', '리뷰', '출산', '재판', '누나', '형', '동생', '조카', '시어머니', '장모님', '며느리', '올케',
    '추석', '명절', '연휴', '설날', '오늘', '시간', '인터넷', '유튜브', '유튜버', '인스타', '커뮤', '여초', '남초',
    '레전드', '대참사', '역대급', '최초', '국대', '프로', '팀', '도시', '학교', '병원', '공항', '택시', '직업', '처음',
    '마지막', '사람', '여자', '남자', '친구', '아이', '애', '가족', '부모', '자식', '남편', '아내', '와이프',
    'JPG', 'GIF', 'MP4', '대회', '일본인', '중국인', '베트남인', '모델', '작가', '의사', '기자', '회장', '의장',
    '지지율', '엔딩', '장면', '상황', '반전', '실력', '관리', '외모', '몸매', '피부', '스트레스', '생활비', '데이트',
    # 엉뚱한 글을 이은 추상·기능 명사 (2026-09-24 스냅숏 오류 사례에서 뽑음)
    '이상', '공개', '난리', '유일', '호소', '유행', '시기', '한국인들', '의미', '초반', '원인', '분위기',
    '한마디', '발언', '비판', '지적', '문화', '충격', '인기', '평가', '조언', '방법', '모습', '소개',
    '해도', '상을', '나선', '줘도', '국가', '인구', '시민', '회원', '신입들', '신입사원들', '글', '실시간', '최신',
    # 나라 이름: 한국·미국처럼 여러 이야기에 두루 나와 단독 연결 근거가 못 된다
    '베트남', '필리핀', '태국', '대만', '홍콩', '러시아', '우크라이나', '북한', '영국', '프랑스', '독일', '인도',
    '호주', '캐나다', '이스라엘', '이란', '튀르키예', '터키', '몽골', '인도네시아', '말레이시아', '브라질', '멕시코',
    '싱가포르',
    '런던',   # SUFFIX_KEEP으로 살린 도시 이름 — 날씨·여행 글에도 두루 나와 나라 이름처럼 단독 근거로 쓰지 않는다
}

# 어미 규칙('~던')이 잘라 먹는 명사 (런던 → 런). 옛 키워드 코드의 SUFFIX_KEEP_WORDS
SUFFIX_KEEP = {'런던'}

# 묶는 근거로 쓰지 않는 사건어·강조어 (이름 끝에 붙이는 것은 그대로 가능)
LINK_STOP = {'사태', '난리', '대형', '대형사고', '충격', '역대', '최악', '긴급', '속보', '단독', '현재', '오늘',
             '어제', '방금', '논란', '사건', '사고', '의혹', '근황', '상황', '결말', '정리', '요약'}

# 같은 대상의 다른 표기 → 하나로
ALIAS = {'PUBG': '배그', '펍지': '배그', '배틀그라운드': '배그', 'TANVUU': '탄부', '방플러': '방플', '마운자': '마운자로'}
# 기사 제목식 한자 약칭 → 이름 (묶기용 사본에만 적용, 화면에는 원제목). '李 대통령'처럼 직함이 붙을 때만
HANJA_ALIAS = [(re.compile(r'李\s*대통령'), '이재명 대통령'), (re.compile(r'尹\s*(전\s*)?대통령'), '윤석열 대통령'),
               (re.compile(r'文\s*(전\s*)?대통령'), '문재인 대통령'), (re.compile(r'與'), '여당 '), (re.compile(r'野'), '야당 ')]

# 규칙 토크나이저가 '~고' 어미로 오인하는 명사 (삼성냉장고 → 서술어)
NOUN_GO = ('냉장고', '광고', '창고', '재고', '경고', '신고', '최고', '사고', '보고서')

# 이름 끝에 붙일 사건 명사 (묶음 안 2글 이상에 나오면). 앞쪽일수록 우선
NAME_EVENTS = ('사건', '사고', '논란', '폭발', '의혹', '사과', '발언', '제재', '해고', '폭로', '열애', '결혼',
               '은퇴', '사망', '구속', '체포', '출시', '인상', '파업', '우승', '탈락', '공지')
# '왜' 점수: 이름의 사건 명사가 제목에도 있으면 가산
WHY_EVENTS = ('사건', '사고', '논란', '의혹', '폭발', '사과문', '사과', '지지율', '부상', '은퇴', '열애', '결혼',
              '사망', '구속', '기소', '체포', '탈퇴', '해체', '출시', '먹튀', '난동', '폭행', '출현', '반전')

# 이름·함께 나온 말에 쓰지 않는 욕설
PROFANITY = re.compile(r'존나|좆|씨발|시발|ㅅㅂ|새끼|병신|ㅂㅅ|개새|지랄|닥쳐|북돼지')
# 맨 위(대표 글·여기서만 뜨거운 글)에 올리지 않을 제목 — 피드에는 그대로
UNSAFE_RE = re.compile(r'ㅈ됐|ㅈ된|ㅈ같|약혐|혐\)|ㅇㅎ|ㅎㅂ|후방|19금|오르가즘|야짤|노출|섹스|야스|씹|존나|좆|씨발|시발|ㅅㅂ|새끼|병신|ㅂㅅ|지랄|북돼지|니미')
# '왜' 점수 감점: 선정적 표시
LEWD_RE = re.compile(r'약혐|혐\)|ㅇㅎ|후방|19금|오르가즘|야짤|노출|ㅗ')
# '왜' 점수 감점: ㅋㅋ·욕설 제목은 설명용으로 뒤로
MEME_RE = re.compile(r'ㅋㅋ|ㄷㄷ|ㅎㅎ|존나|시발|씨발|ㅅㅂ|새끼')
# 포인트 적립·할인 글 — 이슈 묶기에서 뺀다 (피드에는 그대로)
DEAL_RE = re.compile(r'네이버페이|네페|적립|\d+원\s*\+|할인|세일|특가|쿠폰|\(무료\)|/무료\)|핫딜|\$\d+')
# 숫자+단위(7개월·40대)도 연결 근거. %·원·년·일은 흔해서 뺐다 (지지율 20% ↔ 20%할인 오연결)
NUM_UNIT_RE = re.compile(r'\d+(?:개월|살|대|kg|km)')

# ── 규칙 토크나이저 ───────────────────────────────────────────────────────────

# 단어 끝에 붙는 1음절 조사 — 같은 라운드에 어근이 따로 있으면 어근으로 본다
PARTICLES = '이가은는을를의에와과로도만들랑'
# 이 조사들은 어근이 같은 라운드에 없어도 뗀다('명절에'가 급상승 1위에 오른 사례).
# 이·가·은·의·로·도·과·만은 명사 끝에도 흔해서 제외(고양이, 전문가, 김지은, 민주주의, 을지로, 제주도, 국문과, 오천만)
PARTICLES_ALWAYS = '에을를는와들'
# 어절 끝 어미·조사. '려'(우려·배려), '랑'(사랑), '이지'(페이지), '지도', '이나'(차이나), '다가'(바다가)는
# 명사 끝을 잘라 먹어서 뺐다. 1음절 조사는 _features의 어근 병합으로 처리한다.
SUFFIX_RE = re.compile(
    r'(하더라구요|더라구요|더라고요|가보셨어요|셨어요|았어요|었어요'
    r'|겠어요|겠습니다|합니다|습니다|됩니다|입니다'
    r'|이에요|아요|어요|네요|군요|더라고|더라구'
    r'|는데요|인데요|은데요|했어요|봤어요|왔어요'
    r'|아버린|어버린|라버린|아버려|어버려|버린|버려'
    r'|으려고|려고|라고|이라고'
    r'|는지|은지|을지|ㄹ지|면서|으면서'
    r'|이다|하다|됐다|했다|같다|싶다|지만|지는'
    r'|으로|에서|에게|한테|처럼|만큼|보다|까지|부터'
    r'|이며|이고|이랑|이죠|이요'
    r'|는걸|은걸|ㄴ걸|는게|은게|ㄴ게'
    r'|았던|었던|던|았|었)$'
)
# 서술어·관형형·연결형 어미 (어절 끝). 명사를 덜 망가뜨리도록 2음절 이상 어미 위주로
PRED_END_RE = re.compile(
    r'(?:했|됐|였|었|았|겠|싶|같|있|없|않|했었|하였)'   # 선어말/보조 어간이 어절 안에 들어 있으면
    r'|(?:다|요|죠|네|냐|니|까|래|대요|래요|세요|군|구나|더라|더니|는데|은데|던데|니까|려고|려는|면서|지만|고요|거든)$'
    r'|(?:하는|하던|하게|하고|해서|하며|하면|하려|한테|해야|해줘|해봐|하자|했|하니|된|되는|되고|될|됨|함|싶은|싶어|싶다)$'
)
# 관형·연결 어미로 끝나는 활용형(3음절 이상일 때만): 헤어진, 걸렸던, 확인하고, 사귀고, 유일한
PRED_TAIL3_RE = re.compile(r'(?:[가-힣]{2,})(?:고|던|진|린|한|운|른|는|은|며|게|서|지)$')

# 디시 dcbest 말머리 [싱갤] [야갤] … / [속보] [단독] [안내] 등
BRACKET_RE = re.compile(r'[\[【]([^\]】]{1,14})[\]】]')
# '배그)' '블라)' '약혐)' '스포?)' 같은 앞머리 태그 (괄호 앞 1~6글자)
PREFIX_TAG_RE = re.compile(r'^\s*([가-힣A-Za-z0-9?Xx]{1,6})\)\s*')
EXT_RE = re.compile(r'\.+\s*(jpg|jpeg|jpf|gif|png|mp4|twt|txt|manhwa|webp|avi|mov)\b', re.I)
JAMO_RE = re.compile(r'[ㄱ-ㅎㅏ-ㅣ]+')
DC_GALLERY_RE = re.compile(r'^[가-힣]{1,3}갤$')   # dcinside 갤러리 약칭 — 게시판명 잡음
WORD_SPLIT_RE = re.compile(r'[\s,·…:;/"“”\'‘’!?~()\[\]<>|=+*&^%$#@]+')
PIECE_RE = re.compile(r'[가-힣]+|[A-Za-z][A-Za-z0-9\-]*|\d+(?:\.\d+)?[가-힣%]*')

NUM_RE = re.compile(r'^\d')
PRED_NAME_RE = re.compile(r'(났|했|됐|였|었|았|겠|싶)다$|는데$')   # 이름에서 뺄 서술어
NAME_PARTICLE = set('은는이가을를에의')
CASE_P = set('은는이가을를에의도로와과만')
TRAIL_RE = re.compile(r'(\s*[ㄱ-ㅎㅏ-ㅣ]{2,}|\s*\.{2,}|\s*~+|\s*!{2,}|\s*\?{2,}|\s*;+)+\s*$')


def _clean_title(title):
    """(본문 제목, 태그 목록) — 확장자·ㅋㅋ·말머리를 떼고 태그는 따로 돌려준다."""
    tags = [m.group(1).strip() for m in BRACKET_RE.finditer(title)]
    t = BRACKET_RE.sub(' ', title)
    m = PREFIX_TAG_RE.match(t)
    if m:
        tags.append(m.group(1))
        t = t[m.end():]
    t = EXT_RE.sub(' ', t)
    t = JAMO_RE.sub(' ', t)
    return re.sub(r'\s+', ' ', t).strip(), tags


def _norm_title(t):
    """묶기용 제목 사본: 한자 약칭을 이름으로 푼다"""
    for rx, rep in HANJA_ALIAS:
        t = rx.sub(rep, t)
    return t


def _is_board_tag(w):
    return bool(DC_GALLERY_RE.match(w)) or w == '싱글벙글'


def _strip_suffix(w):
    if w in SUFFIX_KEEP:
        return w
    stem = SUFFIX_RE.sub('', w)
    if len(stem) >= 3 and stem[-1] in PARTICLES_ALWAYS:
        stem = stem[:-1]
    return stem


def _rule_tokens(body, tags):
    """[(표면, 정규화, 종류)] 종류: N(명사 후보) P(서술어·부사 추정) L(영문) D(숫자) B(게시판) S(불용)"""
    out = []
    for tg in tags:
        if _is_board_tag(tg):
            out.append((tg, tg, 'B'))
        elif tg.lower() in TAG_STOP or tg in TAG_STOP:
            out.append((tg, tg, 'S'))
        else:
            out.append((tg, tg, 'N'))   # 배그) 삼국지) 같은 주제 태그
    for raw in WORD_SPLIT_RE.split(body):
        for piece in PIECE_RE.findall(raw):
            if re.match(r'[A-Za-z]', piece):
                u = piece.upper()
                out.append((piece, u, 'S' if len(u) < 2 or u in LATIN_STOP else 'L'))
                continue
            if piece[0].isdigit():
                out.append((piece, piece, 'D'))
                continue
            w = piece
            if _is_board_tag(w):
                out.append((w, w, 'B'))
                continue
            stem = _strip_suffix(w)
            if len(stem) < 2 or w in STOP_WORDS or stem in STOP_WORDS or stem in ADV_STOP or w in ADV_STOP:
                out.append((w, stem, 'S'))
                continue
            if PRED_END_RE.search(w) or (len(w) >= 3 and w == stem and PRED_TAIL3_RE.fullmatch(w)
                                         and not w.endswith(('은', '는', '진', '한', '운', '인'))):
                out.append((w, stem, 'P'))
                continue
            out.append((w, stem, 'N'))
    return out


def _char_bigrams(s):
    s = re.sub(r'[^가-힣A-Za-z0-9]', '', s.lower())
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _jaccard(a, b):
    return len(a & b) / len(a | b) if a and b else 0.0


def _features(posts):
    """글마다 {kws: 잇는 데 쓰는 단어 집합, toks: 토큰, body: 정리한 제목, bi: 제목 글자 2-gram}"""
    cleaned = [_clean_title(p['title']) for p in posts]
    raw = [_rule_tokens(body, tags) for body, tags in cleaned]
    # 같은 라운드에 어근이 따로 있으면 '어근+1음절 조사'를 어근으로 본다 ('배그가' → '배그')
    vocab = {n for toks in raw for _, n, k in toks if k == 'N'}
    merge = {w: w[:-1] for w in vocab if len(w) >= 3 and w[-1] in PARTICLES and w[:-1] in vocab}
    feats = []
    for p, (body, _), rt in zip(posts, cleaned, raw):
        toks = []
        for s, n, k in rt:
            n = merge.get(n, n)
            if k == 'P' and s.endswith(NOUN_GO):
                k, n = 'N', s
            # '여자의' 같은 일반어+조사는 되돌린다
            if k == 'N' and len(n) >= 3 and n[-1] in PARTICLES and (n[:-1] in GENERIC or n[:-1] in STOP_WORDS):
                n = n[:-1]
            if k != 'B':
                toks.append((s, ALIAS.get(n, n), k))
        kws = {t[1] for t in toks if t[2] in ('N', 'L')}
        kws = {k for k in kws if len(k) >= 2 and not _is_board_tag(k) and k not in STOP_WORDS}
        kws |= set(NUM_UNIT_RE.findall(p['title']))
        feats.append({'kws': kws, 'toks': toks, 'body': body, 'bi': _char_bigrams(body)})
    # 합성어 쪼개기: '삼성냉장고' → + '삼성', '냉장고' (같은 라운드에 두 조각이 각각 2글 이상 나올 때만)
    df = Counter(k for f in feats for k in f['kws'])
    for f in feats:
        add = set()
        for k in f['kws']:
            if len(k) >= 4 and re.fullmatch(r'[가-힣]+', k):
                for cut in range(2, len(k) - 1):
                    a, b = k[:cut], k[cut:]
                    if df.get(a, 0) >= 2 and df.get(b, 0) >= 2 and a not in GENERIC and b not in GENERIC:
                        add |= {a, b}
                        break
        f['kws'] |= add
    return feats


# ── 묶기 ──────────────────────────────────────────────────────────────────────

class _UF:
    def __init__(self, n):
        self.p = list(range(n))

    def f(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def u(self, a, b):
        a, b = self.f(a), self.f(b)
        if a != b:
            self.p[max(a, b)] = min(a, b)


def _cluster(posts, feats):
    """→ (묶음 목록, 간선 {(i, j): (종류, 무게, 공유어)}, idf). 종류 S(구체어) W(공유어 2개 이상) R(퍼나르기)"""
    n = len(posts)
    df = Counter()
    for f in feats:
        df.update(f['kws'])
    idf = {k: math.log((n + 1) / (c + 0.5)) for k, c in df.items()}
    strong = {k for k, c in df.items() if c >= 2 and k not in GENERIC and k not in LINK_STOP and len(k) >= 2}

    # 글마다 대표 구체어(home) 하나에만 배정 → 같은 home끼리 잇는다 (사슬 방지)
    home = {}
    for i in range(n):
        ks = [k for k in feats[i]['kws'] if k in strong]
        word_ks = [k for k in ks if not NUM_RE.match(k)]
        pick = word_ks or ks                     # 숫자+단위는 다른 구체어가 없을 때만 home
        if pick:
            home[i] = max(pick, key=lambda k: (df[k], idf[k], k))
    groups = defaultdict(list)
    for i, k in home.items():
        groups[k].append(i)
    uf = _UF(n)
    edges = {}
    for k, g in groups.items():
        for a in range(len(g)):
            for b in range(a + 1, len(g)):
                i, j = g[a], g[b]
                if NUM_RE.match(k) and _jaccard(feats[i]['bi'], feats[j]['bi']) < 0.2:
                    continue                     # '30대' 하나로만 겹치는 서로 다른 글
                uf.u(i, j)
                edges[(i, j)] = ('S', idf[k], sorted(feats[i]['kws'] & feats[j]['kws']))
    home_uf = [uf.f(i) for i in range(n)]        # 합치기 전 상태 (응집도 가드용)

    # 두 home은 함께 나온 글이 2개 이상이고 작은 쪽 df의 30% 이상일 때만 합친다 (배그 + 방플)
    co = Counter()
    for i in range(n):
        ks = sorted(k for k in feats[i]['kws'] if k in strong and k in groups and not NUM_RE.match(k))
        for a in range(len(ks)):
            for b in range(a + 1, len(ks)):
                co[(ks[a], ks[b])] += 1
    for (a, b), c in co.items():
        if c >= 2 and c / min(df[a], df[b]) >= 0.3:
            uf.u(groups[a][0], groups[b][0])
    # 같은 글 퍼나르기
    for i in range(n):
        for j in range(i + 1, n):
            if _jaccard(feats[i]['bi'], feats[j]['bi']) >= DUP_J:
                uf.u(i, j)
                edges[(i, j)] = ('R', 99.0, sorted(feats[i]['kws'] & feats[j]['kws']))

    # 한 번만 붙이기(사슬 없음): 외톨이 글 → 한 묶음, 한 커뮤니티 묶음(위성) → 여러 커뮤니티 묶음
    comp = defaultdict(list)
    for i in range(n):
        comp[uf.f(i)].append(i)
    multi = {r: c for r, c in comp.items() if len(c) >= 2}
    # 1순위 단어(most_common)가 같은 횟수면 묶음 안에서 먼저 나온 글의 단어. 글 안 단어는 정렬해 넣어 실행마다 같게
    kw_of = {r: Counter(k for i in c for k in sorted(feats[i]['kws']) if k in strong) for r, c in multi.items()}
    attach = []
    for r0, c0 in comp.items():
        if len(c0) == 1:
            i = c0[0]
            ks = {k for k in feats[i]['kws'] if k in strong and not NUM_RE.match(k)}
            cands = []
            for r, kc in kw_of.items():
                sh = ks & set(kc)
                if not sh:
                    continue
                size = len(multi[r])
                if len(sh) >= 2 or (size >= 3 and any(kc[w] / size >= 0.5 for w in sh)):
                    cands.append((len(sh), sum(kc[w] for w in sh), r))
            cands.sort(reverse=True)
            if cands and (len(cands) == 1 or cands[0][:2] != cands[1][:2]):
                attach.append((i, cands[0][2]))
        elif len({posts[i]['source'] for i in c0}) == 1:
            if not kw_of.get(r0):
                continue
            top0 = kw_of[r0].most_common(1)[0][0]
            cands = []
            for r, kc in kw_of.items():
                if r == r0 or not kc or len({posts[i]['source'] for i in multi[r]}) < 2:
                    continue
                top = kc.most_common(1)[0][0]
                cooc = sum(1 for i in range(n) if top0 in feats[i]['kws'] and top in feats[i]['kws'])
                if cooc >= 1:
                    cands.append((cooc, len(multi[r]), r))
            cands.sort(reverse=True)
            if cands and (len(cands) == 1 or cands[0][0] > cands[1][0]):
                attach.append((c0[0], cands[0][2]))
    for i, r in attach:
        uf.u(i, r)

    comp = defaultdict(list)
    for i in range(n):
        comp[uf.f(i)].append(i)
    comps = []
    for c in comp.values():
        # 응집도 가드: 너무 크거나 넓은데 1순위 단어가 절반을 못 덮으면 합치기 전(home) 묶음으로 되돌린다
        if len(c) > BIG_N or len({posts[i]['source'] for i in c}) > BIG_C:
            cov = Counter(k for i in c for k in feats[i]['kws'] if k in strong)
            if not cov or max(cov.values()) / len(c) < COHESION:
                sub = defaultdict(list)
                for i in c:
                    sub[home_uf[i]].append(i)
                comps.extend(sub.values())
                continue
        comps.append(c)
    # 같은 묶음 안 나머지 글 쌍의 간선 (퍼나르기만으로 이어졌나, 공유어 2개 간선이 있나 판정용)
    for c in comps:
        for a in range(len(c)):
            for b in range(a + 1, len(c)):
                i, j = min(c[a], c[b]), max(c[a], c[b])
                if (i, j) in edges:
                    continue
                sh = feats[i]['kws'] & feats[j]['kws']
                if sh:
                    edges[(i, j)] = ('W' if len(sh) >= 2 else 'S', sum(idf[k] for k in sh), sorted(sh))
    return comps, edges, idf


def _uf_single(comp, pairs):
    """comp 안에서 pairs 간선만으로 전부 이어지나"""
    par = {i: i for i in comp}

    def f(x):
        while par[x] != x:
            par[x] = par[par[x]]
            x = par[x]
        return x
    for a, b in pairs:
        par[f(a)] = f(b)
    return len({f(i) for i in comp}) == 1


# ── 이름 ──────────────────────────────────────────────────────────────────────

def _phrase(comp, feats, idf):
    """묶음 안 글 2개 이상에 나온 인접 명사 2-gram을 겹치는 것끼리 최대 4단어까지 잇는다"""
    bg = Counter()
    for i in comp:
        seq = feats[i]['toks']
        # 글마다 한 번씩 센다. 집합 대신 제목 순서를 지킨 중복 제거라 Counter 순서 = 묶음 안에서 처음 나온 순서
        bg.update(list(dict.fromkeys((a[1], b[1]) for a, b in zip(seq, seq[1:])
                                     if a[2] in ('N', 'L') and b[2] in ('N', 'L') and a[1] != b[1])))
    cand = {g: c for g, c in bg.items() if c >= 2 and not all(w in GENERIC for w in g)}
    if not cand:
        return []
    # 점수가 같으면 묶음 안에서 먼저 나온 구절 (max는 같은 값 중 처음 것을 고른다)
    best = max(cand, key=lambda g: (cand[g], sum(idf.get(w, 0) for w in g)))
    chain = list(best)
    for _ in range(2):
        opts = [(cand[g], 1, g) for g in cand if g[0] == chain[-1] and g[1] not in chain] + \
               [(cand[g], 0, g) for g in cand if g[1] == chain[0] and g[0] not in chain]
        if not opts:
            break
        _, right, g = max(opts)
        chain = chain + [g[1]] if right else [g[0]] + chain
    return chain


def _mean_pos(word, comp, feats):
    """제목 안에서 그 단어가 나오는 상대 위치 평균 (이름 단어 순서용)"""
    pos = []
    for i in comp:
        toks = feats[i]['toks']
        for j, t in enumerate(toks):
            if t[1] == word:
                pos.append(j / max(1, len(toks) - 1))
                break
    return sum(pos) / len(pos) if pos else 1.0


def _event(comp, feats, exclude):
    """묶음 안 2글 이상 제목에 든 사건 명사 (이름 끝에 붙인다)"""
    cnt = Counter()
    for i in comp:
        body = feats[i]['body']
        cnt.update({e for e in NAME_EVENTS if e in body})
    ok = [e for e in NAME_EVENTS if cnt[e] >= 2 and e not in exclude]
    return max(ok, key=lambda e: (cnt[e], -NAME_EVENTS.index(e))) if ok else None


def _surface(word, comp, feats):
    """정규화형(스캔) → 묶음 안에서 실제로 쓰인 형태(스캔들). 정규화형 그대로 쓰인 적 있으면 그대로"""
    forms = Counter(s for i in comp for s, n, k in feats[i]['toks'] if n == word)
    if not forms or word in forms:
        return word
    f = forms.most_common(1)[0][0]
    while len(f) > len(word) and f[-1] in CASE_P:
        f = f[:-1]
    return f if f.startswith(word) else word


def _clean_word(w):
    """이름에 쓸 때 끝 조사 떼기: 스태프가 → 스태프 (로·도·만·과·와는 명사 끝일 수 있어 둔다: 마운자로)"""
    return w[:-1] if len(w) >= 3 and w[-1] in NAME_PARTICLE else w


def _display_title(t):
    """확장자·끝의 ㅋㅋ/ㄷㄷ/…… 를 뗀 제목"""
    t = EXT_RE.sub('', t)
    return TRAIL_RE.sub('', t).strip() or t


def _clean_headline(title, maxlen):
    """퍼진 글 이름: 따옴표·끝 ㅋㅋ·확장자를 뗀 제목, maxlen 넘으면 단어 경계에서 자른다"""
    body, _ = _clean_title(title)
    body = re.sub(r'[“”"‘’\']', '', body)
    body = re.sub(r'\s*(ㄷ|ㅋ|ㅎ|;|~|!|\?|\.)+\s*$', '', body).strip(' .…')
    if len(body) <= maxlen:
        return body
    cut = body[:maxlen]
    if ' ' in cut[8:]:
        cut = cut[:cut.rfind(' ')]
    return cut.rstrip(' ,.…') + '…'


# ── 대표 글 · 시각 ────────────────────────────────────────────────────────────

def _top_safe(p):
    return not UNSAFE_RE.search(p['title'])


def _community_context(posts):
    """글마다 (커뮤니티 안 순위, 커뮤니티 안 댓글 백분위). rank_score는 커뮤니티 안 상대 점수라 순위가 비교 가능하다."""
    by = defaultdict(list)
    for i, p in enumerate(posts):
        by[p['source']].append(i)
    pos, cpct = {}, {}
    for idx in by.values():
        for k, i in enumerate(sorted(idx, key=lambda i: -posts[i]['rank_score']), 1):
            pos[i] = k
        cm = sorted(posts[i].get('comments', 0) for i in idx)
        for i in idx:
            c = posts[i].get('comments', 0)
            cpct[i] = sum(1 for v in cm if v <= c) / len(cm)
    return pos, cpct


def _explain_score(i, posts, feats, cov, n, core, cpct, name):
    """'왜' 한 줄 점수: 이야기를 가장 잘 설명하는 제목 (핵심어 커버리지 + 정보량 + 반응 약간)"""
    f = feats[i]['kws']
    title = posts[i]['title']
    ev_bonus = 0.4 if any(e in name and e in title for e in WHY_EVENTS) else 0
    unsafe = 0 if not LEWD_RE.search(title) else 2
    cover = sum(cov[k] / n for k in core if k in f)          # 이야기 핵심어를 몇 개 품었나
    info = min(len(_clean_title(title)[0]), 36) / 36          # 짧은 밈 제목보다 정보가 많은 제목
    meme = 1.0 if MEME_RE.search(title) else 0
    distinct = len(f & set(core))
    return (cover + 0.35 * max(0, distinct - 1) + info * 0.6 + cpct[i] * 0.2 + posts[i]['rank_score'] / 400
            - meme + ev_bonus - unsafe)


def _parse_dt(s):
    """작성 시각. 날짜만 있는 글(네이트판·인스티즈)은 None — 시각 판정에 쓰지 않는다"""
    if not s or 'T' not in s:
        return None
    try:
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=KST)
    except ValueError:
        return None


def _iso(dt):
    return dt.isoformat(timespec='minutes') if dt else None


# ── 본체 ──────────────────────────────────────────────────────────────────────

def build_issues(posts: list, ps_history: list, now: datetime) -> dict:
    """trends.json의 issues 블록. 글은 posts[].rank로 가리킨다.
    전날 보충 글(prev_day: KST 02~12시에 당일 글이 모자란 칸을 채운 '어제' 글)은 보드 전체(묶기·신규·급상승·+N·
    여기서만 뜨거운 글)에서 뺀다 — 보드의 글·커뮤니티 수와 low_data도 당일 글 기준."""
    rounds = []                                   # 서로 다른 라운드 (같은 글 목록이 이어지면 하나로)
    for r in ps_history or []:
        if not rounds or set(r) != set(rounds[-1]):
            rounds.append(r)
    L = len(rounds)
    posts_all = [p for p in posts if not p.get('prev_day')]
    posts = [p for p in posts_all if not DEAL_RE.search(p['title'])]
    cposts = [dict(p, title=_norm_title(p['title'])) for p in posts]   # 묶기용 사본
    feats = _features(cposts)
    comps, edges, idf = _cluster(cposts, feats)
    pos, cpct = _community_context(posts)
    keys = [_url_key(p['url']) for p in posts]
    first_round = {}
    for r, rnd in enumerate(rounds):
        for k in rnd:
            first_round.setdefault(k, r)
    prev_keys = set(rounds[-2]) if L >= 3 else None   # 이력 3라운드 미만이면 +N(새 글)을 끈다
    recent_cut = now - timedelta(hours=RECENT_H)

    issues = []
    for comp in comps:
        if len(comp) < 2:
            continue
        cs = set(comp)
        ps = [posts[i] for i in comp]
        n = len(comp)
        src_cnt = Counter(p['source'] for p in ps)
        in_edges = {e: v for e, v in edges.items() if e[0] in cs and e[1] in cs}
        cov = Counter()
        for i in comp:
            cov.update(feats[i]['kws'])
        core = sorted([k for k, c in cov.items() if c >= 2 and k not in ADV_STOP and not k[0].isdigit()
                       and not PROFANITY.search(k) and k not in LINK_STOP],
                      key=lambda k: (k in GENERIC, -cov[k], -idf.get(k, 0), k))
        specific = [k for k in core if k not in GENERIC]
        chain = _phrase(comp, feats, idf)

        # 종류 — 퍼진 글(퍼나르기만으로 이어짐) / 주제(한 핵심어 아래 여러 이야기) / 이슈
        # '사건 구절'은 구체어끼리 붙은 2-gram만 센다 ('이재명 대통령' 같은 인물+직함은 사건 구절이 아님)
        bg = Counter()
        for i in comp:
            seq = feats[i]['toks']
            bg.update({(a[1], b[1]) for a, b in zip(seq, seq[1:]) if a[2] in ('N', 'L') and b[2] in ('N', 'L')
                       and a[1] != b[1] and a[1] not in GENERIC and b[1] not in GENERIC})
        story_df = max(bg.values(), default=0)
        if _uf_single(comp, [e for e, v in in_edges.items() if v[0] == 'R']):
            kind = 'repost'
        elif (n >= 5 and specific and cov[specific[0]] / n >= 0.6 and story_df < 3
              and (len(specific) < 2 or cov[specific[1]] / n <= 0.4)):
            kind = 'topic'
        else:
            kind = 'story'

        # 이름
        words = []
        if kind == 'topic':
            # 주제는 핵심어 하나 (+직함 같은 일반어 구절만 허용: '이재명 대통령')
            words = ([w for w in chain if w not in LINK_STOP]
                     if chain and specific[0] in chain and all(w == specific[0] or w in GENERIC for w in chain)
                     else [specific[0]])
        elif kind == 'story':
            if chain:
                words = [w for w in chain if w not in LINK_STOP]
                if specific and specific[0] not in words and cov[specific[0]] / n >= 0.4:
                    words.insert(0, specific[0])
            elif specific:
                words = specific[:2]
                words.sort(key=lambda w: _mean_pos(w, comp, feats))
            if words:
                ev = _event(comp, feats, set(words))
                if ev and len(words) < 4:
                    words.append(ev)
        words = [w for w in words if not PRED_NAME_RE.search(w)]
        name = ' '.join(_clean_word(_surface(w, comp, feats)) for w in words)

        # 대표 글 — 설명력 점수순, 커뮤니티당 1개, 퍼나르기 중복·선정적 제목(2번째부터) 제외
        def why_score(i):
            s = _explain_score(i, posts, feats, cov, n, core[:4], cpct, name)
            return s - (3 if not _top_safe(posts[i]) else 0)
        reps, used = [], set()
        for i in sorted(comp, key=lambda i: -why_score(i)):
            if posts[i]['source'] in used or any(_jaccard(feats[i]['bi'], feats[j]['bi']) >= DUP_J for j in reps):
                continue
            if reps and not _top_safe(posts[i]):
                continue
            reps.append(i)
            used.add(posts[i]['source'])
            if len(reps) == 3:
                break
        head = posts[reps[0]]
        if not name:
            name = _clean_headline(head['title'], 30) if kind == 'repost' else _display_title(head['title'])

        # 시각 — since는 두 번째로 이른 글 (첫 글이 엉뚱하게 오래된 경우를 피한다)
        times = sorted(t for t in (_parse_dt(p.get('date')) for p in ps) if t)
        since = times[1] if len(times) >= 2 else None
        first = times[0] if times else None
        last = times[-1] if times else None
        recent = sum(1 for t in times if t >= recent_cut)
        grew = sum(1 for i in comp if keys[i] not in prev_keys) if prev_keys is not None else None

        series_n, series_c = [], []
        for rnd in rounds:
            present = [posts[i]['source'] for i in comp if keys[i] in rnd]
            series_n.append(len(present))
            series_c.append(len(set(present)))
        # 신선도: 최근 2라운드에 처음 보인 글 비율
        fresh = (sum(1 for i in comp if first_round.get(keys[i], L) >= L - 2) / n) if L >= 3 else None

        status, why = 'steady', ''
        if since and since >= recent_cut:
            status, why = 'new', f'최근 {RECENT_H}시간 안에 여러 곳에 올라오기 시작'
        elif L >= 4:
            n0, n1, c0, c1 = series_n[-4], series_n[-1], series_c[-4], series_c[-1]
            if n1 - n0 >= 3 or (n1 - n0 >= 2 and n0 and n1 / n0 >= 1.5) or c1 - c0 >= 2:
                status, why = 'rising', f'최근 3번 갱신 동안 글 {n0}→{n1}개, 커뮤니티 {c0}→{c1}곳'
        if status == 'steady' and recent >= 2 and recent / n >= 0.25:
            status, why = 'rising', f'최근 {RECENT_H}시간에 새 글 {recent}개'
        if status == 'steady' and last and (now - last) > timedelta(hours=8) and len(times) >= 2:
            status, why = 'quiet', f'마지막 글 {int((now - last).total_seconds() // 3600)}시간 전'

        # 순위 — 퍼짐(커뮤니티별 최고 글 + 같은 곳 추가 글은 로그) × 신선도
        best = defaultdict(float)
        for p in ps:
            best[p['source']] = max(best[p['source']], p['rank_score'])
        spread = sum(best.values()) / 100 + sum(0.5 * math.log1p(c - 1) for c in src_cnt.values())
        if kind == 'repost':
            heat = spread * 0.8                      # 퍼나르기는 신선도 가산 없음 (밈이 실제 사건 위로 오르지 않게)
        else:
            heat = spread * (0.7 + 0.6 * (fresh if fresh is not None else 0.5))
            if kind == 'topic':
                heat *= 0.75                         # 매일 있는 배경 주제는 사건보다 한 칸 낮게
        if status == 'quiet':
            heat *= 0.8

        # 보드에 실을 만한가 — 제목만으로 맞는지 가릴 수 없는 작은 묶음은 뺀다
        shared2 = any(len(v[2]) >= 2 and any(k not in GENERIC for k in v[2]) for v in in_edges.values())
        conf = (n >= 4 and len(src_cnt) >= 2) or kind == 'repost' or shared2
        if kind != 'repost' and not specific:
            conf = False
        if len(src_cnt) == 1:
            # 한 커뮤니티 안 이야기는 '퍼진 이슈'가 아니다. 퍼나르기도 마찬가지 (참조 구현은 한 곳 4글 이상 퍼나르기를
            # 1곳짜리 '퍼진 글'로 올렸다 — 연재물·경기 중계처럼 제목이 비슷한 글이 한 베스트에 몰린 경우)
            conf = False

        # 커뮤니티별 온도(상세 패널): [커뮤니티, 글 수, 그 커뮤니티 안 최고 순위, 댓글]
        sp = []
        for s, cnt in src_cnt.most_common():
            idx = [i for i in comp if posts[i]['source'] == s]
            b = min(idx, key=lambda i: pos[i])
            sp.append([s, cnt, pos[b], sum(posts[i].get('comments', 0) for i in idx)])
        # id: 가장 먼저 보인 글(첫 라운드, 같으면 url_key 사전순) — 갱신 사이에 펼침·필터를 유지하는 데 쓴다
        anchor = min((keys[i] for i in comp), key=lambda k: (first_round.get(k, 99), k))
        kw = [] if kind == 'repost' else [k for k in core if k not in set(words) and k not in GENERIC][:4]
        issues.append((conf, round(heat, 2), {
            'id': hashlib.sha1(anchor.encode()).hexdigest()[:6], 'name': name, 'kind': kind,
            'status': status, 'status_why': why, 'n': n, 'c': len(src_cnt),
            'cm': sum(p.get('comments', 0) for p in ps), 'grew': grew,
            'first': _iso(first), 'since': _iso(since), 'last': _iso(last),
            'why': head['rank'], 'reps': [posts[i]['rank'] for i in reps],
            'sp': sp, 'ranks': sorted(p['rank'] for p in ps), 'kw': kw,
        }))
    issues.sort(key=lambda d: (not d[0], -d[1]))

    top = [d for conf, _, d in issues if conf][:MAX_ITEMS]
    in_any = {r for conf, _, d in issues if conf for r in d['ranks']}
    # 여기서만 뜨거운 글: 보드 이슈에 안 든 커뮤니티 안 1~2위 글, 댓글 백분위순, 커뮤니티당 1개
    hot = sorted([i for i in range(len(posts)) if posts[i]['rank'] not in in_any and pos[i] <= 2 and _top_safe(posts[i])],
                 key=lambda i: -(cpct[i] + (1 if pos[i] == 1 else 0) + posts[i].get('comments', 0) / 1000))
    hot_ranks, seen_src = [], set()
    for i in hot:
        if posts[i]['source'] in seen_src:
            continue
        hot_ranks.append(posts[i]['rank'])
        seen_src.add(posts[i]['source'])
        if len(hot_ranks) == HOT_MAX:
            break
    return {
        'as_of': now.isoformat(timespec='minutes'), 'posts': len(posts_all),
        'communities': len({p['source'] for p in posts_all}),
        'in_issues': len({r for d in top for r in d['ranks']}),
        'low_data': len(posts_all) < LOW_DATA_POSTS, 'badges': L >= 3,
        'items': top, 'hot': hot_ranks,
    }
