# Product Backend 운영 Runbook

## Release 의미

이 구성은 text-only `Production candidate`다. Domain reviewer 2인, 승인된
Claude extraction profile, Released Data generation이 없으면 auto-completion은 계속
OFF이다. OCR/PDF/Image를 받지 않는다. 코드·계약 pin만 있고
Data V2/V3·Claude profile이 `unreleased`인 후보는 진실한 값으로 프로세스를
기동할 수 있지만 전체 `/health/ready`는 503을 유지한다. 이를 리뷰 실행
가능 또는 Production-ready로 해석하지 않는다. Compose에서 Backend
프로세스는 시작하지만 reverse-proxy와 worker는 실행 release readiness가
통과하기 전까지 사용자 트래픽과 Review Job 처리를 열지 않는다. outbox
dispatcher는 DB·Redis만 받는 최소 권한 프로세스이며 worker readiness와 분리된다.

## 사전 조건

1. `.env.example`을 참고해 추적되지 않는 production env를 준비한다.
2. Frontend·Backend·Agent·Data와 모든 기반 image는 tag가 아닌 exact digest로 고정한다.
3. TLS cert/key는 Git 밖의 읽기 전용 디렉터리로 mount한다.
4. Keycloak 초기 관리자로 실제 사용자를 생성하고 `org_id`와 최소 role만 부여한다.
5. `CUSTOMS_PUBLIC_HOST`를 실제 내부 hostname으로 설정한다. Realm import의
   redirect URI는 `CUSTOMS_PUBLIC_ORIGIN` 환경값으로 치환된다. Keycloak public path는
   `/oidc/`, SPA callback은 `/auth/callback`으로 분리한다. reverse proxy에 `/auth/`
   Keycloak location을 추가하면 SPA callback을 가로채므로 금지한다.
6. Backend consumer lock과 일치하는 Agent metadata, 그리고 Git 밖의 release
   manifest 디렉터리를 준비한다. 자동완료를 켤 때만 2인 승인 manifest를 둔다.
   Data provider 코드 계약은 `.env.example`의 clean commit `14cde864...`,
   release `evidence-api-v1.2.0`, build `d7d64cf3...`와 exact 일치해야 한다.
   코드 build pin은 승인된 generation/runtime release를 대체하지 않는다.
   후자가 없는 candidate는 `.env.example`의 release-only 9개 값을 전부
   설정하지 않는다. 일부만 설정하거나 가상 approved 해시를 넣지 않는다.
7. Domain reviewer public key는 env나 release manifest에 넣지 않는다. 보호된 PR로
   `domain_reviewer_trust_store.json`을 변경하고 새 Backend image를 발행해야 한다.
   현재 trust store는 비어 있으므로 자동완료 ON은 의도적으로 시작 단계에서 실패한다.
8. `EVIDENCE_REQUIREMENT_POLICY_RELEASE_FILE`과 `EVIDENCE_TRUSTED_REVIEWERS_FILE`은
   Data image lock과 일치하는 승인 원본만 read-only mount한다. 저장소의 `unreleased`
   sentinel이나 임의 작성 JSON으로 대체하지 않는다.
9. `CUSTOMS_EVIDENCE_TLS_DIR` 인증서 SAN에는 `evidence-proxy`가 있어야 한다.
   `CUSTOMS_AGENT_CA_BUNDLE_FILE`은 이 내부 CA와 Claude 검증용 공인 root를 함께
   포함하고 private key는 포함하지 않는다.
10. Production Compose는 로컬 평문 PostgreSQL/Qdrant/Neo4j를 포함하지 않는다.
    `CUSTOMS_DATA_STORE_TLS_NETWORK`에 운영자가 사전 배치한 TLS endpoint만 연결한다.
    Data env는 PostgreSQL TLS, Qdrant HTTPS/API key, Neo4j `bolt+s`/`neo4j+s`를
    만족해야 하고, read-only 권한 검증 증거 hash를 배포 input에 고정한다.
11. Backend runtime env의 DSN 사용자는 `customs_runtime`, migration 전용 env의 DSN
    사용자는 `customs_migrator`여야 한다. runtime 프로세스에 migration env를 주거나
    `backend-migrate`에 runtime env를 재사용하면 Settings 또는 DB 권한 Gate에서
    실패하는 것이 정상이다. `customs_runtime`에는 Run·Decision·Audit·Stage Event의
    UPDATE/DELETE 권한을 주지 않는다. migration env에는 `ENVIRONMENT=production`,
    `PROCESS_ROLE=migration`, migrator DSN만 두고 OIDC·Agent·MinIO·Redis secret을
    복제하지 않는다.
    outbox env에는 `PROCESS_ROLE=outbox`, runtime DSN, Redis URL만 두고 OIDC·
    Agent·MinIO·release secret을 넣지 않는다.
