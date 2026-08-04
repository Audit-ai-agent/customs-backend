# Security Policy

## 현재 보안 경계

개발용 Local UAT는 Git 밖 승인자 전용 운영 번들로만 조합한다. 따라서 다음을
보안 불변식으로 다룬다.

- UAT 비밀값과 실데이터는 Git에 넣지 않고 개인별 로컬 주입으로만 전달한다.
- 번들은 형제 저장소를 참조할 뿐 제품 코드를 복사하거나 package pin으로 대체하지 않는다.
- 운영·공유 서버·데모 호스팅에 배포하지 않는다.
- 기본 DSN은 개발 예시일 뿐이며 운영 비밀값으로 사용하지 않는다.

제품 Backend를 배포하기 전에는 OIDC, RBAC, tenant 격리, CSRF/CORS 정책,
rate limit, secret manager, 구조화된 오류 마스킹, audit log를 별도로
구현해야 한다.

보안 취약점은 공개 Issue가 아니라 조직 관리자에게 비공개로 보고하고,
재현 절차·영향·완화책을 포함한다.
