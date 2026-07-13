# Backend Architecture

## 의도

이 저장소는 현재 구축된 전체 제품 Backend가 아니다. 기존 통합 검증
경로를 잃지 않으면서 Operational DB의 소유권을 Backend로 고정하는
이행 기반이다.

```text
local CLI/UI (unauthenticated, loopback-only)
  -> customs-ai-agent==0.1.0 public package API
  -> Evidence API v1 over localhost HTTP
  -> immutable ReviewRunSnapshot
  -> OperationalRepository
       -> in-memory (unit/system tests)
       -> PostgreSQL operations schema (live verification)
```

## 불변식

1. Agent graph는 Operational DB를 직접 호출하지 않는다.
2. `review_run_id`는 멱등 키고 기존 payload 변경은 거부한다.
3. rerun은 `parent_run_id`를 가진 새 run으로만 저장한다.
4. Finding은 같은 item/run에 스냅샷된 evidence만 참조한다.
5. 저장 후 canonical hash readback이 다르면 성공으로 표시하지 않는다.
6. Data 계약·평가기는 Data 저장소에서만 변경한다.
7. 시스템 연동은 패키지 버전과 HTTP 계약을 사용하고 형제 소스
   상대경로를 사용하지 않는다.

## 이행 경계

`integration` 네임스페이스는 기존 테스트와 운영 절차를 안전하게 이전하기
위한 전환기 호환계층이다. 향후 제품 Backend를 구축할 때 Case/Job/HITL
Application Service와 인증된 API를 `src` 아래에 추가하고, 이 UI는 계속
로컬 검증 도구로만 남긴다.
