# Ralplan Consensus Plan: feedgate-fetcher Walking Skeleton

- Source spec: `.omc/specs/deep-interview-feedgate-walking-skeleton.md`
- Mode: `--direct` (interview 생략), 자동 consensus (interactive 없음)
- Created: 2026-04-10
- **Revision 2** — Architect review 반영 (Phase 재구성, C5 spec 준수, 누락
  엔드포인트·리스크 추가)
- Status: DRAFT (Planner pass 2 완료, Critic 대기)

---

## RALPLAN-DR Summary

### Principles (5)

1. **TDD 엄격 준수 — E2E red가 첫 번째 red**. Cockburn walking skeleton의
   정의에 따라 end-to-end 테스트를 **가장 먼저** 실패 상태로 박아두고, 이후
   모든 WP는 그 테스트를 green으로 만드는 최단 경로로 정렬한다. 단위 테스트
   는 각 WP의 red-first 사이클로 함께 추가.
2. **바퀴 재발명 금지** — spec이 확정한 라이브러리만 사용. 직접 구현은 해당
   영역에 라이브러리가 없거나 glue 수준일 때만.
3. **Spec 준수 > LOC 최소** — "최소 코드"는 "LOC 최소"가 아니라 "spec 준수
   에 필요한 최소"로 재정의. LOC는 결과 지표이지 목표 지표가 아니다.
4. **단일 프로세스 asyncio** — 별도 워커 프레임워크·큐·프로세스 분리 일체
   금지.
5. **ADR/spec은 계약, 이 plan은 일정** — plan이 ADR/spec과 충돌하면 ADR/
   spec이 이긴다. plan 수정이 필요하면 plan을 고친다.

### Decision Drivers (top 3)

1. **Walking skeleton = "end-to-end 기동 먼저"** — Cockburn의 정의 그대로.
   E2E red가 첫 번째, 나머지는 이를 green으로 만드는 경로.
2. **TDD 비타협** — 모든 implementation WP는 red 테스트 선행.
3. **Spec 완전 준수** — `content_updated_at`, 생애 상태 필드, 엔드포인트
   전체 표 등 spec/feed.md + spec/entry.md + ADR 002의 모든 요구 반영.

### Viable Options

#### Option A: Bottom-up phased TDD (REJECTED by architect review)

Foundation → Pure functions → DB → API → Fetcher → Integration 순으로
쌓고 E2E는 마지막에.

**Rejected**: spec line 128이 명시한 "E2E 테스트 (1개, TDD 첫 red)"를
위반. Cockburn walking skeleton 원칙과 반대 방향. 드라이버 #1과 모순.

#### Option B: Vertical slices (per-endpoint)

엔드포인트마다 test → route → service → DB를 전부 끝내고 다음으로.

**Rejected**: walking skeleton의 본질은 "최소 깊이의 수직 스택 **한 벌**"
이지 "엔드포인트별 독립 슬라이스 **여러 벌**"이 아니다. 엔드포인트 슬라이스
방식은 각 엔드포인트가 모델·upsert·fetcher를 부분적으로만 요구해 첫 슬라이
스에서 모델 일부만 정의한 뒤 두 번째 슬라이스에서 확장하는 식의 **레이어
재작성 반복**이 생긴다. 동시에 TDD 사이클이 레이어를 왕복하며 red-first
규율이 느슨해지기 쉽다. 공정 검토 후에도 Option C가 우위.

#### Option C: E2E-first walking skeleton (CHOSEN, Architect synthesis)

**Phase 0: Foundation + E2E red placeholder** — repo 부트스트랩과 **동시에**
E2E 테스트를 작성해 red로 고정. 이후 모든 WP는 이 E2E를 단계적으로 덜
red하게 만드는 방향으로 진행.

**Phase 1~5: Layer fill** — E2E가 지금 어떤 에러로 fail하는지를 북극성으로
삼아 필요한 레이어(DB → 순수함수 → API → Fetcher → 통합 glue)를 red→green
사이클로 채움. 각 WP는 여전히 자신의 단위/통합 red 테스트를 먼저 작성.

