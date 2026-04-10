# Deep Interview Spec: feedgate-fetcher Walking Skeleton

## Metadata

- Interview ID: feedgate-walking-skeleton-2026-04-10
- Rounds: 4
- Final Ambiguity Score: **10.3%**
- Type: brownfield (ADR/spec 완비, 코드 0)
- Generated: 2026-04-10
- Threshold: 20%
- Status: **PASSED**

## Clarity Breakdown

| Dimension | Score | Weight | Weighted |
|---|---|---|---|
| Goal Clarity | 0.92 | 35% | 0.322 |
| Constraint Clarity | 0.85 | 25% | 0.213 |
| Success Criteria | 0.88 | 25% | 0.220 |
| Context Clarity | 0.95 | 15% | 0.143 |
| **Total Clarity** | | | **0.897** |
| **Ambiguity** | | | **0.103** |

## Goal

`docs/adr/` 및 `docs/spec/`에 정의된 feedgate-fetcher 서비스의 **walking
skeleton**을 TDD로 구현한다. 단일 파드 안에서 FastAPI API 서버와 asyncio
기반 in-process fetcher가 같은 프로세스로 동작하며, seed 피드를 등록하면
스케줄러가 주기적으로 fetch하여 entries를 DB에 upsert하고, `GET /v1/entries`
로 조회 가능한 상태까지 만드는 것이 이번 이터레이션의 완료 지점이다.

"Walking skeleton"의 정의: 프로덕션 코드가 모든 레이어(API → 스케줄러 →
fetcher → 파서 → DB)를 관통하되, 상태 머신·보존 정책 등 spec의 전 기능을
구현하지는 않는다. 데이터 흐름을 끝에서 끝까지 검증하는 최소한의 수직 스택.

## Constraints

### 아키텍처 제약 (확정)

- **단일 프로세스, 단일 파드**. API 서버와 fetcher/스케줄러가 **같은
  asyncio 이벤트 루프** 안에서 동작한다.
- **Postgres** 단일 인스턴스. 같은 파드 밖 별도 서비스.
- **FastAPI + uvicorn**으로 API 서버.
- **asyncio `create_task`**로 lifespan 훅에서 백그라운드 스케줄러 루프
  기동. 별도 워커 프레임워크(Celery/Arq/RQ/APScheduler 등) **사용 안 함**.
- **httpx.AsyncClient 하나**를 API와 fetcher가 공유 (연결 풀 재사용).

### 기술 스택 (확정)

| 레이어 | 라이브러리 | 버전 floor |
|---|---|---|
| Package manager | uv | latest |
| Python | 3.12 | 3.12 |
| Web framework | FastAPI | >=0.115 |
| ASGI server | uvicorn[standard] | >=0.32 |
| Validation | Pydantic v2 | >=2.9 |
| Config | pydantic-settings | >=2.6 |
| **ORM** | **SQLAlchemy 2.0 async** | >=2.0 |
| DB driver | asyncpg | >=0.29 |
| **Migrations** | **Alembic** | >=1.13 |
| HTTP client | httpx | >=0.27 |
| Feed parser | feedparser | >=6.0 |
| URL 정규화 | rfc3986 | >=2.0 |
| Retry/백오프 | tenacity | >=9.0 |
| Async 유틸 | anyio | >=4.0 |
| **Test framework** | **pytest** + **pytest-asyncio** | pytest>=8.0 |
| HTTP mock | respx | >=0.21 |
| Test DB | testcontainers[postgres] | >=4.0 |
| Coverage | pytest-cov | >=5.0 |
| Lint + format | ruff | >=0.7 |
| Type check | mypy | >=1.13 |

### 명시적으로 **사용하지 않는** 라이브러리

