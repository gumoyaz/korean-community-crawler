# CLAUDE.md — 커트 (커뮤니티 트렌드)

한국 인터넷 커뮤니티 인기글을 모아 보여주는 **정적 사이트**. GitHub Actions가 약 10분마다 `build.py`를 실행해 크롤링·렌더링하고 GitHub Pages에 배포한다. 상시 서버는 없다.

운영 주소: https://gumoyaz.github.io/korean-community-crawler/ (프로젝트 페이지라 서브경로가 있음)

## 핵심 파일

| 파일 | 역할 |
|---|---|
| `build.py` | 진입점. 상태 복원 → 간격 가드 → 크롤 → 데일리 생성 → 데일리 음성 → `_site/` 렌더링 (sitemap·robots·llms.txt·404·audio 포함) |
| `tts.py` | 데일리 음성(Gemini TTS). 대본 → 합성 1회(+예비 모델) → 섹션 경계 → MP3(lameenc) → `AUDIO_DIR`(기본 `.audio/`)에 `{date}.mp3`·`{date}.json` |
| `crawler.py` | `TrendCrawler`: todaybeststory API 전량 수집, 커뮤니티별 상태(`source_health`)·전체 `status` 판정, 빠지거나 멈춘 커뮤니티만 직접 스크래핑·이슈링크로 채우기, 오전 전날 글 보충, 랭킹, 이슈 블록·튜닝 로그 반영, `export_state`/`import_state` |
| `sources_issuelink.py` | 이슈링크 2차 소스. `fetch(sources, now, cache=…)` → crawler 글 스키마(`via: 'issuelink'`), `SOURCE_MAP`(이슈링크 15곳 → source id). 어떤 실패도 예외로 올리지 않는다 |
| `issues.py` | '지금 뜨는 이슈' 계산. `build_issues(posts, ps_history, now)` → trends.json의 `issues` 블록. 손으로 관리하는 단어 목록(`STOP_WORDS`·`GENERIC`·`LINK_STOP`·`ALIAS` 등)은 모듈 상단 상수 |
| `daily.py` | 데일리 리포트 선정·프롬프트·저장 (`data/daily/YYYY-MM-DD.json`) |
| `gemini.py` | Gemini REST 호출. 모델 세대별 thinking 설정, 5xx 백오프, 예비 모델 체인(`GEMINI_FALLBACK_MODEL`, 쉼표 구분) |
| `templates/index.html` | 메인. JS가 `{base}/data/trends.json`을 읽어 이슈 보드와 피드를 렌더링 |
| `templates/daily.html` | 데일리 목록·상세·대기 페이지 (빌드 시 서버 렌더링). 상세의 읽어주기 플레이어(음성 파일 재생, 실패하면 브라우저 음성) |
| `.github/workflows/pages.yml` | 빌드·배포 워크플로 (+ 수집 이상 알림 `health` 잡) |
| `.github/workflows/source-probe.yml` · `tools/probe_sources.py` | 수동 실행 전용 소스 실측 프로브(러너 IP에서 TBS·직접 스크래핑·이슈링크·후보 URL의 상태·차단·행 수). 배포·state 없음 |
| `tools/import_daily_db.py` | 옛 SQLite `daily.db` → JSON 변환 |
| `data/daily/` | **커밋되는** 데일리 리포트 아카이브 (Actions 봇이 커밋) |

## 실행 흐름 (pages.yml)

