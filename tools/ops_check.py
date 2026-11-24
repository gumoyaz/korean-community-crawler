"""운영 점검 — ops-check.yml이 하루 한 번 실행한다. 크롤 실행(pages.yml) 밖에서 도는 감시라서
실행 자체가 멈춘 것도 잡는다(수집 이상 알림 source-health는 크롤 실행 안에서만 나서 실행이 멈추면 아무도 모른다).

1) 매일: 사이트가 STALE_H시간 넘게 갱신되지 않았거나, 지난 24시간 외부 트리거(cron-job.org의 workflow_dispatch)가
   한 번도 없으면 'ops-alert' 라벨 이슈를 열고, 상태가 바뀌면 코멘트, 풀리면 닫는다.
   외부 트리거가 끊기면 pages.yml의 백업 schedule만 돌아 사이트가 드문드문 갱신된다 — 토큰 만료가 가장 흔한 원인.
2) 월간 보고: 이번 달(KST) 'monthly-report' 이슈가 없으면 지난 30일 요약을 이슈로 올리고 지난달 보고는 닫는다.
   스케줄이 1일에 빠져도 그달 첫 실행이 올린다. 검색·방문 분석(서치 콘솔·GA4·Clarity)은 키가 없어 넣지 않는다.

환경변수: GITHUB_TOKEN(issues: write, actions: read), GITHUB_REPOSITORY, SITE_URL(선택), FORCE_REPORT=true(선택),
DRY_RUN=true(선택 — 이슈를 건드리지 않고 출력만).
"""
import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent
STALE_H = 3                 # 평소 10분 간격. 백업 schedule만 돌아도 이보다 자주 갱신된다
DISPATCH_WINDOW_H = 24
ALERT_LABEL = 'ops-alert'
REPORT_LABEL = 'monthly-report'
REPORT_DAYS = 30

try:
    sys.path.insert(0, str(ROOT))
    from crawler import SOURCE_META   # source id → (라벨, 이모지, 색)
except Exception:   # 점검은 크롤러 의존성이 깨져도 돌아야 한다
    SOURCE_META = {}

TOKEN = os.environ.get('GITHUB_TOKEN', '')
REPO = os.environ.get('GITHUB_REPOSITORY', 'gumoyaz/korean-community-crawler')
SITE = (os.environ.get('SITE_URL') or 'https://gumoyaz.github.io/korean-community-crawler').rstrip('/')
DRY = os.environ.get('DRY_RUN') == 'true'
API = f'https://api.github.com/repos/{REPO}'
S = requests.Session()
S.headers.update({'Accept': 'application/vnd.github+json', 'User-Agent': 'kurt-ops-check'})
if TOKEN:
    S.headers['Authorization'] = f'Bearer {TOKEN}'


def _iso(v):
    try:
        dt = datetime.fromisoformat(str(v).replace('Z', '+00:00'))
        return dt if dt.tzinfo else dt.replace(tzinfo=KST)
    except (TypeError, ValueError):
        return None


