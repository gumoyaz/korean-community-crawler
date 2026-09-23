"""
Gemini REST 호출 공용 모듈 (crawler.py 메인 AI 요약, daily.py 데일리 리포트가 함께 쓴다).

- SDK 없이 requests로 generateContent를 직접 호출한다.
- 키는 GOOGLE_API_KEY, 모델은 GEMINI_MODEL(빈 값이면 DEFAULT_MODEL), 예비 모델은 GEMINI_FALLBACK_MODEL(쉼표 구분).
- 실패하면 '' 를 반환하고 원인을 한 줄 로그로 남긴다. 키 값은 절대 출력하지 않는다.

모델 선택 근거 (2026-09-23 공식 문서 기준):
- https://ai.google.dev/gemini-api/docs/models — 2.5 모델은 '과거 사용자'로 접근 제한,
  신규 프로젝트에는 3.5 Flash-Lite 또는 3.8 Flash 권장. 3.8 Flash가 가장 성능 좋은 안정 Flash.
- https://ai.google.dev/gemini-api/docs/pricing — 3.8 Flash 무료 티어 있음.
- https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash — thinking은 low/medium/high만,
  'minimal'은 오류.
- https://ai.google.dev/api/generate-content#ThinkingConfig — thinkingLevel(MINIMAL/LOW/MEDIUM/HIGH)은
  3 이상 전용, 이전 모델에 보내면 오류. thinkingBudget과 같이 보내면 400.
- https://ai.google.dev/gemini-api/docs/gemini-3 — 3.x는 temperature 기본값(1.0) 유지 강력 권고
  (낮추면 반복 루프 등 이상 동작 가능).
- 무료 한도는 프로젝트 단위, RPD는 태평양 자정(KST 16~17시)에 리셋. 수치는 AI Studio에서만 공개.
"""

from __future__ import annotations

import os
import re
import time

import requests

# .env를 모듈이 먼저 읽는다 — import 순서와 관계없이 GEMINI_MODEL 등이 반영되도록 (BEX-2).
# 이미 설정된 환경변수(GitHub Actions secrets 등)는 덮어쓰지 않는다.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))
except ImportError:
    pass

DEFAULT_MODEL = 'gemini-3.8-flash'
MODEL = (os.environ.get('GEMINI_MODEL', '').strip() or DEFAULT_MODEL).removeprefix('models/')

# 기본 모델이 실패하면 차례로 넘겨 보는 예비 모델들 (무료 한도는 모델마다 따로).
# 2026-09-23~24 실측: 503 'high demand'가 모델마다 시간대별로 번갈아 났다(3.8·3.7 503, 3.6·3.5 성공,
# 3.5-Lite 60초 무응답이 같은 시각에 섞임) → 예비를 하나만 두면 둘 다 실패하는 날이 생긴다.
# GEMINI_FALLBACK_MODEL에 쉼표로 여러 개를 줄 수 있고 'none'이면 쓰지 않는다.
DEFAULT_FALLBACK_MODELS = 'gemini-3.6-flash,gemini-3.5-flash,gemini-3.5-flash-lite'
_fb = os.environ.get('GEMINI_FALLBACK_MODEL', '').strip() or DEFAULT_FALLBACK_MODELS
FALLBACK_MODELS = ([] if _fb.lower() in ('none', 'off', '0') else
                   [m for m in dict.fromkeys(x.strip().removeprefix('models/') for x in _fb.split(','))
                    if m and m != MODEL])
FALLBACK_MODEL = FALLBACK_MODELS[0] if FALLBACK_MODELS else ''   # 하위 호환(첫 번째 예비 모델)

API_BASE = 'https://generativelanguage.googleapis.com/v1beta'

# 3.x 중 thinkingLevel 'MINIMAL'을 받는 모델 (thinking 문서 표 기준). 나머지 3.x는 'LOW'가 최저
# — 3.7/3.8 Flash는 MINIMAL을 보내면 400.
_MINIMAL_OK = ('gemini-3-flash', 'gemini-3.1-flash-lite', 'gemini-3.5-flash', 'gemini-3.6-flash')

