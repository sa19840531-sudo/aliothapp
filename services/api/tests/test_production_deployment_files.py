"""生产部署声明回归测试。"""

from __future__ import annotations

import os
import re
import runpy
import stat
import subprocess
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
LOCAL_COMPOSE = REPOSITORY_ROOT / "infra" / "docker-compose.yml"
PRODUCTION_COMPOSE = REPOSITORY_ROOT / "infra" / "docker-compose.production.yml"
PRODUCTION_ENV = REPOSITORY_ROOT / "infra" / "env" / ".env.production.example"
RULESET_SEED = (
    REPOSITORY_ROOT / "docs" / "data" / "alioth_first_ruleset_qc_ruleset_seed.csv"
)
COMPOSE_ENV = REPOSITORY_ROOT / "infra" / "env" / "compose.production.example"
API_DOCKERFILE = REPOSITORY_ROOT / "infra" / "docker" / "api" / "Dockerfile.production"
GATEWAY_NGINX = REPOSITORY_ROOT / "infra" / "docker" / "gateway" / "nginx.conf"
GATEWAY_DOCKERFILE = REPOSITORY_ROOT / "infra" / "docker" / "gateway" / "Dockerfile"
GATEWAY_RENDER_SCRIPT = (
    REPOSITORY_ROOT / "infra" / "docker" / "gateway" / "render-radar-vhost.sh"
)
GITLAB_CI = REPOSITORY_ROOT / ".gitlab-ci.yml"
ALEMBIC_ENV = REPOSITORY_ROOT / "services" / "api" / "migrations" / "env.py"
DEPLOY_SCRIPT = REPOSITORY_ROOT / "infra" / "scripts" / "deploy-production.sh"
LOCAL_REDEPLOY_SCRIPT = (
    REPOSITORY_ROOT / "infra" / "scripts" / "redeploy-local-production.sh"
)
VERIFY_SCRIPT = REPOSITORY_ROOT / "infra" / "scripts" / "verify-production.sh"
DEPLOYMENT_GUIDE = (
    REPOSITORY_ROOT
    / "docs"
    / "deployment"
    / "2026-07-17-alioth-single-host-deployment-guide.md"
)
MIGRATION_VERSIONS = REPOSITORY_ROOT / "services" / "api" / "migrations" / "versions"
OBSERVATION_PACKET_IDEMPOTENCY_MIGRATION = (
    MIGRATION_VERSIONS
    / "20260722_0016_observation_packet_idempotency_index.py"
)
PAGE_NAV_ALIAS_VECTOR_MIGRATION = (
    MIGRATION_VERSIONS / "20260724_0017_page_nav_alias_vector_tables.py"
)
RECOVERY_ORIGIN_MIGRATION = (
    MIGRATION_VERSIONS
    / "20260723_0017_allow_recovery_page_exploration_origin.py"
)
RECOVERY_ORIGIN_REPAIR_MIGRATION = (
    MIGRATION_VERSIONS
    / "20260725_0018_repair_recovery_page_exploration_origin_constraint.py"
)
TASK_FINALIZATION_CANCELED_MIGRATION = (
    MIGRATION_VERSIONS / "20260805_0037_task_finalization_canceled.py"
)
WEB_FORMAL_HEAD_MERGE_MIGRATION = (
    MIGRATION_VERSIONS / "20260903_0086_merge_web_formal_heads.py"
)


def test_ci_browser_jobs_install_process_identity_dependency_without_skipping_tests():
    ci = GITLAB_CI.read_text(encoding="utf-8")
    for job in ("backend-test", "h5-postgres-test"):
        body = re.search(rf"(?ms)^{job}:\n(.*?)(?=^\S|\Z)", ci).group(1)
        assert re.search(r"apt-get install[^\n]*\bprocps\b", body), job
        assert re.search(r"apt-get install[^\n]*\bnodejs\b", body), job
        assert "python -m pytest" in body
        assert "allow_failure: true" not in body


def test_backend_ci_preserves_full_scope_and_failure_diagnostics():
    ci = GITLAB_CI.read_text(encoding="utf-8")
    body = re.search(r"(?ms)^backend-test:\n(.*?)(?=^\S|\Z)", ci).group(1)
    assert "python -m pytest services/api/tests -q" in body
    assert "--durations=25 -o faulthandler_timeout=60" in body
    assert "--junitxml=backend-results.xml" in body
    assert "when: always" in body
    assert "junit: backend-results.xml" in body
    assert "allow_failure: true" not in body
    assert "--lf" not in body and "--last-failed" not in body


