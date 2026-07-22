# Customs Backend

Backend가 소유하는 Operational DB 패키지와, 현재 AI Agent → Evidence Service →
Operational DB 경로를 재현하는 로컬 통합 검증 자산을 보존한 저장소다.

`customs_backend` 패키지는 이 자산을 건드리지 않고 추가한 내부
심사자용 text-only Production-candidate Backend다. OIDC/RBAC, Case/Document,
멱등 Job, immutable Review Run, 승인된 CorrectionProposal 기반 child revision,
SupportingDocument 검증, HITL, grounded Chat, Audit/Outbox, 재연결 가능한 SSE 진행
계약을 `/api/v1`로 제공한다. Job의 immutable 입력과 Agent 원본 응답은 서버 측
private persistence에만 보관하고 Browser 응답에는 allowlist된 view만 제공한다.

> **중요:** `integration.agent_webapp`은 무인증 localhost 전용 엔지니어링
> 도구이며 제품 Frontend/Backend API가 아니다. 외부 네트워크에 노출하지
> 마십시오.

## 소유 경계

- `packages/operational_db`: Case/Review Run 결과와 evidence를 불변 스냅샷으로
  저장하는 Backend 소유 패키지
- `src/integration`: 기존 통합 검증을 위한 전환기 네임스페이스
- `tools/local-agent-demo`: localhost 전용 UI 실행 안내
- `tests/system`: Agent·Evidence·Operational DB 조합 행동 검증
- `src/customs_backend`: 인증된 제품 API·Application Service·Port/Adapter
- `compose.production.yaml`: exact image pin을 요구하는 내부 Docker 배포 후보

제품 경계는 Agent V3 request/result Schema hash와 runtime release identity가
Backend consumer lock 및 배포 승인 manifest와 정확히 일치해야 ready가 된다.
자동완료는 기본 OFF이며 승인된 exact release, 검증된 supporting document,
완전한 Evidence, 열린 Gap/Issue/Finding 0건을 모두 충족해도 2인 도메인 승인
manifest가 없으면 `human_review_required`로 강등한다.
검수 승인은 Backend image에 고정된 서로 다른 Ed25519 reviewer key 2개로 서명해야
하며, 현재 packaged trust store는 의도적으로 비어 있어 자동완료는 활성화할 수 없다.
향후 키 등록은 runtime env 변경이 아니라 보호된 PR과 새 exact Backend image로 한다.

Production candidate는 코드·계약용 `AgentServicePin`과 자동완료용
`AgentReleasePin`을 분리한다. 전자는 build/wheel/source, V3/OpenAPI,
prompt/policy/tool, Data provider code build를 exact 고정하고 현재 release
상태가 정직하게 `unreleased`인지 검증한다. 후자는 승인된 model
revision, Data V2/V3 runtime, profile/trust를 추가로 결박한다. 따라서
현재 candidate는 가상 approved 해시 없이 프로세스와 Case/HITL
control-plane을 구성할 수 있지만, 실행 전체 `/health/ready`는 503이며
reverse-proxy는 리뷰 트래픽을 열지 않는다.

Data 플랫폼의 Evidence API JSON Schema, Gold set, RAG 평가기는 이 저장소에
복제하지 않는다. Data 제공자가 canonical 계약과 평가를 소유하고,
Backend 시스템 테스트는 지정된 API 버전을 소비한다.

## Python 3.11 설치와 단위 검증

```powershell
uv sync --locked --group dev --extra unit --python 3.11
uv run --locked ruff format --check .
uv run --locked ruff check .
uv run --locked mypy
uv run --locked --extra unit pytest packages/operational_db/tests tests/unit tests/product
```

GitHub Gate는 exact action SHA와 uv `0.11.28`을 사용한다. ephemeral PostgreSQL에서
migration `003 → 004 → 005 → 006 → 007`, runtime/migrator 권한·append-only 변조 방지,
JUnit skip 0, OpenAPI byte drift, Docker build, Gitleaks/Bandit, hash가 포함된
`uv.lock` 전체 dependency audit를 검사한다. Cross-repository Gate는 조직 변수의
exact Agent commit/wheel SHA를 사용한다. PR에는 cross-repository credential을
무조건 전달하지 않는다. 보호된 `trusted-cross-repository` Environment의 사람
승인을 받은 PR 또는 `main` push에서만 repository-scoped GitHub App 단기 token을
발급하며, 승인 전에는 Job이 대기/실패해 exact wheel의 metadata/review/revision
HTTP 검증을 우회할 수 없다.

