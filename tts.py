"""
데일리 리포트 음성(Gemini TTS) — 리포트 전체를 한 번에 합성해 MP3와 사이드카 메타(json)로 저장한다.

- 대본: 도입 + '○ 번째 이야기. 제목. 본문' × 섹션. 섹션 사이에만 <long pause>(제목 뒤 쉼 태그는 넣지 않는다)
- 합성: generateContent 1회(TTS_MODEL, 기본 gemini-3.8-flash-tts, 목소리 Kore) → 실패하면 예비 모델 1회
- 섹션 경계: 글자 수 비례 추정 → ±4초 안 무음에 스냅(+'○ 번째 이야기.' 짧은 발화 패턴 가점) → 안 되면 추정값
- 인코딩: lameenc 48kbps CBR mono 24kHz (ffmpeg 불필요). lameenc는 선택 의존성이다 — requirements.txt에 넣지 않고
  pages.yml이 따로 설치한다(실패해도 크롤·배포는 계속, 음성만 encode_failed).
- 저장: AUDIO_DIR(기본 <repo>/.audio — Actions에서는 audio 브랜치 스냅숏)에 {date}.mp3 + {date}.json.
  데일리 JSON(data/daily)에는 아무것도 쓰지 않는다 — mp3와 메타가 audio 브랜치의 같은 커밋에 있어야
  main 커밋과 audio push 중 하나만 실패해도 서로 어긋나지 않는다.

근거(2026-09-24 스파이크, TTS 호출 6회 실측):
- 대본 859~1151자 → 음성 107~133초, 응답 28~36초. 말 속도 7.3~7.6자/초. 오디오 토큰은 초당 32개,
  출력 한도 16384토큰 → 한 번에 약 512초(약 3800자)까지.
- 섹션별 개별 호출은 섹션마다 말 속도·음량·음높이가 달라져 기각. <long pause> 실제 길이는 0.92~1.79초로
  들쭉날쭉해 무음 길이만으로는 경계를 못 찾는다 → 추정 + 스냅 + 패턴 가점(합성 3개 × 경계 6개 18/18).
- 3.8 TTS는 audio/wav(RIFF 헤더 포함)를 준다. 헤더를 PCM으로 쓰면 앞에 클릭음이 생긴다.
- 무료 RPM·RPD는 문서에 없다(AI Studio에만 표시) → 429 메시지의 limit 값을 로그로 남긴다.

키 값은 절대 출력하지 않는다.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import os
import re
import sys
import tempfile
import time
import unicodedata
import wave
from array import array
from datetime import datetime, timedelta, timezone

import requests

import gemini  # API_BASE, _api_error(429 일일 한도 판별) 공용

# .env를 모듈이 먼저 읽는다 — import 순서와 관계없이 TTS_MODEL 등이 반영되도록.
# 이미 설정된 환경변수(GitHub Actions secrets 등)는 덮어쓰지 않는다.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))
except ImportError:
    pass

ROOT = os.path.dirname(os.path.abspath(__file__))
KST = timezone(timedelta(hours=9))


def _env(name: str, default: str = '') -> str:
    # Actions의 ${{ vars.X }}는 미설정이면 ''로 들어온다 → 빈 문자열은 미설정으로 취급
    return os.environ.get(name, '').strip() or default


DEFAULT_MODEL = 'gemini-3.8-flash-tts'
DEFAULT_FALLBACK_MODEL = 'gemini-3.8-flash-lite-tts'   # 받아쓰기상 발음이 조금 덜 또렷해 예비로만
DEFAULT_VOICE = 'Kore'
MODEL = _env('TTS_MODEL', DEFAULT_MODEL).removeprefix('models/')
_fb = _env('TTS_FALLBACK_MODEL', DEFAULT_FALLBACK_MODEL).removeprefix('models/')
FALLBACK_MODEL = '' if _fb.lower() in ('none', 'off', '0') or _fb == MODEL else _fb
VOICE = _env('TTS_VOICE', DEFAULT_VOICE)
AUDIO_DIR = _env('AUDIO_DIR') or os.path.join(ROOT, '.audio')   # audio 브랜치 스냅숏 (.gitignore)

SCRIPT_VERSION = 1       # 대본 규칙이 바뀌면 올린다 → source_sha가 바뀌어 백필 창 안의 음성을 다시 만든다
MP3_KBPS = 48            # 40kbps부터 24kHz 전 대역 유지(32k 이하는 LAME이 리샘플·저역통과로 치찰음을 깎음)
KEEP_DAYS = 14           # audio 브랜치 보관 일수 (지나면 prune — 그 페이지는 브라우저 음성)
BACKFILL_DAYS = 7        # 음성이 없거나 옛 내용이면 다시 만드는 최근 일수
REQUEST_TIMEOUT = 150    # 초. 실측 28~36초 / 130초 음성
MAX_SCRIPT_CHARS = 3000  # 넘으면 생성하지 않음 (출력 16384토큰 ≈ 512초 ≈ 3800자)
CHARS_PER_SEC = 7.5      # 길이 검증용 (실측 7.3~7.6)
MIN_RATIO, MAX_RATIO = 0.6, 1.8   # 음성 길이 / 예상 길이 — 벗어나면 잘림·반복 루프로 보고 버린다

# 마지막 generate()/synthesize()의 실패 종류 ('' = 성공). build.py가 재시도 간격을 정할 때 쓴다.
#   'disabled' TTS_ENABLED 아님, 'no_key' 키 없음, 'too_long' 대본이 MAX_SCRIPT_CHARS 초과,
#   'transient' 5xx·네트워크 오류·타임아웃·분당 한도(429), 'quota_day' 일일 한도(RPD) 소진,
#   'http_4xx' 그 밖의 HTTP 오류, 'blocked' candidates 없음, 'bad_audio' finishReason·오디오·길이 이상,
#   'encode_failed' MP3 인코딩 실패(lameenc 없음 등), 'save_failed' 파일 저장 실패
last_error = ''
last_model = ''       # 마지막으로 결과를 받은(또는 시도한) 모델
last_latency = 0.0    # 마지막 합성 응답 시간(초)

RATE = 24000
_DATE_RE = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}')
_FILE_RE = re.compile(r'([0-9]{4}-[0-9]{2}-[0-9]{2})\.(?:mp3|json)')
_TMP_PREFIX = '.tmp-'


def _log(msg: str) -> None:
    print(msg, flush=True)


def _valid_date(date) -> bool:
    if not isinstance(date, str) or not _DATE_RE.fullmatch(date):
        return False
    try:
        datetime.strptime(date, '%Y-%m-%d')
        return True
    except ValueError:
        return False


def enabled() -> bool:
    """TTS_ENABLED == 'true'(Actions: audio 브랜치 fetch 성공 + push 이벤트 아님)이고 GOOGLE_API_KEY가 있는지.
    로컬 기본값은 미설정 → 생성하지 않는다(렌더링은 AUDIO_DIR에 파일이 있으면 싣는다)."""
    return (os.environ.get('TTS_ENABLED', '').strip().lower() == 'true'
            and bool(os.environ.get('GOOGLE_API_KEY', '').strip()))


# ── 대본 ─────────────────────────────────────────────────────────────────────
_ORD = ['첫', '두', '세', '네', '다섯', '여섯', '일곱', '여덟', '아홉', '열']
_COUNT = ['한', '두', '세', '네', '다섯', '여섯', '일곱', '여덟', '아홉', '열']
_LINK = re.compile(r'\[([^\]]*)\]\([^)]*\)')
_HEAD = re.compile(r'^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$')
_LIST = re.compile(r'^\s*(?:[-*+]|\d+[.)])\s+')
SECTION_PAUSE = '<long pause>'   # 공식 인라인 태그(speech-generation 문서). 제목 뒤 <short pause>는 넣지 않는다


def _clean(s: str) -> str:
    s = _LINK.sub(r'\1', s)
    s = re.sub(r'`+|\*\*|__|~~|\*', '', s)
    # 이모지·기호(So) + 변형 선택자·ZWJ 제거
    s = ''.join(ch for ch in s if unicodedata.category(ch) not in ('So', 'Cs', 'Co')
                and ch not in '️︎‍⃣')
    s = s.replace('·', ', ').replace('ㆍ', ', ')
    return re.sub(r'\s+', ' ', s).strip()


def _end(s: str) -> str:
    return s if not s or s[-1] in '.?!…' else s + '.'


def speech_script(summary_md: str, date: str) -> dict:
    """{'text': TTS 입력, 'blocks': [도입, 섹션1 낭독문, ...], 'titles': [정리된 H2 제목, ...]}
    blocks는 쉼 태그를 뺀 실제 낭독문 — 경계 추정의 글자 수 비율에 쓴다.
    titles[i]는 i번째 '## ' 섹션(= 페이지의 i번째 .summary-body h2)이다."""
    intro_lines, sections, cur = [], [], None
    for line in (summary_md or '').splitlines():
        m = _HEAD.match(line)
        if m and len(m.group(1)) == 2:
            cur = {'title': _clean(m.group(2)), 'body': []}
            sections.append(cur)
            continue
        text = _clean(_LIST.sub('', re.sub(r'^\s*>\s?', '', m.group(2) if m else line)))
        # '---'(구분선)처럼 글자·숫자가 없는 줄은 읽지 않는다
        if text and any(ch.isalnum() for ch in text):
            (cur['body'] if cur else intro_lines).append(_end(text))
    n = len(sections)
    cnt = _COUNT[n - 1] if 0 < n <= len(_COUNT) else str(n)
    _, mo, d = date.split('-')
    intro = f'{int(mo)}월 {int(d)}일 커뮤니티 데일리 리포트입니다. 모두 {cnt} 가지 이야기를 전해 드립니다.'
    if intro_lines:
        intro += ' ' + ' '.join(intro_lines)
    blocks, lines = [intro], [intro]
    for i, s in enumerate(sections):
        o = _ORD[i] if i < len(_ORD) else str(i + 1)
        block = f'{o} 번째 이야기. {_end(s["title"])} {" ".join(s["body"])}'.strip()
        blocks.append(block)
        lines += [SECTION_PAUSE, block]
    return {'text': '\n'.join(lines), 'blocks': blocks, 'titles': [s['title'] for s in sections]}


def source_sha(summary_md: str) -> str:
    """음성이 지금 리포트 내용·대본 규칙·목소리와 맞는지 가리는 값. 최종본으로 덮어쓰면 바뀐다."""
    return hashlib.sha256(f'{SCRIPT_VERSION}|{VOICE}|{(summary_md or "").strip()}'.encode()).hexdigest()[:16]


# ── 합성 ─────────────────────────────────────────────────────────────────────
def decode_audio(raw: bytes, mime: str) -> bytes:
    """inlineData → PCM s16le 24kHz mono. 3.8 계열은 audio/wav(RIFF 헤더), 2.5-preview는 audio/L16;rate=24000."""
    if raw[:4] == b'RIFF':
        with wave.open(io.BytesIO(raw)) as w:
            if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (1, 2, RATE):
                raise ValueError(f'예상 밖 형식: {w.getparams()}')
            return w.readframes(w.getnframes())
    m = (mime or '').lower()
    if 'l16' in m or 'pcm' in m:
        rate = re.search(r'rate=(\d+)', m)
        if rate and int(rate.group(1)) != RATE:
            raise ValueError(f'예상 밖 샘플레이트: {mime}')
        # L16은 빅엔디언이 표준이지만 Gemini는 리틀엔디언 PCM을 준다(공식 예제가 그대로 wav로 저장)
        return raw[: len(raw) // 2 * 2]
    raise ValueError(f'모르는 오디오 형식: {mime}')


def _payload(text: str) -> dict:
    return {'contents': [{'parts': [{'text': text}]}],
            'generationConfig': {
                'responseModalities': ['AUDIO'],
                'speechConfig': {'voiceConfig': {'prebuiltVoiceConfig': {'voiceName': VOICE}}}}}


def _redact(s: str, key: str) -> str:
    return s.replace(key, '***') if key else s


def _quota_detail(r: requests.Response) -> str:
    """429 본문에서 한도 정보를 뽑는다 — 무료 RPM·RPD가 문서에 없어서 로그로 실제 값을 알아낸다.
    메시지의 'limit: N, model: X'와 QuotaFailure의 quotaId·quotaValue."""
    try:
        err = r.json().get('error') or {}
    except ValueError:
        err = {}
    msg = str(err.get('message') or r.text[:500])
    found = [f'limit {n}' + (f' ({m})' if m else '')
             for n, m in re.findall(r'limit:\s*(\d+)(?:,\s*model:\s*([\w.\-]+))?', msg)]
    for d in err.get('details') or []:
        if str(d.get('@type', '')).endswith('QuotaFailure'):
            for v in d.get('violations') or []:
                q = v.get('quotaId') or v.get('quotaMetric') or '?'
                val = v.get('quotaValue')
                found.append(f'{q}' + (f'={val}' if val is not None else ''))
    return ', '.join(dict.fromkeys(found))


def _http_error(r: requests.Response, key: str, model: str) -> str:
    """HTTP 오류를 로그로 남기고 실패 종류를 돌려준다. 429는 gemini.py와 같은 방식(QuotaFailure PerDay)."""
    e = gemini._api_error(r)
    code = f' {e["status"]}' if e['status'] else ''
    if e['reason']:
        code += f'/{e["reason"]}'
    msg = _redact(e['message'][:300], key)
    if r.status_code == 429:
        kind = 'quota_day' if e['per_day'] else 'transient'
        quota = _redact(_quota_detail(r), key)
        hint = ('일일 무료 한도(RPD) 소진 - 태평양 자정(KST 16~17시)에 리셋' if e['per_day']
                else '요청 한도 초과(RPM/TPM)') + (f' [한도: {quota}]' if quota else '')
    elif r.status_code >= 500 or r.status_code == 408:
        kind, hint = 'transient', ''
    else:
        kind = 'http_4xx'
        hint = (f'모델 {model}을(를) 쓸 수 없음 — TTS_MODEL·TTS_FALLBACK_MODEL 확인' if r.status_code == 404
                else 'GOOGLE_API_KEY 무효' if e['reason'] == 'API_KEY_INVALID' else '')
    _log(f'[TTS] HTTP {r.status_code}{code} ({model}): {msg}' + (f' → {hint}' if hint else ''))
    return kind


def _call(model: str, key: str, text: str, expect_chars: int | None) -> tuple[bytes | None, str]:
    """generateContent 1회. (PCM, '') 또는 (None, 실패 종류). 검증: finishReason STOP, inlineData, 길이 비율."""
    url = f'{gemini.API_BASE}/models/{model}:generateContent'
    t0 = time.monotonic()
    try:
        r = requests.post(url, json=_payload(text), headers={'x-goog-api-key': key}, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        _log(f'[TTS] 요청 실패 ({model}): {type(e).__name__}: {_redact(str(e), key)[:300]}')
        return None, 'transient'
    latency = time.monotonic() - t0
    if not r.ok:
        return None, _http_error(r, key, model)
    try:
        j = r.json()
    except ValueError:
        _log(f'[TTS] 응답 JSON 파싱 실패 ({model})')
        return None, 'bad_audio'

    candidates = j.get('candidates') or []
    if not candidates:
        fb = j.get('promptFeedback') or {}
        _log(f'[TTS] candidates 없음 ({model}) - blockReason={fb.get("blockReason", "?")}')
        return None, 'blocked'
    c = candidates[0] if isinstance(candidates[0], dict) else {}
    finish = c.get('finishReason', '')
    usage = j.get('usageMetadata') or {}
    if finish != 'STOP':
        _log(f'[TTS] finishReason={finish or "?"} ({model}) - 결과 버림 '
             f'(출력 {usage.get("candidatesTokenCount", "?")}토큰)')
        return None, 'bad_audio'
    parts = (c.get('content') or {}).get('parts') or []
    datas = [p['inlineData'] for p in parts
             if isinstance(p, dict) and isinstance(p.get('inlineData'), dict) and p['inlineData'].get('data')]
    if not datas:
        _log(f'[TTS] inlineData 없음 ({model}, finishReason=STOP)')
        return None, 'bad_audio'
    try:
        pcm = b''.join(decode_audio(base64.b64decode(d['data']), d.get('mimeType', '')) for d in datas)
    except (ValueError, TypeError, EOFError, wave.Error) as e:
        _log(f'[TTS] 오디오 디코드 실패 ({model}): {type(e).__name__}: {e}')
        return None, 'bad_audio'

    dur = len(pcm) / 2 / RATE
    if expect_chars:
        expected = expect_chars / CHARS_PER_SEC
        ratio = dur / expected if expected else 0
        if not (MIN_RATIO <= ratio <= MAX_RATIO):
            _log(f'[TTS] 음성 길이 이상 ({model}): {dur:.1f}초 / 예상 {expected:.0f}초 (비율 {ratio:.2f}, '
                 f'허용 {MIN_RATIO}~{MAX_RATIO}) — 잘림·반복으로 보고 버림')
            return None, 'bad_audio'
    elif dur <= 0:
        _log(f'[TTS] 빈 오디오 ({model})')
        return None, 'bad_audio'
    _log(f'[TTS] {model} 합성 {dur:.1f}초 ({latency:.1f}s, 오디오 {usage.get("candidatesTokenCount", "?")}토큰)')
    return pcm, ''


def synthesize(text: str, expect_chars: int | None = None) -> tuple[bytes, str] | None:
    """(PCM s16le 24kHz mono, 쓴 모델) 또는 None. MODEL 1회 → 실패하면 FALLBACK_MODEL 1회.
    expect_chars(쉼 태그 뺀 낭독 글자 수)를 주면 음성 길이를 검증해 이상하면 예비 모델로 넘어간다.
    실패 종류는 last_error — 둘 다 실패하면 기본 모델의 것(재시도 간격은 기본 모델 기준)."""
    global last_error, last_model, last_latency
    last_error, last_latency = '', 0.0
    last_model = MODEL
    key = os.environ.get('GOOGLE_API_KEY', '').strip()
    if not key:
        _log('[TTS] GOOGLE_API_KEY 없음 - 합성 건너뜀')
        last_error = 'no_key'
        return None
    primary_error = ''
    for i, model in enumerate([MODEL] + ([FALLBACK_MODEL] if FALLBACK_MODEL else [])):
        if i:
            _log(f'[TTS] {MODEL} 실패({primary_error}) → 예비 모델 {model}로 1회 시도')
        last_model = model
        t0 = time.monotonic()
        pcm, err = _call(model, key, text, expect_chars)
        last_latency = round(time.monotonic() - t0, 1)
        if pcm is not None:
            last_error = ''
            return pcm, model
        if i == 0:
            primary_error = err
    last_error = primary_error
    return None


# ── 섹션 경계 ────────────────────────────────────────────────────────────────
SIL_DB = -45.0    # 10ms 프레임 RMS가 이보다 작으면 무음 (TTS 무음 바닥은 약 -80dBFS)
MERGE_GAP = 0.12  # 숨소리·클릭(≤120ms)으로 끊긴 무음은 합친다
MIN_SIL = 0.5     # 경계 후보 무음 최소 길이(초)
WINDOW = 4.0      # 추정 시각 ±초 안에서만 스냅
LEAD = 0.3        # 섹션 시작 = 무음 끝 - LEAD (말 시작 직전으로 이동)
PATTERN_BONUS = 1.5


def _silences(pcm: bytes) -> list[tuple[float, float]]:
    """100ms 이상 무음 구간 [(시작초, 끝초)]. 120ms 이하 소리로 끊긴 무음은 하나로 합친다."""
    a = array('h')
    a.frombytes(pcm[: len(pcm) // 2 * 2])
    if a.itemsize != 2:
        raise RuntimeError('array("h") != 16bit')
    if sys.byteorder == 'big':
        a.byteswap()
    f, res, start = 240, [], None
    n = len(a) // f
    for i in range(n + 1):
        if i < n:
            seg = a[i * f:(i + 1) * f]
            ms = sum(x * x for x in seg) / f
            quiet = ms <= 0 or 20 * math.log10(math.sqrt(ms) / 32768) < SIL_DB
        else:
            quiet = False
        if quiet and start is None:
            start = i
        elif not quiet and start is not None:
            if i - start >= 10:          # 100ms 이상
                s, e = start / 100, i / 100
                if res and s - res[-1][1] <= MERGE_GAP:
                    res[-1] = (res[-1][0], e)
                else:
                    res.append((s, e))
            start = None
    return res


def _estimate(total: float, blocks: list[str]) -> list[float]:
    """블록 글자 수 누적 비율 × 전체 길이 — 섹션 1..N 시작 추정값."""
    w = [len(b) for b in blocks]
    T = sum(w) or 1
    est, cum = [], 0
    for x in w[:-1]:
        cum += x
        est.append(cum / T * total)
    return est


def find_section_starts(pcm: bytes, blocks: list[str]) -> tuple[list[float], str]:
    """섹션 1..N 시작 초(도입부 다음부터, N = len(blocks)-1)와 방법.
    'snap' 모두 무음에 맞춤, 'partial' 일부만, 'estimate' 하나도 못 맞춰(또는 순서가 꼬여) 전부 추정값."""
    total = len(pcm) / 2 / RATE
    est = _estimate(total, blocks)
    sil = [s for s in _silences(pcm) if s[0] > 0.05 and s[1] < total - 0.05]
    cands = [s for s in sil if s[1] - s[0] >= MIN_SIL]
    starts, prev, snapped = [], 0.0, 0
    for e in est:
        best, best_score = None, None
        for s in cands:
            if s[1] <= prev or abs(s[1] - e) > WINDOW:
                continue
            score = abs(s[1] - e)
            # 경계 뒤에는 '○ 번째 이야기.'(0.4~1.4초) 다음 짧은 무음(≥0.2초)이 온다
            nxt = next((t for t in sil if t[0] > s[1] + 0.05), None)
            if nxt and 0.4 <= nxt[0] - s[1] <= 1.4 and nxt[1] - nxt[0] >= 0.2:
                score -= PATTERN_BONUS
            if best_score is None or score < best_score:
                best, best_score = s, score
        if best:
            starts.append(round(max(best[0], best[1] - LEAD), 2))
            prev = best[1]
            snapped += 1
        else:
            starts.append(round(max(e, prev), 2))
    if any(b <= a for a, b in zip(starts, starts[1:])):
        return [round(e, 2) for e in est], 'estimate'
    # 하나도 못 맞췄으면 전부 추정값 — 'partial'과 구분해 모델 운율 변화 같은 전면 실패를 로그에서 보이게 한다
    return starts, 'snap' if snapped == len(est) else ('partial' if snapped else 'estimate')


# ── MP3 ──────────────────────────────────────────────────────────────────────
def encode_mp3(pcm: bytes, kbps: int = MP3_KBPS) -> bytes:
    """CBR mono. 24kHz 입력에서 40kbps 이상이어야 LAME이 24kHz·전대역을 유지한다(32k↓는 리샘플+저역통과).
    CBR이라 Xing 헤더 없이도 브라우저의 길이 계산·탐색이 정확하다."""
    import lameenc
    enc = lameenc.Encoder()
    enc.set_bit_rate(kbps)
    enc.set_in_sample_rate(RATE)
    enc.set_channels(1)
    enc.set_quality(2)
    return bytes(enc.encode(pcm) + enc.flush())


# ── 저장소(AUDIO_DIR) ────────────────────────────────────────────────────────
def mp3_path(date: str) -> str:
    return os.path.join(AUDIO_DIR, f'{date}.mp3')


def _meta_path(date: str) -> str:
    return os.path.join(AUDIO_DIR, f'{date}.json')


def read_meta(date: str) -> dict | None:
    """AUDIO_DIR/{date}.json (사이드카 메타) 또는 None."""
    if not _valid_date(date):
        return None
    try:
        with open(_meta_path(date), encoding='utf-8') as f:
            d = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        _log(f'[TTS] {date}.json 읽기 실패: {type(e).__name__}: {e}')
        return None
    return d if isinstance(d, dict) else None


def is_current(meta: dict | None, summary_md: str) -> bool:
    """메타가 지금 리포트 내용과 맞고(source_sha) {date}.mp3가 온전한지(크기 == meta['bytes'])."""
    if not isinstance(meta, dict) or not _valid_date(meta.get('date')):
        return False
    if meta.get('source_sha') != source_sha(summary_md):
        return False
    size = meta.get('bytes')
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        return False
    try:
        return os.path.getsize(mp3_path(meta['date'])) == size
    except OSError:
        return False


def _write_tmp(data: bytes) -> str:
    fd, tmp = tempfile.mkstemp(dir=AUDIO_DIR, prefix=_TMP_PREFIX)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return tmp


def _save(date: str, mp3: bytes, meta: dict) -> None:
    """임시 파일 → os.replace. 메타를 먼저 지우고 mp3 → 메타 순으로 바꾼다 — 중간에 죽으면 메타가 없거나
    크기가 안 맞아 is_current가 거짓이 된다(다음 실행이 다시 만든다). 임시 파일은 남기지 않는다."""
    os.makedirs(AUDIO_DIR, exist_ok=True)
    tmps = []
    try:
        tmps.append(_write_tmp(mp3))
        body = json.dumps(meta, ensure_ascii=False, indent=1) + '\n'
        tmps.append(_write_tmp(body.encode('utf-8')))
        try:
            os.remove(_meta_path(date))
        except FileNotFoundError:
            pass
        os.replace(tmps[0], mp3_path(date))
        os.replace(tmps[1], _meta_path(date))
    finally:
        for t in tmps:
            try:
                os.remove(t)
            except OSError:
                pass


def generate(date: str, summary_md: str, now: datetime) -> dict | None:
    """대본 → 합성 → 검증 → 경계 → MP3 → AUDIO_DIR/{date}.mp3·{date}.json 저장. 메타 dict를 돌려준다.
    실패하면 None(기존 파일은 그대로)이고 원인은 last_error."""
    global last_error
    last_error = ''
    if not _valid_date(date):
        raise ValueError(f'잘못된 날짜: {date!r}')
    if not enabled():
        last_error = ('no_key' if os.environ.get('TTS_ENABLED', '').strip().lower() == 'true'
                      else 'disabled')
        _log(f'[TTS] {date} 생성 안 함 ({last_error})')
        return None

    script = speech_script(summary_md, date)
    if len(script['text']) > MAX_SCRIPT_CHARS:
        last_error = 'too_long'
        _log(f'[TTS] {date} 대본 {len(script["text"])}자 > {MAX_SCRIPT_CHARS}자 — 생성하지 않음')
        return None
    try:   # 인코더가 없으면 TTS 호출(무료 한도)을 쓰기 전에 멈춘다
        import lameenc  # noqa: F401
    except ImportError:
        last_error = 'encode_failed'
        _log('[TTS] lameenc 없음 (pip install lameenc — 선택 의존성) — 생성하지 않음')
        return None

    chars = sum(len(b) for b in script['blocks'])
    _log(f'[TTS] {date} 합성 시작 (대본 {len(script["text"])}자, 섹션 {len(script["titles"])}개, '
         f'{MODEL}/{VOICE})')
    res = synthesize(script['text'], expect_chars=chars)
    if res is None:
        return None
    pcm, model = res
    duration = round(len(pcm) / 2 / RATE, 2)

    t0 = time.monotonic()
    try:
        starts, boundary = find_section_starts(pcm, script['blocks'])
    except Exception as e:  # 경계를 못 찾아도 음성은 쓴다 — 추정값(섹션 이동이 조금 어긋날 뿐)
        _log(f'[TTS] {date} 경계 분석 오류 {type(e).__name__}: {e} — 추정값 사용')
        starts, boundary = [round(x, 2) for x in _estimate(duration, script['blocks'])], 'estimate'
    t_bound = time.monotonic() - t0

    t0 = time.monotonic()
    try:
        mp3 = encode_mp3(pcm)
    except Exception as e:
        last_error = 'encode_failed'
        _log(f'[TTS] {date} MP3 인코딩 실패: {type(e).__name__}: {e}')
        return None
    t_enc = time.monotonic() - t0

    meta = {
        'date': date,
        'source_sha': source_sha(summary_md),
        'script_version': SCRIPT_VERSION,
        'model': model,
        'voice': VOICE,
        'duration': duration,
        'bytes': len(mp3),
        'kbps': MP3_KBPS,
        'sections': [{'title': t, 'start': s} for t, s in zip(script['titles'], starts)],
        'boundary': boundary,
        'generated_at': now.astimezone(KST).isoformat(timespec='seconds'),
        'latency': last_latency,
    }
    try:
        _save(date, mp3, meta)
    except OSError as e:
        last_error = 'save_failed'
        _log(f'[TTS] {date} 저장 실패: {type(e).__name__}: {e}')
        return None
    _log(f'[TTS] {date} 저장: {mp3_path(date)} ({len(mp3) // 1024}KB, {duration:.1f}초, 경계 {boundary} '
         f'{starts}, 분석 {t_bound:.2f}s·인코딩 {t_enc:.2f}s)')
    return meta


def prune(today: str) -> list[str]:
    """date < today-(KEEP_DAYS-1)인 {date}.mp3·{date}.json을 지우고 지운 날짜를 돌려준다.
    중간에 죽은 실행의 임시 파일(.tmp-*)도 지운다(audio 브랜치에 올라가지 않게)."""
    cutoff = (datetime.strptime(today, '%Y-%m-%d') - timedelta(days=KEEP_DAYS - 1)).strftime('%Y-%m-%d')
    try:
        names = os.listdir(AUDIO_DIR)
    except FileNotFoundError:
        return []
    removed = set()
    for name in names:
        path = os.path.join(AUDIO_DIR, name)
        m = _FILE_RE.fullmatch(name)
        if name.startswith(_TMP_PREFIX) or (m and m.group(1) < cutoff):
            try:
                os.remove(path)
            except OSError as e:
                _log(f'[TTS] {name} 삭제 실패: {type(e).__name__}: {e}')
                continue
            if m:
                removed.add(m.group(1))
    return sorted(removed)


def public_audio(date: str, summary_md: str, base: str) -> dict | None:
    """템플릿용 summary.audio — 지금 리포트 내용과 맞는 음성(is_current)일 때만, 아니면 None.
    sections[i] ↔ 페이지의 i번째 .summary-body h2 (개수가 다르면 플레이어가 섹션 이동을 끄고 재생만 한다)."""
    meta = read_meta(date)
    if not meta or meta.get('date') != date or not is_current(meta, summary_md):
        return None
    sections = []
    for s in meta.get('sections') or []:
        start = s.get('start') if isinstance(s, dict) else None
        if (not isinstance(start, (int, float)) or isinstance(start, bool)
                or not math.isfinite(start) or start < 0):
            sections = []   # 하나라도 깨졌으면 섹션 정보 없이 재생만
            break
        sections.append({'title': str(s.get('title') or ''), 'start': round(float(start), 2)})
    duration = meta.get('duration')
    if not isinstance(duration, (int, float)) or isinstance(duration, bool) or not math.isfinite(duration):
        duration = 0
    return {
        'url': f'{base}/audio/{date}.mp3?v={meta["source_sha"][:8]}',   # CDN이 쿼리를 무시해도 브라우저 캐시는 갱신
        'duration': round(float(duration), 2),
        'sections': sections,
        'model': str(meta.get('model') or ''),
        'voice': str(meta.get('voice') or ''),
    }
