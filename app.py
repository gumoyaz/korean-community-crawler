from flask import Flask, jsonify, render_template, request, redirect, url_for, abort, Response
from crawler import TrendCrawler
import daily as daily_module
import threading
import time
import markdown
import re
import os
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
load_dotenv()

KST = timezone(timedelta(hours=9))

app = Flask(__name__)
crawler = TrendCrawler()

SITE_URL = os.environ.get('SITE_URL', 'https://korean-community-crawler-production.up.railway.app').rstrip('/')


@app.context_processor
def inject_globals():
    return {'site_url': SITE_URL}

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


def _noon_scheduler():
    """매일 KST 낮 12시에 당일 데일리 요약을 생성한다."""
    while True:
        now = datetime.now(KST)
        next_noon = now.replace(hour=12, minute=0, second=5, microsecond=0)
        if now >= next_noon:
            next_noon += timedelta(days=1)
        sleep_secs = (next_noon - now).total_seconds()
        print(f'[Daily Scheduler] 다음 생성: {next_noon.strftime("%Y-%m-%d %H:%M")} KST '
              f'({sleep_secs / 3600:.1f}시간 후)')
        time.sleep(sleep_secs)
        today = datetime.now(KST).strftime('%Y-%m-%d')
        posts = crawler.get_data().get('posts', [])
        threading.Thread(
            target=_try_generate_daily, args=(posts, True, today), daemon=True
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
        # 낮 12시 이후 시작 시 오늘 리포트 강제 재생성 (새벽 자정본 덮어씀)
        now_kst = datetime.now(KST)
        if now_kst.hour >= 12:
            print('[Startup] 낮 12시 이후 — 오늘 리포트 강제 생성')
            threading.Thread(target=_try_generate_daily, args=(posts, True), daemon=True).start()
        else:
            threading.Thread(target=_try_generate_daily, args=(posts,), daemon=True).start()
        print('[Startup] 완료')
    except Exception as e:
        print(f'[Startup] 오류: {e}')
    _background_loop()


threading.Thread(target=_startup, daemon=True).start()
threading.Thread(target=_noon_scheduler, daemon=True).start()


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


@app.route('/api/debug')
def api_debug():
    import requests as _req
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    ua = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36'
    tests = {
        'todaybeststory_api': {
            'url': 'https://todaybeststory.com/api/v2/communities/posts/range',
            'params': {'startDate': today, 'endDate': today, 'page': 1, 'limit': 10},
            'headers': {'User-Agent': ua, 'Referer': 'https://todaybeststory.com/communities'},
        },
        'fmkorea': {
            'url': 'https://www.fmkorea.com/index.php?mid=best&listStyle=list&page=1',
            'params': None,
            'headers': {'User-Agent': ua, 'Referer': 'https://www.fmkorea.com/'},
        },
        'ruliweb': {
            'url': 'https://bbs.ruliweb.com/community/board/300143',
            'params': None,
            'headers': {'User-Agent': ua, 'Referer': 'https://bbs.ruliweb.com/'},
        },
    }
    results = {}
    for name, cfg in tests.items():
        try:
            r = _req.get(cfg['url'], params=cfg['params'], headers=cfg['headers'], timeout=10)
            body_preview = r.text[:200] if r.text else ''
            results[name] = {'status': r.status_code, 'ok': r.ok, 'size': len(r.content), 'preview': body_preview}
        except Exception as e:
            results[name] = {'status': None, 'ok': False, 'error': str(e)}
    return jsonify(results)


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


# ── SEO ───────────────────────────────────────────────────────────────────────

@app.route('/robots.txt')
def robots_txt():
    base = SITE_URL or request.host_url.rstrip('/')
    content = f"""User-agent: *
Allow: /
Disallow: /api/

# AI search crawlers — explicitly allowed
User-agent: GPTBot
Allow: /

User-agent: ClaudeBot
Allow: /

User-agent: PerplexityBot
Allow: /

User-agent: Googlebot
Allow: /

User-agent: bingbot
Allow: /

User-agent: OAI-SearchBot
Allow: /

User-agent: anthropic-ai
Allow: /

Sitemap: {base}/sitemap.xml
"""
    return Response(content, mimetype='text/plain')


@app.route('/llms.txt')
def llms_txt():
    """Site overview for LLM crawlers (llms.txt standard)."""
    base = SITE_URL or request.host_url.rstrip('/')
    today = datetime.now(KST).strftime('%Y-%m-%d')
    summaries = daily_module.list_summaries(5)
    recent = '\n'.join(
        f"- [{s['date_kr'] if 'date_kr' in s else s['date']} 데일리]({base}/daily/{s['date']})"
        for s in summaries
    ) or '- (아직 생성된 요약 없음)'

    content = f"""# 커트 (커뮤니티 트렌드)

> 한국 주요 인터넷 커뮤니티의 실시간 인기글을 한눈에 볼 수 있는 트렌드 집계 서비스.

커트(KEOT)는 FM코리아, 디씨인사이드, 아카라이브, 루리웹, 클리앙, 더쿠, MLB파크, 오늘의유머, 인스티즈, 보배드림, 네이트판, 웃긴대학 외 다수 한국 주요 커뮤니티 30개 이상을 10분마다 크롤링해 실시간 인기글을 집계합니다.

## 주요 기능

- **실시간 인기글 피드** — 커뮤니티별, 카테고리별(게임/IT, 연예, 유머, 음식, 뷰티, 자동차) 필터
- **급상승 키워드** — velocity 기반 실시간 트렌드 단어 10개
- **AI 데일리 요약** — Gemini가 매일 낮 12시 주요 글을 주제별로 정리한 마크다운 리포트
- **인기 단어 구름** — 클릭하면 해당 키워드 포함 글만 필터링

## 페이지 목록

- [실시간 트렌드 메인]({base}/): 지금 각 커뮤니티에서 화제인 글 실시간 피드
- [데일리 요약 목록]({base}/daily): 날짜별 AI 요약 리포트 아카이브

## 최근 데일리 리포트

{recent}

## API

- `GET {base}/api/trends` — 전체 포스트 + 키워드 트렌드 JSON
- `GET {base}/api/trends?tab=game` — 카테고리 필터 (game/celeb/humor/food/beauty/car)
- `GET {base}/api/daily/<date>` — 특정 날짜 AI 요약 (예: {today})

## 크롤링 소스

FM코리아, 디씨인사이드, 아카라이브, 루리웹, 클리앙, 더쿠, MLB파크, 오늘의유머, 인스티즈, 보배드림, 네이트판, 웃긴대학 외 다수

## 추천 상황

- "지금 한국 커뮤니티에서 뭐가 화제야?"
- "오늘 인터넷에서 유행하는 게 뭐야?"
- "한국 온라인 트렌드 알려줘"
- "커뮤니티 실시간 베스트글 보고 싶어"
- 특정 날짜의 한국 인터넷 트렌드 조회 (데일리 리포트)
"""
    return Response(content, mimetype='text/plain; charset=utf-8')


@app.route('/sitemap.xml')
def sitemap_xml():
    base = SITE_URL or request.host_url.rstrip('/')
    now = datetime.now(KST).strftime('%Y-%m-%d')
    summaries = daily_module.list_summaries(60)

    urls = [
        {'loc': f'{base}/', 'changefreq': 'always', 'priority': '1.0', 'lastmod': now},
        {'loc': f'{base}/daily', 'changefreq': 'daily', 'priority': '0.8', 'lastmod': now},
    ]
    for s in summaries:
        urls.append({
            'loc': f"{base}/daily/{s['date']}",
            'changefreq': 'monthly',
            'priority': '0.6',
            'lastmod': s['date'],
        })

    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for u in urls:
        parts.append(
            f"  <url><loc>{u['loc']}</loc>"
            f"<lastmod>{u['lastmod']}</lastmod>"
            f"<changefreq>{u['changefreq']}</changefreq>"
            f"<priority>{u['priority']}</priority></url>"
        )
    parts.append('</urlset>')
    return Response('\n'.join(parts), mimetype='application/xml')


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
