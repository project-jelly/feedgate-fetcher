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

