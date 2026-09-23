"""
Daily deep-summary module.

Persists one Gemini-generated narrative per calendar day (KST) as a JSON file
in the repo (data/daily/YYYY-MM-DD.json). Each summary ingests post titles +
scraped bodies so the reader understands each story without having to visit
the original posts.
"""

from __future__ import annotations

import os
import re
import json
import tempfile
from collections import Counter
from datetime import datetime, timezone, timedelta

import gemini

# .env를 먼저 읽는다 — build.py가 load_dotenv()보다 이 모듈을 먼저 import해도 DAILY_DIR이 반영되도록 (BEX-2)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))
except ImportError:
    pass

# ── 저장 위치 ─────────────────────────────────────────────────────────────────
# DAILY_DIR 환경변수로 덮어쓸 수 있다 (빈 값이면 기본값). 리포에 커밋되는 디렉터리.
DAILY_DIR = (os.environ.get('DAILY_DIR', '').strip()
             or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'daily'))

KST = timezone(timedelta(hours=9))

SAVE_MAX_POSTS = 50   # 리포트에 같이 저장하는 원문 글 수
# 저장하는 글 필드 (데일리 페이지가 쓰는 것만)
_POST_FIELDS = ('title', 'url', 'source', 'source_label', 'source_emoji', 'source_color',
                'views', 'likes', 'comments', 'date', 'rank')

_DATE_RE = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}')


def kst_today() -> str:
    """Return today's date in KST as YYYY-MM-DD."""
    return datetime.now(KST).strftime('%Y-%m-%d')


def is_valid_date(date: str) -> bool:
    """엄격한 YYYY-MM-DD(끝 개행·유니코드 숫자 불허) + 실제 달력 날짜인지."""
    if not isinstance(date, str) or not _DATE_RE.fullmatch(date):
        return False
    try:
        datetime.strptime(date, '%Y-%m-%d')
        return True
    except ValueError:
        return False


# ── JSON 파일 저장소 ──────────────────────────────────────────────────────────

def _path(date: str) -> str:
    return os.path.join(DAILY_DIR, f'{date}.json')


def _write_json_atomic(path: str, data: dict) -> None:
    """같은 디렉터리에 임시 파일로 쓴 뒤 replace — 중간에 죽어도 반쯤 쓴 파일이 남지 않는다."""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix='.tmp-', suffix='.json')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _read_json(date: str) -> dict | None:
    try:
        with open(_path(date), encoding='utf-8') as f:
            d = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        print(f'[Daily] {date}.json 읽기 실패: {e}', flush=True)
        return None
    return d if isinstance(d, dict) else None


def get_summary(date: str) -> dict | None:
    """{date, summary_md, posts, post_count, analyzed_count, generated_at, model} 또는 None."""
    if not is_valid_date(date):
        return None
    d = _read_json(date)
    if not d or not d.get('summary_md'):
        return None
    posts = d.get('posts') if isinstance(d.get('posts'), list) else []
    analyzed = d.get('analyzed_count')
    return {
        'date': date,                     # 파일명이 기준
        'summary_md': d['summary_md'],
        'posts': posts,
        'post_count': d.get('post_count', len(posts)),
        # 모델에 넘긴 글 수. 옛 리포트(이전 도구로 옮긴 것)에는 없다 → None
        'analyzed_count': analyzed if isinstance(analyzed, int) and analyzed > 0 else None,
        'generated_at': d.get('generated_at', ''),
        'model': d.get('model', ''),
    }


def _trim_posts(posts: list) -> list:
    """상위 SAVE_MAX_POSTS개, 필요한 필드만. http(s) 링크가 아닌 글은 뺀다."""
    out = []
    for p in posts or []:
        if not isinstance(p, dict):
            continue
        url = str(p.get('url') or '')
        if not url.startswith(('http://', 'https://')):
            continue
        out.append({k: p[k] for k in _POST_FIELDS if k in p})
        if len(out) >= SAVE_MAX_POSTS:
            break
    return out


def save_summary(date: str, summary_md: str, posts: list, *,
                 generated_at: str | None = None, model: str | None = None) -> str:
    """
    리포트를 DAILY_DIR/YYYY-MM-DD.json에 저장(덮어쓰기)하고 파일 경로를 반환한다.
    generated_at은 생략하면 지금 시각(KST).
    model은 생략하면 마지막 생성에 쓴 모델(gemini.last_model — 예비 모델일 수 있음). 옛 DB 이전 도구만 넘긴다.
    model을 생략한 경우(방금 생성한 리포트)에만 analyzed_count(모델에 넘긴 글 수)를 같이 저장한다.
    """
    if not is_valid_date(date):
        raise ValueError(f'잘못된 날짜: {date!r}')
    if date > kst_today():
        raise ValueError(f'미래 날짜는 저장하지 않음: {date}')
    if not (summary_md or '').strip():
        raise ValueError('빈 요약은 저장하지 않음')

    saved_posts = _trim_posts(posts)
    data = {
        'date': date,
        'generated_at': generated_at or datetime.now(KST).isoformat(timespec='seconds'),
        'model': (gemini.last_model or gemini.MODEL) if model is None else model,
        'post_count': len(saved_posts),   # 같이 저장한 상위 글 수 (최대 SAVE_MAX_POSTS)
        'summary_md': summary_md.strip(),
        'posts': saved_posts,
    }
    if model is None:
        # 방금 생성한 리포트 — 모델에 실제로 넘긴 글 수 (generate_deep_summary와 같은 입력·같은 선정)
        data['analyzed_count'] = len(_select_posts(posts or [], date))
    path = _path(date)
    _write_json_atomic(path, data)
    return path


