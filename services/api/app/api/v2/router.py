"""V2 业务 API 总路由。"""

from fastapi import APIRouter, Depends

from services.api.app.api.v1.auth import require_auth_user
from services.api.app.api.v2.capabilities import router as capabilities_router
from services.api.app.api.v2.devices import router as devices_router
from services.api.app.api.v2.evidence_redactions import router as evidence_redactions_router
from services.api.app.api.v2.flow_foundation import router as flow_foundation_router
from services.api.app.api.v2.h5_shortflows import router as h5_shortflows_router
from services.api.app.api.v2.tasks import router as tasks_router

api_v2_router = APIRouter(dependencies=[Depends(require_auth_user)])
api_v2_router.include_router(capabilities_router, tags=["v2-capabilities"])
api_v2_router.include_router(tasks_router, tags=["v2-tasks"])
api_v2_router.include_router(devices_router, tags=["v2-devices"])
api_v2_router.include_router(h5_shortflows_router, tags=["v2-h5-shortflows"])
api_v2_router.include_router(flow_foundation_router, tags=["v2-flow-foundation"])
api_v2_router.include_router(evidence_redactions_router, tags=["v2-evidence-redactions"])
