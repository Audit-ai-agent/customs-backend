# Security Policy

## 현재 보안 경계

`customs-agent-demo`는 인증, 인가, tenant 격리가 없는 로컬 통합 검증
도구다. 따라서 다음을 보안 불변식으로 다룬다.

- HTTP 서버는 `127.0.0.1`에만 bind한다.
- 원격 client 요청을 middleware에서 403으로 거부한다.
- 사용자 입력 Evidence URL은 loopback host만 허용한다.
- 운영·공유 서버·데모 호스팅에 배포하지 않는다.
- 기본 DSN은 개발 예시일 뿐이며 운영 비밀값으로 사용하지 않는다.

제품 Backend를 배포하기 전에는 OIDC, RBAC, tenant 격리, CSRF/CORS 정책,
rate limit, secret manager, 구조화된 오류 마스킹, audit log를 별도로
구현해야 한다.

보안 취약점은 공개 Issue가 아니라 조직 관리자에게 비공개로 보고하고,
재현 절차·영향·완화책을 포함한다.