def _is_executable_script(path: Path) -> bool:
    """跨平台校验脚本在 Git 索引中的可执行属性。"""
    if os.name != "nt":
        return bool(path.stat().st_mode & stat.S_IXUSR)
    git_mode = subprocess.check_output(
        ["git", "ls-files", "--stage", "--", path.as_posix()],
        text=True,
    ).split(maxsplit=1)[0]
    return git_mode == "100755"


def test_production_compose_uses_immutable_images_and_external_middleware() -> None:
    compose_text = PRODUCTION_COMPOSE.read_text(encoding="utf-8")

    for service_name in (
        "alioth-migrate",
        "alioth-initialize",
        "alioth-api",
        "alioth-worker",
        "alioth-workstation",
        "alioth-gateway",
    ):
        assert f"  {service_name}:" in compose_text

    assert "build:" not in compose_text
    assert "--reload" not in compose_text
    assert "npm install" not in compose_text
    assert "./services" not in compose_text
    assert "./app" not in compose_text
    assert "data_services_net:" in compose_text
    assert "minio_net:" in compose_text
    assert compose_text.count("external: true") == 2
    assert "ALIOTH_IMAGE_TAG:?ALIOTH_IMAGE_TAG is required" in compose_text
    assert "/api/v1/readyz" in compose_text
    assert "urlopen('http://127.0.0.1:8000/api/v1/health'" not in compose_text
    assert "wget --quiet --spider http://127.0.0.1/api/v1/health" not in compose_text
    assert "python scripts/seed_first_ruleset.py" in compose_text
    assert "python scripts/seed_products.py" in compose_text
    assert "python scripts/seed_page_nav_alias.py" in compose_text
    assert "python scripts/sync_capability_registry.py" in compose_text
    assert 'host.docker.internal:host-gateway' in compose_text
    assert '${ALIOTH_GATEWAY_HTTPS_PORT:-443}:443' in compose_text
    assert (
        '${ALIOTH_GATEWAY_TLS_HOST_PATH:-/etc/letsencrypt}:/etc/letsencrypt:ro'
        in compose_text
    )
    assert (
        '${ALIOTH_GATEWAY_ACME_WEBROOT_HOST_PATH:-/opt/alioth/config/acme-challenge}:/var/www/acme-challenge:ro'
        in compose_text
    )


def test_gateway_nginx_declares_radar_domain_route() -> None:
    nginx_text = GATEWAY_NGINX.read_text(encoding="utf-8")
    render_script = GATEWAY_RENDER_SCRIPT.read_text(encoding="utf-8")

    assert "listen 80 default_server;" in nginx_text
    assert "server_name _;" in nginx_text
    assert "include /etc/nginx/alioth-extra/*.conf;" in nginx_text
    assert "gateway_domain=${ALIOTH_GATEWAY_RADAR_DOMAIN:-xnwork.xyz}" in render_script
    assert (
        "gateway_radar_upstream=${ALIOTH_GATEWAY_RADAR_UPSTREAM:-http://host.docker.internal:8787}"
        in render_script
    )
    assert "location ^~ /.well-known/acme-challenge/" in render_script
    assert "${gateway_acme_webroot}/.well-known/acme-challenge" not in render_script
    assert 'return 301 https://$host$request_uri;' in render_script
    assert "listen 443 ssl http2;" in render_script
    assert "proxy_pass ${gateway_radar_upstream};" in render_script


def test_gateway_nginx_allows_apk_upload_up_to_backend_limit() -> None:
    from services.api.app.core.config import PlatformBuildSettings

    nginx_text = GATEWAY_NGINX.read_text(encoding="utf-8")

    match = re.search(
        r"location = /api/v1/app-builds \{(?P<body>[^}]*)\}",
        nginx_text,
    )
    assert match is not None, "网关缺少 APK 上传专用 location，会沿用 /api/ 的 20m 返回 413"
    body = match.group("body")
    assert "proxy_pass http://alioth_api;" in body
    size = re.search(r"client_max_body_size (\d+)m;", body)
    assert size is not None
    # 网关要比后端上限略宽（multipart 头部有开销），超限由后端给出可读提示而不是 nginx 裸 413。
    assert int(size.group(1)) * 1024 * 1024 > PlatformBuildSettings().max_size_bytes


