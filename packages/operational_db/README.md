# Operational DB

AI Agent 내부 상태를 직접 저장하지 않고, 외부에 공개되는 `ReviewRunResult`와 그 실행에
사용된 evidence 전문을 불변 스냅샷으로 보존하는 저장 계층이다.

핵심 규칙:

- `review_run_id`는 멱등 키다. 같은 ID에 다른 payload를 다시 저장하면 거부한다.
- rerun은 기존 행을 갱신하지 않고 `parent_run_id`를 가진 새 run으로 저장한다.
- finding이 참조하는 모든 evidence는 같은 run의 `evidence_snapshots`에 존재해야 한다.
- report status, run status, `requires_human_review`는 저장 전에 일관성을 검증한다.
- Agent 노드는 이 DB를 직접 호출하지 않는다. Headless runner 또는 향후 Application
  Service가 Agent 완료 후 한 트랜잭션으로 저장한다.
- 입력 Mock 문서와 content SHA-256, 분류/추출/정규화 결과, field path별 추출값,
  pairing 결과를 보존한다.
- Evidence API request, item별 HS 후보/coverage/evidence, top-level/item error를
  `result_json`에 보존해 partial/failed 판단도 DB만으로 재현할 수 있다.
- Migration `008`은 검증된 사용자 보고서와 멀티턴 심사 도우미의 conversation,
  call-once job, message를 additive table에 저장한다. 기존 Run·Chat V1·HITL·Audit
  row는 수정하지 않는다.

PostgreSQL 적용:

```powershell
python scripts/migrate.py --dsn $env:POSTGRES_DSN
```

로컬 단위 테스트는 외부 DB 없이 `InMemoryOperationalRepository`로 동일 불변식을
검증한다.