- Celery / Arq / RQ / Dramatiq — 단일 프로세스 asyncio로 불필요
- APScheduler — raw asyncio 루프 ~30줄이 더 단순
- SQLModel — 순수 SQLAlchemy 2.0 async가 더 안정적
- atoma — feedparser의 edge case 커버리지 미달
- requests / aiohttp — httpx가 async 표준
- black / isort / flake8 — ruff 하나로 대체
- poetry / pdm — uv가 더 빠르고 단순
- pytest-postgresql — testcontainers가 프로덕션 동일 바이너리

### TDD 제약

- **테스트 먼저, 구현은 테스트 통과 위해서만.**
- Red → Green → Refactor 사이클 엄수.
- DB mock 금지. testcontainers로 실제 Postgres 사용.
- HTTP 실호출 금지. respx로 transport 레벨 mock.

### 코드 량 목표

- 프로덕션 코드 **~400 LOC 이하**
- 테스트 코드 **~400 LOC**
- 총 **800 LOC 미만** — ADR 000의 "경계의 선명함" 원칙 범위 안.

### 결정 보류 (다음 PR 대상)

walking skeleton에 포함되지 **않는** 것:

- 피드 생애 상태 머신 (active/broken/dead 전이) — spec/feed.md
- 엔트리 content_updated_at 차등 upsert
- 보존 정책 스윕 (ADR 004)
- Prometheus 메트릭 엔드포인트
- 구조화 로깅 (structlog 등)
- Typer/Click CLI 도구
- Per-host rate limit
- 301 redirect 처리
- 에러 코드 테이블 구현
- Seed 피드 자동 투입 (수동 `POST /v1/feeds`로 대체)

이것들은 모두 spec에 정의되어 있으며 후속 PR에서 구현한다.

## Non-Goals

- **프로덕션 배포**. Dockerfile·docker-compose는 포함 가능하지만 k8s
  매니페스트·CI/CD는 이번 PR 밖.
- **인증/권한**. `/v1/` 엔드포인트는 인증 없이 노출 (ADR 002 미해결 그대로).
- **성능 튜닝**. Connection pool 크기 등은 기본값.
- **관측 스택**. 로그는 stdout, 메트릭 없음.
- **마이그레이션 이력 관리 자동화**. `alembic upgrade head` 수동 실행.
- **프론트엔드·UI**. ADR 000이 이미 scope 밖.
- **다국어·i18n**. 에러 메시지는 영어 단일.

## Acceptance Criteria

### E2E 테스트 (1개, TDD 첫 red)

- [ ] **happy path E2E 테스트**가 다음을 검증한다:
  - testcontainers로 실제 Postgres 기동
  - Alembic 마이그레이션 적용
  - FastAPI 앱을 `TestClient`로 기동 (또는 async httpx.AsyncClient)
  - `POST /v1/feeds {"url": "http://fake.test/feed.xml"}` 호출 → 201 OK 응답,
    피드 row 생성 확인
  - respx로 `http://fake.test/feed.xml` 응답을 유효한 Atom/RSS XML로 mock
  - 스케줄러를 1 tick 돌림 (직접 코루틴 호출 또는 `asyncio.sleep(0)` 수준의
    invocation)
  - `GET /v1/entries?feed_ids=<id>` 호출 → mock 피드에 포함된 엔트리 개수가
    반환됨
  - 각 엔트리가 `guid`, `url`, `title`, `published_at`, `fetched_at`
    필드를 포함

### 단위 테스트 (컴포넌트별)

- [ ] **URL 정규화 함수 테스트**: 대소문자·trailing slash·default port·
  fragment 제거를 검증
- [ ] **Feed 파서 wrapper 테스트**: feedparser 결과를 우리 내부 dataclass/
  Pydantic 모델로 매핑. guid·published_at 누락 케이스 처리
- [ ] **Upsert 로직 테스트**: (testcontainers 사용)
  - 새 엔트리 INSERT → `fetched_at` 설정 확인
  - 같은 `(feed_id, guid)` 재실행 → 중복 없음, `fetched_at` 갱신 없음 확인
    (ADR 004의 fetched_at 불변식)