OpenAPI v1을 재생성하려면:

```powershell
uv run --locked python tools/export_product_openapi.py
```

제품 설정·migration·Docker 운영은
[`docs/PRODUCT_BACKEND_RUNBOOK.ko.md`](docs/PRODUCT_BACKEND_RUNBOOK.ko.md)를 따른다.
Production 안전장치를 유지한 채 사용자 관점의 로컬 실행을 조합할 때는
[`docs/LOCAL_UAT_BACKEND.ko.md`](docs/LOCAL_UAT_BACKEND.ko.md)의 명시적
`local_uat` 경계와 `deploy/uat/*.env.example`을 사용한다. 네 저장소 Compose와
실제 secret은 이 저장소가 아니라 개인 `_engineering` harness가 소유한다.

Operational DB migration:

```powershell
uv run python packages/operational_db/scripts/migrate.py --dsn $env:POSTGRES_DSN
```

## 시스템 검증

AI Agent는 형제 폴더가 아닌 exact-commit wheel로 설치한다. 이 저장소의
고정 계약을 소비한다. 현재 제품 Backend candidate의 exact pin은 다음과 같다.

- package: `customs-ai-agent==0.3.0`
- Agent commit: `a021b633b7ba3f4531b1b4facc6f2b0860df3293`
- wheel SHA-256: `086a21c351c493ca9a4892a0cd2b43abdec5642ac247876e5d66537d68af736f`
- source snapshot SHA-256: `fd4bda9473d29874ab4e7cf95009d1aeca721ca532bc49c68538ce85fbd16f66`
- code snapshot SHA-256: `689180deb579488e821b18bd11f86d3342bf82f786616dfefdd0a2b8b95ca0b8`

Backend는 위 identity와 packaged V3 Schema·OpenAPI·prompt·policy·tool registry
identity가 모두 일치할 때만 Agent 응답을 소비한다. 현재 assertion
trust store는 의도적으로 `unreleased`이므로 이 pin은 Production candidate의
코드·계약 경계를 입증하지만 자동완료를 활성화하지 않는다.

현재 비밀정보가 아닌 Data provider 코드 계약 pin은 clean commit
`14cde864425eee615a2e3f6799f6f2719448aab9`, provider
`evidence-api-v1.2.0`, build
`d7d64cf3df464cfc934ca0f347c1b1c461916fe12671eeeaf59c1445872486d3`다.
이는 실제 Knowledge generation·평가·요건정책이 승인된 Data Runtime
Release와 다르다. 후자가 아직 `unreleased`이면 Backend는 자동완료를
시작하지 않는다.

```powershell
uv sync --locked --group dev --extra unit --extra system --python 3.11
$AgentWheel = $env:CUSTOMS_AI_AGENT_WHEEL
if ([string]::IsNullOrWhiteSpace($AgentWheel)) {
    throw "CUSTOMS_AI_AGENT_WHEEL must point to the pinned Agent wheel"
}
uv run --no-sync python tools/install_pinned_agent.py "$AgentWheel" --verify-only
uv run --no-sync python tools/install_pinned_agent.py "$AgentWheel"
uv run --no-sync pytest tests/system
uv sync --check --locked --inexact --group dev --extra unit --extra system
```

현재 개발 환경에 정확한 Agent 배포판과 lock된 system 의존성이 이미
설치되어 있다면 `pytest tests/system`부터 실행할 수 있다. 이 방식은 wheel
배포 계약이지 `sys.path`, editable 설치, 형제 상대경로 의존이 아니다.

아래는 **legacy/historical pilot 전용** 절차다. `src/integration` 경로와
`src/integration/agent_contract.json`이 고정한 Agent 0.1.0, 역사적 Data v1.0
Evidence API, Operational PostgreSQL 경계를 재현한다. 호환되는 legacy Data
readiness가 200인 상태에서만 다음처럼
실행한다. 현재 Data v1.1에는 사용하지 않는다. Data corpus가 불완전하면
기본 모드는 실패하며, `--allow-incomplete-corpus`는 특정 Structured 경로만
진단할 때 사용하는 비-release 옵션이다.

