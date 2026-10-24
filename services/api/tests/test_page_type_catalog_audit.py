"""Catalog 版本与摘要必须进入运行审计。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from services.api.app.agents.tools.page_detection_tools import PageDetectionResult
from services.api.app.domain.page.registry import get_default_page_type_registry
from services.api.app.infrastructure.queue.redis_queue import AgentRunRecord
from services.api.app.infrastructure.queue.state_store import _input_context


def test_page_detection_result_contains_catalog_digest() -> None:
    """页面识别结果默认携带 Catalog 内容摘要。"""
    registry = get_default_page_type_registry()

    result = PageDetectionResult(
        page_capture_id=str(uuid4()),
        detection_status="error",
        page_state="unknown",
        confidence=0,
        reason="测试错误结果",
        catalog_version=registry.catalog.version,
        model="test-model",
    )

    assert result.catalog_sha256 == registry.content_sha256


def test_agent_run_input_context_contains_catalog_audit() -> None:
    """Agent Run 输入上下文携带 Catalog 版本与内容摘要。"""
    registry = get_default_page_type_registry()
    record = AgentRunRecord(
        run_id=str(uuid4()),
        message="执行质检",
        submitted_at=datetime(2026, 7, 30, 10, 0, tzinfo=UTC),
    )

    context = _input_context(record)

    assert context["page_type_catalog"] == {
        "version": registry.catalog.version,
        "sha256": registry.content_sha256,
    }
