"""测试公共装配：为所有 API 测试统一注入假登录态。

业务接口（workstation / tasks / evidence）全量挂 ``require_auth_user`` 后，普通
API 测试不该各自处理登录流程。此处在 conftest 导入期包装 ``create_app``：每个测试
构建的应用默认覆盖鉴权依赖为固定测试用户。

需要走真实鉴权路径的测试（登录/令牌校验/角色权限：test_auth_api、test_users_api、
test_products_api、test_rules_api、test_rules_authz）在各自构建 client 处调用
``pop_test_auth(app)`` 移除该覆盖。
"""

import os

import pytest
from fastapi import FastAPI

# DevUI 实体包在 __init__ 导入期就 build_chat_client（agents/devui_entities，
# 由团队维护），缺 key 会让 test_devui_debug_tools 直接收集失败。测试一律不打真实
# LLM，这里在收集前补一个占位值；已配置真实 key 的环境不覆盖。
os.environ.setdefault("ALIOTH_LLM_API_KEY", "test-only-placeholder-key")
# 单测禁止误用本机 .env.local 的 postgres 设备池去连真实数据库。
os.environ["ALIOTH_APP_DEVICE_LEASE_BACKEND"] = "in_process"
# 单测不写真实 LLM 调用流水；成本流水由专项仓储测试覆盖。
os.environ["ALIOTH_LLM_USAGE_AUDIT_ENABLED"] = "0"

from services.api.app import main as _main_module  # noqa: E402
from services.api.app.api.v1 import auth as _auth_module
from services.api.app.api.v1 import evidence as _evidence_module
from services.api.app.api.v1 import object_access as _object_access_module
from services.api.app.application.device_pool import (
    reset_device_pool_for_tests as _reset_device_pool_for_tests,
)
from services.api.app.core.config import get_settings

TEST_AUTH_USER = _auth_module.AuthUserResponse(
    id="test-user",
    username="tester",
    full_name="测试用户",
    role="平台管理员",
    status="enabled",
    permissions=[
        "dashboard",
        "users",
        "users:any",
        "tenants",
        "roles",
        "auditLogs",
        "qcRules",
        "configCenter",
        "costReports",
        "agentRuns",
        "systemOperations",
    ],
    tenant_id="tenant-1",
    tenant_name="体验中心",
)


@pytest.fixture(autouse=True)
def reset_device_pool(monkeypatch: pytest.MonkeyPatch):
    """设备池是进程级单例，用例租了不还会拖垮后续全部用例。

    App 工作流走 ``get_device_pool().claim()``，失败即 DEVICE_POOL_EXHAUSTED
    「无可用设备」。每个用例前重置，避免同文件内互相污染。
    """
    monkeypatch.setenv("ALIOTH_APP_DEVICE_LEASE_BACKEND", "in_process")
    # 单测默认关闭池满等待，避免 DEVICE_BUSY 用例被默认 600 秒退避拖死。
    monkeypatch.setenv("ALIOTH_DEVICE_WAIT_MAX_SECONDS", "0")
    _reset_device_pool_for_tests()
    yield
    _reset_device_pool_for_tests()


@pytest.fixture(autouse=True)
def isolate_inspection_work_item_mode(monkeypatch: pytest.MonkeyPatch):
    """隔离本地 .env，专项用例可在测试体内显式覆盖工作项模式。

    离场门禁同理：本地 .env.local 若把它开到 shadow/on，装配出的就是
    PostgreSQL 门禁，单测会去连真库（表现为「缺少数据库连接配置」）。
    默认关掉，需要门禁的用例自行显式打开。
    """
    monkeypatch.setenv("ALIOTH_QC_INSPECTION_WORK_ITEMS_MODE", "off")
    monkeypatch.setenv("ALIOTH_WEB_EVIDENCE_EXIT_GATE_MODE", "off")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def pop_test_auth(app: FastAPI) -> None:
    """移除统一注入的假登录态，让该应用走真实鉴权路径。"""
    app.dependency_overrides.pop(_auth_module.require_auth_user, None)


class _AllowAllObjectAccessRepository:
    """普通 API 单测的对象访问桩；权限矩阵测试必须显式移除此覆盖。"""

    async def can_access_task(self, **_kwargs) -> bool:
        return True

    async def can_access_artifact(self, **_kwargs) -> bool:
        return True

    async def can_access_agent_run(self, **_kwargs) -> bool:
        return True

    async def can_access_review(self, **_kwargs) -> bool:
        return True

    async def can_access_build(self, **_kwargs) -> bool:
        return True


def pop_test_object_access(app: FastAPI) -> None:
    """移除对象访问桩，让权限专项用例走真实或自定义仓储。"""
    app.dependency_overrides.pop(_object_access_module._get_object_access_repository, None)


class _TestTicketSubjectRepository:
    """下载票据兑现的当前主体桩；撤权矩阵测试必须显式移除此覆盖。

    与 TEST_AUTH_USER 同身份、enabled、同租户——普通用例兑现票据不连真库；
    停用/换租户/删号等撤权场景由专项测试移除本桩后自行注入。
    """

    async def find_by_id(self, user_id: str) -> dict[str, object] | None:
        if user_id != TEST_AUTH_USER.id:
            return None
        return {
            "id": TEST_AUTH_USER.id,
            "username": TEST_AUTH_USER.username,
            "full_name": TEST_AUTH_USER.full_name,
            "role": TEST_AUTH_USER.role,
            "status": TEST_AUTH_USER.status,
            "tenant_id": TEST_AUTH_USER.tenant_id,
        }


def pop_test_ticket_subject(app: FastAPI) -> None:
    """移除票据主体桩，让撤权复验用例走真实或自定义主体仓储。"""
    app.dependency_overrides.pop(_evidence_module._get_ticket_subject_repository, None)


class _NoopDownloadAuditRepository:
    """下载审计桩：普通用例兑现票据不连真库；审计断言用例自行覆盖为记录器。"""

    async def record(self, **_kwargs) -> None:
        return None


def pop_test_download_audit(app: FastAPI) -> None:
    """移除下载审计桩，让审计专项用例走真实或自定义审计仓储。"""
    app.dependency_overrides.pop(_evidence_module._get_download_audit_repository, None)


_original_create_app = _main_module.create_app


def _create_app_with_test_auth(*args, **kwargs):
    app = _original_create_app(*args, **kwargs)
    app.dependency_overrides[_auth_module.require_auth_user] = lambda: TEST_AUTH_USER
    app.dependency_overrides[_object_access_module._get_object_access_repository] = (
        _AllowAllObjectAccessRepository
    )
    app.dependency_overrides[_evidence_module._get_ticket_subject_repository] = (
        _TestTicketSubjectRepository
    )
    app.dependency_overrides[_evidence_module._get_download_audit_repository] = (
        _NoopDownloadAuditRepository
    )
    return app


# 在测试模块被收集/导入之前完成替换，`from ...main import create_app` 拿到的即包装版。
_main_module.create_app = _create_app_with_test_auth
