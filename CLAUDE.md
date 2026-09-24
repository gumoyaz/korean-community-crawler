# CLAUDE.md — 커트 (커뮤니티 트렌드)

한국 인터넷 커뮤니티 인기글을 모아 보여주는 **정적 사이트**. GitHub Actions가 약 10분마다 `build.py`를 실행해 크롤링·렌더링하고 GitHub Pages에 배포한다. 상시 서버는 없다.

운영 주소: https://gumoyaz.github.io/korean-community-crawler/ (프로젝트 페이지라 서브경로가 있음)

## 핵심 파일

| 파일 | 역할 |
|---|---|
| `build.py` | 진입점. 상태 복원 → 간격 가드 → 크롤 → 데일리 생성 → `_site/` 렌더링 (sitemap·robots·llms.txt·404 포함) |
| `crawler.py` | `TrendCrawler`: todaybeststory API 전량 수집 + 직접 스크래핑 예비 경로, 랭킹, 이슈 블록·튜닝 로그 반영, `export_state`/`import_state` |
| `issues.py` | '지금 뜨는 이슈' 계산. `build_issues(posts, ps_history, now)` → trends.json의 `issues` 블록. 손으로 관리하는 단어 목록(`STOP_WORDS`·`GENERIC`·`LINK_STOP`·`ALIAS` 등)은 모듈 상단 상수 |
| `daily.py` | 데일리 리포트 선정·프롬프트·저장 (`data/daily/YYYY-MM-DD.json`) |
| `gemini.py` | Gemini REST 호출. 모델 세대별 thinking 설정, 5xx 백오프, 예비 모델 체인(`GEMINI_FALLBACK_MODEL`, 쉼표 구분) |
| `templates/index.html` | 메인. JS가 `{base}/data/trends.json`을 읽어 이슈 보드와 피드를 렌더링 |
| `templates/daily.html` | 데일리 목록·상세·대기 페이지 (빌드 시 서버 렌더링) |
| `.github/workflows/pages.yml` | 빌드·배포 워크플로 |
| `tools/import_daily_db.py` | 옛 SQLite `daily.db` → JSON 변환 |
| `data/daily/` | **커밋되는** 데일리 리포트 아카이브 (Actions 봇이 커밋) |

## 실행 흐름 (pages.yml)

1. 트리거: cron-job.org의 `workflow_dispatch`(10분), `schedule`(백업), `main` push(크롤 없이 `--no-crawl` 렌더링만)
2. `actions/cache`로 직전 `state.json` 복원. 캐시가 없으면 `STATE_URL`(사이트의 `data/state.json`), 404면 빈 상태로 시작
3. `build.py`가 `GITHUB_OUTPUT`에 `skip`, `daily_created`, `daily_date`를 쓴다
4. `skip != true`이면 state를 캐시에 저장하고 `_site` 업로드 → deploy 잡이 Pages에 배포
5. `daily_created == true`이면 `data/daily/`만 커밋·푸시 (봇 정체성, `pull --rebase` 후 push)

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
- **소스 간 원시 조회수 비교 금지**: FM코리아 조회수는 API의 합성값이라 0으로 둔다. 선정은 소스별로 정규화된 `rank_score`로 한다.
- **보안**: 데일리 HTML은 `markdown` → `nh3`로 정화한다. 프론트는 외부 텍스트에 `escHtml`, 링크에 `safeUrl`(http/https만)을 쓴다.
- **내부 링크는 `{{ base }}`로 시작**한다. base는 `SITE_URL`의 경로(`/korean-community-crawler`)에서 나온다.

## 환경변수

README의 표 참고. 핵심은 `GOOGLE_API_KEY`(Secret), `GEMINI_MODEL`, `GEMINI_FALLBACK_MODEL`, `SITE_URL`, `GSC_VERIFICATION`(Variables)이다. Actions에서 비어 있는 vars는 `''`로 들어오는데, `build.py`가 import 전에 지워서 기본값을 쓰게 한다.

## 로컬 개발

```bash
pip install -r requirements.txt
python build.py --base "" --out _site --no-crawl     # 운영 state로 렌더링만
python build.py --base "" --out _site --force        # 직접 크롤
python -m http.server -d _site 8000
```

- 테스트할 때는 `DAILY_DIR`을 임시 폴더로 두어 `data/daily/`를 오염시키지 않는다.
- Windows에서는 `PYTHONUTF8=1`로 실행한다(콘솔 인코딩).
- `_site/`, `.state/`, `.env`, `*.db`는 gitignore 대상이다.

## 운영 메모