RETRY_429_MAX_WAIT = 60   # 429 재시도 대기 상한(초). 이보다 길면 재시도하지 않는다.
# 일시적 서버 오류(503 'high demand' 등)는 지수 백오프로 최대 3번 재시도한다(공식 troubleshooting 권고).
# 3.8 Flash는 503이 잦고 5초 1회 재시도로는 회복되지 않았다(2026-09-23 실측 6회 중 5회 503).
# 기본 모델의 재시도는 전체 예산(2×timeout)의 절반 안에서만 한다 → 남은 시간은 예비 모델 차례.
RETRY_5XX = (500, 502, 503, 504)
RETRY_5XX_WAITS = (2, 8, 20)

# 마지막 generate() 호출의 실패 종류 ('' = 성공). 호출부가 재시도 간격을 정할 때 쓴다.
#   'transient' — 5xx·네트워크 오류·타임아웃·분당 한도(429). 곧 다시 시도해도 된다
#   그 외('no_key', 'quota_day', 'http_4xx', 'blocked', 'finish_<REASON>', 'bad_response') — 같은 입력으로는 또 실패
last_error = ''
last_model = ''   # 마지막 generate()가 결과를 받은(또는 마지막으로 시도한) 모델 — 리포트에 기록


def available() -> bool:
    """GOOGLE_API_KEY가 설정돼 있는지."""
    return bool(os.environ.get('GOOGLE_API_KEY', '').strip())


def _version(model: str) -> tuple[int, int] | None:
    """'gemini-3.8-flash' → (3, 8). 'gemini-flash-latest'처럼 버전이 없으면 None."""
    m = re.match(r'gemini-(\d+)(?:\.(\d+))?-', model)
    return (int(m.group(1)), int(m.group(2) or 0)) if m else None


def _thinking_config(model: str) -> dict | None:
    """모델 세대별로 thinking을 가장 낮게 설정한다."""
    ver = _version(model)
    if ver is None:
        return None                      # 알 수 없는 별칭 → 모델 기본값
    if ver[0] >= 3:
        # 3.x는 thinking을 끌 수 없다 → 지원하는 가장 낮은 단계
        return {'thinkingLevel': 'MINIMAL' if model.startswith(_MINIMAL_OK) else 'LOW'}
    if ver == (2, 5) and 'flash' in model:
        return {'thinkingBudget': 0}     # 2.5 Flash 계열은 0으로 끌 수 있다 (Pro는 불가)
    return None


def _api_error(r: requests.Response) -> dict:
    """오류 응답 JSON에서 status/reason/message, 429 재시도 정보를 뽑는다."""
    try:
        err = r.json().get('error') or {}
    except ValueError:
        err = {}
    info = {
        'status': err.get('status') or '',
        'message': ' '.join(str(err.get('message') or r.text[:200]).split()),
        'reason': '',
        'retry_delay': None,   # RetryInfo.retryDelay (초)
        'per_day': False,      # 일일 한도(RPD) 소진 여부 — 재시도해도 소용없음
    }
    for d in err.get('details') or []:
        kind = d.get('@type', '')
        if kind.endswith('ErrorInfo'):
            info['reason'] = d.get('reason', '')
        elif kind.endswith('RetryInfo'):
            m = re.match(r'([\d.]+)s$', str(d.get('retryDelay', '')))
            if m:
                info['retry_delay'] = float(m.group(1))
        elif kind.endswith('QuotaFailure'):
            info['per_day'] = any('PerDay' in v.get('quotaId', '')
                                  for v in d.get('violations') or [])
    return info


def _log_http_error(r: requests.Response, key: str, model: str) -> None:
    e = _api_error(r)
    code = f' {e["status"]}/{e["reason"]}' if e['reason'] else (f' {e["status"]}' if e['status'] else '')
    if e['reason'] == 'API_KEY_INVALID' or 'API key not valid' in e['message']:
        hint = 'GOOGLE_API_KEY가 무효(삭제·폐기된 키)입니다. AI Studio에서 새 키를 발급해 설정하세요.'
    elif r.status_code == 404:
        hint = f'모델 {model}을(를) 쓸 수 없습니다. GEMINI_MODEL·GEMINI_FALLBACK_MODEL을 확인하세요.'
    elif r.status_code == 403:
        hint = '키 권한 문제(API 제한·프로젝트 설정) 또는 모델 접근 제한입니다.'
    elif r.status_code == 429:
        hint = ('일일 무료 한도(RPD) 소진 - 태평양 자정(KST 16~17시)에 리셋됩니다.'
                if e['per_day'] else '요청 한도 초과(RPM/TPM).')
    else:
        hint = ''
    msg = e['message'][:300].replace(key, '***') if key else e['message'][:300]
    print(f'[Gemini] HTTP {r.status_code}{code} ({model}): {msg}'
          + (f' → {hint}' if hint else ''), flush=True)