**Phase 6: Polish** — ruff/mypy/README/coverage 확인.

**Pros**:
- Spec acceptance criteria의 "E2E 첫 red" 만족
- Cockburn walking skeleton 정의 정확히 부합
- 각 WP가 완성될 때마다 E2E의 fail 메시지가 바뀜 → 진행도 시각화
- 단위 테스트와 E2E 테스트가 공존, 둘 다 TDD red-first
- Phase간 gate는 Option A 방식(pytest/ruff/mypy)과 동일하게 유지 가능

**Cons**:
- 첫 E2E 테스트 작성 시 import할 심볼이 없어 "trivial red(import error)"
  로 시작 → "진짜 테스트"로 확장되는 타이밍을 개발자가 의식해야 함
- 완화: Phase 1의 첫 WP에서 E2E 테스트의 **assertion body를 단계적으로
  활성화**하는 규칙을 명시 (아래 Phase 1.0 참조)

### 선택과 근거

**Option C (E2E-first walking skeleton)** 선택.

- Architect가 지적한 spec line 128 위반을 해소
- 드라이버 #1·#2·#3 모두 만족
- Phase별 gate는 Option A의 장점(선명한 중간 검증)을 그대로 유지

---

## Implementation Plan

### Phase 0 — Foundation + E2E red (첫 red는 E2E)

**목표**: repo 부트스트랩 + E2E 테스트 파일을 red로 고정.

| WP | 작업 | 파일 | Gate |
|---|---|---|---|
| 0.1 | `pyproject.toml` 작성 (의존성, `[tool.uv]`, `[tool.ruff]`, `[tool.mypy]`, `[tool.pytest.ini_options]`) | `pyproject.toml` | `uv sync` 성공 |
| 0.2 | `src/feedgate/__init__.py`, `src/feedgate/config.py` (pydantic-settings), `src/feedgate/db.py` (async engine/sessionmaker factory) — **빈 함수/TODO 허용**. import만 성공하면 OK | `src/feedgate/__init__.py`, `src/feedgate/config.py`, `src/feedgate/db.py` | import 성공 |
| 0.3 | **E2E red test 작성**: `tests/test_e2e_walking_skeleton.py` — 아직 존재하지 않는 심볼을 import하는 것부터 trivial red. 이후 Phase 1~5 진행하며 단계적으로 assertion 활성화. **첫 버전 시점의 테스트 body는 완전 형태의 happy path로 작성해두되, pytest의 `xfail(strict=True)` 또는 `skip("WIP walking skeleton")` 없이 그대로 fail** | `tests/test_e2e_walking_skeleton.py` | pytest red (정확히 이 테스트 파일 하나만 red, collection error 혹은 import error 형태 허용) |
| 0.4 | `tests/conftest.py` — testcontainers Postgres fixture (`pg_container` session scope, `async_engine` session scope, `async_session_factory` session scope, 각 함수마다 transaction begin/rollback으로 격리), respx fixture (session scope) | `tests/conftest.py` | fixture 수집 성공 |
| 0.5 | **Foundation smoke test**: `tests/test_foundation.py` — testcontainers가 기동하고 `SELECT 1` 실행 가능. 이 테스트는 green이어야 함 (0.3 E2E는 red 상태 그대로) | `tests/test_foundation.py` | 이 파일만 green |

**Red 정책**: 0.3의 E2E는 Phase 5 끝나기 전까지 **red 상태**로 유지한다. 각
Phase 완료 시점에 E2E를 재실행해 "어떤 에러로 fail 중인지" 확인하고 그
메시지를 다음 WP의 지표로 삼는다.

**Gate 통과 조건**: 0.5 smoke test green + 0.3 E2E가 의도된 red.

---

### Phase 1 — DB 레이어 (E2E의 "no table" 에러 해소)

**목표**: Feed/Entry ORM 모델 + 마이그레이션 + upsert 로직 완성. spec/entry.
md의 `content_updated_at` 규칙 포함.

