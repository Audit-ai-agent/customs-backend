from __future__ import annotations

from pathlib import Path

import pytest

from tools.verify_production_deployment_inputs import validate_environment

ROOT = Path(__file__).resolve().parents[2]


def test_backend_runtime_image_is_digest_pinned_minimal_alpine() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/unit.yml").read_text(encoding="utf-8")
    exact_base = (
        "python:3.11.15-alpine3.24@sha256:"
        "25976e9d34a0fab1f278cae931f34c8303d97bf0c0d7f85b6b4dcf641d7702a4"
    )
    runtime = dockerfile.split(f"FROM {exact_base} AS runtime", 1)[1]

    assert dockerfile.count(f"FROM {exact_base}") == 2
    assert "uv sync --frozen --no-dev --extra production --no-editable" in dockerfile
    assert "COPY --from=build --chown=10001:10001 /app/.venv /app/.venv" in runtime
    assert (
        "COPY --from=build --chown=10001:10001 "
        "/app/packages/operational_db/migrations "
        "/app/packages/operational_db/migrations"
    ) in runtime
    assert "COPY --from=uv" not in runtime
    assert "/usr/local/lib/python3.11/site-packages/*" in runtime
    assert "/usr/local/lib/python3.11/ensurepip" in runtime
    assert "USER 10001:10001" in runtime
    assert "docker build --pull --tag customs-backend:ci ." in workflow
    assert (
        "aquasec/trivy@sha256:086971aaf400beebd94e8300fd8ea623774419597169156cec56eec5b00dfb1e"
    ) in workflow
    assert "--exit-code 1 --severity HIGH,CRITICAL" in workflow
    assert "ignore-unfixed" not in workflow


def test_agent_has_only_allowlisted_proxy_egress() -> None:
    compose = (ROOT / "compose.production.yaml").read_text(encoding="utf-8")
    agent = compose.split("\n  agent:\n", 1)[1].split("\n  egress-proxy:\n", 1)[0]
    proxy = compose.split("\n  egress-proxy:\n", 1)[1].split("\n  data:\n", 1)[0]
    policy = (ROOT / "deploy" / "anthropic-egress-squid.conf").read_text(encoding="utf-8")

    assert "networks: [agent-api, evidence-api, agent-proxy]" in agent
    assert "proxy-egress" not in agent
    assert "HTTPS_PROXY: http://egress-proxy:3128" in agent
    assert "NO_PROXY: evidence-proxy,127.0.0.1,localhost" in agent
    assert "networks: [agent-proxy, proxy-egress]" in proxy
    assert "read_only: true" in proxy
    assert "cap_drop: [ALL]" in proxy
    assert "security_opt: [no-new-privileges:true]" in proxy
    assert "/var/spool/squid" in proxy
    assert "acl ANTHROPIC_API dstdomain api.anthropic.com" in policy
    assert policy.index("http_access allow CONNECT ANTHROPIC_API") < policy.index(
        "http_access deny all"
    )
    assert "http_access allow all" not in policy
    assert "agent-egress" not in compose
    assert "edge:\n    internal: true" in compose
    assert "proxy-egress: {}" in compose


def test_data_runs_v3_behind_internal_tls_with_external_signed_release_inputs() -> None:
    compose = (ROOT / "compose.production.yaml").read_text(encoding="utf-8")
    data = compose.split("\n  data:\n", 1)[1].split("\nnetworks:\n", 1)[0]
    evidence_proxy = compose.split("\n  evidence-proxy:\n", 1)[1].split("\n  egress-proxy:\n", 1)[0]
    agent = compose.split("\n  agent:\n", 1)[1].split("\n  evidence-proxy:\n", 1)[0]
    proxy_policy = (ROOT / "deploy/evidence-nginx.conf").read_text(encoding="utf-8")

    assert 'command: ["python", "-m", "src.production_v3"]' in data
    assert "EVIDENCE_TRANSPORT_MODE: trusted_proxy_tls" in data
    assert "EVIDENCE_TRUSTED_PROXY_CIDRS: 127.0.0.1/32,172.31.252.0/29" in data
    assert "source: ${EVIDENCE_REQUIREMENT_POLICY_RELEASE_FILE:?required}" in data
    assert "target: /run/evidence-release/requirement-policy.json" in data
    assert "source: ${EVIDENCE_TRUSTED_REVIEWERS_FILE:?required}" in data
    assert "target: /run/evidence-release/trusted-reviewers.json" in data
    assert data.count("read_only: true") >= 3
    assert "networks: [evidence-api, data-store]" not in data
    assert "evidence-ingress:" in data
    assert "data-store-tls: {}" in data
    assert "https://evidence-proxy:8443" in agent
    assert "SSL_CERT_FILE: /run/customs-ca/ca-bundle.pem" in agent
    assert "source: ${CUSTOMS_AGENT_CA_BUNDLE_FILE:?required}" in agent
    assert "networks:" in evidence_proxy
    assert "evidence-api: {}" in evidence_proxy
    assert "evidence-ingress:" in evidence_proxy
    assert "read_only: true" in evidence_proxy
    assert "cap_drop: [ALL]" in evidence_proxy
    assert "listen 8443 ssl" in proxy_policy
    assert "proxy_pass http://data:8010" in proxy_policy
    assert "proxy_set_header X-Forwarded-Proto https" in proxy_policy
    assert "location /internal/rag/v3/" in proxy_policy


