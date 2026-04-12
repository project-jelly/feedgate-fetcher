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

> ⚠️ `run()` 루프 자체의 예외 catch는 TDD-exempt (WP 4.3) — 명시적 TC 없음

### C4 — 워커 OOM 후 lease TTL 재배정

#### TC-C4-01 ✅ — SKIP LOCKED race에서 더블 클레임 방지
- **위치**: `tests/test_scheduler_tick.py::test_claim_due_feeds_skip_locked_prevents_double_claim`
- **Given**: 1개 feed, 두 워커 동시 claim 시도, A가 락 유지
- **When**: A가 락 든 채로 B가 claim 시도
- **Then**:
  - A: `[feed_id]` 받음
  - B: `[]` (SKIP LOCKED로 invisible)
- **방어선**: Tier 3 (lease TTL이 핵심 — 이 TC는 in-flight race를 검증)

#### 보조 TC: TC-C4-02 ✅ — Lease bump가 다음 tick 재-claim 방지
- **위치**: `tests/test_scheduler_tick.py::test_claim_due_feeds_advances_lease`
- **Given**: 1개 feed
- **When**: claim 1회 → commit → 같은 `now`로 claim 다시
- **Then**:
  - 첫 claim: `[feed_id]`
  - 두 번째: `[]` (next_fetch_at, last_attempt_at bump 효과)

### C5 — Rolling deploy SIGTERM (in-flight 손실)

#### TC-C5-01 ❌ — graceful drain
- **위치**: 미구현 — `tests/test_main_lifespan.py::test_lifespan_drains_inflight_fetches` (planned)
- **Given**: lifespan 진입, 워커가 fetch 진행 중
- **When**: `stop_event.set()` (SIGTERM 시뮬레이션)
- **Then**:
  - 새 tick 시작 안 함
  - 진행 중 fetch는 grace timeout 안에 완료 시도
  - timeout 초과 시 cancel, 미완료 lease는 다음 워커가 자동 재시도
- **PR 후보**: P1 운영

### C6 — DB 일시 단절

#### TC-C6-01 ❌ — DB 단절 시 백오프 (로그 폭주 방지)
- **위치**: 미구현 — `tests/test_scheduler_tick.py::test_tick_once_backs_off_on_db_unavailable` (planned)
- **Given**: scheduler 루프 진행 중, Postgres 연결 거절
- **When**: tick 시도
- **Then**:
  - 첫 실패는 즉시 로그
  - 이후 N초 backoff 후 재시도 (매 tick마다 로그 폭주 안 함)
- **PR 후보**: P3

---

## 6. 카테고리 D: 악의적 / SSRF

### D1 — AWS metadata service SSRF

#### TC-D1-01 ❌ — 등록 시 사설 IP 차단 (`POST /v1/feeds`)
- **위치**: 미구현 — `tests/test_api_feeds.py::test_post_feed_blocks_aws_metadata_url` (planned)
- **Given**: API client
- **When**: `POST /v1/feeds {"url": "http://169.254.169.254/latest/meta-data/"}`
- **Then**:
  - 응답 `400 Bad Request`
  - DB에 row 생성 안 됨
  - 에러 메시지: `"private/internal address blocked"`
- **방어선**: Tier 1 (SSRF IP 검증)
- **PR 후보**: **P0 보안**

#### TC-D1-02 ❌ — fetch-time 재검증 (DNS rebinding 방어)
- **위치**: 미구현 — `tests/test_fetch_one.py::test_fetch_one_revalidates_host_at_fetch_time` (planned)
- **Given**: 등록 당시엔 public IP였지만 fetch 시점에 DNS가 사설 IP로 응답
- **When**: `fetch_one`
- **Then**:
  - fetch 거부
  - `last_error_code = "ssrf_blocked"`
- **PR 후보**: P0 보안 (D1-01과 한 PR)

### D2 — 내부 포트 정찰