def _get(path, **params):
    r = S.get(API + path, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def _label(s):
    return SOURCE_META.get(s, (s,))[0]


RUNS_WINDOW_D = 3   # 실행 목록 API의 total_count는 2500에서 잘린다(30일 조회가 성공·전체 모두 2500). 3일이면 약 700회


def _runs_count(since: datetime, until: datetime | None = None, **filters) -> int:
    until = until or datetime.now(timezone.utc)
    fmt = lambda d: d.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    total, a = 0, since
    while a < until:
        b = min(a + timedelta(days=RUNS_WINDOW_D), until)
        q = {'created': f'{fmt(a)}..{fmt(b - timedelta(seconds=1))}', 'per_page': 1, **filters}
        total += int(_get('/actions/workflows/pages.yml/runs', **q).get('total_count') or 0)
        a = b
    return total


def _site():
    r = requests.get(f'{SITE}/data/trends.json', params={'t': int(datetime.now().timestamp())}, timeout=30,
                     headers={'User-Agent': 'kurt-ops-check'})
    r.raise_for_status()
    return r.json()


# ── 1) 매일 감시 ─────────────────────────────────────────────────────────────

def check(now: datetime, data) -> list:
    problems = []
    if data is None:
        problems.append('사이트의 data/trends.json을 받지 못함')
    else:
        last = _iso(data.get('checked_at') or data.get('last_updated'))
        if not last:
            problems.append('trends.json에 마지막 수집 시각이 없음')
        elif (now - last).total_seconds() > STALE_H * 3600:
            problems.append(f'사이트 갱신 멈춤: 마지막 수집 {(now - last).total_seconds() / 3600:.1f}시간 전 '
                            f'({last.astimezone(KST):%m-%d %H:%M} KST)')
    try:
        n = _runs_count(now - timedelta(hours=DISPATCH_WINDOW_H), event='workflow_dispatch')
        if n == 0:
            problems.append(f'외부 트리거 끊김: 지난 {DISPATCH_WINDOW_H}시간 workflow_dispatch 실행 0회 — '
                            'cron-job.org 작업 상태와 토큰 만료를 확인 (그동안은 백업 schedule만 돈다)')
    except requests.RequestException as e:
        print(f'[ops] 실행 수 조회 실패: {e}')
    return problems


def _open_issues(label):
    return [i for i in _get('/issues', state='open', labels=label, per_page=20) if 'pull_request' not in i]


def _ensure_label(name, color, desc):
    if DRY:
        return
    r = S.get(f'{API}/labels/{name}', timeout=30)
    if r.status_code == 404:
        S.post(f'{API}/labels', json={'name': name, 'color': color, 'description': desc}, timeout=30)


def alert(now: datetime, problems: list):
    stamp = f'{now.astimezone(KST):%Y-%m-%d %H:%M} KST'
    body = '\n'.join(f'- {p}' for p in problems)
    opened = _open_issues(ALERT_LABEL)
    if problems and not opened:
        print(f'[ops] 이슈 열기:\n{body}')
        if not DRY:
            _ensure_label(ALERT_LABEL, 'b60205', '운영 점검(ops-check) 경고')
            S.post(f'{API}/issues', timeout=30, json={
                'title': f'운영 경고: {problems[0].split(":")[0]} ({stamp})', 'labels': [ALERT_LABEL],
                'body': f'{body}\n\n운영 점검(ops-check.yml)이 하루 한 번 확인합니다. 풀리면 자동으로 닫힙니다.'})
    elif problems:
        issue = opened[0]
        if body not in (issue.get('body') or '') and not _last_comment_has(issue, body):
            print(f'[ops] #{issue["number"]} 코멘트:\n{body}')
            if not DRY:
                S.post(f'{API}/issues/{issue["number"]}/comments', json={'body': f'{stamp}\n\n{body}'}, timeout=30)
        else:
            print(f'[ops] #{issue["number"]} 같은 상태 지속 — 코멘트 안 함')
    else:
        for issue in opened:
            print(f'[ops] #{issue["number"]} 닫기 (회복)')
            if not DRY:
                S.patch(f'{API}/issues/{issue["number"]}', json={'state': 'closed'}, timeout=30)
                S.post(f'{API}/issues/{issue["number"]}/comments', timeout=30,
                       json={'body': f'회복: 사이트 갱신과 외부 트리거 모두 정상 ({stamp}).'})
        if not opened:
            print('[ops] 이상 없음')


def _last_comment_has(issue, text) -> bool:
    if not issue.get('comments'):
        return False
    cs = _get(f'/issues/{issue["number"]}/comments', per_page=100)
    return bool(cs) and text in (cs[-1].get('body') or '')


# ── 2) 월간 보고 ─────────────────────────────────────────────────────────────

def report(now: datetime, data, problems: list) -> str:
    since = now - timedelta(days=REPORT_DAYS)
    lines = [f'기간: {since.astimezone(KST):%Y-%m-%d} ~ {now.astimezone(KST):%Y-%m-%d} (KST, {REPORT_DAYS}일)', '']

    lines.append('### 실행')
    try:
        total = _runs_count(since)
        ok = _runs_count(since, status='success')
        fail = _runs_count(since, status='failure')
        disp = _runs_count(since, event='workflow_dispatch')
        lines.append(f'- Build and deploy {total}회: 성공 {ok}, 실패 {fail}, 그 밖(취소·건너뜀 등) {total - ok - fail}')
        lines.append(f'- 외부 트리거(workflow_dispatch) {disp}회 (10분 간격이면 약 {REPORT_DAYS * 144}회)')
    except requests.RequestException as e:
        lines.append(f'- 실행 수 조회 실패: {e}')

    lines += ['', '### 사이트·수집']
    if data is None:
        lines.append('- trends.json을 받지 못함')
    else:
        lines.append(f'- 지금 상태 {data.get("status")}, 마지막 수집 {data.get("checked_at") or data.get("last_updated")}')
        sh = data.get('source_health') or {}
        bad = {s: e for s, e in sh.items() if isinstance(e, dict) and e.get('status') != 'ok'}
        lines.append('- 커뮤니티: ' + (', '.join(f'{_label(s)} {e.get("status")}' for s, e in sorted(bad.items()))
                                     if bad else f'{len(sh)}곳 모두 ok'))
    lines.append('- 운영 경고: ' + ('; '.join(problems) if problems else '없음'))
    try:
        issues = [i for i in _get('/issues', state='all', labels='source-health', since=since.isoformat(), per_page=100)
                  if (_iso(i['created_at']) or now) >= since]
        lines.append(f'- 수집 이상 이슈(source-health) {len(issues)}건'
                     + (': ' + ', '.join(f'#{i["number"]}' for i in issues[:15]) if issues else ''))
    except requests.RequestException as e:
        lines.append(f'- 수집 이상 이슈 조회 실패: {e}')

    lines += ['', '### 데일리 리포트']
    first = (since.astimezone(KST) + timedelta(days=1)).date()
    last = (now.astimezone(KST) - timedelta(days=1)).date()   # 오늘 것은 정오 전이면 아직 없다
    want = [(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]
    models, missing = Counter(), []
    for d in want:
        f = ROOT / 'data' / 'daily' / f'{d}.json'
        if not f.exists():
            missing.append(d)
            continue
        try:
            x = json.loads(f.read_text(encoding='utf-8'))
            s = x.get('summary') if isinstance(x.get('summary'), dict) else x
            models[s.get('model') or x.get('model') or '?'] += 1
        except (OSError, ValueError):
            models['읽기 실패'] += 1
    lines.append(f'- {len(want) - len(missing)}/{len(want)}일 있음' + (f', 빠진 날: {", ".join(missing)}' if missing else ''))
    if models:
        lines.append('- 쓴 모델: ' + ', '.join(f'{m} {n}' for m, n in models.most_common()))

    lines += ['', '### 직접 볼 것',
              '- 검색·방문(서치 콘솔·GA4·Clarity)은 이 보고에 없다 — 키를 넣지 않았다. Claude에게 "점검"을 요청하면 같이 본다.',
              '- 이 이슈는 닫아도 된다. 다음 달 보고가 올라오면 자동으로 닫힌다.']
    return '\n'.join(lines)


def monthly(now: datetime, data, problems: list, force=False):
    month = f'{now.astimezone(KST):%Y-%m}'
    title = f'월간 점검 {month}'
    recent = [i for i in _get('/issues', state='all', labels=REPORT_LABEL, per_page=20) if 'pull_request' not in i]
    if any(i['title'] == title for i in recent) and not force:
        print(f'[ops] {title} 이미 있음')
        return
    body = report(now, data, problems)
    print(f'[ops] {title}\n{body}')
    if DRY:
        return
    _ensure_label(REPORT_LABEL, '0e8a16', '운영 점검(ops-check) 월간 보고')
    S.post(f'{API}/issues', json={'title': title, 'labels': [REPORT_LABEL], 'body': body}, timeout=30)
    for i in recent:
        if i['state'] == 'open' and i['title'] != title:
            S.patch(f'{API}/issues/{i["number"]}', json={'state': 'closed'}, timeout=30)


def main():
    now = datetime.now(timezone.utc)
    try:
        data = _site()
    except (requests.RequestException, ValueError) as e:
        print(f'[ops] trends.json 실패: {e}')
        data = None
    problems = check(now, data)
    alert(now, problems)
    monthly(now, data, problems, force=os.environ.get('FORCE_REPORT') == 'true')


if __name__ == '__main__':
    main()
