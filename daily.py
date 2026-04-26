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

# How many top posts to include in the prompt
DAILY_TOP_N = 50
# Truncate each body to keep prompt size sane
BODY_MAX_CHARS = 400


def _format_posts_for_prompt(posts: list) -> str:
    lines = []
    for i, p in enumerate(posts[:DAILY_TOP_N], 1):
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

        lines.append(f'### [{i}위] {title}  ({source})')
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

    ranked = sorted(posts, key=lambda p: p.get('rank', 9999))
    if not ranked:
        return ''

    posts_text = _format_posts_for_prompt(ranked)
    count = min(len(ranked), DAILY_TOP_N)

    prompt = f"""당신은 한국 인터넷 커뮤니티 전문 기자입니다.
오늘({date}) 12개 이상의 한국 커뮤니티에서 가장 많이 조회되고 공유된 게시글들을 분석하여,
독자가 이 요약만 읽어도 오늘 온라인 커뮤니티에서 무슨 일이 있었는지 완전히 이해할 수 있도록
깊고 상세한 리포트를 마크다운 형식으로 작성해주세요.

## 작성 원칙
1. **스토리 중심**: 단순 제목 나열이 아니라 각 이슈의 배경·내용·반응을 완전히 설명하세요.
2. **관련 글 통합**: 비슷한 주제의 여러 게시글은 하나의 이야기로 묶어 서술하세요.
3. **본문 적극 활용**: 제공된 본문 내용을 바탕으로 구체적으로 서술하세요.
4. **커뮤니티 반응 포함**: 공감·댓글 수가 높은 글의 분위기·반응도 전달하세요.
5. **사실 기반**: 제공된 정보 이외의 내용은 추측하거나 추가하지 마세요.
6. **형식**: `## 섹션 제목` 으로 주제별 구분, 각 섹션은 3-5문장 이상.
7. 커뮤니티 이름은 나열하지 말고 내용 위주로 서술하세요.

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
                'maxOutputTokens': 8192,
            },
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