1. 트리거: cron-job.org의 `workflow_dispatch`(10분), `schedule`(백업), `main` push(크롤 없이 `--no-crawl` 렌더링만)
2. `Fetch audio`(checkout 바로 뒤): `audio` 브랜치(음성 스냅숏)를 얕게 받아 `.audio/`에 푼다. 브랜치가 없으면(`ls-remote` 종료 코드 2) 첫 실행으로 보고 `base=''`로 계속한다. 원격 접근 실패 등 그 밖의 실패면 `ok`가 없어 TTS·publish가 꺼진다(렌더링은 계속). 브랜치 확인은 `ls-remote origin refs/heads/audio`(전체 이름)로 한다. `--heads origin audio`는 이름 끝 일치라서 `feature/audio`가 있으면 fetch가 실패한다
3. `Install dependencies` 다음 `Install audio encoder`: `lameenc`(선택 의존성)는 `requirements.txt`가 아니라 이 스텝에서 휠로만 설치한다(`continue-on-error`). 실패해도 크롤·배포는 계속하고, 음성은 TTS 호출 전에 `encode_failed`로 멈춘다
4. `actions/cache`로 직전 `state.json` 복원. 캐시가 없으면 `STATE_URL`(사이트의 `data/state.json`), 404면 빈 상태로 시작
5. `build.py`가 `GITHUB_OUTPUT`에 `skip`, `daily_created`, `daily_date`, `audio_changed`, `audio_dates`를 쓴다. 크롤한 실행은 `health`(ok|degraded|stale), `health_detail`, `health_streak`, `health_alert`, `health_changed`, `health_notify`, `health_close`도 쓰고 Step Summary에 커뮤니티별 상태 표를 남긴다. `TTS_ENABLED`는 `Fetch audio` 성공이고 push 이벤트가 아닐 때만 `true`다
6. `skip != true`이면 state를 캐시에 저장하고 `_site` 업로드 → deploy 잡이 Pages에 배포
7. `daily_created == true`이면 `data/daily/`만 커밋·푸시 (봇 정체성, `pull --rebase` 후 push)
8. `audio_changed == true`이면 `Publish audio`(마지막 스텝): `.audio/`를 부모 없는 커밋 1개로 만들어 `audio` 브랜치를 교체한다(`--force-with-lease`, 실패해도 배포는 진행). push 트리거가 `main`만 보므로 이 push는 워크플로를 다시 띄우지 않는다
9. `health` 잡(`issues: write`, gh CLI): `health_alert == true`(알림 대상 문제 3번 연속)이면 `source-health` 라벨 이슈를 열거나(라벨이 없으면 만든다) `health_notify`(상태가 바뀜, 또는 새 장애의 첫 알림 — 이전 이슈가 남아 있어도 알리게)일 때만 코멘트한다. `health_close`(알림 중이던 문제가 없어진 실행과 그 뒤 2번, `build.HEALTH_CLOSE_RUNS`)이면 열린 이슈를 닫고 회복 코멘트를 단다(닫은 다음에 코멘트 — 닫기가 실패하면 다음 실행이 다시 닫고 코멘트가 두 번 붙지 않는다). 값은 `env:`로만 넘긴다(`run:`에 `${{ }}` 없음). 실패해도 배포와 무관하다

## 주요 설계 결정

- **정적 사이트 + 배치 크롤**: Railway 무료 기간이 끝나서 0원 구조로 옮겼다(2026-09). 서버 API(`/api/*`)는 없다. 수동 새로고침은 JSON을 다시 읽기만 한다.
- **state 이어받기**: 실행마다 새 프로세스라서 점수 이력(`post_score_history`, 6회), 직전 글 목록, AI 요약 시각, 이슈 튜닝 로그(`issue_log`)를 `state.json`으로 넘긴다. 이슈 블록은 저장하지 않고 복원할 때 같은 입력으로 다시 계산한다. 옛 state의 단어 이력(`history`)은 읽지 않는다. state를 **받지 못하면(404 제외) 실패로 끝내서** 빈 사이트가 배포되지 않게 한다.
- **지금 뜨는 이슈 (`issues.py`)**: 옛 키워드 영역(급상승 띠·급상승 키워드·단어 구름)을 대체했다(2026-09-24). 제목만 보고 규칙으로 묶으며 LLM·형태소 분석기는 쓰지 않는다. 블록은 제목을 복사하지 않고 `posts[].rank`로 글을 가리킨다. 보드에는 2곳 이상 커뮤니티에 걸친 묶음만 최대 8개 싣는다. 상태 배지와 `+N 새 글`은 점수 이력이 3라운드 이상일 때부터 나온다. 규칙 요약은 README의 '지금 뜨는 이슈'에 있다.
  - **결과가 실행마다 같아야 한다.** 매 실행이 새 프로세스라 해시 시드가 바뀐다. set 순회 순서를 결과에 쓰지 말고, 동점이면 묶음 안에서 먼저 나온 것을 고른다. 이슈 id(가장 먼저 보인 글의 URL 해시)로 프론트가 갱신 사이에 펼침·필터를 유지한다.
  - 계산이 실패하면 `crawler._empty_issues`로 빈 블록을 내고 빌드는 계속한다. `LOW_DATA_POSTS`(350)는 빈 블록도 같은 기준을 쓰도록 `crawler.py`에 두고 `issues.py`가 가져다 쓴다. `issues.py`가 `crawler`를 import하므로 `crawler`는 `_build_issues` 안에서 `issues`를 import한다(순환 import 방지).
  - 실험 시안을 옮기면서 두 가지를 고쳤다. (1) 한 커뮤니티 안에서만 퍼나른 글은 4개 이상이어도 보드에 올리지 않는다(연재물·경기 중계처럼 제목이 비슷한 글이 한 베스트에 몰린 경우). (2) '런던'은 어미 규칙('~던')에서 보호하고 `GENERIC`에도 넣었다.
  - 프론트(`templates/index.html`): 펼침·선택·'모두 보기' 필터를 이슈 id로 갱신 사이에 유지한다. 필터 중이던 이슈가 빠지면 마지막으로 본 글 주소로 계속 거르고, 보던 이슈가 빠지면 보드에 알린다(조용히 1위로 바꾸지 않는다). 이슈 필터 중에는 탭·칩 숫자도 그 이슈 글로 센다. 머리줄 기준 시각은 배지와 같은 `last_updated`를 쓴다. `issues.as_of`는 원본 목록이 그대로여도 실행마다 바뀌어서 `last_updated`가 없을 때만 쓴다.
