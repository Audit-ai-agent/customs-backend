# Contributing

## 변경 원칙

- Python 3.11과 `uv.lock`을 기준으로 사용한다.
- Backend가 소유하지 않는 Agent/Data 소스를 복제하지 않는다.
- 공개 계약 변경은 제공자 저장소에서 먼저 버전으로 발행한다.
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

다른 profile이 필요하면 동일한 방식으로 경로 끝의 `dev`를 `runtime`으로
분리한다. 실제 설치와 재사용은 공유 Harness의 `setup`이
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

Agent 연동을 변경했다면 `contract` 검사를 실행한다. 이 검사는 독립 계약
산출물과 Agent HTTP 응답의 identity를 비교하며, 형제 소스·wheel·commit pin을
요구하지 않는다.

PR에는 문제, 선택 기준, 변경 계약, 실행 명령과 결과, rollback
방법을 기록한다.
