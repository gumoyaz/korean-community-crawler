# CLAUDE.md — 커트 (커뮤니티 트렌드)

한국 인터넷 커뮤니티를 실시간 크롤링해 트렌드를 보여주는 Flask 웹 앱.

## 핵심 파일

| 파일 | 역할 |
|---|---|
| `app.py` | Flask 라우트, 백그라운드 스레드 관리 |
| `crawler.py` | 커뮤니티 크롤러 (`TrendCrawler` 클래스) |
| `daily.py` | 데일리 요약 생성 및 SQLite 저장 |
| `requirements.txt` | 의존성 |
| `Procfile` | Railway/gunicorn 실행 명령 |

템플릿은 `templates/`, 정적 파일은 `static/`에 있음.

## 아키텍처

Flask 앱이 시작될 때 두 개의 백그라운드 스레드를 daemon으로 실행한다:

- **`_startup` → `_background_loop`**: 서버 시작 즉시 첫 크롤링 후, 10분(`REFRESH_INTERVAL=600`)마다 자동 재크롤링
- **`_noon_scheduler`**: 매일 KST 12:00에 당일 데일리 요약 강제 생성

크롤링된 데이터는 메모리(`TrendCrawler` 인스턴스)에만 보관되며, 데일리 요약만 SQLite에 영구 저장된다.

## 환경변수

| 변수 | 필수 | 설명 |
|---|---|---|
| `GOOGLE_API_KEY` | 선택 | Gemini 2.5 Flash API 키. 없으면 AI 요약 기능 비활성화 |
| `DAILY_DB_PATH` | 선택 | SQLite DB 경로. 기본값: `data/daily.db` |
| `PORT` | 선택 | 서버 포트. gunicorn이 주입함 |

## 로컬 실행

```bash
pip install -r requirements.txt
python app.py
# http://localhost:5000
```

## 주요 설계 결정

- **상주 프로세스 필수**: 10분 크롤링 루프와 낮 12시 스케줄러가 백그라운드 스레드로 동작하므로 Vercel 같은 서버리스 플랫폼에는 배포 불가
- **SQLite 단일 파일**: 크롤링 결과는 메모리에만 보관하고 데일리 요약만 DB에 저장. Railway 배포 시 볼륨 마운트 + `DAILY_DB_PATH` 설정으로 재배포 후에도 데이터 유지
- **gunicorn workers=1**: SQLite write 충돌 방지 및 메모리 내 크롤링 데이터 일관성 유지를 위해 단일 worker 강제
- **Gemini REST 직접 호출**: `google-genai` SDK 미설치, `requests`로 REST API 직접 호출해 패키지 의존성 최소화

## 배포 (Railway)

```
web: gunicorn app:app --workers 1 --threads 4 --bind 0.0.0.0:$PORT
```

Railway 설정:
1. GitHub 레포 연결 → 푸시 시 자동 재배포
2. Variables 탭: `GOOGLE_API_KEY`, `DAILY_DB_PATH` 설정
3. Volumes 탭: `/data` 마운트 (데일리 DB 영속화)