- **수집 상태·예비 경로 (2026-09-25)**: 기준은 '직전 게시 목록'이 아니라 커뮤니티별 고정 기대치다.
  - `source_health[source] = {status: ok|degraded|missing|blocked, last_update, last_new, n, since, via[], note?}`. degraded는 TBS 갱신이 90분 넘게 없음(06~24시), missing은 `EXPECTED_BY_HOUR`의 시각이 지났는데 TBS에 당일 글 없음(오유·가생이·인벤은 판정 안 함), blocked는 TBS에서 빠졌거나 멈췄는데 직접 스크래핑도 막혔고(4xx·챌린지·msg.html·0행) 이슈링크로도 못 채움. `EXPECTED_BY_HOUR`는 09-22~25 4일치 첫 수집 시각에 여유를 둔 값이다(매일 같은 곳 2~3시간, 크게 흔들리는 곳 4시간). 3일치 + 2시간으로 잡았던 인스티즈·와이고수 06시는 09-25 06시대 첫 수집에서 missing 오탐이 나서 10시로 늦췄다. 오탐이 또 나면 운영 로그로 조정한다.
  - 전체 `status`: `ok` / `partial`(TBS 끊김·장애, 또는 missing+degraded 3곳 이상) / `stale`(TBS가 온전하지 않은데 예비 경로까지 6곳·150건 미만 → 이전 목록 유지, 이력에 안 넣음) / `stale-source`(TBS 전체 max updateDatetime이 90분 넘게 과거, 같은 목록 분기에서도, degraded처럼 06~24시에만). 예외로 끝나면 안전망이 `stale`로 둔다. 기준 시각은 `_refresh_body`에서 한 번만 정해 수집·판정·유지·보충에 같이 쓴다(01:59에 시작해 02시를 넘긴 실행이 전날+오늘 목록을 02시 이후 목록으로 보지 않게).
  - 예비 경로는 커뮤니티 단위다. 직접 스크래핑은 degraded·missing(·TBS가 온전하지 않을 때 25개 미만)인 곳만. 이슈링크는 (a) 같은 곳(매 실행) + (b) TBS 당일 글 5건 미만인 곳((a)가 같이 있어도 30분 간격, 사이에는 직전 글 재사용 — `_il_at`·`_il_sources`는 (b) 기준). 오전 보충 중(02~12시)에는 저장한 전날 상위 글이 25개인 곳(어제는 글이 넉넉했던 곳)을 (b)에서 빼서, 새벽에 TBS 첫 수집 전인 큰 커뮤니티 칸은 전날 글이 채우고 (b)는 오유·인벤·웃대처럼 어제도 적었던 곳만 받는다(`_prev_day_covered`). 중복은 `_url_key`로 없애고 건강한 TBS → 직접 → 이슈링크 → 멈춘 TBS 순으로 고른다. SLR·보배드림은 TBS(베스트 게시판 번호)와 이슈링크(원래 게시판 번호)의 주소가 달라서, 다른 경로에서 먼저 고른 같은 커뮤니티·같은 제목(`_title_key`, 공백·기호 뺀 5자 이상) 글도 뺀다(같은 경로 안에서 제목만 같은 글은 둘 다 둔다). 이번 결과에 한 글도 없는 커뮤니티(TBS가 온전하지 않으면 25개 미만인 곳)는 날짜 창 안의 이전 글을 `kept: true`로 병합한다. 날짜 판정에 TBS `targetDate`를 `target_date`로 state에 남긴다(공개 JSON에는 안 냄).
  - 점수 이력은 TBS를 끝까지(멈추지 않은 채로) 받은 라운드만 넣고, 40분 안의 라운드는 한 칸으로 덮어쓰며, 240분 넘은 칸은 버린다(칸 시각은 `post_score_times`). TBS limit은 실행마다 100/99를 번갈아 쓴다(URL별 약 10분 캐시).
  - 오전 보충: 00~02시(`MERGE_PREV_END_HOUR`, build.py `DAILY_FINAL_END_HOUR`도 같은 값) 실행이 전날 커뮤니티별 상위 25개를 `state.json`의 `crawler.prev_day`에 두고(이 동안 state.json이 평소 약 370KB에서 약 600KB로 커진다), 02~12시에 당일 글이 25개 미만인 칸만 `prev_day: true`로 채운다. 멈춤 판정은 TBS 당일 글만 센다(`_tbs_today_size`: via·kept·전날 target_date 제외, 커뮤니티당 25개까지). 19곳·350건이면 채우지 않고, 12시 전에는 저장한 전날 글을 지우지 않고 실행마다 다시 판정한다(이슈링크 (b) 글까지 세면 09-25 02:10 재현에서 첫 실행에 보충이 끝나고 전날 글이 지워졌다 — 되돌릴 수 없다). 12시에 저장 글을 비운다. `prev_day.done`은 멈춤 기준을 처음 넘은 날(로그용)이다. 이슈 보드 전체·AI 요약·정오본 데일리 입력에서 뺀다.
  - 알림(`build._health`): health는 ok|degraded|stale 그대로 내되, 연속 횟수는 알림 대상(stale·stale-source·partial, missing·blocked)만 센다. degraded(갱신 멈춤)는 채워지든 아니든 칩 표시만 한다 — 원본의 커뮤니티 수집이 멈춘 것이라 고칠 수 없고, 인스티즈가 거의 매일 몇 시간씩 멈춰서 첫 운영 하루(09-25~26)에 이슈가 4번 열렸다. 하루 넘게 멈추면 날이 바뀐 뒤 기대 시각에 missing이 되어 알리고, 여러 곳이 같이 멈추면 partial로 알린다. 알림 중(연속 3번 이상)이던 상태에서 KST 00~06시(`HEALTH_FROM_HOUR` 전)의 '알림 대상 없음'은 회복으로 세지 않고 직전 연속 횟수·key를 그대로 둔다(held, `health_alert` false라 잡도 안 돈다. 알리기 전 1~2번이면 평소처럼 0으로 돌린다) — 크롤러가 이 시간에 degraded·stale-source를 판정하지 않고 00~02시에는 전날 목록도 보기 때문에, 그대로 두면 저녁부터 이어진 장애가 00:10에 '회복'으로 닫혔다가 02시 뒤 missing으로 새 이슈가 열린다(09-23~24 개드립 사례 재현). 그래서 자정 전 장애는 같은 이슈에 코멘트로 이어지고, 00~06시의 진짜 회복은 06시 뒤에 닫힌다. `state.health`에 `close_left`(남은 닫기 실행 수)가 있다.
