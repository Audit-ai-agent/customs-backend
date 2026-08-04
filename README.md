# Customs Backend

Backend가 소유하는 Operational DB 패키지와 내부 심사자용 text-only Backend다.
OIDC/RBAC, Case/Document,
멱등 Job, immutable Review Run, 승인된 CorrectionProposal 기반 child revision,
SupportingDocument 검증, HITL, grounded Chat, Audit/Outbox, 재연결 가능한 SSE 진행
계약을 `/api/v1`로 제공한다. Job의 immutable 입력과 Agent 원본 응답은 서버 측
private persistence에만 보관하고 Browser 응답에는 allowlist된 view만 제공한다.

## 소유 경계

- `packages/operational_db`: Case/Review Run 결과와 evidence를 불변 스냅샷으로
  저장하는 Backend 소유 패키지
- `src/customs_backend`: 인증된 제품 API·Application Service·Port/Adapter

제품 경계는 Agent V3 request/result Schema hash와 runtime release identity가
Backend consumer lock의 계약 산출물 SHA와 정확히 일치할 때만 통과한다. lock의
`provider_contract_source_commit`은 계약 산출물을 만든 source provenance일 뿐,
실행 중인 Agent commit을 고정하는 gate가 아니다.
자동완료는 기본 OFF이며 승인된 exact release, 검증된 supporting document,
완전한 Evidence, 열린 Gap/Issue/Finding 0건을 모두 충족해도 2인 도메인 승인
manifest가 없으면 `human_review_required`로 강등한다.
검수 승인은 보호된 source revision에 고정된 서로 다른 Ed25519 reviewer key 2개로 서명해야
하며, 현재 packaged trust store는 의도적으로 비어 있어 자동완료는 활성화할 수 없다.
향후 키 등록은 runtime env 변경이 아니라 보호된 PR로만 한다.

Production candidate는 코드·계약용 `AgentServicePin`과 자동완료용
`AgentReleasePin`을 분리한다. 전자는 build/wheel/source, V3/OpenAPI,
prompt/policy/tool, Data provider code build를 exact 고정하고 현재 release
상태가 정직하게 `unreleased`인지 검증한다. 후자는 승인된 model
revision, Data V2/V3 runtime, profile/trust를 추가로 결박한다. 따라서
현재 candidate는 가상 approved 해시 없이 프로세스와 Case/HITL
control-plane을 구성할 수 있지만, 실행 전체 `/health/ready`는 503으로
닫힌다. 이 저장소는 배포 Compose·이미지·운영 환경파일을 제공하지 않는다.

Data 플랫폼의 Evidence API JSON Schema, Gold set, RAG 평가기는 이 저장소에
복제하지 않는다. Data 제공자가 canonical 계약과 평가를 소유하고,
Backend 시스템 테스트는 지정된 API 버전을 소비한다.

## Python 3.11 경량 검사

```powershell
$LockHash = (Get-FileHash uv.lock -Algorithm SHA256).Hash.Substring(0, 12).ToLower()
$env:UV_PROJECT_ENVIRONMENT = Join-Path $env:LOCALAPPDATA "CustomsUAT\envs\customs-backend\$LockHash-dev"
uv sync --locked --group dev --extra unit --python 3.11
uv run --no-sync python tools/check_backend.py quick
uv run --no-sync python tools/check_backend.py contract
```

GitHub PR은 `quick`과 `contract`만 실행한다. 의존성 audit는 같은 workflow에서
`uv.lock`이 변경된 경우에만 실행한다.
전체 migration·package·container·SBOM/Trivy·cross-repository Agent 인증은
일반 PR의 활성 Gate가 아니다. Chat·HITL·child Run을 변경한 경우에만
`uv run --no-sync python tools/check_backend.py candidate`로 대표 흐름을 한 번
확인한다.

OpenAPI v1을 재생성하려면:

```powershell
uv run --locked python tools/export_product_openapi.py
```

사용자 관점의 로컬 실행을 조합할 때는
[`docs/LOCAL_UAT_BACKEND.ko.md`](docs/LOCAL_UAT_BACKEND.ko.md)의 명시적
`local_uat` 경계와 `deploy/uat/*.env.example`을 사용한다. 실제 Compose,
비밀값, 데이터는 Git 밖의 승인자 전용 UAT 운영 번들이 소유한다.

Operational DB migration:

```powershell
uv run python packages/operational_db/scripts/migrate.py --dsn $env:POSTGRES_DSN
```

## Agent 계약 확인

Backend는 Agent 소스 checkout이나 commit이 아니라, Agent가 생성한 독립 계약
산출물의 SHA-256과 실행 응답의 capability/runtime identity를 검증한다. 일상
검사는 위 `contract` 명령으로 충분하며, 실제 권한자 Local UAT는 Git 밖 운영
번들이 형제 저장소와 별도 비밀 주입을 조합한다.

## 현재 비포함 범위

- OCR/PDF/Image 입력 및 OCR/VLM Release
- Data 플랫폼 canonical 계약, Gold set, RAG evaluator
- 통관 도메인 검수 2인의 Production promote 승인
