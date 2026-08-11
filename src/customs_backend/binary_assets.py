"""Fail-closed inspection for the limited binary extraction-preview boundary.

The Backend accepts only PDF, JPEG, and PNG source bytes.  This module never
stores, logs, or serializes those bytes; it returns compact metadata after
magic-byte and decoder validation.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import Literal
import warnings

from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader

from customs_backend.domain import (
    DocumentAssetMediaType,
    MAX_DOCUMENT_ASSET_BYTES,
    MAX_DOCUMENT_ASSET_PAGES,
    MAX_DOCUMENT_ASSET_PIXELS,
)


@dataclass(frozen=True)
class BinaryAssetInspection:
    media_type: DocumentAssetMediaType
    byte_size: int
    page_count: int
    pixel_count: int


def _detected_media_type(payload: bytes) -> DocumentAssetMediaType:
    if payload.startswith(b"%PDF-"):
        return "application/pdf"
    if payload.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    raise ValueError("document asset type is not supported")


def _inspect_pdf(payload: bytes) -> tuple[int, int]:
    try:
        reader = PdfReader(BytesIO(payload), strict=True)
        if reader.is_encrypted:
            raise ValueError("document asset PDF is encrypted")
        pages = len(reader.pages)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("document asset PDF cannot be decoded") from exc
    if not 1 <= pages <= MAX_DOCUMENT_ASSET_PAGES:
        raise ValueError("document asset PDF page count is outside the preview limit")
    # Rasterization happens only in Agent's explicitly configured local
    # preview engine.  The Backend has no pixel claim for a vector PDF.
    return pages, 0


def _inspect_image(payload: bytes, expected: Literal["image/jpeg", "image/png"]) -> tuple[int, int]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(payload)) as verified:
                verified.verify()
            with Image.open(BytesIO(payload)) as image:
                actual = (
                    "image/jpeg"
                    if image.format == "JPEG"
                    else "image/png"
                    if image.format == "PNG"
                    else None
                )
                if actual != expected:
                    raise ValueError("document asset image format differs from its magic type")
                if getattr(image, "n_frames", 1) != 1:
                    raise ValueError("animated image input is not supported")
                width, height = image.size
    except ValueError:
        raise
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError, SyntaxError) as exc:
        raise ValueError("document asset image cannot be decoded safely") from exc
    if width < 1 or height < 1:
        raise ValueError("document asset image dimensions are invalid")
    pixels = width * height
    if pixels > MAX_DOCUMENT_ASSET_PIXELS:
        raise ValueError("document asset image exceeds the preview pixel limit")
    return 1, pixels


def inspect_binary_asset(
    *, payload: bytes, declared_media_type: str
) -> BinaryAssetInspection:
    """Validate one bounded binary upload without trusting its filename/header."""

    if not payload or len(payload) > MAX_DOCUMENT_ASSET_BYTES:
        raise ValueError("document asset exceeds the preview byte limit")
    detected = _detected_media_type(payload)
    if declared_media_type.casefold() != detected:
        raise ValueError("document asset content type does not match its bytes")
    if detected == "application/pdf":
        pages, pixels = _inspect_pdf(payload)
    else:
        pages, pixels = _inspect_image(payload, detected)
    return BinaryAssetInspection(
        media_type=detected,
        byte_size=len(payload),
        page_count=pages,
        pixel_count=pixels,
    )