- **시간대 계약**: `post.date`는 `YYYY-MM-DDTHH:MM:SS+09:00` / `YYYY-MM-DD`(날짜만) / `''` 셋 중 하나다. KST 시각을 `Z`로 저장하지 않는다.
- **데일리 타이밍**: KST 12시 이전에는 만들지 않는다(정오본). 다음 날 00:10~01:59에 하루 전체 목록으로 최종본을 덮어쓴다. 실패하면 다음 실행에서 재시도한다.
- **데일리 프롬프트**: 입력(제목·본문)에 있는 사실만 쓴다. 소속·직함·반응을 추측하면 사실 오류가 난 적이 있다(2026-09-23).
- **데일리 읽어주기 (`templates/daily.html`)**: 상세 record에 `summary.audio`(`{url, duration, sections[{title, start}], model, voice}`)가 있으면 `<audio>`로 재생하고, 없거나 재생에 실패하면 Web Speech로 읽는다. 동작 설명은 README의 '읽어주기'에 있다.
  - `build.py`가 `tts.public_audio()`로 `summary.audio`를 넣는다. `AUDIO_DIR`에 지금 리포트 내용과 맞는(`source_sha` 일치, mp3 크기 == 메타 `bytes`) 음성이 있을 때만 넣고 `_site/audio/`로 복사한다. 옛 내용의 음성(최종본으로 바뀌기 전 정오본 음성)은 싣지 않는다.
  - `sections[i]`는 `.summary-body` 바로 아래 i번째 `h2`와 짝이다. 개수가 다르거나 시각이 오름차순이 아니면 섹션 이동·강조를 끄고 재생만 한다.
  - 음성 정보는 `<script type="application/json" id="ttsAudioData">{{ summary.audio | tojson }}</script>`로 넣는다(`tojson`이 `<`·`>`·`&`·`'`를 이스케이프한다). 값이 있으면 JSON-LD에 `AudioObject`도 넣는다.
  - 사용자가 멈췄는지는 `aWant`로 따로 기억한다. 크롬은 오류로 멈출 때 페이지의 `error` 처리보다 먼저 `audio.paused`를 true로 바꾸기 때문이다. 재생 중 실패는 그 섹션부터 브라우저 음성으로 바로 이어 읽는다. 일시정지 중 실패는 소리 없이 기다렸다가(`speechFrom`) 다음 ▶에 읽는다.
  - 브라우저 음성은 기기마다 다르다. Windows 기본 Heami는 기계음에 가깝고, rate를 1.5로 올려도 약 10%만 빨라졌다(실측). 음성 파일 경로를 따로 둔 이유다.
