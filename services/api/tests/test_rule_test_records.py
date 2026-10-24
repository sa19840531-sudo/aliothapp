"""规则测试记录服务：存证据和结论、取截图、按勾选或时间段删除（连同截图文件）。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.application.page_capture_persistence import PersistedEvidenceArtifact
from services.api.app.application.rule_sandbox import RuleSandboxFinding, RuleSandboxRunResult
from services.api.app.application.rule_sandbox_evidence import (
    RuleSandboxEvidence,
    SandboxPlatform,
    SandboxScreenshot,
)
from services.api.app.application.rule_test_records import (
    RuleTestRecordDeleteError,
    RuleTestRecordNotFoundError,
    RuleTestRecordService,
)

RULE_ID = "00000000-0000-0000-0000-000000000012"
USER_ID = "00000000-0000-0000-0000-000000000001"


class MemoryStore:
    def __init__(self, fail_on: int | None = None) -> None:
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []
        self.fail_on = fail_on
        self.stores = 0

    async def store(self, *, task_id, page_capture_id, artifact_id, artifact_type, content):
        self.stores += 1
        if self.fail_on == self.stores:
            raise OSError("磁盘满了")
        path = f"{task_id}/{page_capture_id}/{artifact_id}.png"
        self.objects[path] = bytes(content)
        return PersistedEvidenceArtifact(
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            storage_path=path,
            content_type="image/png",
            checksum="sha256:x",
            size_bytes=len(content),
            metadata={},
            created=True,
        )

    async def read(self, storage_path: str) -> bytes:
        if storage_path not in self.objects:
            raise FileNotFoundError(storage_path)
        return self.objects[storage_path]

    async def delete(self, storage_path: str) -> None:
        self.deleted.append(storage_path)
        if storage_path == "broken/path.png":
            raise OSError("删不掉")
        self.objects.pop(storage_path, None)


class MemoryRepository:
    def __init__(self, insert_error: Exception | None = None) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.insert_error = insert_error
        self.delete_calls: list[dict[str, Any]] = []

    async def insert_record(self, record: dict[str, Any]) -> None:
        if self.insert_error is not None:
            raise self.insert_error
        self.rows[record["id"]] = record

    async def list_records(self, **kwargs: Any):
        return list(self.rows.values()), len(self.rows)

    async def get_record(self, record_id: str):
        return self.rows.get(record_id)

    async def get_screenshots(self, record_id: str):
        row = self.rows.get(record_id)
        return None if row is None else row["screenshots"]

    async def delete_records(self, *, record_ids, created_from, created_to):
        self.delete_calls.append(
            {"record_ids": record_ids, "created_from": created_from, "created_to": created_to}
        )
        targets = record_ids or list(self.rows)
        return [
            {"id": key, "screenshots": self.rows.pop(key)["screenshots"]}
            for key in targets
            if key in self.rows
        ]


def _evidence(shots: int = 2) -> RuleSandboxEvidence:
    return RuleSandboxEvidence(
        platform=SandboxPlatform.WEB,
        page_url=" https://example.test/a ",
        page_title="详情",
        page_type="content_detail",
        page_state="stable",
        visible_text="操作成功！",
        dom_html="<html></html>",
        ui_snapshot={"schema_version": "ui-snapshot.v2", "elements": []},
        judge_input={"discount_price": 9.9},
        screenshots=tuple(
            SandboxScreenshot(
                filename=f"shot-{index}.png",
                payload=b"\x89PNG" + bytes([index]),
                width=390,
                height=844,
            )
            for index in range(1, shots + 1)
        ),
    )


def _result() -> RuleSandboxRunResult:
    return RuleSandboxRunResult(
        rule_id=RULE_ID,
        rule_code="alioth_12",
        rule_name="提示文案叹号结尾检查",
        executor_type="hybrid",
        rule_status="enabled",
        check_mode="relaxed",
        outcome="fail",
        result_summary="提示文字以叹号结尾",
        problem_desc="「操作成功！」以叹号结尾",
        confidence=0.9,
        confidence_threshold=None,
        findings=(RuleSandboxFinding(summary="叹号结尾", evidence=("页面文字",)),),
        missing_evidence=(),
        used_evidence=("页面文字",),
        notices=(),
        duration_ms=1200,
        judge_input={"page": {}},
        judge_output={"conclusion": "fail"},
    )


def _service(repository: MemoryRepository, store: MemoryStore) -> RuleTestRecordService:
    return RuleTestRecordService(repository=repository, artifact_store_factory=lambda: store)


async def test_save_keeps_screenshots_in_storage_and_the_rest_in_the_record() -> None:
    repository, store = MemoryRepository(), MemoryStore()

    record_id = await _service(repository, store).save(
        tester_id=USER_ID, tester_name="规则配置员", evidence=_evidence(), result=_result()
    )

    row = repository.rows[record_id]
    assert sorted(store.objects) == [
        f"rule-tests/{record_id}/screenshot-1.png",
        f"rule-tests/{record_id}/screenshot-2.png",
    ]
    assert row["screenshots"][0] == {
        "filename": "shot-1.png",
        "width": 390,
        "height": 844,
        "size_bytes": 5,
        "storage_path": f"rule-tests/{record_id}/screenshot-1.png",
        "capture_role": "primary",
    }
    assert row["tester_user_id"] == USER_ID
    assert row["platform"] == "web"
    assert row["page_url"] == "https://example.test/a"
    assert row["dom_html"] == "<html></html>"
    assert row["judge_input"] == {"discount_price": 9.9}
    assert (row["outcome"], row["result_summary"]) == ("fail", "提示文字以叹号结尾")
    # 结论整份留档，和测试接口返回同形
    assert row["result_payload"]["findings"][0] == {
        "summary": "叹号结尾",
        "evidence": ("页面文字",),
    }
    assert row["result_payload"]["judge_output"] == {"conclusion": "fail"}


async def test_save_keeps_before_screenshot_and_second_location_text() -> None:
    repository, store = MemoryRepository(), MemoryStore()
    evidence = RuleSandboxEvidence(
        platform=SandboxPlatform.H5,
        visible_text="月卡 ¥30",
        screenshots=_evidence(1).screenshots,
        before_screenshot=SandboxScreenshot(
            filename="before.png", payload=b"\x89PNG-before", width=390, height=844
        ),
        second_location_text="开通月卡 ¥30",
    )

    record_id = await _service(repository, store).save(
        tester_id=USER_ID, tester_name="规则配置员", evidence=evidence, result=_result()
    )

    row = repository.rows[record_id]
    assert [(item["filename"], item["capture_role"]) for item in row["screenshots"]] == [
        ("shot-1.png", "primary"),
        ("before.png", "before"),
    ]
    assert store.objects[f"rule-tests/{record_id}/before-screenshot.png"] == b"\x89PNG-before"
    assert row["second_location_text"] == "开通月卡 ¥30"
    # 取图按这组的顺序编号，操作前的截图排在页面截图后面
    service = _service(repository, store)
    assert await service.read_screenshot(record_id, 2) == b"\x89PNG-before"
    assert await service.delete(record_ids=[record_id], created_from=None, created_to=None) == 1
    assert store.objects == {}


async def test_save_without_a_real_account_id_leaves_tester_link_empty() -> None:
    repository = MemoryRepository()

    record_id = await _service(repository, MemoryStore()).save(
        tester_id="user-1", tester_name="测试替身", evidence=_evidence(0), result=_result()
    )

    assert repository.rows[record_id]["tester_user_id"] is None
    assert repository.rows[record_id]["screenshots"] == []


async def test_failed_save_removes_the_screenshots_it_already_stored() -> None:
    repository, store = MemoryRepository(RuntimeError("表不存在")), MemoryStore()

    with pytest.raises(RuntimeError, match="表不存在"):
        await _service(repository, store).save(
            tester_id=USER_ID, tester_name="x", evidence=_evidence(), result=_result()
        )
    assert store.objects == {}
    assert len(store.deleted) == 2

    half_store = MemoryStore(fail_on=2)
    with pytest.raises(OSError, match="磁盘满了"):
        await _service(MemoryRepository(), half_store).save(
            tester_id=USER_ID, tester_name="x", evidence=_evidence(), result=_result()
        )
    assert half_store.objects == {}


async def test_storage_config_problem_only_breaks_saving() -> None:
    def broken_factory():
        raise ValueError("MinIO 证据存储缺少配置")

    service = RuleTestRecordService(
        repository=MemoryRepository(), artifact_store_factory=broken_factory
    )

    with pytest.raises(ValueError, match="MinIO"):
        await service.save(
            tester_id=USER_ID, tester_name="x", evidence=_evidence(), result=_result()
        )


async def test_read_screenshot_by_position_and_plain_not_found_errors() -> None:
    repository, store = MemoryRepository(), MemoryStore()
    service = _service(repository, store)
    record_id = await service.save(
        tester_id=USER_ID, tester_name="x", evidence=_evidence(), result=_result()
    )

    assert await service.read_screenshot(record_id, 2) == b"\x89PNG\x02"
    with pytest.raises(RuleTestRecordNotFoundError, match="第 3 张"):
        await service.read_screenshot(record_id, 3)
    with pytest.raises(RuleTestRecordNotFoundError, match="不存在"):
        await service.read_screenshot("00000000-0000-0000-0000-000000000999", 1)
    with pytest.raises(RuleTestRecordNotFoundError, match="不存在"):
        await service.read_screenshot("not-a-uuid", 1)
    store.objects.clear()
    with pytest.raises(RuleTestRecordNotFoundError, match="文件已经不在"):
        await service.read_screenshot(record_id, 1)
    with pytest.raises(RuleTestRecordNotFoundError):
        await service.get_record("not-a-uuid")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"record_ids": [], "created_from": None, "created_to": None}, "请勾选"),
        (
            {"record_ids": [RULE_ID], "created_from": datetime(2026, 9, 1, tzinfo=UTC),
             "created_to": None},
            "只能选一种",
        ),
        ({"record_ids": ["abc"], "created_from": None, "created_to": None}, "编号不对"),
        (
            {"record_ids": [], "created_from": datetime(2026, 9, 2, tzinfo=UTC),
             "created_to": datetime(2026, 9, 1, tzinfo=UTC)},
            "开始时间要早于结束时间",
        ),
    ],
)
async def test_delete_requires_exactly_one_clear_condition(kwargs, message) -> None:
    repository = MemoryRepository()

    with pytest.raises(RuleTestRecordDeleteError, match=message):
        await _service(repository, MemoryStore()).delete(**kwargs)
    assert repository.delete_calls == []


async def test_delete_removes_records_then_their_screenshot_files() -> None:
    repository, store = MemoryRepository(), MemoryStore()
    service = _service(repository, store)
    first = await service.save(
        tester_id=USER_ID, tester_name="x", evidence=_evidence(), result=_result()
    )
    second = await service.save(
        tester_id=USER_ID, tester_name="x", evidence=_evidence(1), result=_result()
    )
    # 文件删不掉只记日志，不影响记录删除结果
    repository.rows[second]["screenshots"].append({"storage_path": "broken/path.png"})

    deleted = await service.delete(
        record_ids=[first, f" {first} ", second], created_from=None, created_to=None
    )

    assert deleted == 2
    assert repository.delete_calls[0]["record_ids"] == [first, second]
    assert repository.rows == {}
    assert store.objects == {}
    assert "broken/path.png" in store.deleted


async def test_delete_by_time_range_passes_bounds_through() -> None:
    repository, store = MemoryRepository(), MemoryStore()
    service = _service(repository, store)
    await service.save(tester_id=USER_ID, tester_name="x", evidence=_evidence(), result=_result())
    until = datetime(2026, 9, 12, tzinfo=UTC)

    deleted = await service.delete(record_ids=[], created_from=None, created_to=until)

    assert deleted == 1
    assert repository.delete_calls[0] == {
        "record_ids": [],
        "created_from": None,
        "created_to": until,
    }
    assert store.objects == {}