def test_compose_uses_liveness_then_authenticated_agent_readiness_and_exact_images() -> None:
    compose = (ROOT / "compose.production.yaml").read_text(encoding="utf-8")
    data = compose.split("\n  data:\n", 1)[1].split("\nnetworks:\n", 1)[0]
    agent = compose.split("\n  agent:\n", 1)[1].split("\n  egress-proxy:\n", 1)[0]

    assert "/internal/rag/v3/health/live" in data
    assert "/internal/rag/v3/health/ready" not in data
    assert "/health/ready" in agent
    assert "\n  knowledge-db:" not in compose
    assert "\n  qdrant:" not in compose
    assert "\n  neo4j:" not in compose
    assert "data-store-tls:\n    external: true" in compose
    assert "CUSTOMS_DATA_STORE_TLS_NETWORK" in compose
    assert "image: postgres:" not in compose
    assert "image: nginx:" not in compose
    assert "image: redis:" not in compose
    assert "image: qdrant/" not in compose
    assert "image: neo4j:" not in compose
    assert "EGRESS_PROXY_IMAGE" in compose


def test_backend_and_healthcheck_match_agent_provider_container_port() -> None:
    compose = (ROOT / "compose.production.yaml").read_text(encoding="utf-8")
    settings = (ROOT / "src/customs_backend/settings.py").read_text(encoding="utf-8")
    example = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "http://127.0.0.1:8080/health/ready" in compose
    assert "127.0.0.1:8020" not in compose
    assert 'agent_base_url: str = "http://127.0.0.1:8080"' in settings
    assert "CUSTOMS_BACKEND_AGENT_BASE_URL=http://agent:8080" in example
    assert "agent:8020" not in example


def test_release_manifest_is_mounted_read_only_and_backend_rootfs_is_read_only() -> None:
    compose = (ROOT / "compose.production.yaml").read_text(encoding="utf-8")
    assert compose.count("target: /run/customs-releases") == 2
    assert compose.count("target: /run/customs-releases\n        read_only: true") == 2
    for service_name, following_service in [
        ("backend-migrate", "backend"),
        ("backend", "worker"),
        ("worker", "outbox-dispatcher"),
        ("outbox-dispatcher", "keycloak"),
    ]:
        block = compose.split(f"\n  {service_name}:\n", 1)[1].split(
            f"\n  {following_service}:\n", 1
        )[0]
        assert "read_only: true" in block
        assert "cap_drop: [ALL]" in block
        assert "security_opt: [no-new-privileges:true]" in block


def test_runtime_database_role_is_reconciled_for_existing_volumes() -> None:
    compose = (ROOT / "compose.production.yaml").read_text(encoding="utf-8")
    script = (ROOT / "deploy/postgres/reconcile-runtime-role.sh").read_text(encoding="utf-8")
    database = compose.split("\n  operations-db:\n", 1)[1].split(
        "\n  operations-db-role-reconcile:\n", 1
    )[0]
    reconcile = compose.split("\n  operations-db-role-reconcile:\n", 1)[1].split("\n  redis:\n", 1)[
        0
    ]
    migration = compose.split("\n  backend-migrate:\n", 1)[1].split("\n  backend:\n", 1)[0]
    assert "/docker-entrypoint-initdb.d" not in database
    assert "CUSTOMS_RUNTIME_DB_PASSWORD" not in database
    assert "ALTER ROLE customs_runtime LOGIN PASSWORD" in script
    assert "source: ./deploy/postgres/reconcile-runtime-role.sh" in reconcile
    assert "read_only: true" in reconcile
    assert "operations-db-role-reconcile:" in migration
    assert "condition: service_completed_successfully" in migration


