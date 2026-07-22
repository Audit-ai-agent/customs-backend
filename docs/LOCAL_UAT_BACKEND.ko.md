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
- `/api/v1/uat/metadata`는 UI 표시용 비밀정보 없는 metadata만 반환한다.

## Evidence profile

- `synthetic-v3`: 전체 제품 흐름용 합성 Evidence. 화면에는
  `TEST_ONLY_V3`로 표시하며 RAG 품질 증거로 사용하지 않는다.
- `preserved-3law`: 보존된 실제 3법령 진단 corpus. 화면에는
  `REAL_3_OF_58`로 표시하며 법적 coverage나 Production readiness로
  해석하지 않는다.
- `unknown`: 원천을 확인하지 못한 안전한 기본값. UAT 결과를 증거로
  채택하기 전에 명시적인 profile로 다시 실행한다.

## 설정 소유권

이 저장소는 `deploy/uat/*.env.example`로 Backend 설정 계약만 소유한다.
실제 secret과 네 저장소를 조합하는 Compose/기동 스크립트는 개인
`_engineering` harness가 소유한다. 생성한 실제 env를 이 저장소에 두거나
커밋하지 않는다.

Runtime, migration, outbox에는 각각 분리된 env를 사용한다. Worker는 runtime
env를 사용하므로 동일한 PostgreSQL, MinIO, Redis, OIDC, Agent HTTP 경계를
선택한다. Outbox dispatcher에는 DB와 Redis 외 secret을 주지 않는다.

## 판정 기준

Local UAT 준비 완료는 `/health/uat-ready=200`을 뜻한다. 결과가
`no_issue_detected`여도 승인되지 않은 Agent/Data/도메인 Release 상태에서는
`human_review_required`가 정상이며, `auto_review_complete`를 만들기 위해
release-only 값을 주입해서는 안 된다.