def _payload(model: str, prompt: str, max_output_tokens: int, temperature: float | None) -> dict:
    """temperature: None이면 2.5는 0.4, 3.x는 보내지 않는다(공식 권고: 3.x는 기본값 1.0 유지)."""
    gen_cfg: dict = {'maxOutputTokens': max_output_tokens}
    ver = _version(model)
    if temperature is None and ver is not None and ver < (3, 0):
        temperature = 0.4
    if temperature is not None:
        gen_cfg['temperature'] = temperature
    thinking = _thinking_config(model)
    if thinking:
        gen_cfg['thinkingConfig'] = thinking
    return {'contents': [{'parts': [{'text': prompt}]}], 'generationConfig': gen_cfg}


def _post(model: str, key: str, payload: dict, timeout: float, t0: float, retry_until: float):
    """generateContent 요청. 응답을 돌려주고, 네트워크 오류·타임아웃이면 None.
    timeout은 요청 1번의 제한 시간. retry_until > 0이면 429는 1번, 5xx는 RETRY_5XX_WAITS 간격으로
    재시도하되 대기를 합쳐 t0부터 retry_until초 안에서만 한다(예비 모델에 시간을 남기려고)."""
    url = f'{API_BASE}/models/{model}:generateContent'
    retry = retry_until > 0
    waits_5xx = list(RETRY_5XX_WAITS) if retry else []
    retried_429 = not retry
    while True:
        try:
            r = requests.post(url, json=payload, headers={'x-goog-api-key': key}, timeout=timeout)
        except requests.RequestException as e:
            print(f'[Gemini] 요청 실패 ({model}): {type(e).__name__}: {str(e).replace(key, "***")[:300]}',
                  flush=True)
            return None
        elapsed = time.monotonic() - t0
        # 429는 한 번만 짧게 재시도 (일일 한도 소진이거나 대기 시간이 길면 바로 포기)
        if r.status_code == 429 and not retried_429:
            e = _api_error(r)
            wait = e['retry_delay'] if e['retry_delay'] is not None else 10.0
            if not e['per_day'] and wait <= RETRY_429_MAX_WAIT and elapsed + wait <= retry_until:
                print(f'[Gemini] HTTP 429 {e["status"]} ({model}) - {wait:.0f}초 후 1회 재시도', flush=True)
                time.sleep(wait + 1)
                retried_429 = True
                continue
        # 503 등 일시적 서버 오류는 2·8·20초 간격으로 재시도
        if r.status_code in RETRY_5XX and waits_5xx and elapsed + waits_5xx[0] <= retry_until:
            wait = waits_5xx.pop(0)
            print(f'[Gemini] HTTP {r.status_code} {_api_error(r)["status"]} ({model}) - '
                  f'{wait}초 후 재시도 (남은 {len(waits_5xx)}회)', flush=True)
            time.sleep(wait)
            continue
        return r


def _result(r, model: str, key: str, max_output_tokens: int) -> tuple[str, str]:
    """(텍스트, 실패 종류). 성공이면 (텍스트, '')."""
    if r is None:
        return '', 'transient'
    if not r.ok:
        _log_http_error(r, key, model)
        if r.status_code in RETRY_5XX:
            return '', 'transient'
        if r.status_code == 429:
            return '', 'quota_day' if _api_error(r)['per_day'] else 'transient'
        return '', 'http_4xx'

    try:
        j = r.json()
    except ValueError:
        print(f'[Gemini] 응답 JSON 파싱 실패 ({model}): {r.text[:200]!r}', flush=True)
        return '', 'bad_response'

    candidates = j.get('candidates') or []
    if not candidates:
        fb = j.get('promptFeedback') or {}
        print(f'[Gemini] candidates 없음 ({model}) - blockReason={fb.get("blockReason", "?")}',
              flush=True)
        return '', 'blocked'

    c = candidates[0]
    finish = c.get('finishReason', '')
    parts = (c.get('content') or {}).get('parts') or []
    text = ''.join(p.get('text', '') for p in parts if not p.get('thought')).strip()
    if finish != 'STOP':
        usage = j.get('usageMetadata') or {}
        print(f'[Gemini] finishReason={finish or "?"} ({model}) - 결과 버림 '
              f'(출력 {usage.get("candidatesTokenCount", "?")}토큰, '
              f'thinking {usage.get("thoughtsTokenCount", 0)}토큰, '
              f'한도 {max_output_tokens})', flush=True)
        return '', f'finish_{finish or "?"}'
    if not text:
        print(f'[Gemini] 빈 응답 ({model}, finishReason=STOP)', flush=True)
        return '', 'bad_response'
    return text, ''