def test_gateway_image_renders_optional_https_vhost_on_startup() -> None:
    dockerfile = GATEWAY_DOCKERFILE.read_text(encoding="utf-8")

    assert (
        "COPY infra/docker/gateway/render-radar-vhost.sh "
        "/docker-entrypoint.d/40-render-radar-vhost.sh"
        in dockerfile
    )
    assert "chmod +x /docker-entrypoint.d/40-render-radar-vhost.sh" in dockerfile
    assert "EXPOSE 80 443 8088" in dockerfile


def test_local_compose_initializes_page_nav_alias_seed_data() -> None:
    compose_text = LOCAL_COMPOSE.read_text(encoding="utf-8")

    assert "python scripts/seed_page_nav_alias.py" in compose_text


def test_compose_initializers_publish_journey_definitions() -> None:
    for compose_file in (LOCAL_COMPOSE, PRODUCTION_COMPOSE):
        compose_text = compose_file.read_text(encoding="utf-8")
        assert "python scripts/seed_journey_definitions.py" in compose_text


def test_production_api_image_contains_runtime_and_initialization_assets() -> None:
    dockerfile = API_DOCKERFILE.read_text(encoding="utf-8")

    assert "COPY services ./services" in dockerfile
    assert "COPY scripts ./scripts" in dockerfile
    assert "COPY docs/data ./docs/data" in dockerfile
    assert "playwright install --with-deps chromium" in dockerfile
    assert "PYTHONPATH=/workspace" in dockerfile
    assert 'USER 10001' in dockerfile
    assert 'pip install --no-cache-dir .' in dockerfile
    assert '.[dev]' not in dockerfile


