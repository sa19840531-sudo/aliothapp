from fastapi import APIRouter, Depends

from services.api.app.api.v1.agents import router as agents_router
from services.api.app.api.v1.app_builds import router as app_builds_router
from services.api.app.api.v1.app_qc_docs import router as app_qc_docs_router
from services.api.app.api.v1.auth import (
    require_agent_permission,
    require_auth_user,
    require_cost_reports_permission,
    validate_auth_secret_at_startup,
)
from services.api.app.api.v1.auth import router as auth_router
from services.api.app.api.v1.cost_reports import router as cost_reports_router
from services.api.app.api.v1.departments import router as departments_router
from services.api.app.api.v1.evidence import router as evidence_router
from services.api.app.api.v1.health import router as health_router
from services.api.app.api.v1.llm_providers import router as llm_providers_router
from services.api.app.api.v1.operations import router as operations_router
from services.api.app.api.v1.products import router as products_router
from services.api.app.api.v1.rule_tests import router as rule_tests_router
from services.api.app.api.v1.rules import router as rules_router
from services.api.app.api.v1.tasks import router as tasks_router
from services.api.app.api.v1.tenants import router as tenants_router
from services.api.app.api.v1.users import router as users_router
from services.api.app.api.v1.workstation.router import router as workstation_router

# 生产环境在 API 路由装配时即校验 JWT 密钥，避免服务已接流量后才暴露配置错误。
validate_auth_secret_at_startup()

api_router = APIRouter()
api_router.include_router(health_router, tags=["health"])
api_router.include_router(
    agents_router,
    tags=["agents"],
    dependencies=[Depends(require_agent_permission)],
)
api_router.include_router(app_builds_router, tags=["app-builds"])
# 正式 App 质检 OpenAPI／Swagger：公开只读，不对齐业务鉴权。
api_router.include_router(app_qc_docs_router)
# 任务业务面统一挂登录鉴权（workstation 在其路由内部挂）。
api_router.include_router(
    tasks_router, tags=["tasks"], dependencies=[Depends(require_auth_user)]
)
api_router.include_router(auth_router, tags=["auth"])
api_router.include_router(users_router, tags=["users"])
api_router.include_router(tenants_router, tags=["tenants"])
api_router.include_router(departments_router, tags=["departments"])
# 运营总览 / 使用分析：跨公司汇总，接口内部按权限键与数据范围收口。
api_router.include_router(operations_router, tags=["operations"])
api_router.include_router(rules_router, tags=["rules"])
# 规则测试：人工证据跑单条规则，结果只回给页面、不落任何正式结果表。
api_router.include_router(rule_tests_router, tags=["rule-tests"])
api_router.include_router(products_router, tags=["products"])
# 成本报表是全平台的模型开销，不按公司拆分，只对总部账号开放。
api_router.include_router(
    cost_reports_router,
    tags=["cost-reports"],
    dependencies=[Depends(require_cost_reports_permission)],
)
# AI 供应商登记与路由设置：只对总部的系统运维权限开放。
api_router.include_router(llm_providers_router, tags=["llm-providers"])
# 证据票据签发端点自身执行登录与对象授权；文件端点只接受资源绑定短时票据。
api_router.include_router(evidence_router, tags=["evidence"])
# APP 自动化 Demo：由 main.create_app 在 ALIOTH_MOBILE_DEMO_ENABLED 时条件挂载（含 loopback 门禁）。
api_router.include_router(workstation_router)
