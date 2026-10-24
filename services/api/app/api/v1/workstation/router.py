"""工作台 API 路由聚合。"""

from fastapi import APIRouter, Depends

from services.api.app.api.v1.auth import require_auth_user
from services.api.app.api.v1.workstation.dashboard import router as dashboard_router
from services.api.app.api.v1.workstation.devices import router as devices_router
from services.api.app.api.v1.workstation.feedback import router as feedback_router
from services.api.app.api.v1.workstation.inspection_scope import router as inspection_scope_router
from services.api.app.api.v1.workstation.page_config import router as page_config_router
from services.api.app.api.v1.workstation.products import router as products_router
from services.api.app.api.v1.workstation.reports import router as reports_router
from services.api.app.api.v1.workstation.reviews import router as reviews_router
from services.api.app.api.v1.workstation.rule_catalog import router as rule_catalog_router
from services.api.app.api.v1.workstation.system import router as system_router
from services.api.app.api.v1.workstation.task_drafts import router as task_drafts_router
from services.api.app.api.v1.workstation.task_insight import router as task_insight_router
from services.api.app.api.v1.workstation.tasks import router as tasks_router

# 工作台业务接口统一挂登录鉴权；资源下载由 Bearer fetch 或资源绑定短时票据完成。
router = APIRouter(prefix="/workstation", dependencies=[Depends(require_auth_user)])
router.include_router(tasks_router, tags=["workstation-tasks"])
router.include_router(task_insight_router, tags=["workstation-task-insight"])
router.include_router(system_router, tags=["workstation-system"])
router.include_router(reports_router, tags=["workstation-reports"])
router.include_router(reviews_router, tags=["workstation-reviews"])
router.include_router(dashboard_router, tags=["workstation-dashboard"])
router.include_router(products_router, tags=["workstation-products"])
router.include_router(devices_router, tags=["workstation-devices"])
router.include_router(inspection_scope_router, tags=["workstation-inspection-scope"])
router.include_router(page_config_router, tags=["workstation-page-config"])
router.include_router(rule_catalog_router, tags=["workstation-rule-catalog"])
router.include_router(feedback_router, tags=["workstation-feedback"])
router.include_router(task_drafts_router, tags=["workstation-task-drafts"])
