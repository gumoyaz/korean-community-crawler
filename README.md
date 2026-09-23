# 커트 — 커뮤니티 트렌드

한국 주요 인터넷 커뮤니티의 인기글을 10분마다 모아 **지금 화제인 글들을 한눈에** 보여주는 사이트입니다.
매일 KST 12시 이후에는 Gemini가 그날의 화제를 정리한 **데일리 리포트**를 만듭니다.

- 사이트: https://gumoyaz.github.io/korean-community-crawler/
- 비용: 0원 (GitHub Actions + GitHub Pages, 상시 서버 없음)

## 동작 방식

```
cron-job.org (10분마다) ──▶ GitHub Actions: Build and deploy
schedule (백업, :07/:22/:37/:52)      │
                                     ├─ 1. 직전 상태 복원 (Actions 캐시 → 없으면 사이트의 data/state.json)
                                     ├─ 2. 크롤링 1회 (todaybeststory API, 부족하면 직접 스크래핑)
                                     ├─ 3. 메인 AI 요약 (6시간마다) / 데일리 리포트 (정오본·최종본)
                                     ├─ 4. _site/ 에 정적 사이트 렌더링 (build.py)
                                     ├─ 5. 새 데일리 리포트가 있으면 data/daily/*.json 을 main 에 커밋
                                     └─ 6. GitHub Pages 에 배포
```

- 사이트 자체는 정적 파일이라, 크롤이 실패해도 마지막으로 배포된 화면이 그대로 떠 있습니다.
- 데일리 리포트는 리포에 JSON으로 커밋되므로 호스팅을 옮겨도 사라지지 않습니다.
- 브라우저는 `data/trends.json`을 10분마다 다시 읽습니다. 서버 API는 없습니다.

## 수집 소스

### todaybeststory.com API (주 경로)

22개 커뮤니티의 당일 인기글 전체(보통 하루 1,300~2,000건)를 `limit=100`으로 끝 페이지까지 받습니다.
FM코리아, 디시인사이드, 아카라이브, 네이트판, 보배드림, 웃긴대학, 딴지일보, 루리웹, 클리앙, 더쿠, MLB파크,
인스티즈, 개드립, 이토랜드, 가생이, 일베, 인벤, 뽐뿌, SLR클럽, 오늘의유머, 와이고수, 82쿡.

- 조회 날짜는 KST 기준이고, KST 00~02시에는 전날 목록도 함께 받습니다.
- FM코리아 조회수는 API가 주는 합성값이라 0으로 둡니다(소스 간 비교 왜곡 방지).

### 직접 스크래핑 (예비 경로)

API 결과가 50건 미만이거나 커뮤니티가 8곳 미만이면 아래 10곳을 직접 긁어 보탭니다.
오늘의유머, 루리웹(베스트), 클리앙, 더쿠, MLB파크(불펜 베스트), 보배드림, 네이트판, 웃긴대학, 아카라이브, 딴지일보.

- 행 단위로 파싱해서 제목·조회수·날짜가 항상 같은 글에서 나옵니다.
- 공지·광고·댓글 수 링크는 걸러냅니다. FM코리아·인스티즈는 봇 차단 때문에 직접 요청하지 않습니다.

## 화면 구성

- **✨ 오늘의 커뮤니티 요약** — Gemini가 커뮤니티별 대표글을 주제별로 묶은 짧은 요약 (6시간마다, 24시간 넘으면 숨김)
- **급상승 티커 / 급상승 키워드** — 직전 크롤 대비 빈도가 늘어난 단어
- **인기 단어 구름** — 클릭하면 그 단어가 들어간 글만 필터링 (키보드로도 선택 가능)
- **카테고리 탭** — 전체 / 게임·IT / 연예·아이돌 / 유머 / 음식·카페 / 뷰티·패션 / 자동차
- **커뮤니티 칩** — 사이트별 필터 (글 수 표시)
- **카드 피드** — 출처 배지, 본문 요약, 시각, 조회·추천·댓글. 클릭하면 원글로 이동
- **데일리 리포트** (`/daily/`) — 날짜별 아카이브, 이전/다음 이동, 읽어주기(TTS)

## 데일리 리포트

| 시점 (KST) | 동작 |
|---|---|
| 12:00 이후 첫 실행 | 오늘 **정오본** 생성 |
| 다음 날 00:10~01:59 | 하루 전체 목록으로 전날 **최종본**을 다시 생성해 덮어씀 (정오본이 없으면 백필) |
| 생성 실패 | 30분 뒤 다음 실행에서 자동 재시도 |

- 입력은 그날 글 가운데 랭킹 상위 20개입니다. 한 커뮤니티는 최대 4개까지만 넣고, 커뮤니티가 10곳 미만이면 생성을 미룹니다.
- 프롬프트는 제목·본문에 있는 사실만 쓰게 합니다. 입력에 없는 소속·직함·반응은 추측하지 않습니다.
- 모델은 `gemini-3.8-flash`이고, 일시 오류(5xx)나 일일 한도에 걸리면 예비 모델 `gemini-3.5-flash-lite`로 넘어갑니다. 둘 다 환경변수로 바꿀 수 있습니다.
- 저장 형식은 `data/daily/YYYY-MM-DD.json`입니다. 필드는 `date, generated_at, model, post_count, analyzed_count, summary_md, posts`입니다.

## 랭킹 로직

**1. 인게이지먼트 점수** — 소스 안에서만 로그 정규화한 뒤 가중 합산합니다. 조회수가 없는 소스는 위치 점수만 씁니다.

```
engagement = 조회수×0.60 + 추천수×0.25 + 댓글수×0.15
base       = 위치점수×0.35 + engagement×0.65
```

위치 점수는 소스별 순번 기준입니다(1위 100점에서 차감).

