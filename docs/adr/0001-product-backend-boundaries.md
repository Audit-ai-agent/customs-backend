# ADR-0001: Text-only Product Backend 경계와 자율처리 권한

- 상태: Accepted for Production candidate
- 날짜: 2026-07-21

## 결정

FastAPI modular monolith을 Browser의 유일 제품 API로 두고 PostgreSQL을
Case·Job·Run·HITL·Chat·Audit의 source of truth로 사용한다. 원문은
MinIO의 immutable hash key로 보존하고 Redis/Dramatiq은 실행 전달만 담당한다.
Agent는 private versioned HTTP API로만 호출하며 Data는 Agent 뒤에 두어
Browser와 Backend가 Knowledge DB를 직접 조회하지 않는다.

Backend는 OIDC JWT의 signature, issuer, audience, expiry, organization, role을 모두
검증하는 Resource Server다. SPA는 Authorization Code + PKCE S256를 사용하고
access token을 memory에만 둔다. Keycloak protocol endpoint를 제외한 제품 데이터
통신은 `/api/v1`만 사용한다.

## 안전 불변식

- text 외 입력은 Agent 호출 전 422로 거절한다.
- 수정은 source document field만 제안할 수 있고 Evidence·Finding·Outcome을 수정할 수 없다.
- 수정 재실행은 parent result digest와 Case revision을 모두 확인한 child Run이다.
- auto-completion은 Backend kill switch, exact Agent/Data/profile/policy pin,
  최소 500 Case locked holdout, unsafe 자동완료 0, clean completion recall 0.95 이상,
  kill-switch 검증 artifact가 모두 하나의 approval subject에 결박되어야 한다.
- approval subject에는 Backend image에 고정된 서로 다른 domain reviewer key 2개의
  Ed25519 서명이 필요하다. runtime env는 reviewer key를 추가할 수 없다. 현재
  packaged trust store는 의도적으로 비어 있어 보호된 PR/release 전에는 자동완료가
  항상 차단된다.
- Agent는 직접 외부 bridge를 갖지 않고 `api.anthropic.com:443` CONNECT만 허용하는
  exact-image egress proxy를 통해 Claude를 호출한다.
- Supporting document의 원문·metadata 생성자는 자기 자료를 검증할 수 없다.
  다른 Approver가 검토하더라도 Data release에서 서버 측으로 확정한 policy identity와
  Agent active assertion trust store에 등록된 Ed25519 receipt가 없으면
  자동완료 근거로 사용하지 않는다. Browser가 policy source/release를 선택하지 못한다.
- Agent에 실제로 전송한 canonical bytes hash와 Agent가 safe default를 채운
  provider-parsed input manifest hash를 분리한다. 이로써 call-once delivery 증거와
  결과–입력 결박을 각각 정확한 대상에 적용한다.
- 이 결과는 세관의 통관 승인이 아니라 자동 사전심사 완료다.

## 영향

기존 `integration` 하네스와 `operational_db` v1 snapshot은 삭제하지 않는다.
제품 테이블과 API는 additive로 나뉘어 전환기 재현성을 보존한다.