- **해외 러너에서 되는 경로 (2026-09-25 Source probe, 미국 Azure IP)**: 이슈링크는 목록·robots.txt 대신 JS 쿠키 봇 확인 페이지(`cupid.js`)를 줘서 0건이다. 풀지 않는다(봇 차단 우회). `sources_issuelink`는 robots.txt 자리에 HTML이 오면 그 실행을 건너뛴다. 한국 IP(로컬)에서는 정상이라 로컬 테스트와 운영 결과가 다르다. 직접 스크래핑은 루리웹·클리앙·더쿠·보배·네이트판·웃대·딴지가 되고 오유·아카라이브·인스티즈·FM코리아는 막히고 MLB파크·SLR은 연결 오류다. 2026-09-26에 러너에서 접속되는 디시(실베)·뽐뿌(HOT)·인벤(오픈이슈 추천)·82쿡(많이 읽은 글 10개)·이토랜드(`/hit/list` 순위 30개, `row_is_link`)·와이고수(실시간 인기)·개드립(인기순) 파서를 더해 예비 경로가 있는 곳이 14곳이 됐다. 인스티즈는 채울 경로가 없다.
- **전체 순위 = 실제 인기 + 커뮤니티 균형 (2026-09-26, 사용자 원칙)**: `crawler._assign_ranks`가 `_popularity`(log10 추정 조회수 × 반감기 24시간)로 정렬하고 같은 커뮤니티 k번째 글에서 `RANK_SOURCE_PENALTY`(0.6)×k를 뺀다. 조회수 없는 FM코리아·개드립은 댓글×100(265로 두면 FM코리아가 상위를 독차지). 예전 방식(커뮤니티별 1등을 모두 100점으로 맞추고 0.65^n 점감)은 1~21위가 커뮤니티별 1등 하나씩이 되어, 조회 8만 더쿠 글이 20위, 글 2개뿐인 인벤 1등이 3위였다. `rank_score`는 그대로라 이슈 보드·데일리는 영향 없다.
- **소스 간 원시 조회수 비교 금지(rank_score·데일리 선정)**: FM코리아 조회수는 API의 합성값이라 0으로 둔다(이슈링크로 받은 FM코리아 글도 0 — 한 커뮤니티에 실제 조회수 글이 섞이면 TBS 글이 뒤로 밀린다). 선정은 소스별로 정규화된 `rank_score`로 한다.
- **보안**: 데일리 HTML은 `markdown` → `nh3`로 정화한다. 프론트는 외부 텍스트에 `escHtml`, 링크에 `safeUrl`(http/https만)을 쓴다.
- **내부 링크는 `{{ base }}`로 시작**한다. base는 `SITE_URL`의 경로(`/korean-community-crawler`)에서 나온다.

## 환경변수

