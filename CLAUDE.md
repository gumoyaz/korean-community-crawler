# CLAUDE.md — 커트 (커뮤니티 트렌드)

한국 인터넷 커뮤니티 인기글을 모아 보여주는 **정적 사이트**. GitHub Actions가 약 10분마다 `build.py`를 실행해 크롤링·렌더링하고 GitHub Pages에 배포한다. 상시 서버는 없다.

운영 주소: https://gumoyaz.github.io/korean-community-crawler/ (프로젝트 페이지라 서브경로가 있음)

## 핵심 파일

| 파일 | 역할 |
|---|---|
| `build.py` | 진입점. 상태 복원 → 간격 가드 → 크롤 → 데일리 생성 → 데일리 음성 → `_site/` 렌더링 (sitemap·robots·llms.txt·404·audio 포함) |
| `tts.py` | 데일리 음성(Gemini TTS). 대본 → 합성 1회(+예비 모델) → 섹션 경계 → MP3(lameenc) → `AUDIO_DIR`(기본 `.audio/`)에 `{date}.mp3`·`{date}.json` |
| `crawler.py` | `TrendCrawler`: todaybeststory API 전량 수집 + 직접 스크래핑 예비 경로, 랭킹, 이슈 블록·튜닝 로그 반영, `export_state`/`import_state` |
| `issues.py` | '지금 뜨는 이슈' 계산. `build_issues(posts, ps_history, now)` → trends.json의 `issues` 블록. 손으로 관리하는 단어 목록(`STOP_WORDS`·`GENERIC`·`LINK_STOP`·`ALIAS` 등)은 모듈 상단 상수 |
| `daily.py` | 데일리 리포트 선정·프롬프트·저장 (`data/daily/YYYY-MM-DD.json`) |
| `gemini.py` | Gemini REST 호출. 모델 세대별 thinking 설정, 5xx 백오프, 예비 모델 체인(`GEMINI_FALLBACK_MODEL`, 쉼표 구분) |
| `templates/index.html` | 메인. JS가 `{base}/data/trends.json`을 읽어 이슈 보드와 피드를 렌더링 |
| `templates/daily.html` | 데일리 목록·상세·대기 페이지 (빌드 시 서버 렌더링). 상세의 읽어주기 플레이어(음성 파일 재생, 실패하면 브라우저 음성) |
| `.github/workflows/pages.yml` | 빌드·배포 워크플로 |
| `tools/import_daily_db.py` | 옛 SQLite `daily.db` → JSON 변환 |
| `data/daily/` | **커밋되는** 데일리 리포트 아카이브 (Actions 봇이 커밋) |

## 실행 흐름 (pages.yml)

1. 트리거: cron-job.org의 `workflow_dispatch`(10분), `schedule`(백업), `main` push(크롤 없이 `--no-crawl` 렌더링만)
2. `Fetch audio`(checkout 바로 뒤): `audio` 브랜치(음성 스냅숏)를 얕게 받아 `.audio/`에 푼다. 브랜치가 없으면(`ls-remote` 종료 코드 2) 첫 실행으로 보고 `base=''`로 계속한다. 원격 접근 실패 등 그 밖의 실패면 `ok`가 없어 TTS·publish가 꺼진다(렌더링은 계속). 브랜치 확인은 `ls-remote origin refs/heads/audio`(전체 이름)로 한다. `--heads origin audio`는 이름 끝 일치라서 `feature/audio`가 있으면 fetch가 실패한다
3. `Install dependencies` 다음 `Install audio encoder`: `lameenc`(선택 의존성)는 `requirements.txt`가 아니라 이 스텝에서 휠로만 설치한다(`continue-on-error`). 실패해도 크롤·배포는 계속하고, 음성은 TTS 호출 전에 `encode_failed`로 멈춘다
4. `actions/cache`로 직전 `state.json` 복원. 캐시가 없으면 `STATE_URL`(사이트의 `data/state.json`), 404면 빈 상태로 시작
5. `build.py`가 `GITHUB_OUTPUT`에 `skip`, `daily_created`, `daily_date`, `audio_changed`, `audio_dates`를 쓴다. `TTS_ENABLED`는 `Fetch audio` 성공이고 push 이벤트가 아닐 때만 `true`다
6. `skip != true`이면 state를 캐시에 저장하고 `_site` 업로드 → deploy 잡이 Pages에 배포
7. `daily_created == true`이면 `data/daily/`만 커밋·푸시 (봇 정체성, `pull --rebase` 후 push)
8. `audio_changed == true`이면 `Publish audio`(마지막 스텝): `.audio/`를 부모 없는 커밋 1개로 만들어 `audio` 브랜치를 교체한다(`--force-with-lease`, 실패해도 배포는 진행). push 트리거가 `main`만 보므로 이 push는 워크플로를 다시 띄우지 않는다