| WP | Red 테스트 | Green 코드 | 비고 |
|---|---|---|---|
| 1.1 | — (구조 정의 WP, TDD 면제) | `src/feedgate/models.py` — SQLAlchemy 2.0 Mapped 스타일. Feed에 본질 컬럼 + `status`/`last_successful_fetch_at`/`last_attempt_at`/`last_error_code`/`effective_url`/`created_at` + 스케줄러 메타(`next_fetch_at`/`etag`/`last_modified`/`consecutive_failures`). Entry에 본질 컬럼 + `content_updated_at NOT NULL`. **compound index `(feed_id, published_at DESC, id DESC)` 및 `(fetched_at)` 인덱스 명시** | spec/feed.md + spec/entry.md 준수 |
| 1.2 | — (Alembic boilerplate, TDD 면제) | `alembic.ini`, `alembic/env.py` (async engine + `run_sync` wrapper로 autogenerate 지원), `alembic/script.py.mako`. env.py는 1.1의 `models.Base.metadata`를 `target_metadata`로 import | SQLAlchemy 2.0 + asyncpg 공식 async 템플릿 복사 후 metadata만 교체 |
| 1.3 | — | `alembic revision --autogenerate -m "initial schema"` → `alembic/versions/0001_initial.py` 생성. **자동 생성 후 수동 검토**로 모든 컬럼·인덱스·FK가 spec과 일치하는지 확인 | diff 검토 |
| 1.4 | `tests/test_migrations.py` — 빈 DB에 `upgrade head` → `feeds`, `entries` 테이블 존재, 필수 인덱스 존재 확인 | — | pytest green |
| 1.5 | `tests/test_upsert.py` (red 세트): (a) 새 엔트리 INSERT → `fetched_at` = `content_updated_at`, (b) **같은 guid 재실행 + 컬럼 동일 → no update (fetched_at·content_updated_at 둘 다 불변)**, (c) **같은 guid 재실행 + `title` 변경 → `title` 및 `content_updated_at` 갱신, `fetched_at` 불변**, (d) `(feed_id, guid)` UNIQUE 충돌 방지 | `src/feedgate/fetcher/upsert.py` — `upsert_entries(session, feed_id, parsed_entries)`. SQLAlchemy `insert().on_conflict_do_update()`를 사용해 비교 대상 필드(title/content/url/author/published_at) 중 하나라도 변경 시 UPDATE + `content_updated_at = now()`, `fetched_at`은 `excluded` 제외. 변경 없으면 no-op (Postgres `WHERE` 절로 예외 처리) | spec/entry.md Mutation 정책 line 58~79 준수 |

**Gate 통과 조건**: Phase 1의 모든 test green. 0.3 E2E를 재실행해 "no API
route" 또는 "no parser" 쪽 에러로 fail 이동 확인.

---

### Phase 2 — 순수 함수 (URL 정규화, Feed 파서 wrapper)

**목표**: DB·HTTP 없는 pure function 레이어 완성. E2E에서 "parser 없음"
에러를 해소.

| WP | Red 테스트 | Green 코드 |
|---|---|---|
| 2.1 | `tests/test_urlnorm.py` — 대소문자, trailing slash, default port, fragment, IDN punycode, 쿼리 파라미터 보존 각각 1케이스 | `src/feedgate/urlnorm.py` — rfc3986 기반 정규화, ≤30 LOC |
| 2.2 | `tests/test_parser.py` — fake Atom/RSS 문자열을 파싱 → 내부 `ParsedFeed`/`ParsedEntry` dataclass로 매핑. guid 누락/published_at 누락/url 누락 케이스 | `src/feedgate/fetcher/parser.py` — `await anyio.to_thread.run_sync(feedparser.parse, body)` wrapper + 내부 dataclass 매핑, ≤80 LOC |

**Gate**: 2.1·2.2 green. 0.3 E2E 재실행해 에러 메시지 전진 확인.

---

### Phase 3 — API 라우터 (엔드포인트 전체 표)

**목표**: ADR 002 엔드포인트 표의 모든 엔드포인트 구현. pydantic 응답 스키마
에 생애 상태 필드 전체 포함.