def test_production_environment_examples_cover_required_runtime_contract() -> None:
    app_env = PRODUCTION_ENV.read_text(encoding="utf-8")
    compose_env = COMPOSE_ENV.read_text(encoding="utf-8")

    for variable_name in (
        "ALIOTH_DATABASE_URL",
        "ALIOTH_REDIS_URL",
        "ALIOTH_AUTH_SECRET",
        "ALIOTH_AGENT_RUN_STATE_STORE_BACKEND",
        "ALIOTH_WORKFLOW_VERSION",
        "ALIOTH_H5_V2_MODE",
        "ALIOTH_H5_INSPECTION_NOTIFICATION_MODE",
        "ALIOTH_H5_VISUAL_COORDINATE_MODE",
        "ALIOTH_H5_AUTH_ENABLED",
        "ALIOTH_H5_AUTH_CONFIG_PATH",
        "ALIOTH_WORKER_ROLES",
        "ALIOTH_APP_RUN_LEASE_SECONDS",
        "ALIOTH_APP_RUN_RECOVERY_INTERVAL_SECONDS",
        "ALIOTH_APP_DEVICE_LEASE_BACKEND",
        "ALIOTH_DEVICE_POOL_SLOTS",
        "ALIOTH_DEVICE_POOL_MORELOGIN_ENV_IDS",
        "ALIOTH_DEVICE_WAIT_MAX_SECONDS",
        "ALIOTH_MORELOGIN_API_MODE",
        "ALIOTH_MORELOGIN_OPEN_API",
        "ALIOTH_MORELOGIN_API_ID",
        "ALIOTH_MORELOGIN_API_KEY",
        "ALIOTH_DEVICE_LEASE_RECOVERY_INTERVAL_SECONDS",
        "ALIOTH_LLM_API_KEY",
        "ALIOTH_LLM_USAGE_AUDIT_ENABLED",
        "ALIOTH_EVIDENCE_STORAGE_BACKEND",
        "ALIOTH_EVIDENCE_MANIFEST_READ_MODE",
        "ALIOTH_WEB_EVIDENCE_EXIT_GATE_MODE",
        "ALIOTH_EVIDENCE_RETENTION_DAYS",
        "ALIOTH_EVIDENCE_GC_ENABLED",
        "ALIOTH_MINIO_ENDPOINT",
        "ALIOTH_MINIO_ACCESS_KEY",
        "ALIOTH_MINIO_SECRET_KEY",
        "ALIOTH_CORS_ALLOWED_ORIGINS",
    ):
        assert f"{variable_name}=" in app_env

    assert "ALIOTH_AGENT_RUN_STATE_STORE_BACKEND=postgres" in app_env
    assert "ALIOTH_AUTH_SECRET=replace-with-at-least-32-random-characters" in app_env
    assert "ALIOTH_EVIDENCE_STORAGE_BACKEND=minio" in app_env
    assert "ALIOTH_EVIDENCE_MANIFEST_READ_MODE=off" in app_env
    assert "ALIOTH_WEB_EVIDENCE_EXIT_GATE_MODE=off" in app_env
    assert "ALIOTH_EVIDENCE_GC_ENABLED=false" in app_env
    assert "ALIOTH_WORKFLOW_VERSION=v2" in app_env
    assert "ALIOTH_H5_V2_MODE=on" in app_env
    assert "ALIOTH_H5_INSPECTION_NOTIFICATION_MODE=enforced" in app_env
    assert "ALIOTH_H5_VISUAL_COORDINATE_MODE=off" in app_env
    assert "ALIOTH_H5_AUTH_ENABLED=false" in app_env
    assert "ALIOTH_H5_AUTH_CONFIG_PATH=" in app_env
    assert "ALIOTH_WORKER_ROLES=explorer,judge" in app_env
    assert "ALIOTH_APP_DEVICE_LEASE_BACKEND=postgres" in app_env
    assert "ALIOTH_DEVICE_POOL_SLOTS=" in app_env
    assert "ALIOTH_DEVICE_POOL_MORELOGIN_ENV_IDS=" in app_env
    assert "ALIOTH_DEVICE_WAIT_MAX_SECONDS=600" in app_env
    # ONIRENT 生产合同：停用 MoreLogin Open API，保留 local + 空地址占位。
    assert "ALIOTH_MORELOGIN_API_MODE=local" in app_env
    assert "\nALIOTH_MORELOGIN_OPEN_API=\n" in f"\n{app_env}\n"
    assert "ALIOTH_LLM_MODEL=gpt-5.6-terra" in app_env
    assert "ALIOTH_LLM_REASONING_EFFORT=low" in app_env
    assert "ALIOTH_LLM_BASE_URL=https://api.routin.ai/v1" in app_env
    assert "ALIOTH_LLM_TIMEOUT_SECONDS=180" in app_env
    assert "ALIOTH_LLM_USAGE_AUDIT_ENABLED=1" in app_env
    assert "ALIOTH_AGENT_MAX_STEPS=200" in app_env
    assert "ALIOTH_AGENT_MAX_ITERATIONS=200" in app_env
    assert "ALIOTH_AGENT_MAX_DURATION_SECONDS=3600" in app_env
    assert "ALIOTH_AGENT_MAX_STEPS_HARD_CAP=600" in app_env
    assert "ALIOTH_QC_SEMANTIC_REQUEST_TIMEOUT_SECONDS=60" in app_env
    assert "ALIOTH_QC_SEMANTIC_MAX_RETRIES=0" in app_env
    assert "ALIOTH_QC_SEMANTIC_CIRCUIT_SECONDS=120" in app_env
    assert "ak-" not in app_env
    assert "CI_REGISTRY_IMAGE=" in compose_env
    assert "ALIOTH_IMAGE_TAG=" in compose_env
    assert "ALIOTH_APP_ENV_FILE=" in compose_env
    assert "ALIOTH_GATEWAY_HTTPS_PORT=443" in compose_env
    assert "ALIOTH_GATEWAY_RADAR_DOMAIN=xnwork.xyz" in compose_env
    assert "ALIOTH_GATEWAY_RADAR_UPSTREAM=http://host.docker.internal:8787" in compose_env
    assert "ALIOTH_GATEWAY_TLS_HOST_PATH=/etc/letsencrypt" in compose_env
    assert (
        "ALIOTH_GATEWAY_ACME_WEBROOT_HOST_PATH=/opt/alioth/config/acme-challenge"
        in compose_env
    )


def test_first_ruleset_uses_production_runtime_limits() -> None:
    seed = RULESET_SEED.read_text(encoding="utf-8")

    assert '""max_steps"":200' in seed
    assert '""max_duration_seconds"":3600' in seed
    assert '""max_ai_calls"":200' in seed


