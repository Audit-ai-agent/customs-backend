# Contributing

## 변경 원칙

- Python 3.11과 `uv.lock`을 기준으로 사용한다.
- Backend가 소유하지 않는 Agent/Data 소스를 복제하지 않는다.
- 공개 계약 변경은 제공자 저장소에서 먼저 버전으로 발행한다.
- `integration` 변경은 localhost 전용·무인증이라는 경계를 약화하지 않는다.
- 비밀값·실제 거래 문서·PII·실행 결과를 commit하지 않는다.

## 로컬 환경

저장소 안에 `.venv`를 남기지 않도록 `uv.lock` hash로 구분한
외부 환경을 사용한다. `uv sync`나 `uv run --locked`를 직접 실행해
저장소에 기본 `.venv`를 만들지 않는다.

```powershell
$LockHash = (Get-FileHash uv.lock -Algorithm SHA256).Hash.Substring(0, 12).ToLower()
$env:UV_PROJECT_ENVIRONMENT = Join-Path $env:LOCALAPPDATA "CustomsUAT\envs\customs-backend\$LockHash-dev"
uv sync --locked --group dev --extra unit --python 3.11
```

다른 profile이 필요하면 동일한 방식으로 경로 끝의 `dev`를 `system`이나
`runtime`으로 분리한다. 실제 설치와 재사용은 공유 Harness의 `setup`이
소유하며, 검사는 이미 준비된 환경을 변경하지 않는다.

## 경량 검사

새 검사 명령은 이미 동기화된 환경을 변경하지 않는다.

```powershell
uv run --no-sync python tools/check_backend.py quick
uv run --no-sync python tools/check_backend.py contract
```

- `quick`: Ruff·mypy·Backend 핵심 unit 검사
- `contract`: Agent HTTP 경계·OpenAPI/Schema snapshot 대표 검사
- `candidate`: Chat 2턴·수정 제안·독립 승인·child Run 대표 흐름

`candidate`는 관련 기능을 완료 판정할 때만 한 번 실행한다.

```powershell
uv run --no-sync python tools/check_backend.py candidate
```

Agent 연동을 변경했다면 `system` extra를 lock대로 설치하고, exact Agent
wheel을 hash 검증 후 `--no-deps` 방식으로 설치한다. 테스트 후 lock 환경이
변하지 않았음도 확인한다.

```powershell
uv sync --locked --group dev --extra unit --extra system --python 3.11
$AgentWheel = $env:CUSTOMS_AI_AGENT_WHEEL
if ([string]::IsNullOrWhiteSpace($AgentWheel)) {
    throw "CUSTOMS_AI_AGENT_WHEEL must point to the pinned Agent wheel"
}
uv run --no-sync python tools/install_pinned_agent.py "$AgentWheel"
uv run --no-sync pytest tests/system
uv sync --check --locked --inexact --group dev --extra unit --extra system
```

위 system 검사는 Agent HTTP 경계 또는 pin을 변경했을 때만 사용하며
일반 PR의 필수 검사가 아니다.

PR에는 문제, 선택 기준, 변경 계약, 실행 명령과 결과, rollback
방법을 기록한다.