| WP | Red 테스트 | Green 코드 | Acceptance |
|---|---|---|---|
| 3.0 | — (스키마 정의, TDD 면제) | `src/feedgate/schemas.py` — `FeedResponse`(id, url, effective_url, title, status, last_successful_fetch_at, last_attempt_at, last_error_code, created_at), `EntryResponse`(id, guid, feed_id, url, title, content, author, published_at, fetched_at, content_updated_at), `PaginatedEntries`, `FeedCreate` | spec/feed.md 응답 예시 + ADR 002 "생애 상태 기본 노출" |
| 3.1 | `tests/test_api_feeds_post.py` — POST에 정상 URL 등록 → 응답 JSON에 `status='active'`, `last_successful_fetch_at=None`, `last_error_code=None` 등 전 필드 포함 확인. DB에 row 존재. `created_at` 자동 설정. **`next_fetch_at = now()`로 초기화** (spec/feed.md 등록 흐름), `consecutive_failures = 0` | `src/feedgate/api/feeds.py` POST 핸들러 | ✓ POST /v1/feeds |
| 3.2 | `tests/test_api_feeds_idempotency.py` — 같은 URL 두 번 POST → 같은 id 반환, 409 아님 | POST 핸들러 확장 | ✓ 멱등 등록 |
| 3.3 | `tests/test_api_feeds_get_list.py` — 피드 여러 개 생성 → GET 목록. 빈 목록 케이스 포함 | GET /v1/feeds 핸들러 | ✓ ADR 002 엔드포인트 표 |
| 3.4 | `tests/test_api_feeds_get_single.py` — 존재하는 id → 200 + feed 객체. 없는 id → 404 | GET /v1/feeds/{id} 핸들러 | ✓ ADR 002 엔드포인트 표 |
| 3.5 | `tests/test_api_feeds_delete.py` — 피드 + 엔트리 있는 상태 → DELETE → 둘 다 삭제 (cascade 확인) | DELETE 핸들러 | ✓ cascade |
| 3.6 | `tests/test_api_entries.py` — 엔트리 여러 개 존재 → `GET /v1/entries?feed_ids=N&limit=M` → 정렬 `(published_at DESC, id DESC)` 확인. cursor 기반 페이지 넘기기 1회 → 중복 없음, limit 준수. `feed_ids` 없으면 400 | `src/feedgate/api/entries.py` + cursor 인코딩/디코딩 (stdlib base64 + json, ≤20 LOC) | ✓ keyset 페이지네이션 |
| 3.7 | `tests/test_api_healthz.py` — `GET /healthz` → 200 + `{"status": "ok"}` | `src/feedgate/api/health.py` (1~2줄) | ✓ ADR 002 line 41 |

**Gate**: 3.x 전부 green. E2E 재실행 → "scheduler 없음" 또는 "fetch_one
없음" 쪽으로 에러 전진.

---

### Phase 4 — Fetcher + Scheduler

**목표**: 한 피드를 fetch → parse → upsert하는 파이프라인과 이를 주기적으로
돌리는 tick 함수. `run()` 루프는 얇은 래퍼로 TDD 면제 영역 명시.

| WP | Red 테스트 | Green 코드 |
|---|---|---|
| 4.1 | `tests/test_fetch_one.py` — respx로 mock한 URL에 대해 `fetch_one(session, http_client, feed)` 호출 → 파싱된 엔트리가 DB에 저장, `feeds.last_successful_fetch_at`/`last_attempt_at` 갱신, **`next_fetch_at = now() + interval`로 재계산**, `consecutive_failures = 0`으로 리셋. 실패 케이스에서 `last_error_code` 세팅·`last_attempt_at` 갱신·**`next_fetch_at`은 다음 주기로 재계산**(단 `status` 전이는 walking skeleton scope 밖) | `src/feedgate/fetcher/http.py` — `fetch_one` 함수: httpx GET → `parser.parse` → `upsert.upsert_entries` → feed 타이머 필드 갱신. 에러는 logging만, 상태 전이 없음. tenacity 기반 retry/백오프는 walking skeleton scope 밖 |
| 4.2 | `tests/test_scheduler_tick.py` — active 피드 여러 개 → `await tick(session_factory, http_client)` 한 번 → 모두 fetch 시도, 성공 결과 DB에 반영 | `src/feedgate/fetcher/scheduler.py` `tick()` 함수: 현재 active 피드 전체 쿼리 → `asyncio.Semaphore(N)` 게이트 + 각각 `fetch_one()` 호출 |
| 4.3 | — (TDD 면제, 아래 정책 박스 참조) | 같은 파일의 `async def run(session_factory, http_client, interval_seconds)` — `while True: await tick(...); await asyncio.sleep(interval_seconds)`. ≤10 LOC |

