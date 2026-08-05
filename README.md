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

## 상세 dev-fixture 시나리오 transcript

기본 개발·CI·일반 로그에는 문서 본문이나 API body를 남기지 않는다. 합성 시나리오의
실제 입력·응답을 검토해야 할 때만, **Agent와 Backend를 각각 시작하기 전에** 두
터미널에서 아래를 설정한다. 이 값은 현재 PC의 Git 밖 `_local` 경로만 허용한다.

```powershell
$TranscriptRoot = Join-Path (Resolve-Path ..).Path '_local\state\scenario-runs'
New-Item -ItemType Directory -Force -Path $TranscriptRoot | Out-Null
$env:CUSTOMS_SCENARIO_TRANSCRIPT_ROOT = $TranscriptRoot
```

Data → Agent → Backend → Frontend를 시작한 뒤, Backend 터미널에서 다음을 실행한다.

```powershell
uv run --locked --no-sync --group dev python tools/run_dev_fixture_scenarios.py `
  --base-url http://127.0.0.1:5173/api/v1
```

이 실행기는 Frontend Vite proxy를 통해 다섯 현재 지원 시나리오를 실행하고, 각
`_local/state/scenario-runs/<review-run-id>/`에 아래를 남긴다.

- `scenario.json`: 합성 문서 원문·실제 review 요청
- `public-api.jsonl`: Frontend proxy → Backend API의 request/response body
- `backend-agent.jsonl`: Backend → Agent 실제 HTTP body (Backend만 기록)
- `agent-data.jsonl`: Agent → Data 실제 HTTP body (Agent만 기록)
- `claude.jsonl`: dev-fixture의 Claude 미호출 표시
- `summary.json`: 기대값·실제 판정·안전 assertion

세션/서비스 token, Authorization·Cookie 헤더, API key, 비밀번호, DSN 및
credential-shaped 값은 모두 `[REDACTED]` 처리한다. 이 transcript는 Git, `_artifacts`,
채팅, PR, 공유 ZIP에 넣지 않는다. 실제 Claude가 필요한 functional-UAT에서는 opt-in
환경변수 없이도 같은 고정 경로를 자동으로 사용한다. 별도의 실제 UAT 실행과 개인
권한은 여전히 필요하다.

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

### Functional-UAT 상세 transcript

`functional-uat`에서는 Backend와 Agent가 시작할 때 별도 opt-in 없이 고정된
`../_local/state/scenario-runs/`을 함께 사용한다. Case 생성, 문서 등록, 심사, Job/결과
조회가 끝나면 실제 review run마다 다음 여섯 파일이 자동으로 남는다.

```text
_local/state/scenario-runs/<review-run-id>/
  scenario.json
  public-api.jsonl        # Backend 소유
  backend-agent.jsonl     # Backend 소유
  agent-data.jsonl        # Agent 소유
  claude.jsonl            # Agent 소유
  summary.json
```

문서 본문, 공개 API body, Backend↔Agent·Agent↔Data body와 Claude prompt/완료 텍스트는
비밀을 제거한 뒤에만 이 PC의 `_local`에 기록한다. Authorization/Cookie/세션·서비스
token/API key/DSN/provider 원문 오류는 기록하지 않는다. 기록 경로·파일 생성·비밀
제거에 실패하면 해당 Functional-UAT는 불완전한 실패로 취급한다.

내용을 화면에 출력하지 않고 구조·종료 상태·redaction만 확인하려면 다음을 실행한다.

```powershell
uv run --no-sync python tools/verify_scenario_transcript.py --run-id <review-run-id>
```

상세 transcript는 Git, `_artifacts`, PR, 채팅, 공유 ZIP에 넣지 않는다. 현재 UAT의
간단한 실행 요약 manifest만 별도의 `_artifacts/functional-uat/`에 둘 수 있다.

## 검사

```powershell
uv run --no-sync python tools/check_backend.py quick
uv run --no-sync python tools/check_backend.py contract
```

`quick`은 lint·type·unit을, `contract`는 V1 Browser OpenAPI와 Agent V3 schema bytes를
검사한다. 실제 기능 확인은 CI가 아니라 팀원의 로컬 입력으로만 실행한다.
