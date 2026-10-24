"""规则测试记录：每次人工证据测试的结论和当时的证据留档。

对应管理后台「规则配置 → 规则测试 → 测试记录」，体验中心管理员都能看、能拿原证据重测、能批量删。
- 截图存证据存储（rule-tests/<记录>/screenshot-N.png，操作前的截图是 before-screenshot.png），
  其余证据和结论存 qc_rule_test_record；
- 存记录失败不影响测试本身，由调用方提示没存上；
- 不自动清理：按勾选或按时间段删除，删记录时一并删截图文件。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID, uuid4

from services.api.app.application.page_capture_persistence import EvidenceArtifactStore
from services.api.app.application.rule_sandbox import RuleSandboxRunResult
from services.api.app.application.rule_sandbox_evidence import (
    BEFORE_ROLE,
    BEFORE_SCREENSHOT_ARTIFACT_ID,
    RuleSandboxEvidence,
)

logger = logging.getLogger(__name__)

# 截图在证据存储里的目录：rule-tests/<记录 ID>/screenshot-N.png
RECORD_STORAGE_NAMESPACE = "rule-tests"
# 一次最多勾选删除的条数
MAX_DELETE_IDS = 1000


class RuleTestRecordNotFoundError(LookupError):
    """记录或截图不存在（可能已被删除）。"""


class RuleTestRecordDeleteError(ValueError):
    """删除条件不对。"""


class RuleTestRecordRepository(Protocol):
    """记录读写端口（PostgresRuleTestRecordRepository 结构性满足）。"""

    async def insert_record(self, record: dict[str, Any]) -> None: ...

    async def list_records(
        self,
        *,
        keyword: str | None,
        outcome: str | None,
        created_from: datetime | None,
        created_to: datetime | None,
        page: int,
        page_size: int,
    ) -> tuple[list[dict[str, Any]], int]: ...

    async def get_record(self, record_id: str) -> dict[str, Any] | None: ...

    async def get_screenshots(self, record_id: str) -> list[dict[str, Any]] | None: ...

    async def delete_records(
        self,
        *,
        record_ids: list[str],
        created_from: datetime | None,
        created_to: datetime | None,
    ) -> list[dict[str, Any]]: ...


class RuleTestRecordService:
    """测试记录的保存、查询、取截图与批量删除。"""

    def __init__(
        self,
        *,
        repository: RuleTestRecordRepository,
        artifact_store_factory: Callable[[], EvidenceArtifactStore],
    ) -> None:
        self._repository = repository
        # 证据存储用到时再建：存储配置有问题只影响存记录，不影响测试本身
        self._artifact_store_factory = artifact_store_factory
        self._artifact_store: EvidenceArtifactStore | None = None

    async def save(
        self,
        *,
        tester_id: str,
        tester_name: str,
        evidence: RuleSandboxEvidence,
        result: RuleSandboxRunResult,
    ) -> str:
        """把这次测试的证据和结论存成一条记录，返回记录 ID；中途失败会删掉已存的截图再抛出。"""
        record_id = str(uuid4())
        stored_paths: list[str] = []
        try:
            store = self._store()
            # 页面截图和操作前的截图放同一组、按采集角色区分；取图时按这组的顺序编号
            shots = [
                (f"screenshot-{index}", "primary", screenshot)
                for index, screenshot in enumerate(evidence.screenshots, start=1)
            ]
            if evidence.before_screenshot is not None:
                shots.append(
                    (BEFORE_SCREENSHOT_ARTIFACT_ID, BEFORE_ROLE, evidence.before_screenshot)
                )
            screenshots: list[dict[str, Any]] = []
            for artifact_id, capture_role, screenshot in shots:
                persisted = await store.store(
                    task_id=RECORD_STORAGE_NAMESPACE,
                    page_capture_id=record_id,
                    artifact_id=artifact_id,
                    artifact_type="screenshot",
                    content=screenshot.payload,
                )
                stored_paths.append(persisted.storage_path)
                screenshots.append(
                    {
                        "filename": screenshot.filename,
                        "width": screenshot.width,
                        "height": screenshot.height,
                        "size_bytes": persisted.size_bytes,
                        "storage_path": persisted.storage_path,
                        "capture_role": capture_role,
                    }
                )
            await self._repository.insert_record(
                {
                    "id": record_id,
                    "rule_id": result.rule_id,
                    "rule_code": result.rule_code,
                    "rule_name": result.rule_name,
                    "tester_user_id": tester_id if _is_uuid(tester_id) else None,
                    "tester_name": tester_name,
                    "platform": evidence.platform.value,
                    "check_mode": result.check_mode,
                    "page_type": evidence.page_type.strip(),
                    "page_state": evidence.page_state.strip(),
                    "page_url": evidence.page_url.strip(),
                    "page_title": evidence.page_title.strip(),
                    "visible_text": evidence.visible_text,
                    "dom_html": evidence.dom_html,
                    "ui_snapshot": evidence.ui_snapshot or None,
                    "judge_input": dict(evidence.judge_input),
                    "second_location_text": evidence.second_location_text,
                    "screenshots": screenshots,
                    "outcome": result.outcome,
                    "result_summary": result.result_summary,
                    # 与测试接口返回的结果同形，记录详情直接按结果卡片展示
                    "result_payload": asdict(result),
                }
            )
        except Exception:
            await self._delete_files(stored_paths)
            raise
        return record_id

    async def list_records(
        self,
        *,
        keyword: str | None,
        outcome: str | None,
        created_from: datetime | None,
        created_to: datetime | None,
        page: int,
        page_size: int,
    ) -> tuple[list[dict[str, Any]], int]:
        return await self._repository.list_records(
            keyword=keyword,
            outcome=outcome,
            created_from=created_from,
            created_to=created_to,
            page=page,
            page_size=page_size,
        )

    async def get_record(self, record_id: str) -> dict[str, Any]:
        record = await self._repository.get_record(record_id) if _is_uuid(record_id) else None
        if record is None:
            raise RuleTestRecordNotFoundError("这条测试记录不存在，可能已经被删除")
        return record

    async def read_screenshot(self, record_id: str, index: int) -> bytes:
        """按记录里的顺序（从 1 开始）读一张截图的 PNG。"""
        screenshots = (
            await self._repository.get_screenshots(record_id) if _is_uuid(record_id) else None
        )
        if screenshots is None:
            raise RuleTestRecordNotFoundError("这条测试记录不存在，可能已经被删除")
        if not 1 <= index <= len(screenshots):
            raise RuleTestRecordNotFoundError(f"这条记录没有第 {index} 张截图")
        try:
            return await self._store().read(str(screenshots[index - 1].get("storage_path") or ""))
        except FileNotFoundError as exc:
            raise RuleTestRecordNotFoundError("截图文件已经不在了") from exc

    async def delete(
        self,
        *,
        record_ids: list[str],
        created_from: datetime | None,
        created_to: datetime | None,
    ) -> int:
        """按勾选或按时间段（二选一）删记录和截图文件，返回删掉的条数。"""
        ids = list(dict.fromkeys(item.strip() for item in record_ids if item.strip()))
        by_time = created_from is not None or created_to is not None
        if ids and by_time:
            raise RuleTestRecordDeleteError("勾选删除和按时间删除只能选一种")
        if not ids and not by_time:
            raise RuleTestRecordDeleteError("请勾选要删除的记录，或者选一个时间段")
        if len(ids) > MAX_DELETE_IDS:
            raise RuleTestRecordDeleteError(f"一次最多删除 {MAX_DELETE_IDS} 条")
        if any(not _is_uuid(item) for item in ids):
            raise RuleTestRecordDeleteError("要删除的记录编号不对，请刷新列表后重试")
        if created_from is not None and created_to is not None and created_from >= created_to:
            raise RuleTestRecordDeleteError("开始时间要早于结束时间")
        deleted = await self._repository.delete_records(
            record_ids=ids,
            created_from=created_from,
            created_to=created_to,
        )
        await self._delete_files(
            [
                str(screenshot.get("storage_path") or "")
                for row in deleted
                for screenshot in row["screenshots"]
                if isinstance(screenshot, dict)
            ]
        )
        return len(deleted)

    def _store(self) -> EvidenceArtifactStore:
        if self._artifact_store is None:
            self._artifact_store = self._artifact_store_factory()
        return self._artifact_store

    async def _delete_files(self, paths: list[str]) -> None:
        # 记录已经删掉（或没存成），文件删不掉只占空间，记日志不打断
        for path in paths:
            if not path:
                continue
            try:
                await self._store().delete(path)
            except Exception:  # noqa: BLE001 - 删文件失败不影响记录删除结果
                logger.warning("规则测试记录截图删除失败：%s", path, exc_info=True)


def _is_uuid(value: str) -> bool:
    try:
        UUID(str(value))
    except (TypeError, ValueError):
        return False
    return True