def list_summaries(limit: int | None = None) -> list[dict]:
    """최신순 [{date, post_count, generated_at}]."""
    try:
        names = os.listdir(DAILY_DIR)
    except FileNotFoundError:
        return []
    dates = sorted((n[:-5] for n in names if n.endswith('.json') and is_valid_date(n[:-5])),
                   reverse=True)
    out = []
    for date in dates:
        d = _read_json(date)
        if not d or not d.get('summary_md'):
            continue
        posts = d.get('posts') if isinstance(d.get('posts'), list) else []
        out.append({
            'date': date,
            'post_count': d.get('post_count', len(posts)),
            'generated_at': d.get('generated_at', ''),
        })
        if limit is not None and len(out) >= limit:
            break
    return out


def has_summary(date: str) -> bool:
    # 파일이 깨져 있으면 없는 것으로 본다 → 다음 실행에서 다시 생성해 덮어쓴다
    return get_summary(date) is not None


# ── Gemini deep summary ───────────────────────────────────────────────────────

DAILY_BASE_N         = 10    # 기본 포함 글 수
DAILY_MAX_N          = 20    # 최대 포함 글 수
DAILY_MAX_PER_SOURCE = 4     # 한 커뮤니티가 차지할 수 있는 최대 슬롯 (과점 방지)
BODY_MAX_CHARS       = 400   # 본문 최대 길이
VELOCITY_THRESHOLD   = 20.0  # rank_score 급등 기준


def _select_posts(posts: list, date: str) -> list:
    """
    1. 오늘/어제 날짜 글 + velocity 급등 글을 후보로 추린다. 후보가 10개 미만이면 전체 글로 fallback.
    2. 상위 DAILY_BASE_N개를 랭킹 순으로 먼저 선택한다.
    3. 나머지 후보 중 rank_score가 높은 글을 DAILY_MAX_N까지 추가한다.
       rank_score는 소스별로 정규화된 점수라 소스 간 원시 조회수를 비교하지 않는다
       (FMK 조회수는 TBS가 채운 합성값이라 원시 합산하면 FMK가 11~20위를 독차지했다).
    2·3 모두 소스당 DAILY_MAX_PER_SOURCE개 상한을 둔다 — FMK는 TBS 목록 앞쪽이라
    position_score가 높아 rank_score만으로도 몰린다. 소스가 적어 못 채우면 상한 없이 채운다.
    """
    try:
        target    = datetime.strptime(date, '%Y-%m-%d')
        yesterday = (target - timedelta(days=1)).strftime('%Y-%m-%d')
    except ValueError:
        yesterday = ''

    by_rank = sorted(posts, key=lambda p: p.get('rank') or 9999)

    candidates = []
    for p in by_rank:
        post_date = (p.get('date') or '')[:10]   # 계약 형식(KST 날짜 또는 +09:00 시각)의 앞 10자
        is_recent = bool(post_date) and post_date in (date, yesterday)
        is_viral  = (p.get('post_velocity') or 0.0) >= VELOCITY_THRESHOLD
        if is_recent or is_viral:
            p = dict(p)
            p['_viral'] = is_viral and not is_recent
            candidates.append(p)

    if len(candidates) < 10:
        candidates = [dict(p, _viral=False) for p in by_rank]

    by_score = sorted(candidates, key=lambda p: p.get('rank_score') or 0.0, reverse=True)
    selected: list = []
    taken: set = set()
    per_src: Counter = Counter()

    def pick(pool: list, limit: int, cap: int | None) -> None:
        for p in pool:
            if len(selected) >= limit:
                return
            if id(p) in taken or (cap is not None and per_src[p.get('source')] >= cap):
                continue
            selected.append(p)
            taken.add(id(p))
            per_src[p.get('source')] += 1

    pick(candidates, DAILY_BASE_N, DAILY_MAX_PER_SOURCE)   # 기본 N개 (랭킹 순)
    pick(by_score, DAILY_MAX_N, DAILY_MAX_PER_SOURCE)      # 나머지 (rank_score 순)
    pick(by_score, DAILY_MAX_N, None)                      # 소스가 적어 못 채웠으면 상한 없이
    return selected


