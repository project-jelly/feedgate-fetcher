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
| `FEEDGATE_FETCH_TIMEOUT_SECONDS` | `20` | Per-request HTTP timeout |
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