def test_candidate_processes_start_but_ingress_waits_for_full_release_readiness() -> None:
    compose = (ROOT / "compose.production.yaml").read_text(encoding="utf-8")
    backend = compose.split("\n  backend:\n", 1)[1].split("\n  worker:\n", 1)[0]
    worker = compose.split("\n  worker:\n", 1)[1].split("\n  outbox-dispatcher:\n", 1)[0]
    outbox = compose.split("\n  outbox-dispatcher:\n", 1)[1].split("\n  keycloak:\n", 1)[0]
    reverse_proxy = compose.split("\n  reverse-proxy:\n", 1)[1].split("\n  frontend:\n", 1)[0]
    frontend = compose.split("\n  frontend:\n", 1)[1].split("\n  backend-migrate:\n", 1)[0]
    assert "agent:\n        # Candidate control-plane" in backend
    assert "condition: service_started" in backend
    assert "agent:\n        condition: service_healthy" in worker
    assert "CUSTOMS_BACKEND_OUTBOX_ENV_FILE" in outbox
    assert "agent:" not in outbox
    assert "CUSTOMS_RELEASE_DIR" not in outbox
    assert "backend:\n        condition: service_healthy" in reverse_proxy
    for block in (reverse_proxy, frontend):
        assert "read_only: true" in block
        assert "cap_drop: [ALL]" in block
        assert "security_opt: [no-new-privileges:true]" in block
    assert "tmpfs:" in reverse_proxy
    assert "tmpfs:" in frontend


def test_environment_example_pins_the_reviewed_data_provider_build() -> None:
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for expected in (
        "CUSTOMS_BACKEND_AGENT_EXPECTED_EVIDENCE_PROVIDER_RELEASE=evidence-api-v1.2.0",
        "CUSTOMS_BACKEND_AGENT_EXPECTED_EVIDENCE_PROVIDER_REVISION="
        "14cde864425eee615a2e3f6799f6f2719448aab9",
        "CUSTOMS_BACKEND_AGENT_EXPECTED_EVIDENCE_PROVIDER_BUILD_SHA256="
        "d7d64cf3df464cfc934ca0f347c1b1c461916fe12671eeeaf59c1445872486d3",
        "CUSTOMS_BACKEND_AGENT_EXPECTED_EVIDENCE_PROVIDER_SOURCE_MANIFEST_SHA256="
        "dd8ceb68c2da83d1fdb82f3d00f4f1137a559fcb577efdf86b6f87e52d84db1c",
        "CUSTOMS_BACKEND_AGENT_EXPECTED_EVIDENCE_PROVIDER_RETRIEVAL_SCHEMA_SHA256="
        "531b59ba4e86f78cef20a4fca052996efd8b88c48853ac12113ca76e69cdd69a",
        "CUSTOMS_BACKEND_AGENT_EXPECTED_EVIDENCE_PROVIDER_READINESS_SCHEMA_SHA256="
        "0a0b50fef627ecbbad954af5878ccb3630ecc51f618132f8cfc674c324467a69",
    ):
        assert expected in example


