# Spec: Entry

- 상태: Draft (구현 전)
- 마지막 업데이트: 2026-04-10
- 관련 ADR: 000, 001, 002, 004

이 문서는 `entries` 엔티티의 **현재 구현 정의**다. 정책 결정은 ADR에 있고,
이 spec은 테이블 스키마와 동작을 기술한다. Spec은 ADR 001의 불변식을
깨지 않는 한 자유롭게 갱신할 수 있다.

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
