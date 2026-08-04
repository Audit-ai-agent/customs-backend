# Backend Architecture

## 의도

저장소는 기존 통합 검증 경로를 보존하면서 `customs_backend`을
제품용 FastAPI modular monolith 경계로 추가한다. 전환기 `integration`은
지우거나 제품 API로 위장하지 않는다.

```text
legacy/historical local CLI/UI (unauthenticated, loopback-only)
  -> customs-ai-agent==0.1.0 public package API
  -> Evidence API v1 over localhost HTTP
  -> immutable ReviewRunSnapshot
  -> OperationalRepository
       -> in-memory (unit/system tests)
       -> PostgreSQL operations schema (live verification)

Browser (OIDC Authorization Code + PKCE)
  -> /api/v1 FastAPI Resource Server
       -> PostgreSQL Case/Job/Run/HITL/Chat/Audit/Outbox
       -> MinIO immutable text source
       -> Redis/Dramatiq worker
            -> private Agent HTTP API
                 -> private Evidence API
```

위 0.1.0 경로는 분리 전 파일럿을 재현하는 보존 자산이며 현재 제품 pin이
아니다. 제품 Backend candidate는 exact commit/wheel/source/code identity로 고정된
`customs-ai-agent==0.3.0`의 private Agent HTTP V2와 V3 review contract만 소비한다.

배포 topology와 Docker 이미지는 이 저장소가 소유하지 않는다. Backend는 Agent·Data
store에 직접 접근하지 않고, Browser는 Backend API만 호출한다. 실제 Local UAT의
network, Compose, TLS, credential 주입은 Git 밖 승인자 운영 번들이 조합한다.
누락·unreleased·artifact hash drift이면 Data→Agent→Backend readiness가 순서대로
닫힌다.

## 불변식

1. Agent graph는 Operational DB를 직접 호출하지 않는다.
2. 조직 범위 `Idempotency-Key`는 동일 사용자 요청만 재사용할 수 있고, `review_run_id`는
   생성된 immutable Run identity다.
3. rerun은 `parent_run_id`를 가진 새 run으로만 저장한다.
4. Finding은 같은 item/run에 스냅샷된 evidence만 참조한다.
5. 저장 후 canonical hash readback이 다르면 성공으로 표시하지 않는다.
6. Data 계약·평가기는 Data 저장소에서만 변경한다.
7. 시스템 연동은 패키지 버전과 HTTP 계약을 사용하고 형제 소스
   상대경로를 사용하지 않는다.
8. Browser는 Agent·Data·DB를 직접 호출하지 않는다.
9. JWT는 서명·issuer·audience·exp·org·role을 모두 검증한다.
10. 수정은 기존 Run을 변경하지 않고 stale digest/revision을 확인한 child Run으로만 생성한다.
11. Backend auto-completion kill switch는 기본 OFF이며 승인 profile과 2인 검수 digest 없이 켤 수 없다.
12. Review enqueue transaction이 Case row를 잠근 상태에서 Case revision, 문서 hash,
    supporting document assertion을 함께 읽고 Job·Event·Outbox를 저장한다. `review_as_of`
    생략값도 이때 날짜로 확정하며 전체 snapshot canonical digest를 저장한다.
13. 같은 Job은 외부 Agent 호출 직전 endpoint·request hash·`started`를 PostgreSQL CAS로
    기록한다. CAS 이후 timeout·connection 종료·process crash처럼 응답 도착 여부가
    불명하면 `delivery_uncertain`으로 격리한다. HTTP 응답을 받은 뒤 Schema·release·
    immutable input 결박이 실패하면 재전송 대상이 아닌
    `response_received_rejected`로 종료한다. 예외적으로 Agent가 submit 전 admission
    Gate에서만 발급하는 `503 dependency_unready`의 canonical body, 단일
    `X-Request-ID`, 단일 `X-Agent-Execution-State: not-started`가 모두 정확히
    일치하면 PostgreSQL CAS로 call state를 `not_started`로 되돌리고 60초 뒤
    재전달한다. 이 증명이 없는 503은 재시도하지 않으며 총 Agent 전달은 최대 3회다.
14. Agent 원본 결과와 model trace는 private 저장소에만 두고 public Run에는 명시적
    allowlist 결과와 Backend가 재평가한 disposition만 노출한다.
15. 수정은 Maker proposal → 자신과 다른 Approver 결정 → immutable child Run 순서만
    허용한다. Admin도 자기 제안을 승인할 수 없고, row lock/CAS와 DB unique scope가
    동시 approve/reject 중 하나만 저장한다. 일반 Run 결정도 같은 원자성 기준을 따른다.
