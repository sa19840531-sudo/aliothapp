import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from services.api.app.api.v1.router import api_router
from services.api.app.api.v1.xingyu_official.gate import (
    xingyu_official_docs_enabled,
    xingyu_official_router_enabled,
)
from services.api.app.api.v2.router import api_v2_router
from services.api.app.core.config import get_settings
from services.api.app.core.errors import register_exception_handlers
from services.api.app.core.logging import configure_logging


@asynccontextmanager
async def _app_lifespan(_app: FastAPI):
    from services.api.app.infrastructure.device.cloud_phone_adb_keepalive import (
        cloud_phone_adb_keepalive_enabled,
        jump_adb_keepalive_loop,
    )

    keepalive = None
    if cloud_phone_adb_keepalive_enabled():
        keepalive = asyncio.create_task(jump_adb_keepalive_loop())
    try:
        yield
    finally:
        if keepalive is not None:
            keepalive.cancel()
            try:
                await keepalive
            except asyncio.CancelledError:
                pass


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging()

    app = FastAPI(
        title=settings.app_name,
        debug=settings.debug,
        lifespan=_app_lifespan,
    )
    if settings.cors_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allowed_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.include_router(api_router, prefix=settings.api_v1_prefix)
    app.include_router(api_v2_router, prefix=settings.api_v2_prefix)
    # mobile-demo：仅本地可选模块存在且显式开启时挂载（仓库不跟踪 Demo 页）。
    if settings.workflow.mobile_demo_enabled:
        try:
            from services.api.app.api.v1.mobile_demo import (  # type: ignore[import-not-found]
                require_mobile_demo_access,
            )
            from services.api.app.api.v1.mobile_demo import (  # type: ignore[import-not-found]
                router as mobile_demo_router,
            )
        except ImportError:
            mobile_demo_router = None
        else:
            mobile_demo_prefix = f"{settings.api_v1_prefix}{mobile_demo_router.prefix}"

            @app.middleware("http")
            async def protect_mobile_demo(request: Request, call_next):
                if request.url.path.startswith(mobile_demo_prefix):
                    try:
                        require_mobile_demo_access(request)
                    except HTTPException as exc:
                        return JSONResponse(
                            {"detail": exc.detail},
                            status_code=exc.status_code,
                        )
                return await call_next(request)

            app.include_router(
                mobile_demo_router,
                prefix=settings.api_v1_prefix,
            )
    # 星语 Official：挂在 /v1/*（对齐青云默认路径），与 /api/v1 业务面分离。
    if xingyu_official_docs_enabled():
        from services.api.app.api.v1.xingyu_official.docs import (
            docs_router as xingyu_docs_router,
        )

        app.include_router(xingyu_docs_router)
    if xingyu_official_router_enabled():
        from services.api.app.api.v1.xingyu_official.router import (
            router as xingyu_official_router,
        )

        app.include_router(xingyu_official_router)
    register_exception_handlers(app)
    return app


app = create_app()