def test_environment_example_pins_the_final_agent_candidate_contracts() -> None:
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for expected in (
        "CUSTOMS_BACKEND_AGENT_EXPECTED_BUILD_SHA=063a168b4aabd502d372b368e61478496d83c41c",
        "CUSTOMS_BACKEND_AGENT_EXPECTED_WHEEL_SHA256="
        "12c100b4b3da46a275f5e36fdd094d3c2e203b780a7bc3296f0cedc7d98b79e2",
        "CUSTOMS_BACKEND_AGENT_EXPECTED_SOURCE_SNAPSHOT_SHA256="
        "594cdfd290258113915fb0a35092a521da5f51aebcb1e34ba247b6b44fdad878",
        "CUSTOMS_BACKEND_AGENT_EXPECTED_PACKAGE_VERSION=0.3.0",
        "CUSTOMS_BACKEND_AGENT_EXPECTED_CODE_SNAPSHOT_SHA256="
        "564aa75f569cd762df2a325671392991f74fdbc95df631ceeba45e7db38dd31e",
        "CUSTOMS_BACKEND_AGENT_EXPECTED_MODEL_ID=claude-sonnet-5",
        "CUSTOMS_BACKEND_AGENT_EXPECTED_EXTRACTION_PROMPT_SHA256="
        "93c7ba900a631f82473c9d480360404c2f93442ecfd2e48a110a1519f0262cb5",
        "CUSTOMS_BACKEND_AGENT_EXPECTED_EXTRACTION_SCHEMA_SHA256="
        "049d9c0a4782945f6b6716d1f9d8ad02a407e88ce7fa0beaa2e2cd5575312b5c",
        "CUSTOMS_BACKEND_AGENT_EXPECTED_COORDINATOR_PROMPT_SHA256="
        "2efae54814785bf1c55520214fb837c0d1de3f3a5df6764b38bd441f19d05c3e",
        "CUSTOMS_BACKEND_AGENT_EXPECTED_NARRATIVE_PROMPT_SHA256="
        "19c86ef071ceaa22e52c009f956e273532d4f2646a53d9fbbaf9b0bdeec0e742",
        "CUSTOMS_BACKEND_AGENT_EXPECTED_CHAT_PROMPT_SHA256="
        "a59aa371794647f7edab3f108dbe5ad8daeb548ce0b54cce690a31568d850ada",
        "CUSTOMS_BACKEND_AGENT_EXPECTED_COORDINATOR_SCHEMA_SHA256="
        "4881321cd51185d301afda78be6679f5bb0c407e8e9a46699f33432de8b594dd",
        "CUSTOMS_BACKEND_AGENT_EXPECTED_NARRATIVE_SCHEMA_SHA256="
        "6b4ec0e2b26926e581643fa2005f5e42baaf9e1ce57a9740d51dcfd0bcce58fd",
        "CUSTOMS_BACKEND_AGENT_EXPECTED_CHAT_SCHEMA_SHA256="
        "683b7e7340d7c9b2f5406a54e71b903301895ee5ec246542c5ceecc4c8bbceb0",
        "CUSTOMS_BACKEND_AGENT_EXPECTED_POLICY_ID=bounded-adaptive-customs-review/v1",
        "CUSTOMS_BACKEND_AGENT_EXPECTED_TOOL_REGISTRY_ID=customs-agent-tools/text-v1",
        "CUSTOMS_BACKEND_AGENT_EXPECTED_ASSERTION_TRUST_STORE_RELEASE_STATE=unreleased",
        "CUSTOMS_BACKEND_AGENT_EXPECTED_ASSERTION_TRUST_STORE_SHA256="
        "ccad11713d4c18b4f19246f32fc68cbd4faba2d66889f9c037845fdf66db3b13",
    ):
        assert expected in example


def test_readme_distinguishes_current_agent_pin_from_legacy_pilot() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    current_section = readme.split("## 시스템 검증", 1)[1].split("legacy/historical pilot 전용", 1)[
        0
    ]
    legacy_section = readme.split("legacy/historical pilot 전용", 1)[1]
    assert "customs-ai-agent==0.3.0" in current_section
    assert "063a168b4aabd502d372b368e61478496d83c41c" in current_section
    assert "12c100b4b3da46a275f5e36fdd094d3c2e203b780a7bc3296f0cedc7d98b79e2" in (current_section)
    assert "customs-ai-agent==0.1.0" not in current_section
    assert "Agent 0.1.0" in legacy_section


def test_oidc_proxy_path_cannot_capture_the_spa_callback() -> None:
    compose = (ROOT / "compose.production.yaml").read_text(encoding="utf-8")
    proxy = (ROOT / "deploy/nginx.conf").read_text(encoding="utf-8")
    realm = (ROOT / "deploy/keycloak/customs-realm.json").read_text(encoding="utf-8")
    example = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "KC_HTTP_RELATIVE_PATH: /oidc" in compose
    assert "location /oidc/" in proxy
    assert "location /auth/" not in proxy
    assert "${CUSTOMS_PUBLIC_ORIGIN}/auth/callback" in realm
    assert "${CUSTOMS_PUBLIC_ORIGIN}/*" not in realm
    assert "https://customs.internal.example/oidc/realms/customs" in example
    assert "http://keycloak:8080/oidc/realms/customs" in example
    assert "location = /auth/callback" in proxy
    assert proxy.count("access_log off;") >= 2


def test_public_proxy_logs_uri_only_and_enforces_modern_tls() -> None:
    proxy = (ROOT / "deploy/nginx.conf").read_text(encoding="utf-8")
    assert '"$request_method $uri $server_protocol"' in proxy
    assert "$request " not in proxy
    assert "$request_uri" not in proxy
    assert "$args" not in proxy
    assert "ssl_protocols TLSv1.2 TLSv1.3;" in proxy
    assert "ssl_session_tickets off;" in proxy
    assert "location /oidc/" in proxy
    assert "location = /auth/callback" in proxy