## 주요 설계 결정

- **정적 사이트 + 배치 크롤**: Railway 무료 기간이 끝나서 0원 구조로 옮겼다(2026-09). 서버 API(`/api/*`)는 없다. 수동 새로고침은 JSON을 다시 읽기만 한다.
- **state 이어받기**: 실행마다 새 프로세스라서 점수 이력(`post_score_history`, 6회), 직전 글 목록, AI 요약 시각, 이슈 튜닝 로그(`issue_log`)를 `state.json`으로 넘긴다. 이슈 블록은 저장하지 않고 복원할 때 같은 입력으로 다시 계산한다. 옛 state의 단어 이력(`history`)은 읽지 않는다. state를 **받지 못하면(404 제외) 실패로 끝내서** 빈 사이트가 배포되지 않게 한다.
- **지금 뜨는 이슈 (`issues.py`)**: 옛 키워드 영역(급상승 띠·급상승 키워드·단어 구름)을 대체했다(2026-09-24). 제목만 보고 규칙으로 묶으며 LLM·형태소 분석기는 쓰지 않는다. 블록은 제목을 복사하지 않고 `posts[].rank`로 글을 가리킨다. 보드에는 2곳 이상 커뮤니티에 걸친 묶음만 최대 8개 싣는다. 상태 배지와 `+N 새 글`은 점수 이력이 3라운드 이상일 때부터 나온다. 규칙 요약은 README의 '지금 뜨는 이슈'에 있다.
  - **결과가 실행마다 같아야 한다.** 매 실행이 새 프로세스라 해시 시드가 바뀐다. set 순회 순서를 결과에 쓰지 말고, 동점이면 묶음 안에서 먼저 나온 것을 고른다. 이슈 id(가장 먼저 보인 글의 URL 해시)로 프론트가 갱신 사이에 펼침·필터를 유지한다.
  - 계산이 실패하면 `crawler._empty_issues`로 빈 블록을 내고 빌드는 계속한다. `LOW_DATA_POSTS`(350)는 빈 블록도 같은 기준을 쓰도록 `crawler.py`에 두고 `issues.py`가 가져다 쓴다. `issues.py`가 `crawler`를 import하므로 `crawler`는 `_build_issues` 안에서 `issues`를 import한다(순환 import 방지).
  - 실험 시안을 옮기면서 두 가지를 고쳤다. (1) 한 커뮤니티 안에서만 퍼나른 글은 4개 이상이어도 보드에 올리지 않는다(연재물·경기 중계처럼 제목이 비슷한 글이 한 베스트에 몰린 경우). (2) '런던'은 어미 규칙('~던')에서 보호하고 `GENERIC`에도 넣었다.
  - 프론트(`templates/index.html`): 펼침·선택·'모두 보기' 필터를 이슈 id로 갱신 사이에 유지한다. 필터 중이던 이슈가 빠지면 마지막으로 본 글 주소로 계속 거르고, 보던 이슈가 빠지면 보드에 알린다(조용히 1위로 바꾸지 않는다). 이슈 필터 중에는 탭·칩 숫자도 그 이슈 글로 센다. 머리줄 기준 시각은 배지와 같은 `last_updated`를 쓴다. `issues.as_of`는 원본 목록이 그대로여도 실행마다 바뀌어서 `last_updated`가 없을 때만 쓴다.
