"""
옛 SQLite 데일리 아카이브를 data/daily/YYYY-MM-DD.json 으로 옮기는 1회용 도구.

옛 스키마: daily_summaries(date TEXT PK, summary_md, posts_json, post_count, generated_at)

사용법:
  python tools/import_daily_db.py --db backup/daily.db              # data/daily/ 로 변환
  python tools/import_daily_db.py --db daily.db --out /tmp/daily    # 다른 디렉터리로
  python tools/import_daily_db.py --db daily.db --overwrite         # 이미 있는 JSON도 덮어쓰기

잘못된 날짜 행(끝 개행, '2099-99-99' 등)·미래 날짜·빈 요약은 건너뛰고 끝에 보고한다.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import daily  # noqa: E402

KST = timezone(timedelta(hours=9))

# 옛 daily.py는 처음(8991ff2)부터 SQLite 시절 끝까지 이 모델만 썼다 (git log -p daily.py 로 확인)
LEGACY_MODEL = 'gemini-2.5-flash'


def _normalize_generated_at(value) -> str | None:
    """오프셋 포함 ISO 8601로 맞춘다. 오프셋이 없으면 KST로 본다. 해석 불가면 None."""
    try:
        dt = datetime.fromisoformat(str(value).strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return dt.isoformat(timespec='seconds')


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='옛 SQLite 데일리 아카이브 → data/daily/*.json')
    ap.add_argument('--db', required=True, help='옛 SQLite 파일 경로 (예: Railway 볼륨에서 받은 daily.db)')
    ap.add_argument('--out', help=f'출력 디렉터리 (기본: {daily.DAILY_DIR})')
    ap.add_argument('--overwrite', action='store_true', help='이미 있는 JSON도 덮어쓴다')
    args = ap.parse_args(argv)

    if not os.path.isfile(args.db):
        print(f'[import] DB 파일 없음: {args.db}')
        return 1
    if args.out:
        daily.DAILY_DIR = os.path.abspath(args.out)

    # 읽기 전용으로 연다 (원본 DB를 건드리지 않도록)
    conn = sqlite3.connect(Path(args.db).resolve().as_uri() + '?mode=ro', uri=True)
    try:
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='daily_summaries'"
        ).fetchone()
        if not has_table:
            print('[import] daily_summaries 테이블이 없습니다.')
            return 1
        rows = conn.execute(
            'SELECT date, summary_md, posts_json, post_count, generated_at '
            'FROM daily_summaries ORDER BY date'
        ).fetchall()
    finally:
        conn.close()

    written, bad_date, exists, failed, warnings = [], [], [], [], []
    for date, summary_md, posts_json, post_count, generated_at in rows:
        if not daily.is_valid_date(date):
            bad_date.append(repr(date))
            continue
        if not args.overwrite and daily.has_summary(date):
            exists.append(date)
            continue

        try:
            posts = json.loads(posts_json or '[]')
            if not isinstance(posts, list):
                raise ValueError('리스트가 아님')
        except ValueError as e:
            warnings.append(f'{date}: posts_json 해석 실패({e}) - 글 목록 없이 저장')
            posts = post_count = None

        # 옛 FMK 조회수는 TBS 합성값(37만~95만 계단값)이다 — 새 정책(crawler.SYNTHETIC_VIEW_SOURCES)처럼 0으로
        for p in posts or []:
            if isinstance(p, dict) and p.get('source') == 'fmkorea':
                p['views'] = 0

        gen_at = _normalize_generated_at(generated_at)
        if gen_at is None:
            # 변환 시각(오늘)을 쓰면 옛 리포트의 발행일·sitemap lastmod가 오늘이 된다 → 그날 정오로 둔다
            gen_at = f'{date}T12:00:00+09:00'
            warnings.append(f'{date}: generated_at {generated_at!r} 해석 실패 - {gen_at}로 저장')

        try:
            daily.save_summary(date, summary_md or '', posts or [],
                               generated_at=gen_at, model=LEGACY_MODEL)
        except ValueError as e:
            failed.append(f'{date} ({e})')
            continue

        saved = daily.get_summary(date)
        if saved and post_count is not None and saved['post_count'] != post_count:
            warnings.append(f'{date}: 글 수 {post_count} → {saved["post_count"]} '
                            f'(상위 {daily.SAVE_MAX_POSTS}개·http(s) 링크만 저장)')
        written.append(date)

    print(f'[import] DB: {args.db} (행 {len(rows)}개)')
    print(f'[import] 저장 {len(written)}개 → {daily.DAILY_DIR}'
          + (f' ({written[0]} ~ {written[-1]})' if written else ''))
    if bad_date:
        print(f'[import] 건너뜀 - 잘못된 날짜 {len(bad_date)}개: {", ".join(bad_date)}')
    if exists:
        print(f'[import] 건너뜀 - 이미 있음 {len(exists)}개 (--overwrite로 덮어쓰기): {", ".join(exists)}')
    if failed:
        print(f'[import] 건너뜀 - 저장 불가 {len(failed)}개: {"; ".join(failed)}')
    for w in warnings:
        print(f'[import] 경고 - {w}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