12. 자동완료 release를 구성할 때는 Data가 발행한 requirement policy, reviewer trust
    store, Evidence Runtime V3 원본 3개를 `/run/customs-releases`에 read-only로 둔다.
    Backend는 policy/approval/trust/runtime canonical hash와 독립 Ed25519 서명 2개를
    재검증한다. 하나라도 다르면 assertion은 신뢰 가능한 영수증이 되지 않으며
    자동완료 Gate가 닫힌다.

## 기동과 검증

```powershell
$env:CUSTOMS_BACKEND_ENV_FILE = "C:\secrets\customs-backend.env"
$env:CUSTOMS_BACKEND_OUTBOX_ENV_FILE = "C:\secrets\customs-outbox.env"
$env:AGENT_ENV_FILE = "C:\secrets\customs-agent.env"
$env:DATA_ENV_FILE = "C:\secrets\customs-data.env"
$env:CUSTOMS_TLS_DIR = "C:\secrets\tls"
$env:CUSTOMS_EVIDENCE_TLS_DIR = "C:\secrets\evidence-tls"
$env:CUSTOMS_AGENT_CA_BUNDLE_FILE = "C:\secrets\ca-bundle.pem"
$env:EVIDENCE_REQUIREMENT_POLICY_RELEASE_FILE = "C:\releases\requirement-policy.json"
$env:EVIDENCE_TRUSTED_REVIEWERS_FILE = "C:\releases\trusted-reviewers.json"
$env:CUSTOMS_RELEASE_DIR = "C:\secrets\customs-releases"
$env:CUSTOMS_PUBLIC_HOST = "customs.internal.example"
$env:CUSTOMS_DATA_STORE_TLS_NETWORK = "customs-data-store-tls"
$env:CUSTOMS_DATA_STORE_ACCESS_MODE = "read_only"
$env:CUSTOMS_DATA_STORE_PERMISSION_ATTESTATION_SHA256 = "<reviewed-sha256>"
uv run --locked python tools/verify_production_deployment_inputs.py `
  --compose-file compose.production.yaml --data-env-file $env:DATA_ENV_FILE
docker compose -f compose.production.yaml up -d
```

확인 순서:

1. `operations-db-role-reconcile` 로 기존 volume을 포함해 runtime 암호가 회전되고,
   이후 `backend-migrate` exit 0
2. `/health/live` 200
3. candidate에서 Agent/Data provider 코드 pin은 exact 일치하고 Data V2/V3와
   Claude profile은 exact `unreleased`임을 확인. 이 단계의 `/health/ready` 503은 정상
4. 승인된 release 적용 후에만 `/health/ready` 200과 exact Agent/Data
   provider·V2 runtime·V3 policy release metadata
5. OIDC login 후 `/api/v1/session`의 org/role
6. text Case 생성→Document/SupportingDocument→Review Job→SSE→Run
7. 중복 Idempotency-Key가 같은 Job을 반환
8. 재시작 후 Run·Audit·Chat이 존재
9. CorrectionProposal을 Reviewer가 만들고 Approver가 승인해야만 child Run 생성
10. Agent/Data identity drift 또는 DB/MinIO/Redis 장애 시 readiness 실패
11. Browser 응답의 Job에 immutable request가, Run에 raw Agent/model trace가 없는지 확인
12. Agent에서 `api.anthropic.com:443` 외 CONNECT와 direct external egress가 거절되는지 확인
13. host에서는 reverse proxy의 8443만 접근되고 Backend·Frontend·Keycloak이 `edge`를
    일반 인터넷 egress로 사용할 수 없는지 확인
14. Data PID가 `src.production_v3`인지, Agent 내부 port가 8080인지, Agent→Evidence가
    `https://evidence-proxy:8443` 이외 경로로 우회되지 않는지 확인
15. Supporting document 생성자의 self-assert는 409, 독립 Approver의 assertion은
    승인된 Data policy resolver·Agent trust store·Backend signing key가 모두 결박될 때만
    signed receipt를 가지는지 확인. 세 요소 중 하나라도 없으면 unsigned로
    남고 auto-completion은 차단되어야 한다.
16. Agent Run identity의 git/source/code hash와 Backend ServicePin을 모두 대조하고,
    실제 wire hash와 provider-parsed input manifest hash가 각각 재현되는지 확인
