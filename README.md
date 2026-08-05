# Customs Backend

Frontend의 Case·문서·심사 화면을 위한 작은 HTTP API다. Browser는 Backend만 호출하며,
Backend는 private Agent HTTP만 호출한다. Agent와 Data, Browser와 DB 사이에 직접 연결은 없다.

## 실행 모드

| Mode | 용도 | Backend 상태 | 로그인 |
| --- | --- | --- | --- |
| `local-development` | 기본 팀 개발 | 메모리 | loopback 개발 session |
| `functional-uat` | 실제 기능 확인 | 로컬 PostgreSQL + MinIO | 로컬 Keycloak OIDC |

두 모드의 결과는 항상 `human_review_required`이며 자동 완료하지 않는다. 두 서비스는
Bearer token과 `X-Customs-Service-Mode`가 정확히 같은 경우에만 통신한다.
Backend는 저장소 `.env`를 읽지 않으며, 아래에서 명시적으로 import한 현재 터미널의
profile 값만 사용한다.

## 기본 개발

Python 3.11과 `uv`가 필요하다. 한 번 설치하고, 생성된 로컬 profile을 Backend와 Agent
프로세스가 함께 읽는다.

Windows 실행 정책이 자신의 새 clone의 `.ps1`만 차단할 때에만, 해당 터미널에서 아래를
한 번 실행한다. 시스템 정책이나 다른 터미널에는 영향을 주지 않는다.

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

```powershell
uv sync --locked --group dev --extra unit --python 3.11
uv run --no-sync customs-backend-profile-init --profile dev-fixture
. .\tools\Import-LocalProfile.ps1 -Profile dev-fixture -Scope backend
uv run --no-sync customs-backend-api
```

`backend.env`는 아래 두 Agent 연결 값을 같은 무작위 값으로 생성한다. loader의
`backend` 범위는 `CUSTOMS_BACKEND_*`만 주입한다. Agent는 자체
`Import-AgentProfile.ps1`로 아래 Agent 두 값과 Data Evidence token만 좁게 읽는다.
Agent 프로세스에는 DB·MinIO·Keycloak·로컬 로그인 값이 전달되지 않는다.

- `CUSTOMS_BACKEND_AGENT_SERVICE_TOKEN`: Backend가 보내는 Bearer token
- `CUSTOMS_AGENT_SERVICE_BEARER_TOKEN`: Agent가 검증하는 동일 token

또한 `CUSTOMS_AGENT_SERVICE_MODE=local-development`를 함께 생성한다.

```powershell
Push-Location ..\customs-ai-agent
uv sync --locked --group dev --python 3.11
. .\tools\Import-AgentProfile.ps1 -Profile dev-fixture
uv run --no-sync customs-ai-agent-dev-serve
```

Data 서비스는 이 Agent보다 먼저 해당 저장소의 `dev-fixture` 명령으로 시작한다. Agent
터미널에서는 `..\customs-ai-agent`로 이동한 뒤, 위의 Agent 명령을 실행한다. 이 명령은
값을 출력하지 않는다. Frontend는 loopback에서
`POST /api/v1/dev/session`을 한 번 호출해 받은 token을 메모리에만 보관한다.

## 실제 기능 확인

이 모드는 실제 Agent/Data 흐름을 호출한다. 법령 원문, Law API 권한, Claude key, 모델
cache와 Data manifest는 Data/Agent의 각자 `_local` 입력이다. Backend가 생성하는 것은
자기 로컬 PostgreSQL·MinIO·Keycloak 값뿐이다.

```powershell
uv sync --locked --group dev --extra unit --extra functional-uat --python 3.11
uv run --no-sync customs-backend-profile-init --profile functional-uat
. .\tools\Import-LocalProfile.ps1 -Profile functional-uat -Scope backend
$Profile = '..\_local\profiles\functional-uat\backend.env'
docker compose --project-name customs-functional-uat --env-file $Profile `
  -f deploy\functional-uat\compose.yaml up -d --wait
uv run --no-sync customs-backend-api
```

로컬 Keycloak 화면에서는 사용자 이름 `local-functional-reviewer`를 사용한다. 비밀번호는
화면에 출력하지 않고, 본인 PC에서만 아래 명시적 명령으로 클립보드에 복사한다.

```powershell
.\tools\Copy-FunctionalUatPassword.ps1
# local Keycloak form에 붙여넣은 뒤 즉시 비운다.
Set-Clipboard -Value ''
```

이 명령은 reviewer 비밀번호 하나만 읽으며 DB·MinIO·Keycloak 관리자·Agent·Data 값은
읽거나 출력하지 않는다. 비밀번호와 clipboard 내용은 Git·채팅·공유 파일에 넣지 않는다.

Agent의 실제 Data 연결은 같은 bearer/mode 값과 함께 Agent의 좁은 importer가 읽는다.

```powershell
Push-Location ..\customs-ai-agent
uv sync --locked --group dev --extra functional-uat --python 3.11
. .\tools\Import-AgentProfile.ps1 -Profile functional-uat-3law
uv run --no-sync customs-ai-agent-functional-uat-serve
```

Data의 `functional-uat-3law build --as-of 2026-08-05`와 serve를 먼저 완료하고, Agent
터미널에서 Git이 무시하는 `../_local/secrets/.env`에 개인
`ANTHROPIC_API_KEY=personal-key` 한 줄을 만든 뒤 `..\customs-ai-agent`에서 위 명령을
실행한다. Functional-UAT Agent importer만 그 파일을 현재 세션에 읽고 값을 출력하지
않는다. 해당 파일은 Git·Docker image·공유 ZIP·채팅에 넣지 않는다.
심사 기준일은 `2026-08-05`로 고정한다. `GET /health/functional-uat-ready`가
Backend의 로컬 의존성과 Agent HTTP 준비 상태를 확인한다. 실행을 끝내면 같은 compose
명령에 `down`을 붙여 로컬 컨테이너만 종료한다.

생성된 `_local/profiles/**/backend.env`와 `customs-realm.json`은 개인 로컬 설정이다.
Git, 채팅, 공유 압축 파일에 포함하지 않는다.
초기화기는 POSIX에서 owner-only (`0600`) 권한을 적용한다. Windows에서는 현재 사용자
workspace의 상속 ACL을 유지하므로, 해당 workspace를 다른 계정과 공유하지 않는다.

## 검사

```powershell
uv run --no-sync python tools/check_backend.py quick
uv run --no-sync python tools/check_backend.py contract
```

`quick`은 lint·type·unit을, `contract`는 V1 Browser OpenAPI와 Agent V3 schema bytes를
검사한다. 실제 기능 확인은 CI가 아니라 팀원의 로컬 입력으로만 실행한다.