README의 표 참고. 핵심은 `GOOGLE_API_KEY`(Secret), `GEMINI_MODEL`, `GEMINI_FALLBACK_MODEL`, `SITE_URL`, `GSC_VERIFICATION`, `GA_MEASUREMENT_ID`(Variables, GA4 `G-…` 형식이 아니면 태그를 넣지 않음)이다. 음성은 `TTS_MODEL`, `TTS_FALLBACK_MODEL`, `TTS_VOICE`(Variables, 비우면 기본값)와 `AUDIO_DIR`을 쓴다. `TTS_ENABLED`는 pages.yml이 계산한다(audio fetch 성공이고 push 이벤트가 아닐 때만 `true`). Actions에서 비어 있는 vars는 `''`로 들어오는데, `build.py`가 import 전에 지워서 기본값을 쓰게 한다.

## 로컬 개발

```bash
pip install -r requirements.txt
python build.py --base "" --out _site --no-crawl     # 운영 state로 렌더링만
python build.py --base "" --out _site --force        # 직접 크롤
python -m http.server -d _site 8000
python tools/probe_sources.py --only tbs,issuelink --no-ipinfo   # 소스 실측(한국 IP). 결과 파일은 임시 폴더(--out으로 바꿈)
```

- 테스트할 때는 `DAILY_DIR`을 임시 폴더로 두어 `data/daily/`를 오염시키지 않는다. 음성도 `AUDIO_DIR`을 임시 폴더로 둔다.
- `--now ISO`는 크롤러 판정 시각(TBS 조회 날짜·`EXPECTED_BY_HOUR`·오전 보충·알림 held)에도 쓰인다. 크롤하면 이슈링크((b) 적은 곳은 거의 매번)와, 멈추거나 빠진 커뮤니티가 있으면 직접 스크래핑 요청도 나간다. 실제 요청 없이 확인할 때는 `requests.Session.request`를 가짜로 바꿔 기록한 응답을 돌려주는 식으로 한다(2026-09-25 검증 방식).
- 로컬 기본값은 `TTS_ENABLED` 미설정, 곧 **음성을 만들지 않는다**. 렌더링은 `AUDIO_DIR`에 맞는 음성이 있으면 싣는다. `--no-crawl`이면 `TTS_ENABLED=true`여도 만들지 않는다. 음성을 직접 만들어 볼 때만 `pip install lameenc==1.8.4`를 따로 설치한다(선택 의존성, pages.yml과 같은 버전).
- Windows에서는 `PYTHONUTF8=1`로 실행한다(콘솔 인코딩).
- `_site/`, `.state/`, `.audio/`, `.env`, `*.db`는 gitignore 대상이다.

## 운영 메모

- 수동 실행: Actions → Build and deploy → Run workflow. "간격 가드 무시"를 체크하면 바로 크롤한다.
- 외부 트리거: cron-job.org가 10분마다 `POST /repos/gumoyaz/korean-community-crawler/actions/workflows/pages.yml/dispatches`를 호출한다.
  - 헤더에 `User-Agent`가 없으면 GitHub가 403을 준다.
  - 토큰은 이 리포만 선택한 fine-grained PAT이고, 권한은 Actions: Read and write다. 만료되면 GitHub에서 재발급해 cron-job.org 헤더를 교체한다. 교체하기 전까지는 백업 `schedule`만 돈다.
- Gemini 무료 한도는 모델마다 따로 있고, 태평양 자정(KST 16~17시)에 리셋된다. 3.8 Flash는 503(high demand)이 잦아서 예비 모델 체인이 자주 쓰인다.
- 상태 확인: `data/state.json`의 `last_run`, `daily_failed_at`과 `data/trends.json`의 `status`, `crawl_count`, `source_health`를 본다. 실행 요약(Step Summary)에 커뮤니티별 상태 표가 있고, 알림 연속 횟수는 `state.json`의 `health`(`status, streak, key, since, detail, close_left`)에 있다. 로그 줄은 `[Health]`, `[IssueLink]`, `[PrevDay]`, `[Refresh] … → 직접 스크래핑 …, 이슈링크 …`.
- 수집 이상 이슈: `source-health` 라벨. 연속 3번 알림 대상이면 열리고 풀리면 닫힌다(KST 00~06시에는 닫지 않고 06시 뒤 판정으로 닫는다). 소음이 크면 `build.HEALTH_ALERT_STREAK`나 알림 대상 규칙(`_health`)을 조정한다.
- 소스 실측: Actions → **Source probe** → Run workflow(`only`로 그룹 선택: tbs,fallback,issuelink,candidates, 비우면 전부 약 2~3분). 결과는 Step Summary와 아티팩트 `source-probe-<run_id>`(JSON·MD, 30일)에 남는다. 러너(해외 IP)에서 직접 스크래핑이 막히는지 이 결과로 판단한다. 판정 `blocked`는 차단 상태 코드·챌린지·msg.html, `empty`는 200인데 파서 0행(선택자 고장일 수 있음)이다.
- 오전 보충·이슈링크 상태: `state.json`의 `crawler.prev_day`(`date, done, posts`), `crawler.issuelink`(`at`·`sources` = (b)를 마지막으로 새로 받은 시각·커뮤니티, `cache` = 원본 주소 캐시 최대 1000개).
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

