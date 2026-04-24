# codex advisor artifact

- Provider: codex
- Exit code: 0
- Created at: 2026-04-24T01:41:41.920Z

## Original task

Analyze this feedgate-fetcher repository thoroughly. Look at the project structure, key files, models, service logic, and tests. Provide a comprehensive technical summary: what this service does, its architecture, key components, data flow, and any notable design decisions.

## Final prompt

Analyze this feedgate-fetcher repository thoroughly. Look at the project structure, key files, models, service logic, and tests. Provide a comprehensive technical summary: what this service does, its architecture, key components, data flow, and any notable design decisions.

## Raw output

```text
이 저장소는 “피드 URL 등록 → 주기적 수집/갱신 → 엔트리 조회 API 제공”을 담당하는 독립 fetcher 서비스입니다.  
현재 구현은 walking skeleton을 넘어, `상태머신(active/broken/dead)`, `SSRF 방어`, `SKIP LOCKED 분산 claim`, `304/ETag 조건부 요청`, `retention sweep`까지 들어가 있습니다.

**구조 요약**
```text
feedgate-fetcher
├─ src/feedgate
│  ├─ main.py            앱 팩토리 + lifespan(스케줄러/retention 태스크)
│  ├─ config.py          FEEDGATE_* 설정
│  ├─ models.py          Feed / Entry ORM + 인덱스
│  ├─ api/
│  │  ├─ feeds.py        등록/조회/삭제/reactivate
│  │  ├─ entries.py      keyset 페이지네이션 조회
│  │  └─ health.py
│  ├─ fetcher/
│  │  ├─ scheduler.py    claim(SKIP LOCKED) + 병렬 fetch 실행
│  │  ├─ http.py         fetch_one 핵심 파이프라인
│  │  ├─ parser.py       feedparser 래퍼
│  │  └─ upsert.py       ON CONFLICT upsert
│  ├─ retention.py       보존 스윕(시간창 ∪ feed별 top-N)
│  ├─ ssrf.py            URL/해결 IP 검증 + transport guard
│  └─ errors.py          RFC7807 Problem Details
├─ alembic/versions      0001~0005 스키마 진화
├─ tests                 API/파서/수집/스케줄러/보안/보존/E2E
└─ docs/adr + docs/spec  설계 의사결정/계약 문서
```

**컴포넌트 관계 (핵심)**
```text
[Client]
   |
   v
[FastAPI App]
   |-- /v1/feeds, /v1/entries, /healthz
   |-- request session(dep): commit/rollback
   |
   +--> [Postgres]
   |
   +--> (lifespan)
         |-- [Scheduler Loop] --claim--> feeds(SKIP LOCKED lease)
         |         |--parallel fetch--> [fetch_one]
         |                              |-> SSRF validate + SSRFGuardTransport
         |                              |-> HTTP GET(stream, timeout, max_bytes)
         |                              |-> parse_feed(feedparser)
         |                              |-> upsert_entries(ON CONFLICT)
         |                              |-> feed status/timer 갱신
         |
         |-- [Retention Loop]
                   |-> DELETE old entries
                      keep = (fetched_at>=cutoff) UNION (feed별 최신 N개)
```

**데이터 모델**
- `Feed`: 사용자 `url`(불변), `effective_url`(301 반영), `status`, fetch 시각/에러코드, claim/백오프 메타데이터, `etag/last_modified`.
- `Entry`: 안정 식별 `(feed_id, guid)`, 본문 필드, `fetched_at`(최초 저장 시각, 불변), `content_updated_at`(내용 변경 시각).
- 인덱스가 쿼리/스케줄 조건과 맞게 설계됨.
- 참고 파일: [models.py](/Users/kangheeyong/project/feedgate-fetcher/src/feedgate/models.py), [0001_initial_schema.py](/Users/kangheeyong/project/feedgate-fetcher/alembic/versions/0001_initial_schema.py)

**핵심 서비스 로직**
1. 피드 등록
- URL 정규화 + SSRF 1차 검증(resolve=False) 후 `ON CONFLICT DO NOTHING`으로 멱등 생성.
- 파일: [feeds.py](/Users/kangheeyong/project/feedgate-fetcher/src/feedgate/api/feeds.py), [urlnorm.py](/Users/kangheeyong/project/feedgate-fetcher/src/feedgate/urlnorm.py)

2. 스케줄링/클레임
- due feed를 `FOR UPDATE SKIP LOCKED`로 claim하고 `next_fetch_at` lease 선점.
- 다중 worker 환경에서 중복 처리 방지.
- 파일: [scheduler.py](/Users/kangheeyong/project/feedgate-fetcher/src/feedgate/fetcher/scheduler.py)

3. fetch_one 파이프라인
- SSRF 재검증(resolve=True) + transport 레벨 재검증(redirect hop 포함)
- 스트리밍 수신, body cap, total budget timeout
- 429/410/304/301/5xx/content-type/oversize 등 세분 처리
- 상태 전이(active→broken→dead), 백오프+jitter, 주간 발행량 기반 다음 fetch 간격 계산
- 파일: [http.py](/Users/kangheeyong/project/feedgate-fetcher/src/feedgate/fetcher/http.py), [ssrf.py](/Users/kangheeyong/project/feedgate-fetcher/src/feedgate/ssrf.py)

4. 엔트리 저장
- `ON CONFLICT(feed_id,guid) DO UPDATE ... WHERE IS DISTINCT FROM ...`
- 동일 payload는 no-op, 변경 시 `content_updated_at`만 갱신, `fetched_at` 유지
- 파일: [upsert.py](/Users/kangheeyong/project/feedgate-fetcher/src/feedgate/fetcher/upsert.py)

5. 조회 API
- feeds: id 기반 keyset(cursor)
- entries: `(published_at DESC, id DESC)` keyset(cursor), `feed_ids` 필수
- 파일: [entries.py](/Users/kangheeyong/project/feedgate-fetcher/src/feedgate/api/entries.py)

6. 보존 스윕
- 시간창 + feed별 top-N 합집합 유지 규칙
- 파일: [retention.py](/Users/kangheeyong/project/feedgate-fetcher/src/feedgate/retention.py)

**테스트 전략**
- testcontainers Postgres 기반 통합 테스트 중심.
- 커버리지 축:
- API 계약/인증/에러 envelope
- fetch_one 상태머신/429/304/조건부요청/빈도기반 스케줄
- scheduler claim race(SKIP LOCKED), per-host 동시성, graceful drain
- SSRF 단위+통합
- retention 규칙
- E2E walking skeleton
- 참고 파일: [test_fetch_one.py](/Users/kangheeyong/project/feedgate-fetcher/tests/test_fetch_one.py), [test_scheduler_tick.py](/Users/kangheeyong/project/feedgate-fetcher/tests/test_scheduler_tick.py), [test_ssrf.py](/Users/kangheeyong/project/feedgate-fetcher/tests/test_ssrf.py), [test_e2e_walking_skeleton.py](/Users/kangheeyong/project/feedgate-fetcher/tests/test_e2e_walking_skeleton.py)

**눈에 띄는 설계 결정**
- 앱 상태를 lifespan 이전에 동기적으로 채워 테스트(ASGITransport)와 운영 경로를 일치시킴.
- 스케줄러 claim 트랜잭션과 fetch 트랜잭션을 분리해 장애 격리.
- 내부 메타데이터는 API에 숨기고, 상태/오류는 API에 노출.
- RFC7807 에러 포맷 일관화.
- 참고: [main.py](/Users/kangheeyong/project/feedgate-fetcher/src/feedgate/main.py), [errors.py](/Users/kangheeyong/project/feedgate-fetcher/src/feedgate/errors.py), [ADR 003](/Users/kangheeyong/project/feedgate-fetcher/docs/adr/003-collection-is-internal-implementation.md)

**추가 관찰**
- 문서 일부(`docs/spec/resilience.md`, `docs/tests/resilience-test-cases.md`)는 “미구현” 표기가 남아 있지만, 코드/테스트는 이미 상당수 구현됨.
- `ErrorCode.PARSE_ERROR` enum은 있으나 `fetch_one` 분류 경로에서는 사실상 거의 안 타는 구조(현재 parse 실패가 일반 예외 경로로 갈 가능성).  
  이 부분은 문서-코드 정합성 점검 포인트입니다.


Reading additional input from stdin...
OpenAI Codex v0.120.0 (research preview)
--------
workdir: /Users/kangheeyong/project/feedgate-fetcher
model: gpt-5.3-codex
provider: openai
approval: never
sandbox: danger-full-access
reasoning effort: medium
reasoning summaries: none
session id: 019dbd23-bd0f-73b1-a2bd-02dde0d21e73
--------
user
Analyze this feedgate-fetcher repository thoroughly. Look at the project structure, key files, models, service logic, and tests. Provide a comprehensive technical summary: what this service does, its architecture, key components, data flow, and any notable design decisions.
codex
요청하신 대로 저장소를 전체적으로 훑어서 구조, 핵심 파일, 모델/서비스 로직, 테스트까지 확인한 뒤 아키텍처와 데이터 흐름을 ASCII 다이어그램으로 정리하겠습니다. 먼저 파일 구조와 진입점을 빠르게 식별하겠습니다.
exec
/bin/zsh -lc 'ls -la' in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
total 616
drwxr-xr-x@ 22 kangheeyong  staff     704 Apr 23 10:01 .
drwxr-xr-x@ 78 kangheeyong  staff    2496 Apr 23 09:32 ..
-rw-r--r--@  1 kangheeyong  staff     168 Apr 23 09:32 .dockerignore
drwxr-xr-x@ 14 kangheeyong  staff     448 Apr 24 10:23 .git
drwxr-xr-x@  3 kangheeyong  staff      96 Apr 23 09:32 .github
-rw-r--r--@  1 kangheeyong  staff    4962 Apr 23 09:32 .gitignore
drwxr-xr-x@  6 kangheeyong  staff     192 Apr 24 10:38 .omc
drwxr-xr-x@  6 kangheeyong  staff     192 Apr 23 10:01 .pytest_cache
drwxr-xr-x@  3 kangheeyong  staff      96 Apr 23 09:46 .repos
drwxr-xr-x@  8 kangheeyong  staff     256 Apr 23 10:01 .venv
-rw-r--r--@  1 kangheeyong  staff    1513 Apr 23 09:32 Dockerfile
-rw-r--r--@  1 kangheeyong  staff   11357 Apr 23 09:32 LICENSE
-rw-r--r--@  1 kangheeyong  staff    6456 Apr 23 09:32 README.md
drwxr-xr-x@  6 kangheeyong  staff     192 Apr 23 10:01 alembic
-rw-r--r--@  1 kangheeyong  staff     604 Apr 23 09:32 alembic.ini
-rw-r--r--@  1 kangheeyong  staff    3179 Apr 23 09:32 docker-compose.yml
drwxr-xr-x@  7 kangheeyong  staff     224 Apr 23 09:32 docs
-rw-r--r--@  1 kangheeyong  staff    1465 Apr 23 09:32 pyproject.toml
drwxr-xr-x@  7 kangheeyong  staff     224 Apr 23 09:32 scripts
drwxr-xr-x@  3 kangheeyong  staff      96 Apr 23 09:32 src
drwxr-xr-x@ 19 kangheeyong  staff     608 Apr 24 10:38 tests
-rw-r--r--@  1 kangheeyong  staff  266131 Apr 23 09:32 uv.lock

codex
이제 코드 파일 목록을 빠르게 분류해서 `앱 진입점/도메인 모델/서비스/인프라/테스트` 축으로 읽겠습니다.
exec
/bin/zsh -lc 'rg --files' in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
src/feedgate/urlnorm.py
src/feedgate/main.py
src/feedgate/errors.py
src/feedgate/ssrf.py
src/feedgate/api/entries.py
src/feedgate/api/__init__.py
src/feedgate/api/feeds.py
src/feedgate/api/health.py
src/feedgate/retention.py
src/feedgate/lifecycle.py
src/feedgate/schemas.py
src/feedgate/fetcher/upsert.py
src/feedgate/fetcher/scheduler.py
src/feedgate/fetcher/http.py
src/feedgate/fetcher/parser.py
src/feedgate/fetcher/__init__.py
src/feedgate/__init__.py
src/feedgate/models.py
src/feedgate/config.py
src/feedgate/db.py
alembic/versions/0003_retention_index.py
alembic/versions/0004_drop_dead_etag_columns.py
alembic/versions/0002_claim_index_coverage.py
alembic/versions/0005_etag_last_modified.py
alembic/versions/0001_initial_schema.py
alembic/env.py
alembic/script.py.mako
docker-compose.yml
scripts/live_verify.py
scripts/overnight_teardown.sh
scripts/live_verify_loop.sh
scripts/overnight_setup.sh
scripts/seed_feeds.py
README.md
alembic.ini
uv.lock
LICENSE
pyproject.toml
Dockerfile
docs/notes/collection-scaling-research.md
docs/spec/feed.md
docs/spec/resilience.md
docs/spec/api.md
docs/spec/entry.md
tests/test_api_auth.py
tests/test_e2e_walking_skeleton.py
tests/test_lifespan_drain.py
tests/test_urlnorm.py
tests/test_foundation.py
tests/test_api_entries.py
tests/test_api_feeds.py
tests/test_ssrf.py
tests/test_migrations.py
tests/test_upsert.py
tests/test_errors.py
tests/test_scheduler_tick.py
tests/test_fetch_one.py
tests/conftest.py
tests/test_api_health.py
tests/test_retention.py
tests/test_parser.py
docs/tests/resilience-test-cases.md
docs/deploy/local-multi-worker.md
docs/adr/002-http-api-contract.md
docs/adr/001-core-data-model.md
docs/adr/004-entry-retention-policy.md
docs/adr/003-collection-is-internal-implementation.md
docs/adr/000-service-purpose-and-scope.md

exec
/bin/zsh -lc "sed -n '1,240p' pyproject.toml" in /Users/kangheeyong/project/feedgate-fetcher
exec
/bin/zsh -lc "sed -n '1,240p' README.md" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
[project]
name = "feedgate-fetcher"
version = "0.0.1"
description = "RSS/Atom feed fetcher microservice — walking skeleton"
requires-python = ">=3.12"
readme = "README.md"
license = { text = "MIT" }
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "pydantic>=2.9",
    "pydantic-settings>=2.6",
    "sqlalchemy[asyncio]>=2.0",
    "asyncpg>=0.29",
    "alembic>=1.13",
    "httpx>=0.27",
    "feedparser>=6.0",
    "rfc3986>=2.0",
    "tenacity>=9.0",
    "anyio>=4.0",
]

[dependency-groups]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.24",
    "pytest-cov>=5.0",
    "respx>=0.21",
    "testcontainers[postgres]>=4.0",
    "ruff>=0.7",
    "mypy>=1.13",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/feedgate"]

[tool.ruff]
line-length = 100
target-version = "py312"
src = ["src", "tests"]

[tool.ruff.lint]
select = ["E", "W", "F", "I", "B", "UP", "SIM", "RUF"]

[tool.ruff.lint.per-file-ignores]
"tests/**/*.py" = ["S101"]

[tool.mypy]
python_version = "3.12"
strict = true
packages = ["feedgate"]
mypy_path = "src"

[[tool.mypy.overrides]]
module = ["feedparser.*", "rfc3986.*", "testcontainers.*"]
ignore_missing_imports = true

[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "session"
asyncio_default_test_loop_scope = "session"
testpaths = ["tests"]
pythonpath = ["src"]
addopts = "-ra --strict-markers"

 succeeded in 0ms:
# feedgate-fetcher

An independent RSS/Atom feed fetcher microservice. Accepts feed URLs,
periodically crawls them, stores entries with deduplication, and exposes
everything via a small HTTP API. Intentionally scoped to "fetch-and-serve"
— no users, no subscriptions, no UI. Other services layer on top.

Service contract lives in `docs/adr/` and `docs/spec/`. Start with
[`docs/adr/000-service-purpose-and-scope.md`](docs/adr/000-service-purpose-and-scope.md).

## Status

**Walking skeleton.** The minimal end-to-end vertical slice is green:
register a feed → scheduler fetches it → entries are queryable via the
API. State machine, retention sweep, metrics, per-host rate limit,
WebSub, and many other features from the spec are intentionally
deferred to a later iteration (see
[`.omc/plans/ralplan-feedgate-walking-skeleton.md`](.omc/plans/ralplan-feedgate-walking-skeleton.md)
non-goals).

## Tech stack

| Concern | Choice |
|---|---|
| Runtime | Python 3.12, single-process asyncio |
| Package manager | [uv](https://github.com/astral-sh/uv) |
| Web framework | FastAPI + uvicorn |
| ORM / migrations | SQLAlchemy 2.0 async + Alembic |
| DB driver | asyncpg (Postgres 16) |
| HTTP client | httpx.AsyncClient |
| Feed parser | feedparser (dispatched via anyio.to_thread) |
| Testing | pytest + pytest-asyncio + testcontainers + respx |
| Lint + format | ruff |
| Type check | mypy --strict |

## Local setup

Prerequisites: `uv`, Docker (for the Postgres container used by the
test suite and for local runs).

```bash
# 1. Install dependencies
uv sync

# 2. Start a local Postgres (testcontainers handles this for tests,
#    but you need one for running the server locally)
docker run --rm -d --name feedgate-pg \
  -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=feedgate \
  -p 5432:5432 \
  postgres:16-alpine

# 3. Apply migrations
export FEEDGATE_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/feedgate
uv run alembic upgrade head

# 4. Run the server (scheduler auto-starts)
uv run uvicorn feedgate.main:create_app --factory --reload
```

The API is then available at `http://127.0.0.1:8000`:

```bash
# Register a feed
curl -X POST http://127.0.0.1:8000/v1/feeds \
  -H "content-type: application/json" \
  -d '{"url": "https://feeds.example.com/atom.xml"}'

# List entries for that feed
curl "http://127.0.0.1:8000/v1/entries?feed_ids=1&limit=10"

# Health check
curl http://127.0.0.1:8000/healthz
```

## Configuration

All settings are read from `FEEDGATE_*` environment variables via
pydantic-settings. See `src/feedgate/config.py` for the full list. The
most common:

| Variable | Default | Meaning |
|---|---|---|
| `FEEDGATE_DATABASE_URL` | `postgresql+asyncpg://postgres:postgres@localhost:5432/feedgate` | asyncpg DSN |
| `FEEDGATE_FETCH_INTERVAL_SECONDS` | `60` | Scheduler loop interval |
| `FEEDGATE_FETCH_CONNECT_TIMEOUT_SECONDS` | `5.0` | TCP connect phase timeout |
| `FEEDGATE_FETCH_READ_TIMEOUT_SECONDS` | `15.0` | Per-chunk read inactivity timeout |
| `FEEDGATE_FETCH_WRITE_TIMEOUT_SECONDS` | `10.0` | Request body write timeout |
| `FEEDGATE_FETCH_POOL_TIMEOUT_SECONDS` | `5.0` | Connection pool acquisition timeout |
| `FEEDGATE_FETCH_TOTAL_BUDGET_SECONDS` | `30.0` | Hard wall-clock cap per fetch (slow-loris guard) |
| `FEEDGATE_FETCH_MAX_BYTES` | `5242880` | Response body cap |
| `FEEDGATE_FETCH_USER_AGENT` | `feedgate-fetcher/0.0.1 ...` | User-Agent header |
| `FEEDGATE_SCHEDULER_ENABLED` | `true` | Disable to skip the background scheduler task |

## Development

```bash
# Lint + format
uv run ruff check .
uv run ruff format .

# Type check
uv run mypy src tests

# Run tests (spins up a Postgres container via testcontainers)
uv run pytest
```

The full test suite takes ~3 seconds after the first container boot.
The walking-skeleton E2E test lives in
[`tests/test_e2e_walking_skeleton.py`](tests/test_e2e_walking_skeleton.py)
and is the north-star check: if that is green, the full fetch pipeline
is wired end-to-end.

## Project layout

```
feedgate-fetcher/
├── pyproject.toml
├── alembic.ini
├── alembic/
│   ├── env.py                   # async engine
│   └── versions/
│       └── 0001_initial_schema.py
├── src/feedgate/
│   ├── main.py                  # create_app() + lifespan
│   ├── config.py                # pydantic-settings
│   ├── db.py                    # engine / session factory
│   ├── models.py                # SQLAlchemy 2.0 Mapped
│   ├── schemas.py               # pydantic request/response
│   ├── urlnorm.py               # URL normalization
│   ├── api/
│   │   ├── __init__.py          # router registration + get_session
│   │   ├── feeds.py
│   │   ├── entries.py
│   │   └── health.py
│   └── fetcher/
│       ├── parser.py            # feedparser wrapper
│       ├── upsert.py            # ON CONFLICT DO UPDATE ... WHERE IS DISTINCT
│       ├── http.py              # fetch_one pipeline
│       └── scheduler.py         # tick_once + run loop
├── tests/
│   ├── conftest.py
│   ├── test_foundation.py
│   ├── test_migrations.py
│   ├── test_urlnorm.py
│   ├── test_parser.py
│   ├── test_upsert.py
│   ├── test_api_health.py
│   ├── test_api_feeds.py
│   ├── test_api_entries.py
│   ├── test_fetch_one.py
│   ├── test_scheduler_tick.py
│   └── test_e2e_walking_skeleton.py
└── docs/
    ├── adr/                      # architecture decision records
    ├── spec/                     # feed + entry spec
    └── notes/                    # research notes
```

## Where to read next

- [`docs/adr/000-service-purpose-and-scope.md`](docs/adr/000-service-purpose-and-scope.md)
  — the service's boundary
- [`docs/adr/001-core-data-model.md`](docs/adr/001-core-data-model.md)
  — inviolable data invariants (guid identity, `fetched_at`
  immutability, upsert semantics, feed lifecycle visibility)
- [`docs/spec/feed.md`](docs/spec/feed.md) and
  [`docs/spec/entry.md`](docs/spec/entry.md) — full table schemas and
  operational rules
- [`.omc/plans/ralplan-feedgate-walking-skeleton.md`](.omc/plans/ralplan-feedgate-walking-skeleton.md)
  — the work plan that drove this first iteration

## License

MIT — see [`LICENSE`](LICENSE).

exec
/bin/zsh -lc "sed -n '1,260p' src/feedgate/main.py" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
"""FastAPI app factory + lifespan wiring.

``create_app()`` builds a FastAPI instance whose ``app.state`` is fully
populated **synchronously** (so tests over ``ASGITransport`` work
without running the ASGI lifespan at all). The lifespan is used only
for the scheduler background task and end-of-process resource cleanup.

Gated state:
  * ``FEEDGATE_SCHEDULER_ENABLED=false`` disables the background
    scheduler task entirely. Tests set this and drive ticks manually
    via ``scheduler.tick_once(app)`` to avoid racing with the
    background loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from feedgate import retention
from feedgate.api import register_routers
from feedgate.config import get_settings
from feedgate.db import make_engine, make_session_factory
from feedgate.errors import register_exception_handlers
from feedgate.fetcher import scheduler
from feedgate.ssrf import SSRFGuardTransport

logger = logging.getLogger(__name__)


async def _drain_background_task(
    task: asyncio.Task[None] | None,
    stop_event: asyncio.Event,
    *,
    name: str,
    drain_seconds: float,
) -> None:
    """Cooperatively drain a background loop on lifespan shutdown.

    Sets ``stop_event``, waits up to ``drain_seconds`` for the task to
    finish its current iteration and exit cleanly, and force-cancels
    if the budget runs out. Force-cancel is the *fallback* path — it
    leaves any in-flight ``fetch_one`` claims dangling until the SKIP
    LOCKED lease TTL elapses, so the budget should be sized to avoid
    hitting it on healthy shutdowns.

    Extracted from the lifespan body specifically so unit tests can
    exercise the timeout/force-cancel branch without spinning up a
    full FastAPI app.
    """
    if task is None:
        return
    stop_event.set()
    try:
        await asyncio.wait_for(task, timeout=drain_seconds)
    except TimeoutError:
        logger.warning(
            "%s task did not drain within %.1fs; force-cancelling",
            name,
            drain_seconds,
        )
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    except Exception:
        logger.exception("%s task raised during drain", name)


def create_app() -> FastAPI:
    """Build a wired-up FastAPI app.

    Engine, session_factory, and ``httpx.AsyncClient`` are created
    eagerly and attached to ``app.state`` so ASGI test clients that
    skip lifespan can still use every route and ``tick_once``. The
    lifespan is responsible only for (a) starting the scheduler task
    when enabled and (b) cleaning up resources at shutdown.
    """
    settings = get_settings()
    engine = make_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_recycle=settings.db_pool_recycle,
    )
    session_factory = make_session_factory(engine)
    # Wrap the default transport with the SSRF guard so that *every*
    # outbound request — including any redirect httpx follows on its
    # own — re-validates the destination URL. Pre-validation in
    # fetch_one only catches the initial host; the transport guard is
    # what keeps a 302 to ``http://169.254.169.254/`` from leaking out.
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=settings.fetch_connect_timeout_seconds,
            read=settings.fetch_read_timeout_seconds,
            write=settings.fetch_write_timeout_seconds,
            pool=settings.fetch_pool_timeout_seconds,
        ),
        transport=SSRFGuardTransport(httpx.AsyncHTTPTransport()),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        scheduler_task: asyncio.Task[None] | None = None
        retention_task: asyncio.Task[None] | None = None
        scheduler_stop = asyncio.Event()
        retention_stop = asyncio.Event()
        if settings.scheduler_enabled:
            scheduler_task = asyncio.create_task(scheduler.run(app, stop_event=scheduler_stop))
            logger.info(
                "scheduler started, interval=%ss",
                settings.fetch_interval_seconds,
            )
        else:
            logger.info("scheduler disabled via FEEDGATE_SCHEDULER_ENABLED=false")
        if settings.retention_enabled:
            retention_task = asyncio.create_task(retention.run(app, stop_event=retention_stop))
            logger.info(
                "retention sweeper started, interval=%ss days=%s min_per_feed=%s",
                settings.retention_sweep_interval_seconds,
                settings.retention_days,
                settings.retention_min_per_feed,
            )
        else:
            logger.info("retention disabled via FEEDGATE_RETENTION_ENABLED=false")
        try:
            yield
        finally:
            # Graceful drain: see ``_drain_background_task`` docstring.
            drain_budget = settings.shutdown_drain_seconds
            await _drain_background_task(
                scheduler_task,
                scheduler_stop,
                name="scheduler",
                drain_seconds=drain_budget,
            )
            await _drain_background_task(
                retention_task,
                retention_stop,
                name="retention",
                drain_seconds=drain_budget,
            )
            await http_client.aclose()
            await engine.dispose()

    app = FastAPI(title="feedgate-fetcher", lifespan=lifespan)
    app.state.session_factory = session_factory
    app.state.http_client = http_client
    app.state.fetch_interval_seconds = settings.fetch_interval_seconds
    app.state.fetch_user_agent = settings.fetch_user_agent
    app.state.fetch_max_bytes = settings.fetch_max_bytes
    app.state.fetch_total_budget_seconds = settings.fetch_total_budget_seconds
    app.state.fetch_max_entries_initial = settings.fetch_max_entries_initial
    app.state.fetch_concurrency = settings.fetch_concurrency
    app.state.fetch_per_host_concurrency = settings.fetch_per_host_concurrency
    app.state.shutdown_drain_seconds = settings.shutdown_drain_seconds
    app.state.fetch_claim_batch_size = settings.fetch_claim_batch_size
    app.state.fetch_claim_ttl_seconds = settings.fetch_claim_ttl_seconds
    app.state.entry_frequency_min_interval_seconds = settings.entry_frequency_min_interval_seconds
    app.state.entry_frequency_max_interval_seconds = settings.entry_frequency_max_interval_seconds
    app.state.entry_frequency_factor = settings.entry_frequency_factor
    app.state.retention_days = settings.retention_days
    app.state.retention_min_per_feed = settings.retention_min_per_feed
    app.state.retention_sweep_interval_seconds = settings.retention_sweep_interval_seconds
    app.state.broken_threshold = settings.broken_threshold
    app.state.dead_duration_days = settings.dead_duration_days
    app.state.broken_max_backoff_seconds = settings.broken_max_backoff_seconds
    app.state.backoff_jitter_ratio = settings.backoff_jitter_ratio
    app.state.dead_probe_interval_days = settings.dead_probe_interval_days
    app.state.api_entries_max_feed_ids = settings.api_entries_max_feed_ids
    app.state.api_entries_default_limit = settings.api_entries_default_limit
    app.state.api_entries_max_limit = settings.api_entries_max_limit
    app.state.api_feeds_max_limit = settings.api_feeds_max_limit
    app.state.api_key = settings.api_key
    register_routers(app)
    register_exception_handlers(app)
    return app


# Run under uvicorn with:  uvicorn feedgate.main:create_app --factory

codex
핵심 런타임은 확인했습니다. 지금부터 `설정/DB 모델/라우터/수집 파이프라인/스케줄러/보존정책` 코드를 모듈별로 읽어서 실제 동작 순서를 정리하겠습니다.
exec
/bin/zsh -lc "sed -n '1,260p' src/feedgate/config.py" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
"""Application configuration via pydantic-settings."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FEEDGATE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/feedgate"
    # Base pool for scheduler(4) + API headroom + retention worker.
    db_pool_size: int = 8
    # Temporary burst capacity when base pool is fully claimed.
    db_max_overflow: int = 4
    # Seconds to wait for a DB connection before raising timeout.
    db_pool_timeout: int = 30
    # Recycle idle-ish connections periodically to avoid stale sockets.
    db_pool_recycle: int = 1800
    fetch_interval_seconds: int = 60
    # Per-phase HTTP timeouts (httpx.Timeout). Splitting these is the
    # primary defense against slow-loris-style upstreams that drip-feed
    # bytes just slowly enough to keep a worker tied up. ``read`` is
    # the load-bearing one (per-chunk inactivity) and is intentionally
    # tighter than the historical 20s blanket. ``connect`` is short
    # because TCP handshake should never take 20s on a healthy host.
    fetch_connect_timeout_seconds: float = 5.0
    fetch_read_timeout_seconds: float = 15.0
    fetch_write_timeout_seconds: float = 10.0
    fetch_pool_timeout_seconds: float = 5.0
    # Hard total wall-clock budget for one ``fetch_one`` call,
    # enforced via ``asyncio.timeout``. Even if every individual chunk
    # arrives within ``read``, an upstream that streams a 200-byte
    # body across many small chunks can still pin a worker; this
    # bound caps the total time and reclassifies the failure as
    # ``ErrorCode.TIMEOUT``. Set comfortably above the sum of the
    # per-phase timeouts so it only fires on pathological cases.
    fetch_total_budget_seconds: float = 30.0
    fetch_max_bytes: int = 5 * 1024 * 1024
    fetch_user_agent: str = "feedgate-fetcher/0.0.1 (+https://github.com/feedgate)"
    fetch_max_entries_initial: int = 50
    fetch_concurrency: int = 4
    # Per-host concurrency cap. The global ``fetch_concurrency`` bounds
    # how many feeds we fetch simultaneously across the whole tick;
    # this knob bounds how many of those can target the **same** host.
    # Default 1 means same-host requests are fully serialized within a
    # tick — important when one origin hosts dozens of our feeds (e.g.
    # all the GitHub release feeds), so we never look like a DDoS to
    # any single upstream. The cap is per-tick (the dict is rebuilt
    # every tick_once); cross-tick spacing is handled by the existing
    # ``next_fetch_at`` schedule.
    fetch_per_host_concurrency: int = 1
    # On shutdown, the lifespan signals the background tasks via a
    # stop ``asyncio.Event`` and gives each one this many seconds to
    # finish its current iteration cleanly. Tasks that overrun the
    # budget are force-cancelled.
    #
    # Sizing: a worst-case tick processes ``ceil(claim_batch_size /
    # fetch_concurrency) * fetch_total_budget_seconds`` worth of
    # serialized batches before its semaphore queue drains. With the
    # default knobs (claim_batch_size=8, fetch_concurrency=4,
    # fetch_total_budget=30s) that is ``2 * 30 = 60s``. We then add
    # a 30s safety margin for retention sweep + DB commit slack.
    # Anything shorter would force-cancel a healthy in-flight tick
    # and leave its claimed feeds dangling until the SKIP LOCKED
    # lease TTL (180s) expires.
    shutdown_drain_seconds: float = 90.0
    # Distributed-claim tuning for the scheduler's SKIP LOCKED loop.
    # A tick atomically reserves up to `fetch_claim_batch_size` feeds
    # by advancing their `next_fetch_at` to `now + claim_ttl_seconds`
    # (and `last_attempt_at = now`). Another worker running in
    # parallel sees the bumped timestamps and skips the feed until the
    # lease expires, giving crash-safe at-least-once semantics without
    # an external queue.
    fetch_claim_batch_size: int = 8
    fetch_claim_ttl_seconds: int = 180
    entry_frequency_min_interval_seconds: int = 300
    entry_frequency_max_interval_seconds: int = 86400
    entry_frequency_factor: int = 1
    scheduler_enabled: bool = True

    # Retention policy (ADR 004, docs/spec/entry.md).
    # Entries are kept if they fall in EITHER the time window
    # (fetched_at >= now - retention_days) OR the per-feed top-N
    # window (most recent retention_min_per_feed by fetched_at DESC).
    # The sweeper runs every retention_sweep_interval_seconds.
    retention_days: int = 90
    retention_min_per_feed: int = 20
    retention_sweep_interval_seconds: int = 3600
    retention_enabled: bool = True

    # Feed lifecycle state machine (docs/spec/feed.md).
    # active -> broken after `broken_threshold` consecutive failures.
    # broken -> dead when (now - last_successful_fetch_at) exceeds
    # `dead_duration_days`, using `created_at` as fallback when no
    # success has ever been recorded. http_410 is an immediate dead
    # transition from any state.
    broken_threshold: int = 3
    dead_duration_days: int = 7
    broken_max_backoff_seconds: int = 3600
    backoff_jitter_ratio: float = 0.25
    dead_probe_interval_days: int = 7

    # API pagination defaults (env-overridable).
    api_entries_max_feed_ids: int = 200
    api_entries_default_limit: int = 50
    api_entries_max_limit: int = 200
    api_feeds_max_limit: int = 200

    api_key: str = ""  # empty = no auth


def get_settings() -> Settings:
    return Settings()

exec
/bin/zsh -lc "sed -n '1,260p' src/feedgate/db.py" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
"""Database engine and session factory.

Walking skeleton: minimal async engine + session_factory plumbing. Used by
FastAPI lifespan (to create at startup, dispose at shutdown), by tests
(fixtures in conftest.py), and by the fetcher (to open per-tick sessions).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def make_engine(
    database_url: str,
    *,
    pool_size: int = 8,
    max_overflow: int = 4,
    pool_timeout: int = 30,
    pool_recycle: int = 1800,
) -> AsyncEngine:
    """Create an async engine. URL must use the asyncpg driver."""
    return create_async_engine(
        database_url,
        future=True,
        echo=False,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=pool_timeout,
        pool_recycle=pool_recycle,
    )


def make_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )

exec
/bin/zsh -lc "sed -n '1,320p' src/feedgate/models.py" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
"""SQLAlchemy 2.0 ORM models.

Contract lives in ADR 001 (invariants) and docs/spec/feed.md +
docs/spec/entry.md (columns, indexes, lifecycle). Walking skeleton
creates all columns the spec requires — logic for status transitions,
error coding, etc. is deferred per the plan's non-goals, but the
schema is complete.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""


class Feed(Base):
    __tablename__ = "feeds"

    # Core identity (ADR 001, spec/feed.md)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    url: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    effective_url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Lifecycle (API-exposed, spec/feed.md)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="active")
    last_successful_fetch_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # Scheduler metadata (internal only, NOT API-exposed per ADR 003)
    next_fetch_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Circuit breaker counter used by the feed lifecycle state machine
    # (spec/feed.md). Incremented on every fetch failure, reset to 0
    # on success; drives the active -> broken transition at threshold.
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    # HTTP conditional-request cache validators (internal only, NOT API-exposed
    # per ADR 003). Stored after each successful 200 response and sent as
    # If-None-Match / If-Modified-Since on the next fetch to enable 304 support.
    etag: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_modified: Mapped[str | None] = mapped_column(Text, nullable=True)

    entries: Mapped[list[Entry]] = relationship(
        "Entry",
        back_populates="feed",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        Index("ix_feeds_status", "status"),
        Index(
            "ix_feeds_due_not_dead",
            "next_fetch_at",
            # Covers due path: status != 'dead' AND next_fetch_at <= now.
            postgresql_where="status <> 'dead'",
        ),
        Index(
            "ix_feeds_dead_last_attempt",
            "last_attempt_at",
            # Covers dead-probe path: status = 'dead' AND last_attempt_at ...
            postgresql_where="status = 'dead'",
        ),
    )


class Entry(Base):
    __tablename__ = "entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    feed_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("feeds.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Stable per-feed identifier (ADR 001 invariant #2)
    guid: Mapped[str] = mapped_column(Text, nullable=False)

    # Content fields (mutable via upsert per spec/entry.md)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    author: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Retention clock — NEVER mutated by upsert (ADR 001 invariant #4, ADR 004)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Last content-bearing change. Equal to fetched_at on first insert.
    content_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    feed: Mapped[Feed] = relationship("Feed", back_populates="entries")

    __table_args__ = (
        UniqueConstraint("feed_id", "guid", name="uq_entries_feed_guid"),
        Index("ix_entries_fetched_at", "fetched_at"),
    )


# Compound index with DESC ordering matches the keyset sort key
# `(published_at DESC, id DESC)` used by GET /v1/entries (ADR 002).
# Defined outside __table_args__ so the class attributes (with .desc())
# are resolvable. Attaches automatically to Entry.__table__.
Index(
    "ix_entries_feed_pub_id",
    Entry.feed_id,
    Entry.published_at.desc(),
    Entry.id.desc(),
)

# Matches retention.sweep window sort key:
# row_number() OVER (PARTITION BY feed_id ORDER BY fetched_at DESC, id DESC).
Index(
    "ix_entries_feed_fetched_id",
    Entry.feed_id,
    Entry.fetched_at.desc(),
    Entry.id.desc(),
)

exec
/bin/zsh -lc "sed -n '1,320p' src/feedgate/schemas.py" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
"""Pydantic request/response schemas.

Shapes the API contract defined in ADR 002 and docs/spec/feed.md /
docs/spec/entry.md. Feed responses carry the full lifecycle field set
(status, last_successful_fetch_at, last_attempt_at, last_error_code,
effective_url) so clients can judge feed health without a second call.
Entry responses carry both `fetched_at` (first-seen, retention clock)
and `content_updated_at` (latest edit) so clients can distinguish new
entries from edited ones.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from feedgate.lifecycle import ErrorCode, FeedStatus


class FeedCreate(BaseModel):
    url: str = Field(..., min_length=1)


class FeedResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    url: str
    effective_url: str
    title: str | None
    status: FeedStatus
    last_successful_fetch_at: datetime | None
    last_attempt_at: datetime | None
    last_error_code: ErrorCode | None
    created_at: datetime


class PaginatedFeeds(BaseModel):
    items: list[FeedResponse]
    next_cursor: str | None = None


class EntryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    guid: str
    feed_id: int
    url: str
    title: str | None
    content: str | None
    author: str | None
    published_at: datetime | None
    fetched_at: datetime
    content_updated_at: datetime


class PaginatedEntries(BaseModel):
    items: list[EntryResponse]
    next_cursor: str | None = None

codex
모델/설정 레이어를 확인했고, 이제 API 라우터와 fetcher 내부(HTTP->파싱->upsert->상태전이)를 읽어서 실제 요청-스케줄러 결합 지점을 파악하겠습니다.
exec
/bin/zsh -lc "sed -n '1,260p' src/feedgate/api/__init__.py" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
"""FastAPI HTTP API.

Router registration and the per-request DB session dependency live
here. The router modules themselves (feeds, entries, health) define
the actual endpoint handlers.

The session factory is read from ``app.state.session_factory``, which
callers (``main.py`` lifespan for production, the test fixtures for
pytest) must set before the app handles any request.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency that yields a request-scoped AsyncSession."""
    session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def require_api_key(request: Request) -> None:
    """FastAPI dependency that enforces X-Api-Key authentication.

    When ``app.state.api_key`` is empty (the default) auth is disabled
    and every request passes through. When set, the ``x-api-key``
    header must match exactly or the request is rejected with 401.
    """
    configured_key: str = getattr(request.app.state, "api_key", "")
    if not configured_key:
        return  # auth disabled
    provided_key = request.headers.get("x-api-key", "")
    if not provided_key or provided_key != configured_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_api_key",
        )


def register_routers(app: FastAPI) -> None:
    """Mount all routers onto ``app``."""
    from feedgate.api import entries, feeds, health

    app.include_router(health.router)
    app.include_router(feeds.router, dependencies=[Depends(require_api_key)])
    app.include_router(entries.router, dependencies=[Depends(require_api_key)])

exec
/bin/zsh -lc "sed -n '1,320p' src/feedgate/api/feeds.py" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
"""Feed CRUD endpoints.

Implements POST/GET/DELETE for ``/v1/feeds`` per ADR 002 and
docs/spec/feed.md. POST is idempotent — reposting an already-registered
URL returns the existing feed with HTTP 200 (409 is intentionally NOT
used; see ADR 002).
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from feedgate.api import get_session
from feedgate.lifecycle import FeedStatus
from feedgate.models import Feed
from feedgate.schemas import FeedCreate, FeedResponse, PaginatedFeeds
from feedgate.ssrf import BlockedURLError, validate_public_url
from feedgate.urlnorm import normalize_url

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/feeds", tags=["feeds"])


def _encode_feed_cursor(feed_id: int) -> str:
    payload = {"i": feed_id}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_feed_cursor(cursor: str) -> int:
    padding = "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(cursor + padding)
        payload = json.loads(raw.decode())
        return int(payload["i"])
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid cursor",
        ) from exc


@router.post(
    "",
    response_model=FeedResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_feed(
    payload: FeedCreate,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Feed:
    url = normalize_url(payload.url)

    # SSRF guard: cheap check (scheme + IP literal). Hostname-resolution
    # check happens at fetch time so a flaky resolver cannot drop a
    # legitimate registration. ``http://10.0.0.1/feed`` is rejected here.
    try:
        await validate_public_url(url, resolve=False)
    except BlockedURLError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"blocked_url: {exc}",
        ) from exc

    stmt = (
        pg_insert(Feed)
        .values(url=url, effective_url=url)
        .on_conflict_do_nothing(index_elements=["url"])
        .returning(Feed.id)
    )
    result = await session.execute(stmt)
    new_id = result.scalar_one_or_none()

    # Load the row either way (newly inserted OR pre-existing).
    feed = (await session.execute(select(Feed).where(Feed.url == url))).scalar_one()
    if new_id is None:
        response.status_code = status.HTTP_200_OK
    return feed


@router.get("", response_model=PaginatedFeeds)
async def list_feeds(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    cursor: Annotated[str | None, Query()] = None,
    limit: int = 50,
    status_filter: Annotated[
        FeedStatus | None,
        Query(
            alias="status",
            description="Filter by lifecycle state (active | broken | dead)",
        ),
    ] = None,
) -> PaginatedFeeds:
    max_limit = request.app.state.api_feeds_max_limit
    limit = max(1, min(limit, max_limit))
    stmt = select(Feed)
    if cursor is not None:
        cur_id = _decode_feed_cursor(cursor)
        stmt = stmt.where(Feed.id > cur_id)
    if status_filter is not None:
        stmt = stmt.where(Feed.status == status_filter)
    stmt = stmt.order_by(Feed.id.asc()).limit(limit + 1)
    result = await session.execute(stmt)
    rows = result.scalars().all()
    has_more = len(rows) > limit
    feeds = list(rows[:limit])
    next_cursor: str | None = None
    if has_more and feeds:
        next_cursor = _encode_feed_cursor(feeds[-1].id)
    return PaginatedFeeds(
        items=[FeedResponse.model_validate(f) for f in feeds],
        next_cursor=next_cursor,
    )


@router.get("/{feed_id}", response_model=FeedResponse)
async def get_feed(
    feed_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Feed:
    feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one_or_none()
    if feed is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="feed not found")
    return feed


@router.delete("/{feed_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_feed(
    feed_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one_or_none()
    if feed is not None:
        await session.delete(feed)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{feed_id}/reactivate", response_model=FeedResponse)
async def reactivate_feed(
    feed_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Feed:
    """Manually flip any feed back to ``active`` (spec/feed.md).

    The primary use case is moving a ``dead`` feed back into the
    fetch rotation after an operator has confirmed the upstream is
    healthy again. Also works on a ``broken`` feed to skip the
    exponential backoff and force an immediate next tick.

    Semantics:
      * ``status`` -> ``'active'``
      * ``consecutive_failures`` -> ``0``
      * ``last_error_code`` -> ``None``
      * ``next_fetch_at`` -> ``now`` (picked up by the very next tick)
      * ``last_successful_fetch_at`` stays unchanged (we have not
        actually succeeded yet — a subsequent successful fetch will
        update it)
    """
    feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one_or_none()
    if feed is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="feed not found")

    if feed.status != FeedStatus.ACTIVE:
        logger.warning(
            "feed_id=%s url=%s state=%s->%s reason=%s",
            feed.id,
            feed.effective_url,
            feed.status,
            FeedStatus.ACTIVE,
            "manual_reactivate",
        )

    feed.status = FeedStatus.ACTIVE
    feed.consecutive_failures = 0
    feed.last_error_code = None
    feed.next_fetch_at = datetime.now(UTC)
    await session.flush()
    await session.refresh(feed)
    return feed

exec
/bin/zsh -lc "sed -n '1,360p' src/feedgate/api/entries.py" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
"""Entry listing endpoint with keyset pagination.

Contract per ADR 002:
  * ``feed_ids`` query parameter is REQUIRED (no global scan).
  * Ordering is ``(published_at DESC, id DESC)``; the compound index
    on ``entries`` was created for exactly this sort.
  * Pagination is keyset-based; the ``cursor`` parameter is an opaque
    string. Clients must not interpret it.

Entries are cached and upserted, so pagination is best-effort under
edits — clients should dedupe by ``guid`` (ADR 002).
"""

from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from feedgate.api import get_session
from feedgate.models import Entry
from feedgate.schemas import EntryResponse, PaginatedEntries

router = APIRouter(prefix="/v1/entries", tags=["entries"])


def _encode_cursor(published_at: datetime | None, entry_id: int) -> str:
    payload = {
        "p": published_at.isoformat() if published_at is not None else None,
        "i": entry_id,
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime | None, int]:
    padding = "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(cursor + padding)
        payload = json.loads(raw.decode())
        p_raw = payload.get("p")
        pub = datetime.fromisoformat(p_raw) if p_raw else None
        return pub, int(payload["i"])
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid cursor",
        ) from exc


@router.get("", response_model=PaginatedEntries)
async def list_entries(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    feed_ids: Annotated[str, Query(..., description="comma-separated feed ids")],
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int | None, Query(ge=1)] = None,
) -> PaginatedEntries:
    max_feed_ids = request.app.state.api_entries_max_feed_ids
    default_limit = request.app.state.api_entries_default_limit
    max_limit = request.app.state.api_entries_max_limit
    limit_value = default_limit if limit is None else limit
    if limit_value > max_limit:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"limit must be less than or equal to {max_limit}",
        )

    try:
        feed_id_list = [int(x) for x in feed_ids.split(",") if x]
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid feed_ids",
        ) from exc

    if not feed_id_list:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="feed_ids is required",
        )
    if len(feed_id_list) > max_feed_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"feed_ids length exceeds {max_feed_ids}",
        )

    stmt = select(Entry).where(Entry.feed_id.in_(feed_id_list))

    if cursor is not None:
        cur_pub, cur_id = _decode_cursor(cursor)
        # Keyset "after": tuples strictly less than (cur_pub, cur_id) in
        # the sort order `(published_at DESC, id DESC)`.
        if cur_pub is None:
            stmt = stmt.where(
                or_(
                    and_(Entry.published_at.is_(None), Entry.id < cur_id),
                    Entry.published_at.is_not(
                        None
                    ),  # DESC NULLS FIRST에서 non-null은 모두 이후 구간
                )
            )
        else:
            stmt = stmt.where(
                or_(
                    Entry.published_at < cur_pub,
                    and_(Entry.published_at == cur_pub, Entry.id < cur_id),
                )
            )

    stmt = stmt.order_by(Entry.published_at.desc(), Entry.id.desc()).limit(limit_value + 1)

    rows = (await session.execute(stmt)).scalars().all()
    has_more = len(rows) > limit_value
    items = list(rows[:limit_value])

    next_cursor: str | None = None
    if has_more and items:
        last = items[-1]
        next_cursor = _encode_cursor(last.published_at, last.id)

    return PaginatedEntries(
        items=[EntryResponse.model_validate(e) for e in items],
        next_cursor=next_cursor,
    )

exec
/bin/zsh -lc "sed -n '1,220p' src/feedgate/api/health.py" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
"""Health check endpoint."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}

exec
/bin/zsh -lc "sed -n '1,320p' src/feedgate/fetcher/http.py" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
"""Fetch-and-upsert pipeline for a single feed.

``fetch_one`` performs one HTTP GET, parses the body, upserts any
entries, and updates the feed's timer fields. Walking skeleton scope:

  * No retry / tenacity — one attempt.
  * No per-host rate limit, no ETag/If-Modified-Since conditional
    requests (left for a later PR).
  * No status-machine transitions — ``feeds.status`` stays
    ``'active'`` regardless of failure; we only record the error code.
  * No response-body size cap enforcement beyond httpx defaults.

On success:
  * ``last_successful_fetch_at`` and ``last_attempt_at`` both set to
    ``now``
  * ``next_fetch_at = now + interval_seconds``
  * ``consecutive_failures = 0``, ``last_error_code = None``
  * ``title`` refreshed from the parsed feed metadata if present

On failure:
  * ``last_attempt_at`` set to ``now``
  * ``next_fetch_at = now + interval_seconds`` (no backoff yet)
  * ``consecutive_failures += 1``
  * ``last_error_code`` set to a short string code
  * Exception is swallowed — the scheduler tick keeps going
"""

from __future__ import annotations

import asyncio
import logging
import random
import socket
import ssl
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

import httpx
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from feedgate.fetcher.parser import parse_feed
from feedgate.fetcher.upsert import upsert_entries
from feedgate.lifecycle import ErrorCode, FeedStatus
from feedgate.models import Entry, Feed
from feedgate.ssrf import BlockedURLError, validate_public_url

logger = logging.getLogger(__name__)


class NotAFeedError(Exception):
    """Raised when a 200 OK response carries a Content-Type that is
    clearly not an RSS/Atom/XML feed (html, json, plain text)."""


class ResponseTooLargeError(Exception):
    """Raised when the streamed response body exceeds the configured
    size cap (``FETCH_MAX_BYTES``). Raised mid-stream so we never load
    the full oversized body into memory."""


def _compute_next_fetch_at(
    feed: Feed,
    *,
    now: datetime,
    base_interval_seconds: int,
    broken_threshold: int,
    broken_max_backoff_seconds: int,
    backoff_jitter_ratio: float,
    server_hint_seconds: int | None = None,
    weekly_entry_count: int = 0,
    entry_frequency_min_interval_seconds: int = 300,
    entry_frequency_max_interval_seconds: int = 86400,
    entry_frequency_factor: int = 1,
) -> datetime:
    """Pick the next fetch instant based on the feed's current status.

    Active and dead feeds use entry_frequency scheduling: feeds that
    post frequently get polled more often, quiet feeds get polled less
    often. When no history is available, falls back to
    ``base_interval_seconds``. Broken feeds use exponential backoff:

        excess_failures = max(0, consecutive_failures - broken_threshold)
        factor = 2 ** excess_failures
        raw = base_interval_seconds * factor
        capped = min(raw, broken_max_backoff_seconds)
        jitter = uniform(-ratio, +ratio) * capped
        next = now + (capped + jitter) seconds

    The jitter prevents thundering-herd recovery when many feeds
    transition to broken together due to a shared upstream outage.
    """
    if feed.status != FeedStatus.BROKEN:
        if weekly_entry_count > 0:
            raw = (7 * 24 * 3600) / (weekly_entry_count * entry_frequency_factor)
            computed = max(
                entry_frequency_min_interval_seconds,
                min(raw, entry_frequency_max_interval_seconds),
            )
        else:
            computed = float(base_interval_seconds)
        effective = max(computed, server_hint_seconds or 0)
        return now + timedelta(seconds=effective)

    excess = max(0, feed.consecutive_failures - broken_threshold)
    factor = 2**excess
    raw_interval = base_interval_seconds * factor
    capped = min(raw_interval, broken_max_backoff_seconds)
    jitter_span = backoff_jitter_ratio * capped
    jitter = random.uniform(-jitter_span, jitter_span)
    return now + timedelta(seconds=capped + jitter)


def _log_transition(feed: Feed, new_status: str, *, reason: str) -> None:
    """Emit a WARNING-level log entry for a feed lifecycle transition.

    INFO is currently swallowed by the default stdlib root logger
    configuration, so lifecycle moves go out at WARNING level to
    ensure operator visibility (see spec/feed.md "관찰 가능성").
    """
    logger.warning(
        "feed_id=%s url=%s state=%s->%s reason=%s",
        feed.id,
        feed.effective_url,
        feed.status,
        new_status,
        reason,
    )


# Content types that unambiguously indicate "this is not a feed".
# We intentionally do NOT maintain an allow-list because many feeds
# serve odd values like ``application/octet-stream`` (Python Insider)
# or omit the header entirely; feedparser handles those just fine.
NOT_A_FEED_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "application/json",
        "application/ld+json",
        "text/plain",
    }
)


def _is_not_a_feed_content_type(ct: str | None) -> bool:
    """Return True if the Content-Type header is clearly not a feed."""
    if not ct:
        return False
    base = ct.split(";", 1)[0].strip().lower()
    return base in NOT_A_FEED_CONTENT_TYPES


def _parse_retry_after(header: str | None, *, now: datetime) -> int | None:
    """Parse ``Retry-After`` per RFC 7231 §7.1.3.

    Accepts either the integer-seconds form (``"120"``) or the
    HTTP-date form (``"Wed, 11 Apr 2026 07:30:00 GMT"``). Cloudflare,
    GitHub, and other large origins emit the date form in production,
    so supporting only seconds would silently drop the signal and
    hammer the upstream at base interval instead of honoring the
    requested delay.

    Returns the delay in seconds relative to ``now``, clamped at zero
    (a past date returns ``0``, not a negative number). Returns
    ``None`` when the header is absent or unparseable in either form.
    """
    if header is None:
        return None
    stripped = header.strip()
    # Integer-seconds form.
    try:
        return max(0, int(stripped))
    except ValueError:
        pass
    # HTTP-date form (RFC 7231 §7.1.1.1). email.utils handles all three
    # legal date formats — IMF-fixdate, RFC 850, asctime.
    try:
        parsed = parsedate_to_datetime(stripped)
    except (TypeError, ValueError):
        return None
    # Dates without an explicit timezone are treated as UTC per RFC 7231.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    delta_seconds = (parsed - now).total_seconds()
    return max(0, int(delta_seconds))


def _parse_cache_hint(headers: httpx.Headers, *, now: datetime) -> int | None:
    """Parse Cache-Control max-age or Expires → seconds from now. Returns None if absent/unparseable."""
    cc = headers.get("cache-control", "")
    for part in cc.split(","):
        stripped = part.strip()
        if stripped.lower().startswith("max-age="):
            try:
                return max(0, int(stripped[8:]))
            except ValueError:
                pass
    expires = headers.get("expires")
    if expires:
        try:
            parsed_dt = parsedate_to_datetime(expires)
            if parsed_dt.tzinfo is None:
                parsed_dt = parsed_dt.replace(tzinfo=UTC)
            return max(0, int((parsed_dt - now).total_seconds()))
        except (TypeError, ValueError):
            pass
    return None


def _classify_error(exc: BaseException) -> ErrorCode:
    """Map a fetch exception to a short error code."""
    if isinstance(exc, BlockedURLError):
        return ErrorCode.BLOCKED
    if isinstance(exc, NotAFeedError):
        return ErrorCode.NOT_A_FEED
    if isinstance(exc, ResponseTooLargeError):
        return ErrorCode.TOO_LARGE
    if isinstance(exc, httpx.TimeoutException | TimeoutError):
        return ErrorCode.TIMEOUT
    if isinstance(exc, httpx.ConnectError):
        return _classify_connect_cause(exc)
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 410:
            return ErrorCode.HTTP_410
        if 400 <= status < 500:
            return ErrorCode.HTTP_4XX
        return ErrorCode.HTTP_5XX
    if isinstance(exc, httpx.TooManyRedirects):
        return ErrorCode.REDIRECT_LOOP
    if isinstance(exc, httpx.HTTPError):
        return ErrorCode.HTTP_ERROR
    return ErrorCode.OTHER


def _classify_connect_cause(exc: httpx.ConnectError) -> ErrorCode:
    """Classify connect failures by traversing cause/context chain."""
    current: BaseException | None = exc
    seen: set[int] = set()

    for _ in range(8):
        if current is None:
            break
        if isinstance(current, ssl.SSLError):
            return ErrorCode.TLS_ERROR
        if isinstance(current, socket.gaierror):
            return ErrorCode.DNS
        if isinstance(current, ConnectionRefusedError):
            return ErrorCode.TCP_REFUSED

        current_id = id(current)
        if current_id in seen:
            break
        seen.add(current_id)

        next_exc = current.__cause__ or current.__context__
        current = next_exc if isinstance(next_exc, BaseException) else None

    return ErrorCode.CONNECTION


async def fetch_one(
    session: AsyncSession,
    http_client: httpx.AsyncClient,
    feed: Feed,
    *,
    now: datetime,
    interval_seconds: int,
    user_agent: str,
    max_bytes: int,
    max_entries_initial: int,
    total_budget_seconds: float,
    broken_threshold: int,
    dead_duration_days: int,
    broken_max_backoff_seconds: int,
    backoff_jitter_ratio: float,
    entry_frequency_min_interval_seconds: int,
    entry_frequency_max_interval_seconds: int,
    entry_frequency_factor: int,
) -> None:
    feed.last_attempt_at = now
    _server_hint: int | None = None

    cutoff = now - timedelta(days=7)
    weekly_entry_count_result = await session.execute(
        select(func.count()).where(
            Entry.feed_id == feed.id,
            Entry.fetched_at >= cutoff,
        )
    )
    weekly_entry_count: int = weekly_entry_count_result.scalar_one()

    try:
        # Pre-flight SSRF check on the feed's effective URL. This catches
        # late-binding DNS rebinding (a hostname that was public when
        # registered but now resolves to ``10.x``) before any socket is
        # opened. The HTTP transport runs the same check on every
        # redirect hop, so a 302 → private IP is also blocked.
        await validate_public_url(feed.effective_url)

        conditional_headers: dict[str, str] = {}
        if feed.etag:
            conditional_headers["If-None-Match"] = feed.etag
        elif feed.last_modified:
            conditional_headers["If-Modified-Since"] = feed.last_modified

        # Hard total wall-clock budget for the entire fetch — guards
        # against slow-loris streams that drip bytes just under the
        # per-chunk read timeout. ``asyncio.timeout`` raises
        # ``TimeoutError`` which ``_classify_error`` maps to
        # ``ErrorCode.TIMEOUT``.
        async with (
            asyncio.timeout(total_budget_seconds),
            http_client.stream(
                "GET",
                feed.effective_url,
                headers={"User-Agent": user_agent, **conditional_headers},
                follow_redirects=True,
            ) as response,

exec
/bin/zsh -lc "sed -n '320,680p' src/feedgate/fetcher/http.py" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
            ) as response,
        ):
            # 304 Not Modified — feed unchanged. Schedule the next fetch
            # and return early without touching consecutive_failures or status.
            if response.status_code == 304:
                if feed.status == FeedStatus.BROKEN:
                    _log_transition(feed, FeedStatus.ACTIVE, reason="http_304_recovery")
                    feed.status = FeedStatus.ACTIVE
                    feed.consecutive_failures = 0
                    feed.last_error_code = None
                    feed.last_successful_fetch_at = now
                _server_hint = _parse_cache_hint(response.headers, now=now)
                feed.next_fetch_at = _compute_next_fetch_at(
                    feed,
                    now=now,
                    base_interval_seconds=interval_seconds,
                    broken_threshold=broken_threshold,
                    broken_max_backoff_seconds=broken_max_backoff_seconds,
                    backoff_jitter_ratio=backoff_jitter_ratio,
                    server_hint_seconds=_server_hint,
                    weekly_entry_count=weekly_entry_count,
                    entry_frequency_min_interval_seconds=entry_frequency_min_interval_seconds,
                    entry_frequency_max_interval_seconds=entry_frequency_max_interval_seconds,
                    entry_frequency_factor=entry_frequency_factor,
                )
                return

            # 429 Rate Limited is NOT a circuit-breaker failure
            # (spec/feed.md). Honor Retry-After, record the code,
            # leave consecutive_failures and status alone, and bail
            # out of fetch_one early. We do NOT update
            # last_successful_fetch_at because we didn't succeed.
            if response.status_code == 429:
                retry_after = _parse_retry_after(response.headers.get("retry-after"), now=now)
                wait_seconds = retry_after if retry_after is not None else 0
                # Floor at base interval — don't hammer the upstream
                # faster than our normal poll rate even if it says "10s".
                wait_seconds = max(wait_seconds, interval_seconds)
                feed.last_error_code = ErrorCode.RATE_LIMITED
                feed.next_fetch_at = now + timedelta(seconds=wait_seconds)
                return

            response.raise_for_status()
            permanent_moved = any(h.status_code == 301 for h in response.history)
            if permanent_moved:
                final_url = str(response.url)
                if final_url != feed.effective_url:
                    feed.effective_url = final_url

            ct = response.headers.get("content-type")
            if _is_not_a_feed_content_type(ct):
                raise NotAFeedError(f"unexpected content-type: {ct}")

            body_parts: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                body_parts.append(chunk)
                size += len(chunk)
                if size > max_bytes:
                    raise ResponseTooLargeError(f"body exceeded {max_bytes} bytes")
            body = b"".join(body_parts)

        parsed = await parse_feed(body)
        entries_to_upsert = parsed.entries
        if entries_to_upsert:
            has_existing_entries = (
                await session.execute(select(exists().where(Entry.feed_id == feed.id)))
            ).scalar_one()
            # Initial-fetch cap: a brand-new feed that advertises hundreds
            # of entries (OpenAI emits ~909, Hugging Face ~762) gets
            # truncated to the top N most-recent, matching what Feedly
            # does in production. Subsequent fetches ignore the cap —
            # the delta is almost always small and ON CONFLICT dedups.
            if not has_existing_entries and len(entries_to_upsert) > max_entries_initial:
                entries_to_upsert = entries_to_upsert[:max_entries_initial]
            await upsert_entries(session, feed.id, entries_to_upsert, now=now)

        if parsed.title:
            feed.title = parsed.title
        feed.last_successful_fetch_at = now
        feed.last_error_code = None
        feed.consecutive_failures = 0
        if feed.status != FeedStatus.ACTIVE:
            _log_transition(feed, FeedStatus.ACTIVE, reason="fetch_succeeded")
            feed.status = FeedStatus.ACTIVE

        new_etag = response.headers.get("etag")
        new_last_modified = response.headers.get("last-modified")
        if new_etag is not None:
            feed.etag = new_etag
        if new_last_modified is not None:
            feed.last_modified = new_last_modified
        _server_hint = _parse_cache_hint(response.headers, now=now)
        if parsed.ttl_seconds is not None and parsed.ttl_seconds > 0:
            _server_hint = max(_server_hint or 0, parsed.ttl_seconds) or None
    except Exception as exc:
        code = _classify_error(exc)
        feed.last_error_code = code
        feed.consecutive_failures = feed.consecutive_failures + 1
        logger.warning(
            "fetch_one failed feed_id=%s url=%s code=%s err=%r",
            feed.id,
            feed.effective_url,
            code,
            exc,
        )

        # Lifecycle transitions (spec/feed.md — circuit breaker + 410)
        if code == ErrorCode.HTTP_410:
            if feed.status != FeedStatus.DEAD:
                _log_transition(feed, FeedStatus.DEAD, reason="http_410")
                feed.status = FeedStatus.DEAD
        else:
            # active -> broken on threshold
            if feed.status == FeedStatus.ACTIVE and feed.consecutive_failures >= broken_threshold:
                _log_transition(
                    feed,
                    FeedStatus.BROKEN,
                    reason=f"consecutive_failures>={broken_threshold}",
                )
                feed.status = FeedStatus.BROKEN
            # broken -> dead on time since last success (fall-through
            # allowed: if active just flipped to broken above AND the
            # feed already has no success for dead_duration_days, we
            # transition straight through to dead in the same call)
            if feed.status == FeedStatus.BROKEN:
                reference = feed.last_successful_fetch_at or feed.created_at
                if now - reference >= timedelta(days=dead_duration_days):
                    _log_transition(
                        feed,
                        FeedStatus.DEAD,
                        reason=f"no_success_for_>={dead_duration_days}d",
                    )
                    feed.status = FeedStatus.DEAD

    # Schedule the next fetch based on the final status. Active feeds
    # use base_interval_seconds; broken feeds use exponential backoff
    # with ±jitter; dead feeds are filtered out by the scheduler so
    # their next_fetch_at is effectively unused but still set for
    # consistency.
    feed.next_fetch_at = _compute_next_fetch_at(
        feed,
        now=now,
        base_interval_seconds=interval_seconds,
        broken_threshold=broken_threshold,
        broken_max_backoff_seconds=broken_max_backoff_seconds,
        backoff_jitter_ratio=backoff_jitter_ratio,
        server_hint_seconds=_server_hint,
        weekly_entry_count=weekly_entry_count,
        entry_frequency_min_interval_seconds=entry_frequency_min_interval_seconds,
        entry_frequency_max_interval_seconds=entry_frequency_max_interval_seconds,
        entry_frequency_factor=entry_frequency_factor,
    )

exec
/bin/zsh -lc "sed -n '1,320p' src/feedgate/fetcher/parser.py" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
"""Feed parser wrapper around feedparser.

feedparser is a synchronous, battle-tested RSS/Atom parser. We dispatch
it to a worker thread via ``anyio.to_thread.run_sync`` so the asyncio
event loop isn't blocked on large feeds or CPU-heavy parsing work.

Output shape is the lightweight ``ParsedFeed`` + ``ParsedEntry``
dataclasses the rest of the pipeline consumes (fetcher.upsert takes
``list[ParsedEntry]`` directly).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import anyio
import feedparser

from feedgate.fetcher.upsert import ParsedEntry


@dataclass(frozen=True)
class ParsedFeed:
    title: str | None
    ttl_seconds: int | None = None
    entries: list[ParsedEntry] = field(default_factory=list)


def _struct_time_to_datetime(st: Any) -> datetime | None:
    """Convert feedparser's time.struct_time to an aware datetime (UTC)."""
    if st is None:
        return None
    try:
        return datetime(st[0], st[1], st[2], st[3], st[4], st[5], tzinfo=UTC)
    except (TypeError, ValueError, IndexError):
        return None


def _extract_content(fp_entry: Any) -> str | None:
    """Pick the 'content' string from a feedparser entry, or None."""
    content_list = fp_entry.get("content")
    if content_list:
        first = content_list[0]
        value = first.get("value") if isinstance(first, dict) else None
        if value:
            return str(value)
    summary = fp_entry.get("summary")
    return str(summary) if summary else None


def _extract_entry(fp_entry: Any) -> ParsedEntry:
    # feedparser uses .id for Atom <id> / RSS <guid>. When a feed omits
    # guid/id, feedparser copies <link> into .id so the field is still
    # populated — that is our de-facto fallback for "guid missing".
    guid = fp_entry.get("id") or fp_entry.get("link") or ""
    url = fp_entry.get("link") or guid
    title = fp_entry.get("title")
    author = fp_entry.get("author")
    content = _extract_content(fp_entry)
    published_at = _struct_time_to_datetime(
        fp_entry.get("published_parsed") or fp_entry.get("updated_parsed")
    )

    return ParsedEntry(
        guid=guid,
        url=url,
        title=title,
        content=content,
        author=author,
        published_at=published_at,
    )


def _parse_sync(body: bytes) -> ParsedFeed:
    parsed = feedparser.parse(body)
    feed_meta = getattr(parsed, "feed", None)
    feed_title = feed_meta.get("title") if feed_meta else None
    entries = [_extract_entry(e) for e in parsed.entries]
    ttl_seconds: int | None = None
    if feed_meta:
        ttl_raw = feed_meta.get("ttl")
        if ttl_raw is not None:
            try:
                ttl_seconds = max(0, int(ttl_raw)) * 60
            except (TypeError, ValueError):
                pass
    return ParsedFeed(title=feed_title, ttl_seconds=ttl_seconds, entries=entries)


async def parse_feed(body: bytes) -> ParsedFeed:
    """Parse a raw feed body (bytes) in a worker thread."""
    return await anyio.to_thread.run_sync(_parse_sync, body)

exec
/bin/zsh -lc "sed -n '1,340p' src/feedgate/fetcher/upsert.py" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
"""Entry upsert logic.

Implements the mutation policy from docs/spec/entry.md:

* New entry (no existing row for (feed_id, guid)):
    INSERT with `fetched_at = content_updated_at = now()`.
* Existing entry, one of {url, title, content, author, published_at}
  changed:
    UPDATE the changed content fields and set `content_updated_at = now()`.
    `fetched_at` is NEVER touched (ADR 001 invariant #4, ADR 004).
* Existing entry, no change:
    no-op (not even `content_updated_at` or `fetched_at` move).

The Postgres-native `ON CONFLICT ... DO UPDATE ... WHERE` form expresses
all three cases in a single statement: the `WHERE` on the conflict
branch guards the UPDATE so identical payloads become no-ops.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from feedgate.models import Entry


@dataclass(frozen=True)
class ParsedEntry:
    """Minimal content-bearing fields a parser emits for one entry.

    This is the upsert input shape. Additional normalization (e.g. URL
    absolutization) happens before this dataclass is constructed.
    """

    guid: str
    url: str
    title: str | None = None
    content: str | None = None
    author: str | None = None
    published_at: datetime | None = None


async def upsert_entries(
    session: AsyncSession,
    feed_id: int,
    entries: list[ParsedEntry],
    *,
    now: datetime,
) -> None:
    """Upsert a batch of parsed entries for a single feed.

    `now` is passed in explicitly so tests and callers can control the
    clock. Both `fetched_at` (new inserts only) and `content_updated_at`
    (new inserts + actual updates) use the same value within one call.
    """
    if not entries:
        return

    for entry in entries:
        stmt = insert(Entry).values(
            feed_id=feed_id,
            guid=entry.guid,
            url=entry.url,
            title=entry.title,
            content=entry.content,
            author=entry.author,
            published_at=entry.published_at,
            fetched_at=now,
            content_updated_at=now,
        )
        excluded = stmt.excluded
        stmt = stmt.on_conflict_do_update(
            index_elements=["feed_id", "guid"],
            set_={
                "url": excluded.url,
                "title": excluded.title,
                "content": excluded.content,
                "author": excluded.author,
                "published_at": excluded.published_at,
                "content_updated_at": func.now(),
            },
            where=(
                Entry.url.is_distinct_from(excluded.url)
                | Entry.title.is_distinct_from(excluded.title)
                | Entry.content.is_distinct_from(excluded.content)
                | Entry.author.is_distinct_from(excluded.author)
                | Entry.published_at.is_distinct_from(excluded.published_at)
            ),
        )
        await session.execute(stmt)

exec
/bin/zsh -lc "sed -n '1,360p' src/feedgate/fetcher/scheduler.py" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
"""In-process asyncio scheduler for feedgate-fetcher.

  * ``tick_once(app)`` — the unit of work. Atomically reserves a
    batch of due feeds via ``SELECT ... FOR UPDATE SKIP LOCKED``,
    advances their ``next_fetch_at`` to ``now + claim_ttl_seconds``
    (and ``last_attempt_at = now`` to cover the dead-probe path),
    commits to release the row locks, and then calls ``fetch_one``
    on each in a fresh session. The lease mechanic is what lets N
    worker replicas run in parallel against a single Postgres
    without claiming the same feed twice — the SKIP LOCKED avoids
    the in-flight claim, the timestamp bump prevents re-claim after
    commit, and a crashed worker's rows become claimable again once
    the TTL elapses. See ``docs/spec/feed.md`` for the queue
    semantics.

  * ``run(app, stop_event)`` — the background loop that drives
    ``tick_once`` on a timer. Intentionally thin and TDD-exempt per
    the plan; its correctness is covered by tick_once integration
    tests and F2 (walking-skeleton E2E).

The app reads its state from ``app.state``:
  * ``session_factory`` (sqlalchemy async_sessionmaker)
  * ``http_client`` (httpx.AsyncClient)
  * ``fetch_interval_seconds``, ``fetch_user_agent``, ``fetch_concurrency``
  * ``fetch_claim_batch_size``, ``fetch_claim_ttl_seconds``
  * full fetcher tunables (broken_threshold, dead_duration_days, ...)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from fastapi import FastAPI
from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from feedgate.fetcher.http import fetch_one
from feedgate.lifecycle import FeedStatus
from feedgate.models import Feed

logger = logging.getLogger(__name__)


def _host_key(url: str) -> str:
    """Extract a per-host throttle key from a feed URL.

    Returns the lowercased hostname so ``Example.com`` and
    ``example.com`` share a semaphore. Falls back to the empty string
    when the URL has no hostname (literal IP feeds keep their address
    as the key, which is the desired behavior — they still throttle
    against themselves)."""
    return (urlsplit(url).hostname or "").lower()


async def _process_feed(
    feed_id: int,
    app: FastAPI,
    sem: asyncio.Semaphore,
    host_sems: dict[str, asyncio.Semaphore],
    per_host_concurrency: int,
    now: datetime,
) -> None:
    """Open a fresh session, load the feed, run fetch_one, commit."""
    sf = app.state.session_factory
    http_client = app.state.http_client
    interval = app.state.fetch_interval_seconds
    ua = app.state.fetch_user_agent
    max_bytes = app.state.fetch_max_bytes
    max_entries_initial = app.state.fetch_max_entries_initial
    total_budget = app.state.fetch_total_budget_seconds
    broken_threshold = app.state.broken_threshold
    dead_duration_days = app.state.dead_duration_days
    broken_max_backoff_seconds = app.state.broken_max_backoff_seconds
    backoff_jitter_ratio = app.state.backoff_jitter_ratio
    entry_frequency_min_interval_seconds = app.state.entry_frequency_min_interval_seconds
    entry_frequency_max_interval_seconds = app.state.entry_frequency_max_interval_seconds
    entry_frequency_factor = app.state.entry_frequency_factor

    async with sem, sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one_or_none()
        if feed is None:
            return
        # Per-host throttle: setdefault is atomic under cooperative
        # asyncio scheduling, so concurrent _process_feed coroutines
        # for the same host see the same semaphore. ``async with`` it
        # *inside* the global semaphore so we hold a global slot while
        # waiting on the host slot — this keeps tick semantics simple
        # (one acquire order, no deadlock potential).
        host_sem = host_sems.setdefault(
            _host_key(feed.effective_url),
            asyncio.Semaphore(per_host_concurrency),
        )
        try:
            async with host_sem:
                await fetch_one(
                    session,
                    http_client,
                    feed,
                    now=now,
                    interval_seconds=interval,
                    user_agent=ua,
                    max_bytes=max_bytes,
                    max_entries_initial=max_entries_initial,
                    total_budget_seconds=total_budget,
                    broken_threshold=broken_threshold,
                    dead_duration_days=dead_duration_days,
                    broken_max_backoff_seconds=broken_max_backoff_seconds,
                    backoff_jitter_ratio=backoff_jitter_ratio,
                    entry_frequency_min_interval_seconds=entry_frequency_min_interval_seconds,
                    entry_frequency_max_interval_seconds=entry_frequency_max_interval_seconds,
                    entry_frequency_factor=entry_frequency_factor,
                )
            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception("fatal error in _process_feed feed_id=%s", feed_id)


async def _claim_due_feeds(
    session: AsyncSession,
    *,
    now: datetime,
    claim_batch_size: int,
    claim_ttl_seconds: int,
    dead_probe_interval_days: int,
) -> list[int]:
    """Reserve up to ``claim_batch_size`` feeds via SKIP LOCKED.

    Two classes of feeds are eligible:

      1. Non-dead feeds (active + broken) whose ``next_fetch_at`` is
         due. This respects the per-feed exponential backoff for
         broken feeds — they are only polled when their own schedule
         says so.
      2. Dead feeds whose ``last_attempt_at`` is older than
         ``dead_probe_interval_days`` (weekly probe, spec/feed.md).
         Dead feeds never had their backoff respected anyway (once
         dead you never come back via the normal path) — the probe
         is the only way they can get re-fetched.

    Every claimed row has both ``next_fetch_at`` bumped to
    ``now + claim_ttl_seconds`` AND ``last_attempt_at`` bumped to
    ``now``. The ``next_fetch_at`` bump covers the non-dead gate;
    the ``last_attempt_at`` bump covers the dead-probe gate. The
    caller MUST commit the session after this returns for the lease
    to become visible to other workers.
    """
    probe_cutoff = now - timedelta(days=dead_probe_interval_days)
    claim_predicate = or_(
        and_(
            Feed.status != FeedStatus.DEAD,
            Feed.next_fetch_at <= now,
        ),
        and_(
            Feed.status == FeedStatus.DEAD,
            or_(
                Feed.last_attempt_at.is_(None),
                Feed.last_attempt_at < probe_cutoff,
            ),
        ),
    )
    claim_ids_stmt = (
        select(Feed.id)
        .where(claim_predicate)
        .order_by(Feed.next_fetch_at)
        .limit(claim_batch_size)
        .with_for_update(skip_locked=True)
    )
    lease_until = now + timedelta(seconds=claim_ttl_seconds)
    update_stmt = (
        update(Feed)
        .where(Feed.id.in_(claim_ids_stmt))
        .values(next_fetch_at=lease_until, last_attempt_at=now)
        .returning(Feed.id)
    )
    claimed_ids = list((await session.execute(update_stmt)).scalars().all())
    return claimed_ids


async def tick_once(app: FastAPI, *, now: datetime | None = None) -> None:
    """Run one scheduler iteration against a Postgres-as-queue.

    Step 1 — claim: open a session, ``SELECT ... FOR UPDATE SKIP
    LOCKED`` a batch of due feeds, bump each row's ``next_fetch_at``
    and ``last_attempt_at`` as a crash-safe lease, commit. Other
    workers running in parallel will not see the claimed rows until
    the lease expires or ``fetch_one`` rewrites the timestamps.

    Step 2 — fetch: for each claimed id, open a fresh session and
    call ``fetch_one`` under a bounded semaphore. Each fetch is
    isolated, so one failing feed cannot poison the others.
    """
    now = now or datetime.now(UTC)
    sf = app.state.session_factory
    async with sf() as session:
        feed_ids = await _claim_due_feeds(
            session,
            now=now,
            claim_batch_size=app.state.fetch_claim_batch_size,
            claim_ttl_seconds=app.state.fetch_claim_ttl_seconds,
            dead_probe_interval_days=app.state.dead_probe_interval_days,
        )
        await session.commit()

    if not feed_ids:
        return

    sem = asyncio.Semaphore(app.state.fetch_concurrency)
    host_sems: dict[str, asyncio.Semaphore] = {}
    per_host_concurrency = app.state.fetch_per_host_concurrency
    await asyncio.gather(
        *(_process_feed(fid, app, sem, host_sems, per_host_concurrency, now) for fid in feed_ids)
    )


async def run(
    app: FastAPI,
    *,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Background loop that calls ``tick_once`` every interval.

    TDD-exempt per WP 4.3 — the loop body is intentionally ~10 LOC and
    its correctness is covered by the integration tests of ``tick_once``
    and by F2 (walking-skeleton E2E). The stop_event parameter is a
    plain ``asyncio.Event``; ``None`` means "run until cancelled".
    """
    interval = app.state.fetch_interval_seconds
    stop = stop_event or asyncio.Event()
    while not stop.is_set():
        try:
            await tick_once(app)
        except Exception:
            logger.exception("scheduler tick raised; continuing")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            continue

exec
/bin/zsh -lc "sed -n '1,360p' src/feedgate/lifecycle.py" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
"""Feed lifecycle constants.

``FeedStatus`` and ``ErrorCode`` are the canonical string values used
across the ORM, API schemas, and fetcher. They are ``StrEnum`` so that
comparisons against plain strings still work (``FeedStatus.ACTIVE ==
"active"`` is ``True``) and JSON serialization is the same as before.

The spec of record is ``docs/spec/feed.md`` — adding a new value here
without updating that document will drift. ``last_error_code`` is also
user-visible, so adding/removing an ``ErrorCode`` member is an API
contract change.
"""

from __future__ import annotations

from enum import StrEnum


class FeedStatus(StrEnum):
    ACTIVE = "active"
    BROKEN = "broken"
    DEAD = "dead"


class ErrorCode(StrEnum):
    # Network / transport
    DNS = "dns"
    TCP_REFUSED = "tcp_refused"
    TLS_ERROR = "tls_error"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    # HTTP status classes
    HTTP_4XX = "http_4xx"
    HTTP_410 = "http_410"
    HTTP_5XX = "http_5xx"
    HTTP_ERROR = "http_error"
    RATE_LIMITED = "rate_limited"
    # Content / parsing
    NOT_A_FEED = "not_a_feed"
    PARSE_ERROR = "parse_error"
    REDIRECT_LOOP = "redirect_loop"
    TOO_LARGE = "too_large"
    # SSRF guard rejected the URL (private IP, bad scheme, etc.)
    BLOCKED = "blocked"
    # Fallback
    OTHER = "other"

codex
이제 보안/정규화/에러/보존 스위퍼와 ADR·스펙 문서를 함께 확인해서 코드와 설계 의도 차이를 같이 짚겠습니다.
exec
/bin/zsh -lc "sed -n '1,320p' src/feedgate/ssrf.py" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
"""SSRF protection — block URLs that target non-public addresses.

Three layers, all funneled through ``validate_public_url``:

  1. **Scheme** must be ``http`` or ``https``. ``file://``, ``gopher://``,
     ``ftp://`` are rejected outright.
  2. **IP literal** in the URL host (``http://10.0.0.1/``,
     ``http://[::1]/``) is checked against the blocked set without DNS.
  3. **Hostname** is resolved via ``loop.getaddrinfo`` and **every**
     returned address is checked. If any one resolves to a private,
     loopback, link-local, reserved, multicast, or unspecified address
     the URL is rejected.

The hostname resolution check is the load-bearing one: it catches
DNS rebinding (a public hostname whose A record points to ``10.x``)
and is re-run by the HTTP transport guard on every redirect hop, so a
``302 → http://169.254.169.254/`` cannot escape it either.

Resolution failures (NXDOMAIN, no A record) are deliberately *allowed*
through. Treating a flaky resolver as "blocked" would silently drop
legitimate registrations, and an unresolvable host fails at fetch time
anyway — it cannot reach an internal target.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

import httpx

ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})


class BlockedURLError(ValueError):
    """Raised when a URL targets a non-public address or unsupported scheme."""


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


async def _resolve(host: str) -> list[str]:
    """Async DNS resolution. Returns the list of address strings, or
    an empty list on resolver failure (NXDOMAIN, no A record, etc.).

    Indirected through this helper so tests can monkeypatch a single
    function instead of stubbing ``socket.getaddrinfo`` globally.
    """
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return []
    return [info[4][0] for info in infos]


async def validate_public_url(url: str, *, resolve: bool = True) -> None:
    """Reject URLs that target non-public addresses.

    ``resolve=False`` skips the DNS step and only checks scheme + IP
    literals. Use it on hot paths where DNS latency or test-environment
    DNS dependencies are a concern (e.g. the ``POST /v1/feeds``
    endpoint), and rely on the fetcher's own pre-flight call (which
    runs with ``resolve=True``) to catch hostnames that resolve to a
    blocked address.

    Raises :class:`BlockedURLError` on violation.
    """
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise BlockedURLError(f"unsupported scheme: {scheme!r}")

    host = parts.hostname
    if not host:
        raise BlockedURLError("missing host")

    # Literal IP — check directly without DNS.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _is_blocked_ip(literal):
            raise BlockedURLError(f"blocked address: {host}")
        return

    if not resolve:
        return

    addrs = await _resolve(host)
    for addr in addrs:
        # getaddrinfo can hand back scoped v6 like "fe80::1%lo0".
        try:
            resolved = ipaddress.ip_address(addr.split("%", 1)[0])
        except ValueError:
            continue
        if _is_blocked_ip(resolved):
            raise BlockedURLError(f"{host} resolves to blocked address {addr}")


class SSRFGuardTransport(httpx.AsyncBaseTransport):
    """``httpx.AsyncBaseTransport`` wrapper that re-validates every
    request URL — including redirect follow-ups — through
    :func:`validate_public_url`.

    The point of guarding at the transport layer (rather than only
    pre-validating in ``fetch_one``) is that ``httpx.AsyncClient`` calls
    ``handle_async_request`` once per HTTP exchange, so a ``302`` to
    ``http://10.0.0.1/`` re-enters this method with the new URL and
    gets blocked before the socket is opened.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await validate_public_url(str(request.url))
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()

exec
/bin/zsh -lc "sed -n '1,280p' src/feedgate/urlnorm.py" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
"""URL normalization per docs/spec/feed.md.

Minimum rules applied:
  * scheme lowercase
  * host lowercase (and IDN → punycode)
  * default ports (:80, :443) stripped
  * trailing slash on non-root paths removed
  * root "/" collapsed to empty (so "http://x.com/" == "http://x.com")
  * fragment (#...) removed
  * query preserved as-is (some feeds use query params to filter)

This is intentionally a small hand-rolled function — rfc3986 does not
strip default ports or remove fragments, and a dozen lines of
``urllib.parse`` covers our needs exactly.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}


def _to_idna(host: str) -> str:
    """Convert an IDN host to its punycode form, lowercased on failure."""
    if not host:
        return ""
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host.lower()


def normalize_url(raw: str) -> str:
    parts = urlsplit(raw.strip())

    scheme = parts.scheme.lower()
    host = _to_idna(parts.hostname or "")

    port = parts.port
    if port is not None and DEFAULT_PORTS.get(scheme) == port:
        port = None

    netloc = host
    if parts.username:
        cred = parts.username
        if parts.password:
            cred = f"{cred}:{parts.password}"
        netloc = f"{cred}@{netloc}"
    if port is not None:
        netloc = f"{netloc}:{port}"

    path = parts.path
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    if path == "/":
        path = ""

    return urlunsplit((scheme, netloc, path, parts.query, ""))

exec
/bin/zsh -lc "sed -n '1,280p' src/feedgate/errors.py" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
from __future__ import annotations

from http import HTTPStatus
from typing import cast

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.types import ExceptionHandler

PROBLEM_JSON_MEDIA_TYPE = "application/problem+json"


def _status_title(status_code: int) -> str:
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return "Error"


def _problem(
    *,
    request: Request,
    status_code: int,
    title: str,
    detail: str,
) -> dict[str, str | int]:
    return {
        "type": "about:blank",
        "title": title,
        "status": status_code,
        "detail": detail,
        "instance": request.url.path,
    }


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    title = _status_title(exc.status_code)
    detail = title if exc.detail is None else str(exc.detail)
    problem = _problem(
        request=request,
        status_code=exc.status_code,
        title=title,
        detail=detail,
    )
    return JSONResponse(
        problem,
        status_code=exc.status_code,
        media_type=PROBLEM_JSON_MEDIA_TYPE,
        headers=exc.headers,
    )


async def validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    errors = exc.errors()
    if errors:
        first = errors[0]
        loc = first.get("loc", ())
        msg = first.get("msg", "validation error")
        detail = f"{loc}: {msg}"
    else:
        detail = "validation error"

    status_code = 422
    title = "Unprocessable Entity"
    problem = _problem(
        request=request,
        status_code=status_code,
        title=title,
        detail=detail,
    )
    return JSONResponse(
        problem,
        status_code=status_code,
        media_type=PROBLEM_JSON_MEDIA_TYPE,
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(
        HTTPException,
        cast(ExceptionHandler, http_exception_handler),
    )
    app.add_exception_handler(
        RequestValidationError,
        cast(ExceptionHandler, validation_exception_handler),
    )

exec
/bin/zsh -lc "sed -n '1,360p' src/feedgate/retention.py" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
"""Entry retention sweep.

Implements the deletion rule from ADR 004 / docs/spec/entry.md:

    For each feed, retain the UNION of
      * entries within the time window (``fetched_at >= cutoff``)
      * the ``min_per_feed`` most-recent entries, regardless of age
    Delete the rest.

The retain rule is expressed as a single Postgres statement using a
window-function CTE for the per-feed top-N and a UNION for the time
window. The ``fetched_at`` clock — not ``published_at`` — is the
retention anchor (ADR 004 decision #1) because we control it.

The ``cutoff`` is passed in explicitly so callers (and tests) control
the clock. Production code calls with ``datetime.now(UTC) -
timedelta(days=settings.retention_days)``.

The function does NOT commit — the caller owns the transaction.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from sqlalchemy import delete, func, select, union
from sqlalchemy.ext.asyncio import AsyncSession

from feedgate.models import Entry

logger = logging.getLogger(__name__)


async def sweep(
    session: AsyncSession,
    *,
    cutoff: datetime,
    min_per_feed: int,
) -> int:
    """Delete aged-out entries that fall outside both retain windows.

    Uses SQLAlchemy 2.0 Core expressions end-to-end — a window-
    function CTE computes per-feed row numbers, the keep set is a
    UNION of the time window and the per-feed top-N, and the final
    DELETE ... RETURNING is an ORM-level bulk delete. Returns the
    number of rows deleted. Does not commit — the caller is
    responsible for the transaction.
    """
    # Per-feed ranking by fetched_at DESC, id DESC (matches the
    # compound keyset index on entries).
    ranked = select(
        Entry.id.label("id"),
        func.row_number()
        .over(
            partition_by=Entry.feed_id,
            order_by=(Entry.fetched_at.desc(), Entry.id.desc()),
        )
        .label("rn"),
    ).cte("ranked")

    # Keep set: time window UNION per-feed top-N.
    time_window = select(Entry.id).where(Entry.fetched_at >= cutoff)
    top_n = select(ranked.c.id).where(ranked.c.rn <= min_per_feed)
    keep = union(time_window, top_n).subquery("keep")

    stmt = (
        delete(Entry)
        .where(Entry.id.not_in(select(keep.c.id)))
        .returning(Entry.id)
        .execution_options(synchronize_session=False)
    )
    result = await session.execute(stmt)
    return len(result.fetchall())


# ---- App-level wrappers ----------------------------------------------------


async def tick_once(
    app: FastAPI,
    *,
    now: datetime | None = None,
) -> int:
    """Run a single retention sweep using settings from ``app.state``.

    Opens a dedicated session, computes the cutoff as
    ``now - retention_days``, executes the sweep, commits on success,
    rolls back on error. Returns the number of rows deleted.
    """
    now = now or datetime.now(UTC)
    days: int = app.state.retention_days
    min_per_feed: int = app.state.retention_min_per_feed
    cutoff = now - timedelta(days=days)

    sf = app.state.session_factory
    async with sf() as session:
        try:
            n = await sweep(
                session,
                cutoff=cutoff,
                min_per_feed=min_per_feed,
            )
            await session.commit()
            return n
        except Exception:
            await session.rollback()
            raise


async def run(
    app: FastAPI,
    *,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Background loop that calls ``tick_once`` every interval.

    TDD-exempt — thin wrapper around ``tick_once``, correctness is
    covered by the sweep tests and the tick_once integration test.
    Never lets the loop die: any exception is logged and swallowed.
    """
    interval = app.state.retention_sweep_interval_seconds
    stop = stop_event or asyncio.Event()
    while not stop.is_set():
        try:
            n = await tick_once(app)
            logger.info("retention sweep deleted %d entries", n)
        except Exception:
            logger.exception("retention sweep raised; continuing")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            continue

exec
/bin/zsh -lc "sed -n '1,260p' docs/adr/000-service-purpose-and-scope.md" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
# ADR 000 — 서비스 목적과 범위

- 상태: Accepted
- 작성일: 2026-04-10

## 컨텍스트

이 리포에서 만들 서비스의 존재 이유와 경계를 처음으로 박아둔다. 이후
모든 결정은 이 문서가 정한 범위 안에서 이루어진다.

구체적인 기술 선택, 스키마, API 형상, 보존 기간 수치, 인증 방식 등
**디테일은 이 ADR에 담지 않는다**. 그런 항목은 후속 ADR과 `docs/spec/`에서
다룬다. 이 문서는 "이 서비스가 무엇인가"와 "무엇이 아닌가"만 확정한다.

## 한 줄 정의

> RSS 피드 URL을 등록받아 주기적으로 fetch하고, 수집한 최신 엔트리를
> HTTP API로 조회할 수 있게 만드는 독립 서비스이다.

## 하는 일

| # | 기능 | 설명 |
|---|---|---|
| 1 | 피드 등록 | RSS/Atom 피드 URL을 등록받는다. |
| 2 | 피드 목록 관리 | 등록된 피드의 목록·상태 조회, 해제를 지원한다. |
| 3 | 주기적 fetch | 등록된 피드를 주기적으로 받아 새 엔트리를 수집한다. |
| 4 | 조건부 요청 | 불필요한 트래픽을 줄이기 위해 fetch 시 조건부 요청을 사용한다. |
| 5 | 실패 대응 | 실패한 피드는 백오프·재시도하고, 반복 실패 시 자동 격리한다. |
| 6 | 엔트리 저장 | fetch한 엔트리를 자체 저장소에 중복 없이 저장한다. |
| 7 | 엔트리 조회 API | 저장된 엔트리를 HTTP API로 조회할 수 있게 노출한다. |
| 8 | 자동 보존 관리 | 일정 기간 지난 엔트리를 자동으로 삭제한다. |
| 9 | 운영 가시성 | 헬스체크, 구조화 로그, 메트릭을 제공한다. |

## 하지 않는 일

다음 기능은 이 서비스가 책임지지 않는다. 필요하면 이 서비스를 호출하는
별도 계층에서 구현한다.

| 영역 | 포함되지 않는 것 |
|---|---|
| 사용자 | 회원가입, 로그인, 세션, 프로필, 사용자 테이블 |
| 개인화 | 구독 관계, 카테고리, 폴더, 태그, 제목 오버라이드 |
| 읽기 상태 | 읽음/안 읽음, 즐겨찾기, 나중에 읽기 |
| 가공 | 요약, 랭킹, 추천, 번역, 본문 재구성 |
| 전달 | 이메일, 푸시, 웹훅, 브로드캐스트 |
| 검색 | 전문 검색, 형태소 분석, 벡터 검색 |
| 보관 | 오래된 엔트리 아카이브, 백업 복원, 히스토리 쿼리 |
| 표현 계층 | 웹 UI, 모바일 앱, 브라우저 확장 |
| 가져오기/내보내기 | OPML import/export, 다른 리더와의 동기화 |
| 크롤 대상 확장 | 일반 웹페이지 크롤, 스크래핑, 이메일 뉴스레터 수신 |

이 목록 중 하나라도 이 리포에 들어오려 할 때는, 먼저 "이것이 정말 피드
수집 파이프라인의 일부인가"를 묻는다.

## 결과

- 서비스는 **자체 저장소를 소유**하며, 외부와는 HTTP API로만 소통한다.
- 엔트리는 "누구의 것"이 아니라 전역이다. 개인화가 필요한 호출자는 자기
  쪽 데이터와 결합해서 사용한다.
- 기능보다 **경계의 선명함**을 우선한다. 이 서비스는 작고 재사용 가능한
  파이프라인이며, 특정 상위 서비스에 종속되지 않는다.
- 보존 기간이 유한하므로 데이터 규모는 구조적으로 제한된다. 파티셔닝·
  아카이브 같은 복잡한 전략이 필요 없다.
- 장애와 배포가 호출자 서비스와 독립된다.
- 이 서비스는 개방된 웹을 크롤하는 특성상 **링크 부식**(URL 소멸·이전,
  엔트리 편집·삭제, 도메인 변경)을 **정상 동작 조건**으로 취급한다. 피드의
  생애 상태와 엔트리의 변경은 예외 처리가 아니라 데이터 모델의 일부다.

## 대안과 기각 사유

- **기능을 한 리포에 전부 구현**: 경계가 흐려지고 릴리스가 늦어진다. 수집
  파이프라인만 먼저 안정화하는 편이 이후 기능을 얹기 쉽다.
- **저장소를 외부 서비스와 공유**: 스키마 결합이 서비스 간 결합이 되어
  독립 배포·리팩터링·장애 격리 이점이 사라진다.
- **무기한 보존 / 아카이브 기능 포함**: 이 서비스의 본질은 "최신화"이며
  기록 보관이 아니다. 아카이브가 필요하면 별도 서비스가 담당한다.

## 후속 ADR에서 다룰 것

- 구현 언어 및 핵심 라이브러리 선택
- 데이터 모델
- HTTP API 계약
- Fetch 스케줄링 전략
- 보존 정책 수치와 재등록 엔트리 처리 규칙
- API 접근 제어 방식

exec
/bin/zsh -lc "sed -n '1,260p' docs/adr/001-core-data-model.md" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
# ADR 001 — 핵심 엔티티와 안정 계약

- 상태: Accepted
- 작성일: 2026-04-10
- 관련: ADR 000, ADR 002, ADR 003, ADR 004,
  `docs/spec/feed.md`, `docs/spec/entry.md`

## 컨텍스트

ADR 000은 이 서비스가 저장하는 것이 "피드(feed)"와 "엔트리(entry)"임을
명시했고, "경계는 HTTP 계약뿐"이라는 원칙과 "링크 부식은 정상 입력"이라는
원칙을 두었다. 이 ADR은 그 두 엔티티의 **불변식(invariant)**만 박는다.

여기 나열하는 불변식은 **ADR 수정 없이는 바꿀 수 없는** 계약이다.

테이블의 구체 컬럼, 타입, 인덱스, 상태 머신, 동작 규칙은 이 ADR의 대상이
**아니다**. 그것들은 `docs/spec/feed.md`와 `docs/spec/entry.md`에서 정의
한다. Spec은 이 ADR의 불변식을 준수하는 한 자유롭게 갱신될 수 있다.

## 결정

### 두 핵심 엔티티

- **Feed** — 사용자가 등록한 RSS/Atom 피드. **생애 상태**를 가진 리소스.
- **Entry** — 피드에서 파싱한 개별 항목. **변경 가능한 캐시**.

### 불변식

1. **피드의 외부 식별자는 `id`** (내부 서로게이트, 한 번 발급되면 불변).
2. **엔트리의 외부 안정 식별자는 `(feed_id, guid)`**. 엔트리의 내부 `id`는
   재삽입 시 변경될 수 있으므로 외부 계약에서 식별자로 쓸 수 없다.
3. **`guid`는 피드가 제공하는 값**이다. 우리는 생성·수정하지 않는다. 해당
   피드 내에서 같은 엔트리를 가리키는 한 불변으로 취급한다.
4. **엔트리의 보존 기준은 `fetched_at`** (ADR 004). 이 값은 **최초 저장
   시각**이며, 편집이나 재fetch로 갱신되지 **않는다**. 편집 추적이 필요하면
   별도 필드(spec/entry.md 참조)를 쓴다.
5. **엔트리는 upsert 대상**이다. 같은 `(feed_id, guid)`에 대해 본문·제목·
   `published_at` 등이 변경되면 최신 값을 반영한다. 이 서비스는 캐시이며,
   편집은 정상 입력이다(ADR 000).
6. **피드의 생애 상태는 API 노출 계약의 일부**다. "URL이 영원히 동작한다"는
   가정하지 않으며(ADR 000), 피드가 지금 살아있는지를 사용자는 API 응답
   만으로 판단할 수 있어야 한다.
7. **피드 해제 시 그 피드의 모든 엔트리는 cascade 삭제**된다.
8. **스케줄러 메타데이터**(`etag`, `last_modified`, `next_fetch_at`,
   연속 실패 카운터 등)는 이 불변식 목록에 포함되지 않는다. ADR 003에
   따라 additive하게 관리되는 구현 세부사항이다.

### 구체 스키마 위치

- 피드 테이블의 컬럼·제약·인덱스·생애 상태 머신·fetch 동작 —
  `docs/spec/feed.md`
- 엔트리 테이블의 컬럼·제약·인덱스·upsert 규칙·mutation 정책 —
  `docs/spec/entry.md`

Spec에 새 컬럼을 추가하거나 기존 컬럼의 동작을 조정하는 것은 이 ADR의
불변식을 깨지 않는 한 **ADR 개정 대상이 아니다**. 예컨대:

- `feeds`에 `last_attempt_at`을 추가하는 것 — spec 갱신만
- `entries`에 `content_updated_at`을 추가하는 것 — spec 갱신만
- 인덱스 추가·변경 — spec 갱신만
- `entries.fetched_at`의 의미를 "최초 저장"에서 "마지막 fetch"로 바꾸는 것
  — 불변식 #4 위반, **ADR 개정 필요**
- `guid`를 서비스가 생성하도록 바꾸는 것 — 불변식 #3 위반, **ADR 개정 필요**

## 결과

- ADR 002의 API 응답은 spec이 정의한 필드 집합 위에 **계약 원칙**만 부여
  한다(식별자 규칙, 버저닝 정책 등).
- ADR 004의 보존 정책은 불변식 #4에 의지한다. `fetched_at`이 편집으로
  갱신되지 않음이 보존 정책의 전제다.
- 엣지케이스(도메인 소멸, URL 이전, 편집, 재등장, 엔트리 사라짐 등)의
  처리 규칙은 spec에서 정의된다. ADR은 원칙만 담는다.
- Spec 갱신은 자유롭지만, 불변식 위반은 PR 리뷰에서 차단되어야 한다.

## 기각한 대안

- **컬럼 목록을 ADR 본문에 직접 나열**
  기각: 편집·추가·인덱스 조정이 정상적인 개발 흐름인데 ADR을 자주 고치면
  결정 로그의 신뢰성이 무너진다. ADR은 불변식만 담고 세부는 spec에 둔다.

- **`feeds`의 생애 상태 필드(status 등)를 스케줄러 메타데이터로 분류**
  기각: 이 필드들은 **API가 사용자에게 보여주는 값**이다. 내부 스케줄링
  전용이 아니다. API 계약의 일부이므로 spec/feed.md의 본질 컬럼에 속한다.

- **엔트리를 write-once로 (편집 무시)**
  기각: 편집·정정은 실제 피드에서 흔한 일이며, "캐시"는 정의상 최신 상태를
  반영해야 한다. write-once는 "최신 피드 캐시"라는 ADR 000의 정의와
  충돌한다.

- **엔트리 편집 시 `fetched_at`도 갱신**
  기각: 그러면 오래된 엔트리가 편집 한 번에 보존 정책을 회피하게 된다.
  보존의 "나이" 개념이 무너진다.

## 미해결

- DB 엔진·구현 언어·구체 SQL 타입 — 별도 결정.
- URL 정규화 상세 규칙 — `docs/spec/feed.md` 또는 전용 spec에서 정의.
- 엔트리에 `guid`가 전혀 없는 피드의 폴백 정책 — spec/entry.md의 미해결로
  이관.

exec
/bin/zsh -lc "sed -n '1,260p' docs/adr/002-http-api-contract.md" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
# ADR 002 — HTTP API 계약

- 상태: Accepted
- 작성일: 2026-04-10
- 관련: ADR 000, ADR 001, ADR 003, ADR 004,
  `docs/spec/feed.md`, `docs/spec/entry.md`

## 컨텍스트

ADR 000은 "외부와의 경계는 HTTP 계약뿐"이라고 했고, 모든 엔드포인트가
`/v1/` 프리픽스를 가진다고 했다. ADR 001은 두 엔티티(feed, entry)의
**불변식**을 박았다. 이 ADR은 그것을 **외부에 어떻게 노출할 것인가**의
계약 원칙을 박는다.

이 ADR이 정의하는 것 (안정 계약):

- 엔드포인트 경로와 HTTP 메서드
- 식별자 규칙
- 페이지네이션 방식과 그 한계
- 에러 응답 형식 골격
- 버저닝 규칙

이 ADR이 정의하지 **않는** 것:

- 응답 필드의 구체 목록 — `docs/spec/feed.md`, `docs/spec/entry.md`
- 인증 방식 — 후속 ADR
- 레이트 리밋·에러 코드 수치 — spec
- 서버 구현 언어·프레임워크 — 별도 결정

## 결정

### 엔드포인트

| 메서드 | 경로 | 용도 |
|---|---|---|
| `POST` | `/v1/feeds` | 피드 등록 |
| `GET` | `/v1/feeds` | 등록된 피드 목록 (`?status=active\|broken\|dead` 필터 선택) |
| `GET` | `/v1/feeds/{id}` | 단일 피드 조회 |
| `DELETE` | `/v1/feeds/{id}` | 피드 해제 (등록 취소) |
| `POST` | `/v1/feeds/{id}/reactivate` | `dead` 피드를 `active`로 수동 복구 (spec/feed.md 상태 머신 참조) |
| `GET` | `/v1/entries` | 엔트리 목록 조회 |
| `GET` | `/healthz` | 헬스체크 |

운영 가시성을 위한 메트릭 엔드포인트(Prometheus 등)는 `/v1/` 밖에 둔다.
경로·포맷은 spec에서 결정한다.

### 식별자 규칙

- **피드의 외부 식별자**는 `feeds.id` (불변, ADR 001).
- **엔트리의 외부 안정 식별자**는 `(feed_id, guid)`. 엔트리 응답은 내부
  `id`와 `guid`를 **둘 다 포함**한다. 같은 엔트리를 판단할 때 클라이언트는
  반드시 `guid`를 기준으로 한다(ADR 001).

### 응답 필드

응답의 구체 필드 목록과 의미는 spec이 정의한다:

- 피드 응답 — `docs/spec/feed.md`
- 엔트리 응답 — `docs/spec/entry.md`

이 ADR이 강제하는 필드 관련 원칙:

1. **피드 응답은 생애 상태를 기본으로 포함한다.** 상태(`status`), 마지막
   성공 시각, 마지막 에러 코드 등 "이 피드가 지금 살아있는가"를 판단할 수
   있는 정보를 spec이 정한 이름으로 노출한다. 이것은 링크 부식을 정상
   입력으로 다루는(ADR 000) 필연적 결과다.
2. **엔트리 응답은 최초 저장 시각과 편집 시각을 구분해서 노출한다.** 전자는
   보존 기준(ADR 001·004), 후자는 "새 엔트리" 대 "편집된 엔트리"를 구분
   하는 수단이다. 정확한 필드명은 spec.
3. **`url`(사용자 입력)과 실제 fetch 대상 URL은 구분 가능해야 한다.**
   301 permanent redirect를 따라가도 사용자가 등록한 URL은 응답에 남는다.
   spec이 필드명을 정한다.
4. **새 필드 추가는 breaking change가 아니다.** 클라이언트는 모르는 필드를
   무시해야 한다. Spec에 필드를 추가하는 것은 ADR 개정 없이 가능하다.
5. **기존 필드의 의미·타입 변경 또는 삭제는 breaking change**이며 `/v2/`로
   가야 한다.

### 피드 등록 — `POST /v1/feeds`

- 요청: `{ "url": "..." }`
- 응답: 등록된 피드 객체 (spec/feed.md가 정의)
- **이미 등록된 URL이면 기존 피드를 반환한다** (멱등). 409를 반환하지
  않는다.
- 등록 직후 첫 fetch 완료를 API 응답이 **보장하지 않는다**. 응답은 "등록
  되었음"만 의미한다. 첫 fetch는 내부 동작이며 비동기다.

### 피드 목록 — `GET /v1/feeds`

- 페이지네이션: keyset, 불투명 커서 문자열.
- 상태별 필터(`?status=active`)는 spec에서 정의한다(additive).

### 피드 해제 — `DELETE /v1/feeds/{id}`

- 해당 피드와 **그 피드의 모든 엔트리**를 cascade 삭제한다.
- 멱등. 존재하지 않는 id에 대한 구체 응답 코드는 spec.

### 엔트리 조회 — `GET /v1/entries`

쿼리 파라미터:

| 파라미터 | 의미 |
|---|---|
| `feed_ids` | 쉼표 구분된 feed id 목록. **지정 필수**. 전역 스캔 금지. |
| `cursor` | keyset 커서 (불투명 문자열) |
| `limit` | 반환 개수 상한. 서버가 최대값 강제. |

**페이지네이션은 keyset 방식이다. OFFSET은 지원하지 않는다.**

정렬 기준은 `(published_at DESC, id DESC)`. 커서는 이 순서쌍을 인코딩한
불투명 문자열이며, 클라이언트는 내부 구조를 가정하지 않는다.

`feed_ids`의 최대 길이와 `limit` 최대값은 spec.

### 편집이 있을 때의 페이지네이션 — best-effort

엔트리는 upsert된다(ADR 001 불변식 #5). 편집으로 `published_at`이 변경
되면 정렬 순서가 변한다. 따라서 `/v1/entries`의 keyset 페이지네이션은
편집에 대해 **best-effort**다. 구체적으로:

- 클라이언트는 같은 엔트리를 두 번 받을 수 있다 → **`guid`로 중복 제거
  해야 한다**
- 이전 페이지에 있던 엔트리가 다음 요청에서 안 보일 수 있다
- 완전한 동기화를 원하는 클라이언트는 편집 시각 기반 보조 스윕을 구현해야
  한다(구체 필드명은 spec/entry.md 참조)

이 한계는 "캐시의 최신성"을 위한 **의도적 트레이드오프**다. 절대적 정확성이
필요한 호출자는 이 서비스를 그대로 쓰면 안 된다.

### 에러 응답 형식

에러 응답은 **RFC 7807 / RFC 9457 Problem Details**를 따른다.
Content-Type은 `application/problem+json`.

필드:
- `type` (URI) — 문제 클래스 식별자. 기본값 `about:blank` (추가 정보 없음을 뜻하는 RFC 7807 센티넬). 특정 도메인 에러는 `https://feedgate.dev/problems/<slug>`로 확장 가능 (additive).
- `title` — 사람이 읽는 짧은 요약 (HTTP 상태 문구 기본값)
- `status` — HTTP 상태 코드 (줄 상태와 중복이지만 본문만 받는 클라이언트 위해)
- `detail` — 구체 에러 메시지
- `instance` — 에러가 발생한 요청 경로

예:
```json
{
  "type": "about:blank",
  "title": "Bad Request",
  "status": 400,
  "detail": "blocked_url: unsupported scheme: 'file'",
  "instance": "/v1/feeds"
}
```

HTTP 상태 코드는 표준 의미를 따른다. 구체 에러 분류의 type URI 추가는
additive이며 breaking change가 아니다.

### 버저닝

- 모든 리소스 엔드포인트는 `/v1/` 프리픽스를 갖는다(ADR 000).
- Breaking change(필드 삭제·의미 변경)가 필요하면 `/v2/`를 병행 운영한다.
- **새 필드 추가는 breaking change가 아니다.**

## 결과

- 응답 필드 집합은 spec이 소유한다. 컬럼 추가·인덱스 변경이 ADR을 건드
  리지 않는다.
- "같은 엔트리"를 판단해야 하는 호출자는 `guid`를 쓴다. 이것은 API 문서에
  명시한다.
- 페이지네이션은 편집에 대해 best-effort임을 API 문서가 **명시적으로**
  경고한다. 숨기지 않는다.
- 피드 응답의 생애 상태 노출은 선택이 아니라 **기본값**이다.

## 기각한 대안

- **OFFSET 페이지네이션**
  기각: N이 커지면 쿼리 플래너가 포기한다. keyset은 인덱스와 맞물려 일정
  비용으로 동작한다.

- **엔트리 외부 식별자로 내부 `id` 단일 사용**
  기각: 재삽입 시 변경되므로 안정 식별자가 아니다.

- **`feed_ids` 없이 전역 `/v1/entries` 허용**
  기각: 서버 전역 스캔 부담 + 호출자 관심사 필터링 실패.

- **편집 시 페이지네이션 일관성 보장**
  기각: 캐시는 최신성이 본질이고, 그 대가로 정렬 순서 흔들림을 받아들인다.
  일관성을 원하면 snapshot 서비스가 별도로 필요.

- **피드 해제 시 엔트리 남겨두기**
  기각: 사용자 모델 위반. Cascade.

- **응답 필드 목록을 ADR 본문에 고정**
  기각: 새 필드 추가마다 ADR을 고치면 결정 로그의 신뢰성이 떨어진다. ADR은
  원칙만, 필드는 spec에.

- **피드 생애 상태 필드를 응답의 선택 항목으로**
  기각: 링크 부식은 정상 입력이고(ADR 000) 사용자가 피드 건강 상태를 몰라
  행동하는 것이 더 위험하다. 기본 노출이 안전한 선택.

## 미해결

- 인증·접근 제어 — 후속 ADR.
- 레이트 리밋, `feed_ids` 최대 길이, `limit` 최대값 — spec.
- 메트릭 엔드포인트 경로·포맷 — spec.
- 구체 에러 코드 목록과 HTTP 상태 매핑 — spec.
- `DELETE /v1/feeds/{id}`의 non-existent id 응답 코드 — spec.
- 편집된 엔트리의 "보조 동기화" 쿼리 파라미터가 필요한지 — spec에서 결정,
  필요해지면 additive 추가.

exec
/bin/zsh -lc 'wc -l src/feedgate/**/*.py tests/*.py docs/spec/*.md docs/adr/*.md | sort -n' in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
       3 src/feedgate/__init__.py
       5 src/feedgate/fetcher/__init__.py
      12 src/feedgate/api/health.py
      13 tests/test_api_health.py
      22 tests/test_foundation.py
      46 src/feedgate/db.py
      46 src/feedgate/lifecycle.py
      53 tests/test_urlnorm.py
      56 src/feedgate/api/__init__.py
      57 tests/test_migrations.py
      59 src/feedgate/urlnorm.py
      61 src/feedgate/schemas.py
      62 tests/test_errors.py
      85 docs/adr/000-service-purpose-and-scope.md
      90 src/feedgate/errors.py
      94 src/feedgate/fetcher/parser.py
      94 src/feedgate/fetcher/upsert.py
      96 docs/adr/003-collection-is-internal-implementation.md
      99 docs/adr/001-core-data-model.md
      99 tests/test_api_auth.py
     119 src/feedgate/config.py
     130 src/feedgate/api/entries.py
     130 tests/test_e2e_walking_skeleton.py
     132 src/feedgate/ssrf.py
     135 src/feedgate/retention.py
     137 docs/adr/004-entry-retention-policy.md
     145 tests/test_parser.py
     146 src/feedgate/models.py
     185 tests/conftest.py
     186 src/feedgate/main.py
     191 src/feedgate/api/feeds.py
     206 docs/adr/002-http-api-contract.md
     206 tests/test_upsert.py
     211 tests/test_lifespan_drain.py
     233 docs/spec/entry.md
     234 tests/test_api_entries.py
     241 src/feedgate/fetcher/scheduler.py
     325 tests/test_retention.py
     342 tests/test_ssrf.py
     347 tests/test_api_feeds.py
     378 docs/spec/api.md
     406 docs/spec/feed.md
     472 src/feedgate/fetcher/http.py
     492 docs/spec/resilience.md
     639 tests/test_scheduler_tick.py
    1873 tests/test_fetch_one.py
    9393 total

exec
/bin/zsh -lc "sed -n '1,260p' docs/spec/feed.md" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
# Spec: Feed

- 상태: Draft
- 마지막 업데이트: 2026-04-12
- 관련 ADR: 000, 001, 002, 003
- 관련 spec: [`entry.md`](entry.md), [`resilience.md`](resilience.md)

이 문서는 `feeds` 엔티티의 **현재 구현 정의**다. 정책 결정은 ADR에 있고,
이 spec은 테이블 스키마와 동작을 기술한다. Spec은 ADR 001의 불변식을
깨지 않는 한 자유롭게 갱신할 수 있다. **위협 모델·SSRF·per-host throttle·
graceful shutdown 등 부하 방어 전반은 [`resilience.md`](resilience.md)에**
별도 정의되어 있으니 fetch 동작의 방어선과 미구현 항목을 함께 확인할 것.

## 목적

사용자가 등록한 RSS/Atom 피드의 URL과 **생애 상태**를 보관한다. 생애
상태는 이 서비스의 일등 시민이다 — 링크 부식을 정상 입력으로 다루기 위함
(ADR 000).

## 테이블 스키마

### `feeds` — 본질 컬럼 (API 노출 대상)

| 컬럼 | 타입 | NULL | 기본값 | 설명 |
|---|---|---|---|---|
| `id` | bigint (PK) | NOT NULL | auto | 내부 서로게이트 키 |
| `url` | text | NOT NULL | — | **사용자가 등록한 URL**. 정규화 후 저장. 등록 후 변경되지 않음 |
| `effective_url` | text | NOT NULL | = url | 현재 fetch 대상 URL. 301 Permanent Redirect 시 갱신 |
| `title` | text | NULL | — | 피드 메타데이터의 제목. fetch마다 최신값 반영 |
| `status` | text | NOT NULL | `'active'` | `active` / `broken` / `dead` 중 하나 |
| `last_successful_fetch_at` | timestamptz | NULL | — | 마지막으로 성공적으로 받아온 시각 |
| `last_attempt_at` | timestamptz | NULL | — | 마지막 fetch 시도 시각 (성공·실패 무관) |
| `last_error_code` | text | NULL | — | 마지막 실패의 에러 코드 (아래 표 A) |
| `created_at` | timestamptz | NOT NULL | now() | 등록 시각 |

### `feeds` — 스케줄러 메타데이터 (API 노출 안 함, ADR 003)

| 컬럼 | 타입 | NULL | 설명 |
|---|---|---|---|
| `next_fetch_at` | timestamptz | NOT NULL | 다음 fetch 예정 시각 |
| `consecutive_failures` | int | NOT NULL DEFAULT 0 | 연속 실패 횟수. 성공 시 0으로 리셋 |

이 컬럼들은 수집 전략 변경에 따라 additive하게 추가·제거될 수 있다.
API는 이 값들을 노출하지 않는다.

### 제약과 인덱스

- `UNIQUE (url)` — 중복 등록 방지 (멱등 등록, ADR 002)
- `INDEX (status)` — 상태별 필터링
- `INDEX (next_fetch_at) WHERE status = 'active'` — 스케줄러의 "곧 fetch할
  대상" 조회 최적화. 부분 인덱스로 크기 최소화.

## 생애 상태 머신

```
          등록
            │
            ▼
      ┌──────────┐   연속 N회 실패   ┌──────────┐
      │  active  │ ────────────────▶│  broken  │
      └──────────┘                   └──────────┘
            ▲                             │
            │                             │ 성공
            │ 성공                        │ (recovery)
            └─────────────────────────────┘
                          │
                          │ 영구 실패 신호:
                          │  - HTTP 410 Gone (즉시)
                          │  - M회 이상 실패 + 영구 에러 부류
                          ▼
                     ┌──────────┐
                     │   dead   │
                     └──────────┘
                     자동 fetch 중단. 수동 재활성화만 가능.
```

### 전이 규칙

모범 사례 패턴: **exponential backoff + circuit breaker**. Netflix
Hystrix / AWS SDK retry / resilience4j와 동일한 철학.

| 현재 | 트리거 | 다음 | 비고 |
|---|---|---|---|
| `active` | `consecutive_failures >= BROKEN_THRESHOLD` | `broken` | 기본 3회 |
| `active` 또는 `broken` | HTTP 410 Gone 수신 | `dead` | 즉시 전이 (유일한 영구 HTTP 신호) |
| `broken` | fetch 성공 (200) | `active` | counters 리셋 |
| `broken` | **마지막 성공 이후 경과 시간 ≥ `DEAD_DURATION`** | `dead` | **시간 기반**, 기본 7일 |
| `dead` | 주간 자동 probe 성공 | `active` | counters 리셋 |
| `dead` | `POST /v1/feeds/{id}/reactivate` | `active` | 수동 재활성화 |

#### 왜 dead 전이가 시간 기반인가 (count가 아니라)

Count 기반(`consecutive_failures >= M`)은 `fetch_interval_seconds`에
취약하다. interval이 3초일 때 30회 실패는 90초, 60초일 때는 30분,
600초일 때는 5시간 — 의미가 전혀 달라진다. 반면 **"마지막 성공 이후
경과 시간"은 interval 설정과 무관**하게 일관된 의미를 갖는다.

7일 기준을 고른 이유:
- 주말(금~일) + 월요일 infra outage를 전부 흡수
- 대부분 일시 장애는 72시간 이내 복구
- Feedly 같은 상용 리더기도 주(week) 단위로 dead 판정

"마지막 성공 시간"이 없는 피드 (첫 성공 전에 broken된 경우)는
`created_at`을 fallback으로 사용한다. 즉 등록 후 7일간 한 번도 성공
못 했으면 dead.

### Broken 상태 exponential backoff

Broken 상태 피드는 다음 공식으로 `next_fetch_at`을 계산한다:

```
excess = max(0, consecutive_failures - BROKEN_THRESHOLD)
backoff_factor = 2 ** excess              # 1, 2, 4, 8, 16, ...
raw_interval = base_interval * backoff_factor
capped = min(raw_interval, BROKEN_MAX_BACKOFF_SECONDS)
jitter = random_uniform(-BACKOFF_JITTER_RATIO, +BACKOFF_JITTER_RATIO) * capped
next_fetch_at = now + timedelta(seconds = capped + jitter)
```

예시 타임라인 (base_interval=60s, cap=3600s, jitter=±25%):

| consecutive_failures | backoff | 다음 시도까지 |
|---|---|---|
| 1 (active) | 60s | ~60s |
| 2 (active) | 60s | ~60s |
| 3 (→ broken 전이) | 60s | ~60s |
| 4 | 120s | 90~150s |
| 5 | 240s | 180~300s |
| 6 | 480s | 360~600s |
| 7 | 960s | 720~1200s |
| 8 | 1920s | 1440~2400s |
| 9 | 3840s → **3600s (cap)** | 2700~4500s |
| 10+ | 3600s (cap) | 2700~4500s |

**왜 지터가 필요한가**: 다수 피드가 동시에 broken에 들어갔을 때 (공통
upstream 장애) 회복 시점도 동시에 몰려 *thundering herd*가 발생한다.
±25% 지터로 회복 요청을 시간축에 분산.

7일간 동일 호스트로 약 168회 재시도 → 7일 경과 시 dead 전이.

### 429 Rate Limited 특수 처리

429 응답은 **circuit breaker 관점에서 "실패가 아니다"**. "천천히 해"와
"고장났다"는 완전히 다른 신호이므로 섞으면 일시 과부하 서버가 false
positive로 dead 전이된다.

규칙:

| 필드 | 429 수신 시 동작 |
|---|---|
| `consecutive_failures` | **증가 안 함** |
| `status` | 전이 **없음** |
| `last_successful_fetch_at` | 변경 없음 (실패가 아님) |
| `last_error_code` | `"rate_limited"` 기록 |
| `last_attempt_at` | 갱신 |
| `next_fetch_at` | `Retry-After` 헤더 존중: `now + max(retry_after_seconds, base_interval)`. 헤더가 없거나 파싱 실패 시 `base_interval` 사용 |

RFC 6585 + AWS / GCP / Cloudflare SDK가 모두 동일한 패턴을 쓴다.

### Dead 재활성화: 자동 주간 probe + 수동 API

**자동 주간 probe**: scheduler의 `tick_once` 쿼리는 기본적으로 dead
피드를 제외하지만, **`last_attempt_at < now - DEAD_PROBE_INTERVAL`**인
dead 피드는 예외적으로 포함한다. Dead 피드는 주 1회 fetch 시도를 받고:

- 성공 → `active`로 복귀, counters 리셋
- 실패 → 그대로 `dead` 유지, `last_attempt_at`만 갱신 (counters는 이미
  상한 이상이므로 증가 안 함)

이 방식으로 영구 사라진 것으로 판정됐지만 실제로는 일시적이었던 피드가
자연 회복될 수 있다.

**수동 API**: `POST /v1/feeds/{id}/reactivate` 엔드포인트 (ADR 002
업데이트)로 운영자가 명시적으로 부활시킬 수 있다. 호출 시:

1. `status = 'active'`
2. `consecutive_failures = 0`
3. `last_error_code = NULL`
4. `next_fetch_at = now()` (즉시 다음 tick에 fetch)
5. 응답: 갱신된 `FeedResponse`

### 임계값 (환경변수, 운영 중 조정 가능)

| 환경변수 | 기본값 | 의미 |
|---|---|---|
| `FEEDGATE_BROKEN_THRESHOLD` | 3 | active → broken 전이 연속 실패 수 |
| `FEEDGATE_DEAD_DURATION_DAYS` | 7 | 마지막 성공 이후 이 기간이 지나면 broken → dead |
| `FEEDGATE_BROKEN_MAX_BACKOFF_SECONDS` | 3600 | exponential backoff 상한 (1시간) |
| `FEEDGATE_BACKOFF_JITTER_RATIO` | 0.25 | ±25% 지터 |
| `FEEDGATE_DEAD_PROBE_INTERVAL_DAYS` | 7 | dead 피드 자동 재시도 주기 |

수치는 운영 데이터를 보고 조정한다. ADR 개정 없이 바꿀 수 있다.

### 관찰 가능성

상태 전이 시 `logger.warning` 레벨 로그 1건 출력:

```
feed_id=42 url=... state=active->broken reason=consecutive_failures>=3
feed_id=42 url=... state=broken->dead reason=7d_since_last_success
feed_id=42 url=... state=dead->active reason=probe_succeeded
feed_id=42 url=... state=dead->active reason=manual_reactivate
```

info 레벨은 현재 stdlib root logger가 WARNING 컷이라 안 보이므로
전이는 warning으로 올림. 구조화 로깅 / 메트릭은 out-of-scope.

## 에러 코드 — 표 A

`last_error_code`의 표준 값. 사용자에게 노출되는 문자열이므로 안정적으로
유지한다.

| 코드 | 의미 | 부류 |
|---|---|---|
| `dns` | DNS 조회 실패 | 일시 (지속 시 영구로 간주) |
| `tcp_refused` | 연결 거부 | 일시 |
| `tls_error` | SSL/TLS 에러 | 일시 |
| `timeout` | 요청 타임아웃 | 일시 |
| `http_4xx` | 400~499 (404 포함, 410 제외) | 일시 |
| `http_410` | 410 Gone | **영구 (즉시 dead)** |
| `http_5xx` | 500~599 | 일시 |
| `rate_limited` | 429 | 일시 (백오프) |
| `not_a_feed` | 200 OK 지만 XML/피드 아님 | 일시 |
| `parse_error` | XML/피드 파싱 실패 | 일시 |
| `redirect_loop` | redirect 체인이 상한 초과 | 일시 |
| `too_large` | 응답 크기 상한 초과 | 일시 (설정 조정 대상) |
| `other` | 기타 분류되지 않은 실패 | 일시 |

## Fetch 동작

### 기본 흐름

1. `effective_url`로 HTTP GET 요청
2. 헤더: `User-Agent`
3. 응답 코드별 처리:

| 상태 | 처리 |
|---|---|
| `200 OK` | Content-Type 검사 → 파싱 → 엔트리 저장 (spec/entry.md) → 성공 |
| `301 Moved Permanently` | Location 헤더로 `effective_url` 갱신. 새 URL로 1회 재시도 |
| `302 / 307 / 308` | Location 따라가되 `effective_url` 불변 |
| `4xx` (410/429 제외) | 실패 처리, `last_error_code = http_4xx` |
| `410 Gone` | 즉시 `status = dead`, `last_error_code = http_410` |
| `429` | **특수 처리** — "429 Rate Limited 특수 처리" 섹션 참조. counters/상태 불변, `Retry-After` 존중 |
| `5xx` | 실패 처리, `http_5xx` |

4. Content-Type이 `application/rss+xml`, `application/atom+xml`,
   `application/xml`, `text/xml` 계열이 아니면 → `not_a_feed`
5. 응답 본문 크기 > 상한 → `too_large`
6. 파싱 실패 → `parse_error`

### 타이머·상태 필드 업데이트 규칙

| 필드 | 성공 (200) | 실패 |
|---|---|---|
| `last_attempt_at` | 갱신 | 갱신 |
| `last_successful_fetch_at` | 갱신 | 유지 |
| `last_error_code` | `NULL`로 리셋 | 새 코드 설정 |
| `consecutive_failures` | `0`으로 리셋 | `+1` |
| `title` | 새 값이 있으면 갱신 | 유지 |

exec
/bin/zsh -lc "sed -n '261,520p' docs/spec/feed.md" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
| `status` | 상태 머신 적용 | 상태 머신 적용 |
| `next_fetch_at` | 스케줄러가 재계산 | 스케줄러가 재계산 (보통 더 먼 미래) |

### 301 Permanent Redirect 처리

- Location 헤더의 절대 URL을 읽는다
- `effective_url`을 그 값으로 갱신
- `url` (사용자가 등록한 원본)은 **절대 건드리지 않는다**
- 같은 fetch 사이클에서 새 URL로 **1회만** 재시도 (중첩 금지, redirect
  chain은 상한 두고 처리)

### 네트워크 설정 (환경변수)

모든 값은 `FEEDGATE_` 접두사. 실제 목록은 `src/feedgate/config.py`가 단일 source of truth.

- `FEEDGATE_FETCH_CONNECT_TIMEOUT_SECONDS` — TCP 연결 단계 타임아웃 (기본 5.0초)
- `FEEDGATE_FETCH_READ_TIMEOUT_SECONDS` — 청크 단위 read 비활성 타임아웃 (기본 15.0초)
- `FEEDGATE_FETCH_WRITE_TIMEOUT_SECONDS` — 요청 바디 write 타임아웃 (기본 10.0초)
- `FEEDGATE_FETCH_POOL_TIMEOUT_SECONDS` — 커넥션 풀 획득 타임아웃 (기본 5.0초)
- `FEEDGATE_FETCH_TOTAL_BUDGET_SECONDS` — 한 fetch 전체 벽시계 상한 (기본 30.0초). Slow-loris 방어용 `asyncio.timeout` 가드. 상세는 `resilience.md` 참조.
- `FEEDGATE_FETCH_MAX_BYTES` — 응답 본문 크기 상한 (기본 5 MiB)
- `FEEDGATE_FETCH_USER_AGENT` — User-Agent 문자열 (기본 `feedgate-fetcher/<ver> (+url)`)
- `FEEDGATE_FETCH_MAX_ENTRIES_INITIAL` — 첫 fetch 시 저장할 최대 엔트리 수 (기본 50).
  OpenAI 909건 같은 케이스 방어. 이후 fetch는 새 엔트리만 들어오므로 이
  상한과 무관.

Redirect chain 상한은 현재 httpx 기본값(20 hops)을 그대로 사용하며 별도 설정 항목이 없다 — 명시적 설정이 필요하면 config.py와 이 목록을 additive하게 확장.

## 등록·조회·해제

### 등록 — `POST /v1/feeds`

1. 요청 body: `{ "url": "..." }`
2. URL 정규화 (아래 "URL 정규화" 섹션)
3. 정규화된 URL로 기존 row 조회
4. 있으면 그 row 반환 (멱등, ADR 002)
5. 없으면 INSERT:
   - `url` = 정규화된 사용자 입력
   - `effective_url` = 같은 값
   - `status = 'active'`
   - 타이머 필드 모두 NULL
   - `next_fetch_at = now()` (즉시 fetch 대상)
6. 내부 큐에 "즉시 fetch" 작업 enqueue (API 응답은 기다리지 않음, ADR 002)
7. 생성된 row 반환

### 조회 — `GET /v1/feeds/{id}`

본질 컬럼을 그대로 노출. 응답 예시:

```json
{
  "id": 42,
  "url": "https://example.com/feed.xml",
  "effective_url": "https://example.com/feed.xml",
  "title": "Example Blog",
  "status": "active",
  "last_successful_fetch_at": "2026-04-10T03:14:15Z",
  "last_attempt_at": "2026-04-10T03:14:15Z",
  "last_error_code": null,
  "created_at": "2026-01-20T09:00:00Z"
}
```

스케줄러 메타데이터(`next_fetch_at`, `consecutive_failures` 등)는
응답에 포함하지 않는다.

### 목록 — `GET /v1/feeds`

Keyset 페이지네이션. 정렬: `(id ASC)`. `?status=active` 필터 지원(옵션).

### 해제 — `DELETE /v1/feeds/{id}`

- 해당 row 삭제
- 연결된 `entries`는 `ON DELETE CASCADE`로 자동 삭제
- 멱등

## URL 정규화

최소 규칙:

- scheme 소문자화 (`HTTPS:` → `https:`)
- host 소문자화
- default port 제거 (`:80`, `:443`)
- 경로 끝 trailing slash 제거 (루트 `/` 제외)
- fragment(`#...`) 제거
- 쿼리 파라미터 **보존** (일부 피드는 쿼리로 필터링)
- IDN 호스트는 punycode로 정규화

상세 edge case(퍼센트 인코딩 케이스 정규화 등)는 구현 단계에서 보완.

## 엣지케이스 체크리스트

링크 부식은 정상 입력이다(ADR 000). 다음 시나리오는 전부 정상 동작으로
취급한다.

### 네트워크·전송

- [x] DNS 실패 (`dns`)
- [x] Connection refused (`tcp_refused`)
- [x] SSL 인증서 오류 (`tls_error`)
- [x] 느린 응답 → 타임아웃 (`timeout`)
- [x] 응답 크기 폭발 → `too_large`
- [x] Redirect loop / 체인 상한 초과 → `redirect_loop`

### HTTP 상태

- [x] 301 Permanent → `effective_url` 갱신, 1회 재시도
- [x] 302/307/308 → 따라가되 url 불변
- [x] 4xx (410 제외) → `http_4xx`, 일시 실패
- [x] 410 Gone → 즉시 `dead`
- [x] 429 → `rate_limited`, 백오프
- [x] 5xx → `http_5xx`, 일시 실패

### 컨텐츠

- [x] 200 OK + HTML 반환 (RSS 엔드포인트 죽음) → `not_a_feed`
- [x] 200 OK + 깨진 XML → `parse_error`
- [x] 빈 피드 (0 entries) → 성공 처리, 엔트리 저장 로직 no-op
- [x] 비 UTF-8 인코딩 → 파싱 시 디코딩 시도
- [x] 거대 피드 (OpenAI 909건) → 첫 fetch는 `FETCH_MAX_ENTRIES_INITIAL`로
      상한

### 생애

- [x] 연속 실패 누적 → broken → dead
- [x] 도메인 NXDOMAIN 지속 → `dns` 누적 → 결국 dead
- [x] 피드가 "조용함" (새 글 없음) → 정상. 스케줄러는 계속 동작
- [x] Cloudflare/WAF가 봇 차단 → 403 → `http_4xx` 누적 → 결국 dead

## 실측 참고

seed 피드(22개) probe 결과(`docs/notes/collection-scaling-research.md`와
`/tmp/feedgate_probe.json`):

- 14/20 피드가 ETag 또는 Last-Modified 지원 (70%). 향후 조건부 요청 구현 시 업스트림 트래픽 절감 여지를 보여주는 참고 수치 — 현 구현은 ETag/IMS 미사용으로 모든 피드를 매 tick 전체 재수신
- 0/20 피드가 WebSub hub 광고 → 이 시드 믹스에서는 push 최적화 여지 없음
- 응답 크기 최대 1.7MB (Chips and Cheese) → 5MB 상한이면 안전
- Uber RSS URL은 404 (죽음) → Uber 같은 케이스가 dead 전이의 대표 사례
- 응답 지연 p95 약 1.5초 → 20초 타임아웃이면 여유

## 미해결

- ETag / If-Modified-Since 조건부 요청 미구현. 향후 feature PR에서 컬럼 재도입 + 헤더 송신 + 304 처리 + 타이머 업데이트 규칙 부활 예정.
- URL 정규화 상세 (퍼센트 인코딩 대소문자, 중복 슬래시 등)
- `FETCH_MAX_ENTRIES_INITIAL` 기본값 확정 (50? 100?)
- 영구 에러 부류의 자동 승격 규칙(예: DNS 실패가 7일 지속되면 dead로)

exec
/bin/zsh -lc "sed -n '1,280p' docs/spec/entry.md" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
# Spec: Entry

- 상태: Draft
- 마지막 업데이트: 2026-04-12
- 관련 ADR: 000, 001, 002, 004
- 관련 spec: [`feed.md`](feed.md), [`resilience.md`](resilience.md)

이 문서는 `entries` 엔티티의 **현재 구현 정의**다. 정책 결정은 ADR에 있고,
이 spec은 테이블 스키마와 동작을 기술한다. Spec은 ADR 001의 불변식을
깨지 않는 한 자유롭게 갱신할 수 있다. **upsert 부하·body cap·initial
fetch 제한 등 fetch 단위 방어선은 [`resilience.md`](resilience.md)에**
정의되어 있다.

## 목적

피드에서 파싱한 개별 엔트리의 **최신 상태**를 저장한다. 엔트리는 변경
가능한 캐시이며 아카이브가 아니다(ADR 000·004). 편집은 정상 입력이다.

## 테이블 스키마

### `entries`

| 컬럼 | 타입 | NULL | 설명 |
|---|---|---|---|
| `id` | bigint (PK) | NOT NULL | 내부 서로게이트 키. **재삽입 시 변경될 수 있음** |
| `feed_id` | bigint (FK) | NOT NULL | `feeds.id` 참조 |
| `guid` | text | NOT NULL | 피드가 제공한 엔트리 식별자. **외부 안정 식별자** |
| `url` | text | NOT NULL | 엔트리 링크 |
| `title` | text | NULL | 엔트리 제목 |
| `content` | text | NULL | 본문 또는 요약 |
| `author` | text | NULL | 작성자 |
| `published_at` | timestamptz | NULL | 피드가 제공한 발행 시각. 정렬·조회용 |
| `fetched_at` | timestamptz | NOT NULL | **최초 저장 시각**. 보존 정책 기준 (ADR 004). **upsert로 갱신되지 않음** |
| `content_updated_at` | timestamptz | NOT NULL | 본문류 필드가 마지막으로 변경된 시각. 최초 저장 시 `= fetched_at` |

### 제약과 인덱스

- `UNIQUE (feed_id, guid)` — 피드 내 guid 중복 방지 (ADR 001)
- `FOREIGN KEY (feed_id) REFERENCES feeds(id) ON DELETE CASCADE` (ADR 002)
- `INDEX (fetched_at)` — 보존 스윕 (ADR 004)
- `INDEX (feed_id, published_at DESC, id DESC)` — 피드별 최신 엔트리
  keyset 조회 (ADR 002)
- `INDEX (feed_id, content_updated_at DESC)` — "최근 편집된 엔트리" 보조
  동기화용. 필요해지면 추가(선택).

## 왜 `fetched_at`과 `content_updated_at`을 분리하나

두 값의 의미가 다르다.

- `fetched_at` — **우리가 이 엔트리를 처음 저장한 시각.** 이후 upsert로
  변하지 않는다. 보존 정책(ADR 004)의 나이 기준이다. "우리가 가진 지
  얼마나 되었는가"를 나타낸다.
- `content_updated_at` — **본문/제목/url/author/published_at 중 하나라도
  실제로 변경된 마지막 시각.** 편집이 없으면 `fetched_at`과 같은 값으로
  유지된다.

이 분리가 없으면 편집 한 번이 엔트리의 "나이"를 리셋하게 되어, 오래된
엔트리가 사소한 편집으로 계속 살아남아 보존 정책이 무력화된다(ADR 001
불변식 #4).

## Upsert 정책

매 fetch마다 받은 각 엔트리에 대해 `(feed_id, guid)` 기준으로 upsert한다.

### 새 엔트리 (DB에 없음)

INSERT:

- 모든 파싱된 필드를 저장
- `fetched_at = now()`
- `content_updated_at = now()`

첫 fetch 시 엔트리 수가 많으면(예: OpenAI 909건) `FETCH_MAX_ENTRIES_INITIAL`
만큼만 상한해서 저장한다(spec/feed.md).

### 기존 엔트리 (DB에 있음)

비교 대상 필드: `title`, `content`, `url`, `author`, `published_at`.

- **하나라도 변경됨** → 해당 필드들을 UPDATE, `content_updated_at = now()`.
  `fetched_at`은 **건드리지 않는다**.
- **전부 동일** → 아무 컬럼도 UPDATE하지 않는다. no-op.

### 필드별 최신성 규칙

| 필드 | 소스 | 변경 가능성 | 동작 |
|---|---|---|---|
| `id` | 우리가 발급 | 재삽입 시 변경 | 외부 식별자로 쓰지 말 것 |
| `feed_id` | 우리가 발급 | 불변 | FK |
| `guid` | 피드 | 불변 (피드가 제공한 후로) | 안정 식별자 |
| `url` | 피드 | 변경 가능 | upsert로 반영 |
| `title` | 피드 | 변경 가능 | upsert로 반영 |
| `content` | 피드 | 변경 가능 | upsert로 반영 |
| `author` | 피드 | 변경 가능 | upsert로 반영 (드묾) |
| `published_at` | 피드 | 변경 가능 | upsert로 반영 |
| `fetched_at` | 우리가 발급 | 불변 (최초 저장 후) | 보존 기준, 건드리지 않음 |
| `content_updated_at` | 우리가 계산 | 본문 변경 시마다 갱신 | 최초 = `fetched_at` |

## API 응답

ADR 002에 따라 엔트리 응답은 `id`와 `guid`를 둘 다 포함한다.

응답 예시:

```json
{
  "id": 98765,
  "guid": "https://example.com/posts/hello-world",
  "feed_id": 42,
  "url": "https://example.com/posts/hello-world",
  "title": "Hello World",
  "content": "...",
  "author": "jane@example.com",
  "published_at": "2026-04-09T12:00:00Z",
  "fetched_at": "2026-04-09T12:05:30Z",
  "content_updated_at": "2026-04-10T08:22:00Z"
}
```

`content_updated_at > fetched_at`이면 그 엔트리는 **편집된 적이 있다**는
신호다.

## 엔트리가 피드에서 사라질 때

피드가 다음 fetch에서 이전에 있던 엔트리를 더 이상 반환하지 않는 경우:

- **우리는 삭제하지 않는다.** 그대로 보관한다.
- 시간이 흘러 보존 정책(ADR 004)에 따라 자연 소멸한다.
- Tombstone 없음(ADR 004).

**근거**: "피드 쪽에서 실수로 한 번 빠졌다가 다음 fetch에 다시 나타남"을
보호한다. 사라짐을 즉시 삭제로 확정하면 flap이 잦아진다.

## 재등장

시간이 지나 삭제된 엔트리가 피드에 다시 나타나면 (같은 guid):

- DB에서 그 엔트리는 이미 보존 정책으로 DELETE된 상태
- `(feed_id, guid)` UNIQUE 충돌 없음 (없으니까)
- 새 row로 INSERT됨 → 내부 `id`가 **바뀐다**
- `fetched_at`이 **재등장 시각**으로 새로 기록됨 → 보존 시계 재시작

이것이 ADR 001 불변식 #2("엔트리의 외부 안정 식별자는 `(feed_id, guid)`")
의 존재 이유다. 내부 `id`는 이 시점에 바뀌지만 `guid`는 그대로이므로, 외부
호출자는 "같은 엔트리"를 여전히 추적할 수 있다.

## 페이지네이션과의 상호작용

ADR 002의 keyset 정렬은 `(published_at DESC, id DESC)`. 편집으로
`published_at`이 변경되면 엔트리가 리스트 중간에서 앞/뒤로 점프할 수 있다.
→ **편집에 대해 best-effort**. ADR 002의 해당 섹션과 함께 읽을 것.

클라이언트 권장 동작:

- 같은 엔트리를 여러 페이지에서 중복 수신 가능성 → `guid`로 중복 제거
- 완전한 동기화가 필요하면 `content_updated_at` 기반 보조 스윕 구현
- "보지 못한 엔트리"를 절대 놓치지 않아야 한다면 이 서비스는 부적합. 캐시는
  최신성이 본질이고 그 대가로 "과거 시점 스냅샷 일관성"을 포기한다.

## 편집 감지 상세

비교는 **값 비교**다. 단, 다음은 현재 구현 단계의 선택 사항이다:

- `content`는 용량이 클 수 있으므로 해시 비교로 최적화할 수 있음 (선택).
  MVP는 직접 비교로 시작.
- HTML 차이 중 whitespace·attribute 순서 같은 **의미 없는 차이**를 무시
  할지: MVP는 그대로 비교 (엄격). 피드에 따라 false positive 발생 가능성
  있으나 용납 범위.
- `published_at`을 초 단위까지 비교할지 ms까지 할지: 초 단위 비교로 시작.

false positive(실제로 안 바뀌었는데 바뀐 것으로 감지)의 비용은 불필요한
`content_updated_at` 갱신뿐이므로 데이터 손상은 없다.

## 엣지케이스 체크리스트

### Mutation

- [x] 같은 guid, 제목만 수정 → upsert, `content_updated_at` 갱신
- [x] 같은 guid, 본문 통째로 수정 → upsert
- [x] 같은 guid, `published_at` 만 변경 → upsert, 정렬 순서 흔들림 (best-effort)
- [x] 같은 guid, author만 변경 → upsert
- [x] 같은 guid, 변경 없음 → no-op
- [x] 같은 피드에서 중복 guid (피드 버그) → 마지막 값 채택

### 생애

- [x] 엔트리 사라짐 → 무시, 보존 정책에 맡김
- [x] 엔트리 재등장 → 새 row, `fetched_at` 재시작, `guid`는 동일
- [x] 같은 내용을 새 guid로 재발행 (피드가 guid 재생성) → 별개 엔트리로
      취급됨. 방어 불가(피드 탓).

### 데이터 품질

- [x] `guid`가 전혀 없음 → **미해결** (아래 참조)
- [x] 비어있는 `title`/`content` → NULL 저장
- [x] 미래 날짜 `published_at` → 그대로 저장. 정렬에서 위로 올라감
- [x] 아주 과거 `published_at` → 그대로 저장
- [x] 상대 URL → feed base URL로 절대화 후 저장
- [x] 비 UTF-8 제목/본문 → 디코딩 후 저장

### 저장 규모

- [x] 첫 fetch에 909건 (OpenAI) → `FETCH_MAX_ENTRIES_INITIAL`로 상한
      (spec/feed.md)
- [x] 지속적으로 꾸준히 발행되는 피드 → 보존 정책이 한계 설정 (ADR 004)

## 실측 참고

seed 피드 probe 결과(`docs/notes/collection-scaling-research.md`):

- 20/20 피드가 모든 엔트리에 `guid` 제공 → "guid 없음" 케이스는 이 시드
  믹스에서는 발생 안 함. 하지만 일반 RSS 스펙상 optional이므로 폴백 필요.
- 20/20 피드가 모든 엔트리에 `published_at` 제공 → NULL 허용은 방어적
  유지.
- `guid`의 형태는 다양: URL(`guid == link`)도 있고, 불투명 문자열
  (Cloudflare `120kAbSMAaPQdCnfDgfd81`)도 있음. spec은 **opaque로 취급**
  하므로 형태에 의존하지 않음.
- 일부 피드는 `guid != link` (예: Meta Engineering의 `?p=23856`) →
  `guid`와 `url`은 별개 필드로 유지해야 함이 실측으로 확인됨.
- 피드당 엔트리 수는 9~909, 중간값 20. 하한 9는 `MIN_ENTRIES_PER_FEED`
  기본값 후보(ADR 004).

## 미해결

- **`guid`가 없는 엔트리의 폴백 정책.** 후보: `link`를 guid로 사용 / 엔트리
  본문 해시 사용 / 저장 거부. 실측에서는 발생하지 않았지만 정책은 필요.
- `content` 변경 감지를 해시로 최적화할지 여부 (성능 문제가 실제로 발생
  하면).
- `published_at` 파싱 실패 엔트리의 처리 (현재: NULL 저장).
- Enclosure(미디어 첨부), categories, image 등 추가 필드의 도입 여부.
  필요해지면 additive.
- 피드에서 사라진 엔트리를 API에서 "stale" 플래그로 구분할지 여부.
  현재는 구분 안 함.

exec
/bin/zsh -lc "sed -n '1,260p' docs/spec/api.md" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
# Spec: HTTP API

- 상태: Draft
- 마지막 업데이트: 2026-04-15
- 관련 ADR: 000, 002, 003, 004
- 관련 spec: feed.md, entry.md, resilience.md

이 문서는 외부 클라이언트가 보는 HTTP 표면(엔드포인트, 요청/응답, 에러, 페이지네이션)의 단일 기준 문서다. ADR 002의 계약을 엔드포인트 단위로 다시 서술하고, 엔티티 필드 의미는 [`feed.md`](feed.md)와 [`entry.md`](entry.md)를 참조한다. 자동 생성 레퍼런스는 API 컨테이너가 제공하는 FastAPI OpenAPI 문서(`/openapi.json`, `/docs`, `/redoc`)를 사용한다.

## 기본 정보

| 항목 | 값 |
| --- | --- |
| Base path prefix | `/v1/` |
| Content-Type (request) | `application/json` |
| Content-Type (response 2xx) | `application/json` |
| Content-Type (response 4xx/5xx) | `application/problem+json` |
| Authentication | 현재 없음 (후속 ADR에서 결정) |
| Versioning | URL prefix `/v1/`; breaking change는 `/v2/`로 분리 (ADR 002) |

현재 구현 컴포넌트 관계:

```text
+---------+      HTTP       +---------------------------+
| Client  |  ----------->   | FastAPI App               |
+---------+                 |                           |
                            |  /healthz  (health.py)   |
                            |  /v1/feeds (feeds.py)    |
                            |  /v1/entries (entries.py)|
                            +-------------+-------------+
                                          |
                                          | 예외 핸들러
                                          v
                                +---------------------+
                                | RFC 7807 Envelope   |
                                | (errors.py)         |
                                +---------------------+
                                          |
                                          | 응답 직렬화
                                          v
                                +---------------------+
                                | Pydantic Schemas    |
                                | (schemas.py)        |
                                +---------------------+
```

## 엔드포인트 목록

현재 코드(`src/feedgate/api/health.py`, `feeds.py`, `entries.py`) 기준 구현 라우트 전체:

| Method | Path | 설명 |
| --- | --- | --- |
| `GET` | `/healthz` | 프로세스 레벨 헬스체크 (`{"status":"ok"}`) |
| `POST` | `/v1/feeds` | 피드 등록 (중복 URL이면 기존 row 반환: 멱등) |
| `GET` | `/v1/feeds` | 피드 목록 조회 (keyset, 상태 필터 지원) |
| `GET` | `/v1/feeds/{feed_id}` | 단일 피드 조회 |
| `DELETE` | `/v1/feeds/{feed_id}` | 피드 해제 (존재 여부와 무관하게 204 멱등) |
| `POST` | `/v1/feeds/{feed_id}/reactivate` | 피드를 `active`로 수동 재활성화 |
| `GET` | `/v1/entries` | 엔트리 목록 조회 (feed_ids 필수, keyset) |

## 에러 응답 형식

에러 본문은 RFC 7807 Problem Details envelope(`application/problem+json`)를 사용한다. 구현 기준은 `src/feedgate/errors.py`이며, ADR 002의 "에러 응답 형식" 절이 계약의 기준 문서다. 모든 `HTTPException`/요청 검증 에러는 아래 구조로 직렬화된다.

```json
{
  "type": "about:blank",
  "title": "Bad Request",
  "status": 400,
  "detail": "blocked_url: unsupported scheme: 'file'",
  "instance": "/v1/feeds"
}
```

고정 키 의미:

| 키 | 의미 |
| --- | --- |
| `type` | 현재 구현 기본값 `about:blank` |
| `title` | HTTP 상태 문구 (`Bad Request`, `Not Found`, `Unprocessable Entity` 등) |
| `status` | HTTP status code |
| `detail` | 엔드포인트별 상세 에러 문자열 |
| `instance` | 요청 path (`request.url.path`) |

## 엔드포인트 상세

### `GET /healthz`

1. 목적: 런타임 liveness 확인용 최소 헬스체크를 제공한다.
2. 요청:
- Query: 없음
- Body: 없음
3. 응답:
- `200 OK`
- Body:

```json
{
  "status": "ok"
}
```

4. 에러:
- 명시적으로 생성하는 비즈니스 에러는 없음 (`health.py`에서 `HTTPException` 미사용)
- 비정상 런타임 오류 시 프레임워크 기본 5xx 가능
5. 관련 spec / ADR references:
- ADR 002 (헬스체크 엔드포인트 존재)

---

### `POST /v1/feeds`

1. 목적: URL을 정규화해 피드를 등록하고, 이미 등록된 URL이면 기존 리소스를 반환한다.
2. 요청:
- Query: 없음
- Body schema: [`FeedCreate`](../../src/feedgate/schemas.py)
- 본문 예시:

```json
{
  "url": "https://example.com/feed.xml"
}
```

- URL 처리 순서:

```text
raw url
  -> normalize_url(urlnorm.py)
  -> validate_public_url(resolve=False)
  -> INSERT ... ON CONFLICT(url) DO NOTHING
```

3. 응답:
- `201 Created`: 신규 등록
- `200 OK`: 동일 URL이 이미 존재(멱등 재등록)
- Body schema: [`FeedResponse`](../../src/feedgate/schemas.py)
- 필드 의미: [`feed.md`](feed.md)의 `feeds` API 노출 컬럼 정의 참조
4. 에러:
- `400 Bad Request`
- `detail="blocked_url: unsupported scheme: 'file'"` (예: `file://...`)
- `detail="blocked_url: missing host"`
- `detail="blocked_url: blocked address: 10.0.0.1"`
- `422 Unprocessable Entity`
- 요청 본문 검증 실패 (`url` 누락/빈 문자열 등)
- detail 패턴 예: `('body', 'url'): Field required`
5. 관련 spec / ADR references:
- ADR 002 `POST /v1/feeds` (멱등 등록)
- [`feed.md`](feed.md) (feed lifecycle 필드 의미)
- [`resilience.md`](resilience.md) (입력 URL/SSRF 방어 맥락)

---

### `GET /v1/feeds`

1. 목적: 등록된 피드 목록을 상태 필터와 keyset 페이지네이션으로 조회한다.
2. 요청:
- Query params:

| 이름 | 타입 | 필수 | 기본값 | 제약/동작 |
| --- | --- | --- | --- | --- |
| `cursor` | `string` | 아니오 | `null` | 이전 페이지의 `next_cursor` 전달 |
| `limit` | `int` | 아니오 | `50` | 코드 기본값 50. 런타임 상한 `api_feeds_max_limit`로 clamp |
| `status` | `active\|broken\|dead` | 아니오 | `null` | Feed 상태 필터 |

- `limit` 실값 계산:

```text
effective_limit = max(1, min(request.limit, app.state.api_feeds_max_limit))
```

- 상한 설정 소스:
- 기본 상한: `api_feeds_max_limit = 200` (`src/feedgate/config.py`)
- 환경변수: `FEEDGATE_API_FEEDS_MAX_LIMIT`
3. 응답:
- `200 OK`
- Body schema: [`PaginatedFeeds`](../../src/feedgate/schemas.py)
- `items[]` 원소 schema: [`FeedResponse`](../../src/feedgate/schemas.py)
- 필드 의미: [`feed.md`](feed.md) 참조
4. 에러:
- `400 Bad Request`
- `detail="invalid cursor"` (base64/json 파싱 실패 또는 payload 키 오류)
- `422 Unprocessable Entity`
- `status` enum 외 값 (`active|broken|dead` 외)
- `limit` 타입 검증 실패(숫자 아님 등)
5. 관련 spec / ADR references:
- ADR 002 `GET /v1/feeds`
- [`feed.md`](feed.md) (status 의미)

---

### `GET /v1/feeds/{feed_id}`

1. 목적: 단일 feed를 ID로 조회한다.
2. 요청:
- Path params:

| 이름 | 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| `feed_id` | `int` | 예 | 조회 대상 feed ID |

- Query: 없음
- Body: 없음
3. 응답:
- `200 OK`
- Body schema: [`FeedResponse`](../../src/feedgate/schemas.py)
- 필드 의미: [`feed.md`](feed.md) 참조
4. 에러:
- `404 Not Found`
- `detail="feed not found"`
- `422 Unprocessable Entity`
- path 파라미터 타입 검증 실패 (`feed_id`가 정수 아님)
5. 관련 spec / ADR references:
- ADR 002 `GET /v1/feeds/{id}`
- [`feed.md`](feed.md)

---

### `DELETE /v1/feeds/{feed_id}`

1. 목적: feed를 해제(삭제)한다. 존재하지 않아도 성공(멱등) 처리한다.
2. 요청:
- Path params:

| 이름 | 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| `feed_id` | `int` | 예 | 삭제 대상 feed ID |

- Query: 없음
- Body: 없음
3. 응답:
- `204 No Content`
- Body: 없음
- DB 레벨에서 `entries.feed_id -> feeds.id`가 `ON DELETE CASCADE`라 연관 entry도 함께 삭제됨
4. 에러:
- `422 Unprocessable Entity`
- path 파라미터 타입 검증 실패 (`feed_id`가 정수 아님)
- 주의: 존재하지 않는 `feed_id`는 에러가 아니라 `204` 응답
5. 관련 spec / ADR references:
- ADR 002 `DELETE /v1/feeds/{id}` (멱등/cascade)
- [`entry.md`](entry.md) FK/ON DELETE CASCADE

---

### `POST /v1/feeds/{feed_id}/reactivate`

1. 목적: feed 상태를 수동으로 `active`로 되돌려 다음 tick에 즉시 재시도되게 한다.
2. 요청:
- Path params:

| 이름 | 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| `feed_id` | `int` | 예 | 재활성화 대상 feed ID |

- Query: 없음
- Body: 없음
3. 응답:
- `200 OK`
- Body schema: [`FeedResponse`](../../src/feedgate/schemas.py)
- 서버 내부 상태 변경:

exec
/bin/zsh -lc "sed -n '261,520p' docs/spec/api.md" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:

```text
status               -> active
consecutive_failures -> 0
last_error_code      -> null
next_fetch_at        -> now(UTC)
```

- `last_successful_fetch_at`는 즉시 변경하지 않음(실제 fetch 성공 시 갱신)
4. 에러:
- `404 Not Found`
- `detail="feed not found"`
- `422 Unprocessable Entity`
- path 파라미터 타입 검증 실패 (`feed_id`가 정수 아님)
5. 관련 spec / ADR references:
- ADR 002 `POST /v1/feeds/{id}/reactivate`
- [`feed.md`](feed.md) (상태 머신/재활성화 규칙)

---

### `GET /v1/entries`

1. 목적: 지정한 feed 집합의 엔트리를 keyset으로 페이지 조회한다.
2. 요청:
- Query params:

| 이름 | 타입 | 필수 | 기본값 | 제약/동작 |
| --- | --- | --- | --- | --- |
| `feed_ids` | `string` (CSV) | 예 | 없음 | 쉼표 구분 정수 ID 목록. 전역 스캔 금지 |
| `cursor` | `string` | 아니오 | `null` | opaque keyset cursor |
| `limit` | `int` | 아니오 | `api_entries_default_limit` | `ge=1`, `api_entries_max_limit` 초과 시 422 |

- 런타임 설정값(`src/feedgate/config.py`):

| 설정 키 | 기본값 | 환경변수 |
| --- | --- | --- |
| `api_entries_max_feed_ids` | `200` | `FEEDGATE_API_ENTRIES_MAX_FEED_IDS` |
| `api_entries_default_limit` | `50` | `FEEDGATE_API_ENTRIES_DEFAULT_LIMIT` |
| `api_entries_max_limit` | `200` | `FEEDGATE_API_ENTRIES_MAX_LIMIT` |

- 커서 조건(정렬 `published_at DESC, id DESC`):

```text
cursor 없음: 첫 페이지
cursor 있음: "cursor 이후(after)" 구간만 조회
```

3. 응답:
- `200 OK`
- Body schema: [`PaginatedEntries`](../../src/feedgate/schemas.py)
- `items[]` 원소 schema: [`EntryResponse`](../../src/feedgate/schemas.py)
- 필드 의미: [`entry.md`](entry.md) 참조
4. 에러:
- `400 Bad Request`
- `detail="invalid cursor"`
- `detail="invalid feed_ids"` (CSV 원소를 int로 변환 실패)
- `detail="feed_ids is required"` (빈 문자열 등으로 실질 입력 없음)
- `detail="feed_ids length exceeds 200"` (초과 시; 숫자는 런타임 설정값 반영)
- `422 Unprocessable Entity`
- `detail="limit must be less than or equal to 200"` (초과 시; 숫자는 런타임 설정값 반영)
- Query 검증 실패 (`limit < 1`, 타입 오류 등)
5. 관련 spec / ADR references:
- ADR 002 `GET /v1/entries`
- [`entry.md`](entry.md) (엔트리 식별자/편집/정렬 영향)

## 페이지네이션

`GET /v1/feeds`, `GET /v1/entries`는 둘 다 keyset cursor를 사용한다.

공통 규칙:

- Cursor는 불투명 문자열(opaque)이다.
- 현재 구현은 `base64(urlsafe, no padding)`로 JSON payload를 인코딩하지만, 클라이언트는 내부 구조를 해석하거나 의존하면 안 된다.
- 마지막 페이지에서는 `next_cursor = null`.

엔드포인트별 정렬 키:

| 엔드포인트 | 정렬 | 커서 키 |
| --- | --- | --- |
| `GET /v1/feeds` | `id ASC` | `i`(마지막 feed id) |
| `GET /v1/entries` | `published_at DESC, id DESC` | `p`(published_at), `i`(id) |

ASCII 흐름:

```text
요청 1 (cursor 없음)
  -> items[0..N-1], next_cursor="abc"

요청 2 (cursor="abc")
  -> items[N..2N-1], next_cursor="def"

요청 k (마지막)
  -> items[...], next_cursor=null
```

동시 편집/업서트 하에서의 일관성 한계 (ADR 002):

- best-effort 페이지네이션이다.
- 동시 변경 중 같은 레코드가 중복 노출되거나 일부 구간이 건너뛰어 보일 수 있다.
- 클라이언트 보정 규칙:
- entries: `guid` 기준 dedupe 필수
- feeds: `id` 기준 dedupe 권장

## Rate Limits

현재 HTTP 레이어 rate limit은 구현되지 않았다. 따라서 rate-limit 관련 응답 헤더도 오늘(2026-04-15) 기준으로는 내려가지 않는다. 도입 시점에는 [`resilience.md`](resilience.md)와 별도 ADR에서 정책/수치/헤더 계약을 확정한다.

## OpenAPI

API 컨테이너 실행 시 FastAPI가 기계 판독용 OpenAPI JSON을 `/openapi.json`에서 제공하고, 대화형 문서는 Swagger UI(`/docs`)와 ReDoc(`/redoc`)에서 제공한다. 이 문서는 사람 중심 설명(spec)이고, OpenAPI는 기계/도구 중심 계약이다.

## 미해결

- 인증/접근 제어 (후속 ADR)
- Rate limit 구체 수치
- 메트릭 엔드포인트 경로/포맷
- 편집된 엔트리의 보조 동기화 파라미터 필요 여부


exec
/bin/zsh -lc "sed -n '1,280p' docs/spec/resilience.md" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
# Spec: Resilience & Load Mitigation

- 상태: Draft v1
- 마지막 업데이트: 2026-04-12
- 관련 ADR: 000, 001, 002, 003
- 관련 spec: [`feed.md`](feed.md), [`entry.md`](entry.md)
- 관련 tests: [`../tests/resilience-test-cases.md`](../tests/resilience-test-cases.md) — 모든 위협 ID에 대한 TC 매핑 카탈로그

이 문서는 feedgate-fetcher가 **악의적/병적 입력, upstream 장애, 인프라 사고**에
직면했을 때 어떤 방어선을 어떤 순서로 작동시키는지 — 그리고 그 결과가
머메이드 다이어그램으로 어떻게 닫힌 흐름을 이루는지 — 를 정의한다.

핵심 원칙: **링크 부식과 외부 장애는 정상 입력**(ADR 000). 우리는 모든
실패 케이스가 자동 복구 가능하거나, 적어도 다른 피드의 처리를 막지 않도록
설계해야 한다.

## 1. 위협 모델 (Threat Catalog)

위협은 4개 카테고리로 묶는다.

| ID | 위협 | 영향 | 발생 가능성 |
|---|---|---|---|
| **A. 단일 피드 → 우리 워커 폭격** |
| A1 | 5GB 이상 응답 본문 (메모리 폭발) | OOM kill | 중 |
| A2 | 압축 폭탄 (5MB → 5GB 해제) | OOM kill | 낮음 |
| A3 | Slow Loris (1바이트/19초) | 워커 stall | 낮음 |
| A4 | 첫 fetch에 1만 entries | DB write storm | 중 |
| A5 | 무한 redirect chain | 워커 stall | 낮음 |
| A6 | 무효 TLS 인증서 | 일시 실패 | 높음 |
| A7 | 매 fetch마다 새 guid 100개 | DB INSERT 폭주 | 중 |
| A8 | 영구 4xx (Cloudflare WAF 차단) | 무한 재시도 | 높음 |
| **B. 다수 피드 → 동시 장애** |
| B1 | CDN 장애로 50개 피드 동시 timeout | 워커 saturate | 중 |
| B2 | broken 피드 100개 동시 backoff cap 해제 (thundering herd) | upstream 폭격 | 중 |
| B3 | 같은 호스트에 100개 피드 등록 → 100 동시 GET | upstream IP 차단 | 높음 |
| B4 | upstream 429 폭탄 | 의미 없는 재시도 | 높음 |
| **C. 우리 인프라 한계** |
| C1 | Postgres 커넥션 풀 고갈 | 신규 fetch 실패 | 중 |
| C2 | retention sweep이 1시간 초과 | 디스크 폭발 | 낮음 (장기) |
| C3 | tick_once 예외 propagate | 스케줄러 죽음 | 낮음 |
| C4 | 워커 OOM kill (in-flight fetch 손실) | 일부 fetch 누락 | 중 |
| C5 | rolling deploy SIGTERM (in-flight fetch 강제 종료) | 매 deploy마다 손실 | 높음 |
| C6 | DB 일시 단절 (네트워크 partition) | 에러 로그 폭주 | 낮음 |
| **D. 악의적 / SSRF** |
| D1 | `POST /v1/feeds {url:"http://169.254.169.254/..."}` (AWS metadata) | 메타데이터 leak | **높음 (보안)** |
| D2 | `http://127.0.0.1:5432/...` (DB 포트 정찰) | 정찰 leak | 중 |
| D3 | `file:///etc/passwd` | 로컬 파일 leak | 낮음 (httpx 미지원) |
| D4 | redirect가 내부 IP로 점프 | SSRF 우회 | 중 |
| D5 | 1초당 1000회 `POST /v1/feeds` (등록 폭주) | API saturate | 중 |
| D6 | 같은 URL 멱등 등록 N회 | 무영향 (idempotent) | 낮음 |

## 2. 방어 레이어 모델 (3-Tier)

모든 위협은 3단계 방어선 중 하나 이상으로 흡수된다.

```mermaid
flowchart LR
    subgraph T1["Tier 1 — Prevent (입구 차단)"]
        P1[URL 정규화]
        P2[SSRF 호스트 검증]
        P3[Content-Type 화이트리스트 거부]
        P4[Per-host throttle]
        P5[API rate limit]
    end
    subgraph T2["Tier 2 — Contain (피해 격리)"]
        C1[fetch_concurrency 세마포어]
        C2[Body size cap 5MB]
        C3[fetch_timeout 20s + wall-clock 30s]
        C4[max_entries_initial 50]
        C5[per-feed 세션 격리]
        C6[SKIP LOCKED claim 격리]
    end
    subgraph T3["Tier 3 — Recover (자동 복구)"]
        R1[State machine: active→broken→dead]
        R2[Exponential backoff + jitter]
        R3[429 Retry-After 존중]
        R4[Lease TTL 자동 재배정]
        R5[ON CONFLICT no-op]
        R6[Weekly dead probe]
        R7[Manual reactivate API]
    end
    T1 --> T2 --> T3
```

| 위협 | Tier 1 (Prevent) | Tier 2 (Contain) | Tier 3 (Recover) | 상태 |
|---|---|---|---|---|
| A1 거대 응답 | — | Body cap 5MB streaming | — | ✅ |
| A2 압축 폭탄 | — | Body cap on **decompressed bytes** | — | ✅ |
| A3 Slow Loris | — | wall-clock 30s + read 10s | — | ❌ wall-clock 미구현 |
| A4 첫 fetch 1만 entries | — | max_entries_initial=50 | — | ✅ |
| A5 redirect loop | — | redirect cap (httpx 기본 20) | — | ⚠️ 명시적 설정 없음 |
| A6 TLS 무효 | — | TLS verify=True | broken → dead | ✅ |
| A7 매번 새 guid 100개 | — | (cap 없음) | ON CONFLICT 무관 | ⚠️ 누적 부담 |
| A8 영구 4xx | — | — | broken → dead 후 주 1회 | ✅ |
| B1 동시 timeout | — | fetch_concurrency=4 | 다음 tick 정상 | ⚠️ stall 시간 |
| B2 thundering herd | — | — | ±25% jitter | ✅ |
| B3 동일 호스트 폭격 | per-host throttle | — | 429 처리 | ❌ 미구현 |
| B4 429 폭탄 | per-host throttle | — | Retry-After 존중 | ✅ Retry-After만 |
| C1 Postgres pool 고갈 | — | pool_size 튜닝 | — | ⚠️ 기본값 |
| C2 retention 지연 | — | cursor batch | — | ⚠️ 미구현 |
| C3 tick 예외 | — | run() try/except | — | ✅ |
| C4 워커 OOM | — | — | Lease TTL 재배정 | ✅ |
| C5 rolling deploy | — | graceful drain | Lease TTL 재배정 | ❌ drain 미구현 |
| C6 DB 단절 | — | — | next tick 재시도 | ⚠️ 로그 폭주 |
| D1 AWS metadata SSRF | **호스트 IP 검증** | — | — | ❌ 미구현 |
| D2 DB 포트 정찰 | **호스트 IP 검증** | not_a_feed | — | ❌ 미구현 |
| D3 file:// | scheme 화이트리스트 | — | — | ✅ |
| D4 redirect SSRF | follow_redirects 수동 검증 | — | — | ❌ 미구현 |
| D5 API 폭주 | API rate limit | — | — | ❌ 미구현 |
| D6 멱등 POST | — | — | upsert idempotent | ✅ |

**범례**: ✅ 구현됨 / ⚠️ 부분 / ❌ 미구현

## 3. 시나리오별 방어 로직 (Mermaid)

### 3.1 피드 등록 — `POST /v1/feeds`

```mermaid
flowchart TD
    Start([POST /v1/feeds<br/>body url]) --> A{스킴이<br/>http or https?}
    A -->|아니오 file/ftp/...| Z1[400 Bad Request<br/>unsupported scheme]
    A -->|예| B[urlnorm.normalize_url]
    B --> C[hostname 추출]
    C --> D[DNS 조회<br/>모든 A/AAAA 레코드]
    D --> E{모든 IP가<br/>public인가?}
    E -->|아니오 사설망/loopback/<br/>link-local/AWS metadata| Z2[400 Bad Request<br/>private address blocked]
    E -->|예| F{기존 row<br/>존재?}
    F -->|예| G[200 OK<br/>기존 row 반환<br/>멱등]
    F -->|아니오| H[INSERT feed<br/>next_fetch_at = now<br/>status = active]
    H --> I[201 Created]

    style A fill:#fef,stroke:#c0c
    style E fill:#fee,stroke:#c00
    style Z1 fill:#fee
    style Z2 fill:#fee
```

**방어 포인트:**
- `A` 스킴 검증 → D3 `file://` 차단
- `E` SSRF IP 검증 → D1, D2 차단 (구현 예정)
- `F` 멱등 → D6 폭주 무력화

### 3.2 단일 fetch — `fetch_one` 전체 흐름

이 다이어그램은 한 피드의 1회 fetch에서 적용되는 모든 방어선을 한 번에 표시한다.

```mermaid
flowchart TD
    Start([fetch_one 진입]) --> A[last_attempt_at = now]
    A --> AA{호스트 throttle<br/>슬롯 가능?}
    AA -->|RPS 초과| AB[await throttle.acquire<br/>최대 N초]
    AB --> B
    AA -->|OK| B[조건부 헤더 부착]
    B --> BA{feed.etag 있음?}
    BA -->|예| BB[If-None-Match 추가]
    BA -->|아니오| BC{feed.last_modified<br/>있음?}
    BB --> BC
    BC -->|예| BD[If-Modified-Since 추가]
    BC -->|아니오| C
    BD --> C[asyncio.timeout 30s 래퍼<br/>+ httpx connect 5s / read 10s]
    C --> D[HTTP GET stream]
    D --> E{follow_redirects}
    E -->|301/302/307/308| EA{redirect 호스트가<br/>여전히 public인가?}
    EA -->|아니오| EB[redirect_blocked<br/>fail++]
    EA -->|예| D
    E -->|aborted: connect| F1[connection]
    E -->|aborted: read timeout| F2[timeout]
    E -->|aborted: tls| F3[tls_error]
    E -->|response received| G{status_code}

    G -->|304| H1[Not Modified<br/>본문 안 받음<br/>SUCCESS path]
    G -->|410| H2[http_410<br/>즉시 dead 전이]
    G -->|429| H3[Retry-After 파싱<br/>RFC 7231 sec or HTTP-date]
    H3 --> H3A[rate_limited<br/>fail 카운터 증가 안 함<br/>next_fetch_at = now+max retry,base]
    G -->|4xx other| F4[http_4xx]
    G -->|5xx| F5[http_5xx]
    G -->|200| I{Content-Type<br/>화이트리스트}
    I -->|html/json/text| F6[not_a_feed]
    I -->|xml/atom/rss/...| J[stream body]

    J --> K{누적 size > 5MB?}
    K -->|예| F7[too_large<br/>스트림 abort]
    K -->|아니오| L{wall-clock 30s<br/>초과?}
    L -->|예| F2
    L -->|아니오| M{본문 끝?}
    M -->|아니오| J
    M -->|예| N[parse_feed]

    N -->|예외| F8[parse_error]
    N -->|성공| O{첫 fetch이고<br/>entries > 50?}
    O -->|예| O1[entries[:50] truncate]
    O -->|아니오| O2
    O1 --> O2[upsert_entries 루프]
    O2 --> P[etag/last_modified 저장<br/>title 갱신<br/>last_successful_fetch_at = now<br/>fail = 0<br/>code = null]
    P --> Q{현재 status<br/>!= active?}
    Q -->|예| Q1[active 전이<br/>+ WARNING 로그]
    Q -->|아니오| R
    Q1 --> R[next_fetch_at 재계산<br/>active: now+60s<br/>broken: backoff+jitter]
    H1 --> P

    F1 & F2 & F3 & F4 & F5 & F6 & F7 & F8 & EB --> S[fail += 1<br/>last_error_code 기록]
    S --> T{fail >= 3?}
    T -->|예 + active| T1[broken 전이]
    T -->|아니오| U
    T1 --> U{broken 상태이고<br/>last_success 7일 경과?}
    U -->|예| U1[dead 전이]
    U -->|아니오| R
    U1 --> R
    H2 --> R

    R --> End([fetch_one 종료])
    H3A --> End

    style AA fill:#fef,stroke:#c0c
    style EA fill:#fee,stroke:#c00
    style I fill:#ffe,stroke:#cc0
    style K fill:#ffe,stroke:#cc0
    style L fill:#ffe,stroke:#cc0
    style C fill:#ffe,stroke:#cc0
```

**방어 포인트 (위협 ID 매핑):**

| 노드 | 방어 위협 | 비고 |
|---|---|---|
| `AA` per-host throttle | B3, B4 | ❌ 미구현 |
| `BA/BC` 조건부 헤더 | A7 (간접) | ⚠️ DB 컬럼만 있음 |
| `C` wall-clock + 세분 timeout | A3, A5, B1 | ⚠️ wall-clock 미구현 |
| `EA` redirect SSRF 재검증 | D4 | ❌ 미구현 |
| `I` Content-Type 화이트리스트 | A1 (HTML 폭격), D2 | ✅ |
| `K` body cap 5MB | A1, A2 | ✅ |
| `O` initial entries cap | A4 | ✅ |
| `H3` Retry-After (sec + HTTP-date) | B4 | ✅ (PR #7) |
| `T/U` lifecycle 전이 | A8 | ✅ |
| `H2` 즉시 dead | A8 (410) | ✅ |

### 3.3 스케줄러 tick — claim & dispatch

```mermaid
sequenceDiagram
    autonumber
    participant W1 as Worker A
    participant W2 as Worker B
    participant DB as Postgres feeds

    rect rgb(245, 235, 255)
    Note over W1,W2: 두 워커가 동시에 tick 시작
    W1->>DB: BEGIN; SELECT FOR UPDATE SKIP LOCKED LIMIT 8
    activate W1
    W2->>DB: BEGIN; SELECT FOR UPDATE SKIP LOCKED LIMIT 8
    activate W2
    DB-->>W1: feeds [1..8] (W1 lock)
    DB-->>W2: feeds [9..16] (W1 행 skip)
    W1->>DB: UPDATE next_fetch_at=now+180s, last_attempt_at=now
    W2->>DB: UPDATE next_fetch_at=now+180s, last_attempt_at=now
    W1->>DB: COMMIT (lock 해제, lease persist)
    deactivate W1
    W2->>DB: COMMIT (lock 해제, lease persist)
    deactivate W2
    end

    Note over W1,W2: claim 끝, fetch는 새 세션에서

    par 워커 A 병렬 fetch
        W1->>W1: fetch feeds 1..8 (HTTP, semaphore=4)
        W1->>DB: 각 feed 결과 update (real next_fetch_at)
    and 워커 B 병렬 fetch
        W2->>W2: fetch feeds 9..16
        W2->>DB: 각 feed 결과 update
    end

    Note over W1,W2: 만약 W1이 fetch 중 OOM kill되면?

    rect rgb(255, 235, 235)
    W1-->>W1: 💥 OOM kill 도중 종료
    Note over DB: feeds 1..8의<br/>next_fetch_at = now+180s 그대로
    Note over W1,W2: 180초 동안은 어떤 워커도 못 집음
    end

    Note over W2: 180초 후, 다음 tick

codex
스펙 문서까지 반영해서 실제 코드와의 일치 여부를 보려면 마이그레이션과 테스트가 중요해서, 다음으로 Alembic 이력과 테스트 설계를 확인하겠습니다.
exec
/bin/zsh -lc "sed -n '1,220p' alembic/versions/0002_claim_index_coverage.py" in /Users/kangheeyong/project/feedgate-fetcher
exec
/bin/zsh -lc "sed -n '1,260p' alembic/versions/0001_initial_schema.py" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
"""align feed claim indexes with scheduler predicates

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-12

Replaces the active-only next_fetch_at partial index with two partial
indexes that match _claim_due_feeds paths:
- status <> 'dead' and next_fetch_at <= now (due path)
- status = 'dead' and last_attempt_at probe filter (dead-probe path)
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_feeds_next_fetch_at_active", table_name="feeds")
    op.create_index(
        "ix_feeds_due_not_dead",
        "feeds",
        ["next_fetch_at"],
        # Matches due path predicate: status != 'dead'.
        postgresql_where=sa.text("status <> 'dead'"),
    )
    op.create_index(
        "ix_feeds_dead_last_attempt",
        "feeds",
        ["last_attempt_at"],
        # Matches dead-probe path predicate: status = 'dead'.
        postgresql_where=sa.text("status = 'dead'"),
    )


def downgrade() -> None:
    op.drop_index("ix_feeds_dead_last_attempt", table_name="feeds")
    op.drop_index("ix_feeds_due_not_dead", table_name="feeds")
    op.create_index(
        "ix_feeds_next_fetch_at_active",
        "feeds",
        ["next_fetch_at"],
        postgresql_where=sa.text("status = 'active'"),
    )

 succeeded in 0ms:
"""initial schema: feeds and entries

Revision ID: 0001
Revises:
Create Date: 2026-04-10

Creates the core data model defined in ADR 001, docs/spec/feed.md, and
docs/spec/entry.md. Includes all columns (API-exposed lifecycle fields
AND internal scheduler metadata), unique constraint on (feed_id, guid),
retention-sweep index on entries.fetched_at, and the compound keyset
index (feed_id, published_at DESC, id DESC) for GET /v1/entries.

Hand-written rather than autogenerated so it works without a live DB at
generation time.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "feeds",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("effective_url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default="active",
            nullable=False,
        ),
        sa.Column(
            "last_successful_fetch_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "last_attempt_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "next_fetch_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("etag", sa.Text(), nullable=True),
        sa.Column("last_modified", sa.Text(), nullable=True),
        sa.Column(
            "consecutive_failures",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("url"),
    )
    op.create_index("ix_feeds_status", "feeds", ["status"])
    op.create_index(
        "ix_feeds_next_fetch_at_active",
        "feeds",
        ["next_fetch_at"],
        postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "entries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("feed_id", sa.Integer(), nullable=False),
        sa.Column("guid", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("author", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "content_updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["feed_id"],
            ["feeds.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("feed_id", "guid", name="uq_entries_feed_guid"),
    )
    op.create_index("ix_entries_fetched_at", "entries", ["fetched_at"])
    # Compound keyset index with DESC ordering for (published_at, id).
    op.create_index(
        "ix_entries_feed_pub_id",
        "entries",
        [
            sa.text("feed_id"),
            sa.text("published_at DESC"),
            sa.text("id DESC"),
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_entries_feed_pub_id", table_name="entries")
    op.drop_index("ix_entries_fetched_at", table_name="entries")
    op.drop_table("entries")
    op.drop_index("ix_feeds_next_fetch_at_active", table_name="feeds")
    op.drop_index("ix_feeds_status", table_name="feeds")
    op.drop_table("feeds")

exec
/bin/zsh -lc "sed -n '1,220p' alembic/versions/0003_retention_index.py" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
"""add retention sweep sort index on entries

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-12

Adds a compound index that matches the retention sweep per-feed window
sort key: (feed_id, fetched_at DESC, id DESC).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_entries_feed_fetched_id",
        "entries",
        [
            sa.text("feed_id"),
            sa.text("fetched_at DESC"),
            sa.text("id DESC"),
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_entries_feed_fetched_id", table_name="entries")

exec
/bin/zsh -lc "sed -n '1,260p' alembic/versions/0004_drop_dead_etag_columns.py" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
"""drop dead etag columns from feeds

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-12

Removes feeds.etag and feeds.last_modified. They were reserved for the
planned ETag/If-Modified-Since feature, but no code path currently
reads/writes them.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("feeds", "etag")
    op.drop_column("feeds", "last_modified")


def downgrade() -> None:
    op.add_column("feeds", sa.Column("etag", sa.Text(), nullable=True))
    op.add_column("feeds", sa.Column("last_modified", sa.Text(), nullable=True))

exec
/bin/zsh -lc "sed -n '1,220p' alembic/versions/0005_etag_last_modified.py" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
"""add etag and last_modified to feeds

Revision ID: 0005
Revises: 0004
Create Date: 2026-04-23

Adds feeds.etag and feeds.last_modified to support HTTP conditional
requests (ETag/If-None-Match and Last-Modified/If-Modified-Since).
Values are stored after each successful 200 response and sent on the
next fetch to enable 304 Not Modified short-circuiting.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("feeds", sa.Column("etag", sa.Text(), nullable=True))
    op.add_column("feeds", sa.Column("last_modified", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("feeds", "last_modified")
    op.drop_column("feeds", "etag")

exec
/bin/zsh -lc "sed -n '1,260p' tests/conftest.py" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
"""Shared pytest fixtures for feedgate-fetcher tests.

- `pg_container` — a Postgres 16 container booted once per test session
- `database_url` — an asyncpg-flavored connection string for the container
- `async_engine` — an `AsyncEngine` over `database_url` (session scope)
- `async_session_factory` — an `async_sessionmaker[AsyncSession]` (session scope)
- `async_session` — a per-test `AsyncSession` wrapped in a SAVEPOINT
  (isolation via rollback)

The `respx_mock` fixture is provided automatically by the `respx` package.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)
from testcontainers.postgres import PostgresContainer

from feedgate.api import register_routers
from feedgate.config import Settings
from feedgate.db import make_engine, make_session_factory

REPO_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


@pytest.fixture(scope="session")
def pg_container() -> Iterator[PostgresContainer]:
    """Boot a Postgres 16 container once per test session."""
    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg


@pytest.fixture(scope="session")
def database_url(pg_container: PostgresContainer) -> str:
    """asyncpg-flavored connection URL for the session's Postgres."""
    raw: str = str(pg_container.get_connection_url())
    # testcontainers gives us a psycopg-flavored URL; rewrite for asyncpg.
    return raw.replace("postgresql+psycopg2://", "postgresql+asyncpg://").replace(
        "postgresql://", "postgresql+asyncpg://"
    )


@pytest.fixture(scope="session")
def apply_migrations(database_url: str) -> str:
    """Run Alembic `upgrade head` once per session.

    Returns the database URL so downstream fixtures can depend on this
    fixture to ensure migrations have been applied before they touch
    the schema.
    """
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(cfg, "head")
    return database_url


@pytest_asyncio.fixture(scope="session")
async def async_engine(apply_migrations: str) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(apply_migrations)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(scope="session")
async def async_session_factory(
    async_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return make_session_factory(async_engine)


@pytest_asyncio.fixture
async def async_session(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Per-test session. Rolls back at the end for isolation."""
    async with async_session_factory() as session:
        try:
            yield session
        finally:
            await session.rollback()


@pytest_asyncio.fixture
async def truncate_tables(async_engine: AsyncEngine) -> None:
    """Wipe feeds and entries between API tests.

    The rollback-per-test isolation used by ``async_session`` does not
    work for API tests because FastAPI opens its own per-request
    session via the ``get_session`` dependency and commits it. So for
    any test that hits the HTTP app, truncate before running.
    """
    async with async_engine.begin() as conn:
        await conn.execute(text("TRUNCATE TABLE feeds, entries RESTART IDENTITY CASCADE"))


@pytest_asyncio.fixture
async def api_app(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> FastAPI:
    """A FastAPI app wired to the test DB session factory.

    Phase 5 ``main.create_app`` will build a similar object but also
    wire in a lifespan + scheduler. For Phase 3 tests we just need the
    routers and the session dependency; no background task.
    """
    settings = Settings()
    app = FastAPI()
    app.state.session_factory = async_session_factory
    app.state.api_key = ""  # no auth in tests by default
    app.state.api_entries_max_feed_ids = settings.api_entries_max_feed_ids
    app.state.api_entries_default_limit = settings.api_entries_default_limit
    app.state.api_entries_max_limit = settings.api_entries_max_limit
    app.state.api_feeds_max_limit = settings.api_feeds_max_limit
    register_routers(app)
    return app


@pytest_asyncio.fixture
async def api_client(
    api_app: FastAPI,
    truncate_tables: None,
) -> AsyncIterator[AsyncClient]:
    """An httpx AsyncClient over ASGITransport into the test app."""
    async with AsyncClient(
        transport=ASGITransport(app=api_app),
        base_url="http://test",
    ) as client:
        yield client


@pytest_asyncio.fixture
async def fetch_app(
    async_session_factory: async_sessionmaker[AsyncSession],
    truncate_tables: None,
) -> AsyncIterator[FastAPI]:
    """A FastAPI app populated with state that ``scheduler.tick_once`` reads.

    Used by Phase 4 fetcher/scheduler tests. Owns a real
    ``httpx.AsyncClient`` — ``respx_mock`` patches its transport so no
    real HTTP happens. The scheduler itself is NOT started; tests call
    ``tick_once`` directly.
    """
    settings = Settings()
    app = FastAPI()
    app.state.session_factory = async_session_factory
    app.state.api_key = ""  # no auth in tests by default
    app.state.http_client = AsyncClient()
    app.state.fetch_interval_seconds = 60
    app.state.fetch_user_agent = "feedgate-fetcher/test"
    app.state.fetch_concurrency = settings.fetch_concurrency
    app.state.fetch_per_host_concurrency = settings.fetch_per_host_concurrency
    app.state.fetch_claim_batch_size = settings.fetch_claim_batch_size
    app.state.fetch_claim_ttl_seconds = settings.fetch_claim_ttl_seconds
    app.state.fetch_max_bytes = settings.fetch_max_bytes
    app.state.fetch_max_entries_initial = settings.fetch_max_entries_initial
    app.state.fetch_total_budget_seconds = settings.fetch_total_budget_seconds
    app.state.broken_threshold = settings.broken_threshold
    app.state.dead_duration_days = settings.dead_duration_days
    app.state.broken_max_backoff_seconds = settings.broken_max_backoff_seconds
    app.state.backoff_jitter_ratio = settings.backoff_jitter_ratio
    app.state.dead_probe_interval_days = settings.dead_probe_interval_days
    app.state.entry_frequency_min_interval_seconds = settings.entry_frequency_min_interval_seconds
    app.state.entry_frequency_max_interval_seconds = settings.entry_frequency_max_interval_seconds
    app.state.entry_frequency_factor = settings.entry_frequency_factor
    try:
        yield app
    finally:
        await app.state.http_client.aclose()

codex
테스트는 양이 많아서 먼저 테스트 이름을 전수로 뽑아 기능 커버리지를 맵핑한 뒤, 큰 파일(`test_fetch_one.py`, `test_scheduler_tick.py`)은 핵심 시나리오만 읽겠습니다.
exec
/bin/zsh -lc 'rg "''^async def test_|''^def test_" tests -n' in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
tests/test_migrations.py:16:async def test_tables_exist_after_migration(async_engine: AsyncEngine) -> None:
tests/test_migrations.py:30:async def test_required_indexes_exist(async_engine: AsyncEngine) -> None:
tests/test_migrations.py:48:async def test_entries_unique_constraint(async_engine: AsyncEngine) -> None:
tests/test_api_auth.py:47:async def test_missing_key_returns_401(auth_client: AsyncClient) -> None:
tests/test_api_auth.py:58:async def test_wrong_key_returns_401(auth_client: AsyncClient) -> None:
tests/test_api_auth.py:70:async def test_correct_key_passes(auth_client: AsyncClient) -> None:
tests/test_api_auth.py:81:async def test_health_no_key_required(auth_app: FastAPI) -> None:
tests/test_api_auth.py:92:async def test_no_auth_when_key_empty(api_client: AsyncClient) -> None:
tests/test_e2e_walking_skeleton.py:49:async def test_walking_skeleton_happy_path(
tests/test_upsert.py:80:async def test_upsert_new_entry_sets_both_timestamps_equal(
tests/test_upsert.py:104:async def test_upsert_identical_payload_is_noop(async_session: AsyncSession, feed: Feed) -> None:
tests/test_upsert.py:128:async def test_upsert_changed_title_updates_content_updated_at_only(
tests/test_upsert.py:171:async def test_upsert_changed_published_at_also_updates(
tests/test_lifespan_drain.py:44:async def test_drain_background_task_returns_for_none_task() -> None:
tests/test_lifespan_drain.py:53:async def test_drain_background_task_clean_exit_within_budget() -> None:
tests/test_lifespan_drain.py:71:async def test_drain_background_task_force_cancels_on_timeout() -> None:
tests/test_lifespan_drain.py:97:async def test_drain_background_task_swallows_loop_exception() -> None:
tests/test_lifespan_drain.py:146:async def test_drain_waits_for_truly_in_flight_fetch_to_complete(
tests/test_errors.py:16:async def test_blocked_url_returns_problem_details(api_client: AsyncClient) -> None:
tests/test_errors.py:31:async def test_feed_not_found_returns_problem_details(api_client: AsyncClient) -> None:
tests/test_errors.py:44:async def test_invalid_cursor_on_entries_returns_problem_details(api_client: AsyncClient) -> None:
tests/test_errors.py:54:async def test_validation_error_returns_problem_details(api_client: AsyncClient) -> None:
tests/test_api_health.py:10:async def test_healthz_returns_ok(api_client: AsyncClient) -> None:
tests/test_api_entries.py:57:async def test_list_entries_requires_feed_ids(api_client: AsyncClient) -> None:
tests/test_api_entries.py:64:async def test_list_entries_returns_sorted_desc_by_published_at(
tests/test_api_entries.py:78:async def test_list_entries_keyset_cursor_advances(
tests/test_api_entries.py:115:async def test_list_entries_response_fields(api_client: AsyncClient, seeded_feed: int) -> None:
tests/test_api_entries.py:135:async def test_list_entries_invalid_cursor_returns_400(
tests/test_api_entries.py:146:async def test_list_entries_invalid_feed_ids_returns_400(
tests/test_api_entries.py:154:async def test_list_entries_cursor_walks_null_to_nonnull_region(
tests/test_api_entries.py:227:async def test_delete_feed_cascades_entries(api_client: AsyncClient, seeded_feed: int) -> None:
tests/test_parser.py:60:async def test_parse_atom_basic() -> None:
tests/test_parser.py:80:async def test_parse_rss_basic() -> None:
tests/test_parser.py:93:async def test_parse_entry_with_missing_guid_falls_back_to_link() -> None:
tests/test_parser.py:115:async def test_parse_entry_with_missing_published_at() -> None:
tests/test_parser.py:135:async def test_parse_empty_entries() -> None:
tests/test_foundation.py:16:async def test_postgres_container_is_reachable(
tests/test_retention.py:63:async def test_sweep_empty_db_is_noop(
tests/test_retention.py:76:async def test_sweep_deletes_entries_older_than_cutoff(
tests/test_retention.py:107:async def test_sweep_keeps_min_per_feed_regardless_of_age(
tests/test_retention.py:136:async def test_sweep_keeps_recent_entries_inside_window(
tests/test_retention.py:162:async def test_sweep_per_feed_isolation(
tests/test_retention.py:204:async def test_sweep_union_of_time_and_topn(
tests/test_retention.py:246:async def test_sweep_returns_zero_when_nothing_to_delete(
tests/test_retention.py:288:async def test_tick_once_runs_sweep_via_app_state(
tests/test_urlnorm.py:8:def test_scheme_lowercased() -> None:
tests/test_urlnorm.py:12:def test_host_lowercased() -> None:
tests/test_urlnorm.py:16:def test_default_http_port_stripped() -> None:
tests/test_urlnorm.py:20:def test_default_https_port_stripped() -> None:
tests/test_urlnorm.py:24:def test_non_default_port_kept() -> None:
tests/test_urlnorm.py:28:def test_trailing_slash_removed_from_path() -> None:
tests/test_urlnorm.py:32:def test_root_trailing_slash_collapsed() -> None:
tests/test_urlnorm.py:36:def test_fragment_removed() -> None:
tests/test_urlnorm.py:40:def test_query_preserved() -> None:
tests/test_urlnorm.py:45:def test_idn_host_to_punycode() -> None:
tests/test_urlnorm.py:52:def test_whitespace_stripped() -> None:
tests/test_scheduler_tick.py:44:async def test_tick_once_fetches_all_active_feeds(
tests/test_scheduler_tick.py:114:async def test_tick_once_with_no_active_feeds_is_noop(
tests/test_scheduler_tick.py:123:async def test_tick_once_continues_when_one_feed_fails(
tests/test_scheduler_tick.py:161:async def test_tick_once_skips_non_due_feeds(
tests/test_scheduler_tick.py:216:async def test_tick_once_probes_stale_dead_feed(
tests/test_scheduler_tick.py:262:async def test_tick_once_skips_recently_probed_dead_feed(
tests/test_scheduler_tick.py:369:async def test_per_host_throttle_serializes_same_host_feeds(
tests/test_scheduler_tick.py:401:async def test_per_host_throttle_allows_distinct_hosts_in_parallel(
tests/test_scheduler_tick.py:434:async def test_claim_due_feeds_skip_locked_prevents_double_claim(
tests/test_scheduler_tick.py:506:async def test_claim_due_feeds_advances_lease(
tests/test_scheduler_tick.py:567:async def test_scheduler_run_exits_cleanly_when_stop_event_is_set(
tests/test_scheduler_tick.py:590:async def test_scheduler_run_drains_in_flight_fetch_before_exit(
tests/test_api_feeds.py:20:async def test_post_feed_creates_row_with_full_lifecycle_fields(
tests/test_api_feeds.py:42:async def test_post_feed_normalizes_url(api_client: AsyncClient) -> None:
tests/test_api_feeds.py:52:async def test_post_feed_is_idempotent(api_client: AsyncClient) -> None:
tests/test_api_feeds.py:70:async def test_create_feed_concurrent_idempotent(
tests/test_api_feeds.py:93:async def test_get_feed_list_returns_created_feeds(
tests/test_api_feeds.py:108:async def test_list_feeds_cursor_walks_all_pages(
tests/test_api_feeds.py:144:async def test_list_feeds_cursor_with_status_filter(
tests/test_api_feeds.py:187:async def test_list_feeds_invalid_cursor_returns_400(
tests/test_api_feeds.py:195:async def test_get_feed_single_returns_feed(api_client: AsyncClient) -> None:
tests/test_api_feeds.py:206:async def test_get_feed_missing_returns_404(api_client: AsyncClient) -> None:
tests/test_api_feeds.py:212:async def test_delete_feed_removes_it(api_client: AsyncClient) -> None:
tests/test_api_feeds.py:224:async def test_delete_missing_feed_is_idempotent(
tests/test_api_feeds.py:258:async def test_list_feeds_status_filter_returns_only_matching(
tests/test_api_feeds.py:280:async def test_list_feeds_invalid_status_returns_422(
tests/test_api_feeds.py:291:async def test_reactivate_dead_feed_flips_to_active_and_resets_counters(
tests/test_api_feeds.py:318:async def test_reactivate_broken_feed_also_works(
tests/test_api_feeds.py:343:async def test_reactivate_missing_feed_returns_404(
tests/test_ssrf.py:58:async def test_validate_blocks_private_ip_literal(url: str) -> None:
tests/test_ssrf.py:73:async def test_validate_blocks_unsupported_scheme(url: str) -> None:
tests/test_ssrf.py:79:async def test_validate_rejects_missing_host() -> None:
tests/test_ssrf.py:85:async def test_validate_allows_public_ip_literal() -> None:
tests/test_ssrf.py:91:async def test_validate_skips_dns_when_resolve_false() -> None:
tests/test_ssrf.py:109:async def test_validate_blocks_dns_rebinding_to_private_ip(
tests/test_ssrf.py:124:async def test_validate_blocks_when_any_resolved_addr_is_private(
tests/test_ssrf.py:140:async def test_validate_allows_public_dns_resolution(
tests/test_ssrf.py:151:async def test_validate_allows_unresolvable_host(
tests/test_ssrf.py:183:async def test_transport_guard_blocks_private_request_before_inner() -> None:
tests/test_ssrf.py:194:async def test_transport_guard_passes_public_request_to_inner(
tests/test_ssrf.py:211:async def test_transport_guard_blocks_redirect_target(
tests/test_ssrf.py:261:async def test_post_feed_rejects_blocked_url(
tests/test_ssrf.py:271:async def test_post_feed_blocked_url_does_not_create_row(
tests/test_ssrf.py:294:async def test_fetch_one_marks_blocked_when_host_resolves_to_private_ip(
tests/test_fetch_one.py:125:async def test_fetch_one_success_stores_entries_and_updates_timers(
tests/test_fetch_one.py:168:async def test_fetch_one_301_updates_effective_url(
tests/test_fetch_one.py:209:async def test_fetch_one_302_does_not_update_effective_url(
tests/test_fetch_one.py:249:async def test_fetch_one_http_404_records_error_without_raising(
tests/test_fetch_one.py:284:async def test_fetch_one_second_success_resets_failure_counter(
tests/test_fetch_one.py:337:async def test_fetch_one_rejects_html_content_type_as_not_a_feed(
tests/test_fetch_one.py:393:async def test_fetch_one_caps_initial_fetch_entries(
tests/test_fetch_one.py:428:async def test_fetch_one_no_cap_on_subsequent_fetch(
tests/test_fetch_one.py:482:async def test_fetch_one_rejects_oversized_response_as_too_large(
tests/test_fetch_one.py:524:async def test_fetch_one_active_to_broken_after_n_failures(
tests/test_fetch_one.py:569:async def test_fetch_one_broken_to_active_on_success(
tests/test_fetch_one.py:618:async def test_fetch_one_broken_to_dead_after_duration_since_last_success(
tests/test_fetch_one.py:663:async def test_fetch_one_broken_to_dead_falls_back_to_created_at(
tests/test_fetch_one.py:709:async def test_fetch_one_broken_stays_broken_within_duration(
tests/test_fetch_one.py:755:async def test_fetch_one_http_410_transitions_to_dead_immediately(
tests/test_fetch_one.py:788:async def test_fetch_one_accepts_blank_content_type(
tests/test_fetch_one.py:834:async def test_fetch_one_429_is_not_a_circuit_breaker_failure(
tests/test_fetch_one.py:869:async def test_fetch_one_429_honors_retry_after_header(
tests/test_fetch_one.py:903:async def test_fetch_one_429_floors_retry_after_at_base_interval(
tests/test_fetch_one.py:937:async def test_fetch_one_429_honors_retry_after_http_date(
tests/test_fetch_one.py:974:async def test_fetch_one_total_budget_kills_slow_response(
tests/test_fetch_one.py:1022:def test_parse_retry_after_none_returns_none() -> None:
tests/test_fetch_one.py:1027:def test_classify_error_tls() -> None:
tests/test_fetch_one.py:1034:def test_classify_error_dns() -> None:
tests/test_fetch_one.py:1041:def test_classify_error_tcp_refused() -> None:
tests/test_fetch_one.py:1048:def test_classify_error_connect_error_without_known_cause() -> None:
tests/test_fetch_one.py:1053:def test_classify_error_too_many_redirects() -> None:
tests/test_fetch_one.py:1058:def test_classify_error_nested_cause_chain() -> None:
tests/test_fetch_one.py:1067:def test_parse_retry_after_integer_seconds() -> None:
tests/test_fetch_one.py:1076:def test_parse_retry_after_http_date_future() -> None:
tests/test_fetch_one.py:1082:def test_parse_retry_after_http_date_past_clamps_to_zero() -> None:
tests/test_fetch_one.py:1088:def test_parse_retry_after_unparseable_returns_none() -> None:
tests/test_fetch_one.py:1094:def test_compute_next_fetch_at_active_feed_uses_exact_base_interval() -> None:
tests/test_fetch_one.py:1109:def test_compute_next_fetch_at_broken_at_threshold_boundary_uses_base_with_jitter() -> None:
tests/test_fetch_one.py:1127:def test_compute_next_fetch_at_broken_feed_exponential_factor() -> None:
tests/test_fetch_one.py:1144:def test_compute_next_fetch_at_broken_feed_capped_at_max_backoff() -> None:
tests/test_fetch_one.py:1166:async def test_fetch_one_304_schedules_next_fetch_without_updating_success_fields(
tests/test_fetch_one.py:1201:async def test_fetch_one_sends_if_none_match_on_second_fetch(
tests/test_fetch_one.py:1263:async def test_fetch_one_sends_if_modified_since_when_no_etag(
tests/test_fetch_one.py:1310:async def test_fetch_one_updates_etag_on_new_200(
tests/test_fetch_one.py:1362:async def test_304_recovers_broken_feed(
tests/test_fetch_one.py:1408:async def test_200_cache_control_max_age_sets_next_fetch(
tests/test_fetch_one.py:1444:async def test_200_cache_control_max_age_floored_at_base_interval(
tests/test_fetch_one.py:1480:async def test_200_expires_header_sets_next_fetch(
tests/test_fetch_one.py:1522:async def test_304_cache_control_max_age_respected(
tests/test_fetch_one.py:1571:async def test_rss_ttl_sets_next_fetch(
tests/test_fetch_one.py:1607:async def test_rss_ttl_floored_at_base_interval(
tests/test_fetch_one.py:1672:async def test_entry_frequency_active_feed_with_history(
tests/test_fetch_one.py:1713:async def test_entry_frequency_new_feed_falls_back_to_base_interval(
tests/test_fetch_one.py:1750:async def test_entry_frequency_very_active_feed_clamped_at_min(
tests/test_fetch_one.py:1791:async def test_entry_frequency_quiet_feed_clamped_at_max(
tests/test_fetch_one.py:1830:async def test_entry_frequency_broken_feed_ignores_history(

codex
테스트 목록 기준으로 커버리지는 매우 넓습니다. 큰 테스트 파일은 `핵심 시나리오/가정/검증 포인트`를 확인할 수 있게 상단 구조와 대표 케이스 위주로 읽겠습니다.
exec
/bin/zsh -lc "sed -n '1,260p' tests/test_e2e_walking_skeleton.py" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
"""E2E walking skeleton test — the TDD first red.

This test is intentionally red at Phase 0 of the plan and progresses through
fail modes as Phases 1~5 fill in the missing pieces. The test body is
written in its final form; each Phase completion shifts the failure mode
forward (ImportError → AttributeError → HTTP 404 → assertion → green at
Phase 5.2).

DO NOT mark this test `xfail` or `skip`. It must fail loudly until the
walking skeleton is complete. See `.omc/plans/ralplan-feedgate-walking-
skeleton.md` Phase 0.3 for the policy.
"""

from __future__ import annotations

import os

import pytest
import respx
from httpx import ASGITransport, AsyncClient
from testcontainers.postgres import PostgresContainer

ATOM_BODY = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Fake Test Feed</title>
  <id>http://fake.test/feed.xml</id>
  <updated>2026-04-10T00:00:00Z</updated>
  <entry>
    <title>Hello World</title>
    <id>http://fake.test/posts/hello</id>
    <link href="http://fake.test/posts/hello"/>
    <updated>2026-04-10T00:00:00Z</updated>
    <published>2026-04-10T00:00:00Z</published>
    <content>First entry.</content>
  </entry>
  <entry>
    <title>Second Post</title>
    <id>http://fake.test/posts/second</id>
    <link href="http://fake.test/posts/second"/>
    <updated>2026-04-10T01:00:00Z</updated>
    <published>2026-04-10T01:00:00Z</published>
    <content>Second entry.</content>
  </entry>
</feed>
"""


@pytest.mark.asyncio
async def test_walking_skeleton_happy_path(
    pg_container: PostgresContainer,
    respx_mock: respx.Router,
    truncate_tables: None,  # ensure a clean feeds/entries table
) -> None:
    """Register a feed, run one scheduler tick, verify entries appear via API.

    Expected failure mode per Phase:
      * Phase 0 end: ImportError (feedgate.main / scheduler do not exist)
      * Phase 1 end: same (main.py still absent)
      * Phase 2 end: same
      * Phase 3 end: same (or AttributeError on scheduler.tick_once)
      * Phase 4 end: same (main.py still absent)
      * Phase 5.1:   test runs but wiring may be off (404 / missing field)
      * Phase 5.2:   green
    """
    # Imports live inside the test body so earlier-Phase failures surface
    # as ImportError on the specific missing symbol rather than a pytest
    # collection error on the whole file.
    from feedgate.fetcher import scheduler
    from feedgate.main import create_app

    # Wire the app to the test database and disable the background
    # scheduler task — we will drive ticks manually to avoid racing with
    # the lifespan-spawned loop.
    os.environ["FEEDGATE_SCHEDULER_ENABLED"] = "false"
    db_url = pg_container.get_connection_url()
    db_url = db_url.replace("postgresql+psycopg2://", "postgresql+asyncpg://")
    db_url = db_url.replace("postgresql://", "postgresql+asyncpg://")
    os.environ["FEEDGATE_DATABASE_URL"] = db_url

    # Mock the external feed URL with a minimal valid Atom document.
    feed_url = "http://fake.test/feed.xml"
    respx_mock.get(feed_url).respond(
        status_code=200,
        headers={"Content-Type": "application/atom+xml"},
        content=ATOM_BODY,
    )

    app = create_app()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        # Register the feed.
        resp = await client.post("/v1/feeds", json={"url": feed_url})
        assert resp.status_code in (200, 201), resp.text
        feed = resp.json()
        assert feed["url"] == feed_url
        assert feed["status"] == "active"
        assert "last_successful_fetch_at" in feed
        feed_id = feed["id"]

        # Drive one scheduler tick manually.
        await scheduler.tick_once(app)

        # Fetch entries for the registered feed.
        resp = await client.get(
            "/v1/entries",
            params={"feed_ids": str(feed_id), "limit": "10"},
        )
        assert resp.status_code == 200, resp.text
        page = resp.json()

        assert "items" in page
        entries = page["items"]
        assert len(entries) == 2
        guids = {e["guid"] for e in entries}
        assert "http://fake.test/posts/hello" in guids
        assert "http://fake.test/posts/second" in guids
        for e in entries:
            for field in (
                "id",
                "guid",
                "feed_id",
                "url",
                "title",
                "fetched_at",
                "content_updated_at",
            ):
                assert field in e, f"missing {field!r} in entry response"

exec
/bin/zsh -lc "sed -n '1,280p' tests/test_api_entries.py" in /Users/kangheeyong/project/feedgate-fetcher
exec
/bin/zsh -lc "sed -n '1,260p' tests/test_api_feeds.py" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
"""/v1/entries endpoint — keyset pagination, feed_ids filter, bounds.

Covers plan WP 3.6.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from feedgate.models import Entry, Feed


@pytest_asyncio.fixture
async def seeded_feed(
    api_app: FastAPI,
    async_session_factory: async_sessionmaker[AsyncSession],
    truncate_tables: None,
) -> int:
    """Create a feed with 5 entries and return its id.

    Published timestamps are 1 hour apart, most-recent first in the
    desired keyset order.
    """
    async with async_session_factory() as session:
        feed = Feed(url="http://seed.test/feed", effective_url="http://seed.test/feed")
        session.add(feed)
        await session.flush()
        feed_id = feed.id

        base = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)
        for i in range(5):
            session.add(
                Entry(
                    feed_id=feed_id,
                    guid=f"guid-{i}",
                    url=f"http://seed.test/posts/{i}",
                    title=f"Post {i}",
                    content=f"Content {i}",
                    author="seed",
                    published_at=base + timedelta(hours=i),
                    fetched_at=base,
                    content_updated_at=base,
                )
            )
        await session.commit()
        return feed_id


@pytest.mark.asyncio
async def test_list_entries_requires_feed_ids(api_client: AsyncClient) -> None:
    resp = await api_client.get("/v1/entries")
    # Missing required query param -> 422 from FastAPI validation
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_entries_returns_sorted_desc_by_published_at(
    api_client: AsyncClient, seeded_feed: int
) -> None:
    resp = await api_client.get("/v1/entries", params={"feed_ids": str(seeded_feed), "limit": 10})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["items"]) == 5
    # published_at DESC ordering -> guid-4 first, guid-0 last
    guids = [e["guid"] for e in body["items"]]
    assert guids == [f"guid-{i}" for i in (4, 3, 2, 1, 0)]
    assert body["next_cursor"] is None


@pytest.mark.asyncio
async def test_list_entries_keyset_cursor_advances(
    api_client: AsyncClient, seeded_feed: int
) -> None:
    first = await api_client.get("/v1/entries", params={"feed_ids": str(seeded_feed), "limit": 2})
    assert first.status_code == 200
    page1 = first.json()
    assert len(page1["items"]) == 2
    assert [e["guid"] for e in page1["items"]] == ["guid-4", "guid-3"]
    assert page1["next_cursor"] is not None

    second = await api_client.get(
        "/v1/entries",
        params={
            "feed_ids": str(seeded_feed),
            "limit": 2,
            "cursor": page1["next_cursor"],
        },
    )
    assert second.status_code == 200
    page2 = second.json()
    assert [e["guid"] for e in page2["items"]] == ["guid-2", "guid-1"]
    # Final page should have 1 item
    third = await api_client.get(
        "/v1/entries",
        params={
            "feed_ids": str(seeded_feed),
            "limit": 2,
            "cursor": page2["next_cursor"],
        },
    )
    assert third.status_code == 200
    page3 = third.json()
    assert [e["guid"] for e in page3["items"]] == ["guid-0"]
    assert page3["next_cursor"] is None


@pytest.mark.asyncio
async def test_list_entries_response_fields(api_client: AsyncClient, seeded_feed: int) -> None:
    resp = await api_client.get("/v1/entries", params={"feed_ids": str(seeded_feed), "limit": 1})
    assert resp.status_code == 200
    entry = resp.json()["items"][0]
    for field in (
        "id",
        "guid",
        "feed_id",
        "url",
        "title",
        "content",
        "author",
        "published_at",
        "fetched_at",
        "content_updated_at",
    ):
        assert field in entry, f"missing {field}"


@pytest.mark.asyncio
async def test_list_entries_invalid_cursor_returns_400(
    api_client: AsyncClient, seeded_feed: int
) -> None:
    resp = await api_client.get(
        "/v1/entries",
        params={"feed_ids": str(seeded_feed), "cursor": "not-valid-base64!"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_list_entries_invalid_feed_ids_returns_400(
    api_client: AsyncClient,
) -> None:
    resp = await api_client.get("/v1/entries", params={"feed_ids": "not,a,number"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_list_entries_cursor_walks_null_to_nonnull_region(
    api_client: AsyncClient,
    async_session_factory: async_sessionmaker[AsyncSession],
    truncate_tables: None,
) -> None:
    async with async_session_factory() as session:
        feed = Feed(url="http://cursor.test/feed", effective_url="http://cursor.test/feed")
        session.add(feed)
        await session.flush()
        feed_id = feed.id

        expected_ids: list[int] = []

        for i in range(3):
            entry = Entry(
                feed_id=feed_id,
                guid=f"null-guid-{i}",
                url=f"http://cursor.test/null/{i}",
                title=f"Null {i}",
                content=f"Null content {i}",
                author="seed",
                published_at=None,
                fetched_at=datetime(2026, 4, 15, 0, 0, 0, tzinfo=UTC),
                content_updated_at=datetime(2026, 4, 15, 0, 0, 0, tzinfo=UTC),
            )
            session.add(entry)
            await session.flush()
            expected_ids.append(entry.id)

        for i, pub in enumerate(
            (
                datetime(2026, 4, 1, 0, 0, 0, tzinfo=UTC),
                datetime(2026, 4, 2, 0, 0, 0, tzinfo=UTC),
                datetime(2026, 4, 3, 0, 0, 0, tzinfo=UTC),
            )
        ):
            entry = Entry(
                feed_id=feed_id,
                guid=f"dated-guid-{i}",
                url=f"http://cursor.test/dated/{i}",
                title=f"Dated {i}",
                content=f"Dated content {i}",
                author="seed",
                published_at=pub,
                fetched_at=datetime(2026, 4, 15, 0, 0, 0, tzinfo=UTC),
                content_updated_at=datetime(2026, 4, 15, 0, 0, 0, tzinfo=UTC),
            )
            session.add(entry)
            await session.flush()
            expected_ids.append(entry.id)

        await session.commit()

    collected_ids: list[int] = []
    cursor: str | None = None

    while True:
        params: dict[str, str | int] = {"feed_ids": str(feed_id), "limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        resp = await api_client.get("/v1/entries", params=params)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        collected_ids.extend(item["id"] for item in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert Counter(collected_ids) == Counter(expected_ids)
    assert len(collected_ids) == 6


@pytest.mark.asyncio
async def test_delete_feed_cascades_entries(api_client: AsyncClient, seeded_feed: int) -> None:
    # Delete the seeded feed
    delete_resp = await api_client.delete(f"/v1/feeds/{seeded_feed}")
    assert delete_resp.status_code == 204

    resp = await api_client.get("/v1/entries", params={"feed_ids": str(seeded_feed)})
    assert resp.status_code == 200
    assert resp.json()["items"] == []

 succeeded in 0ms:
"""/v1/feeds endpoints — POST, GET (list + single), DELETE.

Covers plan WPs 3.1 (POST), 3.2 (POST idempotency), 3.3 (GET list),
3.4 (GET single), 3.5 (DELETE cascade).
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from feedgate.models import Feed


@pytest.mark.asyncio
async def test_post_feed_creates_row_with_full_lifecycle_fields(
    api_client: AsyncClient,
) -> None:
    resp = await api_client.post(
        "/v1/feeds",
        json={"url": "http://example.com/feed.xml"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()

    assert body["id"] > 0
    assert body["url"] == "http://example.com/feed.xml"
    assert body["effective_url"] == "http://example.com/feed.xml"
    assert body["title"] is None
    assert body["status"] == "active"
    assert body["last_successful_fetch_at"] is None
    assert body["last_attempt_at"] is None
    assert body["last_error_code"] is None
    assert "created_at" in body


@pytest.mark.asyncio
async def test_post_feed_normalizes_url(api_client: AsyncClient) -> None:
    resp = await api_client.post(
        "/v1/feeds",
        json={"url": "HTTP://Example.COM:80/feed.xml#foo"},
    )
    assert resp.status_code == 201
    assert resp.json()["url"] == "http://example.com/feed.xml"


@pytest.mark.asyncio
async def test_post_feed_is_idempotent(api_client: AsyncClient) -> None:
    first = await api_client.post(
        "/v1/feeds",
        json={"url": "http://example.com/feed.xml"},
    )
    assert first.status_code == 201
    first_id = first.json()["id"]

    # Second POST with the same URL should return the same row with 200.
    second = await api_client.post(
        "/v1/feeds",
        json={"url": "http://example.com/feed.xml"},
    )
    assert second.status_code == 200
    assert second.json()["id"] == first_id


@pytest.mark.asyncio
async def test_create_feed_concurrent_idempotent(
    api_client: AsyncClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    url = "http://concurrent.test/feed.xml"
    responses = await asyncio.gather(
        *[api_client.post("/v1/feeds", json={"url": url}) for _ in range(5)]
    )

    statuses = [resp.status_code for resp in responses]
    assert all(code in {200, 201} for code in statuses), statuses
    assert statuses.count(201) == 1, statuses
    assert statuses.count(200) >= 4, statuses

    ids = [resp.json()["id"] for resp in responses]
    assert len(set(ids)) == 1, ids

    async with async_session_factory() as session:
        feeds = (await session.execute(select(Feed).where(Feed.url == url))).scalars().all()
    assert len(feeds) == 1


@pytest.mark.asyncio
async def test_get_feed_list_returns_created_feeds(
    api_client: AsyncClient,
) -> None:
    await api_client.post("/v1/feeds", json={"url": "http://a.test/feed"})
    await api_client.post("/v1/feeds", json={"url": "http://b.test/feed"})

    resp = await api_client.get("/v1/feeds")
    assert resp.status_code == 200
    body = resp.json()
    urls = {item["url"] for item in body["items"]}
    assert "http://a.test/feed" in urls
    assert "http://b.test/feed" in urls


@pytest.mark.asyncio
async def test_list_feeds_cursor_walks_all_pages(
    api_client: AsyncClient,
) -> None:
    seeded_ids: set[int] = set()
    for i in range(5):
        resp = await api_client.post("/v1/feeds", json={"url": f"http://paginate.test/{i}"})
        assert resp.status_code == 201
        seeded_ids.add(resp.json()["id"])

    seen_ids: list[int] = []
    cursor: str | None = None
    page = 0
    while True:
        params: dict[str, str | int] = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        resp = await api_client.get("/v1/feeds", params=params)
        assert resp.status_code == 200
        body = resp.json()
        seen_ids.extend(item["id"] for item in body["items"])
        next_cursor = body["next_cursor"]
        if len(body["items"]) == 2 and next_cursor is not None:
            assert isinstance(next_cursor, str)
            assert next_cursor
        if next_cursor is None:
            break
        cursor = next_cursor
        page += 1
        assert page < 10

    assert len(seen_ids) == 5
    assert len(set(seen_ids)) == 5
    assert set(seen_ids) == seeded_ids


@pytest.mark.asyncio
async def test_list_feeds_cursor_with_status_filter(
    api_client: AsyncClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    active_1 = await api_client.post("/v1/feeds", json={"url": "http://status-page.test/1"})
    active_2 = await api_client.post("/v1/feeds", json={"url": "http://status-page.test/2"})
    broken = await api_client.post("/v1/feeds", json={"url": "http://status-page.test/3"})
    assert active_1.status_code == 201
    assert active_2.status_code == 201
    assert broken.status_code == 201

    broken_id = broken.json()["id"]
    async with async_session_factory() as session:
        await session.execute(
            update(Feed).where(Feed.id == broken_id).values(status="broken"),
        )
        await session.commit()

    active_ids = {active_1.json()["id"], active_2.json()["id"]}
    seen_active_ids: list[int] = []
    cursor: str | None = None
    while True:
        params: dict[str, str | int] = {"limit": 1, "status": "active"}
        if cursor is not None:
            params["cursor"] = cursor
        resp = await api_client.get("/v1/feeds", params=params)
        assert resp.status_code == 200
        body = resp.json()
        for item in body["items"]:
            assert item["status"] == "active"
            assert item["id"] != broken_id
            seen_active_ids.append(item["id"])
        next_cursor = body["next_cursor"]
        if next_cursor is None:
            break
        cursor = next_cursor

    assert len(seen_active_ids) == 2
    assert len(set(seen_active_ids)) == 2
    assert set(seen_active_ids) == active_ids


@pytest.mark.asyncio
async def test_list_feeds_invalid_cursor_returns_400(
    api_client: AsyncClient,
) -> None:
    resp = await api_client.get("/v1/feeds", params={"cursor": "!!!bogus!!!"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_get_feed_single_returns_feed(api_client: AsyncClient) -> None:
    created = await api_client.post("/v1/feeds", json={"url": "http://x.test/feed"})
    feed_id = created.json()["id"]

    resp = await api_client.get(f"/v1/feeds/{feed_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == feed_id
    assert resp.json()["url"] == "http://x.test/feed"


@pytest.mark.asyncio
async def test_get_feed_missing_returns_404(api_client: AsyncClient) -> None:
    resp = await api_client.get("/v1/feeds/999999")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_feed_removes_it(api_client: AsyncClient) -> None:
    created = await api_client.post("/v1/feeds", json={"url": "http://delete.test/feed"})
    feed_id = created.json()["id"]

    delete_resp = await api_client.delete(f"/v1/feeds/{feed_id}")
    assert delete_resp.status_code == 204

    follow_up = await api_client.get(f"/v1/feeds/{feed_id}")
    assert follow_up.status_code == 404


@pytest.mark.asyncio
async def test_delete_missing_feed_is_idempotent(
    api_client: AsyncClient,
) -> None:
    resp = await api_client.delete("/v1/feeds/424242")
    # The plan leaves the exact code for missing IDs to spec; we
    # implement "idempotent delete" → 204.
    assert resp.status_code == 204


# ---- status filter + manual reactivation -----------------------------------


async def _seed_feed(
    sf: async_sessionmaker[AsyncSession],
    *,
    url: str,
    status: str,
    consecutive_failures: int = 0,
    last_error_code: str | None = None,
) -> int:
    async with sf() as session:
        feed = Feed(
            url=url,
            effective_url=url,
            status=status,
            consecutive_failures=consecutive_failures,
            last_error_code=last_error_code,
        )
        session.add(feed)
        await session.commit()
        return feed.id


@pytest.mark.asyncio
async def test_list_feeds_status_filter_returns_only_matching(
    api_client: AsyncClient,
    async_session_factory: async_sessionmaker[AsyncSession],

exec
/bin/zsh -lc "sed -n '1,260p' tests/test_scheduler_tick.py" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
"""scheduler.tick_once integration test (Phase 4 WP 4.2).

Seeds multiple active feeds, mocks their URLs, runs a single tick,
and verifies every feed got fetched and its entries stored.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest
import respx
from fastapi import FastAPI
from httpx import Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from feedgate.fetcher import scheduler
from feedgate.fetcher.scheduler import _claim_due_feeds
from feedgate.models import Entry, Feed


def _atom_with(guid: str, title: str) -> bytes:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Feed for {title}</title>
  <id>http://t.test/{guid}</id>
  <updated>2026-04-10T00:00:00Z</updated>
  <entry>
    <title>{title}</title>
    <id>{guid}</id>
    <link href="{guid}"/>
    <published>2026-04-10T00:00:00Z</published>
    <content>body of {title}</content>
  </entry>
</feed>
""".encode()


@pytest.mark.asyncio
async def test_tick_once_fetches_all_active_feeds(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory

    # Seed three active feeds and one "inactive" feed that should be skipped.
    feed_urls = [
        "http://t.test/a/feed",
        "http://t.test/b/feed",
        "http://t.test/c/feed",
    ]
    inactive_url = "http://t.test/x/feed"

    async with sf() as session:
        for u in feed_urls:
            session.add(Feed(url=u, effective_url=u))
        session.add(
            Feed(
                url=inactive_url,
                effective_url=inactive_url,
                status="dead",
            )
        )
        await session.commit()

    # Mock the three active feed URLs
    for url in feed_urls:
        guid = url + "/post-1"
        respx_mock.get(url).mock(
            return_value=Response(
                200,
                content=_atom_with(guid, f"post for {url}"),
                headers={"Content-Type": "application/atom+xml"},
            )
        )
    # Inactive URL should never be called — don't mock it.

    await scheduler.tick_once(fetch_app)

    # All three active feeds should have 1 entry each.
    async with sf() as session:
        entry_count_total = int(
            (await session.execute(select(func.count()).select_from(Entry))).scalar_one()
        )
        assert entry_count_total == 3

        # Each active feed has its last_successful_fetch_at set.
        result = await session.execute(
            select(
                Feed.url,
                Feed.last_successful_fetch_at,
                Feed.status,
                Feed.consecutive_failures,
            )
        )
        by_url = {row.url: row for row in result}

    for u in feed_urls:
        row = by_url[u]
        assert row.last_successful_fetch_at is not None
        assert row.status == "active"
        assert row.consecutive_failures == 0

    inactive = by_url[inactive_url]
    assert inactive.last_successful_fetch_at is None
    assert inactive.status == "dead"


@pytest.mark.asyncio
async def test_tick_once_with_no_active_feeds_is_noop(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    # No feeds in the DB. Should not raise or issue any requests.
    await scheduler.tick_once(fetch_app)


@pytest.mark.asyncio
async def test_tick_once_continues_when_one_feed_fails(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    ok_url = "http://t.test/ok/feed"
    bad_url = "http://t.test/bad/feed"

    async with sf() as session:
        session.add(Feed(url=ok_url, effective_url=ok_url))
        session.add(Feed(url=bad_url, effective_url=bad_url))
        await session.commit()

    respx_mock.get(ok_url).mock(
        return_value=Response(
            200,
            content=_atom_with(ok_url + "/1", "ok post"),
            headers={"Content-Type": "application/atom+xml"},
        )
    )
    respx_mock.get(bad_url).mock(return_value=Response(500))

    await scheduler.tick_once(fetch_app)

    async with sf() as session:
        ok = (await session.execute(select(Feed).where(Feed.url == ok_url))).scalar_one()
        bad = (await session.execute(select(Feed).where(Feed.url == bad_url))).scalar_one()

    assert ok.last_successful_fetch_at is not None
    assert ok.last_error_code is None
    assert ok.consecutive_failures == 0

    assert bad.last_successful_fetch_at is None
    assert bad.last_error_code == "http_5xx"
    assert bad.consecutive_failures == 1


@pytest.mark.asyncio
async def test_tick_once_skips_non_due_feeds(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """Non-dead feeds whose ``next_fetch_at`` is in the future must
    be skipped so that the exponential backoff on broken feeds is
    actually honored."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    due_url = "http://t.test/due/feed"
    future_url = "http://t.test/future/feed"

    now = datetime(2026, 4, 11, 12, 0, 0, tzinfo=UTC)

    async with sf() as session:
        session.add(
            Feed(
                url=due_url,
                effective_url=due_url,
                next_fetch_at=now - timedelta(seconds=5),  # due
            )
        )
        session.add(
            Feed(
                url=future_url,
                effective_url=future_url,
                next_fetch_at=now + timedelta(hours=1),  # not due
            )
        )
        await session.commit()

    # Only the due URL is mocked — if tick_once wrongly tried the
    # future feed, respx would raise unmatched-request.
    respx_mock.get(due_url).mock(
        return_value=Response(
            200,
            content=_atom_with(due_url + "/1", "due post"),
            headers={"Content-Type": "application/atom+xml"},
        )
    )

    await scheduler.tick_once(fetch_app, now=now)

    async with sf() as session:
        due_feed = (await session.execute(select(Feed).where(Feed.url == due_url))).scalar_one()
        future_feed = (
            await session.execute(select(Feed).where(Feed.url == future_url))
        ).scalar_one()

    # Due feed was fetched
    assert due_feed.last_successful_fetch_at is not None
    # Future feed was NOT fetched — last_attempt_at still untouched
    assert future_feed.last_attempt_at is None


@pytest.mark.asyncio
async def test_tick_once_probes_stale_dead_feed(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """Dead feeds whose ``last_attempt_at`` is older than the probe
    interval must be fetched. A successful probe returns the feed
    to ``active`` via fetch_one's success path."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    dead_url = "http://t.test/stale-dead/feed"

    now = datetime(2026, 4, 11, 12, 0, 0, tzinfo=UTC)
    stale_attempt = now - timedelta(days=8)  # > 7 day probe interval

    async with sf() as session:
        session.add(
            Feed(
                url=dead_url,
                effective_url=dead_url,
                status="dead",
                last_attempt_at=stale_attempt,
                last_error_code="http_4xx",
                consecutive_failures=50,
            )
        )
        await session.commit()

    respx_mock.get(dead_url).mock(
        return_value=Response(
            200,
            content=_atom_with(dead_url + "/1", "revived"),
            headers={"Content-Type": "application/atom+xml"},
        )
    )

    await scheduler.tick_once(fetch_app, now=now)

    async with sf() as session:
        revived = (await session.execute(select(Feed).where(Feed.url == dead_url))).scalar_one()

    assert revived.status == "active"  # probe succeeded -> resurrection
    assert revived.consecutive_failures == 0
    assert revived.last_error_code is None
    assert revived.last_successful_fetch_at is not None



exec
/bin/zsh -lc "sed -n '260,560p' tests/test_scheduler_tick.py" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:

@pytest.mark.asyncio
async def test_tick_once_skips_recently_probed_dead_feed(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """Dead feeds whose ``last_attempt_at`` is WITHIN the probe
    interval must be skipped entirely. No request should be issued."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    dead_url = "http://t.test/fresh-dead/feed"

    now = datetime(2026, 4, 11, 12, 0, 0, tzinfo=UTC)
    fresh_attempt = now - timedelta(hours=12)  # well under 7 days

    async with sf() as session:
        session.add(
            Feed(
                url=dead_url,
                effective_url=dead_url,
                status="dead",
                last_attempt_at=fresh_attempt,
                last_error_code="http_410",
                consecutive_failures=1,
            )
        )
        await session.commit()

    # Deliberately no mock — if tick_once hit this URL, respx would
    # raise unmatched-request. The assertion is "tick returns cleanly
    # and the feed state is untouched".
    await scheduler.tick_once(fetch_app, now=now)

    async with sf() as session:
        still_dead = (await session.execute(select(Feed).where(Feed.url == dead_url))).scalar_one()

    assert still_dead.status == "dead"
    assert still_dead.last_attempt_at == fresh_attempt  # untouched
    assert still_dead.last_error_code == "http_410"


async def _seed_due_feeds(
    sf: async_sessionmaker[AsyncSession],
    urls: list[str],
    now: datetime,
) -> None:
    async with sf() as session:
        for u in urls:
            session.add(
                Feed(
                    url=u,
                    effective_url=u,
                    next_fetch_at=now - timedelta(seconds=5),
                )
            )
        await session.commit()


def _make_barrier_recorder() -> tuple[
    dict[str, int],
    asyncio.Event,
    Callable[[Request], Awaitable[Response]],
]:
    """Build a respx side-effect that tracks how many calls are
    simultaneously in flight via a counter, AND signals an
    ``asyncio.Event`` the moment two callers overlap.

    Each call increments ``in_flight``, records the running max,
    sets the event when two callers are inside the side-effect at
    the same time, then waits for the event with a small timeout
    before returning. This makes the parallel-vs-serialized assertion
    deterministic regardless of CI scheduling jitter:

      * If the per-host throttle lets both callers in, the second
        call sets the event and both return immediately — observed
        ``max_in_flight == 2``, event ``is_set``.
      * If the throttle serializes them, only one is ever inside the
        side-effect, the event never fires, and the wait_for inside
        the recorder times out — observed ``max_in_flight == 1``,
        event NOT set. The 0.1s ceiling per call keeps the test
        well under one second total.
    """
    state: dict[str, int] = {"in_flight": 0, "max_in_flight": 0}
    both_in_flight = asyncio.Event()

    async def recorder(request: Request) -> Response:
        state["in_flight"] += 1
        state["max_in_flight"] = max(state["max_in_flight"], state["in_flight"])
        if state["in_flight"] >= 2:
            both_in_flight.set()
        # Wait long enough that a slow CI runner reliably lets the
        # second coroutine walk through _process_feed (DB query, host
        # sem, validate_public_url, respx dispatch) and reach the
        # recorder while the first is still holding. 0.1s was too
        # tight — the first caller timed out alone and the event
        # never fired in the parallel-hosts test.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(both_in_flight.wait(), timeout=1.0)
        state["in_flight"] -= 1
        guid = str(request.url) + "/post-1"
        return Response(
            200,
            content=_atom_with(guid, "throttle"),
            headers={"Content-Type": "application/atom+xml"},
        )

    return state, both_in_flight, recorder


@pytest.mark.asyncio
async def test_per_host_throttle_serializes_same_host_feeds(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """Two feeds on the same host must NEVER overlap inside the HTTP
    side-effect. The barrier event the recorder uses is wired so that
    a second concurrent caller would *immediately* set it; if the
    per-host semaphore is doing its job, the second caller is blocked
    in scheduler land and never reaches the side-effect, so the event
    stays clear and ``max_in_flight`` stays at 1."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    url_a = "http://same.test/a/feed"
    url_b = "http://same.test/b/feed"

    now = datetime(2026, 4, 11, 12, 0, 0, tzinfo=UTC)
    await _seed_due_feeds(sf, [url_a, url_b], now)

    state, both_in_flight, recorder = _make_barrier_recorder()
    respx_mock.get(url_a).mock(side_effect=recorder)
    respx_mock.get(url_b).mock(side_effect=recorder)

    await scheduler.tick_once(fetch_app, now=now)

    assert state["max_in_flight"] == 1, (
        f"per-host throttle leaked: max_in_flight={state['max_in_flight']}"
    )
    assert not both_in_flight.is_set(), (
        "barrier event fired — two callers were inside the side-effect simultaneously"
    )


@pytest.mark.asyncio
async def test_per_host_throttle_allows_distinct_hosts_in_parallel(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """The per-host cap must NOT serialize feeds on *different* hosts.
    Cross-host parallelism is the property the global ``fetch_concurrency``
    knob exists to provide; if this regresses, throughput collapses to
    one feed at a time on a healthy multi-origin batch.

    The barrier event makes the assertion deterministic: as soon as
    a second caller enters the side-effect, the event fires and both
    return. If the loop happens to enter them in strict series, the
    event never fires and the test fails loudly."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    url_a = "http://host-a.test/feed"
    url_b = "http://host-b.test/feed"

    now = datetime(2026, 4, 11, 12, 0, 0, tzinfo=UTC)
    await _seed_due_feeds(sf, [url_a, url_b], now)

    state, both_in_flight, recorder = _make_barrier_recorder()
    respx_mock.get(url_a).mock(side_effect=recorder)
    respx_mock.get(url_b).mock(side_effect=recorder)

    await scheduler.tick_once(fetch_app, now=now)

    assert both_in_flight.is_set(), "barrier event never fired — distinct hosts were serialized"
    assert state["max_in_flight"] == 2, (
        f"distinct hosts wrongly serialized: max_in_flight={state['max_in_flight']}"
    )


@pytest.mark.asyncio
async def test_claim_due_feeds_skip_locked_prevents_double_claim(
    fetch_app: FastAPI,
) -> None:
    """Two concurrent workers must never claim the same feed.

    We stage a single due feed, then fire two overlapping claim
    transactions. The first worker acquires ``FOR UPDATE`` on the
    row; the second hits ``SKIP LOCKED`` and sees an empty set. This
    is the core correctness test for the Postgres-as-queue model —
    if it ever regresses, two worker replicas will double-fetch
    every due feed on every tick.
    """
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/skip-locked-race/feed"

    now = datetime(2026, 4, 11, 12, 0, 0, tzinfo=UTC)

    async with sf() as session:
        session.add(
            Feed(
                url=feed_url,
                effective_url=feed_url,
                next_fetch_at=now - timedelta(seconds=5),
            )
        )
        await session.commit()

    # Barrier: worker A acquires the row lock, THEN signals worker B
    # to attempt its own claim. B runs while A's transaction is still
    # open, so A's FOR UPDATE is in effect and B must SKIP LOCKED it.
    a_has_locked = asyncio.Event()
    b_is_done = asyncio.Event()

    async def worker_a() -> list[int]:
        async with sf() as session:
            claimed = await _claim_due_feeds(
                session,
                now=now,
                claim_batch_size=8,
                claim_ttl_seconds=180,
                dead_probe_interval_days=7,
            )
            # A now holds FOR UPDATE on the selected rows. Release
            # control so worker B can run its query under the lock.
            a_has_locked.set()
            await b_is_done.wait()
            await session.commit()
            return claimed

    async def worker_b() -> list[int]:
        await a_has_locked.wait()
        async with sf() as session:
            claimed = await _claim_due_feeds(
                session,
                now=now,
                claim_batch_size=8,
                claim_ttl_seconds=180,
                dead_probe_interval_days=7,
            )
            await session.commit()
            b_is_done.set()
            return claimed

    a_result, b_result = await asyncio.gather(worker_a(), worker_b())

    # Worker A claimed the feed; worker B's SKIP LOCKED made it
    # invisible, so B returns an empty list. No double-claim.
    assert len(a_result) == 1
    assert b_result == []


@pytest.mark.asyncio
async def test_claim_due_feeds_advances_lease(
    fetch_app: FastAPI,
) -> None:
    """After a successful claim, the feed's ``next_fetch_at`` is
    advanced to ``now + claim_ttl_seconds`` and ``last_attempt_at``
    is set to ``now``. A subsequent claim call in a new transaction
    must now skip the feed because its gate timestamps are in the
    future / freshly attempted. This is what prevents re-claim
    across ticks, not just within one tick."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/lease-advance/feed"

    now = datetime(2026, 4, 11, 12, 0, 0, tzinfo=UTC)
    claim_ttl = 180

    async with sf() as session:
        session.add(
            Feed(
                url=feed_url,
                effective_url=feed_url,
                next_fetch_at=now - timedelta(seconds=5),
            )
        )
        await session.commit()

    async with sf() as session:
        first = await _claim_due_feeds(
            session,
            now=now,
            claim_batch_size=8,
            claim_ttl_seconds=claim_ttl,
            dead_probe_interval_days=7,
        )
        await session.commit()

    assert len(first) == 1

    async with sf() as session:
        # Same `now` — the prior claim should have bumped both
        # timestamps so the gate filters this feed out.
        second = await _claim_due_feeds(
            session,
            now=now,
            claim_batch_size=8,
            claim_ttl_seconds=claim_ttl,
            dead_probe_interval_days=7,
        )
        await session.commit()

    assert second == []

    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.url == feed_url))).scalar_one()
    assert feed.next_fetch_at == now + timedelta(seconds=claim_ttl)
    assert feed.last_attempt_at == now

exec
/bin/zsh -lc "sed -n '560,760p' tests/test_scheduler_tick.py" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
    assert feed.last_attempt_at == now


# ---- graceful shutdown drain ------------------------------------------------


@pytest.mark.asyncio
async def test_scheduler_run_exits_cleanly_when_stop_event_is_set(
    fetch_app: FastAPI,
) -> None:
    """The background loop must respect ``stop_event`` and return
    cleanly without raising. Without this, lifespan shutdown can only
    rely on ``task.cancel()``, which interrupts in-flight fetches and
    leaves SKIP LOCKED claims dangling until the lease TTL elapses."""
    fetch_app.state.fetch_interval_seconds = 0.05
    stop = asyncio.Event()
    task = asyncio.create_task(scheduler.run(fetch_app, stop_event=stop))

    # Let the loop spin a couple of (no-op) ticks so we know it is
    # really running, then signal stop.
    await asyncio.sleep(0.15)
    stop.set()

    await asyncio.wait_for(task, timeout=2.0)
    assert task.done()
    assert not task.cancelled()
    assert task.exception() is None


@pytest.mark.asyncio
async def test_scheduler_run_drains_in_flight_fetch_before_exit(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """If shutdown is signaled *while* a fetch is in flight, the
    drain path must let that fetch finish — the side-effect mock
    sets the stop event mid-call and we then assert the feed has
    ``last_successful_fetch_at`` populated. A naive cancel-on-stop
    implementation would interrupt fetch_one and leave the feed in
    its claimed-but-unreached state, failing this assertion."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    fetch_app.state.fetch_interval_seconds = 0.05
    feed_url = "http://t.test/drain/feed"
    now = datetime.now(UTC) - timedelta(seconds=5)
    async with sf() as session:
        session.add(Feed(url=feed_url, effective_url=feed_url, next_fetch_at=now))
        await session.commit()

    stop = asyncio.Event()

    async def stop_then_succeed(request: Request) -> Response:
        # Signal shutdown WHILE the fetch is in flight. The current
        # tick must still complete before the loop exits.
        stop.set()
        return Response(
            200,
            content=_atom_with(feed_url + "/post-1", "drained"),
            headers={"Content-Type": "application/atom+xml"},
        )

    respx_mock.get(feed_url).mock(side_effect=stop_then_succeed)

    task = asyncio.create_task(scheduler.run(fetch_app, stop_event=stop))
    await asyncio.wait_for(task, timeout=3.0)
    assert task.exception() is None

    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.url == feed_url))).scalar_one()
        entry_count = int(
            (
                await session.execute(
                    select(func.count()).select_from(Entry).where(Entry.feed_id == feed.id)
                )
            ).scalar_one()
        )
    assert feed.last_successful_fetch_at is not None, (
        "in-flight fetch was interrupted by shutdown — drain path is broken"
    )
    assert feed.last_error_code is None
    assert entry_count == 1

exec
/bin/zsh -lc 'rg "''^# ----|''^async def test_|''^def test_" tests/test_fetch_one.py -n' in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
125:async def test_fetch_one_success_stores_entries_and_updates_timers(
168:async def test_fetch_one_301_updates_effective_url(
209:async def test_fetch_one_302_does_not_update_effective_url(
249:async def test_fetch_one_http_404_records_error_without_raising(
284:async def test_fetch_one_second_success_resets_failure_counter(
337:async def test_fetch_one_rejects_html_content_type_as_not_a_feed(
393:async def test_fetch_one_caps_initial_fetch_entries(
428:async def test_fetch_one_no_cap_on_subsequent_fetch(
482:async def test_fetch_one_rejects_oversized_response_as_too_large(
524:async def test_fetch_one_active_to_broken_after_n_failures(
569:async def test_fetch_one_broken_to_active_on_success(
618:async def test_fetch_one_broken_to_dead_after_duration_since_last_success(
663:async def test_fetch_one_broken_to_dead_falls_back_to_created_at(
709:async def test_fetch_one_broken_stays_broken_within_duration(
755:async def test_fetch_one_http_410_transitions_to_dead_immediately(
788:async def test_fetch_one_accepts_blank_content_type(
821:# ---- next_fetch_at / exponential backoff (pure function tests) -------------
834:async def test_fetch_one_429_is_not_a_circuit_breaker_failure(
869:async def test_fetch_one_429_honors_retry_after_header(
903:async def test_fetch_one_429_floors_retry_after_at_base_interval(
937:async def test_fetch_one_429_honors_retry_after_http_date(
974:async def test_fetch_one_total_budget_kills_slow_response(
1022:def test_parse_retry_after_none_returns_none() -> None:
1027:def test_classify_error_tls() -> None:
1034:def test_classify_error_dns() -> None:
1041:def test_classify_error_tcp_refused() -> None:
1048:def test_classify_error_connect_error_without_known_cause() -> None:
1053:def test_classify_error_too_many_redirects() -> None:
1058:def test_classify_error_nested_cause_chain() -> None:
1067:def test_parse_retry_after_integer_seconds() -> None:
1076:def test_parse_retry_after_http_date_future() -> None:
1082:def test_parse_retry_after_http_date_past_clamps_to_zero() -> None:
1088:def test_parse_retry_after_unparseable_returns_none() -> None:
1094:def test_compute_next_fetch_at_active_feed_uses_exact_base_interval() -> None:
1109:def test_compute_next_fetch_at_broken_at_threshold_boundary_uses_base_with_jitter() -> None:
1127:def test_compute_next_fetch_at_broken_feed_exponential_factor() -> None:
1144:def test_compute_next_fetch_at_broken_feed_capped_at_max_backoff() -> None:
1162:# ---- ETag / If-None-Match + Last-Modified / If-Modified-Since tests ---------
1166:async def test_fetch_one_304_schedules_next_fetch_without_updating_success_fields(
1201:async def test_fetch_one_sends_if_none_match_on_second_fetch(
1263:async def test_fetch_one_sends_if_modified_since_when_no_etag(
1310:async def test_fetch_one_updates_etag_on_new_200(
1358:# ---- Improvement 1: 304 recovers a broken feed ------------------------------
1362:async def test_304_recovers_broken_feed(
1404:# ---- Improvement 2: Cache-Control / Expires header parsing ------------------
1408:async def test_200_cache_control_max_age_sets_next_fetch(
1444:async def test_200_cache_control_max_age_floored_at_base_interval(
1480:async def test_200_expires_header_sets_next_fetch(
1522:async def test_304_cache_control_max_age_respected(
1553:# ---- Improvement 3: RSS TTL field -------------------------------------------
1571:async def test_rss_ttl_sets_next_fetch(
1607:async def test_rss_ttl_floored_at_base_interval(
1642:# ---- entry_frequency scheduling tests ---------------------------------------
1672:async def test_entry_frequency_active_feed_with_history(
1713:async def test_entry_frequency_new_feed_falls_back_to_base_interval(
1750:async def test_entry_frequency_very_active_feed_clamped_at_min(
1791:async def test_entry_frequency_quiet_feed_clamped_at_max(
1830:async def test_entry_frequency_broken_feed_ignores_history(

codex
`fetch_one` 테스트는 상태머신·조건부요청·주기 계산까지 세밀해서, 개선 포인트 섹션 중심으로 샘플을 읽고 요약에 반영하겠습니다.
exec
/bin/zsh -lc "sed -n '1,220p' tests/test_fetch_one.py" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
"""fetch_one unit tests (Phase 4 WP 4.1).

Happy path and one failure case. respx mocks the transport so no real
HTTP is issued.
"""

from __future__ import annotations

import socket
import ssl
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from httpx import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from feedgate.config import Settings
from feedgate.fetcher.http import (
    _classify_error,
    _compute_next_fetch_at,
    _parse_retry_after,
    fetch_one,
)
from feedgate.lifecycle import ErrorCode
from feedgate.models import Entry, Feed

_TEST_SETTINGS = Settings()
_FETCH_DEFAULTS: dict[str, Any] = {
    "max_bytes": _TEST_SETTINGS.fetch_max_bytes,
    "max_entries_initial": _TEST_SETTINGS.fetch_max_entries_initial,
    "total_budget_seconds": _TEST_SETTINGS.fetch_total_budget_seconds,
    "broken_threshold": _TEST_SETTINGS.broken_threshold,
    "dead_duration_days": _TEST_SETTINGS.dead_duration_days,
    "broken_max_backoff_seconds": _TEST_SETTINGS.broken_max_backoff_seconds,
    "backoff_jitter_ratio": _TEST_SETTINGS.backoff_jitter_ratio,
    "entry_frequency_min_interval_seconds": 300,
    "entry_frequency_max_interval_seconds": 86400,
    "entry_frequency_factor": 1,
}


def _kwargs(**overrides: Any) -> dict[str, Any]:
    return {**_FETCH_DEFAULTS, **overrides}


ATOM_BODY = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Test Feed</title>
  <id>http://t.test/feed</id>
  <updated>2026-04-10T00:00:00Z</updated>
  <entry>
    <title>Alpha</title>
    <id>http://t.test/posts/alpha</id>
    <link href="http://t.test/posts/alpha"/>
    <published>2026-04-10T00:00:00Z</published>
    <content>Alpha body</content>
  </entry>
  <entry>
    <title>Beta</title>
    <id>http://t.test/posts/beta</id>
    <link href="http://t.test/posts/beta"/>
    <published>2026-04-10T01:00:00Z</published>
    <content>Beta body</content>
  </entry>
</feed>
"""


async def _create_feed(session_factory: async_sessionmaker[AsyncSession], url: str) -> int:
    async with session_factory() as session:
        feed = Feed(url=url, effective_url=url)
        session.add(feed)
        await session.commit()
        return feed.id


async def _load_feed(
    session_factory: async_sessionmaker[AsyncSession], feed_id: int
) -> dict[str, Any]:
    async with session_factory() as session:
        row = (
            await session.execute(
                select(
                    Feed.url,
                    Feed.effective_url,
                    Feed.title,
                    Feed.status,
                    Feed.last_successful_fetch_at,
                    Feed.last_attempt_at,
                    Feed.last_error_code,
                    Feed.next_fetch_at,
                    Feed.consecutive_failures,
                ).where(Feed.id == feed_id)
            )
        ).one()
        return {
            "url": row.url,
            "effective_url": row.effective_url,
            "title": row.title,
            "status": row.status,
            "last_successful_fetch_at": row.last_successful_fetch_at,
            "last_attempt_at": row.last_attempt_at,
            "last_error_code": row.last_error_code,
            "next_fetch_at": row.next_fetch_at,
            "consecutive_failures": row.consecutive_failures,
        }


async def _count_entries_for_feed(
    session_factory: async_sessionmaker[AsyncSession], feed_id: int
) -> int:
    async with session_factory() as session:
        result = await session.execute(
            select(func.count()).select_from(Entry).where(Entry.feed_id == feed_id)
        )
        return int(result.scalar_one())


@pytest.mark.asyncio
async def test_fetch_one_success_stores_entries_and_updates_timers(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/feed"
    feed_id = await _create_feed(sf, feed_url)

    respx_mock.get(feed_url).mock(
        return_value=Response(
            200, content=ATOM_BODY, headers={"Content-Type": "application/atom+xml"}
        )
    )

    now = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)
    interval = 60

    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=now,
            interval_seconds=interval,
            user_agent="test-agent",
            **_FETCH_DEFAULTS,
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    assert state["title"] == "Test Feed"
    assert state["last_successful_fetch_at"] == now
    assert state["last_attempt_at"] == now
    assert state["next_fetch_at"] == now + timedelta(seconds=interval)
    assert state["last_error_code"] is None
    assert state["consecutive_failures"] == 0
    assert state["status"] == "active"

    assert await _count_entries_for_feed(sf, feed_id) == 2


@pytest.mark.asyncio
async def test_fetch_one_301_updates_effective_url(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    old_url = "http://old.test/feed"
    new_url = "http://new.test/feed"
    feed_id = await _create_feed(sf, old_url)

    respx_mock.get(old_url).mock(return_value=Response(301, headers={"Location": new_url}))
    respx_mock.get(new_url).mock(
        return_value=Response(
            200,
            content=ATOM_BODY,
            headers={"Content-Type": "application/atom+xml"},
        )
    )

    now = datetime(2026, 4, 10, 12, 30, 0, tzinfo=UTC)

    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=now,
            interval_seconds=60,
            user_agent="test-agent",
            **_FETCH_DEFAULTS,
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    assert state["effective_url"] == new_url
    assert state["url"] == old_url
    assert state["last_successful_fetch_at"] is not None
    assert state["last_error_code"] is None


@pytest.mark.asyncio
async def test_fetch_one_302_does_not_update_effective_url(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    original_url = "http://tmp.test/feed"
    detour_url = "http://detour.test/feed"
    feed_id = await _create_feed(sf, original_url)

    respx_mock.get(original_url).mock(return_value=Response(302, headers={"Location": detour_url}))
    respx_mock.get(detour_url).mock(
        return_value=Response(

exec
/bin/zsh -lc "sed -n '220,520p' tests/test_fetch_one.py" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
        return_value=Response(
            200,
            content=ATOM_BODY,
            headers={"Content-Type": "application/atom+xml"},
        )
    )

    now = datetime(2026, 4, 10, 12, 40, 0, tzinfo=UTC)

    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=now,
            interval_seconds=60,
            user_agent="test-agent",
            **_FETCH_DEFAULTS,
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    assert state["effective_url"] == original_url
    assert state["url"] == original_url
    assert state["last_successful_fetch_at"] is not None


@pytest.mark.asyncio
async def test_fetch_one_http_404_records_error_without_raising(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/dead"
    feed_id = await _create_feed(sf, feed_url)

    respx_mock.get(feed_url).mock(return_value=Response(404))

    now = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)

    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=now,
            interval_seconds=60,
            user_agent="test-agent",
            **_FETCH_DEFAULTS,
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    assert state["last_attempt_at"] == now
    assert state["last_successful_fetch_at"] is None
    assert state["last_error_code"] == "http_4xx"
    assert state["consecutive_failures"] == 1
    assert state["status"] == "active"  # no state machine in walking skeleton
    assert await _count_entries_for_feed(sf, feed_id) == 0


@pytest.mark.asyncio
async def test_fetch_one_second_success_resets_failure_counter(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/flaky"
    feed_id = await _create_feed(sf, feed_url)

    # First attempt fails
    respx_mock.get(feed_url).mock(return_value=Response(500))
    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC),
            interval_seconds=60,
            user_agent="test-agent",
            **_FETCH_DEFAULTS,
        )
        await session.commit()

    state1 = await _load_feed(sf, feed_id)
    assert state1["consecutive_failures"] == 1
    assert state1["last_error_code"] == "http_5xx"

    # Second attempt succeeds
    respx_mock.get(feed_url).mock(
        return_value=Response(
            200, content=ATOM_BODY, headers={"Content-Type": "application/atom+xml"}
        )
    )
    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=datetime(2026, 4, 10, 13, 0, 0, tzinfo=UTC),
            interval_seconds=60,
            user_agent="test-agent",
            **_FETCH_DEFAULTS,
        )
        await session.commit()

    state2 = await _load_feed(sf, feed_id)
    assert state2["consecutive_failures"] == 0
    assert state2["last_error_code"] is None
    assert state2["last_successful_fetch_at"] is not None


@pytest.mark.asyncio
async def test_fetch_one_rejects_html_content_type_as_not_a_feed(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """200 OK + ``text/html`` body must be recorded as ``not_a_feed``
    and must not insert any entries, even if the body looks parseable.
    """
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/html-cloaking-as-feed"
    feed_id = await _create_feed(sf, feed_url)

    # A Cloudflare WAF page or a 200 OK HTML error page — not a feed.
    respx_mock.get(feed_url).mock(
        return_value=Response(
            200,
            content=b"<html><body>Attention Required</body></html>",
            headers={"Content-Type": "text/html; charset=utf-8"},
        )
    )

    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=datetime(2026, 4, 11, 0, 0, 0, tzinfo=UTC),
            interval_seconds=60,
            user_agent="test-agent",
            **_FETCH_DEFAULTS,
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    assert state["last_error_code"] == "not_a_feed"
    assert state["last_successful_fetch_at"] is None
    assert state["consecutive_failures"] == 1
    assert await _count_entries_for_feed(sf, feed_id) == 0


MANY_ATOM_ENTRIES = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Many entries feed</title>
  <id>http://t.test/many</id>
  <updated>2026-04-10T00:00:00Z</updated>
  <entry><title>E0</title><id>http://t.test/many/e0</id><link href="http://t.test/many/e0"/><published>2026-04-10T00:00:00Z</published><content>b0</content></entry>
  <entry><title>E1</title><id>http://t.test/many/e1</id><link href="http://t.test/many/e1"/><published>2026-04-10T01:00:00Z</published><content>b1</content></entry>
  <entry><title>E2</title><id>http://t.test/many/e2</id><link href="http://t.test/many/e2"/><published>2026-04-10T02:00:00Z</published><content>b2</content></entry>
  <entry><title>E3</title><id>http://t.test/many/e3</id><link href="http://t.test/many/e3"/><published>2026-04-10T03:00:00Z</published><content>b3</content></entry>
  <entry><title>E4</title><id>http://t.test/many/e4</id><link href="http://t.test/many/e4"/><published>2026-04-10T04:00:00Z</published><content>b4</content></entry>
  <entry><title>E5</title><id>http://t.test/many/e5</id><link href="http://t.test/many/e5"/><published>2026-04-10T05:00:00Z</published><content>b5</content></entry>
</feed>
"""


@pytest.mark.asyncio
async def test_fetch_one_caps_initial_fetch_entries(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """Brand-new feed with many entries must be truncated to
    ``max_entries_initial`` on the first fetch."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/many"
    feed_id = await _create_feed(sf, feed_url)

    respx_mock.get(feed_url).mock(
        return_value=Response(
            200,
            content=MANY_ATOM_ENTRIES,
            headers={"Content-Type": "application/atom+xml"},
        )
    )

    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=datetime(2026, 4, 11, 0, 0, 0, tzinfo=UTC),
            interval_seconds=60,
            user_agent="test-agent",
            **_kwargs(max_entries_initial=3),
        )
        await session.commit()

    assert await _count_entries_for_feed(sf, feed_id) == 3


@pytest.mark.asyncio
async def test_fetch_one_no_cap_on_subsequent_fetch(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """Once a feed already has some entries, the initial cap must
    NOT apply — later fetches can bring in more entries than the cap."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/many"
    feed_id = await _create_feed(sf, feed_url)

    respx_mock.get(feed_url).mock(
        return_value=Response(
            200,
            content=MANY_ATOM_ENTRIES,
            headers={"Content-Type": "application/atom+xml"},
        )
    )

    # First fetch with cap=3 → 3 entries persisted
    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=datetime(2026, 4, 11, 0, 0, 0, tzinfo=UTC),
            interval_seconds=60,
            user_agent="test-agent",
            **_kwargs(max_entries_initial=3),
        )
        await session.commit()

    assert await _count_entries_for_feed(sf, feed_id) == 3

    # Second fetch with the same feed body (6 entries) and the same cap:
    # the cap must NOT apply because existing_count > 0. All 6 entries
    # should end up in the DB (3 existing upserted + 3 new).
    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=datetime(2026, 4, 11, 1, 0, 0, tzinfo=UTC),
            interval_seconds=60,
            user_agent="test-agent",
            **_kwargs(max_entries_initial=3),
        )
        await session.commit()

    assert await _count_entries_for_feed(sf, feed_id) == 6


@pytest.mark.asyncio
async def test_fetch_one_rejects_oversized_response_as_too_large(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """A response body larger than ``max_bytes`` must be rejected as
    ``too_large`` with no entries persisted and no successful-fetch
    timer advance."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/huge-feed"
    feed_id = await _create_feed(sf, feed_url)

    # 4 KB payload, cap set to 1 KB below.
    oversized = b"<feed>" + (b"x" * 4096) + b"</feed>"
    respx_mock.get(feed_url).mock(
        return_value=Response(
            200,
            content=oversized,
            headers={"Content-Type": "application/atom+xml"},
        )
    )

    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=datetime(2026, 4, 11, 0, 0, 0, tzinfo=UTC),
            interval_seconds=60,
            user_agent="test-agent",
            **_kwargs(max_bytes=1024),
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    assert state["last_error_code"] == "too_large"
    assert state["last_successful_fetch_at"] is None
    assert state["consecutive_failures"] == 1
    assert await _count_entries_for_feed(sf, feed_id) == 0

exec
/bin/zsh -lc "sed -n '520,840p' tests/test_fetch_one.py" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
    assert await _count_entries_for_feed(sf, feed_id) == 0


@pytest.mark.asyncio
async def test_fetch_one_active_to_broken_after_n_failures(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """After ``broken_threshold`` consecutive failures, an active feed
    transitions to ``broken``. Failures below the threshold leave the
    status unchanged."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/flaky-to-broken"
    feed_id = await _create_feed(sf, feed_url)

    respx_mock.get(feed_url).mock(return_value=Response(500))

    async def _one_call(i: int) -> None:
        async with sf() as session:
            feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
            await fetch_one(
                session,
                fetch_app.state.http_client,
                feed,
                now=datetime(2026, 4, 11, 0, i, 0, tzinfo=UTC),
                interval_seconds=60,
                user_agent="test-agent",
                **_kwargs(broken_threshold=3),
            )
            await session.commit()

    for i in (1, 2):
        await _one_call(i)
        state = await _load_feed(sf, feed_id)
        assert state["status"] == "active", f"call {i}: {state}"
        assert state["consecutive_failures"] == i

    await _one_call(3)
    state = await _load_feed(sf, feed_id)
    assert state["status"] == "broken"
    assert state["consecutive_failures"] == 3

    await _one_call(4)
    state = await _load_feed(sf, feed_id)
    assert state["status"] == "broken"
    assert state["consecutive_failures"] == 4


@pytest.mark.asyncio
async def test_fetch_one_broken_to_active_on_success(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """A successful fetch on a broken feed flips it back to active and
    resets consecutive_failures to 0."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/recovered"
    async with sf() as session:
        feed = Feed(
            url=feed_url,
            effective_url=feed_url,
            status="broken",
            consecutive_failures=7,
            last_error_code="http_5xx",
        )
        session.add(feed)
        await session.commit()
        feed_id = feed.id

    respx_mock.get(feed_url).mock(
        return_value=Response(
            200,
            content=ATOM_BODY,
            headers={"Content-Type": "application/atom+xml"},
        )
    )

    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=datetime(2026, 4, 11, 0, 0, 0, tzinfo=UTC),
            interval_seconds=60,
            user_agent="test-agent",
            **_kwargs(broken_threshold=3),
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    assert state["status"] == "active"
    assert state["consecutive_failures"] == 0
    assert state["last_error_code"] is None
    assert state["last_successful_fetch_at"] is not None


@pytest.mark.asyncio
async def test_fetch_one_broken_to_dead_after_duration_since_last_success(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """A broken feed whose ``last_successful_fetch_at`` is older than
    ``dead_duration_days`` transitions to ``dead`` on the next failure."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/long-broken"

    now = datetime(2026, 4, 15, 12, 0, 0, tzinfo=UTC)
    last_success = datetime(2026, 4, 7, 12, 0, 0, tzinfo=UTC)  # 8 days ago

    async with sf() as session:
        feed = Feed(
            url=feed_url,
            effective_url=feed_url,
            status="broken",
            consecutive_failures=20,
            last_error_code="http_5xx",
            last_successful_fetch_at=last_success,
        )
        session.add(feed)
        await session.commit()
        feed_id = feed.id

    respx_mock.get(feed_url).mock(return_value=Response(500))

    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=now,
            interval_seconds=60,
            user_agent="test-agent",
            **_kwargs(broken_threshold=3, dead_duration_days=7),
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    assert state["status"] == "dead"


@pytest.mark.asyncio
async def test_fetch_one_broken_to_dead_falls_back_to_created_at(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """If a feed has never had a successful fetch, the dead clock
    counts from ``created_at`` instead of ``last_successful_fetch_at``."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/never-succeeded"

    now = datetime(2026, 4, 15, 12, 0, 0, tzinfo=UTC)
    created = datetime(2026, 4, 7, 12, 0, 0, tzinfo=UTC)  # 8 days ago

    async with sf() as session:
        feed = Feed(
            url=feed_url,
            effective_url=feed_url,
            status="broken",
            consecutive_failures=20,
            last_error_code="connection",
            last_successful_fetch_at=None,
            created_at=created,
        )
        session.add(feed)
        await session.commit()
        feed_id = feed.id

    respx_mock.get(feed_url).mock(return_value=Response(500))

    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=now,
            interval_seconds=60,
            user_agent="test-agent",
            **_kwargs(broken_threshold=3, dead_duration_days=7),
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    assert state["status"] == "dead"


@pytest.mark.asyncio
async def test_fetch_one_broken_stays_broken_within_duration(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """A broken feed that is still within the dead_duration window
    must stay ``broken`` — no premature dead transition."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/recent-broken"

    now = datetime(2026, 4, 15, 12, 0, 0, tzinfo=UTC)
    last_success = datetime(2026, 4, 13, 12, 0, 0, tzinfo=UTC)  # 2 days ago

    async with sf() as session:
        feed = Feed(
            url=feed_url,
            effective_url=feed_url,
            status="broken",
            consecutive_failures=5,
            last_error_code="http_5xx",
            last_successful_fetch_at=last_success,
        )
        session.add(feed)
        await session.commit()
        feed_id = feed.id

    respx_mock.get(feed_url).mock(return_value=Response(500))

    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=now,
            interval_seconds=60,
            user_agent="test-agent",
            **_kwargs(broken_threshold=3, dead_duration_days=7),
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    assert state["status"] == "broken"
    assert state["consecutive_failures"] == 6


@pytest.mark.asyncio
async def test_fetch_one_http_410_transitions_to_dead_immediately(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """HTTP 410 Gone is the only explicit permanent HTTP signal. It
    transitions the feed to ``dead`` on the first occurrence, even if
    consecutive_failures is still below the broken threshold."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/gone"
    feed_id = await _create_feed(sf, feed_url)

    respx_mock.get(feed_url).mock(return_value=Response(410))

    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=datetime(2026, 4, 11, 0, 0, 0, tzinfo=UTC),
            interval_seconds=60,
            user_agent="test-agent",
            **_kwargs(broken_threshold=3),
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    assert state["status"] == "dead"
    assert state["last_error_code"] == "http_410"
    assert state["consecutive_failures"] == 1


@pytest.mark.asyncio
async def test_fetch_one_accepts_blank_content_type(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """No Content-Type header + valid XML must still succeed — many
    real-world feeds ship empty or unusual Content-Type values."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/blank-ct-feed"
    feed_id = await _create_feed(sf, feed_url)

    respx_mock.get(feed_url).mock(
        return_value=Response(200, content=ATOM_BODY),
    )

    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=datetime(2026, 4, 11, 0, 0, 0, tzinfo=UTC),
            interval_seconds=60,
            user_agent="test-agent",
            **_FETCH_DEFAULTS,
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    assert state["last_error_code"] is None
    assert state["last_successful_fetch_at"] is not None
    assert await _count_entries_for_feed(sf, feed_id) == 2


# ---- next_fetch_at / exponential backoff (pure function tests) -------------


def _fake_feed(status: str, consecutive_failures: int) -> Feed:
    return Feed(
        url="http://t.test/dummy",
        effective_url="http://t.test/dummy",
        status=status,
        consecutive_failures=consecutive_failures,
    )


@pytest.mark.asyncio
async def test_fetch_one_429_is_not_a_circuit_breaker_failure(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """Repeated 429 responses must NOT increment consecutive_failures
    and must NOT transition the feed state. 429 is "slow down", not
    "you are broken". Only ``last_error_code`` is recorded."""

exec
/bin/zsh -lc "sed -n '840,1220p' tests/test_fetch_one.py" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
    "you are broken". Only ``last_error_code`` is recorded."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/rate-limited"
    feed_id = await _create_feed(sf, feed_url)

    respx_mock.get(feed_url).mock(return_value=Response(429))

    for i in range(5):
        async with sf() as session:
            feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
            await fetch_one(
                session,
                fetch_app.state.http_client,
                feed,
                now=datetime(2026, 4, 11, 0, i, 0, tzinfo=UTC),
                interval_seconds=60,
                user_agent="test-agent",
                **_kwargs(broken_threshold=3),
            )
            await session.commit()

    state = await _load_feed(sf, feed_id)
    assert state["status"] == "active"
    assert state["consecutive_failures"] == 0
    assert state["last_error_code"] == "rate_limited"
    assert state["last_successful_fetch_at"] is None


@pytest.mark.asyncio
async def test_fetch_one_429_honors_retry_after_header(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """When Retry-After is present and >= base_interval, fetch_one
    schedules next_fetch_at at ``now + retry_after``."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/retry-after-300"
    feed_id = await _create_feed(sf, feed_url)

    respx_mock.get(feed_url).mock(return_value=Response(429, headers={"Retry-After": "300"}))

    now = datetime(2026, 4, 11, 0, 0, 0, tzinfo=UTC)
    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=now,
            interval_seconds=60,
            user_agent="test-agent",
            **_kwargs(broken_threshold=3),
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    delta = (state["next_fetch_at"] - now).total_seconds()
    # Retry-After 300s > base_interval 60s → use 300
    assert delta == 300
    assert state["last_error_code"] == "rate_limited"


@pytest.mark.asyncio
async def test_fetch_one_429_floors_retry_after_at_base_interval(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """Retry-After values shorter than base_interval must be clamped
    up to base_interval — we never poll faster than our normal
    schedule just because the upstream says "10s"."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/retry-after-small"
    feed_id = await _create_feed(sf, feed_url)

    respx_mock.get(feed_url).mock(return_value=Response(429, headers={"Retry-After": "10"}))

    now = datetime(2026, 4, 11, 0, 0, 0, tzinfo=UTC)
    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=now,
            interval_seconds=60,
            user_agent="test-agent",
            **_kwargs(broken_threshold=3),
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    delta = (state["next_fetch_at"] - now).total_seconds()
    # Retry-After 10 < base_interval 60 → floor at 60
    assert delta == 60


@pytest.mark.asyncio
async def test_fetch_one_429_honors_retry_after_http_date(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """Cloudflare/GitHub emit Retry-After as an HTTP-date, not a
    delta-seconds integer. fetch_one must convert the absolute date to
    a relative delay against ``now`` and honor it (subject to the
    base-interval floor)."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/retry-after-http-date"
    feed_id = await _create_feed(sf, feed_url)

    now = datetime(2026, 4, 11, 0, 0, 0, tzinfo=UTC)
    # 5 minutes in the future, RFC 7231 IMF-fixdate form
    retry_date = "Sat, 11 Apr 2026 00:05:00 GMT"
    respx_mock.get(feed_url).mock(return_value=Response(429, headers={"Retry-After": retry_date}))

    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=now,
            interval_seconds=60,
            user_agent="test-agent",
            **_kwargs(broken_threshold=3),
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    delta = (state["next_fetch_at"] - now).total_seconds()
    assert delta == 300
    assert state["last_error_code"] == "rate_limited"


@pytest.mark.asyncio
async def test_fetch_one_total_budget_kills_slow_response(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """A respx side-effect that sleeps longer than ``total_budget_seconds``
    must trigger ``asyncio.timeout`` inside fetch_one. The feed is
    marked with ``last_error_code = 'timeout'`` and ``consecutive_failures``
    is bumped — that is the load-bearing slow-loris defense.
    """
    import asyncio as _asyncio  # local alias to avoid clashing with re-exports

    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/slow-loris/feed"
    feed_id = await _create_feed(sf, feed_url)

    async def slow_response(request: Any) -> Response:
        # Sleep is several times the budget — guarantees the timeout
        # fires before the body is delivered.
        await _asyncio.sleep(0.5)
        return Response(
            200,
            content=ATOM_BODY,
            headers={"Content-Type": "application/atom+xml"},
        )

    respx_mock.get(feed_url).mock(side_effect=slow_response)

    now = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)

    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=now,
            interval_seconds=60,
            user_agent="test-agent",
            **_kwargs(total_budget_seconds=0.05),
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    assert state["last_error_code"] == "timeout"
    assert state["last_successful_fetch_at"] is None
    assert state["consecutive_failures"] == 1


def test_parse_retry_after_none_returns_none() -> None:
    now = datetime(2026, 4, 11, 0, 0, 0, tzinfo=UTC)
    assert _parse_retry_after(None, now=now) is None


def test_classify_error_tls() -> None:
    cause = ssl.SSLError("handshake fail")
    exc = httpx.ConnectError("tls")
    exc.__cause__ = cause
    assert _classify_error(exc) == ErrorCode.TLS_ERROR


def test_classify_error_dns() -> None:
    cause = socket.gaierror(-2, "Name or service not known")
    exc = httpx.ConnectError("dns")
    exc.__cause__ = cause
    assert _classify_error(exc) == ErrorCode.DNS


def test_classify_error_tcp_refused() -> None:
    cause = ConnectionRefusedError()
    exc = httpx.ConnectError("tcp refused")
    exc.__cause__ = cause
    assert _classify_error(exc) == ErrorCode.TCP_REFUSED


def test_classify_error_connect_error_without_known_cause() -> None:
    exc = httpx.ConnectError("generic")
    assert _classify_error(exc) == ErrorCode.CONNECTION


def test_classify_error_too_many_redirects() -> None:
    exc = httpx.TooManyRedirects("redirect loop")
    assert _classify_error(exc) == ErrorCode.REDIRECT_LOOP


def test_classify_error_nested_cause_chain() -> None:
    dns_cause = socket.gaierror(-2, "Name or service not known")
    wrap = OSError("wrap")
    wrap.__cause__ = dns_cause
    exc = httpx.ConnectError("outer")
    exc.__cause__ = wrap
    assert _classify_error(exc) == ErrorCode.DNS


def test_parse_retry_after_integer_seconds() -> None:
    now = datetime(2026, 4, 11, 0, 0, 0, tzinfo=UTC)
    assert _parse_retry_after("120", now=now) == 120
    # Whitespace allowed around the integer form.
    assert _parse_retry_after("  60 ", now=now) == 60
    # Negative integers clamp to zero.
    assert _parse_retry_after("-10", now=now) == 0


def test_parse_retry_after_http_date_future() -> None:
    now = datetime(2026, 4, 11, 0, 0, 0, tzinfo=UTC)
    # IMF-fixdate, 2 minutes in the future
    assert _parse_retry_after("Sat, 11 Apr 2026 00:02:00 GMT", now=now) == 120


def test_parse_retry_after_http_date_past_clamps_to_zero() -> None:
    now = datetime(2026, 4, 11, 0, 10, 0, tzinfo=UTC)
    # Date is 10 minutes earlier than now — must clamp at 0, not go negative
    assert _parse_retry_after("Sat, 11 Apr 2026 00:00:00 GMT", now=now) == 0


def test_parse_retry_after_unparseable_returns_none() -> None:
    now = datetime(2026, 4, 11, 0, 0, 0, tzinfo=UTC)
    assert _parse_retry_after("not-a-date", now=now) is None
    assert _parse_retry_after("", now=now) is None


def test_compute_next_fetch_at_active_feed_uses_exact_base_interval() -> None:
    now = datetime(2026, 4, 11, 12, 0, 0, tzinfo=UTC)
    feed = _fake_feed("active", 1)

    result = _compute_next_fetch_at(
        feed,
        now=now,
        base_interval_seconds=60,
        broken_threshold=3,
        broken_max_backoff_seconds=3600,
        backoff_jitter_ratio=0.25,
    )
    assert result == now + timedelta(seconds=60)


def test_compute_next_fetch_at_broken_at_threshold_boundary_uses_base_with_jitter() -> None:
    now = datetime(2026, 4, 11, 12, 0, 0, tzinfo=UTC)
    # Just transitioned: consecutive_failures == threshold, excess = 0
    feed = _fake_feed("broken", 3)

    result = _compute_next_fetch_at(
        feed,
        now=now,
        base_interval_seconds=60,
        broken_threshold=3,
        broken_max_backoff_seconds=3600,
        backoff_jitter_ratio=0.25,
    )
    delta_seconds = (result - now).total_seconds()
    # factor=1, raw=60, jitter in [-15, +15]
    assert 45 <= delta_seconds <= 75


def test_compute_next_fetch_at_broken_feed_exponential_factor() -> None:
    now = datetime(2026, 4, 11, 12, 0, 0, tzinfo=UTC)
    # excess = 5 - 3 = 2, factor = 4, raw = 240s, jitter [-60, +60]
    feed = _fake_feed("broken", 5)

    result = _compute_next_fetch_at(
        feed,
        now=now,
        base_interval_seconds=60,
        broken_threshold=3,
        broken_max_backoff_seconds=3600,
        backoff_jitter_ratio=0.25,
    )
    delta_seconds = (result - now).total_seconds()
    assert 180 <= delta_seconds <= 300


def test_compute_next_fetch_at_broken_feed_capped_at_max_backoff() -> None:
    now = datetime(2026, 4, 11, 12, 0, 0, tzinfo=UTC)
    # excess = 20 - 3 = 17, factor = 2**17 = 131072, raw ~= 7.8M s,
    # capped at 3600s, jitter [-900, +900]
    feed = _fake_feed("broken", 20)

    result = _compute_next_fetch_at(
        feed,
        now=now,
        base_interval_seconds=60,
        broken_threshold=3,
        broken_max_backoff_seconds=3600,
        backoff_jitter_ratio=0.25,
    )
    delta_seconds = (result - now).total_seconds()
    assert 2700 <= delta_seconds <= 4500


# ---- ETag / If-None-Match + Last-Modified / If-Modified-Since tests ---------


@pytest.mark.asyncio
async def test_fetch_one_304_schedules_next_fetch_without_updating_success_fields(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """A 304 Not Modified response must advance next_fetch_at but must NOT
    update last_successful_fetch_at or consecutive_failures."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/conditional-304"
    feed_id = await _create_feed(sf, feed_url)

    respx_mock.get(feed_url).mock(return_value=Response(304))

    now = datetime(2026, 4, 23, 10, 0, 0, tzinfo=UTC)
    interval = 60

    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=now,
            interval_seconds=interval,
            user_agent="test-agent",
            **_FETCH_DEFAULTS,
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    assert state["next_fetch_at"] == now + timedelta(seconds=interval)
    assert state["last_successful_fetch_at"] is None
    assert state["consecutive_failures"] == 0


@pytest.mark.asyncio
async def test_fetch_one_sends_if_none_match_on_second_fetch(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """After a 200 that returns ETag, the next fetch must include
    If-None-Match with that ETag value."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/etag-feed"
    feed_id = await _create_feed(sf, feed_url)

    etag_value = '"abc123"'

    # First fetch: 200 with ETag header
    respx_mock.get(feed_url).mock(
        return_value=Response(
            200,
            content=ATOM_BODY,
            headers={"Content-Type": "application/atom+xml", "ETag": etag_value},
        )
    )

exec
/bin/zsh -lc "sed -n '1220,1620p' tests/test_fetch_one.py" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
    )

    now1 = datetime(2026, 4, 23, 10, 0, 0, tzinfo=UTC)
    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=now1,
            interval_seconds=60,
            user_agent="test-agent",
            **_FETCH_DEFAULTS,
        )
        await session.commit()

    # Second fetch: verify If-None-Match is sent
    captured_headers: dict[str, str] = {}

    def capture_and_respond(request: Any) -> Response:
        captured_headers.update(dict(request.headers))
        return Response(304)

    respx_mock.get(feed_url).mock(side_effect=capture_and_respond)

    now2 = datetime(2026, 4, 23, 11, 0, 0, tzinfo=UTC)
    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=now2,
            interval_seconds=60,
            user_agent="test-agent",
            **_FETCH_DEFAULTS,
        )
        await session.commit()

    assert captured_headers.get("if-none-match") == etag_value


@pytest.mark.asyncio
async def test_fetch_one_sends_if_modified_since_when_no_etag(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """When there is no ETag but a Last-Modified is stored, the next
    fetch must include If-Modified-Since instead."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/last-modified-feed"
    last_modified_value = "Wed, 23 Apr 2026 09:00:00 GMT"

    async with sf() as session:
        feed = Feed(
            url=feed_url,
            effective_url=feed_url,
            last_modified=last_modified_value,
        )
        session.add(feed)
        await session.commit()
        feed_id = feed.id

    captured_headers: dict[str, str] = {}

    def capture_and_respond(request: Any) -> Response:
        captured_headers.update(dict(request.headers))
        return Response(304)

    respx_mock.get(feed_url).mock(side_effect=capture_and_respond)

    now = datetime(2026, 4, 23, 10, 0, 0, tzinfo=UTC)
    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=now,
            interval_seconds=60,
            user_agent="test-agent",
            **_FETCH_DEFAULTS,
        )
        await session.commit()

    assert captured_headers.get("if-modified-since") == last_modified_value
    assert "if-none-match" not in captured_headers


@pytest.mark.asyncio
async def test_fetch_one_updates_etag_on_new_200(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """When a 200 response returns a new ETag, feed.etag must be updated
    to reflect the latest value."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/etag-update-feed"
    old_etag = '"old-etag"'
    new_etag = '"new-etag"'

    async with sf() as session:
        feed = Feed(
            url=feed_url,
            effective_url=feed_url,
            etag=old_etag,
        )
        session.add(feed)
        await session.commit()
        feed_id = feed.id

    respx_mock.get(feed_url).mock(
        return_value=Response(
            200,
            content=ATOM_BODY,
            headers={"Content-Type": "application/atom+xml", "ETag": new_etag},
        )
    )

    now = datetime(2026, 4, 23, 10, 0, 0, tzinfo=UTC)
    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=now,
            interval_seconds=60,
            user_agent="test-agent",
            **_FETCH_DEFAULTS,
        )
        await session.commit()

    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        assert feed.etag == new_etag


# ---- Improvement 1: 304 recovers a broken feed ------------------------------


@pytest.mark.asyncio
async def test_304_recovers_broken_feed(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """A 304 on a broken feed must flip it back to active and reset failure counters."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/broken-304-recovery"

    async with sf() as session:
        feed = Feed(
            url=feed_url,
            effective_url=feed_url,
            status="broken",
            consecutive_failures=5,
            last_error_code="http_5xx",
        )
        session.add(feed)
        await session.commit()
        feed_id = feed.id

    respx_mock.get(feed_url).mock(return_value=Response(304))

    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=now,
            interval_seconds=60,
            user_agent="test-agent",
            **_FETCH_DEFAULTS,
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    assert state["status"] == "active"
    assert state["consecutive_failures"] == 0
    assert state["last_successful_fetch_at"] == now


# ---- Improvement 2: Cache-Control / Expires header parsing ------------------


@pytest.mark.asyncio
async def test_200_cache_control_max_age_sets_next_fetch(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """Cache-Control: max-age=7200 on a 200 must set next_fetch_at to now+7200s."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/cache-control-max-age"
    feed_id = await _create_feed(sf, feed_url)

    respx_mock.get(feed_url).mock(
        return_value=Response(
            200,
            content=ATOM_BODY,
            headers={"Content-Type": "application/atom+xml", "Cache-Control": "max-age=7200"},
        )
    )

    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=now,
            interval_seconds=60,
            user_agent="test-agent",
            **_FETCH_DEFAULTS,
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    assert state["next_fetch_at"] == now + timedelta(seconds=7200)


@pytest.mark.asyncio
async def test_200_cache_control_max_age_floored_at_base_interval(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """Cache-Control: max-age=10 < base_interval=60 must be floored to 60s."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/cache-control-small-max-age"
    feed_id = await _create_feed(sf, feed_url)

    respx_mock.get(feed_url).mock(
        return_value=Response(
            200,
            content=ATOM_BODY,
            headers={"Content-Type": "application/atom+xml", "Cache-Control": "max-age=10"},
        )
    )

    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=now,
            interval_seconds=60,
            user_agent="test-agent",
            **_FETCH_DEFAULTS,
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    assert state["next_fetch_at"] == now + timedelta(seconds=60)


@pytest.mark.asyncio
async def test_200_expires_header_sets_next_fetch(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """Expires header 2h in the future must set next_fetch_at approximately now+2h."""
    from email.utils import format_datetime as _fmt_dt

    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/expires-header"
    feed_id = await _create_feed(sf, feed_url)

    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    expires_dt = now + timedelta(hours=2)
    expires_str = _fmt_dt(expires_dt, usegmt=True)

    respx_mock.get(feed_url).mock(
        return_value=Response(
            200,
            content=ATOM_BODY,
            headers={"Content-Type": "application/atom+xml", "Expires": expires_str},
        )
    )

    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=now,
            interval_seconds=60,
            user_agent="test-agent",
            **_FETCH_DEFAULTS,
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    delta = (state["next_fetch_at"] - now).total_seconds()
    assert abs(delta - 7200) <= 5


@pytest.mark.asyncio
async def test_304_cache_control_max_age_respected(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """Cache-Control: max-age=3600 on a 304 must set next_fetch_at to now+3600s."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/304-cache-control"
    feed_id = await _create_feed(sf, feed_url)

    respx_mock.get(feed_url).mock(
        return_value=Response(304, headers={"Cache-Control": "max-age=3600"})
    )

    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=now,
            interval_seconds=60,
            user_agent="test-agent",
            **_FETCH_DEFAULTS,
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    assert state["next_fetch_at"] == now + timedelta(seconds=3600)


# ---- Improvement 3: RSS TTL field -------------------------------------------

_RSS_TTL_120 = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
<title>TTL Feed</title>
<ttl>120</ttl>
<item><title>A</title><link>http://ex.com/1</link><guid>http://ex.com/1</guid></item>
</channel></rss>"""

_RSS_TTL_0 = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
<title>TTL Feed</title>
<ttl>0</ttl>
<item><title>A</title><link>http://ex.com/1</link><guid>http://ex.com/1</guid></item>
</channel></rss>"""


@pytest.mark.asyncio
async def test_rss_ttl_sets_next_fetch(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """RSS <ttl>120</ttl> (minutes) → 7200s must win over base_interval=60s."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/rss-ttl-120"
    feed_id = await _create_feed(sf, feed_url)

    respx_mock.get(feed_url).mock(
        return_value=Response(
            200,
            content=_RSS_TTL_120,
            headers={"Content-Type": "application/rss+xml"},
        )
    )

    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=now,
            interval_seconds=60,
            user_agent="test-agent",
            **_FETCH_DEFAULTS,
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    assert state["next_fetch_at"] == now + timedelta(seconds=7200)


@pytest.mark.asyncio
async def test_rss_ttl_floored_at_base_interval(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """RSS <ttl>0</ttl> → 0s must be floored to base_interval=60s."""
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/rss-ttl-0"
    feed_id = await _create_feed(sf, feed_url)

    respx_mock.get(feed_url).mock(
        return_value=Response(
            200,
            content=_RSS_TTL_0,
            headers={"Content-Type": "application/rss+xml"},

exec
/bin/zsh -lc "sed -n '1620,1910p' tests/test_fetch_one.py" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
            headers={"Content-Type": "application/rss+xml"},
        )
    )

    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=now,
            interval_seconds=60,
            user_agent="test-agent",
            **_FETCH_DEFAULTS,
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    assert state["next_fetch_at"] == now + timedelta(seconds=60)


# ---- entry_frequency scheduling tests ---------------------------------------

EMPTY_ATOM_BODY = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Empty Feed</title>
  <id>http://t.test/empty</id>
  <updated>2026-04-10T00:00:00Z</updated>
</feed>
"""


async def _insert_entries(
    session_factory: async_sessionmaker[AsyncSession],
    feed_id: int,
    fetched_ats: list[datetime],
) -> None:
    async with session_factory() as session:
        for i, fat in enumerate(fetched_ats):
            entry = Entry(
                feed_id=feed_id,
                guid=f"http://t.test/ef-entry/{feed_id}/{i}",
                url=f"http://t.test/ef-entry/{feed_id}/{i}",
                fetched_at=fat,
                content_updated_at=fat,
            )
            session.add(entry)
        await session.commit()


@pytest.mark.asyncio
async def test_entry_frequency_active_feed_with_history(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/ef-history"
    feed_id = await _create_feed(sf, feed_url)

    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    fetched_ats = [now - timedelta(days=i) for i in range(7)]
    await _insert_entries(sf, feed_id, fetched_ats)

    respx_mock.get(feed_url).mock(
        return_value=Response(
            200, content=ATOM_BODY, headers={"Content-Type": "application/atom+xml"}
        )
    )

    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=now,
            interval_seconds=60,
            user_agent="test-agent",
            **_kwargs(
                entry_frequency_min_interval_seconds=300,
                entry_frequency_max_interval_seconds=86400,
                entry_frequency_factor=1,
            ),
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    delta = (state["next_fetch_at"] - now).total_seconds()
    assert abs(delta - 86400) <= 5


@pytest.mark.asyncio
async def test_entry_frequency_new_feed_falls_back_to_base_interval(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/ef-new"
    feed_id = await _create_feed(sf, feed_url)

    respx_mock.get(feed_url).mock(
        return_value=Response(
            200, content=EMPTY_ATOM_BODY, headers={"Content-Type": "application/atom+xml"}
        )
    )

    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=now,
            interval_seconds=3600,
            user_agent="test-agent",
            **_kwargs(
                entry_frequency_min_interval_seconds=300,
                entry_frequency_max_interval_seconds=86400,
                entry_frequency_factor=1,
            ),
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    assert state["next_fetch_at"] == now + timedelta(seconds=3600)


@pytest.mark.asyncio
async def test_entry_frequency_very_active_feed_clamped_at_min(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/ef-very-active"
    feed_id = await _create_feed(sf, feed_url)

    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    fetched_ats = [now - timedelta(minutes=i) for i in range(10000)]
    await _insert_entries(sf, feed_id, fetched_ats)

    respx_mock.get(feed_url).mock(
        return_value=Response(
            200, content=ATOM_BODY, headers={"Content-Type": "application/atom+xml"}
        )
    )

    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=now,
            interval_seconds=60,
            user_agent="test-agent",
            **_kwargs(
                entry_frequency_min_interval_seconds=300,
                entry_frequency_max_interval_seconds=86400,
                entry_frequency_factor=1,
            ),
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    delta = (state["next_fetch_at"] - now).total_seconds()
    assert abs(delta - 300) <= 5


@pytest.mark.asyncio
async def test_entry_frequency_quiet_feed_clamped_at_max(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/ef-quiet"
    feed_id = await _create_feed(sf, feed_url)

    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    await _insert_entries(sf, feed_id, [now - timedelta(days=6)])

    respx_mock.get(feed_url).mock(
        return_value=Response(
            200, content=ATOM_BODY, headers={"Content-Type": "application/atom+xml"}
        )
    )

    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=now,
            interval_seconds=60,
            user_agent="test-agent",
            **_kwargs(
                entry_frequency_min_interval_seconds=300,
                entry_frequency_max_interval_seconds=86400,
                entry_frequency_factor=1,
            ),
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    assert state["next_fetch_at"] == now + timedelta(seconds=86400)


@pytest.mark.asyncio
async def test_entry_frequency_broken_feed_ignores_history(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    feed_url = "http://t.test/ef-broken"

    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    last_success = now - timedelta(days=1)

    async with sf() as session:
        feed = Feed(
            url=feed_url,
            effective_url=feed_url,
            status="broken",
            consecutive_failures=5,
            last_error_code="http_5xx",
            last_successful_fetch_at=last_success,
        )
        session.add(feed)
        await session.commit()
        feed_id = feed.id

    fetched_ats = [now - timedelta(hours=i) for i in range(100)]
    await _insert_entries(sf, feed_id, fetched_ats)

    respx_mock.get(feed_url).mock(return_value=Response(500))

    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=now,
            interval_seconds=60,
            user_agent="test-agent",
            **_kwargs(broken_threshold=3, dead_duration_days=7),
        )
        await session.commit()

    state = await _load_feed(sf, feed_id)
    delta = (state["next_fetch_at"] - now).total_seconds()
    assert abs(delta - 864) > 5

exec
/bin/zsh -lc "sed -n '1,260p' tests/test_retention.py" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
"""Entry retention sweep tests (ADR 004, docs/spec/entry.md).

The rule:
    Per feed, KEEP the union of
      * entries with ``fetched_at >= cutoff`` (time window)
      * the ``min_per_feed`` most-recent entries (top-N window)
    Everything else gets DELETED.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from feedgate import retention
from feedgate.models import Entry, Feed
from feedgate.retention import sweep


async def _mk_feed(session: AsyncSession, url: str) -> int:
    feed = Feed(url=url, effective_url=url)
    session.add(feed)
    await session.flush()
    return feed.id


async def _mk_entry(
    session: AsyncSession,
    feed_id: int,
    guid: str,
    fetched_at: datetime,
) -> None:
    session.add(
        Entry(
            feed_id=feed_id,
            guid=guid,
            url=f"http://t.test/{guid}",
            title=guid,
            fetched_at=fetched_at,
            content_updated_at=fetched_at,
        )
    )


async def _count_entries(session: AsyncSession, feed_id: int) -> int:
    result = await session.execute(
        select(func.count()).select_from(Entry).where(Entry.feed_id == feed_id)
    )
    return int(result.scalar_one())


async def _guids_for_feed(session: AsyncSession, feed_id: int) -> set[str]:
    result = await session.execute(select(Entry.guid).where(Entry.feed_id == feed_id))
    return {row[0] for row in result.all()}


@pytest.mark.asyncio
async def test_sweep_empty_db_is_noop(
    async_session: AsyncSession,
    truncate_tables: None,
) -> None:
    deleted = await sweep(
        async_session,
        cutoff=datetime(2026, 1, 1, tzinfo=UTC),
        min_per_feed=20,
    )
    assert deleted == 0


@pytest.mark.asyncio
async def test_sweep_deletes_entries_older_than_cutoff(
    async_session: AsyncSession,
    truncate_tables: None,
) -> None:
    feed_id = await _mk_feed(async_session, "http://t.test/feed-age")

    now = datetime(2026, 4, 11, tzinfo=UTC)
    # 5 entries spaced 1 day apart going back in time
    for i in range(5):
        await _mk_entry(
            async_session,
            feed_id,
            guid=f"age-{i}",
            fetched_at=now - timedelta(days=i),
        )
    await async_session.flush()

    # Cutoff = now - 2d  (inclusive, >= cutoff keeps equal-to-boundary)
    # → keeps days 0, 1, 2 ; drops days 3, 4
    # Per-feed top-N also protects the most recent, so set
    # min_per_feed=0 to isolate the time-window behaviour.
    cutoff = now - timedelta(days=2)
    deleted = await sweep(async_session, cutoff=cutoff, min_per_feed=0)

    assert deleted == 2
    assert await _count_entries(async_session, feed_id) == 3
    remaining = await _guids_for_feed(async_session, feed_id)
    assert remaining == {"age-0", "age-1", "age-2"}


@pytest.mark.asyncio
async def test_sweep_keeps_min_per_feed_regardless_of_age(
    async_session: AsyncSession,
    truncate_tables: None,
) -> None:
    """Even if every entry is ancient, the most-recent N must stay."""
    feed_id = await _mk_feed(async_session, "http://t.test/feed-ancient")

    ancient = datetime(2020, 1, 1, tzinfo=UTC)  # way before cutoff
    # 10 entries all way in the past, spaced 1 hour apart
    for i in range(10):
        await _mk_entry(
            async_session,
            feed_id,
            guid=f"anc-{i}",
            fetched_at=ancient + timedelta(hours=i),
        )
    await async_session.flush()

    cutoff = datetime(2026, 1, 1, tzinfo=UTC)  # all 10 are older
    deleted = await sweep(async_session, cutoff=cutoff, min_per_feed=3)

    # 10 ancient entries, time window keeps 0, top-3 keeps 3 most-recent
    # (anc-9, anc-8, anc-7). Delete = 10 - 3 = 7.
    assert deleted == 7
    remaining = await _guids_for_feed(async_session, feed_id)
    assert remaining == {"anc-9", "anc-8", "anc-7"}


@pytest.mark.asyncio
async def test_sweep_keeps_recent_entries_inside_window(
    async_session: AsyncSession,
    truncate_tables: None,
) -> None:
    """Entries inside the time window must be kept even if the
    per-feed top-N cap would not include them."""
    feed_id = await _mk_feed(async_session, "http://t.test/feed-recent")

    now = datetime(2026, 4, 11, tzinfo=UTC)
    for i in range(10):
        await _mk_entry(
            async_session,
            feed_id,
            guid=f"rec-{i}",
            fetched_at=now - timedelta(hours=i),  # all within last 10 hours
        )
    await async_session.flush()

    cutoff = now - timedelta(days=1)  # all 10 are inside the window
    deleted = await sweep(async_session, cutoff=cutoff, min_per_feed=2)

    assert deleted == 0
    assert await _count_entries(async_session, feed_id) == 10


@pytest.mark.asyncio
async def test_sweep_per_feed_isolation(
    async_session: AsyncSession,
    truncate_tables: None,
) -> None:
    """Each feed gets its OWN top-N — feeds don't share the budget."""
    feed_a = await _mk_feed(async_session, "http://t.test/feed-a")
    feed_b = await _mk_feed(async_session, "http://t.test/feed-b")

    ancient = datetime(2020, 1, 1, tzinfo=UTC)
    # Feed A: 5 ancient entries
    for i in range(5):
        await _mk_entry(
            async_session,
            feed_a,
            guid=f"a-{i}",
            fetched_at=ancient + timedelta(hours=i),
        )
    # Feed B: 5 ancient entries
    for i in range(5):
        await _mk_entry(
            async_session,
            feed_b,
            guid=f"b-{i}",
            fetched_at=ancient + timedelta(hours=i),
        )
    await async_session.flush()

    cutoff = datetime(2026, 1, 1, tzinfo=UTC)
    deleted = await sweep(async_session, cutoff=cutoff, min_per_feed=2)

    # Each feed: 5 entries - 2 kept = 3 deleted -> 6 total deleted
    assert deleted == 6
    assert await _count_entries(async_session, feed_a) == 2
    assert await _count_entries(async_session, feed_b) == 2

    remaining_a = await _guids_for_feed(async_session, feed_a)
    remaining_b = await _guids_for_feed(async_session, feed_b)
    assert remaining_a == {"a-4", "a-3"}
    assert remaining_b == {"b-4", "b-3"}


@pytest.mark.asyncio
async def test_sweep_union_of_time_and_topn(
    async_session: AsyncSession,
    truncate_tables: None,
) -> None:
    """Integration case: some entries inside the window, some outside
    but protected by top-N, some outside both (should be deleted)."""
    feed_id = await _mk_feed(async_session, "http://t.test/feed-union")

    now = datetime(2026, 4, 11, tzinfo=UTC)
    # 3 recent entries (inside window)
    for i in range(3):
        await _mk_entry(
            async_session,
            feed_id,
            guid=f"new-{i}",
            fetched_at=now - timedelta(hours=i),
        )
    # 10 ancient entries
    ancient = datetime(2020, 1, 1, tzinfo=UTC)
    for i in range(10):
        await _mk_entry(
            async_session,
            feed_id,
            guid=f"old-{i}",
            fetched_at=ancient + timedelta(hours=i),
        )
    await async_session.flush()

    cutoff = now - timedelta(days=1)  # keeps the 3 recent ones
    deleted = await sweep(async_session, cutoff=cutoff, min_per_feed=5)

    # Keep set: 3 recent (time window) + top-5 by fetched_at DESC
    # (which is new-0, new-1, new-2, old-9, old-8) = 5 unique.
    # UNION of time window {new-0, new-1, new-2} and top-5
    # {new-0, new-1, new-2, old-9, old-8} = 5 entries.
    # Total: 13, keep 5, delete 8.
    assert deleted == 8
    remaining = await _guids_for_feed(async_session, feed_id)
    assert remaining == {"new-0", "new-1", "new-2", "old-9", "old-8"}


@pytest.mark.asyncio
async def test_sweep_returns_zero_when_nothing_to_delete(
    async_session: AsyncSession,
    truncate_tables: None,
) -> None:
    feed_id = await _mk_feed(async_session, "http://t.test/feed-fresh")
    now = datetime(2026, 4, 11, tzinfo=UTC)
    for i in range(5):
        await _mk_entry(
            async_session,
            feed_id,
            guid=f"fresh-{i}",
            fetched_at=now - timedelta(minutes=i),
        )
    await async_session.flush()


exec
/bin/zsh -lc "sed -n '260,420p' tests/test_retention.py" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:

    deleted = await sweep(
        async_session,
        cutoff=now - timedelta(days=90),
        min_per_feed=20,
    )
    assert deleted == 0
    assert await _count_entries(async_session, feed_id) == 5


# ---- retention.tick_once integration ---------------------------------------


@pytest_asyncio.fixture
async def retention_app(
    async_session_factory: async_sessionmaker[AsyncSession],
    truncate_tables: None,
) -> FastAPI:
    """Minimal FastAPI app with the state retention.tick_once reads."""
    app = FastAPI()
    app.state.session_factory = async_session_factory
    app.state.retention_days = 90
    app.state.retention_min_per_feed = 2
    app.state.retention_sweep_interval_seconds = 3600
    return app


@pytest.mark.asyncio
async def test_tick_once_runs_sweep_via_app_state(
    retention_app: FastAPI,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """tick_once reads cutoff + min_per_feed from ``app.state``, opens
    its own session, commits, and returns the delete count."""
    # 5 ancient entries, retention_min_per_feed=2 in the fixture
    # → tick_once deletes 3, keeps the 2 most recent.
    ancient = datetime(2020, 1, 1, tzinfo=UTC)
    async with async_session_factory() as session:
        feed_id = await _mk_feed(session, "http://t.test/feed-tick")
        for i in range(5):
            await _mk_entry(
                session,
                feed_id,
                guid=f"t-{i}",
                fetched_at=ancient + timedelta(hours=i),
            )
        await session.commit()

    now = datetime(2026, 4, 11, tzinfo=UTC)
    deleted = await retention.tick_once(retention_app, now=now)
    assert deleted == 3

    async with async_session_factory() as session:
        count = (
            await session.execute(
                select(func.count()).select_from(Entry).where(Entry.feed_id == feed_id)
            )
        ).scalar_one()
        guids = {
            row[0]
            for row in (
                await session.execute(select(Entry.guid).where(Entry.feed_id == feed_id))
            ).all()
        }
    assert count == 2
    assert guids == {"t-4", "t-3"}

exec
/bin/zsh -lc "sed -n '260,380p' tests/test_ssrf.py" in /Users/kangheeyong/project/feedgate-fetcher
exec
/bin/zsh -lc "sed -n '1,260p' tests/test_ssrf.py" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
)
async def test_post_feed_rejects_blocked_url(
    api_client: AsyncClient,
    url: str,
) -> None:
    resp = await api_client.post("/v1/feeds", json={"url": url})
    assert resp.status_code == 400, resp.text
    assert "blocked_url" in resp.text


@pytest.mark.asyncio
async def test_post_feed_blocked_url_does_not_create_row(
    api_client: AsyncClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Defense check: a rejected POST must NOT leave a Feed row behind.
    A future PR could accidentally swap the validation past the
    insert; this test would catch that regression."""
    resp = await api_client.post(
        "/v1/feeds",
        json={"url": "http://10.1.2.3/feed"},
    )
    assert resp.status_code == 400
    async with async_session_factory() as session:
        rows = (await session.execute(select(Feed))).scalars().all()
    assert rows == []


# ---------------------------------------------------------------------------
# Integration — fetch_one pre-flight catches DNS rebinding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_one_marks_blocked_when_host_resolves_to_private_ip(
    fetch_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a feed registered with a benign hostname whose DNS
    later flips to ``10.x`` must be marked with ``last_error_code =
    'blocked'`` and never reach the HTTP client. We do not mock the
    URL with respx — if the SSRF pre-check failed to fire, respx
    would raise unmatched-request and the test would still fail loud.
    """
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    rebound_url = "http://rebound.example.com/feed"

    async with sf() as session:
        session.add(Feed(url=rebound_url, effective_url=rebound_url))
        await session.commit()

    async def fake_resolve(host: str) -> list[str]:
        return ["10.0.0.42"]

    monkeypatch.setattr(ssrf, "_resolve", fake_resolve)

    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.url == rebound_url))).scalar_one()
        await fetch_one(
            session,
            fetch_app.state.http_client,
            feed,
            now=datetime(2026, 4, 12, 12, 0, 0, tzinfo=UTC),
            interval_seconds=fetch_app.state.fetch_interval_seconds,
            user_agent=fetch_app.state.fetch_user_agent,
            max_bytes=fetch_app.state.fetch_max_bytes,
            max_entries_initial=fetch_app.state.fetch_max_entries_initial,
            total_budget_seconds=fetch_app.state.fetch_total_budget_seconds,
            broken_threshold=fetch_app.state.broken_threshold,
            dead_duration_days=fetch_app.state.dead_duration_days,
            broken_max_backoff_seconds=fetch_app.state.broken_max_backoff_seconds,
            backoff_jitter_ratio=fetch_app.state.backoff_jitter_ratio,
            entry_frequency_min_interval_seconds=fetch_app.state.entry_frequency_min_interval_seconds,
            entry_frequency_max_interval_seconds=fetch_app.state.entry_frequency_max_interval_seconds,
            entry_frequency_factor=fetch_app.state.entry_frequency_factor,
        )
        await session.commit()

    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.url == rebound_url))).scalar_one()
    assert feed.last_error_code == ErrorCode.BLOCKED
    assert feed.last_successful_fetch_at is None
    assert feed.consecutive_failures == 1

 succeeded in 0ms:
"""SSRF protection — unit + integration coverage.

Maps to docs/spec/resilience.md threat category D and the
TC-D1..D4 entries in docs/tests/resilience-test-cases.md.

Layered defense:
  * ``validate_public_url`` rejects scheme/IP-literal/DNS-resolved
    blocked addresses.
  * ``POST /v1/feeds`` calls it with ``resolve=False`` so a private IP
    literal is rejected with ``400`` at registration time.
  * ``fetch_one`` calls it with ``resolve=True`` (the default), so a
    DNS-rebinding host registered earlier is blocked at fetch time.
  * ``SSRFGuardTransport`` re-runs the check on every request the
    httpx client makes — including redirect follow-ups, which are the
    only path that bypasses the fetch_one pre-check.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from feedgate import ssrf
from feedgate.fetcher.http import fetch_one
from feedgate.lifecycle import ErrorCode
from feedgate.models import Feed
from feedgate.ssrf import (
    BlockedURLError,
    SSRFGuardTransport,
    validate_public_url,
)

# ---------------------------------------------------------------------------
# Unit tests — validate_public_url
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.1/feed",  # TC-D1-01 RFC1918
        "http://172.16.0.1/feed",  # TC-D1-01 RFC1918
        "http://192.168.1.1/feed",  # TC-D1-01 RFC1918
        "http://127.0.0.1/feed",  # TC-D1-02 loopback
        "http://[::1]/feed",  # TC-D1-02 loopback v6
        "http://169.254.169.254/latest/meta-data/",  # TC-D2-01 cloud metadata
        "http://0.0.0.0/feed",  # unspecified
        "http://[fe80::1]/feed",  # link-local v6
    ],
)
async def test_validate_blocks_private_ip_literal(url: str) -> None:
    with pytest.raises(BlockedURLError):
        await validate_public_url(url, resolve=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "gopher://internal.host/",
        "ftp://internal.host/",
        "javascript:alert(1)",
    ],
)
async def test_validate_blocks_unsupported_scheme(url: str) -> None:
    with pytest.raises(BlockedURLError):
        await validate_public_url(url, resolve=False)


@pytest.mark.asyncio
async def test_validate_rejects_missing_host() -> None:
    with pytest.raises(BlockedURLError):
        await validate_public_url("http:///feed", resolve=False)


@pytest.mark.asyncio
async def test_validate_allows_public_ip_literal() -> None:
    await validate_public_url("http://8.8.8.8/feed", resolve=False)
    await validate_public_url("https://1.1.1.1/", resolve=False)


@pytest.mark.asyncio
async def test_validate_skips_dns_when_resolve_false() -> None:
    """``resolve=False`` must not invoke ``_resolve``. We assert by
    swapping in a sentinel that would explode if called — this is the
    contract POST /v1/feeds depends on so registration latency stays
    bounded and tests do not need a network resolver."""

    async def explode(host: str) -> list[str]:  # pragma: no cover - guard
        raise AssertionError(f"_resolve was called for {host}")

    original = ssrf._resolve
    ssrf._resolve = explode
    try:
        await validate_public_url("http://example.com/feed", resolve=False)
    finally:
        ssrf._resolve = original


@pytest.mark.asyncio
async def test_validate_blocks_dns_rebinding_to_private_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TC-D3-01: a perfectly innocent-looking hostname whose DNS
    record points at ``10.x`` must be rejected at fetch time."""

    async def fake_resolve(host: str) -> list[str]:
        return ["10.0.0.5"]

    monkeypatch.setattr(ssrf, "_resolve", fake_resolve)
    with pytest.raises(BlockedURLError, match="resolves to blocked"):
        await validate_public_url("http://attacker.example.com/feed")


@pytest.mark.asyncio
async def test_validate_blocks_when_any_resolved_addr_is_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a hostname returns multiple A records and *any one* is
    private, the URL is blocked. This catches the dual-stack rebinding
    trick where one record is public and another is internal."""

    async def fake_resolve(host: str) -> list[str]:
        return ["8.8.8.8", "10.0.0.5"]

    monkeypatch.setattr(ssrf, "_resolve", fake_resolve)
    with pytest.raises(BlockedURLError):
        await validate_public_url("http://mixed.example.com/feed")


@pytest.mark.asyncio
async def test_validate_allows_public_dns_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_resolve(host: str) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr(ssrf, "_resolve", fake_resolve)
    await validate_public_url("http://example.com/feed")


@pytest.mark.asyncio
async def test_validate_allows_unresolvable_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolution failure (NXDOMAIN, no A record) is *allowed*. The
    fetch will fail naturally and we do not want a flaky resolver to
    drop registrations. The other layers still apply."""

    async def fake_resolve(host: str) -> list[str]:
        return []

    monkeypatch.setattr(ssrf, "_resolve", fake_resolve)
    await validate_public_url("http://no-such-host.test/feed")


# ---------------------------------------------------------------------------
# Unit tests — SSRFGuardTransport
# ---------------------------------------------------------------------------


class _SpyTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(str(request.url))
        return httpx.Response(200)

    async def aclose(self) -> None:
        pass


@pytest.mark.asyncio
async def test_transport_guard_blocks_private_request_before_inner() -> None:
    inner = _SpyTransport()
    guard = SSRFGuardTransport(inner)
    request = httpx.Request("GET", "http://10.0.0.1/feed")

    with pytest.raises(BlockedURLError):
        await guard.handle_async_request(request)
    assert inner.calls == []  # inner transport was never reached


@pytest.mark.asyncio
async def test_transport_guard_passes_public_request_to_inner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_resolve(host: str) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr(ssrf, "_resolve", fake_resolve)
    inner = _SpyTransport()
    guard = SSRFGuardTransport(inner)
    request = httpx.Request("GET", "http://example.com/feed")

    response = await guard.handle_async_request(request)
    assert response.status_code == 200
    assert inner.calls == ["http://example.com/feed"]


@pytest.mark.asyncio
async def test_transport_guard_blocks_redirect_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TC-D4-01: a public URL that 302s to a private IP must be
    blocked at the **second** request, before the inner transport is
    even asked. We simulate httpx's redirect-follow loop by hand by
    pushing two requests through the same guard.
    """

    resolved_addrs = {
        "public.example.com": ["93.184.216.34"],
        "internal.example.com": ["10.0.0.5"],
    }

    async def fake_resolve(host: str) -> list[str]:
        return resolved_addrs.get(host, [])

    monkeypatch.setattr(ssrf, "_resolve", fake_resolve)

    inner = _SpyTransport()
    guard = SSRFGuardTransport(inner)

    # First hop is public — should pass and reach inner.
    first = httpx.Request("GET", "http://public.example.com/feed")
    await guard.handle_async_request(first)
    assert inner.calls == ["http://public.example.com/feed"]

    # Second hop (the redirect httpx would follow) goes to a private
    # host. The guard refuses it without invoking the inner transport.
    second = httpx.Request("GET", "http://internal.example.com/feed")
    with pytest.raises(BlockedURLError):
        await guard.handle_async_request(second)
    assert inner.calls == ["http://public.example.com/feed"]  # unchanged


# ---------------------------------------------------------------------------
# Integration — POST /v1/feeds rejects blocked URLs with 400
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.1/feed",  # TC-D1-01
        "http://127.0.0.1/feed",  # TC-D1-02
        "http://169.254.169.254/latest/meta-data/",  # TC-D2-01
        "file:///etc/passwd",
    ],
)

exec
/bin/zsh -lc "sed -n '1,220p' tests/test_api_auth.py" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
"""Tests for optional API key authentication."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from feedgate.api import register_routers
from feedgate.config import Settings


@pytest_asyncio.fixture
async def auth_app(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> FastAPI:
    """App with api_key configured."""
    settings = Settings()
    app = FastAPI()
    app.state.session_factory = async_session_factory
    app.state.api_key = "test-secret"
    app.state.api_entries_max_feed_ids = settings.api_entries_max_feed_ids
    app.state.api_entries_default_limit = settings.api_entries_default_limit
    app.state.api_entries_max_limit = settings.api_entries_max_limit
    app.state.api_feeds_max_limit = settings.api_feeds_max_limit
    register_routers(app)
    return app


@pytest_asyncio.fixture
async def auth_client(
    auth_app: FastAPI,
    truncate_tables: None,
) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=auth_app),
        base_url="http://test",
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_missing_key_returns_401(auth_client: AsyncClient) -> None:
    """POST /v1/feeds without X-Api-Key must return 401."""
    response = await auth_client.post(
        "/v1/feeds",
        json={"url": "https://example.com/feed.xml"},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid_api_key"


@pytest.mark.asyncio
async def test_wrong_key_returns_401(auth_client: AsyncClient) -> None:
    """POST /v1/feeds with a wrong X-Api-Key must return 401."""
    response = await auth_client.post(
        "/v1/feeds",
        json={"url": "https://example.com/feed.xml"},
        headers={"X-Api-Key": "wrong-key"},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid_api_key"


@pytest.mark.asyncio
async def test_correct_key_passes(auth_client: AsyncClient) -> None:
    """POST /v1/feeds with the correct X-Api-Key must not return 401."""
    response = await auth_client.post(
        "/v1/feeds",
        json={"url": "https://example.com/feed.xml"},
        headers={"X-Api-Key": "test-secret"},
    )
    assert response.status_code != 401


@pytest.mark.asyncio
async def test_health_no_key_required(auth_app: FastAPI) -> None:
    """GET /health must return 200 even when auth is enabled and no key is sent."""
    async with AsyncClient(
        transport=ASGITransport(app=auth_app),
        base_url="http://test",
    ) as client:
        response = await client.get("/healthz")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_no_auth_when_key_empty(api_client: AsyncClient) -> None:
    """When api_key is empty, /v1/feeds works without any key header."""
    response = await api_client.post(
        "/v1/feeds",
        json={"url": "https://example.com/feed.xml"},
    )
    # 201 Created or 409 Conflict are both fine — anything except 401
    assert response.status_code != 401

exec
/bin/zsh -lc "sed -n '1,220p' tests/test_errors.py" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from feedgate.errors import PROBLEM_JSON_MEDIA_TYPE, register_exception_handlers


@pytest.fixture(autouse=True)
def _register_problem_handlers(api_app: FastAPI) -> None:
    register_exception_handlers(api_app)


@pytest.mark.asyncio
async def test_blocked_url_returns_problem_details(api_client: AsyncClient) -> None:
    resp = await api_client.post("/v1/feeds", json={"url": "http://10.0.0.1/feed"})
    body = resp.json()

    assert resp.status_code == 400
    assert resp.headers["content-type"].startswith(PROBLEM_JSON_MEDIA_TYPE)
    assert {"type", "title", "status", "detail", "instance"}.issubset(body.keys())
    assert body["status"] == 400
    assert body["type"] == "about:blank"
    assert body["title"] == "Bad Request"
    assert "blocked_url" in body["detail"]
    assert body["instance"] == "/v1/feeds"


@pytest.mark.asyncio
async def test_feed_not_found_returns_problem_details(api_client: AsyncClient) -> None:
    resp = await api_client.get("/v1/feeds/99999")
    body = resp.json()

    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith(PROBLEM_JSON_MEDIA_TYPE)
    assert body["status"] == 404
    assert body["title"] == "Not Found"
    assert body["detail"] == "feed not found"
    assert body["instance"] == "/v1/feeds/99999"


@pytest.mark.asyncio
async def test_invalid_cursor_on_entries_returns_problem_details(api_client: AsyncClient) -> None:
    resp = await api_client.get("/v1/entries?feed_ids=1&cursor=!!!bogus!!!")
    body = resp.json()

    assert resp.status_code == 400
    assert resp.headers["content-type"].startswith(PROBLEM_JSON_MEDIA_TYPE)
    assert body["detail"] == "invalid cursor"


@pytest.mark.asyncio
async def test_validation_error_returns_problem_details(api_client: AsyncClient) -> None:
    resp = await api_client.post("/v1/feeds", json={"url": ""})
    body = resp.json()

    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith(PROBLEM_JSON_MEDIA_TYPE)
    assert body["title"] == "Unprocessable Entity"
    assert body["status"] == 422
    assert "url" in body["detail"]

exec
/bin/zsh -lc "sed -n '1,260p' tests/test_lifespan_drain.py" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
"""Coverage for the lifespan drain helper.

PR #15 added a graceful shutdown path but only tested the
``scheduler.run`` side of the contract. Codex review on PR #15 flagged
two gaps:

  1. The lifespan-side drain logic (``wait_for(task, drain_budget)`` →
     force-cancel) was never exercised. A regression that flipped to
     plain ``task.cancel()`` would still pass the existing tests.
  2. The in-flight test set ``stop_event`` from inside a respx
     side-effect that returned 200 immediately, so a hypothetical
     cancel-on-stop implementation could still pass it by sheer
     timing — the in-flight state was barely held.

This file plugs both gaps. The drain helper is now exposed at module
level (``feedgate.main._drain_background_task``) so we can unit-test
it directly without spinning up a FastAPI app, and the in-flight
test holds the side-effect on an explicit ``release`` event so the
fetch is provably mid-flight when shutdown fires.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
import respx
from fastapi import FastAPI
from httpx import Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from feedgate.fetcher import scheduler
from feedgate.main import _drain_background_task
from feedgate.models import Feed

# ---------------------------------------------------------------------------
# Unit tests on _drain_background_task — no scheduler, no DB.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_background_task_returns_for_none_task() -> None:
    """A None task is a no-op — exercised by the lifespan when the
    feature flag for that loop is disabled."""
    stop = asyncio.Event()
    await _drain_background_task(None, stop, name="scheduler", drain_seconds=1.0)
    assert not stop.is_set(), "must not touch the event for a None task"


@pytest.mark.asyncio
async def test_drain_background_task_clean_exit_within_budget() -> None:
    """A well-behaved loop that exits as soon as the stop event fires
    must NOT trigger the force-cancel branch."""
    stop = asyncio.Event()

    async def loop() -> None:
        # Mimic scheduler.run: wait for stop, then return.
        await stop.wait()

    task = asyncio.create_task(loop())
    await _drain_background_task(task, stop, name="scheduler", drain_seconds=1.0)

    assert task.done()
    assert not task.cancelled()
    assert task.exception() is None


@pytest.mark.asyncio
async def test_drain_background_task_force_cancels_on_timeout() -> None:
    """A misbehaving loop that ignores the stop event must be force-
    cancelled after the drain budget elapses. The cancel side-effect
    is the load-bearing assertion — we deliberately don't depend on
    caplog because pytest log-capture interacts oddly with how other
    tests in the suite reconfigure the root logger."""
    stop = asyncio.Event()

    async def hung_loop() -> None:
        # Never returns. ``stop`` is set but the loop ignores it.
        await asyncio.sleep(60)

    task = asyncio.create_task(hung_loop())

    await _drain_background_task(
        task,
        stop,
        name="scheduler",
        drain_seconds=0.05,
    )

    assert task.done()
    assert task.cancelled()


@pytest.mark.asyncio
async def test_drain_background_task_swallows_loop_exception() -> None:
    """If the loop raises during drain (not on cancel), the helper
    must absorb it and return — the lifespan exit MUST NOT propagate
    background task exceptions or the process will not shut down
    cleanly."""
    stop = asyncio.Event()

    async def boom() -> None:
        await stop.wait()
        raise RuntimeError("kaboom")

    task = asyncio.create_task(boom())

    # The helper must NOT raise here. If it does, this line propagates
    # and the test fails with the original RuntimeError, which is the
    # exact behavior we are guarding against.
    await _drain_background_task(
        task,
        stop,
        name="scheduler",
        drain_seconds=1.0,
    )

    assert task.done()


# ---------------------------------------------------------------------------
# End-to-end drain on a real scheduler.run with a HELD in-flight fetch.
# ---------------------------------------------------------------------------


def _atom_with(guid: str) -> bytes:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>drain held</title>
  <id>{guid}</id>
  <updated>2026-04-12T00:00:00Z</updated>
  <entry>
    <title>x</title>
    <id>{guid}/post-1</id>
    <link href="{guid}/post-1"/>
    <published>2026-04-12T00:00:00Z</published>
    <content>x</content>
  </entry>
</feed>
""".encode()


@pytest.mark.asyncio
async def test_drain_waits_for_truly_in_flight_fetch_to_complete(
    fetch_app: FastAPI,
    respx_mock: respx.Router,
) -> None:
    """Stronger version of the existing in-flight drain test. The
    side-effect blocks on an explicit ``release`` event so the fetch
    is *provably* mid-flight when shutdown fires. A cancel-on-stop
    implementation would interrupt the fetch and the feed would
    never get ``last_successful_fetch_at``; the cooperative drain
    sees the fetch through to completion.

    Sequence:
      1. Spawn ``scheduler.run(stop_event=stop)`` as a task.
      2. ``in_flight`` event fires when the side-effect enters →
         we KNOW the fetch is currently inside http_client.stream.
      3. Set ``stop`` while the fetch is held.
      4. Briefly assert the loop has NOT exited yet (still draining).
      5. Release the side-effect.
      6. Loop drains, returns. Assert the feed was fully fetched.
    """
    sf: async_sessionmaker[AsyncSession] = fetch_app.state.session_factory
    fetch_app.state.fetch_interval_seconds = 0.05
    feed_url = "http://t.test/held-drain/feed"
    seed_now = datetime.now(UTC) - timedelta(seconds=5)
    async with sf() as session:
        session.add(Feed(url=feed_url, effective_url=feed_url, next_fetch_at=seed_now))
        await session.commit()

    in_flight = asyncio.Event()
    release = asyncio.Event()

    async def held_response(request: Request) -> Response:
        in_flight.set()
        await release.wait()
        return Response(
            200,
            content=_atom_with(feed_url),
            headers={"Content-Type": "application/atom+xml"},
        )

    respx_mock.get(feed_url).mock(side_effect=held_response)

    stop = asyncio.Event()
    task = asyncio.create_task(scheduler.run(fetch_app, stop_event=stop))

    # Wait until the fetch is REALLY mid-call.
    await asyncio.wait_for(in_flight.wait(), timeout=2.0)

    # Signal shutdown while the fetch is still held.
    stop.set()
    # Give the loop a few ticks of the event loop to observe stop.
    await asyncio.sleep(0.05)
    assert not task.done(), "loop exited before the held fetch was released"

    # Now let the fetch complete.
    release.set()

    await asyncio.wait_for(task, timeout=3.0)
    assert task.exception() is None

    async with sf() as session:
        feed = (await session.execute(select(Feed).where(Feed.url == feed_url))).scalar_one()
    assert feed.last_successful_fetch_at is not None, (
        "drain interrupted the in-flight fetch — cooperative drain is broken"
    )
    assert feed.last_error_code is None

exec
/bin/zsh -lc "sed -n '1,240p' docs/adr/003-collection-is-internal-implementation.md" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
# ADR 003 — 수집 방식은 내부 구현

- 상태: Accepted
- 작성일: 2026-04-10
- 관련: ADR 000, ADR 001 (예정), ADR 002 (예정), `docs/notes/collection-scaling-research.md`

## 컨텍스트

이 서비스가 하는 일의 중심은 **피드를 주기적으로 fetch**하는 것이다.
그러나 "어떻게 주기적으로 fetch할지"에는 수많은 선택지가 있다.

- 고정 주기 / 적응형 주기
- 글로벌 동시성 제한 / per-host 동시성 제한
- 단일 프로세스 / 분산 워커
- DB 클레임 / 외부 큐 (Redis, NATS, SQS 등)
- 폴링 전용 / WebSub 혼합

각 선택지의 트레이드오프는 **실제 부하 데이터 없이 사전에 결정할 수
없다**. 피드 수, 호스트 분포, 평균 발행 빈도, 장애 패턴이 전부 다르기
때문이다. 조사 내용은 `docs/notes/collection-scaling-research.md`에 보존해
두었다.

동시에 ADR 000은 "경계는 HTTP 계약뿐"이라고 못박았다. 외부 세계가 보는
계약은 API 응답뿐이고, 그 밑의 수집 엔진이 무엇이든 외부에는 관계없다.

## 결정

**수집 방식에 관한 결정은 ADR로 박지 않는다.** 수집 엔진은 교체 가능한
내부 구현이며, 다음 두 계약을 깨지 않는 한 자유롭게 바꿀 수 있다.

- ADR 001: 핵심 엔티티 스키마 (feed, entry의 **본질 컬럼**)
- ADR 002: HTTP API 계약

"핵심 엔티티 스키마"는 "이 서비스가 무엇을 저장·노출하는가"를 정의하는
컬럼만을 뜻한다. 스케줄러가 자기 동작을 위해 필요로 하는 메타데이터
컬럼(`next_fetch_at`, `etag`, `last_modified`, `consecutive_failures`,
`hub_url` 등)은 **핵심 엔티티 스키마에 속하지 않으며**, 수집 방식이 바뀔
때 additive하게 추가·제거할 수 있는 구현 세부사항이다.

## MVP가 갖춰야 할 최소 위생

수집 방식을 "자유롭게 교체 가능"하게 두되, 다음 항목은 "복잡도"가 아니라
"기본 위생"이므로 MVP에서도 반드시 포함한다. 없으면 한 피드가 전체
서비스를 고장낼 수 있다.

| 항목 | 이유 |
|---|---|
| HTTP 요청 타임아웃 | 슬로우 피드로 워커 고갈 방지 |
| 응답 본문 크기 상한 | 거대 피드로 메모리 폭주 방지 |
| 식별 가능한 User-Agent | 차단 협상 여지, 상대 운영자 식별 |
| 전역 동시성 상한 | 자원 사용 상한 |
| ETag / If-Modified-Since 조건부 요청 | ADR 000의 기능 목록에 이미 포함 |
| `(feed_id, guid)` 유니크 제약 | 중복 저장 방지 |

구체 수치(타임아웃 초, 크기 MB, 동시성, UA 문자열 등)는 **이 ADR의
결정 대상이 아니다**. `docs/spec/`과 환경변수로 관리한다.

## 결과

- 수집 전략(고정 주기 → 적응형 → WebSub → 분산 등)을 도입·교체할 때는
  **코드 변경과 DB 마이그레이션**으로 진행하며, 그 자체에 대해 ADR을
  쓰지 않는다.
- 스케줄러 메타데이터 컬럼은 **additive**하게만 성장시킨다. 기존 컬럼
  삭제나 의미 변경은 금지. 필요하면 새 컬럼을 추가하고 옛 컬럼은 사용
  중단 후 별도 마이그레이션으로 정리한다.
- 수집 전략 선택지가 필요해질 때는 `docs/notes/collection-scaling-
  research.md`의 "재검토 트리거"를 확인하고, 트리거가 충족되었다면 해당
  기법의 도입을 검토한다.
- 만약 수집 방식 변경이 ADR 001/002의 계약을 깨야만 가능하다면, 그것은
  수집 방식 결정이 아니라 **계약 변경**이므로 해당 ADR을 개정한다.

## 기각한 대안

- **기법별로 ADR을 미리 다 만든다** (적응형 주기 ADR, per-host ADR,
  WebSub ADR, Claimer ADR 등)
  기각: YAGNI. 실제 부하 없이 파라미터를 정하면 운영 중 전부 재튜닝하게
  되고, ADR이 "근거 없는 숫자의 저장소"가 된다. ADR은 결정의 역사이지
  구현 상세의 창고가 아니다.

- **아무 방어 없는 나이브 MVP** (타임아웃/크기 상한도 나중에)
  기각: "단순함"과 "취약함"은 다르다. 위 최소 위생 항목은 복잡도가 거의
  없고, 빠지면 단일 피드 하나가 서비스 전체를 멈출 수 있다.

- **모든 수집 파라미터(주기, 동시성, UA 문자열)를 ADR에 박기**
  기각: 수치는 운영 중 자주 조정된다. ADR에 박으면 조정할 때마다 ADR을
  고치게 되어 결정 로그의 신뢰성이 무너진다. 수치는 spec/env의 영역이다.

- **"수집 전략"을 ADR 000 범위에서 제외한다고 암묵적으로만 두기**
  기각: 명시하지 않으면 나중에 누군가가 "적응형 주기 도입" 같은 구현
  작업을 시작하면서 새 ADR을 쓰려고 한다. 이 ADR은 "그것들은 ADR로 다루지
  않는다"를 명시적으로 박아두어 그런 시도를 사전에 차단한다.

## 미해결

없음. 이 ADR은 "무엇을 결정하지 않기로 결정했는가"를 박는 문서이며,
구체 수집 전략은 구현 단계에서 코드와 spec으로 다룬다.

exec
/bin/zsh -lc "sed -n '1,260p' docs/adr/004-entry-retention-policy.md" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
# ADR 004 — 엔트리 보존 정책

- 상태: Accepted
- 작성일: 2026-04-10
- 관련: ADR 000, ADR 001 (예정)

## 컨텍스트

ADR 000은 "이 서비스는 최신 피드 캐시이며 아카이브가 아니다"라고 명시
했고, 그에 따라 "자동 보존 관리"를 기능 목록에 포함했다. 그러나 구체적인
보존 규칙은 정해지지 않았다.

보존 규칙을 설계하려면 다음 세 가지를 정해야 한다.

1. 어떤 시각을 기준으로 "오래되었다"를 판단할 것인가
2. 느리게 발행되는 피드가 보존 정책 때문에 "텅 빈" 상태가 되는 문제를
   어떻게 처리할 것인가
3. 삭제된 엔트리가 피드에 여전히 남아 있을 때 재삽입을 허용할 것인가

이 ADR은 이 세 가지에 대한 **정책적 결정**만 내린다. 구체 수치(며칠, 몇
개)는 이 ADR의 결정 대상이 **아니다**.

## 결정

### 1. 기준 시각은 `fetched_at`

"얼마나 오래된 엔트리인가"는 우리가 **fetch한 시각**(`fetched_at`)을 기준
으로 판단한다. 피드가 제공하는 `published_at`은 보존 판단에 사용하지
않는다.

이유:

- `published_at`은 피드가 거짓말할 수 있다. 미래 날짜, 아주 오래된 날짜,
  빈 값이 현실에서 흔하다. 우리가 통제하지 못하는 값을 보존 판단의 핵심
  기준으로 삼을 수 없다.
- `fetched_at`은 우리가 쓴 값이라 항상 존재하고, 의미가 "우리가 가진 지
  얼마나 되었는가"로 명확하다.
- "이 서비스는 캐시다"라는 ADR 000의 철학과 정확히 일치한다: **우리가
  가진 지 일정 기간이 지난 것을 버린다.**

`published_at`은 엔트리 **정렬·조회**에는 계속 사용한다. 보존 판단에만
쓰지 않는다.

### 2. 피드당 최소 유지 개수 보장

순수 시간 기반 삭제는 발행이 드문 피드(분기별 뉴스레터, 연간 보고서,
오래된 블로그 등)에서 "엔트리 0건" 상태를 만든다. 사용자가 피드를 열면
아무것도 보이지 않는 UX 절벽이 생긴다.

이를 피하기 위해 **피드당 가장 최근 N개는 나이와 무관하게 유지**한다.
실제 삭제 규칙은 **합집합**이다.

> 각 피드에 대해, 다음 두 집합의 합집합을 유지한다.
> 1. 최근 M일 이내에 fetch한 엔트리 전부
> 2. 그 피드에서 가장 최근에 fetch한 N개 엔트리
>
> 이 합집합에 속하지 않는 엔트리만 삭제한다.

이 규칙은 여전히 저장 규모를 구조적으로 제한한다: 최대치는 대략
`피드 수 × (M일 동안의 fetch량 + N)`이므로 무제한 성장이 아니다.

### 3. 재삽입 허용, tombstone 없음

보존 기간이 지나 삭제된 엔트리가 피드에서 여전히 보이면, 다음 fetch 때
자연스럽게 재삽입된다. 삭제된 guid를 기억하기 위한 별도 tombstone 테이블
은 두지 않는다.

이유:

- "캐시는 무언가를 기억하지 않아야 한다"는 원칙과 일치.
- Tombstone 테이블 자체도 보존 정책이 필요해져 복잡도가 중첩된다.
- 재삽입으로 생기는 부작용(내부 `id`가 바뀜)은 API 계약에서 `guid`를
  안정 식별자로 노출함으로써 관리한다. 이것은 ADR 002(API 계약)에서
  다룬다.

## 결과

### 스키마 요구사항 (ADR 001이 반드시 반영해야 할 것)

- `entries.fetched_at`은 **NOT NULL**이다. 보존 판단의 기준이므로 반드시
  존재해야 한다.
- `entries(fetched_at)` 인덱스가 필요하다. 주기적 삭제 스윕을 효율적으로
  수행하기 위함.
- 피드당 최근 N개 판정을 위해 `entries(feed_id, fetched_at DESC)` 인덱스가
  조회·보존 양쪽에 유용하다(ADR 001에서 최종 형태를 결정).
- 그 외 추가 컬럼이나 테이블은 요구하지 않는다. Tombstone 테이블 없음.

### 운영 파라미터 (이 ADR의 결정 대상 아님)

구체 수치는 `docs/spec/`과 환경변수로 관리한다. 운영 중 자유롭게 조정할
수 있으며, 조정할 때 이 ADR을 개정할 필요는 없다.

파라미터 목록:

- 시간 기반 유지 기간 (M일)
- 피드당 최소 유지 개수 (N개)
- 삭제 스윕 주기

### 철학적 정합성

- ADR 000의 "캐시이며 아카이브가 아니다"와 일치: 유한 기간 + 유한 개수.
- 저장 규모가 구조적으로 제한되므로 파티셔닝·아카이브 전략이 필요 없다.
- Tombstone 없음은 "캐시는 기억하지 않는다"는 원칙과 일치.
- 기준 시각이 `fetched_at`이라는 선택은 "우리가 통제하는 값만 신뢰한다"는
  크롤러/파이프라인 일반 원칙과 일치.

## 기각한 대안

- **`published_at` 기준으로 보존 판단**
  기각: 피드가 제공하는 값을 신뢰할 수 없다. 미래 날짜, 과거 날짜, 빈 값이
  모두 흔하다. 보존 판단처럼 데이터 삭제로 이어지는 규칙은 우리가 통제
  하는 값으로만 해야 한다.

- **`COALESCE(published_at, fetched_at)` 또는 `GREATEST(...)` 조합**
  기각: 견고성은 약간 올라가지만 규칙의 의미가 모호해진다(어느 쪽이
  이겼는지 상황별). `fetched_at` 단일 기준이 "우리가 가진 지 M일"이라는
  명확한 의미를 가진다.

- **피드당 최소 개수 없이 시간만**
  기각: 발행이 드문 피드에서 "엔트리 0건" UX 절벽이 생긴다. 이 서비스의
  본질이 "최신 피드 캐시"라 해도, 피드당 몇십 개 수준은 여전히 "캐시"지
  "아카이브"가 아니다.

- **Tombstone 테이블로 재삽입 방지**
  기각: 캐시 철학 위반. 테이블 자체에도 보존 정책이 필요해져 복잡도가
  중첩된다. 재삽입 부작용은 `guid` 안정성 계약으로 충분히 관리 가능하다.

- **무기한 보존 / 아카이브**
  기각: ADR 000이 이미 기각한 대안이다.

## 미해결

- 삭제 스윕 구현 방식(배치 DELETE vs 파티션 drop 등) — 구현 단계에서
  `docs/spec/`으로 결정한다.
- 보존 정책으로 인해 "이 피드는 오래된 글이 잘렸다"를 사용자에게 API
  응답에서 알릴 필요가 있는지 여부 — 필요해지면 ADR 002(API 계약)에서
  다룬다.

exec
/bin/zsh -lc "sed -n '1,220p' tests/test_parser.py" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
"""Feed parser wrapper tests.

Validates that feedparser → ParsedFeed/ParsedEntry mapping works for
both Atom and RSS 2.0, with reasonable behaviour on missing fields.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from feedgate.fetcher.parser import parse_feed

ATOM_SAMPLE = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Sample Atom Feed</title>
  <id>http://example.com/</id>
  <updated>2026-04-10T00:00:00Z</updated>
  <entry>
    <title>First Post</title>
    <id>http://example.com/posts/1</id>
    <link href="http://example.com/posts/1"/>
    <published>2026-04-10T00:00:00Z</published>
    <updated>2026-04-10T00:00:00Z</updated>
    <content>First content</content>
    <author><name>Alice</name></author>
  </entry>
  <entry>
    <title>Second Post</title>
    <id>http://example.com/posts/2</id>
    <link href="http://example.com/posts/2"/>
    <published>2026-04-10T01:00:00Z</published>
    <updated>2026-04-10T01:00:00Z</updated>
    <summary>Second summary</summary>
  </entry>
</feed>
"""

RSS_SAMPLE = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
  <channel>
    <title>Sample RSS Feed</title>
    <link>http://example.com/</link>
    <description>desc</description>
    <item>
      <title>RSS Post</title>
      <guid>http://example.com/rss/1</guid>
      <link>http://example.com/rss/1</link>
      <pubDate>Thu, 10 Apr 2026 00:00:00 +0000</pubDate>
      <description>RSS content</description>
      <author>bob@example.com (Bob)</author>
    </item>
  </channel>
</rss>
"""


@pytest.mark.asyncio
async def test_parse_atom_basic() -> None:
    feed = await parse_feed(ATOM_SAMPLE)
    assert feed.title == "Sample Atom Feed"
    assert len(feed.entries) == 2

    first = feed.entries[0]
    assert first.guid == "http://example.com/posts/1"
    assert first.url == "http://example.com/posts/1"
    assert first.title == "First Post"
    assert first.content == "First content"
    assert first.author == "Alice"
    assert isinstance(first.published_at, datetime)

    second = feed.entries[1]
    assert second.guid == "http://example.com/posts/2"
    # Summary falls back into content
    assert second.content == "Second summary"


@pytest.mark.asyncio
async def test_parse_rss_basic() -> None:
    feed = await parse_feed(RSS_SAMPLE)
    assert feed.title == "Sample RSS Feed"
    assert len(feed.entries) == 1
    entry = feed.entries[0]
    assert entry.guid == "http://example.com/rss/1"
    assert entry.url == "http://example.com/rss/1"
    assert entry.title == "RSS Post"
    assert entry.content == "RSS content"
    assert isinstance(entry.published_at, datetime)


@pytest.mark.asyncio
async def test_parse_entry_with_missing_guid_falls_back_to_link() -> None:
    xml = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
  <channel>
    <title>T</title>
    <link>http://example.com/</link>
    <description>d</description>
    <item>
      <title>No guid</title>
      <link>http://example.com/no-guid</link>
      <description>body</description>
    </item>
  </channel>
</rss>
"""
    feed = await parse_feed(xml)
    assert len(feed.entries) == 1
    # feedparser copies <link> into .id when no <guid> is present
    assert feed.entries[0].guid == "http://example.com/no-guid"


@pytest.mark.asyncio
async def test_parse_entry_with_missing_published_at() -> None:
    xml = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>T</title><id>http://e.com/</id>
  <updated>2026-04-10T00:00:00Z</updated>
  <entry>
    <title>No date</title>
    <id>x1</id>
    <link href="http://e.com/x1"/>
  </entry>
</feed>
"""
    feed = await parse_feed(xml)
    entry = feed.entries[0]
    # Either None or a datetime is acceptable (feedparser may fall back
    # to the feed's <updated>).
    assert entry.published_at is None or isinstance(entry.published_at, datetime)


@pytest.mark.asyncio
async def test_parse_empty_entries() -> None:
    xml = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Empty</title>
  <id>http://e.com/</id>
  <updated>2026-04-10T00:00:00Z</updated>
</feed>
"""
    feed = await parse_feed(xml)
    assert feed.title == "Empty"
    assert feed.entries == []

exec
/bin/zsh -lc "sed -n '1,240p' tests/test_upsert.py" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
"""Entry upsert semantics — spec/entry.md mutation policy.

Covers the four cases from plan WP 1.5:

(a) new entry INSERT -> fetched_at == content_updated_at
(b) re-upsert identical payload -> no-op (both timestamps unchanged)
(c) re-upsert with any content field changed -> UPDATE affected fields
    + content_updated_at = now(), but fetched_at stays immutable
(d) (feed_id, guid) UNIQUE is not violated across repeated upserts
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from feedgate.fetcher.upsert import ParsedEntry, upsert_entries
from feedgate.models import Entry, Feed


@pytest_asyncio.fixture
async def feed(async_session: AsyncSession) -> Feed:
    """Insert a fresh feed row and return it. Rolled back per-test."""
    row = Feed(url="http://fake.test/feed.xml", effective_url="http://fake.test/feed.xml")
    async_session.add(row)
    await async_session.flush()
    return row


async def _entry_row(session: AsyncSession, feed_id: int, guid: str) -> Entry:
    result = await session.execute(
        select(Entry).where(Entry.feed_id == feed_id, Entry.guid == guid)
    )
    return result.scalar_one()


async def _entry_snapshot(session: AsyncSession, feed_id: int, guid: str) -> dict[str, Any]:
    """Read entry fields as a plain dict, bypassing the ORM identity map.

    Used in the "upsert twice" tests so the second read isn't tainted by
    the cached ORM instance from the first read (which would otherwise
    trigger lazy reload and fail inside async-session context).
    """
    result = await session.execute(
        select(
            Entry.title,
            Entry.content,
            Entry.author,
            Entry.url,
            Entry.published_at,
            Entry.fetched_at,
            Entry.content_updated_at,
        ).where(Entry.feed_id == feed_id, Entry.guid == guid)
    )
    row = result.one()
    return {
        "title": row.title,
        "content": row.content,
        "author": row.author,
        "url": row.url,
        "published_at": row.published_at,
        "fetched_at": row.fetched_at,
        "content_updated_at": row.content_updated_at,
    }


async def _count_entries(session: AsyncSession, feed_id: int) -> int:
    result = await session.execute(
        select(func.count()).select_from(Entry).where(Entry.feed_id == feed_id)
    )
    return int(result.scalar_one())


@pytest.mark.asyncio
async def test_upsert_new_entry_sets_both_timestamps_equal(
    async_session: AsyncSession, feed: Feed
) -> None:
    t0 = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)
    parsed = ParsedEntry(
        guid="guid-a",
        url="http://fake.test/posts/a",
        title="Hello",
        content="Body",
        author="Alice",
        published_at=datetime(2026, 4, 10, 11, 0, 0, tzinfo=UTC),
    )

    await upsert_entries(async_session, feed.id, [parsed], now=t0)

    row = await _entry_row(async_session, feed.id, "guid-a")
    assert row.title == "Hello"
    assert row.content == "Body"
    assert row.author == "Alice"
    assert row.fetched_at == t0
    assert row.content_updated_at == t0


@pytest.mark.asyncio
async def test_upsert_identical_payload_is_noop(async_session: AsyncSession, feed: Feed) -> None:
    t0 = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)
    t1 = t0 + timedelta(hours=1)
    parsed = ParsedEntry(
        guid="guid-a",
        url="http://fake.test/posts/a",
        title="Hello",
        content="Body",
        author="Alice",
        published_at=datetime(2026, 4, 10, 11, 0, 0, tzinfo=UTC),
    )

    await upsert_entries(async_session, feed.id, [parsed], now=t0)
    await upsert_entries(async_session, feed.id, [parsed], now=t1)

    row = await _entry_row(async_session, feed.id, "guid-a")
    # Both timestamps must be unchanged (no-op) because content is identical.
    assert row.fetched_at == t0
    assert row.content_updated_at == t0
    # And exactly one row exists (UNIQUE constraint honored).
    assert await _count_entries(async_session, feed.id) == 1


@pytest.mark.asyncio
async def test_upsert_changed_title_updates_content_updated_at_only(
    async_session: AsyncSession, feed: Feed
) -> None:
    t0 = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)
    original = ParsedEntry(
        guid="guid-a",
        url="http://fake.test/posts/a",
        title="Hello",
        content="Body",
        author="Alice",
        published_at=datetime(2026, 4, 10, 11, 0, 0, tzinfo=UTC),
    )
    edited = ParsedEntry(
        guid="guid-a",
        url="http://fake.test/posts/a",
        title="Hello (edited)",
        content="Body",
        author="Alice",
        published_at=datetime(2026, 4, 10, 11, 0, 0, tzinfo=UTC),
    )

    await upsert_entries(async_session, feed.id, [original], now=t0)
    before = await _entry_snapshot(async_session, feed.id, "guid-a")

    # Use a later clock for the second call; the UPDATE branch sets
    # content_updated_at via SQL now() so the actual value will be
    # greater than the original regardless of the `now` parameter, but
    # fetched_at must stay frozen.
    await upsert_entries(
        async_session,
        feed.id,
        [edited],
        now=t0 + timedelta(hours=2),
    )
    after = await _entry_snapshot(async_session, feed.id, "guid-a")

    assert after["title"] == "Hello (edited)"
    assert after["fetched_at"] == before["fetched_at"]  # immutable
    assert after["content_updated_at"] > before["content_updated_at"]  # advanced
    assert await _count_entries(async_session, feed.id) == 1


@pytest.mark.asyncio
async def test_upsert_changed_published_at_also_updates(
    async_session: AsyncSession, feed: Feed
) -> None:
    """Cover the published_at branch of the distinct-from check."""
    t0 = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)
    original = ParsedEntry(
        guid="guid-a",
        url="http://fake.test/posts/a",
        title="Hello",
        content="Body",
        author="Alice",
        published_at=datetime(2026, 4, 10, 11, 0, 0, tzinfo=UTC),
    )
    edited = ParsedEntry(
        guid="guid-a",
        url="http://fake.test/posts/a",
        title="Hello",
        content="Body",
        author="Alice",
        published_at=datetime(2026, 4, 10, 11, 30, 0, tzinfo=UTC),
    )

    await upsert_entries(async_session, feed.id, [original], now=t0)
    before = await _entry_snapshot(async_session, feed.id, "guid-a")

    await upsert_entries(
        async_session,
        feed.id,
        [edited],
        now=t0 + timedelta(hours=2),
    )
    after = await _entry_snapshot(async_session, feed.id, "guid-a")

    assert after["published_at"] == datetime(2026, 4, 10, 11, 30, 0, tzinfo=UTC)
    assert after["fetched_at"] == before["fetched_at"]
    assert after["content_updated_at"] > before["content_updated_at"]

exec
/bin/zsh -lc "sed -n '1,200p' tests/test_migrations.py" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
"""Migration smoke test.

Verifies that Alembic's `upgrade head` creates the `feeds` and `entries`
tables with the required indexes and the unique constraint defined in
docs/spec/feed.md and docs/spec/entry.md.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


@pytest.mark.asyncio
async def test_tables_exist_after_migration(async_engine: AsyncEngine) -> None:
    async with async_engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' ORDER BY table_name"
            )
        )
        tables = {row[0] for row in result}
    assert "feeds" in tables
    assert "entries" in tables


@pytest.mark.asyncio
async def test_required_indexes_exist(async_engine: AsyncEngine) -> None:
    async with async_engine.connect() as conn:
        result = await conn.execute(
            text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
        )
        indexes = {row[0] for row in result}
    assert "ix_feeds_status" in indexes
    # Migration 0002 replaced the active-only partial with two
    # partials that match the real _claim_due_feeds predicates.
    assert "ix_feeds_due_not_dead" in indexes
    assert "ix_feeds_dead_last_attempt" in indexes
    assert "ix_feeds_next_fetch_at_active" not in indexes
    assert "ix_entries_fetched_at" in indexes
    assert "ix_entries_feed_pub_id" in indexes
    assert "ix_entries_feed_fetched_id" in indexes


@pytest.mark.asyncio
async def test_entries_unique_constraint(async_engine: AsyncEngine) -> None:
    async with async_engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = 'entries'::regclass AND contype = 'u'"
            )
        )
        constraints = {row[0] for row in result}
    assert "uq_entries_feed_guid" in constraints

exec
/bin/zsh -lc "sed -n '1,120p' src/feedgate/fetcher/__init__.py" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
"""feedgate.fetcher — fetch/parse/upsert pipeline and asyncio scheduler.

Phase 1 provides only the upsert layer. HTTP fetching, parser wrapper
and scheduler arrive in Phase 2 and Phase 4.
"""

exec
/bin/zsh -lc "sed -n '1,120p' src/feedgate/__init__.py" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
"""feedgate-fetcher — RSS/Atom feed fetcher microservice."""

__version__ = "0.0.1"

codex
마지막으로 운영 보조 스크립트와 배포 설정도 확인해서 “개발/운영 워크플로” 관점까지 포함해 정리하겠습니다.
exec
/bin/zsh -lc "sed -n '1,260p' docker-compose.yml" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
# feedgate-fetcher local stack
#
# 4 services share one image (built once) and one Postgres:
#
#   postgres   single instance, named volume for persistence
#   migrate    one-shot Alembic upgrade, runs to completion then exits
#   api        FastAPI server (FEEDGATE_SCHEDULER_ENABLED=false,
#              FEEDGATE_RETENTION_ENABLED=true) — handles HTTP requests
#              and runs the retention sweeper
#   worker     scheduler-only role (FEEDGATE_SCHEDULER_ENABLED=true,
#              FEEDGATE_RETENTION_ENABLED=false) — scalable via
#              `docker compose up -d --scale worker=N worker`
#
# The host port mapping for `postgres` (55432:5432) is preserved so
# any host process (e.g. a developer-run uvicorn) can still connect
# at `localhost:55432`. The compose-managed services connect through
# the Docker network at `postgres:5432`. The host uvicorn and the
# compose worker can run **simultaneously** against the same DB —
# SKIP LOCKED keeps them from double-claiming feeds.
#
# Quick start:
#   docker compose build
#   docker compose up -d postgres
#   docker compose run --rm migrate
#   docker compose up -d api
#   docker compose up -d --scale worker=3 worker
#   docker compose ps

services:
  postgres:
    image: postgres:16-alpine
    container_name: feedgate-pg
    environment:
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgres
      POSTGRES_DB: feedgate
    ports:
      - "55432:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres -d feedgate"]
      interval: 2s
      timeout: 2s
      retries: 15
    volumes:
      - pg-data:/var/lib/postgresql/data

  migrate:
    image: feedgate-fetcher:latest
    build: .
    command: ["uv", "run", "--no-sync", "alembic", "upgrade", "head"]
    environment:
      FEEDGATE_DATABASE_URL: postgresql+asyncpg://postgres:postgres@postgres:5432/feedgate
    depends_on:
      postgres:
        condition: service_healthy
    restart: "no"

  api:
    image: feedgate-fetcher:latest
    environment:
      FEEDGATE_DATABASE_URL: postgresql+asyncpg://postgres:postgres@postgres:5432/feedgate
      FEEDGATE_SCHEDULER_ENABLED: "false"
      FEEDGATE_RETENTION_ENABLED: "true"
    ports:
      # 8766 on host → 8000 in container.
      # 8765 is intentionally left for a host-run uvicorn so the two
      # can coexist during the migration period. Drop the host
      # uvicorn first if you want to free 8765.
      - "8766:8000"
    depends_on:
      migrate:
        condition: service_completed_successfully
    restart: unless-stopped

  worker:
    image: feedgate-fetcher:latest
    environment:
      FEEDGATE_DATABASE_URL: postgresql+asyncpg://postgres:postgres@postgres:5432/feedgate
      FEEDGATE_SCHEDULER_ENABLED: "true"
      FEEDGATE_RETENTION_ENABLED: "false"
    # No host port mapping — workers do not serve external HTTP.
    # They still run uvicorn internally, but only because the
    # lifespan is what starts scheduler.run() as an asyncio task.
    # Scale with: `docker compose up -d --scale worker=N worker`
    depends_on:
      migrate:
        condition: service_completed_successfully
    restart: unless-stopped

volumes:
  pg-data:

exec
/bin/zsh -lc "sed -n '1,260p' Dockerfile" in /Users/kangheeyong/project/feedgate-fetcher
exec
/bin/zsh -lc "sed -n '1,260p' scripts/seed_feeds.py" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
# syntax=docker/dockerfile:1.6
#
# feedgate-fetcher container image.
#
# Single-stage on top of the official `uv` slim Python 3.12 image:
# `uv sync` is split into a deps-only pass and a project pass so that
# pyproject.toml/uv.lock changes invalidate only one cache layer.
#
# The same image is reused by every service in docker-compose.yml
# (migrate / api / worker). Behavior is selected entirely by env vars
# (`FEEDGATE_SCHEDULER_ENABLED`, `FEEDGATE_RETENTION_ENABLED`).

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# 1) Dependency manifests first → cached layer reused as long as
#    pyproject.toml / uv.lock are unchanged.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# 2) Project source.
COPY src ./src
COPY alembic ./alembic
COPY alembic.ini ./

# 3) Install the project itself into the venv.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

EXPOSE 8000

# Default command runs the API server. The worker service overrides
# nothing — it uses the same uvicorn entry point but flips
# `FEEDGATE_SCHEDULER_ENABLED=true` so the lifespan starts the
# background scheduler task.
CMD ["uv", "run", "--no-sync", "uvicorn", "feedgate.main:create_app", \
     "--factory", "--host", "0.0.0.0", "--port", "8000"]

 succeeded in 0ms:
#!/usr/bin/env python3
"""Register every seed feed from the catalog into the running service.

Idempotent: reposting a URL that is already registered returns HTTP
200 and the existing row (ADR 002). Exits 0 if every request
completed, 1 if any request failed (HTTP 5xx, 4xx other than 200,
or network error).

The catalog is hard-coded here so this script has zero dependencies
outside the stdlib and can run in any Python 3.11+ interpreter.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

API_BASE = os.environ.get("FEEDGATE_API_BASE", "http://127.0.0.1:8765")

# (category, url). Kept in the order the user's catalog specified so the
# POST order is deterministic and the DB layout is reproducible.
SEED_FEEDS: list[tuple[str, str]] = [
    ("ai-research", "https://huggingface.co/blog/feed.xml"),
    ("ai-research", "https://openai.com/news/rss.xml"),
    ("ai-research", "https://deepmind.google/blog/rss.xml"),
    ("ai-research", "https://engineering.fb.com/feed/"),
    ("ai-infra", "https://developer.nvidia.com/blog/feed"),
    ("ai-infra", "https://pytorch.org/blog/feed.xml"),
    ("eng-blogs", "https://netflixtechblog.com/feed"),
    ("eng-blogs", "https://www.uber.com/blog/engineering/rss/"),
    ("eng-blogs", "https://blog.cloudflare.com/rss/"),
    ("eng-blogs", "https://stripe.com/blog/feed.rss"),
    ("eng-blogs", "https://tailscale.com/blog/index.xml"),
    ("eng-blogs", "https://dropbox.tech/feed"),
    ("eng-blogs", "https://fly.io/blog/feed.xml"),
    ("k8s-cloudnative", "https://www.cncf.io/feed/"),
    ("k8s-cloudnative", "https://kubernetes.io/feed.xml"),
    ("lowlevel", "https://simonwillison.net/atom/everything/"),
    ("lowlevel", "https://lwn.net/headlines/newrss"),
    ("hardware", "https://blogs.nvidia.com/feed/"),
    ("hardware", "https://www.nextplatform.com/feed/"),
    ("hardware", "https://chipsandcheese.com/feed/"),
    ("hardware", "https://nvidianews.nvidia.com/releases.xml"),
    ("misc", "https://blog.python.org/feeds/posts/default"),
]


def post_feed(url: str) -> tuple[int, object]:
    req = urllib.request.Request(
        f"{API_BASE}/v1/feeds",
        data=json.dumps({"url": url}).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except urllib.error.URLError as e:
        return 0, f"network error: {e}"


def main() -> int:
    created = 0
    existing = 0
    failed: list[tuple[str, str, int, object]] = []
    for category, url in SEED_FEEDS:
        status, body = post_feed(url)
        if status == 201:
            created += 1
            fid = body["id"] if isinstance(body, dict) else "?"
            print(f"  +  [{category:17}] id={fid:<4} {url}")
        elif status == 200:
            existing += 1
            fid = body["id"] if isinstance(body, dict) else "?"
            print(f"  =  [{category:17}] id={fid:<4} {url}")
        else:
            failed.append((category, url, status, body))
            print(f"  !  [{category:17}] HTTP {status}: {url}")

    total = len(SEED_FEEDS)
    print(f"\nTotal {total} | created {created} | existing {existing} | failed {len(failed)}")
    if failed:
        print("\nFailed URLs:")
        for cat, url, status, body in failed:
            print(f"  - [{cat}] {url} -> {status}: {str(body)[:200]}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

exec
/bin/zsh -lc "sed -n '1,260p' scripts/live_verify.py" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
#!/usr/bin/env python3
"""Live verification battery for feedgate-fetcher.

Runs one end-to-end check of the running service plus direct DB
invariants. Writes a structured JSON report to
``var/verify-runs/<timestamp>.json`` and prints a one-line summary
on stdout.

Intended to be called every 10 minutes from a bash loop (see
``scripts/live_verify_loop.sh``). Fully stateless — each invocation
is self-contained so a prior failed run does not poison the next
one.

Exit code:
  0 — every check passed
  1 — one or more checks failed (report file still written)
  2 — verifier itself crashed before finishing (unreachable API,
      DB down, etc.)

Environment variables:
  FEEDGATE_API_BASE     — base URL of the running uvicorn
                          (default: http://127.0.0.1:8765)
  FEEDGATE_VERIFY_DSN   — asyncpg-compatible DSN for direct DB peek
                          (default: postgresql://postgres:postgres@localhost:55432/feedgate)
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import os
import sys
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import asyncpg
import httpx

API_BASE = os.environ.get("FEEDGATE_API_BASE", "http://127.0.0.1:8765")
DB_DSN = os.environ.get(
    "FEEDGATE_VERIFY_DSN",
    "postgresql://postgres:postgres@localhost:55432/feedgate",
)
REPO_ROOT = Path(__file__).resolve().parent.parent
RUN_DIR = REPO_ROOT / "var" / "verify-runs"


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunReport:
    timestamp: str
    api_base: str
    duration_ms: int
    checks: list[CheckResult] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)


# --- HTTP API checks --------------------------------------------------------


async def check_healthz(client: httpx.AsyncClient) -> CheckResult:
    try:
        r = await client.get("/healthz", timeout=5)
    except Exception as e:
        return CheckResult("healthz", False, f"exception: {e!r}")
    body: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        body = r.json()
    ok = r.status_code == 200 and body.get("status") == "ok"
    return CheckResult("healthz", ok, f"HTTP {r.status_code}")


async def check_feeds_list(
    client: httpx.AsyncClient,
) -> tuple[CheckResult, list[dict[str, Any]]]:
    try:
        r = await client.get("/v1/feeds", params={"limit": 200}, timeout=10)
    except Exception as e:
        return CheckResult("feeds_list", False, f"exception: {e!r}"), []
    if r.status_code != 200:
        return CheckResult("feeds_list", False, f"HTTP {r.status_code}"), []
    items = r.json().get("items", [])
    return (
        CheckResult("feeds_list", True, f"{len(items)} feeds", {"n": len(items)}),
        items,
    )


async def check_feed_get(client: httpx.AsyncClient, feed_id: int) -> CheckResult:
    try:
        r = await client.get(f"/v1/feeds/{feed_id}", timeout=5)
    except Exception as e:
        return CheckResult(f"feed_get_{feed_id}", False, f"exception: {e!r}")
    if r.status_code != 200:
        return CheckResult(f"feed_get_{feed_id}", False, f"HTTP {r.status_code}")
    body = r.json()
    required = {
        "id",
        "url",
        "effective_url",
        "title",
        "status",
        "last_successful_fetch_at",
        "last_attempt_at",
        "last_error_code",
        "created_at",
    }
    missing = required - set(body)
    ok = not missing
    return CheckResult(
        f"feed_get_{feed_id}",
        ok,
        "OK" if ok else f"missing fields: {sorted(missing)}",
    )


async def check_missing_feed_404(client: httpx.AsyncClient) -> CheckResult:
    try:
        r = await client.get("/v1/feeds/9999999", timeout=5)
    except Exception as e:
        return CheckResult("feed_missing_404", False, f"exception: {e!r}")
    return CheckResult("feed_missing_404", r.status_code == 404, f"HTTP {r.status_code}")


async def check_idempotent_post(client: httpx.AsyncClient, url: str) -> CheckResult:
    try:
        r = await client.post("/v1/feeds", json={"url": url}, timeout=10)
    except Exception as e:
        return CheckResult("idempotent_post", False, f"exception: {e!r}")
    return CheckResult(
        "idempotent_post",
        r.status_code == 200,
        f"HTTP {r.status_code} (expected 200 for duplicate)",
    )


async def check_entries_feed_ids_required(
    client: httpx.AsyncClient,
) -> CheckResult:
    try:
        r = await client.get("/v1/entries", timeout=5)
    except Exception as e:
        return CheckResult("entries_feed_ids_required", False, f"exception: {e!r}")
    return CheckResult(
        "entries_feed_ids_required",
        r.status_code == 422,
        f"HTTP {r.status_code}",
    )


async def check_entries_invalid_cursor(
    client: httpx.AsyncClient,
) -> CheckResult:
    try:
        r = await client.get(
            "/v1/entries",
            params={"feed_ids": "1", "cursor": "!!!bogus!!!"},
            timeout=5,
        )
    except Exception as e:
        return CheckResult("entries_invalid_cursor_400", False, f"exception: {e!r}")
    return CheckResult(
        "entries_invalid_cursor_400",
        r.status_code == 400,
        f"HTTP {r.status_code}",
    )


async def check_entries_aggregate(
    client: httpx.AsyncClient, feed_ids: list[int]
) -> tuple[CheckResult, int]:
    if not feed_ids:
        return CheckResult("entries_aggregate", False, "no feeds"), 0
    ids_csv = ",".join(str(i) for i in feed_ids)
    try:
        r = await client.get(
            "/v1/entries",
            params={"feed_ids": ids_csv, "limit": 200},
            timeout=10,
        )
    except Exception as e:
        return CheckResult("entries_aggregate", False, f"exception: {e!r}"), 0
    if r.status_code != 200:
        return CheckResult("entries_aggregate", False, f"HTTP {r.status_code}"), 0
    items = r.json().get("items", [])
    return CheckResult("entries_aggregate", True, f"{len(items)} entries"), len(items)


async def check_cursor_walk(client: httpx.AsyncClient, feed_id: int) -> CheckResult:
    """Page through one feed with limit=2, assert uniqueness and monotonicity."""
    seen: set[str] = set()
    cursor: str | None = None
    pages = 0
    prev_published: str | None = None
    while pages < 5:
        params: dict[str, str] = {"feed_ids": str(feed_id), "limit": "2"}
        if cursor is not None:
            params["cursor"] = cursor
        try:
            r = await client.get("/v1/entries", params=params, timeout=10)
        except Exception as e:
            return CheckResult("cursor_walk", False, f"exception on page {pages}: {e!r}")
        if r.status_code != 200:
            return CheckResult("cursor_walk", False, f"HTTP {r.status_code} on page {pages}")
        body = r.json()
        for e in body.get("items", []):
            if e["guid"] in seen:
                return CheckResult("cursor_walk", False, f"duplicate guid {e['guid']}")
            seen.add(e["guid"])
            cur_published = e.get("published_at")
            if (
                prev_published is not None
                and cur_published is not None
                and cur_published > prev_published
            ):
                return CheckResult(
                    "cursor_walk",
                    False,
                    f"sort order broken: {cur_published} > {prev_published}",
                )
            prev_published = cur_published
        cursor = body.get("next_cursor")
        pages += 1
        if not cursor:
            break
    return CheckResult("cursor_walk", True, f"{pages} pages, {len(seen)} unique entries")


# --- DB invariant checks ----------------------------------------------------


async def check_db_invariants() -> list[CheckResult]:
    results: list[CheckResult] = []
    conn = await asyncpg.connect(DB_DSN)
    try:
        n_feeds = await conn.fetchval("SELECT COUNT(*) FROM feeds")
        n_entries = await conn.fetchval("SELECT COUNT(*) FROM entries")

        ts_violations = await conn.fetchval(
            "SELECT COUNT(*) FROM entries WHERE content_updated_at < fetched_at"
        )
        orphans = await conn.fetchval(
            "SELECT COUNT(*) FROM entries e "
            "LEFT JOIN feeds f ON f.id = e.feed_id "

exec
/bin/zsh -lc "sed -n '1,260p' docs/deploy/local-multi-worker.md" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
# Local Multi-Worker Deployment (docker compose)

- 상태: Draft
- 마지막 업데이트: 2026-04-12
- 관련 spec: [`../spec/resilience.md`](../spec/resilience.md)
- 관련 코드: `Dockerfile`, `docker-compose.yml`

이 문서는 docker compose만으로 **워커 N대 + API 1대 + Postgres 1대**를
띄우고 PR #8 (SKIP LOCKED) 동작을 로컬에서 검증하는 절차를 정의한다.
k8s manifest 작성 전, **로컬에서 멀티 워커가 실제로 disjoint 피드를
나눠 처리하는 것을 확인**하는 단계.

## 1. 아키텍처

```
                  ┌─────────────┐
                  │  postgres   │  (단일 — 절대 늘리지 않음)
                  │  pg-data 볼륨│
                  └──────┬──────┘
                         │ 5432 (docker network)
       ┌─────────────────┼──────────────────┐
       │                 │                  │
┌──────▼──────┐  ┌───────▼──────┐  ┌────────▼─────────┐
│   migrate   │  │     api      │  │   worker × N     │
│  one-shot   │  │ replicas: 1  │  │  --scale=N       │
│  alembic    │  │ port 8766    │  │  no host port    │
│  upgrade    │  │ retention ON │  │  scheduler ON    │
└─────────────┘  └──────────────┘  └──────────────────┘
                                     ▲
                                     │ SKIP LOCKED
                                     │ disjoint claim
```

- **postgres**: 단일 컨테이너, 호스트 포트 `55432:5432` (호스트에서 직접 접속 가능)
- **migrate**: `alembic upgrade head` 1회 실행 후 종료
- **api**: FastAPI HTTP, retention 루프 포함, scheduler 비활성. 호스트 8766 노출.
- **worker**: scheduler 루프 활성, retention 비활성, HTTP 포트 미노출. **`--scale`로 N개 늘림**.

## 2. 빠른 시작

```bash
# 1) 이미지 빌드 (pyproject.toml 변경 시 다시 빌드)
docker compose build

# 2) Postgres 띄우기 + 마이그레이션 1회
docker compose up -d postgres
docker compose run --rm migrate

# 3) API 띄우기
docker compose up -d api

# 4) Worker N대 띄우기 (예: 3대)
docker compose up -d --scale worker=3 worker

# 5) 상태 확인
docker compose ps
```

기대 출력 (worker가 3개):

```
NAME                                IMAGE                       STATUS
feedgate-pg                         postgres:16-alpine          Up healthy
feedgate-fetcher-api-1              feedgate-fetcher:latest     Up
feedgate-fetcher-worker-1           feedgate-fetcher:latest     Up
feedgate-fetcher-worker-2           feedgate-fetcher:latest     Up
feedgate-fetcher-worker-3           feedgate-fetcher:latest     Up
```

## 3. 호스트 uvicorn과 공존

이 compose 구성은 **호스트에서 직접 띄운 uvicorn(포트 8765)을 건드리지 않는다**.

| 구성 요소 | 호스트 포트 | DB 접속 경로 |
|---|---|---|
| 호스트 uvicorn | `8765` | `localhost:55432` |
| compose api | `8766` | `postgres:5432` (docker network) |
| compose worker | (없음) | `postgres:5432` |

세 종류의 워커(호스트 uvicorn + compose api 안의 backround task가 아님 + N개 compose worker)가 같은 Postgres를 공유한다. **SKIP LOCKED 덕분에 같은 피드를 중복 fetch하지 않는다.** 이게 멀티 워커 검증의 핵심.

호스트 uvicorn을 빼고 싶으면 그 프로세스만 종료하면 된다. compose 측에는 영향 없음.

## 4. 멀티 워커 검증 체크리스트

PR #8이 실제로 동작하는지 눈으로 확인하는 절차:

### A. 워커 로그에서 disjoint claim 확인

```bash
docker compose logs --tail 50 worker
```

각 worker pod의 로그에서 fetch_one 호출이 **서로 다른 feed_id를 처리하는지** 확인. 같은 feed_id가 두 워커에서 동시에 처리되면 안 됨 (scheduler 로그가 WARNING/INFO 레벨이라 fetch 결과를 직접 보긴 어려움 — DB 검증이 더 확실).

### B. DB 불변식 검증

```bash
# verify 스크립트가 호스트 uvicorn(8765) 또는 compose api(8766)를 가리키도록 환경변수
export FEEDGATE_API=http://127.0.0.1:8766
uv run python scripts/live_verify.py
```

기대: `OK 17/17 ... entries_api=200`. 특히 다음 체크가 통과해야 함:

- `db_no_duplicate_guids` (entries 중복 INSERT 안 됨)
- `db_timestamp_invariant` (fetched_at 불변식)
- `db_no_orphan_entries` (FK 무결성)

### C. tick 시간 단축 측정 (선택)

`fetch_interval_seconds` 동안 처리되는 피드 수를 worker 수별로 비교:

```bash
# worker=1
docker compose up -d --scale worker=1 worker
# 5분 대기 후 metric 측정 (직접 DB 쿼리 또는 verify 결과)

# worker=3
docker compose up -d --scale worker=3 worker
# 같은 측정
```

이론상 worker=3일 때 한 tick에서 3배 많은 피드를 동시 처리. 실제 측정은 `entries_inserted_total` 증분 비율로.

## 5. 운영 시나리오 (트러블슈팅)

### Q. `migrate` 컨테이너가 즉시 종료되는데 정상인가?
**A**. 정상이다. one-shot 컨테이너로 alembic upgrade가 끝나면 exit 0. `docker compose run --rm migrate`로 명시적으로 한 번만 돌리는 게 가장 깔끔. compose v2의 `service_completed_successfully` 조건 덕분에 api/worker는 자동으로 마이그레이션 완료를 기다린다.

### Q. worker 1대를 죽이면 어떻게 되나?
**A**. SKIP LOCKED + lease TTL(180s, `fetch_claim_ttl_seconds`)로 자동 복구. 죽기 전 claim한 피드는 180초 후 lease 만료 → 다른 워커가 다음 tick에 픽업.

```bash
docker compose stop feedgate-fetcher-worker-2
sleep 200
docker compose ps
# worker-2가 stopped 상태에서도 worker-1, worker-3이 정상 진행
```

### Q. compose의 Postgres와 호스트 PG를 둘 다 띄우면?
**A**. 별개 인스턴스다. 호스트 PG는 호스트 시스템 또는 별도 docker run으로 띄운 것, compose의 `postgres`는 compose 네임스페이스 안에 있음. 컨테이너 안에서는 `postgres:5432`로 접속하므로 호스트 PG와 충돌 없음. 단, 호스트 uvicorn이 어느 PG를 가리키는지 `FEEDGATE_DATABASE_URL`로 명시할 것.

### Q. `pg-data` 볼륨을 비우고 싶다
**A**.
```bash
docker compose down -v   # ⚠️ 모든 데이터 삭제
docker compose up -d postgres
docker compose run --rm migrate
```

### Q. 빌드가 오래 걸린다
**A**. 첫 빌드에서 uv가 deps 전체를 install (~2분). 이후엔 `pyproject.toml` 변경 없으면 캐시 hit으로 ~10초. BuildKit 캐시 마운트(`uv` 캐시 디렉토리)를 활용하므로 같은 머신에서 재빌드 시 더 빠르다.

## 6. 한계 & TODO

| 항목 | 현재 | 향후 (k8s 직전) |
|---|---|---|
| Multi-replica API | replicas:1 고정 | k8s HPA로 동적 확장 |
| Worker autoscale | 수동 `--scale` | k8s HPA + custom metric (`feed_state_count{state=active}`) |
| Retention 분리 | api 컨테이너 안 (lifespan) | k8s CronJob으로 분리 |
| Migration 자동 실행 | 수동 `docker compose run --rm migrate` | k8s Job + initContainer 패턴 |
| Secret 관리 | docker-compose.yml에 평문 | k8s Secret + envFrom |
| Healthcheck | postgres만 | api/worker도 `/healthz` 기반 |
| Graceful shutdown | scheduler + retention 루프 cooperative drain (stop_event + 90s budget + force-cancel fallback, `src/feedgate/main.py::_drain_background_task`) | k8s `terminationGracePeriodSeconds` + preStop hook으로 lifecycle 확장 |

이 항목들은 별도 PR로 진행한다. 현재 docker compose 구성은 **로컬 멀티 워커 검증**과 **k8s manifest 작성 전 단계**의 위치를 잡는다.

## 7. 다음 단계

1. `docker compose build && docker compose up -d --scale worker=3 worker`로 멀티 워커 동작 확인
2. `live_verify.py`로 24시간 정도 돌려서 entries 누락/중복 0 확인
3. Worker 강제 종료/재기동 시나리오로 lease TTL 자동 복구 검증
4. 성공하면 → k8s manifest PR로 넘어감

codex
핵심 코드는 다 확인했고, 마지막으로 CI/테스트 정책 문서까지 확인한 뒤 전체 아키텍처 요약으로 정리하겠습니다.
exec
/bin/zsh -lc "sed -n '1,260p' .github/workflows/ci.yml" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

permissions:
  contents: read

jobs:
  test:
    name: lint + type + test
    runs-on: ubuntu-latest
    timeout-minutes: 15

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v5
        with:
          version: latest
          enable-cache: true

      - name: Pin Python
        run: uv python install 3.12

      - name: Install dependencies (frozen)
        run: uv sync --frozen

      - name: Ruff lint
        run: uv run ruff check .

      - name: Ruff format check
        run: uv run ruff format --check .

      - name: Mypy strict
        run: uv run mypy src tests

      - name: Pytest
        # testcontainers boots a real Postgres 16 container; the
        # ubuntu-latest runner ships with Docker so no extra setup.
        run: uv run pytest -q

exec
/bin/zsh -lc 'rg --files .github docs/tests' in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
docs/tests/resilience-test-cases.md
.github/workflows/ci.yml

exec
/bin/zsh -lc "sed -n '1,280p' docs/tests/resilience-test-cases.md" in /Users/kangheeyong/project/feedgate-fetcher
 succeeded in 0ms:
# Test Cases: Resilience & Defense Logic

- 상태: Draft v1
- 마지막 업데이트: 2026-04-12
- 관련 spec: [`../spec/resilience.md`](../spec/resilience.md)
- 관련 spec: [`../spec/feed.md`](../spec/feed.md), [`../spec/entry.md`](../spec/entry.md)

이 문서는 [`resilience.md`](../spec/resilience.md)의 모든 위협 ID(A1~D6)에
**하나 이상의 테스트 케이스가 매핑**되도록 보장한다. 각 TC는:

- **TC-ID**: 위협 ID 기반 (예: `TC-A1-01`)
- **상태**: ✅ 구현됨 / ⚠️ 부분 / ❌ 미구현 (방어 자체가 미구현이라 TC도 미구현)
- **테스트 위치**: `tests/<file>.py::<test_name>` 형식
- **Given / When / Then** 시나리오

## 1. 적용 범위

- 모든 단위/통합 테스트는 **testcontainers Postgres 16**으로 실행 (SQLite 불가 — SKIP LOCKED, GIN 인덱스 등 PG 전용 기능 사용)
- HTTP는 `respx`로 모킹, 실제 외부 호출 없음
- 시간은 명시적 `now: datetime` 인자로 주입 (deterministic)
- TC가 ❌인 항목은 **방어가 미구현이라 동작 자체가 없음** — 해당 PR에서 TC와 코드를 함께 추가해야 함

## 2. Threat → TC 매핑 요약

| 위협 | 카테고리 | TC 수 | 구현 비율 |
|---|---|---|---|
| A1 거대 응답 | Tier 2 cap | 1 | 1/1 ✅ |
| A2 압축 폭탄 | Tier 2 cap | 0 | 0/1 ❌ (간접 보장만) |
| A3 Slow Loris | Tier 2 timeout | 0 | 0/1 ❌ |
| A4 첫 fetch 1만 entries | Tier 2 cap | 2 | 2/2 ✅ |
| A5 redirect loop | Tier 2 cap | 0 | 0/1 ❌ |
| A6 TLS 무효 | Tier 3 state | 0 | 0/1 ❌ (직접 TC 없음) |
| A7 매번 새 guid 100개 | Tier 2 cap | 0 | 0/1 ❌ |
| A8 영구 4xx | Tier 3 state machine | 6 | 6/6 ✅ |
| B1 동시 timeout | Tier 2 contain | 1 | 1/1 ✅ |
| B2 thundering herd | Tier 3 jitter | 1 | 1/1 ✅ |
| B3 동일 호스트 폭격 | Tier 1 throttle | 0 | 0/1 ❌ |
| B4 429 폭탄 | Tier 3 Retry-After | 4 | 4/4 ✅ |
| C1 PG pool 고갈 | Tier 2 tune | 0 | 0/1 ❌ |
| C2 retention 지연 | Tier 2 batch | 0 | 0/1 ❌ |
| C3 tick 예외 propagate | Tier 2 try/except | 1 | 1/1 ✅ |
| C4 워커 OOM | Tier 3 lease TTL | 1 | 1/1 ✅ |
| C5 rolling deploy | Tier 2 drain | 0 | 0/1 ❌ |
| C6 DB 단절 | Tier 3 backoff | 0 | 0/1 ❌ |
| D1 AWS metadata SSRF | Tier 1 IP block | 0 | 0/2 ❌ |
| D2 DB 포트 정찰 | Tier 1 IP block | 0 | 0/1 ❌ |
| D3 file:// | Tier 1 scheme | 0 | 0/1 ❌ (httpx 자동) |
| D4 redirect SSRF | Tier 1 re-check | 0 | 0/1 ❌ |
| D5 API 폭주 | Tier 1 rate limit | 0 | 0/1 ❌ |
| D6 멱등 POST | Tier 3 ON CONFLICT | 1 | 1/1 ✅ |

**총계: 18 TC 구현 / 27 TC 필요 → 67% 커버리지** (구현 안 된 9개는 모두 ❌
방어선이 없어서 함께 미구현)

---

## 3. 카테고리 A: 단일 피드가 우리를 공격

### A1 — 5GB 응답 본문

#### TC-A1-01 ✅ — `too_large` 분류 + 스트림 abort
- **위치**: `tests/test_fetch_one.py::test_fetch_one_rejects_oversized_response_as_too_large`
- **Given**: feed 1개, mock 응답 본문 4KB, `max_bytes=1KB`
- **When**: `fetch_one(feed, ..., max_bytes=1024)`
- **Then**:
  - `last_error_code = "too_large"`
  - `last_successful_fetch_at` 변화 없음
  - 본문이 다 수신되기 전에 stream abort (메모리 cap 검증)
- **방어선**: Tier 2 (Body cap 5MB streaming, 호출자 override 가능)

### A2 — 압축 폭탄 (gzip 5MB → 5GB)

#### TC-A2-01 ❌ — gzip 응답이 cap을 decompressed 기준으로 트리거
- **위치**: 미구현 — `tests/test_fetch_one.py::test_fetch_one_compression_bomb_blocked` (planned)
- **Given**: 100KB짜리 gzip 응답, decompressed 시 100MB 본문
- **When**: `fetch_one(..., max_bytes=5*1024*1024)`
- **Then**:
  - `last_error_code = "too_large"`
  - 5MB 이상 메모리 할당 안 됨 (Counter로 검증)
- **현재 상태**: httpx가 자동 decompress하고 cap이 decompressed bytes 기준이라
  **이론상 동작**하지만 회귀 방지 TC가 없음
- **PR 후보**: P2 (확실성 보강)

### A3 — Slow Loris (1바이트/19초)

#### TC-A3-01 ❌ — wall-clock total timeout으로 stall 차단
- **위치**: 미구현 — `tests/test_fetch_one.py::test_fetch_one_slow_loris_wall_clock_timeout` (planned)
- **Given**: mock 응답이 1바이트씩 19초 간격으로 전송, 본문 끝없음
- **When**: `fetch_one(..., total_timeout=30)`
- **Then**:
  - 30초 wall-clock 초과 시 abort
  - `last_error_code = "timeout"`
- **방어선**: Tier 2 (wall-clock + httpx 세분 timeout)
- **PR 후보**: **P1 운영** (반드시 처리)

### A4 — 첫 fetch에 1만 entries

#### TC-A4-01 ✅ — initial cap이 동작
- **위치**: `tests/test_fetch_one.py::test_fetch_one_caps_initial_fetch_entries`
- **Given**: feed 1개 (entries 0개 있음), mock 응답에 entries 6개, `max_entries_initial=3`
- **When**: `fetch_one(..., max_entries_initial=3)`
- **Then**: entries 테이블에 3개만 저장됨

#### TC-A4-02 ✅ — 재 fetch 시엔 cap 적용 안 됨
- **위치**: `tests/test_fetch_one.py::test_fetch_one_no_cap_on_subsequent_fetch`
- **Given**: 첫 fetch로 3개 저장된 feed, 같은 mock(6개) 다시 응답
- **When**: 같은 feed로 fetch_one 다시 호출, 같은 cap=3
- **Then**: entries 테이블에 6개 (cap이 first fetch에만 적용된 것 검증)

### A5 — 무한 redirect chain

#### TC-A5-01 ❌ — redirect chain N회 초과 시 `redirect_loop`
- **위치**: 미구현 — `tests/test_fetch_one.py::test_fetch_one_rejects_redirect_chain_over_cap` (planned)
- **Given**: mock이 매번 다른 URL로 302 응답 (>5회)
- **When**: `fetch_one(..., max_redirects=5)`
- **Then**:
  - `last_error_code = "redirect_loop"` (또는 httpx 기본 동작 확인)
  - 5회 이상 fetch 안 함
- **현재 상태**: httpx 기본 cap 20회 — 동작은 하지만 직접 검증 TC 없음, error code 분류도 명시 안 됨
- **PR 후보**: P3

### A6 — TLS 인증서 무효

#### TC-A6-01 ❌ — `tls_error` 분류
- **위치**: 미구현 — `tests/test_fetch_one.py::test_fetch_one_tls_error_classified` (planned)
- **Given**: mock이 SSL 핸드셰이크 실패 시뮬레이션 (httpx.ConnectError 변형 또는 httpx.SSLError)
- **When**: `fetch_one`
- **Then**:
  - `last_error_code = "tls_error"`
  - `consecutive_failures += 1`
- **현재 상태**: `_classify_error`에서 일반 `ConnectError`로 잡혀서 `connection`으로 분류됨. `tls_error` 별도 분류가 없음 — **분류 자체부터 추가 필요**
- **PR 후보**: P3

### A7 — 매 fetch마다 새 guid 100개

#### TC-A7-01 ❌ — fetch당 entries cap (없는 cap 추가)
- **위치**: 미구현 — `tests/test_fetch_one.py::test_fetch_one_caps_per_fetch_entries` (planned)
- **Given**: existing entries=10, mock 응답에 새 guid 100개
- **When**: `fetch_one(..., max_entries_per_fetch=20)`
- **Then**: 한 번의 fetch로 20개만 추가, 나머지는 다음 fetch로 미룸
- **현재 상태**: cap 자체가 없음. 100개 다 INSERT됨
- **PR 후보**: P2 (스케일 시 의미)

### A8 — 영구 4xx (Cloudflare WAF 차단)

#### TC-A8-01 ✅ — active → broken 전이 (3회 실패)
- **위치**: `tests/test_fetch_one.py::test_fetch_one_active_to_broken_after_n_failures`
- **Given**: active feed, mock이 매번 404 응답
- **When**: 3회 fetch_one
- **Then**: `consecutive_failures=3, status='broken'`

#### TC-A8-02 ✅ — broken → dead (마지막 성공 7일 경과)
- **위치**: `tests/test_fetch_one.py::test_fetch_one_broken_to_dead_after_duration_since_last_success`
- **Given**: broken feed, last_successful_fetch_at = now - 8일
- **When**: fetch_one
- **Then**: `status='dead'`

#### TC-A8-03 ✅ — broken → dead (성공 없으면 created_at fallback)
- **위치**: `tests/test_fetch_one.py::test_fetch_one_broken_to_dead_falls_back_to_created_at`

#### TC-A8-04 ✅ — 410 즉시 dead 전이
- **위치**: `tests/test_fetch_one.py::test_fetch_one_http_410_transitions_to_dead_immediately`

#### TC-A8-05 ✅ — broken 상태에서 7일 안엔 broken 유지
- **위치**: `tests/test_fetch_one.py::test_fetch_one_broken_stays_broken_within_duration`

#### TC-A8-06 ✅ — broken → active (성공 시 회복)
- **위치**: `tests/test_fetch_one.py::test_fetch_one_broken_to_active_on_success`

---

## 4. 카테고리 B: 다수 피드 동시 장애

### B1 — CDN 장애로 50개 피드 동시 timeout

#### TC-B1-01 ✅ — 일부 피드 실패해도 나머지 진행
- **위치**: `tests/test_scheduler_tick.py::test_tick_once_continues_when_one_feed_fails`
- **Given**: 2개 피드, 하나는 200, 하나는 500
- **When**: `tick_once`
- **Then**:
  - 성공한 피드: `last_successful_fetch_at` 갱신
  - 실패한 피드: `last_error_code = "http_5xx"`, fail++
  - 두 피드 모두 처리됨 (한 피드 실패가 다른 피드를 막지 않음)
- **방어선**: Tier 2 (per-feed 세션 격리)

### B2 — Thundering herd (broken 100개 동시 cap 해제)

#### TC-B2-01 ✅ — backoff에 ±25% jitter 적용
- **위치**: `tests/test_fetch_one.py::test_compute_next_fetch_at_broken_at_threshold_boundary_uses_base_with_jitter`
- **Given**: broken 상태, consecutive_failures=3 (just-transitioned)
- **When**: `_compute_next_fetch_at(...)`
- **Then**: `result - now`이 `[45s, 75s]` 범위 (60s ± 25%)
- **방어선**: Tier 3 (jitter)

> 추가로 `test_compute_next_fetch_at_broken_feed_exponential_factor`,
> `test_compute_next_fetch_at_broken_feed_capped_at_max_backoff` 등이
> backoff 곡선과 cap을 검증.

### B3 — 동일 호스트 100개 피드 → 100 동시 GET

#### TC-B3-01 ❌ — per-host RPS throttle 동작
- **위치**: 미구현 — `tests/test_fetch_one.py::test_fetch_one_per_host_throttle` (planned)
- **Given**: 같은 호스트의 피드 5개, throttler `rps=1, burst=2`
- **When**: 5개를 동시에 fetch_one 호출
- **Then**:
  - 첫 2개는 즉시 fetch
  - 이후 3개는 1초 간격으로 fetch (총 ~3초 소요)
  - 모두 200 받음 (upstream 폭격 없음)
- **방어선**: Tier 1 (per-host throttle, 미구현)
- **PR 후보**: **P1 운영**

#### TC-B3-02 ❌ — 다른 호스트는 throttle 적용 안 됨
- **위치**: 미구현 — `test_fetch_one_per_host_throttle_isolates_hosts` (planned)
- **Given**: medium.com 피드 5개 + dev.to 피드 5개
- **When**: 동시 fetch
- **Then**: 두 호스트는 독립적으로 throttle (총 시간 ≈ 단일 호스트 5개와 동일)

### B4 — 429 폭탄

#### TC-B4-01 ✅ — Retry-After (정수 초) 존중
- **위치**: `tests/test_fetch_one.py::test_fetch_one_429_honors_retry_after_header`
- **Given**: mock 429 + `Retry-After: 300`
- **When**: `fetch_one(now=N, interval_seconds=60)`
- **Then**: `next_fetch_at = N + 300s`, `last_error_code='rate_limited'`

#### TC-B4-02 ✅ — Retry-After가 base interval보다 짧으면 floor
- **위치**: `tests/test_fetch_one.py::test_fetch_one_429_floors_retry_after_at_base_interval`
- **Given**: `Retry-After: 10`, `interval_seconds=60`
- **Then**: `next_fetch_at = N + 60s` (floor 적용)

#### TC-B4-03 ✅ — Retry-After (HTTP-date) 존중
- **위치**: `tests/test_fetch_one.py::test_fetch_one_429_honors_retry_after_http_date`
- **Given**: `Retry-After: Sat, 11 Apr 2026 00:05:00 GMT`
- **Then**: `next_fetch_at - now == 300s`

#### TC-B4-04 ✅ — 429는 circuit breaker 트립 아님
- **위치**: `tests/test_fetch_one.py::test_fetch_one_429_is_not_a_circuit_breaker_failure`
- **Given**: active 피드, 429 응답
- **Then**: `consecutive_failures` **변화 없음**, `status` 그대로

> `_parse_retry_after`에 대한 단위 테스트 5개가 별도 존재
> (`test_parse_retry_after_*` 6개).

---

## 5. 카테고리 C: 우리 인프라 한계

### C1 — Postgres 커넥션 풀 고갈

#### TC-C1-01 ❌ — pool_size 초과 시 graceful 대기
- **위치**: 미구현 — `tests/test_db_pool.py::test_pool_saturation_blocks_not_errors` (planned)
- **Given**: `pool_size=2, max_overflow=0`, 4개 동시 fetch_one
- **When**: 동시 실행
- **Then**:
  - 처음 2개 즉시 진행
  - 나머지 2개는 대기 후 진행 (에러 없음)
  - 풀 빠지면 `pool_pre_ping`이 dead connection 감지
- **PR 후보**: P1 운영

### C2 — retention sweep 1시간 초과

#### TC-C2-01 ❌ — cursor batch sweep 동작
- **위치**: 미구현 — `tests/test_retention.py::test_sweep_cursor_batches` (planned)
- **Given**: entries 100,000개 (cutoff 통과)
- **When**: `sweep_once(batch_size=1000)`
- **Then**:
  - 100회의 batch DELETE 발사
  - 각 batch 사이에 commit (long transaction 방지)
  - 총 시간 < N초 (벤치마크)
- **PR 후보**: P2

### C3 — tick_once 예외 propagate

#### TC-C3-01 ✅ — 빈 DB에서 tick_once는 no-op
- **위치**: `tests/test_scheduler_tick.py::test_tick_once_with_no_active_feeds_is_noop`
- **Given**: feeds 0개
- **When**: `tick_once`
- **Then**: 예외 없음, return cleanly
- **방어선**: Tier 2 (try/except — `run()` 루프에서 catch)


codex
이 저장소는 “피드 URL 등록 → 주기적 수집/갱신 → 엔트리 조회 API 제공”을 담당하는 독립 fetcher 서비스입니다.  
현재 구현은 walking skeleton을 넘어, `상태머신(active/broken/dead)`, `SSRF 방어`, `SKIP LOCKED 분산 claim`, `304/ETag 조건부 요청`, `retention sweep`까지 들어가 있습니다.

**구조 요약**
```text
feedgate-fetcher
├─ src/feedgate
│  ├─ main.py            앱 팩토리 + lifespan(스케줄러/retention 태스크)
│  ├─ config.py          FEEDGATE_* 설정
│  ├─ models.py          Feed / Entry ORM + 인덱스
│  ├─ api/
│  │  ├─ feeds.py        등록/조회/삭제/reactivate
│  │  ├─ entries.py      keyset 페이지네이션 조회
│  │  └─ health.py
│  ├─ fetcher/
│  │  ├─ scheduler.py    claim(SKIP LOCKED) + 병렬 fetch 실행
│  │  ├─ http.py         fetch_one 핵심 파이프라인
│  │  ├─ parser.py       feedparser 래퍼
│  │  └─ upsert.py       ON CONFLICT upsert
│  ├─ retention.py       보존 스윕(시간창 ∪ feed별 top-N)
│  ├─ ssrf.py            URL/해결 IP 검증 + transport guard
│  └─ errors.py          RFC7807 Problem Details
├─ alembic/versions      0001~0005 스키마 진화
├─ tests                 API/파서/수집/스케줄러/보안/보존/E2E
└─ docs/adr + docs/spec  설계 의사결정/계약 문서
```

**컴포넌트 관계 (핵심)**
```text
[Client]
   |
   v
[FastAPI App]
   |-- /v1/feeds, /v1/entries, /healthz
   |-- request session(dep): commit/rollback
   |
   +--> [Postgres]
   |
   +--> (lifespan)
         |-- [Scheduler Loop] --claim--> feeds(SKIP LOCKED lease)
         |         |--parallel fetch--> [fetch_one]
         |                              |-> SSRF validate + SSRFGuardTransport
         |                              |-> HTTP GET(stream, timeout, max_bytes)
         |                              |-> parse_feed(feedparser)
         |                              |-> upsert_entries(ON CONFLICT)
         |                              |-> feed status/timer 갱신
         |
         |-- [Retention Loop]
                   |-> DELETE old entries
                      keep = (fetched_at>=cutoff) UNION (feed별 최신 N개)
```

**데이터 모델**
- `Feed`: 사용자 `url`(불변), `effective_url`(301 반영), `status`, fetch 시각/에러코드, claim/백오프 메타데이터, `etag/last_modified`.
- `Entry`: 안정 식별 `(feed_id, guid)`, 본문 필드, `fetched_at`(최초 저장 시각, 불변), `content_updated_at`(내용 변경 시각).
- 인덱스가 쿼리/스케줄 조건과 맞게 설계됨.
- 참고 파일: [models.py](/Users/kangheeyong/project/feedgate-fetcher/src/feedgate/models.py), [0001_initial_schema.py](/Users/kangheeyong/project/feedgate-fetcher/alembic/versions/0001_initial_schema.py)

**핵심 서비스 로직**
1. 피드 등록
- URL 정규화 + SSRF 1차 검증(resolve=False) 후 `ON CONFLICT DO NOTHING`으로 멱등 생성.
- 파일: [feeds.py](/Users/kangheeyong/project/feedgate-fetcher/src/feedgate/api/feeds.py), [urlnorm.py](/Users/kangheeyong/project/feedgate-fetcher/src/feedgate/urlnorm.py)

2. 스케줄링/클레임
- due feed를 `FOR UPDATE SKIP LOCKED`로 claim하고 `next_fetch_at` lease 선점.
- 다중 worker 환경에서 중복 처리 방지.
- 파일: [scheduler.py](/Users/kangheeyong/project/feedgate-fetcher/src/feedgate/fetcher/scheduler.py)

3. fetch_one 파이프라인
- SSRF 재검증(resolve=True) + transport 레벨 재검증(redirect hop 포함)
- 스트리밍 수신, body cap, total budget timeout
- 429/410/304/301/5xx/content-type/oversize 등 세분 처리
- 상태 전이(active→broken→dead), 백오프+jitter, 주간 발행량 기반 다음 fetch 간격 계산
- 파일: [http.py](/Users/kangheeyong/project/feedgate-fetcher/src/feedgate/fetcher/http.py), [ssrf.py](/Users/kangheeyong/project/feedgate-fetcher/src/feedgate/ssrf.py)

4. 엔트리 저장
- `ON CONFLICT(feed_id,guid) DO UPDATE ... WHERE IS DISTINCT FROM ...`
- 동일 payload는 no-op, 변경 시 `content_updated_at`만 갱신, `fetched_at` 유지
- 파일: [upsert.py](/Users/kangheeyong/project/feedgate-fetcher/src/feedgate/fetcher/upsert.py)

5. 조회 API
- feeds: id 기반 keyset(cursor)
- entries: `(published_at DESC, id DESC)` keyset(cursor), `feed_ids` 필수
- 파일: [entries.py](/Users/kangheeyong/project/feedgate-fetcher/src/feedgate/api/entries.py)

6. 보존 스윕
- 시간창 + feed별 top-N 합집합 유지 규칙
- 파일: [retention.py](/Users/kangheeyong/project/feedgate-fetcher/src/feedgate/retention.py)

**테스트 전략**
- testcontainers Postgres 기반 통합 테스트 중심.
- 커버리지 축:
- API 계약/인증/에러 envelope
- fetch_one 상태머신/429/304/조건부요청/빈도기반 스케줄
- scheduler claim race(SKIP LOCKED), per-host 동시성, graceful drain
- SSRF 단위+통합
- retention 규칙
- E2E walking skeleton
- 참고 파일: [test_fetch_one.py](/Users/kangheeyong/project/feedgate-fetcher/tests/test_fetch_one.py), [test_scheduler_tick.py](/Users/kangheeyong/project/feedgate-fetcher/tests/test_scheduler_tick.py), [test_ssrf.py](/Users/kangheeyong/project/feedgate-fetcher/tests/test_ssrf.py), [test_e2e_walking_skeleton.py](/Users/kangheeyong/project/feedgate-fetcher/tests/test_e2e_walking_skeleton.py)

**눈에 띄는 설계 결정**
- 앱 상태를 lifespan 이전에 동기적으로 채워 테스트(ASGITransport)와 운영 경로를 일치시킴.
- 스케줄러 claim 트랜잭션과 fetch 트랜잭션을 분리해 장애 격리.
- 내부 메타데이터는 API에 숨기고, 상태/오류는 API에 노출.
- RFC7807 에러 포맷 일관화.
- 참고: [main.py](/Users/kangheeyong/project/feedgate-fetcher/src/feedgate/main.py), [errors.py](/Users/kangheeyong/project/feedgate-fetcher/src/feedgate/errors.py), [ADR 003](/Users/kangheeyong/project/feedgate-fetcher/docs/adr/003-collection-is-internal-implementation.md)

**추가 관찰**
- 문서 일부(`docs/spec/resilience.md`, `docs/tests/resilience-test-cases.md`)는 “미구현” 표기가 남아 있지만, 코드/테스트는 이미 상당수 구현됨.
- `ErrorCode.PARSE_ERROR` enum은 있으나 `fetch_one` 분류 경로에서는 사실상 거의 안 타는 구조(현재 parse 실패가 일반 예외 경로로 갈 가능성).  
  이 부분은 문서-코드 정합성 점검 포인트입니다.
tokens used
138,166

```

## Concise summary

Provider completed successfully. Review the raw output for details.

## Action items

- Review the response and extract decisions you want to apply.
- Capture follow-up implementation tasks if needed.
