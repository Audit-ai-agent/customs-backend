# 상용형 Local UAT Backend API v2

## 목적

이 API는 내부 심사 담당자가 텍스트 통관 문서를 실제 Claude와 보존된 3개 법령
Evidence 환경으로 검토하고, 검증된 설명 보고서와 멀티턴 심사 도우미를 사용할 수
있도록 Backend가 제공하는 사용자 경험 경계입니다.

이 경계는 법적 통관 승인이나 Production 자동완료 경계가 아닙니다. OCR·PDF·이미지,
58개 법령 전체 corpus, 도메인 전문가 승인도 포함하지 않습니다.

## 활성 조건

다음 조건을 모두 만족한 Local UAT runtime에서만 기능을 활성화합니다.

- `CUSTOMS_BACKEND_ENHANCED_AGENT_EXPERIENCE=true`
- `CUSTOMS_BACKEND_UAT_ADAPTIVE_ACTIVE=true`
- `preserved-3law-v3-scenario-eval` Evidence profile
- literal loopback Public Backend·Agent URL
- 24시간 이내의 Agent-owned capability SHA-256과 capability file SHA-256
- 자동완료 OFF

기능 스위치는 기본값이 모두 `false`입니다. migration, outbox, Production runtime,
다른 Evidence profile에서는 capability identity를 받을 수 없습니다.

## 공개 API

- `GET /api/v2/uat/metadata`
- `POST /api/v2/cases/{case_id}/reviews`
- `GET /api/v2/jobs/{job_id}`
- `GET /api/v2/jobs/{job_id}/events/stream`
- `GET /api/v2/review-runs/{review_run_id}/experience`
- `POST /api/v2/review-runs/{review_run_id}/assistant/conversations`
- `GET /api/v2/review-runs/{review_run_id}/assistant/conversations`
- `POST /api/v2/assistant/conversations/{conversation_id}/messages`
- `GET /api/v2/assistant/conversations/{conversation_id}/messages`
- `GET /api/v2/assistant/conversations/{conversation_id}/jobs`
- `GET /api/v2/assistant/jobs/{assistant_job_id}`
- `GET /api/v2/assistant/jobs/{assistant_job_id}/events/stream`

기존 `/api/v1` 요청·응답은 변경하지 않습니다. 현재 통합 OpenAPI는
`docs/openapi-v2.json`, 보존된 기존 계약은 `docs/openapi-v1.json`입니다.

## 신뢰 경계

Browser는 Agent URL·token, Evidence URL·token, 과거 대화 배열, Agent prompt를
제출하지 않습니다. Backend가 같은 조직·심사 건·심사 회차·결과 hash에 속한 최근
8개 메시지만 Agent 요청에 결박합니다.

심사 건의 문서 revision이 대화가 결박된 심사 회차보다 새로워지면, 이전 대화 이력은
조회할 수 있지만 새 질문은 `409`로 차단합니다. 담당자는 최신 문서로 재심사한 뒤 새
결과에 결박된 대화를 시작해야 합니다.

Agent 응답은 다음 순서로 검증한 후에만 사용자에게 공개합니다.

1. provider-owned JSON Schema와 consumer lock 검증
2. Agent 내부 OpenAPI route inventory·canonical hash와 capability 검증
3. Canonical Claim Catalog 독립 재구성
4. Claim·FactLedger·Citation 참조 검증
5. 숫자·날짜·HSK·문서번호 등 보호 값의 원본 존재 검증
6. 기존 Issue·Finding·Severity·Outcome 불변 검증
7. 검증된 결과와 Presentation 또는 Answer의 원자적 저장

검증 전 Claude token이나 raw response는 Browser로 전달하지 않습니다.

## 실행 단계와 재연결

Agent NDJSON stage event는 Backend가 검증한 뒤 안전한 `StageEvent`로 영속화합니다.
Agent terminal은 계약 완결성 확인에만 사용하고 공개 이력에 중복 저장하지 않습니다.
Backend가 결과 저장을 끝낸 뒤 `completed` terminal을 정확히 한 번 기록합니다.

SSE 재연결 시 `Last-Event-ID` 다음 sequence부터 이미 저장된 이벤트를 재전송합니다.
외부 호출 시작 이후 완료 여부를 증명할 수 없으면 재호출하지 않고
`delivery_uncertain`으로 종료합니다.

예상 시간은 같은 조직·Evidence profile·문서 수의 최근 성공 심사 30개를 사용합니다.
표본이 5개 이상이면 p50/p90, 미만이면 검증된 기본 범위인 4~7분을 표시합니다.
가짜 진행률은 만들지 않습니다.

`GET /api/v2/jobs/{job_id}`와 심사 생성 응답의 `job`은 기존 전체 예상 시간인
`eta`를 그대로 유지하고, 추가로 `stage_eta`를 제공합니다. 단계별 표본은 성공한
심사 건 하나를 한 개로 계산합니다. 같은 심사 건에서 같은 단계가 재시도되면 각
시도의 `duration_ms`를 먼저 합산하므로 재시도 횟수가 표본 수를 부풀리지 않습니다.

- `stage_eta.stages[].source=observed`: 같은 Evidence profile·문서 수에서 유효한
  성공 심사 표본이 5개 이상이며 `p50_seconds`, `p90_seconds`를 사용할 수 있습니다.
- `stage_eta.stages[].source=insufficient_samples`: 해당 단계의 유효 표본이 5개
  미만이며 단계별 p50/p90은 `null`입니다.
- `stage_eta.insufficient_sample_behavior=no_stage_estimate_use_overall_eta`: 단계별
  수치를 임의로 나누지 말고 전체 `eta`만 표시해야 한다는 뜻입니다. 전체 표본도
  5개 미만이면 기존 `validated_default` 4~7분 범위를 사용합니다.

Frontend는 `stage_eta.stages`의 순서를 실행 단계 표시 순서로 사용할 수 있습니다.
다만 `insufficient_samples`인 단계에는 시간 숫자를 표시하지 않고 “표본 수집 중”으로
표시하며, 전체 예상 범위는 계속 `eta.range_min_seconds`와
`eta.range_max_seconds`를 사용해야 합니다.

## 영속화와 Rollback

Migration `20260727_0008`은 다음 additive table만 생성합니다.

- `review_presentations`
- `assistant_conversations`
- `assistant_jobs`
- `assistant_messages`

기존 Review Run, Provider Result, Chat V1, HITL, Audit row는 수정하거나 변환하지
않습니다. Rollback 전에는 실행 중 Job을 drain하고 비식별 Assistant 데이터를
내보낸 후 기능 스위치를 끕니다. Data Platform의 image·volume·Knowledge snapshot은
이 과정에서 변경하지 않습니다.
