# Contributing

## 변경 원칙

- Python 3.11과 `uv.lock`을 기준으로 사용한다.
- Backend가 소유하지 않는 Agent/Data 소스를 복제하지 않는다.
- 공개 계약 변경은 제공자 저장소에서 먼저 버전으로 발행한다.
- `integration` 변경은 localhost 전용·무인증이라는 경계를 약화하지 않는다.
- 비밀값·실제 거래 문서·PII·실행 결과를 commit하지 않는다.

## 필수 검증

```powershell
uv sync --locked --extra unit --python 3.11
uv run --locked --extra unit pytest packages/operational_db/tests
```

Agent 연동을 변경했다면 exact Agent wheel과 `system` extra를 설치한 후:

```powershell
uv run --no-sync pytest tests/system
```

PR에는 문제, 선택 기준, 변경 계약, 실행 명령과 결과, rollback
방법을 기록한다.