- [ ] **POST /v1/feeds 멱등성 테스트**: 같은 URL을 두 번 등록 → 같은 row 반환
  (ADR 002)
- [ ] **GET /v1/entries 페이지네이션 테스트**: keyset 커서로 페이지 넘기면
  중복 없이 순회

### 비기능 요구사항

- [ ] `ruff check .` 경고 없음
- [ ] `mypy .` 에러 없음
- [ ] `pytest` 전체 통과
- [ ] `alembic upgrade head`가 빈 DB에서 성공적으로 실행됨
- [ ] README에 로컬 실행 방법 1~2줄 명시

## Assumptions Exposed & Resolved

| Assumption | Challenge | Resolution |
|---|---|---|
| 워커는 별도 프로세스가 필요하다 | 정말 별도 프로세스가 필요한가? 단일 asyncio 루프로 충분하지 않나? | 같은 프로세스, asyncio 백그라운드로 결정. Celery 계열 전부 기각. |
| 첫 빌드에 모든 spec 기능이 들어가야 한다 | walking skeleton과 full MVP의 차이는? | Walking skeleton으로 범위 확정. 상태 머신·보존·메트릭은 다음 PR로 분리. |
| ORM보다 raw SQL이 코드가 적다 | Alembic autogenerate까지 포함하면? | SQLAlchemy 2.0 async가 총 코드량에서 우위. ORM 선택. |
| TDD는 단위 테스트 중심이어야 한다 | E2E 없이 "진짜 동작"을 어떻게 보장? | 단위 + E2E 1개 하이브리드. 빠른 피드백과 통합 보장 동시 확보. |
| APScheduler가 단일 프로세스 스케줄링에 필요 | cron 표현식, persistence가 우리에게 필요한가? | 불필요. raw asyncio 루프 30줄로 대체. |
| 각 edge case(URL 이전, 도메인 소멸 등)가 이번 빌드에 포함 | 최소 통합이 먼저 동작해야 detail이 의미 있다 | spec으로 이관된 edge case들은 walking skeleton에 포함하지 않음. |

## Technical Context

### 기존 Repo 상태

- 코드: **없음** (`docs/` 디렉토리만 존재)
- ADR: 000, 001, 002, 003, 004 (완비)
- Spec: `docs/spec/feed.md`, `docs/spec/entry.md` (완비)
- Notes: `docs/notes/collection-scaling-research.md`
- Git 브랜치: `mvp-v1`
- Main: `main`

### 제안 프로젝트 레이아웃

```
feedgate-fetcher/
├── pyproject.toml
├── uv.lock
├── README.md
├── alembic.ini
├── alembic/
│   ├── env.py
│   └── versions/
│       └── 0001_initial.py        # autogenerate로 생성
├── src/
│   └── feedgate/
│       ├── __init__.py
│       ├── main.py                # FastAPI app + lifespan + scheduler task
│       ├── config.py              # pydantic-settings
│       ├── db.py                  # SQLAlchemy engine, session factory
│       ├── models.py              # Feed, Entry ORM 모델
│       ├── schemas.py             # Pydantic request/response 모델
│       ├── api/
│       │   ├── __init__.py
│       │   ├── feeds.py           # /v1/feeds 라우터
│       │   └── entries.py         # /v1/entries 라우터
│       ├── fetcher/
│       │   ├── __init__.py
│       │   ├── http.py            # httpx 세팅 + fetch_one()
│       │   ├── parser.py          # feedparser wrapper → internal dataclass
│       │   ├── upsert.py          # entries upsert 로직
│       │   └── scheduler.py       # raw asyncio 루프
│       └── urlnorm.py             # rfc3986 기반 정규화
├── tests/
│   ├── conftest.py                # testcontainers fixture, respx fixture
│   ├── test_urlnorm.py
│   ├── test_parser.py
│   ├── test_upsert.py
│   ├── test_api_feeds.py
│   ├── test_api_entries.py
│   └── test_e2e_walking_skeleton.py
└── docs/                           # (기존)
```

