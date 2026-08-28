"""Backend's fail-closed public projection of Data guidance registries.

Data owns source ingestion and immutable snapshot verification.  Backend ships
only exact, Git-safe registry-manifest copies so it can reject an Agent answer
that tries to expose an unregistered source, altered official URL, or scope
outside the declared Korean trade coverage.  No source body, receipt, hash of
raw material, or local path is loaded here.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
from importlib import resources
from typing import Literal
from urllib.parse import unquote, urlsplit


GuidanceRegistryVersion = Literal["1.0", "2.0"]
GuidanceDirection = Literal["import", "export"]
GuidanceRequestedDirection = Literal["import", "export", "both"]

_VERSIONS: tuple[GuidanceRegistryVersion, ...] = ("1.0", "2.0")
_MANIFEST_NAMES: dict[GuidanceRegistryVersion, str] = {
    "1.0": "guidance_registry_v1.manifest.json",
    "2.0": "guidance_registry_v2.manifest.json",
}
# These are deliberately pinned in Backend in addition to the cross-repo
# exact-byte harness mapping.  ``registry_sha256`` is Data's immutable
# registry digest; the second value is the SHA-256 of the safe manifest bytes
# copied into this package.  Neither value identifies or contains a source
# body, but both make an accidental version/manifest substitution fail closed.
_EXPECTED_REGISTRY_SHA256: dict[GuidanceRegistryVersion, str] = {
    "1.0": "6fed779ee0ec1cd2358972925fe1b2788838a197d4f33e62e461fcfed2bb7d6c",
    "2.0": "a450ae6286d9a12729c5459543a35fd9c1c3df2ff59977700f72887c3df6543e",
}
_EXPECTED_MANIFEST_SHA256: dict[GuidanceRegistryVersion, str] = {
    "1.0": "da3aa6e3644012d55bebb5ae1bbc2d2f052a68d0b3a3c14aafab56a677192092",
    "2.0": "51375a77351e73a32763e65679099d9bf04190078956e28ed1161b3ec5e1b7bb",
}
_SOURCE_KINDS = frozenset(
    {
        "official_law",
        "official_law_catalog",
        "official_agency_guidance",
        "official_requirement_reference",
        "official_public_guidance",
        "development_fixture",
    }
)
_DIRECTIONS = frozenset({"import", "export"})
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


class GuidanceRegistryProjectionError(ValueError):
    """A packaged registry or Agent-provided provenance is invalid."""


@dataclass(frozen=True)
class RegistryScope:
    topic: str
    scope: str
    directions: tuple[GuidanceDirection, ...]
    required_source_ids: frozenset[str]


@dataclass(frozen=True)
class RegistrySource:
    source_id: str
    source_kind: str
    title: str
    official_uri: str
    scope_directions: dict[tuple[str, str], frozenset[GuidanceDirection]]


@dataclass(frozen=True)
class GuidanceRegistryProjection:
    version: GuidanceRegistryVersion
    registry_sha256: str
    sources: dict[str, RegistrySource]
    scopes: dict[tuple[str, str], RegistryScope]


def _strict_object(raw: bytes, *, name: str) -> dict[str, object]:
    try:
        decoded = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GuidanceRegistryProjectionError(f"{name} is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise GuidanceRegistryProjectionError(f"{name} must be a JSON object")
    return decoded


def _identifier(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise GuidanceRegistryProjectionError(f"{label} is invalid")
    return value


def _directions(value: object, *, label: str) -> tuple[GuidanceDirection, ...]:
    if not isinstance(value, list) or not value:
        raise GuidanceRegistryProjectionError(f"{label} is invalid")
    if (
        any(not isinstance(item, str) or item not in _DIRECTIONS for item in value)
        or len(value) != len(set(value))
    ):
        raise GuidanceRegistryProjectionError(f"{label} is invalid")
    canonical = tuple(direction for direction in ("import", "export") if direction in value)
    if list(canonical) != value:
        raise GuidanceRegistryProjectionError(f"{label} is not canonical")
    return canonical  # type: ignore[return-value]


def _safe_https_uri(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise GuidanceRegistryProjectionError(f"{label} is invalid")
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise GuidanceRegistryProjectionError(f"{label} is invalid") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or ":" in parsed.netloc.rsplit("@", 1)[-1]
        or parsed.fragment
    ):
        raise GuidanceRegistryProjectionError(f"{label} is invalid")
    return value


def _source_from_v1(
    raw: object,
    *,
    topic_directions: dict[str, tuple[GuidanceDirection, ...]],
) -> RegistrySource:
    if not isinstance(raw, dict):
        raise GuidanceRegistryProjectionError("V1 source is invalid")
    source_id = _identifier(raw.get("source_id"), label="V1 source_id")
    source_kind = raw.get("source_kind")
    title = raw.get("title")
    official_uri = _safe_https_uri(raw.get("official_uri"), label="V1 official_uri")
    topics = raw.get("topics")
    if (
        not isinstance(source_kind, str)
        or source_kind not in _SOURCE_KINDS
        or not isinstance(title, str)
        or not title
        or not isinstance(topics, list)
        or not topics
        or any(not isinstance(topic, str) or topic not in topic_directions for topic in topics)
    ):
        raise GuidanceRegistryProjectionError("V1 source is invalid")
    source_directions = _directions(raw.get("directions"), label="V1 source directions")
    scopes: dict[tuple[str, str], frozenset[GuidanceDirection]] = {}
    for topic in topics:
        shared = tuple(direction for direction in topic_directions[topic] if direction in source_directions)
        if shared:
            scopes[(topic, "legacy_topic")] = frozenset(shared)
    if not scopes:
        raise GuidanceRegistryProjectionError("V1 source has no supported topic")
    return RegistrySource(
        source_id=source_id,
        source_kind=source_kind,
        title=title,
        official_uri=official_uri,
        scope_directions=scopes,
    )


def _load_v1(payload: dict[str, object]) -> GuidanceRegistryProjection:
    if payload.get("schema_version") != "1.0":
        raise GuidanceRegistryProjectionError("V1 registry version is invalid")
    registry_sha256 = payload.get("registry_sha256")
    topics = payload.get("topics")
    sources = payload.get("sources")
    if (
        not isinstance(registry_sha256, str)
        or registry_sha256 != _EXPECTED_REGISTRY_SHA256["1.0"]
        or not isinstance(topics, list)
        or not isinstance(sources, list)
    ):
        raise GuidanceRegistryProjectionError("V1 registry is invalid")
    topic_directions: dict[str, tuple[GuidanceDirection, ...]] = {}
    scopes: dict[tuple[str, str], RegistryScope] = {}
    for raw_topic in topics:
        if not isinstance(raw_topic, dict):
            raise GuidanceRegistryProjectionError("V1 topic is invalid")
        topic = _identifier(raw_topic.get("topic"), label="V1 topic")
        directions = _directions(raw_topic.get("directions"), label="V1 topic directions")
        required_source_ids = raw_topic.get("source_ids")
        if (
            topic in topic_directions
            or not isinstance(required_source_ids, list)
            or not required_source_ids
            or any(
                not isinstance(item, str) or _IDENTIFIER.fullmatch(item) is None
                for item in required_source_ids
            )
            or len(required_source_ids) != len(set(required_source_ids))
        ):
            raise GuidanceRegistryProjectionError("V1 topic is invalid")
        topic_directions[topic] = directions
        scopes[(topic, "legacy_topic")] = RegistryScope(
            topic=topic,
            scope="legacy_topic",
            directions=directions,
            required_source_ids=frozenset(required_source_ids),
        )
    parsed_sources = {
        source.source_id: source
        for source in (_source_from_v1(item, topic_directions=topic_directions) for item in sources)
    }
    if len(parsed_sources) != len(sources):
        raise GuidanceRegistryProjectionError("V1 source identifiers are duplicated")
    for scope in scopes.values():
        if not scope.required_source_ids.issubset(parsed_sources):
            raise GuidanceRegistryProjectionError("V1 topic source is missing")
    return GuidanceRegistryProjection(
        version="1.0",
        registry_sha256=registry_sha256,
        sources=parsed_sources,
        scopes=scopes,
    )


def _load_v2(payload: dict[str, object]) -> GuidanceRegistryProjection:
    if payload.get("schema_version") != "2.0":
        raise GuidanceRegistryProjectionError("V2 registry version is invalid")
    registry_sha256 = payload.get("registry_sha256")
    raw_topics = payload.get("topics")
    raw_sources = payload.get("sources")
    if (
        payload.get("jurisdiction") != "KR"
        or not isinstance(registry_sha256, str)
        or registry_sha256 != _EXPECTED_REGISTRY_SHA256["2.0"]
        or not isinstance(raw_topics, list)
        or not isinstance(raw_sources, list)
    ):
        raise GuidanceRegistryProjectionError("V2 registry is invalid")

    scopes: dict[tuple[str, str], RegistryScope] = {}
    for raw_topic in raw_topics:
        if not isinstance(raw_topic, dict):
            raise GuidanceRegistryProjectionError("V2 topic is invalid")
        topic = _identifier(raw_topic.get("topic"), label="V2 topic")
        scoped_entries = raw_topic.get("scopes")
        if not isinstance(raw_topic.get("title"), str) or not raw_topic["title"]:
            raise GuidanceRegistryProjectionError("V2 topic is invalid")
        if not isinstance(scoped_entries, list) or not scoped_entries:
            raise GuidanceRegistryProjectionError("V2 topic has no scope")
        for raw_scope in scoped_entries:
            if not isinstance(raw_scope, dict):
                raise GuidanceRegistryProjectionError("V2 scope is invalid")
            scope = _identifier(raw_scope.get("scope"), label="V2 scope")
            directions = _directions(raw_scope.get("directions"), label="V2 scope directions")
            required_source_ids = raw_scope.get("required_source_ids")
            key = (topic, scope)
            if (
                key in scopes
                or not isinstance(raw_scope.get("title"), str)
                or not raw_scope["title"]
                or not isinstance(required_source_ids, list)
                or not required_source_ids
                or any(
                    not isinstance(item, str) or _IDENTIFIER.fullmatch(item) is None
                    for item in required_source_ids
                )
                or len(required_source_ids) != len(set(required_source_ids))
            ):
                raise GuidanceRegistryProjectionError("V2 scope is invalid")
            scopes[key] = RegistryScope(
                topic=topic,
                scope=scope,
                directions=directions,
                required_source_ids=frozenset(required_source_ids),
            )

    sources: dict[str, RegistrySource] = {}
    for raw_source in raw_sources:
        if not isinstance(raw_source, dict):
            raise GuidanceRegistryProjectionError("V2 source is invalid")
        source_id = _identifier(raw_source.get("source_id"), label="V2 source_id")
        source_kind = raw_source.get("source_kind")
        title = raw_source.get("title")
        scope_entries = raw_source.get("scopes")
        source_topics = raw_source.get("topics")
        if (
            source_id in sources
            or not source_id.startswith("v2_")
            or raw_source.get("jurisdiction") != "KR"
            or not isinstance(source_kind, str)
            or source_kind not in _SOURCE_KINDS
            or not isinstance(title, str)
            or not title
            or not isinstance(raw_source.get("authority"), str)
            or not raw_source["authority"]
            or raw_source.get("availability") != "requires_kr_trade_snapshot"
            or not isinstance(source_topics, list)
            or not source_topics
            or any(
                not isinstance(topic, str) or _IDENTIFIER.fullmatch(topic) is None
                for topic in source_topics
            )
            or len(source_topics) != len(set(source_topics))
            or not isinstance(scope_entries, list)
            or not scope_entries
        ):
            raise GuidanceRegistryProjectionError("V2 source is invalid")
        # Validate the source's broad directions even though fine-grained
        # scope directions are what governs a concrete answer.
        source_directions = _directions(raw_source.get("directions"), label="V2 source directions")
        scope_directions: dict[tuple[str, str], frozenset[GuidanceDirection]] = {}
        for raw_scope in scope_entries:
            if not isinstance(raw_scope, dict):
                raise GuidanceRegistryProjectionError("V2 source scope is invalid")
            topic = _identifier(raw_scope.get("topic"), label="V2 source scope topic")
            scope = _identifier(raw_scope.get("scope"), label="V2 source scope")
            directions = _directions(
                raw_scope.get("directions"), label="V2 source scope directions"
            )
            key = (topic, scope)
            if key not in scopes or key in scope_directions:
                raise GuidanceRegistryProjectionError("V2 source scope is invalid")
            if not set(directions).issubset(scopes[key].directions):
                raise GuidanceRegistryProjectionError("V2 source scope direction is invalid")
            if not set(directions).issubset(source_directions):
                raise GuidanceRegistryProjectionError("V2 source direction does not match its scope")
            scope_directions[key] = frozenset(directions)
        if set(source_topics) != {topic for topic, _scope in scope_directions}:
            raise GuidanceRegistryProjectionError("V2 source topics do not match its scopes")
        sources[source_id] = RegistrySource(
            source_id=source_id,
            source_kind=source_kind,
            title=title,
            official_uri=_safe_https_uri(raw_source.get("official_uri"), label="V2 official_uri"),
            scope_directions=scope_directions,
        )
    for scope_entry in scopes.values():
        if not scope_entry.required_source_ids.issubset(sources):
            raise GuidanceRegistryProjectionError("V2 required source is missing")
        if any(
            not sources[source_id].scope_directions.get((scope_entry.topic, scope_entry.scope))
            for source_id in scope_entry.required_source_ids
        ):
            raise GuidanceRegistryProjectionError("V2 required source scope is missing")
    return GuidanceRegistryProjection(
        version="2.0",
        registry_sha256=registry_sha256,
        sources=sources,
        scopes=scopes,
    )


@lru_cache(maxsize=2)
def guidance_registry_projection(version: GuidanceRegistryVersion) -> GuidanceRegistryProjection:
    """Load exactly one packaged public registry projection."""

    if version not in _VERSIONS:
        raise GuidanceRegistryProjectionError("guidance registry version is unsupported")
    name = _MANIFEST_NAMES[version]
    try:
        raw = resources.files("customs_backend").joinpath("contracts", "evidence", "v3", name).read_bytes()
    except OSError as exc:
        raise GuidanceRegistryProjectionError("guidance registry manifest is missing") from exc
    if sha256(raw).hexdigest() != _EXPECTED_MANIFEST_SHA256[version]:
        raise GuidanceRegistryProjectionError("guidance registry manifest digest is invalid")
    payload = _strict_object(raw, name=name)
    return _load_v1(payload) if version == "1.0" else _load_v2(payload)


def validate_guidance_resolution(
    *,
    registry_version: GuidanceRegistryVersion,
    topic: str,
    scope: str,
    requested_direction: GuidanceRequestedDirection,
    applicable_directions: list[GuidanceDirection],
    coverage: str,
) -> GuidanceRegistryProjection:
    """Verify that a public resolution exactly fits the packaged projection."""

    registry = guidance_registry_projection(registry_version)
    expected_scope = registry.scopes.get((topic, scope))
    if expected_scope is None or list(expected_scope.directions) != applicable_directions:
        raise GuidanceRegistryProjectionError("guidance resolution is outside registry coverage")
    if (
        requested_direction != "both"
        and requested_direction not in expected_scope.directions
        and coverage in {"covered", "partial"}
    ):
        raise GuidanceRegistryProjectionError("grounded guidance direction is not applicable")
    return registry


def _v1_law_uri_matches(uri: str, source: RegistrySource) -> bool:
    """Retain the legacy article citation form while V2 uses exact page URLs."""

    if uri == source.official_uri:
        return True
    try:
        parsed = urlsplit(uri)
    except ValueError:
        return False
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.hostname.casefold().removeprefix("www.") != "law.go.kr"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or ":" in parsed.netloc.rsplit("@", 1)[-1]
        or parsed.query
        or parsed.fragment
    ):
        return False
    segments = [segment for segment in unquote(parsed.path).split("/") if segment]
    return len(segments) == 3 and segments[:2] == ["법령", source.title]


def validate_guidance_citation(
    *,
    registry: GuidanceRegistryProjection,
    registry_source_id: str,
    source_kind: str,
    source_title: str,
    source_uri: str,
    topic: str,
    scope: str,
    requested_direction: GuidanceRequestedDirection,
    applicable_directions: list[GuidanceDirection],
) -> None:
    """Verify one Agent citation cannot alter the approved registry source."""

    source = registry.sources.get(registry_source_id)
    source_directions = source.scope_directions.get((topic, scope)) if source else None
    required_directions = (
        set(applicable_directions)
        if requested_direction == "both"
        else {requested_direction}
    )
    if (
        source is None
        or source.source_kind != source_kind
        or source.title != source_title
        or source_directions is None
        or not required_directions.issubset(source_directions)
    ):
        raise GuidanceRegistryProjectionError("guidance citation is outside registry coverage")
    if registry.version == "1.0" and source.source_kind == "official_law":
        if not _v1_law_uri_matches(source_uri, source):
            raise GuidanceRegistryProjectionError("legacy law citation URI is invalid")
    elif source_uri != source.official_uri:
        raise GuidanceRegistryProjectionError("guidance citation URI does not match registry")


def validate_development_fixture_citation(
    *,
    registry: GuidanceRegistryProjection,
    registry_source_id: str,
    source_uri: str,
    topic: str,
    scope: str,
    requested_direction: GuidanceRequestedDirection,
    applicable_directions: list[GuidanceDirection],
) -> None:
    """Verify a local deterministic fixture citation keeps its registry binding.

    The Agent's registry check emits development-fixture guidance with a real
    registry source binding but a clearly synthetic fixed URI, so the official
    title/URI equality of ``validate_guidance_citation`` can never hold for it.
    The registry source and its (topic, scope, direction) support must still
    hold; only the official title/URI comparison is replaced by the exact
    fixed fixture URI.  Functional-UAT rejects this source kind separately.
    """

    source = registry.sources.get(registry_source_id)
    source_directions = source.scope_directions.get((topic, scope)) if source else None
    required_directions = (
        set(applicable_directions)
        if requested_direction == "both"
        else {requested_direction}
    )
    expected_uri = (
        f"https://customs.local.invalid/dev-fixture/guidance/{topic}"
        if registry.version == "1.0"
        else f"https://customs.local.invalid/dev-fixture/guidance/{topic}/{scope}"
    )
    if (
        source is None
        or source_directions is None
        or not required_directions.issubset(source_directions)
        or source_uri != expected_uri
    ):
        raise GuidanceRegistryProjectionError(
            "development fixture citation is outside registry coverage"
        )


def verify_guidance_registry_projection_bundle() -> None:
    """Fail closed when either checked-in V1/V2 public projection drifts."""

    for version in _VERSIONS:
        guidance_registry_projection(version)


__all__ = [
    "GuidanceRegistryProjection",
    "GuidanceRegistryProjectionError",
    "guidance_registry_projection",
    "validate_development_fixture_citation",
    "validate_guidance_citation",
    "validate_guidance_resolution",
    "verify_guidance_registry_projection_bundle",
]
