from flask import Flask, jsonify, render_template, request, redirect, url_for, abort
from crawler import TrendCrawler
import daily as daily_module
import threading
import time
import markdown
import re
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
load_dotenv()

KST = timezone(timedelta(hours=9))

app = Flask(__name__)
crawler = TrendCrawler()

REFRESH_INTERVAL = 600  # 10분

# Daily summary: generate once per KST day, at most one background job running
_daily_lock = threading.Lock()
_daily_generating = False


def _try_generate_daily(posts: list, force: bool = False, target_date: str = None):
    """Generate a daily summary. target_date defaults to today (KST)."""
    global _daily_generating
    with _daily_lock:
        if _daily_generating:
            return
        date = target_date or daily_module.kst_today()
        if not force and daily_module.has_summary(date):
            return
        if len(posts) < 20:
            return
        _daily_generating = True

    try:
        print(f'[Daily] {date} 요약 생성 시작')
        md = daily_module.generate_deep_summary(posts, date)
        if md:
            daily_module.save_summary(date, md, posts[:50])
    finally:
        with _daily_lock:
            _daily_generating = False


def _midnight_scheduler():
    """매일 KST 자정에 하루가 끝난 날(어제) 데일리 요약을 생성한다."""
    while True:
        now = datetime.now(KST)
        tomorrow = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=5, microsecond=0
        )
        sleep_secs = (tomorrow - now).total_seconds()
        print(f'[Daily Scheduler] 다음 생성: {tomorrow.strftime("%Y-%m-%d %H:%M")} KST '
              f'({sleep_secs / 3600:.1f}시간 후)')
        time.sleep(sleep_secs)
        # 자정이 됐을 때 하루가 막 끝난 날 = 어제
        yesterday = (datetime.now(KST) - timedelta(days=1)).strftime('%Y-%m-%d')
        posts = crawler.get_data().get('posts', [])
        threading.Thread(
            target=_try_generate_daily, args=(posts, True, yesterday), daemon=True
        ).start()


def _background_loop():
    while True:
        time.sleep(REFRESH_INTERVAL)
        print(f'[Auto] 크롤링 시작 ({time.strftime("%H:%M:%S")})')
        try:
            crawler.refresh()
            posts = crawler.get_data().get('posts', [])
            threading.Thread(target=_try_generate_daily, args=(posts,), daemon=True).start()
            print('[Auto] 완료')
        except Exception as e:
            print(f'[Auto] 오류: {e}')


def _startup():
    """서버 시작 후 첫 크롤링을 백그라운드로 실행."""
    daily_module.init_db()
    print('[Startup] 초기 크롤링 시작')
    try:
        crawler.refresh()
        posts = crawler.get_data().get('posts', [])
        threading.Thread(target=_try_generate_daily, args=(posts,), daemon=True).start()
        print('[Startup] 완료')
    except Exception as e:
        print(f'[Startup] 오류: {e}')
    _background_loop()


threading.Thread(target=_startup, daemon=True).start()
threading.Thread(target=_midnight_scheduler, daemon=True).start()


# ── helpers ───────────────────────────────────────────────────────────────────

def _md_to_html(text: str) -> str:
    """Convert markdown to safe HTML."""
    return markdown.markdown(
        text,
        extensions=['nl2br', 'sane_lists'],
    )


def _format_date_kr(date_str: str) -> str:
    """'2025-04-26'  →  '2025년 4월 26일'"""
    try:
        dt = datetime.strptime(date_str, '%Y-%m-%d')
        return f'{dt.year}년 {dt.month}월 {dt.day}일'
    except Exception:
        return date_str


# ── Main routes ───────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html', refresh_interval=REFRESH_INTERVAL)


@app.route('/daily')
def daily_list():
    summaries = daily_module.list_summaries(60)
    for s in summaries:
        s['date_kr'] = _format_date_kr(s['date'])
    today = daily_module.kst_today()
    return render_template('daily.html',
                           summaries=summaries,
                           today=today,
                           view='list',
                           summary=None)


