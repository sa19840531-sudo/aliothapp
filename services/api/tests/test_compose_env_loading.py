"""测试 Docker Compose 对本机环境文件的加载边界。"""

from pathlib import Path

COMPOSE_FILE = Path(__file__).resolve().parents[3] / "infra" / "docker-compose.yml"
LOCAL_ENV_TEMPLATE = (
    Path(__file__).resolve().parents[3] / "infra" / "env" / ".env.local.example"
)
ROOT_ENV_TEMPLATE = Path(__file__).resolve().parents[3] / ".env.example"
PRODUCTION_ENV_TEMPLATE = (
    Path(__file__).resolve().parents[3] / "infra" / "env" / ".env.production.example"
)
H5_RUNTIME_ENVIRONMENT_KEYS = (
    "ALIOTH_H5_VISUAL_COORDINATE_MODE",
    "ALIOTH_H5_BROWSER_PROXY_SERVER",
    "ALIOTH_H5_BROWSER_PROXY_BYPASS",
)
WEB_RUNTIME_ENVIRONMENT_KEYS = (
    "ALIOTH_WEB_BROWSER_PROXY_SERVER",
    "ALIOTH_WEB_BROWSER_PROXY_BYPASS",
)


def _service_block(compose_text: str, service_name: str) -> str:
    """按顶层服务缩进截取单个服务配置。"""
    marker = f"  {service_name}:"
    lines = compose_text.splitlines()
    start = lines.index(marker)
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if lines[index].startswith("  ")
            and not lines[index].startswith("    ")
            and lines[index].endswith(":")
        ),
        len(lines),
    )
    return "\n".join(lines[start:end])


def test_python_services_allow_an_explicit_local_override_env_file() -> None:
    compose_text = COMPOSE_FILE.read_text(encoding="utf-8")

    for service_name in (
        "alioth-initialize",
        "alioth-api",
        "alioth-worker",
        "alioth-devui",
    ):
        block = _service_block(compose_text, service_name)
        assert "path: ${ALIOTH_LOCAL_OVERRIDE_ENV_FILE:-../.env.local}" in block
        assert "required: false" in block

    root_env = ROOT_ENV_TEMPLATE.read_text(encoding="utf-8")
    assert "ALIOTH_LOCAL_OVERRIDE_ENV_FILE=../.env.local" in root_env


def test_postgres_keeps_infra_environment_file() -> None:
    compose_text = COMPOSE_FILE.read_text(encoding="utf-8")
    postgres_block = _service_block(compose_text, "alioth-postgres")

    assert "${ALIOTH_ENV_FILE:-./env/.env.local}" in postgres_block
    assert "path: ../.env.local" not in postgres_block


def test_h5_runtime_settings_are_loaded_only_from_standard_env_files() -> None:
    """API 与 Worker 不得用 environment 覆盖 env_file 中的 H5 运行配置。"""
    compose_text = COMPOSE_FILE.read_text(encoding="utf-8")

    for service_name in ("alioth-api", "alioth-worker"):
        block = _service_block(compose_text, service_name)
        assert "${ALIOTH_ENV_FILE:-./env/.env.local}" in block
        assert "path: ${ALIOTH_LOCAL_OVERRIDE_ENV_FILE:-../.env.local}" in block
        assert all(key not in block for key in H5_RUNTIME_ENVIRONMENT_KEYS)


def test_local_service_env_template_keeps_h5_runtime_disabled_by_default() -> None:
    """实际本地服务模板必须显式关闭视觉坐标并保持代理为空。"""
    template_lines = {
        line.strip()
        for line in LOCAL_ENV_TEMPLATE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert "ALIOTH_H5_VISUAL_COORDINATE_MODE=off" in template_lines
    assert "ALIOTH_H5_BROWSER_PROXY_SERVER=" in template_lines
    assert "ALIOTH_H5_BROWSER_PROXY_BYPASS=" in template_lines
    assert all("host.docker.internal" not in line for line in template_lines)


def test_standard_env_templates_keep_web_proxy_disabled_by_default() -> None:
    """根、本地和生产模板都必须暴露 Web 专属代理空配置。"""
    for template in (
        ROOT_ENV_TEMPLATE,
        LOCAL_ENV_TEMPLATE,
        PRODUCTION_ENV_TEMPLATE,
    ):
        template_lines = {
            line.strip()
            for line in template.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }

        assert all(
            f"{key}=" in template_lines for key in WEB_RUNTIME_ENVIRONMENT_KEYS
        ), template
        assert all("host.docker.internal" not in line for line in template_lines), template
