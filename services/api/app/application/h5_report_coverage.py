"""H5 报告覆盖投影与门禁绑定：只消费有身份、有内容哈希的封存快照。"""

from collections.abc import Mapping
from typing import Any

from services.api.app.domain.exploration.h5_coverage import H5CoverageSnapshot


def validated_h5_report_coverage(
    coverage: Mapping[str, Any], *, task_id: str, agent_run_id: str,
) -> dict[str, Any]:
    """校验原始覆盖内容，保留事实周期与封存周期的区别。"""

    if coverage.get("task_id") != task_id or coverage.get("agent_run_id") != agent_run_id:
        raise ValueError("H5 Coverage 的 Task/Run 与报告不一致")
    snapshot_id = coverage.get("coverage_snapshot_id")
    epoch = coverage.get("finalization_epoch")
    if not isinstance(snapshot_id, str) or not snapshot_id.strip():
        raise ValueError("H5 报告缺少 Coverage Snapshot 身份")
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
        raise ValueError("H5 报告缺少有效 Coverage Epoch")
    snapshot = H5CoverageSnapshot.from_dict(coverage)
    return {
        **snapshot.to_dict(),
        "task_id": task_id,
        "agent_run_id": agent_run_id,
        "coverage_snapshot_id": snapshot_id,
        "finalization_epoch": epoch,
        "gate_snapshot_id": coverage.get("gate_snapshot_id"),
        "gate_finalization_epoch": coverage.get("gate_finalization_epoch"),
    }


def h5_page_exploration_for_report(
    summary: Mapping[str, Any], *, coverage: Mapping[str, Any],
    task_id: str, agent_run_id: str,
) -> dict[str, Any]:
    """Coverage 提供页面范围事实，任务终态保留时间线的完整收口结论。"""

    frozen = validated_h5_report_coverage(coverage, task_id=task_id, agent_run_id=agent_run_id)
    coverage_complete = (
        frozen["completion_ready"] is True and bool(frozen["required_page_types"])
    )
    timeline_complete = summary.get("complete")
    complete = (
        timeline_complete
        if coverage_complete and isinstance(timeline_complete, bool)
        else coverage_complete
    )
    quality_status = str(
        summary.get("quality_status")
        or ("complete" if complete else "needs_review")
    )
    requires_review = summary.get("requires_review")
    if not isinstance(requires_review, bool):
        requires_review = not complete
    if not coverage_complete:
        complete = False
        quality_status = "needs_review"
        requires_review = True
    return {
        **summary,
        "tracking_available": True,
        "complete": complete,
        "quality_status": quality_status,
        "requires_review": requires_review,
        "h5_coverage": frozen,
    }


def bind_h5_report_coverage(
    report: dict[str, Any], *, coverage: Mapping[str, Any], task_id: str,
    agent_run_id: str, gate_snapshot_id: str, gate_finalization_epoch: int,
) -> None:
    """封存事务中比对 Gate 实际输入；禁止用新快照覆盖旧报告正文。"""

    task = report.get("task")
    if (
        not isinstance(task, dict) or task.get("task_id") != task_id
        or report.get("agent_run_id") != agent_run_id
    ):
        raise ValueError("H5 报告正文的 Task/Run 不一致")
    gate = report.get("completion_gate")
    if (
        report.get("is_final") is not True or report.get("report_stage") != "final"
        or not isinstance(gate, dict) or gate.get("gate_passed") is not True
        or gate.get("gate_snapshot_id") != gate_snapshot_id
        or type(gate.get("finalization_epoch")) is not int
        or gate["finalization_epoch"] != gate_finalization_epoch
    ):
        raise ValueError("H5 报告正文的 Gate/封存 Epoch 不一致")
    exploration = report.get("page_exploration")
    if not isinstance(exploration, dict) or not isinstance(exploration.get("h5_coverage"), dict):
        raise ValueError("H5 正式报告缺少 Coverage Snapshot")
    expected = validated_h5_report_coverage(coverage, task_id=task_id, agent_run_id=agent_run_id)
    actual = validated_h5_report_coverage(
        exploration["h5_coverage"], task_id=task_id, agent_run_id=agent_run_id,
    )
    for key, value in expected.items():
        if key not in {"gate_snapshot_id", "gate_finalization_epoch"} and actual[key] != value:
            raise ValueError(f"H5 报告 Coverage 与 Gate 输入不一致：{key}")
    if not expected["completion_ready"] or not expected["required_page_types"]:
        raise ValueError("H5 正式报告 Coverage 尚未完成")
    if exploration.get("complete") is not True:
        raise ValueError("H5 正式报告探索结论与 Coverage 不一致")
    exploration["h5_coverage"] = {
        **actual,
        "gate_snapshot_id": gate_snapshot_id,
        "gate_finalization_epoch": gate_finalization_epoch,
    }