#### TC-D2-01 ❌ — 등록 시 loopback 차단
- **위치**: 미구현 — `tests/test_api_feeds.py::test_post_feed_blocks_loopback_url` (planned)
- **Given**: `POST /v1/feeds {"url": "http://127.0.0.1:5432/"}`
- **Then**: `400 Bad Request`, DB row 없음
- **PR 후보**: P0 (D1과 동일 PR)

### D3 — `file://` URL

#### TC-D3-01 ❌ — 스킴 화이트리스트
- **위치**: 미구현 — `tests/test_api_feeds.py::test_post_feed_blocks_non_http_scheme` (planned)
- **Given**: `POST /v1/feeds {"url": "file:///etc/passwd"}`
- **Then**: `400 Bad Request`
- **현재 상태**: httpx가 file:// 미지원이라 fetch 단계에서 에러 나지만, **등록 단계에서 명시적 차단 없음**. 등록은 통과하고 fetch가 실패함 → 운영자가 보면 혼란
- **PR 후보**: P0 (D1과 동일 PR)

### D4 — Redirect SSRF

#### TC-D4-01 ❌ — 301이 사설 IP로 점프 시 차단
- **위치**: 미구현 — `tests/test_fetch_one.py::test_fetch_one_blocks_redirect_to_private_ip` (planned)
- **Given**: public URL에 fetch, mock이 `301 Location: http://169.254.169.254/`
- **When**: `fetch_one`
- **Then**:
  - redirect follow 안 함
  - `last_error_code = "redirect_blocked"`
- **현재 상태**: `httpx`가 `follow_redirects=True`로 무조건 따라감. **수동 검증 필요**
- **PR 후보**: P0

### D5 — API 폭주

#### TC-D5-01 ❌ — `POST /v1/feeds` rate limit
- **위치**: 미구현 — `tests/test_api_feeds.py::test_post_feed_rate_limited` (planned)
- **Given**: API rate limit 10 req/s/IP
- **When**: 같은 IP에서 1초에 50회 `POST /v1/feeds`
- **Then**:
  - 처음 ~10개는 200/201
  - 나머지는 `429 Too Many Requests`
- **PR 후보**: P2

### D6 — 멱등 POST

#### TC-D6-01 ✅ — 같은 URL N회 POST는 멱등
- **위치**: `tests/test_api_feeds.py::test_post_feed_is_idempotent`
- **Given**: 이미 등록된 URL
- **When**: 같은 URL `POST /v1/feeds` 두 번째
- **Then**:
  - 두 번째 응답은 `200 OK` (201 아님)
  - DB row는 1개 (중복 INSERT 안 함)
- **방어선**: Tier 3 (UNIQUE constraint + idempotent path)

---

## 7. 보조 TC (직접 카테고리 없음, 그러나 방어 모델 핵심)

### upsert idempotent (entries 단위 보호)

| TC | 위치 |
|---|---|
| TC-U-01 ✅ identical payload no-op | `tests/test_upsert.py::test_upsert_identical_payload_is_noop` |
| TC-U-02 ✅ title 변경 → content_updated_at만 갱신 | `test_upsert_changed_title_updates_content_updated_at_only` |
| TC-U-03 ✅ published_at 변경도 감지 | `test_upsert_changed_published_at_also_updates` |
| TC-U-04 ✅ 신규 entry는 fetched_at == content_updated_at | `test_upsert_new_entry_sets_both_timestamps_equal` |

### Walking skeleton E2E

| TC | 위치 |
|---|---|
| TC-E2E-01 ✅ 등록 → 수집 → 조회 전체 흐름 | `tests/test_e2e_walking_skeleton.py::test_walking_skeleton_happy_path` |

### Lifecycle 보조

