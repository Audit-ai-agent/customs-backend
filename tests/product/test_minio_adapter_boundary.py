from __future__ import annotations

import hashlib
import sys
from types import ModuleType, SimpleNamespace

import pytest

from customs_backend.adapters import MinioObjectStore
from customs_backend.domain import MAX_TEXT_DOCUMENT_UTF8_BYTES


class _FakeS3Error(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _install_fake_minio(monkeypatch: pytest.MonkeyPatch, *, make_error: str) -> None:
    class FakeMinio:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def bucket_exists(self, _bucket: str) -> bool:
            return False

        def make_bucket(self, _bucket: str) -> None:
            raise _FakeS3Error(make_error)

    package = ModuleType("minio")
    package.Minio = FakeMinio  # type: ignore[attr-defined]
    error_module = ModuleType("minio.error")
    error_module.S3Error = _FakeS3Error  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "minio", package)
    monkeypatch.setitem(sys.modules, "minio.error", error_module)


def test_parallel_start_accepts_only_already_owned_bucket_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_minio(monkeypatch, make_error="BucketAlreadyOwnedByYou")
    MinioObjectStore(
        endpoint="minio:9000",
        access_key="access",
        secret_key="secret",
        bucket="customs-documents",
        secure=False,
    )


def test_minio_configuration_error_is_not_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_minio(monkeypatch, make_error="AccessDenied")
    with pytest.raises(_FakeS3Error, match="AccessDenied"):
        MinioObjectStore(
            endpoint="minio:9000",
            access_key="access",
            secret_key="secret",
            bucket="customs-documents",
            secure=False,
        )


def test_minio_read_is_bounded_before_hash_or_decode() -> None:
    class Response:
        headers: dict[str, str] = {}
        closed = False
        released = False

        def read(self, amount: int) -> bytes:
            assert amount == MAX_TEXT_DOCUMENT_UTF8_BYTES + 1
            return b"x" * amount

        def close(self) -> None:
            self.closed = True

        def release_conn(self) -> None:
            self.released = True

    response = Response()
    store = object.__new__(MinioObjectStore)
    store._bucket = "customs-documents"  # type: ignore[attr-defined]
    store._client = SimpleNamespace(get_object=lambda _bucket, _key: response)  # type: ignore[attr-defined]
    with pytest.raises(ValueError, match="byte safety limit"):
        store.get_text(key="case/document", expected_sha256="0" * 64)
    assert response.closed is True
    assert response.released is True


def test_minio_bounded_read_accepts_hash_bound_utf8() -> None:
    payload = "통관 문서".encode()

    class Response:
        headers = {"content-length": str(len(payload))}

        def read(self, amount: int) -> bytes:
            assert amount == MAX_TEXT_DOCUMENT_UTF8_BYTES + 1
            return payload

        def close(self) -> None:
            pass

        def release_conn(self) -> None:
            pass

    store = object.__new__(MinioObjectStore)
    store._bucket = "customs-documents"  # type: ignore[attr-defined]
    store._client = SimpleNamespace(get_object=lambda _bucket, _key: Response())  # type: ignore[attr-defined]
    assert (
        store.get_text(
            key="case/document",
            expected_sha256=hashlib.sha256(payload).hexdigest(),
        )
        == "통관 문서"
    )