- **stale 처리**: 수집이 0건이거나 커뮤니티 수가 급감하면 이전 글을 유지하고 `status: 'stale'`로 둔다. 이 경우 이력에는 추가하지 않는다.
- **시간대 계약**: `post.date`는 `YYYY-MM-DDTHH:MM:SS+09:00` / `YYYY-MM-DD`(날짜만) / `''` 셋 중 하나다. KST 시각을 `Z`로 저장하지 않는다.
- **데일리 타이밍**: KST 12시 이전에는 만들지 않는다(정오본). 다음 날 00:10~01:59에 하루 전체 목록으로 최종본을 덮어쓴다. 실패하면 다음 실행에서 재시도한다.
- **데일리 프롬프트**: 입력(제목·본문)에 있는 사실만 쓴다. 소속·직함·반응을 추측하면 사실 오류가 난 적이 있다(2026-09-23).
- **데일리 읽어주기 (`templates/daily.html`)**: 상세 record에 `summary.audio`(`{url, duration, sections[{title, start}], model, voice}`)가 있으면 `<audio>`로 재생하고, 없거나 재생에 실패하면 Web Speech로 읽는다. 동작 설명은 README의 '읽어주기'에 있다.
  - `build.py`가 `tts.public_audio()`로 `summary.audio`를 넣는다. `AUDIO_DIR`에 지금 리포트 내용과 맞는(`source_sha` 일치, mp3 크기 == 메타 `bytes`) 음성이 있을 때만 넣고 `_site/audio/`로 복사한다. 옛 내용의 음성(최종본으로 바뀌기 전 정오본 음성)은 싣지 않는다.
  - `sections[i]`는 `.summary-body` 바로 아래 i번째 `h2`와 짝이다. 개수가 다르거나 시각이 오름차순이 아니면 섹션 이동·강조를 끄고 재생만 한다.
  - 음성 정보는 `<script type="application/json" id="ttsAudioData">{{ summary.audio | tojson }}</script>`로 넣는다(`tojson`이 `<`·`>`·`&`·`'`를 이스케이프한다). 값이 있으면 JSON-LD에 `AudioObject`도 넣는다.
  - 사용자가 멈췄는지는 `aWant`로 따로 기억한다. 크롬은 오류로 멈출 때 페이지의 `error` 처리보다 먼저 `audio.paused`를 true로 바꾸기 때문이다. 재생 중 실패는 그 섹션부터 브라우저 음성으로 바로 이어 읽는다. 일시정지 중 실패는 소리 없이 기다렸다가(`speechFrom`) 다음 ▶에 읽는다.
  - 브라우저 음성은 기기마다 다르다. Windows 기본 Heami는 기계음에 가깝고, rate를 1.5로 올려도 약 10%만 빨라졌다(실측). 음성 파일 경로를 따로 둔 이유다.
- **소스 간 원시 조회수 비교 금지**: FM코리아 조회수는 API의 합성값이라 0으로 둔다. 선정은 소스별로 정규화된 `rank_score`로 한다.
- **보안**: 데일리 HTML은 `markdown` → `nh3`로 정화한다. 프론트는 외부 텍스트에 `escHtml`, 링크에 `safeUrl`(http/https만)을 쓴다.
- **내부 링크는 `{{ base }}`로 시작**한다. base는 `SITE_URL`의 경로(`/korean-community-crawler`)에서 나온다.

## 환경변수

README의 표 참고. 핵심은 `GOOGLE_API_KEY`(Secret), `GEMINI_MODEL`, `GEMINI_FALLBACK_MODEL`, `SITE_URL`, `GSC_VERIFICATION`(Variables)이다. 음성은 `TTS_MODEL`, `TTS_FALLBACK_MODEL`, `TTS_VOICE`(Variables, 비우면 기본값)와 `AUDIO_DIR`을 쓴다. `TTS_ENABLED`는 pages.yml이 계산한다(audio fetch 성공이고 push 이벤트가 아닐 때만 `true`). Actions에서 비어 있는 vars는 `''`로 들어오는데, `build.py`가 import 전에 지워서 기본값을 쓰게 한다.

## 로컬 개발

```bash
pip install -r requirements.txt
python build.py --base "" --out _site --no-crawl     # 운영 state로 렌더링만
python build.py --base "" --out _site --force        # 직접 크롤
python -m http.server -d _site 8000
```