| TC | 위치 |
|---|---|
| TC-L-01 ✅ 성공 시 fail counter 리셋 | `test_fetch_one_second_success_resets_failure_counter` |
| TC-L-02 ✅ dead 피드 주간 probe 동작 | `test_tick_once_probes_stale_dead_feed` |
| TC-L-03 ✅ 주간 probe 범위 안엔 skip | `test_tick_once_skips_recently_probed_dead_feed` |
| TC-L-04 ✅ 수동 reactivate (dead → active) | `test_reactivate_dead_feed_flips_to_active_and_resets_counters` |
| TC-L-05 ✅ 수동 reactivate (broken → active) | `test_reactivate_broken_feed_also_works` |

---

## 8. 미구현 TC 우선순위 (PR로 묶기)

| 우선순위 | TC 묶음 | 추정 PR |
|---|---|---|
| **P0 보안** | TC-D1-01, TC-D1-02, TC-D2-01, TC-D3-01, TC-D4-01 (5개) | 1 PR — SSRF + scheme 차단 |
| **P1 운영** | TC-A3-01 (slow loris), TC-C5-01 (graceful drain) | 별도 2 PR |
| **P1 운영** | TC-B3-01, TC-B3-02 (per-host throttle 2개) | 1 PR |
| **P1 운영** | TC-C1-01 (PG pool 고갈) | 1 PR |
| **P2 비용** | TC-A7-01 (fetch당 entries cap), TC-A2-01 (압축 폭탄) | 별도 2 PR |
| **P2 운영** | TC-C2-01 (retention cursor batch), TC-D5-01 (API rate limit) | 별도 2 PR |
| **P3** | TC-A5-01 (redirect cap), TC-A6-01 (TLS 분류), TC-C6-01 (DB backoff) | 별도 3 PR |

## 9. 테스트 작성 컨벤션

새 resilience TC를 작성할 때 다음을 따른다:

1. **Fixture 재사용**: `fetch_app`, `api_client`, `respx_mock`, `async_session_factory`. 새 fixture 만들지 말 것.
2. **시간은 명시적으로 주입**: `now: datetime` 인자로 받고, `datetime.now(UTC)` 직접 호출 금지 (날짜 경계 회귀 방지 — TC-C4-01 류 회귀 사례 참고)
3. **Feed seed 시 `next_fetch_at` 명시**: `func.now()` 기본값 의존 금지 (PR #9 회귀 사례)
4. **HTTP는 respx로 모킹**: 실제 HTTP 호출 금지
5. **assertion은 단일 TC 1개 시나리오**: 한 테스트 함수에 여러 시나리오 묶지 말 것. parametrize 사용 가능.
6. **방어선이 멀티 레이어면 각 레이어를 분리 검증**: 예) SSRF는 등록 단계 + fetch 단계 두 TC.

## 10. 메트릭 회귀 검증

위 TC가 다 통과한다고 해서 운영에서도 동작한다는 보장은 없다. 운영 메트릭과
연결되어야 한다. 다음 metric들은 PR마다 함께 검증한다 ([resilience.md §6](../spec/resilience.md#6-메트릭-tier-3-recover의-가시성)):

- `ssrf_blocked_total` — D1/D2/D4 PR 후 0보다 커지면 차단 동작 증거
- `host_throttle_wait_seconds_bucket` — B3 PR 후 throttle 발생 검증
- `claim_lease_recoveries_total` — C4 시나리오 운영 데이터로 검증
- `fetch_outcome_total{code="..."}` — A1, A6, B4 분포 추적

## 11. 미해결 / 후속 결정 필요

- TC-A6-01: `tls_error` 별도 분류 추가가 의미 있는지 — `connection`으로 묶어도 OK인가?
  → ADR 추가 검토.
- TC-A7-01: per-fetch entries cap의 기본값 제안 — 200? 500? 무제한 유지?
  → 운영 데이터 보고 결정.
- TC-D5-01: API rate limit을 FastAPI 미들웨어 vs nginx/ingress 어디서 처리?
  → 인프라 ADR 필요.