**Scheduler run() 루프 TDD 정책** (명시):
> `run()`은 `tick()`을 주기적으로 호출하는 얇은 무한 루프(≤10 LOC)다. TDD로
> 의미 있는 red 테스트를 쓰기 어렵고(event loop 장악·sleep 패치 필요) 얻는
> 가치가 낮다. 다음 원칙으로 관리한다:
> 1. `run()` 자체는 단위 테스트 대상 아님 (TDD 면제 명시)
> 2. `run()`이 호출하는 `tick()`은 4.2에서 완전히 커버
> 3. `run()`의 존재·정상 종료는 F단계 E2E 또는 smoke runtime 체크로 간접
>    검증
> 4. 향후 `run()`에 로직이 추가되면(백오프 등) 그 시점에 TDD 대상으로 승격

**Gate**: 4.1·4.2 green. E2E 재실행 → "main.py 없음" 또는 lifespan 관련
에러만 남음.

---

### Phase 5 — Integration (E2E red → green)

**목표**: FastAPI 앱에 lifespan 붙이고 0.3의 E2E를 **드디어 green으로**.

| WP | 작업 | 파일 |
|---|---|---|
| 5.1 | `src/feedgate/main.py` — `@asynccontextmanager lifespan`: engine/session factory/httpx.AsyncClient 생성. **스케줄러 기동은 환경변수 `FEEDGATE_SCHEDULER_ENABLED`로 분기** (기본 `true`, E2E 테스트에서 `false`로 세팅해 race 회피). `true`면 `scheduler_task = asyncio.create_task(scheduler.run(...))`. shutdown 시 task cancel + client/engine 정리. FastAPI app에 라우터 등록 (`/v1/feeds`, `/v1/entries`, `/healthz`) | `src/feedgate/main.py` |
| 5.2 | **0.3의 E2E 테스트를 검토하고 필요 시 조정**. 현재 policy: `TestClient` 대신 `httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")` 사용. `respx.mock(using="httpx")`로 외부 URL mock. Scheduler `run()` 루프는 E2E 안에서 기동하지 않고 `tick()`을 **직접 수동 호출** (race condition 회피). 흐름: POST /v1/feeds → `await scheduler.tick(...)` 직접 호출 → GET /v1/entries 검증 | `tests/test_e2e_walking_skeleton.py` 조정 |

**Gate**: 0.3 E2E **green**. 전체 pytest green. Spec의 E2E acceptance
criterion 만족.

---

### Phase 6 — Polish

| WP | 작업 |
|---|---|
| 6.1 | `uv run ruff check .` 경고 0, `uv run ruff format .` 적용 |
| 6.2 | `uv run mypy src tests` 에러 0 |
| 6.3 | README 로컬 실행 가이드 (`uv sync` → Postgres 기동 → `alembic upgrade head` → `uv run uvicorn feedgate.main:app`) |
| 6.4 | 전체 pytest 실행, coverage 참고 (강제 게이트 아님), LOC 점검 (spec 준수가 우선, 초과해도 spec을 희생시키지 말 것) |

**Gate**: 전체 green + ruff/mypy 깨끗.

---

## Dependency Graph (revised)

