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
`3cff42f2da4c7debce0c51fea4e05f3eb62b48f6`, wheel SHA-256
`f81d3376962b3c9551aea59748af8b6f3352f4f3784bfef735bc5431589a63ea`다.

```powershell
uv sync --locked --group dev --extra unit --extra system --python 3.11
uv run --no-sync python tools/install_pinned_agent.py ..\_artifacts\packages\customs_ai_agent-0.1.0-py3-none-any.whl --verify-only
uv run --no-sync python tools/install_pinned_agent.py ..\_artifacts\packages\customs_ai_agent-0.1.0-py3-none-any.whl
uv run --no-sync pytest tests/system
uv sync --check --locked --inexact --group dev --extra unit --extra system
```

현재 개발 환경에 정확한 Agent 배포판과 lock된 system 의존성이 이미
설치되어 있다면 `pytest tests/system`부터 실행할 수 있다. 이 방식은 wheel
배포 계약이지 `sys.path`, editable 설치, 형제 상대경로 의존이 아니다.

실제 Evidence API와 Operational PostgreSQL까지 확인하는 release pilot은
Data readiness가 200인 상태에서 다음처럼 실행한다. Data corpus가 불완전하면
기본 모드는 실패하며, `--allow-incomplete-corpus`는 특정 Structured 경로만
진단할 때 사용하는 비-release 옵션이다.

```powershell
uv run --no-sync python packages/operational_db/scripts/migrate.py
uv run --no-sync python tools/run_live_pilot.py
```

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