def _format_posts_for_prompt(posts: list) -> str:
    lines = []
    for i, p in enumerate(posts, 1):
        source = p.get('source_label') or p.get('source', '')
        title  = (p.get('title') or '').strip()
        body   = (p.get('summary') or '').strip()
        likes  = p.get('likes') or 0
        cmts   = p.get('comments') or 0

        if body and len(body) > BODY_MAX_CHARS:
            body = body[:BODY_MAX_CHARS] + '…'

        # 조회수는 넣지 않는다 — 소스마다 단위가 다르고 FMK 값은 합성이라 사실 기반 원칙에 어긋난다
        stat_parts = []
        if likes:  stat_parts.append(f'공감 {likes:,}')
        if cmts:   stat_parts.append(f'댓글 {cmts:,}')
        stats = ' · '.join(stat_parts)

        viral_tag = ' 🔥급상승' if p.get('_viral') else ''
        lines.append(f'### [{i}위] {title}  ({source}){viral_tag}')
        if stats:
            lines.append(stats)
        if body:
            lines.append(f'본문: {body}')
        lines.append('')

    return '\n'.join(lines)


def generate_deep_summary(posts: list, date: str | None = None) -> str:
    """
    Call Gemini to produce a deep, narrative daily summary.
    Returns the markdown string, or '' on failure (원인은 로그로 남긴다).
    """
    if not gemini.available():
        print('[Daily] GOOGLE_API_KEY 없음 - 데일리 생성 건너뜀', flush=True)
        return ''

    if not date:
        date = kst_today()
    if not is_valid_date(date):
        print(f'[Daily] 잘못된 날짜 {date!r} - 데일리 생성 건너뜀', flush=True)
        return ''

    ranked = _select_posts(posts or [], date)
    if not ranked:
        print(f'[Daily] {date} 선정된 글 없음 - 데일리 생성 건너뜀', flush=True)
        return ''

    posts_text = _format_posts_for_prompt(ranked)
    count = len(ranked)

    # 입력은 대부분 제목뿐이다(본문은 일부 글만). 분량·'반응'을 요구하면 모델이 반응·배경·소속을 지어낸다
    # (09-23 초안에서 여야가 뒤바뀐 사례) → 제공된 사실만, 정보가 적으면 짧게 쓰게 한다.
    prompt = f"""당신은 한국 인터넷 커뮤니티 트렌드 요약 편집자입니다.
오늘({date}) 주요 커뮤니티에서 화제가 된 게시글들을 정리해주세요.
아래 글은 대부분 제목만 있고, 일부에만 본문 앞부분이 있습니다.

## 작성 원칙
1. **제공된 사실만**: 아래 제목·본문·공감/댓글 수에 있는 내용만 쓸 것. 반응·평가·결과·배경·원인은 본문에 적혀 있을 때만 쓰고, 제목만 있는 글은 제목이 말하는 사실만 전할 것. 추측·창작 금지.
2. **인물·단체 정보 금지**: 실존 인물·정당·단체의 소속·직함·성향(여당/야당 등)을 덧붙이지 말 것. 이름과 제목의 표현만 쓸 것.
3. **반응 묘사 금지**: '비판이 쏟아졌다', '논쟁이 이어졌다', '관심을 모았다', '화제가 됐다' 같은 반응·분위기 서술은 본문에 그런 내용이 있을 때만. 조회수는 언급하지 말 것.
4. **관련 글 통합**: 비슷한 주제는 하나의 섹션으로 묶어 서술. 이미 한 말 반복 금지.
5. **형식**: `## 섹션 제목` 으로 주제별 구분. 섹션 수 5-8개. 섹션당 1-4문장 — 제목뿐인 글은 한 문장이면 충분하다. 분량을 맞추려고 내용을 보태지 말 것.
6. **뒤 섹션도 같은 기준**: 앞 섹션만 자세히 쓰고 뒤로 갈수록 생략하지 말 것. 모든 섹션을 정보량에 맞게 같은 기준으로 서술.
7. 커뮤니티 이름 나열 금지. 🔥급상승 표시된 글은 섹션 제목에 "🔥" 표시.
8. 불필요한 서론·마무리 인사·감상 금지. 이슈 설명만.

## 오늘의 주요 게시글 ({count}개)

{posts_text}
"""

    # 3.x는 thinking 토큰도 출력 한도에 포함된다 → 잘림(MAX_TOKENS) 방지용 여유
    text = gemini.generate(prompt, max_output_tokens=16384)
    if text:
        print(f'[Daily] {date} 깊은 요약 생성 완료 ({len(text)}자, {gemini.last_model or gemini.MODEL}, 글 {count}개)',
              flush=True)
    else:
        print(f'[Daily] {date} 깊은 요약 생성 실패 ({gemini.MODEL}) - 위 [Gemini] 로그 참고', flush=True)
    return text
