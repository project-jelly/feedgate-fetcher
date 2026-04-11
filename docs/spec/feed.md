# Spec: Feed

- 상태: Draft (구현 전)
- 마지막 업데이트: 2026-04-10
- 관련 ADR: 000, 001, 002, 003

이 문서는 `feeds` 엔티티의 **현재 구현 정의**다. 정책 결정은 ADR에 있고,
이 spec은 테이블 스키마와 동작을 기술한다. Spec은 ADR 001의 불변식을
깨지 않는 한 자유롭게 갱신할 수 있다.

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
| `etag` | text | NULL | 마지막 응답의 `ETag` 헤더 |
| `last_modified` | text | NULL | 마지막 응답의 `Last-Modified` 헤더 |
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
| `broken` | fetch 성공 (200/304) | `active` | counters 리셋 |
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
2. 헤더: `User-Agent`, 있으면 `If-None-Match`(etag), `If-Modified-Since`
   (last_modified)
3. 응답 코드별 처리:

| 상태 | 처리 |
|---|---|
| `200 OK` | Content-Type 검사 → 파싱 → 엔트리 저장 (spec/entry.md) → 성공 |
| `304 Not Modified` | 본문 없음. 성공 처리. 엔트리 변경 없음 |
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

| 필드 | 성공 (200 또는 304) | 실패 |
|---|---|---|
| `last_attempt_at` | 갱신 | 갱신 |
| `last_successful_fetch_at` | 갱신 | 유지 |
| `last_error_code` | `NULL`로 리셋 | 새 코드 설정 |
| `consecutive_failures` | `0`으로 리셋 | `+1` |
| `etag` | 새 값 또는 `NULL` | 유지 |
| `last_modified` | 새 값 또는 `NULL` | 유지 |
| `title` | 새 값이 있으면 갱신 | 유지 |
| `status` | 상태 머신 적용 | 상태 머신 적용 |
| `next_fetch_at` | 스케줄러가 재계산 | 스케줄러가 재계산 (보통 더 먼 미래) |

### 301 Permanent Redirect 처리

- Location 헤더의 절대 URL을 읽는다
- `effective_url`을 그 값으로 갱신
- `url` (사용자가 등록한 원본)은 **절대 건드리지 않는다**
- 같은 fetch 사이클에서 새 URL로 **1회만** 재시도 (중첩 금지, redirect
  chain은 상한 두고 처리)

### 네트워크 설정 (환경변수)

- `FETCH_TIMEOUT` — HTTP 요청 타임아웃 (기본 20초)
- `FETCH_MAX_BYTES` — 응답 본문 크기 상한 (기본 5MB)
- `FETCH_MAX_REDIRECTS` — redirect chain 상한 (기본 5)
- `FETCH_USER_AGENT` — User-Agent 문자열 (기본 `feedgate-fetcher/<ver> (+url)`)
- `FETCH_MAX_ENTRIES_INITIAL` — 첫 fetch 시 저장할 최대 엔트리 수 (기본 50).
  OpenAI 909건 같은 케이스 방어. 이후 fetch는 새 엔트리만 들어오므로 이
  상한과 무관.

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

스케줄러 메타데이터(`etag`, `next_fetch_at`, `consecutive_failures` 등)는
응답에 포함하지 않는다.

### 목록 — `GET /v1/feeds`

Keyset 페이지네이션. 정렬: `(id ASC)`. `?status=active` 필터 지원(옵션).

### 해제 — `DELETE /v1/feeds/{id}`

- 해당 row 삭제
- 연결된 `entries`는 `ON DELETE CASCADE`로 자동 삭제
- 멱등

### 수동 재활성화 (미정)

`dead`로 전이된 피드를 사용자가 다시 살리려면? 현재는 **DELETE 후 재등록**
으로만 가능. 전용 API는 필요성이 확인되면 추가 (미해결).

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
- [x] 304 Not Modified → 성공, 엔트리 변경 없음
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
- [x] 피드가 "조용함" (새 글 없음) → 정상. 스케줄러는 계속 돌고 304로 응답
- [x] Cloudflare/WAF가 봇 차단 → 403 → `http_4xx` 누적 → 결국 dead

## 실측 참고

seed 피드(22개) probe 결과(`docs/notes/collection-scaling-research.md`와
`/tmp/feedgate_probe.json`):

- 14/20 피드가 ETag 또는 Last-Modified 지원 (70%). 나머지는 매번 full fetch
- 0/20 피드가 WebSub hub 광고 → 이 시드 믹스에서는 push 최적화 여지 없음
- 응답 크기 최대 1.7MB (Chips and Cheese) → 5MB 상한이면 안전
- Uber RSS URL은 404 (죽음) → Uber 같은 케이스가 dead 전이의 대표 사례
- 응답 지연 p95 약 1.5초 → 20초 타임아웃이면 여유

## 미해결

- 수동 재활성화 API 경로
- URL 정규화 상세 (퍼센트 인코딩 대소문자, 중복 슬래시 등)
- `FETCH_MAX_ENTRIES_INITIAL` 기본값 확정 (50? 100?)
- 영구 에러 부류의 자동 승격 규칙(예: DNS 실패가 7일 지속되면 dead로)