```
0.1 pyproject.toml
  │
  ├─▶ 0.2 __init__/config/db (빈 껍데기)
  │         │
  │         ▼
  ├─▶ 0.3 E2E red placeholder ◀── (이 red가 Phase 5까지 북극성)
  │         │
  │         ▼
  ├─▶ 0.4 conftest.py (testcontainers fixture)
  │         │
  │         ▼
  └─▶ 0.5 foundation smoke test (green)
            │
            ▼
  1.1 models (spec 본질 컬럼 + 스케줄러 메타 + compound index)
            │
            ▼
  1.2 alembic env.py (1.1의 metadata import)
            │
            ▼
  1.3 alembic autogenerate → 0001_initial.py
            │
            ▼
  1.4 migration smoke test (green)
            │
            ▼
  1.5 upsert test (red) → upsert.py (green, content_updated_at 포함)
            │
            ▼
  2.1 urlnorm test → urlnorm.py ─┐
  2.2 parser test → parser.py ────┤
                                   ▼
  3.0 schemas.py (pydantic, 생애 상태 필드 전체)
            │
            ▼
  3.1~3.7 API 라우터들 (POST, GET list, GET single, DELETE, GET entries, healthz)
            │
            ▼
  4.1 fetch_one test → http.py
            │
            ▼
  4.2 scheduler.tick test → scheduler.py
            │
            ▼
  4.3 scheduler.run() 얇은 래퍼 (TDD 면제)
            │
            ▼
  5.1 main.py lifespan + 라우터 등록
            │
            ▼
  5.2 E2E 테스트 조정 → 0.3이 드디어 green
            │
            ▼
  6.1~6.4 polish
```

---

## Test Plan

### Red-first 순서

모든 implementation WP는 예외 없이 `*_red_test → implementation → green`.
단 TDD 면제로 명시된 WP는 예외:
- 0.1 (pyproject.toml)
- 0.2 (빈 껍데기 모듈)
- 1.1 (모델 구조 정의 — 검증은 1.4)
- 1.2 (alembic boilerplate — 검증은 1.4)
- 1.3 (autogenerate diff — 수동 검토 후 1.4가 검증)
- 3.0 (pydantic 스키마 — 검증은 3.1~3.7)
- 4.3 (`run()` 얇은 루프 — 정책 박스 참조)

### Test 레이어

| 레이어 | 도구 | 대상 |
|---|---|---|
| Unit (DB 없음) | pytest | urlnorm, parser, cursor encoding |
| Integration (DB) | pytest + testcontainers (session scope) | upsert, migration smoke, API routes, fetch_one, scheduler.tick |
| E2E | pytest + testcontainers + respx + httpx.AsyncClient/ASGITransport | `test_e2e_walking_skeleton.py` (Phase 0부터 red, Phase 5에서 green) |

### Mock 정책

- **DB mock 금지** — 전부 testcontainers 사용
- **HTTP real call 금지** — 전부 respx로 mock. `using="httpx"` 세션 scope
- **feedparser mock 금지** — 실제 XML 문자열 인풋
- **lifespan 우회 금지** — E2E는 `ASGITransport`로 진짜 lifespan 실행. 단
  scheduler `run()` 루프는 테스트 내에서 기동하지 않고 `tick()` 수동 호출로
  대체(race 회피)

### Test isolation

- `pg_container`/`async_engine`/`async_session_factory`: session scope
- 각 테스트 함수: fixture에서 begin transaction → test 실행 → rollback.
  단 **E2E 테스트는 예외** — ASGITransport로 FastAPI가 자체 세션을 열므로
  rollback 격리 불가. 대신 각 E2E 시작 시 `TRUNCATE feeds, entries
  RESTART IDENTITY CASCADE` 수행(E2E는 1개뿐이라 부담 없음)

---

## Acceptance Criteria Mapping

| Spec Criterion | WP |
|---|---|
| E2E happy path 테스트 (1개, TDD 첫 red) | 0.3 (red 작성) → 5.2 (green) |
| URL 정규화 테스트 | 2.1 |
| Feed 파서 wrapper 테스트 | 2.2 |
| Upsert 로직 테스트 (fetched_at 불변, content_updated_at 갱신) | 1.5 |
| POST /v1/feeds 멱등성 | 3.2 |
| GET /v1/entries 페이지네이션 | 3.6 |
| `ruff check` 경고 0 | 6.1 |
| `mypy` 에러 0 | 6.2 |
| `pytest` 전체 통과 | 6.4 |
| `alembic upgrade head` 성공 | 1.3, 1.4 |
| README 로컬 실행 가이드 | 6.3 |
| ADR 002 엔드포인트 표 전체 (POST, GET list, GET single, DELETE, GET entries, healthz) | 3.1, 3.3, 3.4, 3.5, 3.6, 3.7 |
| Feed 응답에 생애 상태 필드 전체 포함 | 3.0, 3.1 |
| 엔트리 `content_updated_at` NOT NULL + 차등 갱신 규칙 | 1.1, 1.5 |