@app.route('/daily/<date>')
def daily_detail(date: str):
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
        abort(404)
    record = daily_module.get_summary(date)
    if not record:
        # If it's today and not yet generated, show a pending page
        today = daily_module.kst_today()
        if date == today:
            return render_template('daily.html',
                                   view='pending',
                                   date=date,
                                   date_kr=_format_date_kr(date),
                                   summary=None,
                                   summaries=[])
        abort(404)

    record['summary_html'] = _md_to_html(record['summary_md'])
    record['date_kr'] = _format_date_kr(record['date'])

    # Adjacent dates for navigation
    all_dates = [s['date'] for s in daily_module.list_summaries(365)]
    idx = all_dates.index(date) if date in all_dates else -1
    prev_date = all_dates[idx + 1] if idx >= 0 and idx + 1 < len(all_dates) else None
    next_date = all_dates[idx - 1] if idx > 0 else None

    return render_template('daily.html',
                           view='detail',
                           summary=record,
                           prev_date=prev_date,
                           next_date=next_date,
                           summaries=[])


# ── API routes ────────────────────────────────────────────────────────────────

@app.route('/api/trends')
def api_trends():
    data = crawler.get_data()
    tab = request.args.get('tab', 'all')
    src = request.args.get('source', '')
    if src:
        data['posts'] = [p for p in data['posts'] if p['source'] == src]
    elif tab == 'instagram':
        data['posts'] = [p for p in data['posts'] if p['source'] == 'instagram']
    elif tab == 'food':
        data['posts'] = [p for p in data['posts'] if p.get('is_food')]
    elif tab == 'beauty':
        data['posts'] = [p for p in data['posts'] if p.get('is_beauty') or p.get('is_fashion')]
    elif tab == 'game':
        data['posts'] = [p for p in data['posts'] if p.get('is_game')]
    elif tab == 'celeb':
        data['posts'] = [p for p in data['posts'] if p.get('is_celeb')]
    elif tab == 'humor':
        data['posts'] = [p for p in data['posts'] if p.get('is_humor')]
    elif tab == 'car':
        data['posts'] = [p for p in data['posts'] if p.get('is_car')]
    data['total'] = len(data['posts'])
    return jsonify(data)


@app.route('/api/refresh', methods=['POST'])
def api_refresh():
    threading.Thread(target=crawler.refresh, daemon=True).start()
    return jsonify({'status': 'started'})


@app.route('/api/status')
def api_status():
    d = crawler.get_data()
    return jsonify({
        'status': d['status'],
        'last_updated': d['last_updated'],
        'total': d['total'],
        'crawl_count': d['crawl_count'],
    })


@app.route('/api/daily')
def api_daily_list():
    return jsonify(daily_module.list_summaries(60))


@app.route('/api/daily/<date>')
def api_daily_detail(date: str):
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
        return jsonify({'error': 'invalid date'}), 400
    record = daily_module.get_summary(date)
    if not record:
        return jsonify({'error': 'not found'}), 404
    return jsonify({
        'date': record['date'],
        'summary_md': record['summary_md'],
        'post_count': record['post_count'],
        'generated_at': record['generated_at'],
    })


@app.route('/api/daily/generate', methods=['POST'])
def api_daily_generate():
    """Force-generate (or re-generate) today's daily summary."""
    data = request.get_json(silent=True) or {}
    date = data.get('date', daily_module.kst_today())
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
        return jsonify({'error': 'invalid date'}), 400

    posts = crawler.get_data().get('posts', [])
    if len(posts) < 5:
        return jsonify({'error': 'not enough posts'}), 503

    def _gen():
        md = daily_module.generate_deep_summary(posts, date)
        if md:
            daily_module.save_summary(date, md, posts[:50])

    threading.Thread(target=_gen, daemon=True).start()
    return jsonify({'status': 'started', 'date': date})


if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
