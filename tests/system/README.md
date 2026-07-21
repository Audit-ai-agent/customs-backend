# System test boundary

이 폴더는 Backend Application Service가 AI Agent 결과를 Operational DB 스냅샷으로
저장하고 readback hash를 검증하는 경계를 테스트한다.

- 이 폴더는 legacy/historical integration 경계를 보존하며
  `customs-ai-agent==0.1.0` wheel을 의도적으로 고정한다.
- 제품 Backend의 현재 pin은 `customs-ai-agent==0.3.0`이며
  `tests/product` 및 private Agent HTTP V2/V3 계약으로 검증한다.
- 현재 자동 테스트는 주입된 deterministic Retriever와 in-memory repository를
  사용하여 Backend 경계를 격리 검증한다.
- 실제 Evidence Service·PostgreSQL 연동은 `customs-review` 또는 로컬 UI로
  수행하며, 서비스가 없으면 PASS/SKIP으로 처리하지 않는다.
- Data 제공자의 Evidence JSON Schema, Gold fixture, structured/hybrid evaluator를
  이 저장소에 복제하지 않는다.
- Data 계약 호환성과 RAG 품질 gate는 `customs-data-platform`의 canonical
  테스트와 발행된 Evidence API v1 fixture를 사용해 별도 CI에서 검증한다.