# 예비 모델은 남은 시간이 이보다 적으면 시도하지 않는다(초)
FALLBACK_MIN_SECONDS = 15


def generate(prompt: str, *, max_output_tokens: int = 8192, temperature: float | None = None,
             timeout: int = 120) -> str:
    """
    성공(finishReason == 'STOP')이면 텍스트, 그 외(키 없음/HTTP 오류/candidates 없음/MAX_TOKENS/SAFETY 등)는 ''.
    실패 원인은 print로 한 줄 남긴다. 키 값은 절대 출력하지 않는다.

    temperature: None이면 2.5는 0.4, 3.x는 보내지 않는다(공식 권고: 3.x는 기본값 1.0 유지).
    값을 주면 모델과 관계없이 그대로 보낸다.
    기본 모델이 실패하면 FALLBACK_MODELS를 차례로 1번씩 시도한다. 전체 소요는 약 2×timeout 이내이고,
    기본 모델의 재시도는 그 절반 안에서만 해서 예비 모델에 시간을 남긴다.
    실패 종류는 last_error, 결과를 낸(마지막으로 시도한) 모델은 last_model.
    """
    global last_error, last_model
    last_error = ''
    last_model = MODEL
    key = os.environ.get('GOOGLE_API_KEY', '').strip()
    if not key:
        print('[Gemini] GOOGLE_API_KEY 없음 - 생성 건너뜀', flush=True)
        last_error = 'no_key'
        return ''

    budget = 2 * timeout
    t0 = time.monotonic()
    r = _post(MODEL, key, _payload(MODEL, prompt, max_output_tokens, temperature), timeout, t0,
              retry_until=budget / 2)
    text, last_error = _result(r, MODEL, key, max_output_tokens)
    if text:
        return text

    primary_error = last_error
    for fb in FALLBACK_MODELS:
        remaining = budget - (time.monotonic() - t0)
        if remaining < FALLBACK_MIN_SECONDS:
            print(f'[Gemini] 남은 시간 {remaining:.0f}초 — 예비 모델 시도 중단', flush=True)
            break
        print(f'[Gemini] {last_model} 실패({last_error}) → 예비 모델 {fb}로 1회 시도', flush=True)
        last_model = fb
        r = _post(fb, key, _payload(fb, prompt, max_output_tokens, temperature),
                  min(timeout, remaining), time.monotonic(), retry_until=0)
        text, last_error = _result(r, fb, key, max_output_tokens)
        if text:
            return text
    # 모두 실패하면 재시도 간격은 기본 모델의 실패 종류로 정한다
    last_error = primary_error
    return ''


def check_model(model: str | None = None) -> bool:
    """키와 모델이 쓸 수 있는지 모델 메타데이터 GET으로 확인한다 (generateContent 호출 아님)."""
    model = model or MODEL
    key = os.environ.get('GOOGLE_API_KEY', '').strip()
    if not key:
        print('[Gemini] GOOGLE_API_KEY 없음', flush=True)
        return False
    try:
        r = requests.get(f'{API_BASE}/models/{model}', headers={'x-goog-api-key': key}, timeout=15)
    except requests.RequestException as e:
        print(f'[Gemini] 요청 실패 ({model}): {type(e).__name__}', flush=True)
        return False
    if not r.ok:
        _log_http_error(r, key, model)
        return False
    try:
        limit = r.json().get('outputTokenLimit', '?')
    except ValueError:
        limit = '?'
    print(f'[Gemini] {model} 조회 성공 (출력 한도 {limit}토큰)', flush=True)
    return True


if __name__ == '__main__':
    # 새 키를 넣은 뒤 확인용: python gemini.py  (기본 모델, 예비 모델들 순서로 조회)
    ok = check_model()
    for fb in FALLBACK_MODELS:
        check_model(fb)   # 예비 모델은 없어도 기본 모델로 동작하므로 종료 코드에 넣지 않는다
    raise SystemExit(0 if ok else 1)