> **호환성 경고:** Agent 0.1.0은 현재 Data Platform
> `evidence-api-v1.2.0`이 요구하는 provider/generation build-binding header를
> 보내지 않으므로 retrieval 전 HTTP 412로 fail-closed하는 것이 정상이다.
> 이 pilot을 현재 Agent–Data 통합 완료 증거나 제품 Backend로 사용하지 않는다.

이 pilot은 실제 LLM 도입 전 결정론적 경계만 검증하므로 `llm_mode=mock`을
`ReviewRunRequest`에 명시한다. `llm` 요청은 concrete provider가 구현될 때까지
Agent에서 fail-fast하며 mock으로 fallback하지 않는다. 기본 Evidence URL은 실제
Data API와 같은 `http://127.0.0.1:8010`이다.

Data compose의 PostgreSQL host port가 다른 프로젝트와 충돌하면 그 프로젝트를
중단하지 말고 같은 PowerShell 세션에서 포트를 격리한다. Data compose와 Backend
pilot이 동일한 값을 사용해야 한다. 로컬 기본값 `55432`는 다른
프로젝트의 일반적인 PostgreSQL `5432`와 충돌하지 않도록 고정한 값이다.

```powershell
$env:EVIDENCE_SERVICE_BASE_URL = "http://127.0.0.1:8010"
$env:POSTGRES_PORT = "55432"            # Data compose/config가 읽는 host port
$env:CUSTOMS_OPS_HOST_PORT = "55432"   # Backend가 local DSN 생성에 사용

$OpsDsn = "postgresql://trade:change_me@127.0.0.1:55432/trade_doc"
uv run --no-sync python packages/operational_db/scripts/migrate.py --dsn $OpsDsn
uv run --no-sync python tools/run_live_pilot.py --ops-host-port 55432
```

전체 DSN을 별도로 관리해야 하면 `CUSTOMS_OPS_DSN`이 생성 DSN보다 우선한다.
실제 비밀번호가 든 DSN은 명령행 인자나 문서에 쓰지 말고 로컬 secret 주입으로
환경변수에 설정한다. `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`는 DSN을
자동 생성할 때 사용한다.

Data가 로컬 `DEMO_TOKEN` 보호를 켠 경우 `EVIDENCE_SERVICE_TOKEN`을 환경변수로만
주입한다. token은 URL, CLI 인자, 실행 report, Operational snapshot에 포함되지 않고
`X-Demo-Token` 요청 header로만 전달된다.

```powershell
$env:EVIDENCE_SERVICE_TOKEN = (Get-Content C:\path\to\local-secret.txt -Raw).Trim()
uv run --no-sync python tools/run_live_pilot.py --ops-host-port 55432
```

`--review-as-of YYYY-MM-DD` 또는 `CUSTOMS_REVIEW_AS_OF`를 지정하면 그 날짜를
그대로 Agent 요청에 전달한다. 둘 다 없으면 release pilot은 Data readiness의
`supported_review_as_of`를 사용한다. readiness가 날짜를 제공하지 않는 불완전 corpus
진단에서는 날짜를 명시하지 않으면 실패한다.

pilot 성공은 다음을 모두 요구한다.

- Evidence readiness가 release 기준을 충족하거나 진단 모드가 명시돼 있을 것
- Agent가 실제 Evidence HTTP API를 호출할 것
- Operational DB 저장 후 canonical hash readback이 일치할 것
- 같은 `review_run_id`의 두 번째 실행이 저장 결과를 재사용할 것
- incomplete/failed retrieval을 자동 완료하지 않을 것

CLI 및 로컬 UI:

```powershell
uv run --no-sync customs-review --scenario happy_path --no-store --review-as-of 2026-07-10
uv run --no-sync customs-agent-demo
```

UI는 `http://127.0.0.1:8020`에서만 열린다. Evidence Service URL도
`localhost`, `127.0.0.0/8`, `::1`만 허용하며 외부 URL은 거부한다.

## 현재 비포함 범위

- OCR/PDF/Image 입력 및 OCR/VLM Release
- Data 플랫폼 canonical 계약, Gold set, RAG evaluator
- 통관 도메인 검수 2인의 Production promote 승인
