from flask import Flask, jsonify, render_template, request
from crawler import TrendCrawler
import threading
import time
from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__)
crawler = TrendCrawler()

REFRESH_INTERVAL = 300  # 5분


def _background_loop():
    while True:
        time.sleep(REFRESH_INTERVAL)
        print(f'[Auto] 크롤링 시작 ({time.strftime("%H:%M:%S")})')
        try:
            crawler.refresh()
            print('[Auto] 완료')
        except Exception as e:
            print(f'[Auto] 오류: {e}')


def _startup():
    """서버 시작 후 첫 크롤링을 백그라운드로 실행."""
    print('[Startup] 초기 크롤링 시작')
    try:
        crawler.refresh()
        print('[Startup] 완료')
    except Exception as e:
        print(f'[Startup] 오류: {e}')
    _background_loop()


threading.Thread(target=_startup, daemon=True).start()


@app.route('/')
def index():
    return render_template('index.html', refresh_interval=REFRESH_INTERVAL)


@app.route('/api/trends')
def api_trends():
    data = crawler.get_data()
    # 탭 필터 지원
    tab = request.args.get('tab', 'all')
    src = request.args.get('source', '')  # fmkorea, todayhumor, ruliweb, clien, instagram
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


if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