**2. 시간 감쇠** — `decay = e^(-나이(일) / 90)` (반감기 약 62일). 90일을 넘은 글은 제외합니다.

**3. 소스 대표글 가산점** — 소스마다 1위 글에 ×1.10을 줍니다.

**4. 다양성 점감 + 하드 캡** — 같은 소스의 n번째 글은 `×0.65^n`이고, 소스당 최대 25개입니다.

## 키워드 트렌드

```
velocity    = (현재 − 직전)×2 + (현재 − 4회전 전)
trend_score = min(100, base + max(0, velocity)×3)
```

- 최근 6회 크롤의 단어 빈도를 `state.json`에 보관합니다. 이력이 4회 미만이면 가장 오래된 회차와 비교합니다.
- API 목록이 직전과 똑같으면(시간당 갱신 사이) 이력에 쌓지 않습니다.
- 수집 규모가 2배 넘게 다른 회차끼리는 비교하지 않습니다.
- 불용어(STOP_WORDS), 조사·어미 제거, 합성어 오탐 제외 규칙을 거칩니다.

## 로컬 실행

```bash
pip install -r requirements.txt
python build.py --base "" --out _site --force
python -m http.server -d _site 8000
# http://localhost:8000
```

- `--no-crawl` — 크롤 없이 운영 사이트의 `state.json`으로 렌더링만 합니다.
- `--state-file PATH` — 상태를 파일에서 읽습니다.
- `--now ISO` — 현재 시각을 주입합니다(테스트용).
- KST 12시 이후에 `--force`로 크롤하면 `data/daily/`에 오늘 리포트가 생길 수 있습니다. 테스트할 때는 `DAILY_DIR`을 임시 폴더로 지정하세요.

## 환경변수

로컬은 `.env`, Actions는 Secrets(키)와 Variables(나머지)에 넣습니다. 비워 두면 기본값을 씁니다.

| 변수 | 기본값 | 설명 |
|---|---|---|
| `GOOGLE_API_KEY` | (없음) | Gemini API 키. 없으면 AI 요약·데일리 리포트만 꺼짐 |
| `GEMINI_MODEL` | `gemini-3.8-flash` | 기본 모델 |
| `GEMINI_FALLBACK_MODEL` | `gemini-3.5-flash-lite` | 예비 모델. `none`이면 끔 |
| `SITE_URL` | `https://gumoyaz.github.io/korean-community-crawler` | canonical·sitemap·llms.txt의 기준 주소. 경로 부분이 내부 링크의 base가 됨 |
| `GSC_VERIFICATION` | (없음) | 서치 콘솔 HTML 태그 인증값. 있으면 메타태그 출력 |
| `STATE_URL` | `{SITE_URL}/data/state.json` | Actions 캐시가 없을 때 상태를 받아올 주소 |
| `MIN_INTERVAL_MIN` | `7` | 이 시간(분) 안에 다시 호출되면 크롤·배포를 건너뜀 |
| `DAILY_DIR` | `data/daily` | 데일리 리포트 저장 폴더 |

## 배포 설정 (최초 1회)

1. 리포를 **Public**으로 둡니다. 무료 플랜의 Pages와 Actions 무제한 사용 조건입니다.
2. Settings → Pages → Source를 **GitHub Actions**로 지정합니다.
3. Settings → Secrets and variables → Actions에서 Secret `GOOGLE_API_KEY`를 등록합니다.
4. Actions 탭 → **Build and deploy** → Run workflow로 첫 배포를 합니다. `main` 푸시로 돌면 크롤 없이 렌더링만 합니다.
5. [cron-job.org](https://cron-job.org)에 10분 간격 작업을 등록합니다. Actions의 `schedule`은 지연·누락이 잦아 백업으로만 둡니다.
   - URL: `POST https://api.github.com/repos/gumoyaz/korean-community-crawler/actions/workflows/pages.yml/dispatches`
   - 헤더: `Authorization: Bearer <fine-grained PAT>`, `Accept: application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28`
   - 본문: `{"ref":"main"}` (성공하면 204)
   - PAT 권한: 이 리포만 선택, Repository permissions → **Actions: Read and write**

## 산출물

| 경로 | 내용 |
|---|---|
| `/` | 메인 (JS가 `data/trends.json`을 읽어 렌더링) |
| `/data/trends.json` | 게시글, 키워드 트렌드, AI 요약, 갱신 시각 |
| `/data/state.json` | 다음 실행이 이어받을 상태 (키워드 이력, 점수 이력, 직전 글 목록) |
| `/daily/`, `/daily/YYYY-MM-DD/` | 데일리 목록·상세 (서버 렌더링) |
| `/sitemap.xml`, `/robots.txt`, `/llms.txt`, `/404.html` | SEO·AI 크롤러용 |

## SEO / AI 검색

- 페이지마다 description, canonical(끝 슬래시 통일), Open Graph, Twitter Card, JSON-LD(`WebSite` / `Article` / `CollectionPage`)를 넣습니다.
- `sitemap.xml`에는 메인·데일리 목록·날짜별 리포트가 들어갑니다. `lastmod`는 리포트 생성일입니다.
- `llms.txt`는 ChatGPT·Claude·Gemini·Perplexity 같은 AI 크롤러용 사이트 안내서입니다.
- 프로젝트 페이지(`/korean-community-crawler/`)라서 `robots.txt`는 크롤러가 읽지 않습니다. 서치 콘솔에 `sitemap.xml`을 직접 제출해야 합니다.

## 옛 데이터 이전

Railway 시절 SQLite(`daily.db`)가 있다면 JSON으로 옮길 수 있습니다.

```bash
python tools/import_daily_db.py --db daily.db --out data/daily
```