**상태: 운영 중.** 2026-09-25 첫 운영 실행에서 9/24(09:50)·9/23 백필(10:00)·9/25 정오본(12:10) 음성이 모두 `gemini-3.8-flash-tts`로 만들어졌다. 경계는 셋 다 `snap`이었고, `Publish audio`의 교체 push(부모 없는 커밋, 비-fast-forward)도 막히지 않았다(`tts.publish_fails` 0). 러너(py3.12)에 `lameenc` 휠도 설치됐다. 2026-09-24 스파이크(TTS 호출 6회)로 정하고 구현했다(`tts.py`, `build.py`의 `_maybe_generate_audio`·`_site_audio`, `pages.yml`의 `Fetch audio`·`Install audio encoder`·`Publish audio`, `templates/daily.html` 플레이어). 같은 날 실제 TTS 1회로 끝까지 확인했다: 9/24 정오본(대본 1067자) → 130.8초 음성, 응답 33.4초, 경계 `snap`(6개 모두 '○ 번째 이야기' 패턴), MP3 785,088바이트(약 0.8MB). 로컬 베어 리포를 `audio` 원격으로 써서 fetch → 생성 → publish(부모 없는 커밋 1개) → 다음 실행이 받은 음성을 최신으로 보고 호출하지 않는 것까지 봤다. 2026-09-25에는 TTS를 HTTP 층에서 가짜로 두고(실측 WAV 응답) pages.yml에서 뽑은 스텝 본문으로 첫 운영 순서(브랜치 생성 → 백필 교체 push → 모두 최신 → push 이벤트 렌더)와 헤드리스 크롬 375·1280 재생을 다시 확인했다.

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
- **한도(미확인)**: TTS 무료 RPM·RPD는 문서에 없다(AI Studio에만 표시). 스파이크에서 11분 안에 6회 호출했을 때 429는 없었고, 운영 첫날(2026-09-25) 3회 호출에서도 없었다. 평소 사용량은 하루 2회(정오본·최종본)이고, 백필·재시도를 더해도 KST 하루 6회(예비 모델까지 치면 최대 12회 요청)로 막는다. 429가 나면 로그에 `[한도: limit N (모델), quotaId=값]`이 남는다. 실제 값을 보면 이 줄에 적는다.

## 앞으로 할 것