- 테스트할 때는 `DAILY_DIR`을 임시 폴더로 두어 `data/daily/`를 오염시키지 않는다. 음성도 `AUDIO_DIR`을 임시 폴더로 둔다.
- 로컬 기본값은 `TTS_ENABLED` 미설정, 곧 **음성을 만들지 않는다**. 렌더링은 `AUDIO_DIR`에 맞는 음성이 있으면 싣는다. `--no-crawl`이면 `TTS_ENABLED=true`여도 만들지 않는다. 음성을 직접 만들어 볼 때만 `pip install lameenc==1.8.4`를 따로 설치한다(선택 의존성, pages.yml과 같은 버전).
- Windows에서는 `PYTHONUTF8=1`로 실행한다(콘솔 인코딩).
- `_site/`, `.state/`, `.audio/`, `.env`, `*.db`는 gitignore 대상이다.

## 운영 메모

- 수동 실행: Actions → Build and deploy → Run workflow. "간격 가드 무시"를 체크하면 바로 크롤한다.
- 외부 트리거: cron-job.org가 10분마다 `POST /repos/gumoyaz/korean-community-crawler/actions/workflows/pages.yml/dispatches`를 호출한다.
  - 헤더에 `User-Agent`가 없으면 GitHub가 403을 준다.
  - 토큰은 이 리포만 선택한 fine-grained PAT이고, 권한은 Actions: Read and write다. 만료되면 GitHub에서 재발급해 cron-job.org 헤더를 교체한다. 교체하기 전까지는 백업 `schedule`만 돈다.
- Gemini 무료 한도는 모델마다 따로 있고, 태평양 자정(KST 16~17시)에 리셋된다. 3.8 Flash는 503(high demand)이 잦아서 예비 모델 체인이 자주 쓰인다.
- 상태 확인: `data/state.json`의 `last_run`, `daily_failed_at`과 `data/trends.json`의 `status`, `crawl_count`를 본다.
- 이슈 품질 확인: `data/state.json`의 `crawler.issue_log`에 결과가 바뀐 빌드의 이슈 요약(`t, posts, communities, items[id,name,kind,status,n,c]`)이 최대 72건·30KB까지 쌓인다. 한 건이 약 0.9KB라서 실제로는 30KB 상한에 먼저 걸린다(약 30건). 크롤 없는 푸시 빌드(`--no-crawl`)는 기록하지 않는다.
- 음성 확인: 빌드 로그의 `[TTS]` 줄(합성 길이·응답 시간·경계 방법, 429면 `[한도: ...]`), `data/state.json`의 `tts`(`failed_at, error, day, attempts, date, pending, publish_fails`), `audio` 브랜치의 `YYYY-MM-DD.json`을 본다.
  - 경계 방법: `snap`은 모두 무음에 맞춘 것, `partial`은 일부만 맞춘 것, `estimate`는 하나도 못 맞춰 전부 글자 수 추정값인 것이다. `estimate`가 이어지면 모델 운율이 바뀐 것일 수 있다.
  - `tts.error == 'publish_failed'`이면 직전 실행이 만든 음성이 `audio` 브랜치에 올라가지 않은 것이다. `Publish audio` 스텝 로그와 브랜치 규칙을 확인한다.
  - `tts.error`의 다른 값: `transient`(5xx·타임아웃·분당 한도 429, 30분 뒤), `quota_day`(일일 한도 429, 180분 뒤), `bad_audio`·`blocked`(그 날짜를 뒤로 미룸), `encode_failed`(`lameenc` 설치 실패 — TTS는 호출하지 않음), `http_4xx`(모델 이름·키 확인).
- `audio` 브랜치 다루기: main과 관계없는 고아 브랜치라 main에 머지하지 않는다. 내용은 `git fetch origin audio` 뒤 `git show origin/audio:YYYY-MM-DD.json`으로 본다.
  - 특정 날짜 음성을 다시 만들려면 `audio` 브랜치에서 그 날짜 파일을 지우는 커밋을 올린다. 다음 실행이 받아서 없는 것으로 보고 다시 만든다(최근 7일 리포트만). 최근 7일을 모두 다시 만들려면 `tts.SCRIPT_VERSION`을 올린다.
  - 브랜치를 통째로 지우면 다음 실행이 첫 실행처럼 새로 만든다. 최근 7일 리포트를 한 실행에 1개씩 다시 합성하고(KST 하루 최대 6회), 8~14일 전 음성은 사라진다.
  - 음성 생성만 끄는 Variables 스위치는 없다. 끄려면 pages.yml의 `TTS_ENABLED` 식을 `false`로 바꾼다. 이미 `audio` 브랜치에 있는 음성은 계속 실린다(14일 정리도 멈춘다).