def test_gitlab_pipeline_preserves_dev_deployment_order_and_serialization() -> None:
    pipeline = GITLAB_CI.read_text(encoding="utf-8")

    assert "resource_group: alioth-dev" in pipeline
    assert "'+refs/heads/dev:refs/remotes/origin/dev'" in pipeline
    assert "git checkout --detach '$CI_COMMIT_SHA'" in pipeline
    assert "infra/scripts/redeploy-local-production.sh" in pipeline
    assert "--branch dev" in pipeline
    assert "--commit '$CI_COMMIT_SHA'" in pipeline
    assert "infra/scripts/verify-production.sh" in pipeline
    assert '$CI_COMMIT_BRANCH == "dev" && $CI_PIPELINE_SOURCE == "push"' in pipeline
    assert "when: on_success" in pipeline
    assert "allow_failure: false" in pipeline


def test_gitlab_pipeline_deduplicates_open_dev_merge_request_branch() -> None:
    pipeline = GITLAB_CI.read_text(encoding="utf-8")

    assert '$CI_PIPELINE_SOURCE == "merge_request_event"' in pipeline
    assert '$CI_COMMIT_BRANCH == "dev" && $CI_OPEN_MERGE_REQUESTS' in pipeline
    assert "when: never" in pipeline


def test_gitlab_backend_test_caches_playwright_browser() -> None:
    pipeline = GITLAB_CI.read_text(encoding="utf-8")

    assert 'PLAYWRIGHT_BROWSERS_PATH: "$CI_PROJECT_DIR/.cache/ms-playwright"' in pipeline
    assert "prefix: backend-test" in pipeline
    assert "- .cache/ms-playwright" in pipeline
    assert "python -m playwright install --with-deps chromium" in pipeline


def test_gitlab_h5_postgres_regression_uses_isolated_database() -> None:
    pipeline = GITLAB_CI.read_text(encoding="utf-8")

    assert "h5-postgres-test:" in pipeline
    assert "pgvector/pgvector:0.8.2-pg17-bookworm" in pipeline
    assert "alias: postgres" in pipeline
    assert "POSTGRES_DB: alioth_h5_test" in pipeline
    assert (
        "ALIOTH_DATABASE_URL: postgresql://postgres:postgres@127.0.0.1:5432/postgres"
        in pipeline
    )
    assert (
        "ALIOTH_TEST_DATABASE_URL: "
        "postgresql://postgres:postgres@127.0.0.1:5432/alioth_h5_test"
        in pipeline
    )
    assert "socat TCP-LISTEN:5432,bind=127.0.0.1" in pipeline
    assert "$(find services/api/tests -maxdepth 1 -name 'test_h5*.py'" in pipeline
    assert "! -name 'test_h5_acceptance_fixture_server.py' -print)" in pipeline
    assert "- h5-postgres-test" in pipeline


def test_alembic_keeps_existing_application_loggers_enabled() -> None:
    migration_environment = ALEMBIC_ENV.read_text(encoding="utf-8")

    assert "disable_existing_loggers=False" in migration_environment


def test_h5_ci_reports_case_identity_and_slow_test_stacks_without_skipping_checks() -> None:
    pipeline = GITLAB_CI.read_text(encoding="utf-8")
    job = pipeline.split("h5-postgres-test:", 1)[1].split("\nworkstation-build:", 1)[0]

    assert "-v --durations=25 -o faulthandler_timeout=60" in job
    assert "--junitxml=h5-postgres-results.xml" in job
    assert "services/api/tests/test_web_sequence_seed_lineage_postgres.py" in job
    assert "junit: h5-postgres-results.xml" in job
    assert "when: always" in job
    assert "--maxfail" not in job
    assert "allow_failure: true" not in job
    assert "$(find services/api/tests -maxdepth 1 -name 'test_h5*.py'" in job


def test_gitlab_main_merge_request_uses_lightweight_promotion_check() -> None:
    pipeline = GITLAB_CI.read_text(encoding="utf-8")

    assert ".full-validation:" in pipeline
    assert pipeline.count("extends: .full-validation") == 7
    assert "workstation-test:" in pipeline
    assert "admin-test:" not in pipeline
    assert "frontend-e2e:" in pipeline
    assert "mcr.microsoft.com/playwright:v1.62.1-noble" in pipeline
    assert "- frontend-e2e" in pipeline
    assert "'$CI_PIPELINE_SOURCE == \"push\" && $CI_COMMIT_BRANCH == \"main\"'" not in pipeline
    assert 'CI_MERGE_REQUEST_TARGET_BRANCH_NAME == "main"' in pipeline
    assert "main-promotion-check:" in pipeline
    assert 'GIT_DEPTH: "0"' in pipeline
    assert "'+refs/heads/dev:refs/remotes/origin/dev'" in pipeline
    assert 'git merge-base --is-ancestor "$CI_COMMIT_SHA" origin/dev' in pipeline
    assert "待合入 main 的提交尚未进入 dev，请先完成 dev 验收。" in pipeline


