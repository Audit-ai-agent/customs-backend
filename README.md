# Customs Backend

Backend가 소유하는 Operational DB 패키지와, 현재 AI Agent → Evidence Service →
Operational DB 경로를 재현하는 로컬 통합 검증 자산을 보존한 저장소다.

> **중요:** `integration.agent_webapp`은 무인증 localhost 전용 엔지니어링
> 도구이며 제품 Frontend/Backend API가 아니다. 외부 네트워크에 노출하지
> 마십시오.

## 소유 경계

- `packages/operational_db`: Case/Review Run 결과와 evidence를 불변 스냅샷으로
  저장하는 Backend 소유 패키지
- `src/integration`: 기존 통합 검증을 위한 전환기 네임스페이스
- `tools/local-agent-demo`: localhost 전용 UI 실행 안내
- `tests/system`: Agent·Evidence·Operational DB 조합 행동 검증

Data 플랫폼의 Evidence API JSON Schema, Gold set, RAG 평가기는 이 저장소에
복제하지 않는다. Data 제공자가 canonical 계약과 평가를 소유하고,
Backend 시스템 테스트는 지정된 API 버전을 소비한다.

## Python 3.11 설치와 단위 검증

```powershell
uv sync --locked --group dev --extra unit --python 3.11
uv run --locked ruff check .
uv run --locked mypy
uv run --locked --extra unit pytest packages/operational_db/tests tests/unit
```

Operational DB migration:

```powershell
uv run python packages/operational_db/scripts/migrate.py --dsn $env:POSTGRES_DSN
```

## 시스템 검증

AI Agent는 형제 폴더가 아닌 exact-commit wheel로 설치한다. 이 저장소의
고정 계약은 `customs-ai-agent==0.1.0`이며, 다른 버전은 실행 전에
거부된다. 현재 기준 artifact는 Agent commit
`1bd9f6d493d4eb60a0d602e5ba0230f704e94e2a`, wheel SHA-256
`88a485cde9e1b26a85a4856b7af6aa89b74620187ff59fb4e3b9c1fc360cdf0a`다.

> **호환성 경고:** 이 pin과 아래 pilot은 분리 전 통합 경로를 재현하기 위한
> 역사적·전환기 자산이다. Agent 0.1.0은 현재 Data Platform
> `evidence-api-v1.1.0`이 요구하는 네 개의 provider build-binding header를
> 보내지 않으므로 해당 API와 호환되지 않으며 retrieval 전에 HTTP 412로
> fail-closed하는 것이 정상이다. 이 도구를 현재 Agent–Data 통합 완료 증거나
> 제품 Backend로 사용하지 않는다. 현재 계약의 통합은 Data 최종 병합 SHA에
> Agent 0.3.0 candidate를 다시 고정한 뒤 별도 cross-repository Gate로 검증한다.

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

역사적 Data v1.0 Evidence API와 Operational PostgreSQL 경계를 확인하는
전환기 pilot은 호환되는 legacy Data readiness가 200인 상태에서 다음처럼
실행한다. 현재 Data v1.1에는 사용하지 않는다. Data corpus가 불완전하면
기본 모드는 실패하며, `--allow-incomplete-corpus`는 특정 Structured 경로만
진단할 때 사용하는 비-release 옵션이다.

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

- 제품용 인증·RBAC·tenant 격리 Backend
- 실제 파일 업로드·OCR/VLM·Job queue·HITL API
- Data 플랫폼 canonical 계약, Gold set, RAG evaluator
- 운영 배포 구성