## 데일리 음성 파일(Gemini TTS)

**상태: 구현 완료(2026-09-25), Actions 첫 운영 실행 확인 전**(아래 '앞으로 할 것'). 2026-09-24 스파이크(TTS 호출 6회)로 정하고 구현했다(`tts.py`, `build.py`의 `_maybe_generate_audio`·`_site_audio`, `pages.yml`의 `Fetch audio`·`Install audio encoder`·`Publish audio`, `templates/daily.html` 플레이어). 같은 날 실제 TTS 1회로 끝까지 확인했다: 9/24 정오본(대본 1067자) → 130.8초 음성, 응답 33.4초, 경계 `snap`(6개 모두 '○ 번째 이야기' 패턴), MP3 785,088바이트(약 0.8MB). 로컬 베어 리포를 `audio` 원격으로 써서 fetch → 생성 → publish(부모 없는 커밋 1개) → 다음 실행이 받은 음성을 최신으로 보고 호출하지 않는 것까지 봤다. 2026-09-25에는 TTS를 HTTP 층에서 가짜로 두고(실측 WAV 응답) pages.yml에서 뽑은 스텝 본문으로 첫 운영 순서(브랜치 생성 → 백필 교체 push → 모두 최신 → push 이벤트 렌더)와 헤드리스 크롬 375·1280 재생을 다시 확인했다.