17. 같은 Case revision의 같은 logical review를 다른 Idempotency-Key로 재요청해도
    실행 중 Job 하나만 재사용되는지, 다른 actor는 409인지 확인
18. Agent 응답 유실로 `delivery_uncertain`이 된 Job은 새 요청을 막는지 확인한다.
    Approver/Admin이 Agent 감사 증거 hash를 확인한 뒤
    `POST /api/v1/jobs/{job_id}/reconciliation`에 `confirmed_not_processed`를 기록해야만
    새 Job을 허용한다. Reviewer 단독 재조정은 403이어야 한다.
19. HTTP/JSON은 도착했지만 Agent 계약·release·input binding을 거절한 Job이
    `response_received_rejected`로 종료되고 reconciliation/retry 대상이 아닌지 확인한다.
20. reverse proxy access log에 query string이 없고 `/oidc/`, `/auth/callback`이
    access-log off인지 확인한다. TLS 1.2/1.3 외 협상은 거절한다.

입력 Release 경계는 reverse proxy와 Backend 모두에서 요청 본문 4 MiB로
고정한다. 개별 text 문서는 200,000자 이하이고, 하나의 Review snapshot은
최대 6개 primary 문서와 합계 500,000자만 허용한다. SUPPORTING 원문은
manifest에 보존하되 Agent 분류 대상에서 제외한다. Backend→Agent 직렬화
요청은 2 MiB를 초과하면 call-once barrier 전에 거절한다. 초과를 proxy
설정이나 DB 직접 수정으로
우회하지 않는다.

## 장애·Rollback

- Agent/Data partial, identity drift, 인증 오류 시 auto-completion을 즉시 OFF한다.
- Job은 PostgreSQL이 source of truth이며 Redis 손실을 완료로 표시하지 않는다.
- Object Store read처럼 Agent 호출 전 실패만 동일 고정 Run ID로 최대 3회 재시도한다.
  Agent `/reviews` 또는 `/revisions` 호출 직전 PostgreSQL call-once CAS가 닫힌 뒤에는
  timeout, 응답 유실, worker crash를 포함해 같은 Job을 절대 재호출하지
  않는다. 도착 여부가 불명한 Job만 `delivery_uncertain`으로 격리한다. 응답을
  받은 후 Schema/release/input 검증에 실패한 Job은
  `response_received_rejected`로 종료하고 재조정하지 않는다. 새 호출은 Approver/Admin이
  Agent 측 미처리를 입증하는 증거 hash로 명시적 재조정한 뒤에만 허용한다. 처리 여부가
  확인되지 않으면 재조정하지 않고 격리를 유지한다.
- Run, HumanDecision, Audit는 직접 삭제하지 않는다.
- migration owner도 append-only trigger를 우회하지 않는다. 변경이 필요하면 기존 행을
  고치지 말고 새 보정 이벤트/Run과 forward-only migration을 추가한다.
- DB·MinIO·Data generation backup을 같은 release manifest에 묶어 restore 후 hash를 대조한다.
- 코드 문제는 이전 exact image digest로 compose를 돌리고 forward-only 보정 migration을 작성한다.

## 비밀정보·감사

- JWT, Agent token, DSN, MinIO key를 URL·response·audit detail·log에 남기지 않는다.
- Browser는 Backend Bearer token만 memory에 보유하고 Agent/Data credential을 받지 않는다.
- 원문 조회는 Case org·role 검증 후 Backend가 MinIO hash를 대조해 반환한다.

## 아직 외부 검증이 필요한 Release Gate

- `CUSTOMS_TLS_DIR`, `CUSTOMS_EVIDENCE_TLS_DIR`, `CUSTOMS_AGENT_CA_BUNDLE_FILE`의 실제
  조직 인증서 발급·SAN·권한·rotation과 TLS-enabled PostgreSQL/Qdrant/Neo4j 조합은
  저장소가 만들거나 우회할 수 없는 외부 Release blocker다. self-signed 임시 인증서나
  평문 Store 연결로 이 Gate를 통과 처리하지 않는다.
- 실제 PostgreSQL/Redis/MinIO/Keycloak/Agent/Data image 조합의 staging E2E
- 선택한 exact Squid image의 read-only/tmpfs/cap-drop 기동과 허용·차단 CONNECT 실측
- DB·MinIO backup/restore 및 이전 image/Data generation rollback 훈련
- 침투 테스트, 부하 테스트, 99.5% 가용성 계측
- Claude release profile과 Data generation에 대한 독립 도메인 검수 2인 승인

이 항목이 통과하기 전 배포물의 명칭은 `Production candidate`이며 자동완료는 OFF다.
