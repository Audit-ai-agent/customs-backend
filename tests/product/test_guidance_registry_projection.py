from __future__ import annotations

from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path

import pytest

from customs_backend.domain import (
    AnswerScope,
    CitationKind,
    ConversationMessageRecord,
    ConversationMessageRole,
    MessageCitation,
)
from customs_backend.functional_repository import _message_persistence_dump
from customs_backend.guidance_registry import (
    guidance_registry_projection,
    verify_guidance_registry_projection_bundle,
)
from customs_backend.ports import ConflictError
from customs_backend.service import _validate_guidance_citations


ROOT = Path(__file__).resolve().parents[2]


def test_packaged_v1_v2_guidance_manifests_are_exact_data_projections() -> None:
    """Backend pins only safe Data metadata, never source material or hashes in UI."""

    expected = {
        "1.0": {
            "registry": "6fed779ee0ec1cd2358972925fe1b2788838a197d4f33e62e461fcfed2bb7d6c",
            "manifest": "da3aa6e3644012d55bebb5ae1bbc2d2f052a68d0b3a3c14aafab56a677192092",
        },
        "2.0": {
            "registry": "a450ae6286d9a12729c5459543a35fd9c1c3df2ff59977700f72887c3df6543e",
            "manifest": "51375a77351e73a32763e65679099d9bf04190078956e28ed1161b3ec5e1b7bb",
        },
    }
    for version, values in expected.items():
        projection = guidance_registry_projection(version)  # type: ignore[arg-type]
        manifest = ROOT / "src" / "customs_backend" / "contracts" / "evidence" / "v3" / (
            f"guidance_registry_v{version[0]}.manifest.json"
        )
        assert projection.registry_sha256 == values["registry"]
        assert sha256(manifest.read_bytes()).hexdigest() == values["manifest"]
        assert projection.sources
        assert projection.scopes
    verify_guidance_registry_projection_bundle()


def test_v2_registry_keeps_the_page_level_direction_binding() -> None:
    projection = guidance_registry_projection("2.0")
    export_source = projection.sources["v2_kcs_export_clearance"]
    import_source = projection.sources["v2_kcs_import_clearance"]

    assert export_source.official_uri.startswith("https://www2.customs.go.kr/")
    assert export_source.scope_directions[("customs_clearance", "export_clearance_flow")] == {
        "export"
    }
    assert import_source.scope_directions[("customs_clearance", "import_clearance_flow")] == {
        "import"
    }


def test_v2_registry_source_id_is_preserved_only_in_internal_postgres_state() -> None:
    """A restart keeps validated provenance without widening Browser output."""

    record = ConversationMessageRecord(
        message_id="msg-v2-provenance",
        conversation_id="conversation-v2-provenance",
        role=ConversationMessageRole.assistant,
        content="공식 수출 통관 안내입니다.",
        answer_scope=AnswerScope.general_customs_guidance,
        citations=[
            MessageCitation(
                kind=CitationKind.guidance_evidence,
                source_id="record-v2-export-clearance",
                registry_source_id="v2_kcs_export_clearance",
                source_kind="official_agency_guidance",
                source_title="수출 통관",
                source_uri=(
                    "https://www2.customs.go.kr/kcs/cm/cntnts/cntntsView.do?cntntsId=818&mi=2820"
                ),
                as_of="2026-08-05",
                snapshot_id="kr-trade-v2-2026-08-05",
                coverage="covered",
            )
        ],
        created_by="customs-ai-agent",
        created_at=datetime(2026, 8, 5, tzinfo=UTC),
    )

    public_value = record.model_dump(mode="json")
    persisted_value = _message_persistence_dump(record)

    assert "registry_source_id" not in public_value["citations"][0]
    assert persisted_value["citations"][0]["registry_source_id"] == "v2_kcs_export_clearance"
    restored = ConversationMessageRecord.model_validate(persisted_value)
    assert restored.citations[0].registry_source_id == "v2_kcs_export_clearance"


def test_live_official_citation_cannot_bypass_v1_v2_registry_hash_binding() -> None:
    """Only the deterministic local fixture may omit an immutable registry digest."""

    citation = MessageCitation(
        kind=CitationKind.guidance_evidence,
        source_id="unbound-official-record",
        source_kind="official_agency_guidance",
        source_title="수출 통관",
        source_uri="https://www2.customs.go.kr/kcs/cm/cntnts/cntntsView.do?cntntsId=818&mi=2820",
        as_of="2026-08-05",
        snapshot_id="kr-trade-v2-2026-08-05",
        coverage="covered",
    )

    with pytest.raises(ConflictError, match="registry provenance"):
        _validate_guidance_citations(
            [citation],
            expected_as_of=date(2026, 8, 5),
            functional_uat=False,
        )