**전부 매핑됨**. Architect 지적의 누락 엔드포인트·`content_updated_at`·`
healthz`·compound index 모두 반영.

---

## Non-goals (spec에서 상속, 재확인)

Walking skeleton PR에서 구현하지 **않는 것**:

- 피드 생애 상태 머신 (`active → broken → dead` 전이 로직)
- `last_error_code`의 정교한 분류 (walking skeleton은 단일 문자열만 기록)
- 보존 정책 스윕 (ADR 004)
- Prometheus 메트릭
- 구조화 로깅 (structlog 등)
- Typer/Click CLI
- Per-host rate limit
- 301 redirect 처리
- Seed 피드 자동 투입
- 인증/권한
- Docker Compose / Kubernetes 매니페스트
- CI/CD 파이프라인
- **tenacity 기반 retry/백오프 로직** (의존성은 `pyproject.toml`에 포함되지만
  walking skeleton의 `fetch_one`에서는 사용하지 않음. 후속 PR에서 상태 머신과
  함께 도입)

위 항목이 PR에 섞여 들어오려 하면 **reject**.

---

## Risk & Mitigation (revised)

| Risk | 영향 | 완화 |
|---|---|---|
| SQLAlchemy 2.0 async + Alembic async env.py boilerplate 난이도 | Phase 1.2~1.3 막힘 | 공식 SQLAlchemy 2.0 + asyncpg async 템플릿을 **그대로** 복사. autogenerate는 `run_sync` wrapper 필수 — 직접 작성 금지, 공식 예제 인용 |
| testcontainers 기동 지연 | 테스트 반복 피로 | session scope 한 번만. 함수마다 transaction rollback 격리. E2E만 truncate 사용 |
| feedparser sync 호출로 이벤트 루프 블로킹 | 스케줄러 전체 정지 | `await anyio.to_thread.run_sync` 강제. lint 규칙 또는 코드 리뷰로 직접 호출 금지 |
| keyset cursor 버그 (페이지 누락·중복) | 테스트 실패 | 3.6 테스트에서 최소 1회 이상 cursor 넘기기 포함 |
| E2E scheduler timing race | flaky 테스트 | `run()` 루프를 테스트에서 기동하지 않음. `tick()`을 수동 호출. 5.2에 명시 |
| **FastAPI TestClient + lifespan event loop 충돌** | E2E hang 또는 pending task 경고 | `TestClient` 금지. `httpx.AsyncClient(transport=ASGITransport(app=app))` 사용. lifespan은 `lifespan="on"`으로 확실히 실행 |
| **respx mock이 lifespan httpx.AsyncClient에 적용 안 됨** | E2E에서 real HTTP 나감 | `respx.mock(using="httpx")` session scope fixture. `respx.calls.assert_called()` 확인 |
| **Alembic autogenerate + async engine metadata reflection 복잡도** | 1.2~1.3 시간 초과 | "공식 템플릿이 있다"는 낙관 금지. SQLAlchemy 2.0 docs의 `async_engine` + `run_sync(context.run_migrations)` 패턴을 사전 리서치 후 적용 |
| **동시 POST /v1/feeds 같은 URL → UNIQUE 충돌 race** | 한 쪽 500 반환 | walking skeleton scope에서는 **알려진 한계**로 명시. 순차 테스트만 수행. 이후 PR에서 `INSERT ... ON CONFLICT DO NOTHING RETURNING`로 수정 |
| **LOC 목표가 spec 준수를 압박** | `content_updated_at` 로직 누락 등 단순화 | Principle #3 "spec 준수 > LOC 최소" 명시. LOC는 결과 지표. 6.4 polish에서 LOC 초과 시 **삭제 대상이 되는 것은 "덤으로 추가된 코드"지 spec 준수 로직이 아님** |

