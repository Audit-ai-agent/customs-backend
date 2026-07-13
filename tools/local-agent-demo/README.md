# Local Agent Demo

기존 `agent_webapp.py`/`agent_webui.html`을 Backend 저장소에서 보존한 localhost 전용
통합 검증 도구다. 제품 Backend나 Frontend가 아니다.

1. Evidence Service를 `127.0.0.1:8010`에서 준비한다.
2. Operational PostgreSQL migration을 적용하고 `POSTGRES_DSN`을 설정한다.
3. exact Agent wheel과 Backend `system` extra를 설치한다.
4. `run.ps1`을 실행하고 `http://127.0.0.1:8020`을 연다.

외부 interface에 bind하는 우회 실행은 지원하지 않는다.
