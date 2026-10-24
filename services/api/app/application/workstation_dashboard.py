"""工作台首页聚合用例。"""

from typing import Protocol

from services.api.app.domain.iam.access_scope import AccessScope


class WorkstationDashboardRepository(Protocol):
    """工作台首页聚合仓储契约。"""

    async def get_dashboard_overview(self, *, access_scope: AccessScope) -> dict:
        """查询首页聚合数据。"""


class WorkstationDashboardService:
    """工作台首页聚合服务。"""

    def __init__(self, *, repository: WorkstationDashboardRepository) -> None:
        self._repository = repository

    async def get_overview(self, *, access_scope: AccessScope) -> dict:
        overview = await self._repository.get_dashboard_overview(access_scope=access_scope)
        # 本月 Token（2.0 原型首页卡片）：用量表不可用时不拖垮整个首页。
        usage_reader = getattr(self._repository, "get_month_token_usage", None)
        token_usage = None
        if usage_reader is not None:
            try:
                token_usage = await usage_reader(access_scope=access_scope)
            except Exception:  # noqa: BLE001
                token_usage = None
        overview["token_usage"] = token_usage
        return overview