---

## ADR (in-plan, revised)

### Decision

Walking skeleton을 **Phase 0~6의 E2E-first TDD**로 구현한다.

- Phase 0에서 E2E 테스트를 red로 박아둔다
- Phase 1~5 각 WP가 E2E 에러 메시지를 한 단계씩 전진시킨다
- Phase 5에서 E2E가 green이 되면 walking skeleton 완성
- 모든 단위·통합 테스트는 각 WP의 red-first 사이클로 함께 추가

### Drivers

1. Spec line 128 "E2E 테스트 (1개, TDD 첫 red)"
2. Cockburn walking skeleton 원칙(end-to-end 기동 우선)
3. Spec/feed.md, spec/entry.md, ADR 002의 완전 준수 (엔드포인트·컬럼·인덱스
   전부)

### Alternatives Considered

- **Bottom-up phased (원 Option A)**: spec line 128 위반, Cockburn 원칙 역행
  → 기각 (Architect review)
- **Vertical slices (Option B)**: 레이어 왕복 중 TDD 규율 유지 어려움 →
  기각
- **Big-bang (Option C)**: spec TDD 제약 위반 → 원천 기각

### Why Chosen

Option C (E2E-first)는 Cockburn 원칙과 spec acceptance criteria를 동시에
만족하며, Option A의 장점인 "Phase별 선명한 gate"도 그대로 유지할 수 있다
(각 Phase 끝에서 pytest/ruff/mypy 게이트 + E2E 에러 메시지 전진 확인).

### Consequences

- Phase 0의 E2E red는 "import error" 수준의 trivial red로 시작하지만 Phase
  진행 중 점점 "진짜 테스트"가 됨. 이 전이를 의식하지 않으면 placeholder로
  방치될 위험 → Phase 5.2에서 반드시 재검토
- 약 25개의 work package (0.x=5, 1.x=5, 2.x=2, 3.x=8, 4.x=3, 5.x=2, 6.x=4)
- Spec 준수 로직(`content_updated_at` 차등 갱신, 생애 상태 필드 노출,
  compound index) 포함으로 LOC가 원래 목표 <400 프로덕션보다 약간 증가할
  가능성 — 그래도 800 total 이내 유지 시도, 초과 시 Principle #3에 따라
  spec 우선

### Follow-ups (다음 PR)

- 상태 머신 구현 (spec/feed.md `active → broken → dead` 전이)
- `last_error_code` 세분화 (표 A 기반)
- 보존 정책 스윕 (ADR 004)
- Per-host rate limit
- 301 redirect 처리 (`effective_url` 업데이트)
- Seed 피드 자동 투입 CLI
- 메트릭 엔드포인트 + 구조화 로깅
- 동시 POST race 수정 (`INSERT ... ON CONFLICT ... RETURNING` 패턴)
- WebSub 지원 여부 결정 (research note 참고)

---

## Pass Status

- [x] Planner pass 1 완료
- [x] Architect review 완료 — verdict **ITERATE** (10개 항목 지적)
- [x] Planner pass 2 완료 — Architect 지적 모두 반영
- [x] **Critic evaluation 완료 — verdict APPROVE** (10/10 Architect 지적
      Addressed 확인, blocking issues 없음, 5개 minor suggestions 제시)
- [x] **Planner pass 3 완료** — Critic minor suggestions 5개 반영:
      (1) WP 5.1 `FEEDGATE_SCHEDULER_ENABLED` flag로 E2E race 회피,
      (2) WP 3.1 `next_fetch_at = now()` 초기화 명시,
      (3) WP 4.1 `next_fetch_at` 재계산 + `consecutive_failures` 리셋 명시,
      (4) Option B 기각 근거 보강,
      (5) Non-goals에 tenacity retry 로직 명시
- [x] **Consensus 달성** — 실행 준비 완료
