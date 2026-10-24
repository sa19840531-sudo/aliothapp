"""列表离页前发现与详情逻辑页恢复，始终使用当前可信 Capture。"""

from dataclasses import replace

from services.api.app.application.web_page_classification import WEB_DETAIL_PAGE_TYPES
from services.api.app.application.web_run_stage_orchestrator import (
    WebStageEffectResult,
    WebStageFailure,
)
from services.api.app.domain.exploration.web_run_stage_checkpoint import WebRunStage, WebStageCursor


class WebRuntimeStageDiscovery:
    def __init__(self, *, command, evidence, frontier, checkpoints, ads):
        self._command = command
        self._evidence = evidence
        self._frontier = frontier
        self._checkpoints = checkpoints
        self._ads = ads

    async def before_primary_scroll(self, cursor):
        """在 ready 基线仍为当前帧时登记顶部 href，后续滚动不改变候选来源。"""
        classification = replace(cursor, current_stage=WebRunStage.CLASSIFICATION)
        if await self._evidence.find_completed(
            classification, operation="list_href_discovery"
        ) is not None:
            return
        capture_id = cursor.cursor_payload["capture_id"]
        # 菜单全集只由首页签发；其他页仍记录已处理 marker，允许后续详情流程。
        if cursor.logical_page_key == "home":
            await self._frontier.discover(self._command, source_capture_id=capture_id)
        await self._evidence.save_completed(
            classification,
            WebStageEffectResult(cursor_payload={"capture_id": capture_id}),
            operation="list_href_discovery",
        )

    async def before_detail(self, cursor, reference):
        if (
            await self._evidence.find_completed(
                replace(cursor, current_stage=WebRunStage.HREF_DISCOVERY)
            )
            is not None
        ):
            return
        if (
            await self._evidence.find_completed(
                replace(cursor, current_stage=WebRunStage.CLASSIFICATION),
                operation="list_href_discovery",
            )
            is not None
        ):
            return
        raise WebStageFailure("WEB_LIST_DISCOVERY_REQUIRED")

    async def schedule_detail(self, cursor, outcome):
        authoritative_page_type = outcome.authoritative_page_type
        if outcome.authoritative_page_type not in WEB_DETAIL_PAGE_TYPES:
            # 已存在的不可变 marker 缺权威字段时，只接受既有账本的持久分类。
            authority = await self._frontier.detail_authority(self._command, cursor)
            if authority is None:
                # 原列表刷新不是新详情落地，不能因此重复登记广告分支。
                source_url = cursor.cursor_payload.get("current_url")
                if source_url and outcome.cursor_payload.get("current_url") == source_url:
                    return
                # 详情候选实际落到列表页时仍按真实分类采广告，不冒充详情成功。
                capture_id = outcome.cursor_payload.get("capture_id")
                if capture_id and capture_id != cursor.cursor_payload.get("capture_id"):
                    await self._ads.schedule(
                        replace(
                            cursor,
                            logical_page_key="detail:" + cursor.logical_page_key,
                            cursor_payload={**cursor.cursor_payload, **outcome.cursor_payload},
                        ),
                        self._checkpoints,
                    )
                return
            _, authoritative_page_type = authority
        await self._checkpoints.create(
            WebStageCursor.start(
                run_id=cursor.run_id,
                logical_page_key="detail:" + cursor.logical_page_key,
                stage=WebRunStage.HREF_DISCOVERY,
                cursor_payload={
                    "session_id": self._command.session_id,
                    "finalization_epoch": cursor.cursor_payload["finalization_epoch"],
                    **outcome.cursor_payload,
                    "parent_capture_id": cursor.cursor_payload["capture_id"],
                },
            )
        )

    async def execute(self, cursor):
        # 分类阶段内的列表 marker 使用原分类幂等身份，不使用 HREF 阶段身份。
        classification = replace(cursor, current_stage=WebRunStage.CLASSIFICATION)
        listed = await self._evidence.find_completed(
            classification, operation="list_href_discovery"
        )
        if listed is not None:
            detail = await self._evidence.find_completed(classification, operation="detail")
            if (
                detail is not None
                and detail.cursor_payload["capture_id"] != listed.cursor_payload["capture_id"]
            ):
                await self.schedule_detail(
                    replace(
                        cursor, cursor_payload={**cursor.cursor_payload, **listed.cursor_payload}
                    ),
                    detail,
                )
            return
        # 兼容未配置前置发现的运行时；正常路径在主体滚动前已有幂等 marker。
        await self._discover(cursor, cursor.cursor_payload)

    async def _discover(self, cursor, reference):
        if cursor.logical_page_key == "home":
            await self._frontier.discover(
                self._command, source_capture_id=reference["capture_id"]
            )
        await self._ads.schedule(
            replace(cursor, cursor_payload={**cursor.cursor_payload, **reference}),
            self._checkpoints,
        )
