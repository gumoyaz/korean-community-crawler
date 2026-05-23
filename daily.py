"""
Daily deep-summary module.

Persists one Gemini-generated narrative per calendar day (KST) to SQLite.
Each summary ingests post titles + scraped bodies so the reader understands
each story without having to visit the original posts.
"""

import os
import sqlite3
import json
import requests
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta

# ── DB path ──────────────────────────────────────────────────────────────────
# Override with DAILY_DB_PATH env var (e.g., a Railway volume mount).
_DEFAULT_DB_DIR = os.path.join(os.path.dirname(__file__), 'data')
DB_PATH = os.environ.get(
    'DAILY_DB_PATH',
    os.path.join(_DEFAULT_DB_DIR, 'daily.db'),
)

KST = timezone(timedelta(hours=9))


def kst_today() -> str:
    """Return today's date in KST as YYYY-MM-DD."""
    return datetime.now(KST).strftime('%Y-%m-%d')


# ── SQLite helpers ────────────────────────────────────────────────────────────

@contextmanager
def _db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with _db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_summaries (
                date         TEXT PRIMARY KEY,
                summary_md   TEXT NOT NULL,
                posts_json   TEXT NOT NULL DEFAULT '[]',
                post_count   INTEGER NOT NULL DEFAULT 0,
                generated_at TEXT NOT NULL
            )
        """)


# ── CRUD ──────────────────────────────────────────────────────────────────────

def get_summary(date: str) -> dict | None:
    with _db() as conn:
        row = conn.execute(
            'SELECT * FROM daily_summaries WHERE date = ?', (date,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d['posts'] = json.loads(d.pop('posts_json', '[]'))
        return d


def save_summary(date: str, summary_md: str, posts: list):
    now_iso = datetime.now(KST).isoformat()
    with _db() as conn:
        conn.execute("""
            INSERT INTO daily_summaries (date, summary_md, posts_json, post_count, generated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                summary_md   = excluded.summary_md,
                posts_json   = excluded.posts_json,
                post_count   = excluded.post_count,
                generated_at = excluded.generated_at
        """, (date, summary_md, json.dumps(posts, ensure_ascii=False),
              len(posts), now_iso))


def list_summaries(limit: int = 60) -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            'SELECT date, post_count, generated_at FROM daily_summaries '
            'ORDER BY date DESC LIMIT ?', (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def has_summary(date: str) -> bool:
    with _db() as conn:
        row = conn.execute(
            'SELECT 1 FROM daily_summaries WHERE date = ?', (date,)
        ).fetchone()
        return row is not None


# ── Gemini deep summary ───────────────────────────────────────────────────────

DAILY_BASE_N       = 10    # 기본 포함 글 수
DAILY_MAX_N        = 20    # 최대 포함 글 수
BODY_MAX_CHARS     = 400   # 본문 최대 길이
VELOCITY_THRESHOLD = 20.0  # rank_score 급등 기준


def _engagement_score(p: dict) -> float:
    """조회수·추천수·댓글 수를 하나의 점수로 합산 (단위 통일을 위해 가중치 적용)."""
    return (p.get('views', 0) * 1.0
            + p.get('likes', 0) * 10.0
            + p.get('comments', 0) * 5.0)


def _select_posts(posts: list, date: str) -> list:
    """
    1. 오늘/어제 날짜 글 + velocity 급등 글을 후보로 추린다.
    2. 상위 DAILY_BASE_N개를 랭킹 순으로 먼저 선택한다.
    3. 나머지 후보 중 조회수·추천수 합산이 높은 글을 DAILY_MAX_N까지 추가한다.
    4. 후보가 10개 미만이면 전체 상위 글로 fallback.
    """
    try:
        target    = datetime.strptime(date, '%Y-%m-%d').replace(tzinfo=KST)
        yesterday = (target - timedelta(days=1)).strftime('%Y-%m-%d')
    except ValueError:
        yesterday = ''

    candidates = []
    for p in sorted(posts, key=lambda p: p.get('rank', 9999)):
        post_date = (p.get('date') or '')[:10]
        is_recent = post_date in (date, yesterday)
        is_viral  = p.get('post_velocity', 0.0) >= VELOCITY_THRESHOLD
        if is_recent or is_viral:
            p = dict(p)
            p['_viral'] = is_viral and not is_recent
            candidates.append(p)

    if len(candidates) < 10:
        candidates = [dict(p) for p in sorted(posts, key=lambda p: p.get('rank', 9999))]
        for p in candidates:
            p['_viral'] = False

    # 기본 N개 (랭킹 순)
    selected = candidates[:DAILY_BASE_N]

    # 나머지를 조회수·추천수 합산 내림차순으로 정렬해 추가
    rest = sorted(candidates[DAILY_BASE_N:],
                  key=_engagement_score, reverse=True)
    for p in rest:
        if len(selected) >= DAILY_MAX_N:
            break
        selected.append(p)

    return selected


def _format_posts_for_prompt(posts: list) -> str:
    lines = []
    for i, p in enumerate(posts, 1):
        source = p.get('source_label') or p.get('source', '')
        title  = p.get('title', '').strip()
        body   = (p.get('summary') or '').strip()
        views  = p.get('views', 0)
        likes  = p.get('likes', 0)
        cmts   = p.get('comments', 0)

        if body and len(body) > BODY_MAX_CHARS:
            body = body[:BODY_MAX_CHARS] + '…'

        stat_parts = []
        if views:  stat_parts.append(f'조회 {views:,}')
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
    Returns the markdown string, or '' on failure.
    """
    api_key = os.environ.get('GOOGLE_API_KEY', '')
    if not api_key:
        return ''

    if not date:
        date = kst_today()

    if not posts:
        return ''

    ranked = _select_posts(posts, date)
    if not ranked:
        return ''

    posts_text = _format_posts_for_prompt(ranked)
    count = len(ranked)

    prompt = f"""당신은 한국 인터넷 커뮤니티 트렌드 요약 편집자입니다.
오늘({date}) 주요 커뮤니티에서 화제가 된 게시글들을 정리해주세요.

## 작성 원칙
1. **밀도 우선**: 각 섹션은 이슈의 핵심·맥락·반응을 담되, 내용이 없는 문장은 쓰지 말 것. 이미 한 말 반복 금지.
2. **관련 글 통합**: 비슷한 주제는 하나의 섹션으로 묶어 서술.
3. **사실 기반**: 제공된 정보 외 추측·창작 금지. 제목·본문에 없는 내용 지어내지 말 것.
4. **형식**: `## 섹션 제목` 으로 주제별 구분. 섹션 수 5-8개. 섹션당 2-4문장.
5. 커뮤니티 이름 나열 금지. 내용 위주 서술.
6. 🔥급상승 표시된 글은 섹션 제목에 "🔥" 표시.
7. 불필요한 서론·마무리 인사·감상 금지. 이슈 설명만.

## 오늘의 주요 게시글 ({count}개)

{posts_text}
"""

    try:
        url = (
            'https://generativelanguage.googleapis.com/v1beta/models/'
            'gemini-2.5-flash:generateContent'
        )
        payload = {
            'contents': [{'parts': [{'text': prompt}]}],
            'generationConfig': {
                'temperature': 0.4,
                'maxOutputTokens': 4096,
            },
            'thinkingConfig': {'thinkingBudget': 0},
        }
        r = requests.post(
            url, json=payload,
            headers={'x-goog-api-key': api_key},
            timeout=60,
        )
        r.raise_for_status()
        text = r.json()['candidates'][0]['content']['parts'][0]['text']
        print(f'[Daily] {date} 깊은 요약 생성 완료 ({len(text)}자)')
        return text.strip()
    except Exception as e:
        print(f'[Daily] 요약 생성 오류: {e}')
        return ''
