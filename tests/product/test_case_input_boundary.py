"""Regression coverage for the Case document-text forwarding boundary."""

from __future__ import annotations

import pytest

from customs_backend.case_input_boundary import case_input_notice


@pytest.mark.parametrize(
    "agent_field_label",
    [
        "Invoice No",
        "Invoice Number",
        "Packing No",
        "Packing Number",
        "B/L",
        "B/L No",
        "AWB",
        "AWB No",
        "HS Code",
        "Description",
        "Quantity",
        "Unit Price",
        "Amount",
        "Total",
        "Total Amount",
        "Country of Origin",
        "Submission No",
        "Submission Date",
        "Submission Status",
        "신고번호",
        "접수번호",
        "제출일",
        "처리상태",
        "품명",
        "수량",
        "단가",
        "금액",
        "총액",
        "원산지",
        "HS 코드",
    ],
)
def test_case_input_boundary_covers_every_agent_document_field_label(
    agent_field_label: str,
) -> None:
    """Backend must stop every two-row record the Agent would stop itself."""

    assert (
        case_input_notice(f"{agent_field_label}: 예시값\n수량: 1")
        == "unregistered_document_text"
    )


def test_case_input_boundary_stops_korean_product_and_unit_price_record() -> None:
    """Regression: this formerly reached Agent/Functional-UAT transcript."""

    assert case_input_notice("품명: 냉동 고등어\n단가: USD 10") == "unregistered_document_text"


@pytest.mark.parametrize(
    "content",
    [
        "품명: 냉동 고등어",
        "단가: USD 10",
        "INV-001",
        "현재 사건의 수량 차이를 설명해 주세요.\n어떤 문서를 확인해야 하나요?",
    ],
)
def test_case_input_boundary_keeps_short_identifiers_and_business_questions(content: str) -> None:
    assert case_input_notice(content) is None


def test_case_input_boundary_covers_agent_heading_and_table_forms() -> None:
    assert case_input_notice("Invoice\nDescription: 냉동 고등어") == "unregistered_document_text"
    assert case_input_notice("항목|값\n---\n품명|냉동 고등어\n수량|10") == "unregistered_document_text"