- 수동 실행: Actions → Build and deploy → Run workflow. "간격 가드 무시"를 체크하면 바로 크롤한다.
- 외부 트리거: cron-job.org가 10분마다 `POST /repos/gumoyaz/korean-community-crawler/actions/workflows/pages.yml/dispatches`를 호출한다.
  - 헤더에 `User-Agent`가 없으면 GitHub가 403을 준다.
  - 토큰은 이 리포만 선택한 fine-grained PAT이고, 권한은 Actions: Read and write다. 만료되면 GitHub에서 재발급해 cron-job.org 헤더를 교체한다. 교체하기 전까지는 백업 `schedule`만 돈다.
- Gemini 무료 한도는 모델마다 따로 있고, 태평양 자정(KST 16~17시)에 리셋된다. 3.8 Flash는 503(high demand)이 잦아서 예비 모델 체인이 자주 쓰인다.
- 상태 확인: `data/state.json`의 `last_run`, `daily_failed_at`과 `data/trends.json`의 `status`, `crawl_count`를 본다.
- 이슈 품질 확인: `data/state.json`의 `crawler.issue_log`에 결과가 바뀐 빌드의 이슈 요약(`t, posts, communities, items[id,name,kind,status,n,c]`)이 최대 72건·30KB까지 쌓인다. 한 건이 약 0.9KB라서 실제로는 30KB 상한에 먼저 걸린다(약 30건). 크롤 없는 푸시 빌드(`--no-crawl`)는 기록하지 않는다.

## 앞으로 할 것

- [x] ~~(1순위) 키워드 영역 통합.~~ 급상승 띠·급상승 키워드·인기 단어 구름을 '🔥 지금 뜨는 이슈' 보드 하나로 바꿨다(2026-09-24, `issues.py`). 단어 빈도 이력과 `trends.rising`·`trends.top`·`trends.keywords`도 없앴다.
- [ ] **이슈 보드 튜닝.** `issue_log`로 며칠간 아침·저녁 정밀도를 확인하고, `GENERIC` 목록을 보강하고, 2글짜리 작은 이슈를 어떻게 다룰지 정한다. 09-24 실데이터에서 '결혼한 결혼'·'전장연 절대'(오전), '즉각 조사 거부 추석'·'포로 한국 보내'(저녁)처럼 어색한 이름이 나왔다.
- [ ] **(2순위) 크롤링 소스 안정화.** 지금은 todaybeststory API 하나에 거의 전부를 의존한다(단일 장애점). 예비 경로인 직접 스크래핑은 해외(Actions) IP에서 검증되지 않았다. 소스 다중화, 소스별 상태 추적, 장애 감지를 넣는다. 오전에는 원본의 당일 목록이 작아서(09-24 08시 원본 593건 → 게시 265건·17곳) 피드가 얇다. 전날 목록을 몇 시까지 함께 쓸지도 여기서 정한다(지금은 KST 02시까지만).
- [ ] Actions(해외 IP)에서 직접 스크래핑 예비 경로 성공률 확인. TBS가 실패해 fallback이 도는 날의 로그를 본다.
- [ ] TBS가 부분적으로 실패하고 fallback까지 전부 실패하면 커뮤니티 수가 적은 목록이 `ok`로 통과한다. stale 판정을 보강한다.
- [ ] 이슈 묶기 단어와 '함께 나온 말'에 조사 '이/가'가 붙은 형태(예: "승무원이")가 남는다. 같은 라운드에 어근이 따로 없으면 '승무원'과 다른 단어로 본다(이름에서는 끝 조사를 뗀다). "불꽃놀이"처럼 원래 '이'로 끝나는 명사를 깨지 않는 방법을 찾는다.
- [ ] Gemini 모델 운용을 점검한다. 3.8 Flash의 503·일일 한도 빈도를 보고 기본 모델과 체인 순서를 조정한다.
- [ ] (선택) SNS 공유용 1200×630 `og:image`를 만든다. 지금은 투명 배경 로고를 그대로 쓴다.
- [ ] (선택) 커스텀 도메인을 연결한다. 다음 호스팅 이전 때 SEO 손실을 막는다.
- [ ] (선택) 방문 분석(GA4 등)을 붙인다.
- [ ] (선택) 댓글·로그인처럼 사용자 입력이 필요한 기능은 Supabase(RLS 필수, 무료는 7일 비활성 시 일시정지)로 붙인다.

## 도메인을 바꿀 때

- `SITE_URL` Variable(또는 `build.py`의 `DEFAULT_SITE_URL`)을 수정한다.
- 서치 콘솔에 새 속성을 등록하고 `GSC_VERIFICATION`을 넣은 뒤 `sitemap.xml`을 제출한다.
- 옛 주소에서 301 리다이렉트는 할 수 없다(Pages 제약).