def test_deployment_script_preserves_backup_migration_and_verification_order() -> None:
    script = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    backup_index = script.index('sudo "$BACKUP_SCRIPT"')
    stop_index = script.index("compose stop alioth-gateway alioth-worker alioth-api")
    migration_index = script.index("compose --profile ops run --rm alioth-migrate")
    initialize_index = script.index("compose --profile ops run --rm alioth-initialize")
    start_index = script.index("compose up -d")
    verify_index = script.index('"$REPOSITORY_ROOT/infra/scripts/verify-production.sh"')

    assert (
        backup_index
        < stop_index
        < migration_index
        < initialize_index
        < start_index
        < verify_index
    )
    assert _is_executable_script(DEPLOY_SCRIPT)
    assert _is_executable_script(VERIFY_SCRIPT)


def test_production_verification_does_not_anonymously_call_protected_api() -> None:
    script = VERIFY_SCRIPT.read_text(encoding="utf-8")

    assert "/api/v1/readyz" in script
    assert "/api/v1/health" not in script
    assert "/api/v1/workstation/system/status" not in script


def test_deployment_scripts_preserve_optional_app_qc_compose_override() -> None:
    scripts = {
        "deploy": DEPLOY_SCRIPT.read_text(encoding="utf-8"),
        "redeploy": LOCAL_REDEPLOY_SCRIPT.read_text(encoding="utf-8"),
        "verify": VERIFY_SCRIPT.read_text(encoding="utf-8"),
    }

    for script_name, script in scripts.items():
        assert (
            "ALIOTH_COMPOSE_OVERRIDE_FILE:-/opt/alioth/config/"
            "docker-compose.app-qc.yml"
        ) in script
        expected_guard = (
            'if [[ -f "$COMPOSE_OVERRIDE_FILE" ]]'
            if script_name == "redeploy"
            else 'if [ -f "$COMPOSE_OVERRIDE_FILE" ]'
        )
        assert expected_guard in script
        assert '-f "$COMPOSE_OVERRIDE_FILE"' in script

    assert 'ALIOTH_COMPOSE_OVERRIDE_FILE="$COMPOSE_OVERRIDE_FILE"' in scripts["deploy"]
    assert 'ALIOTH_COMPOSE_OVERRIDE_FILE="$COMPOSE_OVERRIDE_FILE"' in scripts["redeploy"]
    assert (
        "-f ${COMPOSE_OVERRIDE_FILE} up -d --force-recreate"
        in scripts["redeploy"]
    )


def test_dockerignore_excludes_local_secrets_and_heavy_artifacts() -> None:
    dockerignore = (REPOSITORY_ROOT / ".dockerignore").read_text(encoding="utf-8")

    for pattern in (".env", ".env.*", ".venv", "**/node_modules", ".alioth"):
        assert pattern in dockerignore
    assert "!docs/data/**" in dockerignore


def test_deployment_guide_uses_current_single_migration_head() -> None:
    revisions: set[str] = set()
    down_revisions: set[str] = set()
    for migration_file in MIGRATION_VERSIONS.glob("*.py"):
        migration_text = migration_file.read_text(encoding="utf-8")
        revision_match = re.search(r'^revision = "([^"]+)"$', migration_text, re.MULTILINE)
        if revision_match is None:
            continue
        revisions.add(revision_match.group(1))
        down_revisions.update(
            revision
            for revision in re.findall(
                r'"(\d{8}_\d{4})"',
                migration_text.split("branch_labels", 1)[0],
            )
            if revision != revision_match.group(1)
        )

    heads = revisions - down_revisions
    assert len(heads) == 1

    guide_text = DEPLOYMENT_GUIDE.read_text(encoding="utf-8")
    assert f"{heads.pop()} (head)" in guide_text