16. Agent V3 request/result는 provider 원본 Schema bytes의 SHA-256·길이를 검증한 뒤
    Draft 2020-12 Schema와 Backend 독립 의미 불변식을 모두 통과해야 사용한다.
17. Backend release pin은 Agent build/wheel/source snapshot, model revision, 네 prompt,
    세 Agentic schema, policy/tool registry, release profile/trust store 및 kill switch를
    전체 필드 exact 비교한다.
18. Data는 코드 계약(provider release/revision/build/source manifest/Schema)과
    승인된 V2 generation runtime, V3 requirement-policy runtime을 분리해 표시한다.
    Backend는 두 runtime identity의 canonical hash를 직접 재계산하고, 서로
    nested identity가 다르거나 코드 계약 pin과 다르면 readiness를 실패시킨다.
19. 코드·계약용 `AgentServicePin`과 자동완료용 `AgentReleasePin`을
    분리한다. 미승인 candidate는 가상 release hash 없이 기동하지만,
    Data V2/V3·profile이 `unreleased`인 한 전체 readiness 503과 ingress 차단을 유지한다.
20. Agent 응답 중 Case revision이 바뀌면 최종 Run 저장 transaction이 Case row 아래에서
    `auto_review_complete`를 `human_review_required`로 강등하고 재심사 action을 남긴다.
21. 공개 ingress와 FastAPI는 동일한 4 MiB 요청 본문 상한을 강제한다. 개별 text는
    200,000자, immutable Review snapshot은 6개 primary와 합계 500,000자를
    최대로 한다. SUPPORTING 원문은 hash를 재검증하지만 분류 대상에서
    제외한다. 직렬화된 내부 Agent 요청 2 MiB 상한을 포함해 call-once
    barrier를 닫기 전에 계약을 검증한다.
22. Agent call-once에 저장하는 request hash는 실제 전송 bytes의 canonical
    hash다. Agent `input_manifest_sha256`는 pinned V3 model이 text-only 문서에
    `image_pages=[]`, `pdf_pages=null`을 채운 provider-parsed canonical shape의
    hash다. 둘을 섞으면 동일 입력을 오인하거나 실제 Agent 응답을 거부하므로
    각각 독립적으로 검증한다.
23. Agent 결과의 runtime identity는 package/code뿐 아니라 `agent_git_sha`와
    `agent_source_snapshot_sha256`도 ServicePin의 build/source와 exact 일치해야
    한다. 중간 metadata만 일치하고 개별 Run identity가 다른 경우 결과를 폐기한다.
24. PostgreSQL object owner인 `customs_migrator`는 forward-only migration에서만
    사용하고 제품 API·worker·outbox는 최소 권한 `customs_runtime`만 사용한다.
    Run(내부 provider 결과 포함), HumanDecision, Audit, Stage Event는 runtime의
    UPDATE/DELETE 권한을 제거하고 owner에게도 append-only trigger를 적용한다.
25. 같은 `(org, Case, revision, logical request)`의 active/uncertain Agent 호출은
    하나만 존재한다. 동일 actor의 중복은 기존 Job을 재사용하고 다른 actor는
    충돌로 거절한다. `delivery_uncertain`은 Approver/Admin의
    `confirmed_not_processed` 증거 기반 재조정 전까지 새 Agent 호출을 차단한다.
26. Human supporting-document assertion은 Data의 승인 policy 원본, reviewer trust
    store, Evidence Runtime V3를 Backend가 독립 검증해 얻은 policy identity에만
    서명할 수 있다. worker에 signing key가 없거나 세 release artifact 중 하나라도
    불일치하면 unsigned 상태로 보존하고 자동완료를 허용하지 않는다.
27. Agent 결과의 `review_as_of`, `trade_type`, `knowledge_snapshot_id`는 Job input,
    Agent runtime identity, 승인 release pin에 각각 exact 결박한 후에만 저장한다.
28. Proposal decision과 Case Chat은 actor·Case·endpoint 범위 Idempotency-Key와
    canonical request digest를 영속 저장한다. 응답 유실 후 같은 요청은 기존
    decision/message/child Job을 반환하고 다른 payload 재사용은 거절한다.

## 이행 경계

`integration`은 계속 로컬 검증 도구로만 남는다. 제품은
`customs_backend.api`와 versioned `/api/v1`을 사용한다. 현재 Release는
text-only Production candidate이며 OCR/PDF/Image는 422로 명시적 거절한다.