- [x] ~~(1순위) 키워드 영역 통합.~~ 급상승 띠·급상승 키워드·인기 단어 구름을 '🔥 지금 뜨는 이슈' 보드 하나로 바꿨다(2026-09-24, `issues.py`). 단어 빈도 이력과 `trends.rising`·`trends.top`·`trends.keywords`도 없앴다.
- [ ] **이슈 보드 튜닝.** `issue_log`로 며칠간 아침·저녁 정밀도를 확인하고, `GENERIC` 목록을 보강하고, 2글짜리 작은 이슈를 어떻게 다룰지 정한다. 09-24 실데이터에서 '결혼한 결혼'·'전장연 절대'(오전), '즉각 조사 거부 추석'·'포로 한국 보내'(저녁)처럼 어색한 이름이 나왔다.
- [x] ~~(2순위) 크롤링 소스 안정화.~~ 커뮤니티별 상태(`source_health`)와 `status` 4종, 커뮤니티 단위 예비 경로, 이슈링크 2차 소스, 오전 전날 글 보충('어제'), 수집 이상 알림(`source-health` 이슈), 소스 실측 프로브를 넣었다(2026-09-25). 일베 매핑을 빼고 네이트판 예비 URL을 '톡커들의 선택'으로 바꿨다. 부분 실패 + fallback 실패도 절대 기준(6곳·150건)과 `partial`로 판정한다.
- [ ] **소스 안정화 첫 운영 확인.**
  - ~~(1) Source probe~~ 2026-09-25 실행 완료(결과는 설계 결정의 '해외 러너에서 되는 경로'). 새 파서 7곳은 로컬에서만 실제로 받아 봤다 — 다음 프로브나 fallback이 도는 날 러너 로그로 확인한다.
  - ~~(2) `health` 잡~~ 2026-09-25~26 이슈 #1~#4를 열고 닫는 것까지 실제로 동작했다(모두 인스티즈 degraded — 이후 degraded는 알림에서 뺐다).
  - (3) 며칠간 `[Health]` 로그로 `EXPECTED_BY_HOUR` missing 오탐과 `source-health` 이슈 빈도를 본다.
  - (4) 이슈링크 약관의 수집 금지 여부는 확인하지 못했다(robots.txt는 `Allow:/`). `/go/` HEAD가 이슈링크 클릭 집계에 잡히는지도 모른다. 원본 URL 캐시로 요청을 줄였다.
  - (5) 오전 이슈링크 (b)는 어제도 글이 적었던 곳(09-24 목록 기준 오유·인벤·웃대 3곳)만 받도록 줄였다(09-25 02:10 재현은 9곳이었다). 전날 상위 글을 저장하지 못한 날(00~02시 실행 실패 등)은 예전처럼 TBS 5건 미만인 곳 전부가 (b)다.
- [ ] 이슈 묶기 단어와 '함께 나온 말'에 조사 '이/가'가 붙은 형태(예: "승무원이")가 남는다. 같은 라운드에 어근이 따로 없으면 '승무원'과 다른 단어로 본다(이름에서는 끝 조사를 뗀다). "불꽃놀이"처럼 원래 '이'로 끝나는 명사를 깨지 않는 방법을 찾는다.
- [ ] Gemini 모델 운용을 점검한다. 3.8 Flash의 503·일일 한도 빈도를 보고 기본 모델과 체인 순서를 조정한다.
- [x] ~~데일리 음성 첫 Actions 실행 확인.~~ 2026-09-25에 음성 3개가 만들어져 `audio` 브랜치에 올라갔다. 교체 push 3번이 모두 통과했고(브랜치 규칙 없음), `lameenc`가 설치됐고, 경계는 모두 `snap`이었다. 429가 나면 `[한도: ...]` 값을 위 '한도' 줄에 적는다.
- [ ] 읽어주기를 실기기에서 확인한다. 사파리·파이어폭스에서 음성 파일 오류 때 `pause` 이벤트가 `audio.error`보다 먼저 오면, 재생 중 끊김도 바로 이어 읽지 않고 '일시정지' 대기 상태가 된다(▶ 한 번이면 그 섹션부터 이어짐). 전환 안내(role=status)를 스크린리더가 읽는지도 헤드리스 크롬 접근성 트리까지만 봤다.
- [ ] (선택) SNS 공유용 1200×630 `og:image`를 만든다. 지금은 투명 배경 로고를 그대로 쓴다.
- [ ] (선택) 커스텀 도메인을 연결한다. 다음 호스팅 이전 때 SEO 손실을 막는다.
- [ ] 방문 분석(GA4): 코드는 넣었다(2026-09-26, `GA_MEASUREMENT_ID`). GA4 속성·웹 스트림을 만들고 측정 ID를 Variables에 넣으면 켜진다. 켠 뒤 GA4 MCP로 검색 유입(`sessionDefaultChannelGroup` Organic Search)을 본다.
- SEO 문구(2026-09-26): 메인 '실시간 커뮤니티 인기글 모음 | 커트', 데일리 상세 설명은 그날 요약 소제목 3개(`build._daily_seo_desc`, 글 제목은 욕설 때문에 쓰지 않음).
- [ ] (선택) 댓글·로그인처럼 사용자 입력이 필요한 기능은 Supabase(RLS 필수, 무료는 7일 비활성 시 일시정지)로 붙인다.

## 도메인을 바꿀 때

- `SITE_URL` Variable(또는 `build.py`의 `DEFAULT_SITE_URL`)을 수정한다.
- 서치 콘솔에 새 속성을 등록하고 `GSC_VERIFICATION`을 넣은 뒤 `sitemap.xml`을 제출한다.
- 옛 주소에서 301 리다이렉트는 할 수 없다(Pages 제약).
