# Test Cases: Resilience

위협 ID → TC 매핑. 구현된 항목은 아래 테스트로 회귀를 막는다.
미구현 항목(❌)은 `resilience.md` §5 우선순위 목록을 참조한다.

## TC 매핑

| 위협 | 상태 | 테스트 위치 |
|---|---|---|
| A1 거대 응답 (body cap) | ✅ | `test_fetch_one.py::test_fetch_one_rejects_oversized_response_as_too_large` |
| A3 Slow Loris (wall-clock budget) | ✅ | `test_fetch_one.py::test_fetch_one_total_budget_kills_slow_response` |
| A4 첫 fetch entries cap | ✅ | `test_fetch_one.py::test_fetch_one_caps_initial_fetch_entries` |
| A4 재fetch cap 미적용 | ✅ | `test_fetch_one.py::test_fetch_one_no_cap_on_subsequent_fetch` |
| A6 TLS 에러 분류 | ✅ | `test_fetch_one.py::test_classify_error_tls` |
| A8 active → broken | ✅ | `test_fetch_one.py::test_fetch_one_active_to_broken_after_n_failures` |
| A8 broken → dead (시간 기반) | ✅ | `test_fetch_one.py::test_fetch_one_broken_to_dead_after_duration_since_last_success` |
| A8 broken → dead (created_at fallback) | ✅ | `test_fetch_one.py::test_fetch_one_broken_to_dead_falls_back_to_created_at` |
| A8 410 즉시 dead | ✅ | `test_fetch_one.py::test_fetch_one_http_410_transitions_to_dead_immediately` |
| A8 broken 유지 (7일 미경과) | ✅ | `test_fetch_one.py::test_fetch_one_broken_stays_broken_within_duration` |
| A8 broken → active 회복 | ✅ | `test_fetch_one.py::test_fetch_one_broken_to_active_on_success` |
| B1 일부 실패해도 나머지 진행 | ✅ | `test_scheduler_tick.py::test_tick_once_continues_when_one_feed_fails` |
| B2 thundering herd jitter | ✅ | `test_fetch_one.py::test_compute_next_fetch_at_broken_at_threshold_boundary_uses_base_with_jitter` |
| B3 per-host 직렬화 | ✅ | `test_scheduler_tick.py::test_per_host_throttle_serializes_same_host_feeds` |
| B3 다른 호스트 병렬 허용 | ✅ | `test_scheduler_tick.py::test_per_host_throttle_allows_distinct_hosts_in_parallel` |
| B4 429 Retry-After (초) | ✅ | `test_fetch_one.py::test_fetch_one_429_honors_retry_after_header` |
| B4 Retry-After floor | ✅ | `test_fetch_one.py::test_fetch_one_429_floors_retry_after_at_base_interval` |
| B4 Retry-After (HTTP-date) | ✅ | `test_fetch_one.py::test_fetch_one_429_honors_retry_after_http_date` |
| B4 429 circuit breaker 아님 | ✅ | `test_fetch_one.py::test_fetch_one_429_is_not_a_circuit_breaker_failure` |
| C3 tick 예외 삼킴 | ✅ | `test_scheduler_tick.py::test_tick_once_with_no_active_feeds_is_noop` |
| C4 SKIP LOCKED 더블 클레임 방지 | ✅ | `test_scheduler_tick.py::test_claim_due_feeds_skip_locked_prevents_double_claim` |
| C4 lease bump | ✅ | `test_scheduler_tick.py::test_claim_due_feeds_advances_lease` |
| C5 graceful drain (clean exit) | ✅ | `test_lifespan_drain.py::test_drain_background_task_clean_exit_within_budget` |
| C5 graceful drain (force cancel) | ✅ | `test_lifespan_drain.py::test_drain_background_task_force_cancels_on_timeout` |
| C5 in-flight fetch drain | ✅ | `test_lifespan_drain.py::test_drain_waits_for_truly_in_flight_fetch_to_complete` |
| D1/D2 사설 IP 차단 (등록) | ✅ | `test_ssrf.py::test_post_feed_rejects_blocked_url` |
| D1/D2 DNS rebinding 차단 | ✅ | `test_ssrf.py::test_validate_blocks_dns_rebinding_to_private_ip` |
| D1/D2 사설 IP 차단 (fetch) | ✅ | `test_ssrf.py::test_fetch_one_marks_blocked_when_host_resolves_to_private_ip` |
| D3 file:// scheme 차단 | ✅ | `test_ssrf.py::test_validate_blocks_unsupported_scheme` |
| D4 redirect SSRF transport 차단 | ✅ | `test_ssrf.py::test_transport_guard_blocks_redirect_target` |
| D6 멱등 POST | ✅ | `test_api_feeds.py::test_post_feed_is_idempotent` |
| ETag / If-None-Match 송신 | ✅ | `test_fetch_one.py::test_fetch_one_sends_if_none_match_on_second_fetch` |
| If-Modified-Since 송신 | ✅ | `test_fetch_one.py::test_fetch_one_sends_if_modified_since_when_no_etag` |
| 304 Not Modified 처리 | ✅ | `test_fetch_one.py::test_fetch_one_304_schedules_next_fetch_without_updating_success_fields` |
| dead 주간 probe | ✅ | `test_scheduler_tick.py::test_tick_once_probes_stale_dead_feed` |
| 수동 reactivate | ✅ | `test_api_feeds.py::test_reactivate_dead_feed_flips_to_active_and_resets_counters` |

## 미구현 (TC 없음)

| 위협 | 항목 | 우선순위 |
|---|---|---|
| A2 | 압축 폭탄 회귀 TC | P2 |
| A7 | 매 fetch entries cap | P2 |
| C1 | PG pool 고갈 시나리오 | P1 |
| C2 | retention cursor batch | P2 |
| D5 | API rate limit | P2 |

## 작성 컨벤션

1. `now: datetime` 명시 주입 — `datetime.now(UTC)` 직접 호출 금지
2. Feed seed 시 `next_fetch_at` 명시 — `func.now()` 기본값 의존 금지
3. HTTP는 `respx`로 모킹, 실제 외부 호출 없음
4. 방어 레이어가 여러 개면 각 레이어 분리 검증 (등록 단계 + fetch 단계)