def test_deployment_input_verifier_rejects_floating_images_and_plaintext_stores() -> None:
    exact = "example.invalid/team/image@sha256:" + "a" * 64
    environment = {
        name: exact
        for name in (
            "AGENT_IMAGE",
            "BACKEND_IMAGE",
            "DATA_IMAGE",
            "EGRESS_PROXY_IMAGE",
            "EVIDENCE_PROXY_IMAGE",
            "FRONTEND_IMAGE",
            "KEYCLOAK_IMAGE",
            "MINIO_IMAGE",
            "POSTGRES_IMAGE",
            "REDIS_IMAGE",
            "REVERSE_PROXY_IMAGE",
        )
    }
    environment.update(
        {
            "CUSTOMS_DATA_STORE_TLS_NETWORK": "customs-data-store-tls",
            "CUSTOMS_DATA_STORE_ACCESS_MODE": "read_only",
            "CUSTOMS_DATA_STORE_PERMISSION_ATTESTATION_SHA256": "b" * 64,
        }
    )
    secure_data = {
        "POSTGRES_SSLMODE": "verify-full",
        "QDRANT_URL": "https://qdrant.internal.invalid",
        "QDRANT_API_KEY": "runtime-secret-placeholder",
        "NEO4J_URI": "neo4j+s://neo4j.internal.invalid",
    }
    validate_environment(environment, secure_data)

    floating = dict(environment)
    floating["FRONTEND_IMAGE"] = "example.invalid/team/image:latest"
    with pytest.raises(ValueError, match="exact registry image"):
        validate_environment(floating, secure_data)
    plaintext = {**secure_data, "QDRANT_URL": "http://qdrant:6333"}
    with pytest.raises(ValueError, match="HTTPS"):
        validate_environment(environment, plaintext)


def test_cross_repository_pr_gate_uses_an_environment_scoped_deploy_key() -> None:
    workflow = (ROOT / ".github/workflows/unit.yml").read_text(encoding="utf-8")
    cross = workflow.split("\n  cross-repository-system:\n", 1)[1]
    assert "CUSTOMS_CROSS_REPO_READ_TOKEN" not in workflow
    assert "pull_request_target" not in workflow
    assert (
        "if: github.event_name == 'pull_request' || "
        "(github.event_name == 'push' && github.ref == 'refs/heads/main')"
    ) in cross
    assert "environment: trusted-cross-repository" in cross
    assert "actions/create-github-app-token@" not in cross
    assert "repository: ${{ github.repository_owner }}/customs-ai-agent" in cross
    assert "ssh-key: ${{ secrets.CUSTOMS_AI_AGENT_DEPLOY_KEY }}" in cross
    assert "persist-credentials: false" in cross
    assert "verify_production_deployment_inputs.py" in workflow
    assert "Negative production input checks fail closed" in workflow


def test_public_ingress_declares_the_same_four_mib_body_limit_as_backend() -> None:
    proxy = (ROOT / "deploy/nginx.conf").read_text(encoding="utf-8")
    api = (ROOT / "src/customs_backend/api.py").read_text(encoding="utf-8")

    assert "client_max_body_size 4m;" in proxy
    assert "MAX_PUBLIC_REQUEST_BODY_BYTES = 4 * 1024 * 1024" in api
    assert "RequestBodyLimitMiddleware" in api


def test_agent_transport_limits_are_explicitly_aligned_with_backend_admission() -> None:
    compose = (ROOT / "compose.production.yaml").read_text(encoding="utf-8")
    service = (ROOT / "src/customs_backend/service.py").read_text(encoding="utf-8")

    assert 'CUSTOMS_AGENT_MAX_BODY_BYTES: "2097152"' in compose
    assert 'CUSTOMS_AGENT_MAX_DOCUMENTS: "6"' in compose
    assert 'CUSTOMS_AGENT_MAX_TEXT_CHARACTERS: "500000"' in compose
    assert "MAX_REVIEW_PRIMARY_DOCUMENTS = 6" in service
    assert "MAX_REVIEW_TEXT_CHARACTERS = 500_000" in service
    assert "MAX_AGENT_REQUEST_BODY_BYTES = 2 * 1024 * 1024" in service