- **모델·목소리**: `gemini-3.8-flash-tts`, `Kore`. 예비는 `gemini-3.8-flash-lite-tts`(받아쓰기상 발음이 조금 덜 또렷함). generateContent에 `responseModalities: ["AUDIO"]`, `speechConfig.voiceConfig.prebuiltVoiceConfig.voiceName`을 쓴다. 3.8은 `audio/wav`(RIFF 헤더 포함, 24kHz mono 16bit)를 돌려준다. RIFF 헤더를 PCM으로 쓰면 앞에 클릭음이 생긴다.
- **한 번에 합성**: 리포트 전체를 1회 호출로 만든다. 대본 859~1151자가 107~133초 음성이 됐고, 응답은 28~36초 걸렸다. 오디오 토큰은 초당 32개이고 출력 한도가 16384토큰이라, 최대 약 512초(약 3800자)까지 된다. 섹션마다 따로 호출하면 말 속도·음량·음높이가 섹션마다 달라져서 기각했다.
- **섹션 경계**: 대본은 섹션마다 '○ 번째 이야기. 제목. 본문'이고, 섹션 사이에만 `<long pause>`를 넣는다. 글자 수 비례로 추정한 뒤 ±4초 안의 무음에 맞추고, '○ 번째 이야기' 패턴이면 가점을 준다. 스파이크 합성 3개에서 경계 18개가 모두 맞았다. `<long pause>`의 실제 길이(0.92~1.79초)가 들쭉날쭉해서 무음 길이만으로는 못 찾는다.
- **MP3**: `lameenc` 48kbps CBR mono 24kHz. ffmpeg가 필요 없다(ubuntu-latest 러너에는 기본 설치가 아니다). 2분 리포트가 약 0.8MB다. 32kbps 이하는 LAME이 치찰음 대역을 깎는다.
- **저장소**: 고아 브랜치 `audio`에 최근 14일치만 둔다. 매번 부모 없는 커밋 1개로 교체해서(`--force-with-lease`) 히스토리가 쌓이지 않는다. 빌드는 얕은 fetch로 `.audio/`에 풀고 `_site/audio/`로 복사해 사이트와 같은 주소로 낸다. Pages는 `.mp3`를 `audio/mp3`로 주고 Range 요청을 지원한다. 메타(`source_sha`, `sections` 등)는 같은 커밋의 `YYYY-MM-DD.json`에 둔다. `data/daily` JSON에는 필드를 넣지 않는다. actions/cache(7일 미접근이면 삭제), Release 자산(다른 origin, `application/octet-stream`), main 직접 커밋(히스토리 비대)은 기각했다.
- **생성 정책**: 한 실행에 최대 1개만 만든다. 대상은 최근 7일 리포트 중 음성이 없거나 `source_sha`가 다른 것이고, 최신 날짜부터 한다. 실패하면 30분 뒤, 일일 한도 소진이면 180분 뒤에 다시 시도하고, KST 하루 최대 6회까지 시도한다(`state.json`의 `tts`). 최종본으로 덮어쓰면 sha가 바뀌므로 새 음성이 나올 때까지는 브라우저 음성으로 읽는다.
  - **만들지 않는 실행**: 한 실행에 Gemini 긴 작업은 하나만 해서 15분 타임아웃을 지킨다. 그래서 데일리 생성을 시도한 실행(성공·실패 모두. 실패 경로도 약 250초를 쓸 수 있다), `build.py` 시작 후 `TTS_START_BUDGET_SEC`(420초)가 지난 실행, `--no-crawl` 실행에서는 음성을 만들지 않는다.
  - **publish 실패 감지**: 생성에 성공하면 `tts.pending`에 `{date, sha}`를 남긴다. 다음 실행에서 같은 날짜·sha가 여전히 대상이면 `audio` 브랜치에 올라가지 않은 것(`Publish audio` 실패)으로 본다. 이때 `error='publish_failed'`로 기록하고 `publish_fails`를 1 올린다. 처음이면 30분 뒤에 한 번 더 만든다. 연속 2번째부터는 그날(KST)은 더 만들지 않는다. 그래서 계속 실패해도 첫날 2회, 이후 하루 1회만 호출한다. publish가 확인되면 `publish_fails`는 0으로 돌아간다.
  - `source_sha`는 `SCRIPT_VERSION|VOICE|summary_md`의 해시다. 대본 규칙을 바꾸면 `tts.SCRIPT_VERSION`을 올린다(백필 창 안의 음성을 다시 만든다).
  - 직전 실패가 일시적이지 않으면(`blocked`·`bad_audio` 등) 그 날짜를 대상 목록 뒤로 미룬다. 대본이 3000자를 넘는 리포트는 만들지 않는다(시도 횟수도 안 씀).
  - 저장은 임시 파일 → `os.replace`이고 메타를 먼저 지운 뒤 mp3 → 메타 순으로 바꾼다. 중간에 죽으면 다음 실행이 다시 만든다. 음성 단계의 어떤 예외도 크롤·데일리·배포를 막지 않는다.
- **한도(미확인)**: TTS 무료 RPM·RPD는 문서에 없다(AI Studio에만 표시). 스파이크에서 11분 안에 6회 호출했을 때 429는 없었다. 평소 사용량은 하루 2회(정오본·최종본)이고, 백필·재시도를 더해도 KST 하루 6회(예비 모델까지 치면 최대 12회 요청)로 막는다. 429가 나면 로그에 `[한도: limit N (모델), quotaId=값]`이 남는다. 실제 값을 보면 이 줄에 적는다.

## 앞으로 할 것