def test_web_formal_merge_revision_joins_both_heads_without_schema_writes() -> None:
    migration = runpy.run_path(str(WEB_FORMAL_HEAD_MERGE_MIGRATION))

    assert migration["revision"] == "20260903_0086"
    assert migration["down_revision"] == (
        "20260901_0081",
        "20260902_0085",
    )
    assert migration["branch_labels"] is None
    assert migration["depends_on"] is None

    class ForbiddenMigrationOp:
        def __getattr__(self, name: str):
            raise AssertionError(f"merge revision 不得执行 schema 操作：{name}")

    migration["upgrade"].__globals__["op"] = ForbiddenMigrationOp()
    migration["downgrade"].__globals__["op"] = ForbiddenMigrationOp()
    assert migration["upgrade"]() is None
    assert migration["downgrade"]() is None


def test_task_finalization_canceled_migration_extends_current_head() -> None:
    migration_text = TASK_FINALIZATION_CANCELED_MIGRATION.read_text(encoding="utf-8")

    assert 'revision = "20260805_0037"' in migration_text
    assert 'down_revision = "20260805_0036"' in migration_text
    assert "ck_qc_task_execution_status" in migration_text
    assert "ck_qc_task_finalization_legacy_status" in migration_text
    assert "ck_qc_report_gate_execution" in migration_text
    assert "execution_status = 'canceled' AND status = 'canceled'" in migration_text


def test_observation_packet_idempotency_migration_matches_conflict_key() -> None:
    migration_text = OBSERVATION_PACKET_IDEMPOTENCY_MIGRATION.read_text(
        encoding="utf-8"
    )

    assert 'revision = "20260722_0016"' in migration_text
    assert 'down_revision = "20260721_0015"' in migration_text
    assert "CREATE UNIQUE INDEX IF NOT EXISTS" in migration_text
    assert "uq_qc_observation_packet_run_page_rule" in migration_text
    assert "ON qc_observation_packet" in migration_text
    for column_name in (
        "created_by_agent_run_id",
        "page_capture_id",
        "rule_id",
    ):
        assert column_name in migration_text
        assert f"{column_name} IS NOT NULL" in migration_text
    assert "DROP INDEX IF EXISTS" in migration_text


def test_page_nav_alias_vector_migration_declares_alias_and_embedding_tables() -> None:
    migration_text = PAGE_NAV_ALIAS_VECTOR_MIGRATION.read_text(encoding="utf-8")

    assert 'revision = "20260724_0017"' in migration_text
    assert 'down_revision = "20260723_0017"' in migration_text
    assert "CREATE EXTENSION IF NOT EXISTS vector" in migration_text
    assert "CREATE TABLE IF NOT EXISTS page_nav_alias_term" in migration_text
    assert "CREATE TABLE IF NOT EXISTS page_nav_alias_embedding" in migration_text
    for column_name in (
        "canonical_code",
        "page_subtype_code",
        "description",
        "normalized_text",
        "embedding_text",
        "review_status",
    ):
        assert column_name in migration_text
    assert "embedding vector(" in migration_text
    assert "COMMENT ON TABLE page_nav_alias_term" in migration_text
    assert "COMMENT ON TABLE page_nav_alias_embedding" in migration_text
    assert "COMMENT ON COLUMN page_nav_alias_term.description" in migration_text
    assert "COMMENT ON COLUMN page_nav_alias_embedding.embedding" in migration_text
    assert "DROP TABLE IF EXISTS page_nav_alias_embedding" in migration_text
    assert "DROP TABLE IF EXISTS page_nav_alias_term" in migration_text


def test_recovery_origin_migration_matches_runtime_domain() -> None:
    migration_text = RECOVERY_ORIGIN_MIGRATION.read_text(encoding="utf-8")

    assert 'revision = "20260723_0017"' in migration_text
    assert 'down_revision = "20260722_0016"' in migration_text
    assert "ck_qc_page_exploration_action_origin" in migration_text
    assert "'entry', 'discovered', 'recovery'" in migration_text
    assert "SET origin = 'discovered'" in migration_text

def test_recovery_origin_repair_migration_keeps_latest_head_constraint() -> None:
    migration_text = RECOVERY_ORIGIN_REPAIR_MIGRATION.read_text(encoding="utf-8")

    assert 'revision = "20260725_0018"' in migration_text
    assert 'down_revision = "20260724_0017"' in migration_text
    assert "ck_qc_page_exploration_action_origin" in migration_text
    assert "'entry', 'discovered', 'recovery'" in migration_text
    assert "SET origin = 'discovered'" in migration_text
