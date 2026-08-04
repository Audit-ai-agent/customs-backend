# Backend 구조

```text
Frontend Browser
  -> Backend /api/v1
       -> Case / text document / review state
       -> Agent HTTP /internal/agent/v2
            -> Data Evidence HTTP
```

Backend는 Case와 text document, Job, review result를 소유한다. Agent는 문서 심사와
Data 호출을 소유한다. 형제 저장소 import, Browser의 Agent/Data credential 사용, DB 직접
접근은 없다.

`local-development`는 메모리 상태와 loopback 개발 session을 사용한다. Docker, 모델,
Law API, Claude key 없이 네 HTTP 흐름을 점검한다.

`functional-uat`는 로컬 PostgreSQL·MinIO·Keycloak과 실제 Agent/Data를 사용한다.
Backend는 `2026-08-05` 기준일을 고정하고 심사 요청을 한 번 처리한다.

공통 경계는 다음뿐이다.

1. Backend→Agent Bearer token
2. 정확히 같은 `X-Customs-Service-Mode`
3. Git 추적 V3 request/result schema bytes
4. immutable input hash와 결과 hash
5. 항상 사람 검토 상태

기존 Browser 호환 필드는 정보 표시용으로만 남아 있으며, 어떤 결과도 자동으로 끝내지
않는다.