- [x] ~~(1순위) 키워드 영역 통합.~~ 급상승 띠·급상승 키워드·인기 단어 구름을 '🔥 지금 뜨는 이슈' 보드 하나로 바꿨다(2026-09-24, `issues.py`). 단어 빈도 이력과 `trends.rising`·`trends.top`·`trends.keywords`도 없앴다.
- [ ] **이슈 보드 튜닝.** `issue_log`로 며칠간 아침·저녁 정밀도를 확인하고, `GENERIC` 목록을 보강하고, 2글짜리 작은 이슈를 어떻게 다룰지 정한다. 09-24 실데이터에서 '결혼한 결혼'·'전장연 절대'(오전), '즉각 조사 거부 추석'·'포로 한국 보내'(저녁)처럼 어색한 이름이 나왔다.
- [ ] **(2순위) 크롤링 소스 안정화.** 지금은 todaybeststory API 하나에 거의 전부를 의존한다(단일 장애점). 예비 경로인 직접 스크래핑은 해외(Actions) IP에서 검증되지 않았다. 소스 다중화, 소스별 상태 추적, 장애 감지를 넣는다. 오전에는 원본의 당일 목록이 작아서(09-24 08시 원본 593건 → 게시 265건·17곳) 피드가 얇다. 전날 목록을 몇 시까지 함께 쓸지도 여기서 정한다(지금은 KST 02시까지만).
- [ ] Actions(해외 IP)에서 직접 스크래핑 예비 경로 성공률 확인. TBS가 실패해 fallback이 도는 날의 로그를 본다.
- [ ] TBS가 부분적으로 실패하고 fallback까지 전부 실패하면 커뮤니티 수가 적은 목록이 `ok`로 통과한다. stale 판정을 보강한다.
- [ ] 이슈 묶기 단어와 '함께 나온 말'에 조사 '이/가'가 붙은 형태(예: "승무원이")가 남는다. 같은 라운드에 어근이 따로 없으면 '승무원'과 다른 단어로 본다(이름에서는 끝 조사를 뗀다). "불꽃놀이"처럼 원래 '이'로 끝나는 명사를 깨지 않는 방법을 찾는다.
- [ ] Gemini 모델 운용을 점검한다. 3.8 Flash의 503·일일 한도 빈도를 보고 기본 모델과 체인 순서를 조정한다.
- [ ] **데일리 음성 첫 Actions 실행 확인.**
  - (1) `Publish audio`가 저장소 규칙(브랜치 보호·rulesets)에 막히지 않는지 본다. `audio` 브랜치를 처음 만드는 push만 보면 안 된다. **두 번째 publish**, 곧 부모 없는 커밋으로 교체하는 비-fast-forward push도 성공하는지 봐야 한다. '강제 push 금지'가 모든 브랜치에 걸려 있으면 첫 생성은 통과하고 두 번째부터 거부된다. 막히면 `state.json`의 `tts.error`가 `publish_failed`가 된다. 이때 음성은 하루 1~2회만 다시 만들어지고 페이지에는 거의 실리지 않는다. 2026-09-25 공개 API 조회로는 규칙이 없었다(`/rules/branches/audio`·`/rulesets` 빈 목록, main `protected: false`). 그래도 첫 백필 실행(두 번째 publish)의 `Publish audio` 로그로 확인한다.
  - (2) `Install audio encoder` 스텝에서 러너(py3.12)에 `lameenc` 휠이 설치되는지 본다. 로컬 Windows py3.10에서만 확인했다. 실패해도 빌드는 계속되며, `[TTS] lameenc 없음` 로그가 남는다.
  - (3) `[TTS]` 줄의 경계 방법과, 429가 나면 `[한도: ...]` 값을 기록한다.
- [ ] 읽어주기를 실기기에서 확인한다. 사파리·파이어폭스에서 음성 파일 오류 때 `pause` 이벤트가 `audio.error`보다 먼저 오면, 재생 중 끊김도 바로 이어 읽지 않고 '일시정지' 대기 상태가 된다(▶ 한 번이면 그 섹션부터 이어짐). 전환 안내(role=status)를 스크린리더가 읽는지도 헤드리스 크롬 접근성 트리까지만 봤다.
- [ ] (선택) SNS 공유용 1200×630 `og:image`를 만든다. 지금은 투명 배경 로고를 그대로 쓴다.
- [ ] (선택) 커스텀 도메인을 연결한다. 다음 호스팅 이전 때 SEO 손실을 막는다.
- [ ] (선택) 방문 분석(GA4 등)을 붙인다.
- [ ] (선택) 댓글·로그인처럼 사용자 입력이 필요한 기능은 Supabase(RLS 필수, 무료는 7일 비활성 시 일시정지)로 붙인다.

## 도메인을 바꿀 때

- `SITE_URL` Variable(또는 `build.py`의 `DEFAULT_SITE_URL`)을 수정한다.
- 서치 콘솔에 새 속성을 등록하고 `GSC_VERIFICATION`을 넣은 뒤 `sitemap.xml`을 제출한다.
- 옛 주소에서 301 리다이렉트는 할 수 없다(Pages 제약).
