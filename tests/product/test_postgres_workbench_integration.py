from __future__ import annotations

import hashlib
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event, Lock

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from tests.product.agent_v3_fixture import build_agent_v3_result

from customs_backend.agent_chat_semantics import expected_answer_projection
from customs_backend.adapters import (
    CallableAgentClient,
    InMemoryJobPublisher,
    InMemoryObjectStore,
)
from customs_backend.domain import (
    AgentAnswerResponse,
    AgentReviewRequest,
    AuditRecord,
    CaseCreate,
    ChatInteractionRecord,
    CorrectionOperation,
    CorrectionProposalCreate,
    CorrectionProposalDecisionCreate,
    DocumentRecord,
    GroundedQuestion,
    HumanDecisionCreate,
    Principal,
    ReviewEnqueueRequest,
    Role,
    StageEvent,
    TextDocumentCreate,
    canonical_digest,
    utc_now,
)
from customs_backend.ports import (
    AgentPreExecutionRetryableError,
    ConflictError,
    NotFoundError,
    QuotaExceededError,
)
from customs_backend.repository import PostgresWorkbenchRepository
from customs_backend.service import WorkbenchService


def _agent_result(request: AgentReviewRequest) -> dict:
    return build_agent_v3_result(
        request,
        auto_complete=False,
        knowledge_snapshot_id="postgres-integration-generation",
    )