### 핵심 구현 노트

- **FastAPI lifespan**: `@asynccontextmanager` 기반 lifespan에서 DB 엔진
  초기화, httpx.AsyncClient 초기화, `scheduler_task = asyncio.create_task
  (scheduler.run(...))`, shutdown 시 정리.
- **Scheduler 루프**: 단순 `while True: await asyncio.sleep(N); await
  claim_and_fetch(session, http)`. N은 config. 복잡한 스케줄링 없음 —
  walking skeleton은 "모든 active 피드를 매 N초마다 fetch"로 충분.
- **feedparser는 sync**: `await anyio.to_thread.run_sync(feedparser.parse,
  body)`로 스레드풀에 던짐.
- **DB 세션**: FastAPI 의존성 주입으로 API 쪽은 request-scoped session.
  Fetcher 쪽은 루프마다 새 세션을 열고 닫음.
- **SQLAlchemy 2.0 async**: `create_async_engine("postgresql+asyncpg://...")`,
  `async_sessionmaker`, `select()` 스타일 쿼리. Mapped 컬럼 스타일 (2.0
  권장).
- **Alembic async**: Alembic env.py에서 async engine 사용을 위한 표준
  boilerplate 적용.

### Walking skeleton에서 도입되지만 spec 완전 구현은 아닌 것

- `feeds.status` 컬럼은 생성되나 값은 **항상 `'active'`** (전이 로직 없음)
- `feeds.last_successful_fetch_at`은 fetch 성공 시 갱신하지만 `broken/dead`
  판정 없음
- `entries.content_updated_at`은 컬럼만 생성, 초기값 = `fetched_at`, 차등
  갱신 로직 없음 (upsert가 맹목적 덮어쓰기)
- `entries(fetched_at)` 인덱스는 생성하지만 보존 스윕은 없음

이것들은 **스키마는 정확하게** 만들어 두되 로직만 빈 상태로 둔다. 다음 PR
에서 채운다.

## Ontology (Key Entities)

| Entity | Type | Fields | Relationships |
|---|---|---|---|
| Feed | core domain | id, url, effective_url, title, status, last_successful_fetch_at, last_attempt_at, last_error_code, created_at, (scheduler: next_fetch_at, etag, last_modified, consecutive_failures) | has many Entry |
| Entry | core domain | id, feed_id, guid, url, title, content, author, published_at, fetched_at, content_updated_at | belongs to Feed |
| WalkingSkeleton | scope boundary | E2E flow, TDD cycle | constrains Feed/Entry/Scheduler/API |
| Scheduler | supporting | asyncio loop, interval, claim function | drives Feed fetching |
| Fetcher | supporting | httpx client, feedparser wrapper, upsert logic | produces Entry from Feed |
| TestStrategy | supporting | E2E happy path, component unit tests, testcontainers, respx | validates all entities |
| TechStack | external system | FastAPI, SQLAlchemy 2.0, asyncpg, Alembic, pytest, httpx, feedparser, tenacity, uv, ruff, mypy | implements all entities |

## Ontology Convergence

| Round | Entity Count | New | Changed | Stable | Stability Ratio |
|---|---|---|---|---|---|
| 1 (process model) | 4 | 4 | 0 | 0 | N/A |
| 2 (scope + TDD + libs) | 6 | 3 (WalkingSkeleton, TestStrategy, TechStack) | 0 | 3 (Feed, Entry, Scheduler) | 50% |
| 3 (DB layer = SQLAlchemy) | 7 | 1 (Fetcher 명시화) | 1 (TechStack: SQLAlchemy 포함) | 5 | 86% |
| 4 (test strategy) | 7 | 0 | 1 (TestStrategy: E2E + unit 확정) | 6 | 100% |

Round 4에서 완전 수렴. 새 개체 없음, 기존 개체의 성질만 확정.

## Interview Transcript

<details>
<summary>Full Q&A (4 rounds)</summary>

