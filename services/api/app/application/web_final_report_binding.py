"""Web V2 正式报告不可变绑定的统一校验合同。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from services.api.app.application.report_payloads import stable_report_payload_hash
from services.api.app.domain.exploration.web_coverage_snapshot import (
    WebCoverageSnapshot,
)


class WebFinalReportBindingError(ValueError):
    """正式报告正文与其 Gate、覆盖快照或身份绑定不一致。"""


@dataclass(frozen=True)
class WebFinalReportBindingExpectation:
    """持久化绑定表给出的不可变预期值。"""

    task_id: str
    agent_run_id: str | None = None
    report_id: str | None = None
    report_version: int | None = None
    report_payload_hash: str | None = None
    gate_snapshot_id: str | None = None
    finalization_epoch: int | None = None
    coverage_snapshot_id: str | None = None
    coverage_snapshot_hash: str | None = None
    coverage_snapshot: WebCoverageSnapshot | None = None


def is_web_v2_final_contract(payload: object) -> bool:
    """只识别显式冻结的 Web V2/full/enforced 正式报告合同。"""

    if not isinstance(payload, dict):
        return False
    contract = payload.get("report_contract")
    return (
        isinstance(contract, dict)
        and str(contract.get("target_platform") or "").casefold() == "web"
        and str(contract.get("workflow_version") or "").casefold() == "v2"
        and contract.get("execution_mode") == "full_inspection"
        and contract.get("notification_mode") == "enforced"
    )


def is_database_web_v2_formal(
    *,
    target_platform: object,
    task_config: object,
) -> bool:
    """仅以数据库冻结 Task 身份路由 Web 正式报告校验。"""

    return (
        str(target_platform or "").strip().casefold() == "web"
        and isinstance(task_config, dict)
        and str(task_config.get("workflow_version") or "").strip().casefold()
        == "v2"
        and task_config.get("execution_mode") == "full_inspection"
        and task_config.get("web_inspection_notification_mode") == "enforced"
    )


def validate_web_final_report_binding(
    payload: dict[str, Any],
    *,
    expectation: WebFinalReportBindingExpectation,
) -> None:
    """校验正文身份、版本/哈希、Gate/epoch 与覆盖快照绑定。"""

    if not is_web_v2_final_contract(payload):
        raise WebFinalReportBindingError("报告不是 Web V2 full/enforced 冻结合同")
    if payload.get("report_stage") != "final" or payload.get("is_final") is not True:
        raise WebFinalReportBindingError("报告阶段不是 final")
    task = payload.get("task")
    gate = payload.get("completion_gate")
    exploration = payload.get("page_exploration")
    coverage = exploration.get("web_coverage") if isinstance(exploration, dict) else None
    if not isinstance(task, dict) or task.get("task_id") != expectation.task_id:
        raise WebFinalReportBindingError("task identity 不一致")
    run_id = str(payload.get("agent_run_id") or "")
    if not run_id or (
        expectation.agent_run_id is not None and run_id != expectation.agent_run_id
    ):
        raise WebFinalReportBindingError("run identity 不一致")
    canonical_report_id = f"task:{expectation.task_id}:run:{run_id}"
    if (
        expectation.report_id is not None
        and expectation.report_id != canonical_report_id
    ):
        raise WebFinalReportBindingError("report identity 未绑定当前 task/run")
    expected_report_id = expectation.report_id or canonical_report_id
    if payload.get("report_id") != expected_report_id:
        raise WebFinalReportBindingError("report identity 不一致")
    version = payload.get("report_binding_version")
    if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
        raise WebFinalReportBindingError("report version 无效")
    if expectation.report_version is not None and version != expectation.report_version:
        raise WebFinalReportBindingError("report version 不一致")
    if (
        expectation.report_payload_hash is not None
        and stable_report_payload_hash(payload) != expectation.report_payload_hash
    ):
        raise WebFinalReportBindingError("report payload hash 不一致")
    if not isinstance(gate, dict) or gate.get("gate_passed") is not True:
        raise WebFinalReportBindingError("Completion Gate 未通过")
    gate_id = str(gate.get("gate_snapshot_id") or "")
    epoch = gate.get("finalization_epoch")
    if not gate_id or isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
        raise WebFinalReportBindingError("Gate identity 无效")
    if expectation.gate_snapshot_id is not None and gate_id != expectation.gate_snapshot_id:
        raise WebFinalReportBindingError("Gate identity 不一致")
    if expectation.finalization_epoch is not None and epoch != expectation.finalization_epoch:
        raise WebFinalReportBindingError("finalization epoch 不一致")
    explanation = exploration.get("web_report_explanation") if isinstance(exploration, dict) else None
    if explanation is not None and (
        not isinstance(explanation, dict)
        or explanation.get("schema_version") != "web-report-explanation.v1"
        or explanation.get("task_id") != expectation.task_id
        or explanation.get("agent_run_id") != run_id
        or type(explanation.get("finalization_epoch")) is not int
        or explanation.get("finalization_epoch") != epoch
    ):
        raise WebFinalReportBindingError("Web 报告解释与冻结 Task/Run/Epoch 不一致")
    if not isinstance(coverage, dict):
        raise WebFinalReportBindingError("缺少冻结 Web coverage")
    coverage_id = str(coverage.get("coverage_snapshot_id") or "")
    coverage_hash = str(coverage.get("snapshot_hash") or "")
    if not coverage_id or not coverage_hash:
        raise WebFinalReportBindingError("coverage identity/hash 无效")
    if coverage.get("finalization_epoch") != epoch:
        raise WebFinalReportBindingError("coverage epoch 与 Gate 不一致")
    if (
        expectation.coverage_snapshot_id is not None
        and coverage_id != expectation.coverage_snapshot_id
    ):
        raise WebFinalReportBindingError("coverage identity 不一致")
    if (
        expectation.coverage_snapshot_hash is not None
        and coverage_hash != expectation.coverage_snapshot_hash
    ):
        raise WebFinalReportBindingError("coverage hash 不一致")
    if expectation.coverage_snapshot is not None:
        _validate_coverage_material(
            coverage,
            expected=expectation.coverage_snapshot,
        )


def _validate_coverage_material(
    payload: dict[str, Any],
    *,
    expected: WebCoverageSnapshot,
) -> None:
    """重算数据库行与正文 Coverage，并逐字段校验不可变 material。"""

    try:
        validated_expected = WebCoverageSnapshot(
            snapshot_id=expected.snapshot_id,
            task_id=expected.task_id,
            agent_run_id=expected.agent_run_id,
            finalization_epoch=expected.finalization_epoch,
            required_page_types=expected.required_page_types,
            observed_page_types=expected.observed_page_types,
            missing_page_types=expected.missing_page_types,
            unrecognized_page_types=expected.unrecognized_page_types,
            normalization_version=expected.normalization_version,
            coverage_status=expected.coverage_status,
            snapshot_hash=expected.snapshot_hash,
            created_at=expected.created_at,
            optional_page_types=expected.optional_page_types,
            not_applicable_page_types=expected.not_applicable_page_types,
            applicability_mode=expected.applicability_mode,
            catalog_version=expected.catalog_version,
            catalog_hash=expected.catalog_hash,
            snapshot_protocol_version=expected.snapshot_protocol_version,
            observed_lifecycle_page_types=(
                expected.observed_lifecycle_page_types
            ),
            unknown_page_capture_ids=expected.unknown_page_capture_ids,
            supplemental_evidence_summary=(
                expected.supplemental_evidence_summary
            ),
        )
        applicability = payload.get("applicability_snapshot")
        if not isinstance(applicability, dict):
            raise ValueError("缺少 applicability_snapshot")
        required = _coverage_tuple(payload, "required_page_types")
        optional = _coverage_tuple(applicability, "optional_page_types")
        not_applicable = _coverage_tuple(
            applicability,
            "not_applicable_page_types",
        )
        if required != _coverage_tuple(applicability, "required_page_types"):
            raise ValueError("required_page_types 双份投影不一致")
        observed = _coverage_tuple(payload, "observed_page_types")
        missing = _coverage_tuple(payload, "missing_page_types")
        unrecognized = _coverage_tuple(payload, "unrecognized_page_types")
        protocol = _coverage_text(applicability, "snapshot_protocol_version")
        lifecycle: tuple[str, ...] = ()
        unknown_capture_ids: tuple[str, ...] = ()
        supplemental: dict[str, int] = {}
        if protocol == "web-coverage-snapshot.v4":
            for field_name, top_level_value in (
                ("observed_page_types", observed),
                ("missing_page_types", missing),
                ("unrecognized_page_types", unrecognized),
            ):
                if _coverage_tuple(applicability, field_name) != top_level_value:
                    raise ValueError(f"{field_name} 双份投影不一致")
            lifecycle = _coverage_tuple(
                payload,
                "observed_lifecycle_page_types",
            )
            unknown_capture_ids = _coverage_tuple(
                payload,
                "unknown_page_capture_ids",
            )
            supplemental = _coverage_integer_summary(
                payload,
                "supplemental_evidence_summary",
            )
            if lifecycle != _coverage_tuple(
                applicability,
                "lifecycle_observations",
            ):
                raise ValueError("lifecycle_observations 双份投影不一致")
            if unknown_capture_ids != _coverage_tuple(
                applicability,
                "unknown_page_capture_ids",
            ):
                raise ValueError("unknown_page_capture_ids 双份投影不一致")
            if supplemental != _coverage_integer_summary(
                applicability,
                "supplemental_evidence_summary",
            ):
                raise ValueError("supplemental_evidence_summary 双份投影不一致")
        validated_payload = WebCoverageSnapshot(
            snapshot_id=str(payload.get("coverage_snapshot_id") or ""),
            task_id=validated_expected.task_id,
            agent_run_id=validated_expected.agent_run_id,
            finalization_epoch=_coverage_integer(payload, "finalization_epoch"),
            required_page_types=required,
            observed_page_types=observed,
            missing_page_types=missing,
            unrecognized_page_types=unrecognized,
            normalization_version=_coverage_text(
                payload,
                "normalization_version",
            ),
            coverage_status=_coverage_text(payload, "coverage_status"),
            snapshot_hash=_coverage_text(payload, "snapshot_hash"),
            created_at=validated_expected.created_at,
            optional_page_types=optional,
            not_applicable_page_types=not_applicable,
            applicability_mode=_coverage_text(
                applicability,
                "applicability_mode",
            ),
            catalog_version=_coverage_text(applicability, "catalog_version"),
            catalog_hash=_coverage_text(applicability, "catalog_hash"),
            snapshot_protocol_version=protocol,
            observed_lifecycle_page_types=lifecycle,
            unknown_page_capture_ids=unknown_capture_ids,
            supplemental_evidence_summary=supplemental,
        )
    except (TypeError, ValueError, AttributeError) as exc:
        raise WebFinalReportBindingError(
            "Coverage Snapshot 不完整或哈希重算失败"
        ) from exc
    if validated_payload.hash_material() != validated_expected.hash_material():
        raise WebFinalReportBindingError("coverage 不可变 material 不一致")


def _coverage_tuple(payload: dict[str, Any], key: str) -> tuple[str, ...]:
    value = payload.get(key)
    if not isinstance(value, list | tuple) or any(
        not isinstance(item, str) for item in value
    ):
        raise ValueError(f"coverage.{key} 必须为字符串数组")
    return tuple(value)


def _coverage_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"coverage.{key} 不能为空")
    return value


def _coverage_integer(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"coverage.{key} 必须为非负整数")
    return value


def _coverage_integer_summary(
    payload: dict[str, Any],
    key: str,
) -> dict[str, int]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"coverage.{key} 必须为对象")
    result: dict[str, int] = {}
    for name, count in value.items():
        if (
            not isinstance(name, str)
            or not name.strip()
            or name != name.strip()
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count < 0
        ):
            raise ValueError(f"coverage.{key} 必须为非负整数映射")
        result[name] = count
    return dict(sorted(result.items()))
