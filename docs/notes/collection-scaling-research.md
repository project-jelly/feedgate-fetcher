# 수집 스케일 관련 조사 노트

- 작성일: 2026-04-10
- 상태: Reference (ADR 아님)
- 관련: ADR 000, ADR 003

## 배경

MVP는 단일 프로세스 + 고정 주기 폴링으로 시작한다(ADR 003). 그러나 피드
수가 늘거나 운영 중 병목이 드러날 때 어떤 선택지가 있는지를 미리 조사해
두었다. 이 문서는 결정이 아니라 **나중에 꺼내볼 참고 자료**이며, 향후 수집
전략을 손볼 때 출발점으로 삼는다.

## 본질 — RSS fetcher는 focused crawler와 같은 문제

주어진 URL 집합을 주기적으로 재방문하고, 변경 여부를 판단하고, 호스트에
정중하게 행동해야 한다. 크롤러 문헌에서 쓰이는 개념이 그대로 적용된다.

- URL frontier → 우리의 "다음 fetch 대상 큐"
- Adaptive re-visit policy (Cho & Garcia-Molina) → 발행 빈도 기반 주기 조정
- Politeness (per-host rate limit, robots.txt, 식별 가능한 UA)
- 조건부 요청 (ETag / If-Modified-Since)
- 콘텐츠 해시 기반 중복 탐지 → 우리의 `(feed_id, guid)` + 필요 시 content
  hash

## 피드 수가 늘 때 터지는 순서

1. **Per-host rate limit 부재** — 빅 호스트(medium, substack, tistory 등)에
   피드가 편중되면 가장 먼저 터진다. 글로벌 동시성만 제어하면 사실상 그
   호스트를 DDoS하게 되고 429/차단을 받는다.
2. **슬로우/거대 피드로 워커 고갈** — 타임아웃과 응답 크기 상한이 없으면
   한두 개 피드가 워커풀 전체를 먹는다.
3. **스케줄러 O(N) 스캔 + 재시작 thundering herd** — 재시작 시 모든 피드가
   `due` 상태가 되어 동시 폭주.
4. **개별 insert IOPS** — 엔트리를 하나씩 INSERT하면 N이 커질수록 DB가
   먼저 포화된다. 배치 upsert(`ON CONFLICT DO NOTHING`)가 사실상 강제.
5. **단일 프로세스 한계** — 수만 피드까지는 여유롭지만 그 이후는 수평
   확장 필요.
6. **Autovacuum / 인덱스 크기** — 수천만 row부터 체감.
7. **저장소 파티셔닝** — 보존 정책(ADR 004)이 있는 한 거의 오지 않는다.

## 상용 서비스의 접근

### Feedly Fetcher

- 분산 fetcher를 여러 머신에 배치.
- **적응형 폴링 주기** — 발행 빈도와 사이트 인기도에 따라 주기 조정.
  - Pro+/Enterprise: 최소 7분
  - Pro: 15분~1시간
  - Basic: 30분~1일
- 거대한 피드는 앞부분만 파싱(truncate).
- 식별 가능한 User-Agent (`Feedly/1.0`).
- 지속적으로 중복 제거 최적화.

### WebSub (구 PubSubHubbub)

- 피드가 `<link rel="hub">`로 허브를 광고하면 fetcher는 폴링 대신 **구독
  등록**만 하고, 새 글이 나올 때 허브가 HTTP POST로 밀어준다.
- Feedly는 WebSub 지원 피드의 폴링을 **하루 1회**로 떨어뜨려 부하를
  10~100배 절감.
- 2017년 W3C Recommendation으로 승격. 워드프레스·블로거·미디엄 일부가
  지원한다.

### Superfeedr

- "폴링 지옥을 대신 떠안아주는" 중개 서비스. WebSub 미지원 피드까지 자기
  쪽에서 폴링해서 구독자에게는 push로 전달한다.
- 폴링 확장성 자체를 상품화했다는 사실이 "폴링이 얼마나 어려운가"의
  방증이다.

## MVP 이후 적용 가능한 개선 기법

| 기법 | 예상 효과 | 복잡도 |
|---|---|---|
| Per-host 동시성 제한 + 호스트별 백오프 | 빅 호스트 편중 문제 해소 | 낮음 |
| 적응형 주기 (발행 빈도 기반) | 폴링 수 수 배 절감 | 낮음~중간 |
| 식별 가능한 User-Agent | 차단 협상 여지 | 매우 낮음 |
| 요청 타임아웃 + 응답 크기 상한 | 슬로우/거대 피드 방어 | 매우 낮음 (MVP 포함) |
| 피드 본문 truncate | 거대 피드 방어 | 낮음 |
| Jitter (등록·재시작 시) | Thundering herd 완화 | 낮음 |
| Claimer 추상화 (DB 클레임 → 큐) | 수평 확장 대비 | 중간 |
| WebSub 구독 지원 | 활성 피드 부하 10~100배 절감 | 중간 |
| 외부 큐 (Redis/NATS) | 수십만~백만 피드 | 높음 |
| 샤딩 / 파티셔닝 | 대규모 | 높음 |

## 재검토 트리거

다음 중 하나라도 충족되면 해당 영역을 다시 들여다본다. 재검토 결과가
유의미한 방향 전환이면 새 ADR로 승격한다.

- 등록된 피드 수 > **1,000**
- 한 호스트에 집중된 피드 수 > **20**
- 4xx/5xx 비율 > **5%** (지속)
- Fetch latency p95 > **5초** (지속)
- Fetcher 프로세스 CPU/메모리 > **70%** (지속)
- WebSub 허브를 노출하는 피드 비율 > **30%**

## 예상 스케일 구간

같은 하드웨어라는 가정하의 대략적 감(정확한 벤치 아님).

| 구성 | 감당 가능 피드 수 |
|---|---|
| 단일 프로세스 + 고정 주기 (MVP) | 수천 ~ 1만 |
| + per-host 제한 + 적응형 주기 | 수만 |
| + WebSub 구독 | 같은 하드웨어로 10배 상승 |
| + 분산 fetcher (DB 클레임) | 수십만 |
| + 외부 큐 / 샤딩 | 수백만 (Feedly·Inoreader 급) |

## 참고 자료

- Feedly Fetcher — https://feedly.com/fetcher.html
- Feedly 폴링 주기 문서 — https://docs.feedly.com/article/212-how-often-does-feedly-update
- Superfeedr — https://superfeedr.com/
- Superfeedr Blog — https://blog.superfeedr.com/pubsubhubbub.html
- WebSub (Wikipedia) — https://en.wikipedia.org/wiki/WebSub
- HN 논의 "Every user's RSS reader polling every website..." —
  https://news.ycombinator.com/item?id=29815726