### Round 0 — 초기 입력

**User**: "우리 피드랑 엔트리 이거 실제로 만들자 지금은 단순한 구조 단일
파드로 동작되게 만들고 디비는 포스트그레스를 쓰면 되는데 이거 어떤 기술을
써서 해야하지 일단 fastapi로 api 서버를 받으면 되는데 워커는??"

**초기 ambiguity**: 100%

### Round 1 — 프로세스 모델

**Q**: '단일 파드'에서 API 서버와 fetcher의 프로세스 관계?
**Options**: 같은 프로세스 asyncio / 별개 프로세스 / API만 / 외부 크론
**A**: 같은 프로세스, asyncio 백그라운드

**Ambiguity**: 42% (Goal: 0.70, Constraints: 0.50, Criteria: 0.30,
Context: 0.90)

### Round 2 — 범위 + TDD + 라이브러리

**Q**: Walking skeleton의 완료 기준?
**A**: Walking skeleton (API CRUD + asyncio 스케줄러 + fetch + upsert)

**추가 입력**: "이거 tdd로 진행해줘야하고 일단 사용할 라이브러리를 조사하자
코드 수는 최소로 했으면해 바퀴를 재발명하는건 피하고 싶거든"

**Agent action**: 라이브러리 조사 리포트 작성
- 툴링: uv, ruff, mypy, python 3.12
- 웹: FastAPI + uvicorn + pydantic v2
- DB: (SQLAlchemy async vs asyncpg 직접 — 사용자 결정 요청)
- Fetch: httpx + feedparser + rfc3986 + tenacity
- Test: pytest + pytest-asyncio + respx + testcontainers
- 스케줄러: raw asyncio (APScheduler 기각)
- 로깅/메트릭: 이번 PR 제외

**Ambiguity**: 22% (Goal: 0.85, Constraints: 0.55, Criteria: 0.70,
Context: 0.90)

### Round 3 — DB 레이어 결정

**Q**: SQLAlchemy 2.0 async vs asyncpg 직접?
**A**: SQLAlchemy 2.0 async (ORM이므로 우리 코드가 최소)

**Ambiguity**: 18%

### Round 4 — 테스트 전략

**Q**: TDD acceptance test 형태?
**Options**: E2E 1개+단위 조합 / E2E 여러 개 / 단위만 / BDD
**A**: E2E 1개 + 단위 테스트 조합

**Final ambiguity**: 10.3%

</details>

## Challenge Agent Modes Used

해당 없음. 4라운드 내에 임계점(20%)에 도달하여 Contrarian/Simplifier/
Ontologist 모드가 활성화되기 전에 수렴.

## Execution Bridge

다음 실행 옵션 중 선택:

1. **Ralplan → Autopilot**: 이 spec을 Planner/Architect/Critic 합의로 다듬은
   뒤 autopilot 실행. 가장 품질 높지만 시간 투자 큼.
2. **Autopilot 직행**: ralplan 생략, autopilot이 Phase 1 planning부터 시작.
3. **Ralph**: 단일 루프로 acceptance criteria가 통과할 때까지 반복.
4. **Team**: N개 에이전트 병렬로 테스트·구현 분담.
5. **Manual (직접 구현)**: 이 spec을 참고해 사용자와 대화하며 단계별 구현.

## Notes

- 이 spec은 **walking skeleton** 전용이다. 전체 MVP는 이후 별도 spec에서
  정의한다.
- 결정된 라이브러리와 디폴트(Python 3.12, src/ 레이아웃, stdlib 로깅, 메트릭
  제외, CLI 도구 제외)는 "최소 코드" 원칙에 따라 agent가 선택했다. 조정이
  필요하면 spec을 수정하거나 실행 시 재확인.
- Uber RSS URL처럼 죽은 seed 피드는 walking skeleton 검증에 사용하지 않는다.
  첫 E2E 테스트는 respx로 완전히 mock된 URL 사용.
