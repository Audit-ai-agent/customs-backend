from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image
from pypdf import PdfWriter

from customs_backend.binary_assets import inspect_binary_asset


def _pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _image(image_format: str) -> bytes:
    output = BytesIO()
    Image.new("RGB", (2, 3), color=(0, 0, 0)).save(output, format=image_format)
    return output.getvalue()


def test_pdf_magic_and_page_metadata_are_verified_without_text_extraction() -> None:
    inspected = inspect_binary_asset(payload=_pdf(), declared_media_type="application/pdf")
    assert inspected.media_type == "application/pdf"
    assert inspected.page_count == 1
    assert inspected.pixel_count == 0


@pytest.mark.parametrize(
    ("image_format", "media_type"), [("JPEG", "image/jpeg"), ("PNG", "image/png")]
)
def test_supported_image_magic_and_decoder_metadata_are_verified(
    image_format: str, media_type: str
) -> None:
    inspected = inspect_binary_asset(payload=_image(image_format), declared_media_type=media_type)
    assert inspected.media_type == media_type
    assert inspected.page_count == 1
    assert inspected.pixel_count == 6


def test_media_parameters_and_magic_mismatches_are_rejected() -> None:
    with pytest.raises(ValueError, match="content type"):
        inspect_binary_asset(payload=_pdf(), declared_media_type="application/pdf; charset=binary")
    with pytest.raises(ValueError, match="content type"):
        inspect_binary_asset(payload=_image("PNG"), declared_media_type="image/jpeg")
