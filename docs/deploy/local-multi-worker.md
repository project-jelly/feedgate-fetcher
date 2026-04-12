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
| Graceful shutdown | uvicorn 기본 (drain 없음) | resilience.md C5 항목 구현 후 |

이 항목들은 별도 PR로 진행한다. 현재 docker compose 구성은 **로컬 멀티 워커 검증**과 **k8s manifest 작성 전 단계**의 위치를 잡는다.

## 7. 다음 단계

1. `docker compose build && docker compose up -d --scale worker=3 worker`로 멀티 워커 동작 확인
2. `live_verify.py`로 24시간 정도 돌려서 entries 누락/중복 0 확인
3. Worker 강제 종료/재기동 시나리오로 lease TTL 자동 복구 검증
4. 성공하면 → k8s manifest PR로 넘어감
