# Local User UAT Backend

이 모드는 실제 서비스와 유사한 사용자 흐름을 로컬에서 확인하기 위한
`LOCAL UAT / DIAGNOSTIC` 전용 실행 경계다. Production 환경을 완화하거나
승인된 Release로 가장하는 기능이 아니다.

## 보장하는 경계

- `environment=local_uat` runtime은 PostgreSQL, MinIO, Redis/Dramatiq,
  OIDC, HTTP Agent adapter만 선택한다. In-memory 또는 Mock Agent로
  자동 대체하지 않는다.
- Claude mode는 `actual`, text-only이며 Backend 자동완료는 항상 OFF다.
- Production release-only pin, signed policy, assertion signing key,
  자동완료 approval을 넣으면 Settings 검증 단계에서 시작을 거절한다.
- `/health/ready`는 Local UAT에서 항상 503이다. Production readiness와
  UAT 실행 가능성을 섞지 않는다.
- `/health/uat-ready`는 OIDC, PostgreSQL, MinIO, Redis, 실제 Agent의
  `/health/uat-ready`와 V2 metadata가 모두 준비되고 diagnostic identity가
  일치할 때만 200이다. 이후 각 review 요청도 Agent가 Evidence readiness를
  다시 fail-closed로 검사한다.
- `/api/v1/uat/metadata`는 검증된 V3 Evidence identity에서 가져온
  `supported_review_as_of`와 명시적인 `evidence_profile`, `evidence_scope`,
  `evidence_data_source`, `evidence_policy_mode`를 포함한 UI 표시용 metadata만
  반환한다.
  Agent metadata가 없거나 identity/date/digest가 잘못되면 503으로 닫힌다.
- Review Run의 `result_detail.model_execution_summary`는 Anthropic provider,
  model ID, text-only 여부, 호출 수, 토큰 합계, prompt/schema hash
  완전성과 runtime identity 일치 여부만 보존한다. Case Chat도
  `generation_mode`과 동일한 bounded summary를 남긴다. request/message ID,
  hash 값, raw prompt/response, 원문, credential은 public record에 넣지 않는다.

## Evidence profile

- `synthetic-v3`: 전체 제품 흐름용 합성 Evidence. 화면에는
  `TEST_ONLY_V3`로 표시하며 RAG 품질 증거로 사용하지 않는다.
- `preserved-3law-v3-diagnostic`: 보존된 실제 3법령 진단 corpus를
  PostgreSQL structured lookup, Qdrant dense/BM25, Neo4j graph expansion으로
  조회하는 process-level 실행 프로필이다. 화면에는
  `ACTUAL_3_LAW_RAG_3_OF_58`로 표시하며 법적 coverage나 Production readiness로
  해석하지 않는다.
- `preserved-3law-v3-scenario-eval`: 동일한 read-only 3법령 Knowledge
  snapshot을 9개 사용자 시나리오에서 실제 조회한다. 화면에는
  `ACTUAL_3_LAW_RAG_SCENARIO_EVAL_3_OF_58`로 표시하고 정책 모드는
  `SEALED_TEST_ONLY_ACTUAL_CCCT_HUMAN_ONLY`로 고정한다. 이는 실제 법적 승인이나
  Production 자동완료 승인이 아니라 서명 검증된 UAT fixture identity다.
- `unknown`: 원천을 확인하지 못한 안전한 기본값. UAT 결과를 증거로
  채택하기 전에 명시적인 profile로 다시 실행한다.

Backend는 Harness가 봉인된 Data 진단 manifest에서 주입한
`CUSTOMS_BACKEND_UAT_EVIDENCE_*` 관측값을 시작 시 검증한다. 실제 3법령
프로필은 `3/58`, PostgreSQL 조문 수, Qdrant point 수, Neo4j 조문 수가
모두 결박되고 세 저장소 수가 일치해야 시작된다. `/api/v1/uat/metadata`는
이 안전한 개수와 `rag_quality_evaluated=false`를 공개하지만 Data URL,
DSN, token, 원본 manifest 경로는 공개하지 않는다.

Scenario-eval 프로필은 세 Store 모두 정확히 `1,007`건이어야 하며,
Backend→Agent timeout은 실제 Agent 실행 budget보다 큰 `435`초 이상이어야 한다.
프로필·Data source·정책 모드·개수 중 하나라도 다른 조합은 Settings 검증에서
시작을 거절한다. 기존 진단 프로필은 `ACTUAL_CODES_HUMAN_ONLY`, 합성 프로필은
`SYNTHETIC_FIXTURE` 정책 모드를 계속 사용한다.

Scenario-eval에서 사람 승인 assertion을 만드는 경우에만 Harness가 `_secrets`
밖으로 노출되지 않는 private Ed25519 registry와 Agent용 public trust store를
생성한다. Backend는 `uat-approver`의 exact authority, active 한 개 key,
public trust SHA를 시작 시 결박한다. 정책 fixture도 exact CCCT
`CCCT_DOC_46E819BB87B27371`, `human_only`, Data runtime SHA,
`test-only-three-law-scenario-eval-human-only.v1` release와 source SHA를
독립 검증한다. unknown code에는 assertion 정책을 적용하지 않는다. 이 경로,
private key, Data URL과 credential은 health, OpenAPI, 로그에 공개하지 않는다.

인증된 UI는 scenario-eval에서만
`GET /api/v1/uat/review-runs/{review_run_id}/evidence-trace`를 호출할 수 있다.
Backend는 해당 조직의 Review Run과 그 결과에 저장된 Evidence request ID,
Knowledge snapshot을 먼저 확인한 뒤 외부 JSONL의 exact 한 행만 결박한다.
응답은 Structured/Dense/BM25/Graph의 시도·성공, 결과 수, Top-K 수, 소요시간만
포함한다. provider identity, trace 경로, query·문서 원문, Data URL·token은
반환하지 않는다. Production과 다른 profile에서는 이 endpoint가 404다.

## 설정 소유권

이 저장소는 `deploy/uat/*.env.example`로 Backend 설정 계약만 소유한다.
실제 secret과 네 저장소를 조합하는 Compose/기동 스크립트는 개인
`_engineering` harness가 소유한다. 생성한 실제 env를 이 저장소에 두거나
커밋하지 않는다.

Runtime, migration, outbox에는 각각 분리된 env를 사용한다. Worker는 runtime
env를 사용하므로 동일한 PostgreSQL, MinIO, Redis, OIDC, Agent HTTP 경계를
선택한다. Outbox dispatcher에는 DB와 Redis 외 secret을 주지 않는다.

Evidence 프로필은 Review 요청의 사용자 입력이 아니다. Harness가 Data·Agent·Backend를
시작할 때 한 프로필을 선택하고 세 프로세스의 readiness identity가 정확히 일치해야
한다. 브라우저에는 현재 활성 프로필만 안전한 metadata로 공개하며 임의 URL, token,
process 재시작 API는 제공하지 않는다. 다른 프로필을 확인하려면 Harness가 기존 UAT를
내리고 같은 exact source로 다시 기동해야 한다.

## 판정 기준

Local UAT 준비 완료는 `/health/uat-ready=200`을 뜻한다. 결과가
`no_issue_detected`여도 승인되지 않은 Agent/Data/도메인 Release 상태에서는
`human_review_required`가 정상이며, `auto_review_complete`를 만들기 위해
release-only 값을 주입해서는 안 된다.