@pytest.mark.postgres_integration
def test_postgres_migration_atomicity_persistence_and_isolation() -> None:
    dsn = os.environ.get("CUSTOMS_BACKEND_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("set CUSTOMS_BACKEND_TEST_POSTGRES_DSN to an ephemeral migrated database")

    engine = create_engine(dsn)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT current_user")) == "customs_runtime"
        tables = set(
            connection.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'operations'"
                )
            ).scalars()
        )
    assert {
        "product_cases",
        "product_documents",
        "supporting_documents",
        "product_jobs",
        "product_review_runs",
        "correction_proposals",
        "human_decisions",
        "product_outbox",
        "review_stage_events",
    } <= tables
    with engine.connect() as connection:
        job_columns = set(
            connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='operations' AND table_name='product_jobs'"
                )
            ).scalars()
        )
        decision_columns = set(
            connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='operations' AND table_name='human_decisions'"
                )
            ).scalars()
        )
        chat_interaction_columns = set(
            connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='operations' "
                    "AND table_name='case_chat_interactions'"
                )
            ).scalars()
        )
    assert {
        "agent_call_state",
        "agent_call_endpoint",
        "agent_call_request_sha256",
        "agent_call_started_at",
        "case_revision",
        "logical_request_sha256",
    } <= job_columns
    assert {
        "decision_scope",
        "idempotency_key",
        "idempotency_request_digest",
    } <= decision_columns
    assert {"created_by", "created_at"} <= chat_interaction_columns

    repository = PostgresWorkbenchRepository(dsn)
    objects = InMemoryObjectStore()
    publisher = InMemoryJobPublisher()
    entered = Event()
    release = Event()
    agent_calls: list[tuple[str, list[str]]] = []

    def review(request: AgentReviewRequest) -> dict:
        agent_calls.append(
            (request.review_run_id, [document.document_id for document in request.documents])
        )
        if len(agent_calls) == 1:
            entered.set()
            assert release.wait(timeout=10)
        return _agent_result(request)

    agent = CallableAgentClient(review)
    service = WorkbenchService(
        repository=repository,
        object_store=objects,
        publisher=publisher,
        agent=agent,
        auto_completion_enabled=False,
    )
    reviewer_a = Principal(
        subject="postgres-reviewer-a",
        org_id="postgres-org-a",
        roles=frozenset({Role.reviewer}),
    )
    approver_a = Principal(
        subject="postgres-approver-a",
        org_id="postgres-org-a",
        roles=frozenset({Role.approver}),
    )
    reviewer_b = Principal(
        subject="postgres-reviewer-b",
        org_id="postgres-org-b",
        roles=frozenset({Role.reviewer}),
    )

    case_a = service.create_case(reviewer_a, CaseCreate(title="PostgreSQL Case A"))
    _case_after_first, first_document = service.add_document(
        reviewer_a,
        case_a.case_id,
        TextDocumentCreate(
            document_type="CI",
            filename="invoice.txt",
            content_text="INVOICE\nHS CODE: 0303540000",
        ),
    )
    job, created = service.enqueue_review(
        reviewer_a,
        case_a.case_id,
        ReviewEnqueueRequest(),
        "postgres-immutable-job",
    )
    assert created
    assert job.request_digest == canonical_digest(job.request)
    assert job.request["review_as_of"] == job.created_at.date().isoformat()
    immutable_revision = int(job.request["case_revision"])
    service.add_document(
        reviewer_a,
        case_a.case_id,
        TextDocumentCreate(
            document_type="PL",
            filename="packing-list.txt",
            content_text="PACKING LIST\nQTY: 10",
        ),
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_execution = executor.submit(
            service.execute_job,
            org_id=reviewer_a.org_id,
            job_id=job.job_id,
        )
        assert entered.wait(timeout=5)
        duplicate_execution = executor.submit(
            service.execute_job,
            org_id=reviewer_a.org_id,
            job_id=job.job_id,
        )
        with pytest.raises(ConflictError):
            duplicate_execution.result(timeout=5)
        release.set()
        first_run = first_execution.result(timeout=10)

    assert agent_calls == [(job.review_run_id, [first_document.document_id])]
    assert first_run.case_revision == immutable_revision
    assert first_run.review_run_id == job.review_run_id
    assert len(repository.list_runs(reviewer_a.org_id, case_a.case_id)) == 1

    # A new repository instance simulates process restart and must read the same
    # immutable Job, provider result, Run, events, and outbox state.
    restarted_repository = PostgresWorkbenchRepository(dsn)
    restarted_service = WorkbenchService(
        repository=restarted_repository,
        object_store=objects,
        publisher=publisher,
        agent=agent,
        auto_completion_enabled=False,
    )
    assert restarted_repository.get_job(reviewer_a.org_id, job.job_id).status.value == ("succeeded")
    assert (
        restarted_repository.get_run(reviewer_a.org_id, first_run.review_run_id).result_digest
        == first_run.result_digest
    )
    assert (
        restarted_repository.get_provider_result(reviewer_a.org_id, first_run.review_run_id)[
            "result_sha256"
        ]
        == first_run.provider_result_sha256
    )

    lease_one = restarted_repository.claim_pending_outbox(
        lease_id="outbox-lease-one",
        lease_expires_at=utc_now() + timedelta(minutes=1),
        limit=100,
    )
    assert len(lease_one) == 1
    assert (
        restarted_repository.claim_pending_outbox(
            lease_id="outbox-lease-two",
            lease_expires_at=utc_now() + timedelta(minutes=1),
            limit=100,
        )
        == []
    )
    restarted_repository.release_outbox_claim(lease_one[0].outbox_id, lease_id="outbox-lease-one")
    assert restarted_service.dispatch_pending_outbox() == 1
    assert restarted_repository.list_pending_outbox() == []

    # Stale revision CAS must roll back the document and audit insert together.
    case_now = restarted_service.get_case(reviewer_a, case_a.case_id)
    rejected_document_id = f"doc_{uuid.uuid4().hex}"
    rejected_text = b"stale write"
    rejected_record = DocumentRecord(
        document_id=rejected_document_id,
        case_id=case_a.case_id,
        org_id=reviewer_a.org_id,
        document_type="CI",
        filename="stale.txt",
        media_type="text/plain",
        content_sha256=hashlib.sha256(rejected_text).hexdigest(),
        object_key="test/stale.txt",
        byte_size=len(rejected_text),
        character_count=len(rejected_text.decode("utf-8")),
        created_by=reviewer_a.subject,
        created_at=utc_now(),
    )
    stale_audit = AuditRecord(
        audit_id=f"audit_{uuid.uuid4().hex}",
        org_id=reviewer_a.org_id,
        case_id=case_a.case_id,
        actor_id=reviewer_a.subject,
        action="stale.write.must.rollback",
        created_at=utc_now(),
    )
    with pytest.raises(ConflictError):
        restarted_repository.add_document(
            rejected_record,
            expected_revision=case_now.revision - 1,
            new_manifest_digest="f" * 64,
            audit=stale_audit,
        )
    assert rejected_document_id not in {
        item.document_id
        for item in restarted_repository.list_documents(reviewer_a.org_id, case_a.case_id)
    }
    assert "stale.write.must.rollback" not in {
        item.action for item in restarted_repository.list_audit(reviewer_a.org_id, case_a.case_id)
    }

    # Build a current parent Run, then enforce proposal -> Approver -> child Job.
    current_job, _created = restarted_service.enqueue_review(
        reviewer_a,
        case_a.case_id,
        ReviewEnqueueRequest(),
        "postgres-current-parent",
    )
    current_parent = restarted_service.execute_job(
        org_id=reviewer_a.org_id,
        job_id=current_job.job_id,
    )
    current_case = restarted_service.get_case(reviewer_a, case_a.case_id)

    # Chat reserves its durable idempotency scope before the external request.
    # A concurrent duplicate cannot call Agent twice, while a committed replay
    # returns the exact immutable message.
    chat_entered = Event()
    chat_release = Event()
    chat_calls = 0

    def answer(provider_result, requested_question, question_id):
        nonlocal chat_calls
        chat_calls += 1
        chat_entered.set()
        assert chat_release.wait(timeout=10)
        return AgentAnswerResponse.model_validate(
            expected_answer_projection(
                provider_result=provider_result,
                question=requested_question,
                effective_disposition=current_parent.review_disposition.value,
            ).deterministic_wire(question_id)
        )

    restarted_service.agent = CallableAgentClient(review, answer=answer)
    question = GroundedQuestion(
        # A field-intent question must cross the external Agent call-once
        # barrier. Outcome questions are intentionally answered by the
        # Backend-owned canonical projection without an Agent call.
        question="문서",
        bound_result_digest=current_parent.result_digest,
    )

    def ask_once():
        return restarted_service.answer(
            reviewer_a,
            current_parent.review_run_id,
            question,
            "postgres-chat-call-once",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_answer = executor.submit(ask_once)
        assert chat_entered.wait(timeout=5)
        duplicate_answer = executor.submit(ask_once)
        with pytest.raises(ConflictError):
            duplicate_answer.result(timeout=5)
        chat_release.set()
        stored_answer = first_answer.result(timeout=10)
    assert ask_once() == stored_answer
    assert chat_calls == 1

    # Eleven distinct keys arrive together for one actor. The reservation
    # transaction must durably admit exactly ten; counting only completed chat
    # history before reserving would allow all eleven through this boundary.
    quota_actor = Principal(
        subject="postgres-chat-quota-actor",
        org_id=reviewer_a.org_id,
        roles=frozenset({Role.approver}),
    )
    quota_calls: list[str] = []
    quota_calls_lock = Lock()

    def quota_answer(provider_result, requested_question, question_id):
        with quota_calls_lock:
            quota_calls.append(question_id)
        return AgentAnswerResponse.model_validate(
            expected_answer_projection(
                provider_result=provider_result,
                question=requested_question,
                effective_disposition=current_parent.review_disposition.value,
            ).deterministic_wire(question_id)
        )

    restarted_service.agent = CallableAgentClient(review, answer=quota_answer)

    def ask_unique(index: int):
        return restarted_service.answer(
            quota_actor,
            current_parent.review_run_id,
            GroundedQuestion(
                # Keep every admitted request on the external field-answer
                # path while distinct idempotency keys exercise the quota.
                question="문서",
                bound_result_digest=current_parent.result_digest,
            ),
            f"postgres-chat-quota-{index}",
        )

    admitted = []
    rejected = []
    with ThreadPoolExecutor(max_workers=11) as executor:
        futures = [executor.submit(ask_unique, index) for index in range(11)]
        for future in futures:
            try:
                admitted.append(future.result(timeout=15))
            except QuotaExceededError as exc:
                rejected.append(exc)
    assert len(admitted) == 10
    assert len(rejected) == 1
    assert len(quota_calls) == 10
    assert len({item.message_id for item in admitted}) == 10
    assert (
        len(
            [
                item
                for item in restarted_repository.list_chat(
                    reviewer_a.org_id, current_parent.review_run_id
                )
                if item.created_by == quota_actor.subject
            ]
        )
        == 10
    )

    # The authoritative 100/run cap counts durable reservations, calls already
    # started, and completed responses. Fill this Run to 99 with a deliberate
    # state mix, then race two different actors for the final slot.
    def quota_interaction(label: str, actor: str) -> ChatInteractionRecord:
        now = utc_now()
        return ChatInteractionRecord(
            interaction_id=f"interaction_{uuid.uuid4().hex}",
            review_run_id=current_parent.review_run_id,
            case_id=current_parent.case_id,
            org_id=reviewer_a.org_id,
            idempotency_key=f"postgres-run-cap-{label}",
            idempotency_request_digest=canonical_digest({"label": label, "kind": "request"}),
            agent_request_sha256=canonical_digest({"label": label, "kind": "agent"}),
            created_by=actor,
            created_at=now,
            updated_at=now,
        )

    for index in range(88):
        proposed = quota_interaction(
            f"fill-{index}",
            f"postgres-run-cap-actor-{index // 10}",
        )
        reserved, created = restarted_repository.reserve_chat_interaction(
            proposed,
            actor_window_started_at=proposed.created_at - timedelta(minutes=1),
            actor_window_limit=10,
            review_run_limit=100,
        )
        assert created
        if index % 2 == 0:
            restarted_repository.start_chat_call_once(
                org_id=reviewer_a.org_id,
                interaction_id=reserved.interaction_id,
            )

    def reserve_final_slot(index: int):
        proposed = quota_interaction(
            f"final-{index}",
            f"postgres-run-cap-final-actor-{index}",
        )
        return restarted_repository.reserve_chat_interaction(
            proposed,
            actor_window_started_at=proposed.created_at - timedelta(minutes=1),
            actor_window_limit=10,
            review_run_limit=100,
        )

    final_admitted = []
    final_rejected = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(reserve_final_slot, index) for index in range(2)]
        for future in futures:
            try:
                final_admitted.append(future.result(timeout=10))
            except QuotaExceededError as exc:
                final_rejected.append(exc)
    assert len(final_admitted) == 1
    assert len(final_rejected) == 1
    with engine.connect() as connection:
        state_counts = dict(
            connection.execute(
                text(
                    "SELECT state, count(*) FROM operations.case_chat_interactions "
                    "WHERE org_id = :org_id AND review_run_id = :review_run_id "
                    "GROUP BY state"
                ),
                {
                    "org_id": reviewer_a.org_id,
                    "review_run_id": current_parent.review_run_id,
                },
            ).all()
        )
    assert state_counts == {"call_started": 44, "completed": 11, "reserved": 45}

    proposal_request = CorrectionProposalCreate(
        parent_result_digest=current_parent.result_digest,
        parent_input_manifest_sha256=current_parent.agent_input_manifest_sha256,
        case_revision=current_case.revision,
        corrections=[
            CorrectionOperation(
                document_id=first_document.document_id,
                field_path="fields.invoice_number",
                new_value="INV-POSTGRES-CORRECTED",
                reason="독립 검수에서 원문 재확인",
            )
        ],
    )

    def create_same_proposal():
        return restarted_service.create_correction_proposal(
            reviewer_a,
            current_parent.review_run_id,
            proposal_request,
            "postgres-proposal-create",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        proposal_futures = [executor.submit(create_same_proposal) for _ in range(2)]
        proposal_replays = [future.result(timeout=10) for future in proposal_futures]
    assert proposal_replays[0] == proposal_replays[1]
    proposal = proposal_replays[0]
    decided, decision, child_job = restarted_service.decide_correction_proposal(
        approver_a,
        proposal.proposal_id,
        CorrectionProposalDecisionCreate(
            decision="approve",
            reason="원문과 correction을 독립 확인",
            parent_result_digest=current_parent.result_digest,
            case_revision=current_case.revision,
        ),
        "postgres-approved-child",
    )
    assert decided.status.value == "approved"
    assert decision.proposal_id == proposal.proposal_id
    assert child_job is not None and child_job.parent_run_id == current_parent.review_run_id
    replayed = restarted_service.decide_correction_proposal(
        approver_a,
        proposal.proposal_id,
        CorrectionProposalDecisionCreate(
            decision="approve",
            reason="원문과 correction을 독립 확인",
            parent_result_digest=current_parent.result_digest,
            case_revision=current_case.revision,
        ),
        "postgres-approved-child",
    )
    assert replayed == (decided, decision, child_job)
    child_run = restarted_service.execute_job(
        org_id=reviewer_a.org_id,
        job_id=child_job.job_id,
    )
    assert child_run.parent_run_id == current_parent.review_run_id
    assert (
        PostgresWorkbenchRepository(dsn)
        .get_run(reviewer_a.org_id, child_run.review_run_id)
        .result_digest
        == child_run.result_digest
    )

    concurrent_proposal = restarted_service.create_correction_proposal(
        reviewer_a,
        child_run.review_run_id,
        CorrectionProposalCreate(
            parent_result_digest=child_run.result_digest,
            parent_input_manifest_sha256=child_run.agent_input_manifest_sha256,
            case_revision=current_case.revision,
            corrections=[
                CorrectionOperation(
                    document_id=first_document.document_id,
                    field_path="fields.invoice_number",
                    new_value="INV-CONCURRENT",
                    reason="concurrent independent review",
                )
            ],
        ),
        "postgres-concurrent-proposal-create",
    )

    def decide_proposal(value: str, key: str):
        return restarted_service.decide_correction_proposal(
            approver_a,
            concurrent_proposal.proposal_id,
            CorrectionProposalDecisionCreate(
                decision=value,
                reason=f"concurrent proposal {value}",
                parent_result_digest=child_run.result_digest,
                case_revision=current_case.revision,
            ),
            key,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        proposal_futures = [
            executor.submit(decide_proposal, "approve", "proposal-race-approve"),
            executor.submit(decide_proposal, "reject", "proposal-race-reject"),
        ]
    proposal_results = []
    proposal_conflicts = []
    for future in proposal_futures:
        try:
            proposal_results.append(future.result())
        except ConflictError as exc:
            proposal_conflicts.append(exc)
    assert len(proposal_results) == 1
    assert len(proposal_conflicts) == 1
    assert PostgresWorkbenchRepository(dsn).get_correction_proposal(
        reviewer_a.org_id, concurrent_proposal.proposal_id
    ).status.value in {"approved", "rejected"}

    # A database lock plus the unique decision scope permits exactly one
    # terminal decision even when approve/reject race in separate transactions.
    def decide_current(value: str):
        return restarted_service.decide(
            approver_a,
            current_parent.review_run_id,
            HumanDecisionCreate(decision=value, reason=f"concurrent {value}"),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        decision_futures = [
            executor.submit(decide_current, value) for value in ("approve", "reject")
        ]
    decision_results = []
    decision_conflicts = []
    for future in decision_futures:
        try:
            decision_results.append(future.result())
        except ConflictError as exc:
            decision_conflicts.append(exc)
    assert len(decision_results) == 1
    assert len(decision_conflicts) == 1
    assert (
        len(restarted_repository.list_decisions(reviewer_a.org_id, current_parent.review_run_id))
        == 2
    )  # one proposal decision plus one general Run decision

    # Once an external call may have been delivered, a process restart must
    # never call the Agent again for the same Job.
    uncertain_job, _ = restarted_service.enqueue_review(
        reviewer_a,
        case_a.case_id,
        ReviewEnqueueRequest(),
        "postgres-call-once-crash",
    )
    uncertain_calls = 0

    def uncertain_review(_request: AgentReviewRequest) -> dict:
        nonlocal uncertain_calls
        uncertain_calls += 1
        raise TimeoutError("external response lost")

    restarted_service.agent = CallableAgentClient(uncertain_review)
    with pytest.raises(TimeoutError, match="external response lost"):
        restarted_service.execute_job(org_id=reviewer_a.org_id, job_id=uncertain_job.job_id)
    uncertain_record = restarted_repository.get_job(reviewer_a.org_id, uncertain_job.job_id)
    assert uncertain_record is not None
    assert uncertain_record.status.value == "delivery_uncertain"
    assert uncertain_record.agent_call_state.value == "delivery_uncertain"
    after_crash = WorkbenchService(
        repository=PostgresWorkbenchRepository(dsn),
        object_store=objects,
        publisher=publisher,
        agent=CallableAgentClient(_agent_result),
        auto_completion_enabled=False,
    )
    with pytest.raises(ConflictError):
        after_crash.execute_job(org_id=reviewer_a.org_id, job_id=uncertain_job.job_id)
    assert uncertain_calls == 1

    case_b = restarted_service.create_case(reviewer_b, CaseCreate(title="PostgreSQL Case B"))
    assert {item.case_id for item in restarted_service.list_cases(reviewer_a)} == {case_a.case_id}
    assert {item.case_id for item in restarted_service.list_cases(reviewer_b)} == {case_b.case_id}
    with pytest.raises(NotFoundError):
        restarted_service.get_case(reviewer_b, case_a.case_id)
    assert restarted_repository.get_run(reviewer_b.org_id, child_run.review_run_id) is None

    with engine.connect() as connection:
        counts = dict(
            connection.execute(
                text(
                    "SELECT org_id, count(*) FROM operations.product_cases "
                    "WHERE org_id LIKE 'postgres-org-%' GROUP BY org_id ORDER BY org_id"
                )
            ).all()
        )
    assert counts == {"postgres-org-a": 1, "postgres-org-b": 1}

    # Runtime privileges and DB triggers independently protect audit, decision,
    # stage-event, Run, and its embedded provider result from mutation.
    audit_id = restarted_repository.list_audit(reviewer_a.org_id, case_a.case_id)[0].audit_id
    stage_event_id = restarted_repository.list_stage_events(reviewer_a.org_id, job.job_id)[
        0
    ].event_id
    runtime_tamper_statements = [
        (
            "UPDATE operations.product_review_runs SET provider_result_json='{}'::jsonb "
            "WHERE review_run_id=:identifier",
            first_run.review_run_id,
        ),
        (
            "UPDATE operations.human_decisions SET record_json='{}'::jsonb "
            "WHERE decision_id=:identifier",
            decision.decision_id,
        ),
        (
            "DELETE FROM operations.product_audit_log WHERE audit_id=:identifier",
            audit_id,
        ),
        (
            "DELETE FROM operations.review_stage_events WHERE event_id=:identifier",
            stage_event_id,
        ),
    ]
    for statement, identifier in runtime_tamper_statements:
        with pytest.raises(DBAPIError, match="permission denied"):
            with engine.begin() as connection:
                connection.execute(text(statement), {"identifier": identifier})

    migrator_dsn = os.environ.get("CUSTOMS_BACKEND_TEST_POSTGRES_MIGRATOR_DSN")
    if migrator_dsn:
        owner_engine = create_engine(migrator_dsn)
        # Table owners bypass GRANT/REVOKE, so the trigger is a separate control.
        with pytest.raises(DBAPIError, match="append-only"):
            with owner_engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE operations.product_review_runs "
                        "SET provider_result_json='{}'::jsonb WHERE review_run_id=:run_id"
                    ),
                    {"run_id": first_run.review_run_id},
                )
        owner_engine.dispose()
    engine.dispose()


@pytest.mark.postgres_integration
def test_postgres_pre_execution_retry_survives_restart_without_outbox_duplication() -> None:
    dsn = os.environ.get("CUSTOMS_BACKEND_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("set CUSTOMS_BACKEND_TEST_POSTGRES_DSN to an ephemeral migrated database")

    suffix = uuid.uuid4().hex
    reviewer = Principal(
        subject=f"preexec-retry-reviewer-{suffix}",
        org_id=f"preexec-retry-org-{suffix}",
        roles=frozenset({Role.reviewer}),
    )
    objects = InMemoryObjectStore()
    publisher = InMemoryJobPublisher()
    transport_attempts = 0
    runtime_executions = 0

    def review(request: AgentReviewRequest) -> dict:
        nonlocal transport_attempts, runtime_executions
        transport_attempts += 1
        if transport_attempts == 1:
            raise AgentPreExecutionRetryableError(
                request_id="8a5dc01d-41f5-4e56-b9ba-a305f493af2a"
            )
        runtime_executions += 1
        return _agent_result(request)

    first_repository = PostgresWorkbenchRepository(dsn)
    first_service = WorkbenchService(
        repository=first_repository,
        object_store=objects,
        publisher=publisher,
        agent=CallableAgentClient(review),
        auto_completion_enabled=False,
    )
    case = first_service.create_case(
        reviewer,
        CaseCreate(title="PostgreSQL pre-execution retry"),
    )
    first_service.add_document(
        reviewer,
        case.case_id,
        TextDocumentCreate(
            document_type="CI",
            filename="invoice.txt",
            content_text="INVOICE\nHS CODE: 0303540000",
        ),
    )
    job, created = first_service.enqueue_review(
        reviewer,
        case.case_id,
        ReviewEnqueueRequest(),
        f"postgres-pre-execution-retry-{suffix}",
    )
    assert created

    with pytest.raises(AgentPreExecutionRetryableError):
        first_service.execute_job(org_id=reviewer.org_id, job_id=job.job_id)
    retryable = first_repository.get_job(reviewer.org_id, job.job_id)
    assert retryable is not None
    assert retryable.status.value == "queued"
    assert retryable.agent_call_state.value == "not_started"
    assert retryable.attempt_count == 1
    assert retryable.agent_call_endpoint is None
    assert retryable.agent_call_request_sha256 is None
    assert retryable.agent_call_started_at is None

    engine = create_engine(dsn)
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT status, agent_call_state, agent_call_endpoint, "
                "agent_call_request_sha256, agent_call_started_at, attempt_count "
                "FROM operations.product_jobs WHERE org_id=:org_id AND job_id=:job_id"
            ),
            {"org_id": reviewer.org_id, "job_id": job.job_id},
        ).one()
    assert tuple(row) == ("queued", "not_started", None, None, None, 1)

    restarted_repository = PostgresWorkbenchRepository(dsn)
    restarted_service = WorkbenchService(
        repository=restarted_repository,
        object_store=objects,
        publisher=publisher,
        agent=CallableAgentClient(review),
        auto_completion_enabled=False,
    )
    run = restarted_service.execute_job(org_id=reviewer.org_id, job_id=job.job_id)
    succeeded = restarted_repository.get_job(reviewer.org_id, job.job_id)
    assert succeeded is not None
    assert succeeded.status.value == "succeeded"
    assert succeeded.agent_call_state.value == "completed"
    assert succeeded.attempt_count == 2
    assert succeeded.error_code is None
    assert run.review_run_id == job.review_run_id
    assert transport_attempts == 2
    assert runtime_executions == 1
    assert len(restarted_repository.list_runs(reviewer.org_id, case.case_id)) == 1

    with engine.connect() as connection:
        outbox_count = connection.scalar(
            text(
                "SELECT count(*) FROM operations.product_outbox "
                "WHERE org_id=:org_id AND aggregate_id=:job_id"
            ),
            {"org_id": reviewer.org_id, "job_id": job.job_id},
        )
    assert outbox_count == 1
    events = restarted_repository.list_stage_events(reviewer.org_id, job.job_id)
    scheduled = [
        event
        for event in events
        if event.message_code == "review.agent_pre_execution_retry_scheduled"
    ]
    assert len(scheduled) == 1
    assert scheduled[0].detail["agent_request_id"] == (
        "8a5dc01d-41f5-4e56-b9ba-a305f493af2a"
    )

    cas_case = restarted_service.create_case(
        reviewer,
        CaseCreate(title="PostgreSQL pre-execution CAS race"),
    )
    restarted_service.add_document(
        reviewer,
        cas_case.case_id,
        TextDocumentCreate(
            document_type="CI",
            filename="cas-invoice.txt",
            content_text="INVOICE\nHS CODE: 0303540000",
        ),
    )
    cas_job, cas_created = restarted_service.enqueue_review(
        reviewer,
        cas_case.case_id,
        ReviewEnqueueRequest(),
        f"postgres-pre-execution-cas-{suffix}",
    )
    assert cas_created
    lease_id = f"preexec-cas-lease-{suffix}"
    sequence = len(
        restarted_repository.list_stage_events(reviewer.org_id, cas_job.job_id)
    ) + 1
    claimed = restarted_repository.claim_job(
        org_id=reviewer.org_id,
        job_id=cas_job.job_id,
        lease_id=lease_id,
        lease_expires_at=utc_now() + timedelta(minutes=5),
        event=StageEvent(
            event_id=f"preexec-cas-claim-event-{suffix}",
            job_id=cas_job.job_id,
            sequence=sequence,
            stage="document_analysis",
            state="started",
            message_code="agent.started",
            occurred_at=utc_now(),
        ),
        audit=AuditRecord(
            audit_id=f"preexec-cas-claim-audit-{suffix}",
            org_id=reviewer.org_id,
            case_id=cas_case.case_id,
            actor_id="system:test",
            action="review.started",
            created_at=utc_now(),
        ),
    )
    assert claimed is not None
    request_sha256 = "a" * 64
    restarted_repository.start_agent_call_once(
        org_id=reviewer.org_id,
        job_id=cas_job.job_id,
        expected_lease_id=lease_id,
        endpoint="review",
        request_sha256=request_sha256,
        audit=AuditRecord(
            audit_id=f"preexec-cas-start-audit-{suffix}",
            org_id=reviewer.org_id,
            case_id=cas_case.case_id,
            actor_id="system:test",
            action="agent_call.started",
            created_at=utc_now(),
        ),
    )
    event_count_before = len(
        restarted_repository.list_stage_events(reviewer.org_id, cas_job.job_id)
    )
    audit_count_before = len(
        restarted_repository.list_audit(reviewer.org_id, cas_case.case_id)
    )
    with engine.connect() as connection:
        outbox_count_before = connection.scalar(
            text(
                "SELECT count(*) FROM operations.product_outbox "
                "WHERE org_id=:org_id AND aggregate_id=:job_id"
            ),
            {"org_id": reviewer.org_id, "job_id": cas_job.job_id},
        )
    assert outbox_count_before == 1
    writers = [PostgresWorkbenchRepository(dsn), PostgresWorkbenchRepository(dsn)]

    def reset_started_call(index: int):
        return writers[index].mark_agent_pre_execution_retryable(
            org_id=reviewer.org_id,
            job_id=cas_job.job_id,
            expected_lease_id=lease_id,
            expected_endpoint="review",
            expected_request_sha256=request_sha256,
            error_code="dependency_unready",
            terminal=False,
            event=StageEvent(
                event_id=f"preexec-cas-race-event-{suffix}-{index}",
                job_id=cas_job.job_id,
                sequence=sequence + 1,
                stage="failed",
                state="failed",
                message_code="review.agent_pre_execution_retry_scheduled",
                occurred_at=utc_now(),
                detail={
                    "error_code": "dependency_unready",
                    "agent_request_id": f"race-request-{index}",
                },
            ),
            audit=AuditRecord(
                audit_id=f"preexec-cas-race-audit-{suffix}-{index}",
                org_id=reviewer.org_id,
                case_id=cas_case.case_id,
                actor_id="system:test",
                action="job.agent_pre_execution_retry_scheduled",
                detail={
                    "error_code": "dependency_unready",
                    "agent_request_id": f"race-request-{index}",
                },
                created_at=utc_now(),
            ),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(reset_started_call, index) for index in range(2)]
    successes = []
    conflicts = []
    for future in futures:
        try:
            successes.append(future.result(timeout=10))
        except ConflictError as exc:
            conflicts.append(exc)
    assert len(successes) == 1
    assert len(conflicts) == 1
    cas_reset = restarted_repository.get_job(reviewer.org_id, cas_job.job_id)
    assert cas_reset is not None
    assert cas_reset.status.value == "queued"
    assert cas_reset.agent_call_state.value == "not_started"
    assert cas_reset.attempt_count == 1
    assert (
        len(restarted_repository.list_stage_events(reviewer.org_id, cas_job.job_id))
        == event_count_before + 1
    )
    assert (
        len(restarted_repository.list_audit(reviewer.org_id, cas_case.case_id))
        == audit_count_before + 1
    )
    with engine.connect() as connection:
        outbox_count_after = connection.scalar(
            text(
                "SELECT count(*) FROM operations.product_outbox "
                "WHERE org_id=:org_id AND aggregate_id=:job_id"
            ),
            {"org_id": reviewer.org_id, "job_id": cas_job.job_id},
        )
    assert outbox_count_after == outbox_count_before == 1
    engine.dispose()


@pytest.mark.postgres_integration
def test_postgres_supporting_document_cap_serializes_concurrent_direct_inserts() -> None:
    migrator_dsn = os.environ.get("CUSTOMS_BACKEND_TEST_POSTGRES_MIGRATOR_DSN")
    if not migrator_dsn:
        pytest.skip("set the migrator DSN for the authoritative database cap test")
    engine = create_engine(migrator_dsn)
    suffix = uuid.uuid4().hex
    case_id = f"supporting-cap-{suffix}"
    org_id = f"supporting-cap-org-{suffix}"
    document_ids = [f"supporting-cap-doc-{suffix}-{index:03d}" for index in range(101)]
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO operations.product_cases"
                    "(case_id, org_id, revision, record_json) "
                    "VALUES (:case_id, :org_id, 0, '{}'::jsonb)"
                ),
                {"case_id": case_id, "org_id": org_id},
            )
            connection.execute(
                text(
                    "INSERT INTO operations.product_documents"
                    "(document_id, case_id, org_id, record_json) "
                    "VALUES (:document_id, :case_id, :org_id, '{}'::jsonb)"
                ),
                [
                    {"document_id": document_id, "case_id": case_id, "org_id": org_id}
                    for document_id in document_ids
                ],
            )
            connection.execute(
                text(
                    "INSERT INTO operations.supporting_documents"
                    "(supporting_document_id, document_id, case_id, org_id, record_json) "
                    "VALUES (:supporting_id, :document_id, :case_id, :org_id, '{}'::jsonb)"
                ),
                [
                    {
                        "supporting_id": f"{case_id}:{document_id}",
                        "document_id": document_id,
                        "case_id": case_id,
                        "org_id": org_id,
                    }
                    for document_id in document_ids[:99]
                ],
            )

        def insert_boundary(document_id: str) -> bool:
            try:
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "INSERT INTO operations.supporting_documents"
                            "(supporting_document_id, document_id, case_id, org_id, record_json) "
                            "VALUES (:supporting_id, :document_id, :case_id, :org_id, "
                            "'{}'::jsonb)"
                        ),
                        {
                            "supporting_id": f"{case_id}:{document_id}",
                            "document_id": document_id,
                            "case_id": case_id,
                            "org_id": org_id,
                        },
                    )
            except DBAPIError as exc:
                if getattr(exc.orig, "sqlstate", None) == "23514":
                    return False
                raise
            return True

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(insert_boundary, document_id) for document_id in document_ids[99:]
            ]
            results = [future.result(timeout=10) for future in futures]
        assert sorted(results) == [False, True]
        with engine.connect() as connection:
            count = connection.scalar(
                text(
                    "SELECT count(*) FROM operations.supporting_documents "
                    "WHERE org_id=:org_id AND case_id=:case_id"
                ),
                {"org_id": org_id, "case_id": case_id},
            )
        assert count == 100
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM operations.supporting_documents WHERE case_id=:case_id"),
                {"case_id": case_id},
            )
            connection.execute(
                text("DELETE FROM operations.product_documents WHERE case_id=:case_id"),
                {"case_id": case_id},
            )
            connection.execute(
                text("DELETE FROM operations.product_cases WHERE case_id=:case_id"),
                {"case_id": case_id},
            )
        engine.dispose()
