"""规则执行结果写回用例。"""

import json
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from services.api.app.application.judgment_wording_policy import (
    enforce_plain_display_text,
    neutralize_business_characterizations,
)
from services.api.app.domain.inspection.rule_reuse import (
    LOOKUP_REUSE_SCOPES,
    REUSABLE_SOURCE_STATUSES,
    RULE_REUSE_RECORD_PROTOCOL_VERSION,
)

logger = logging.getLogger(__name__)

_PLAIN_SUMMARY_FALLBACKS = {
    "pass": "检查通过",
    "fail": "发现问题，详见证据",
    "warning": "发现问题，详见证据",
    "manual_review": "需人工复核",
    "evidence_insufficient": "证据不足，未能完成判定",
    "error": "判定过程出错",
    "skipped": "规则未执行",
}
_PLAIN_TEXT_GENERIC_FALLBACK = "详情见证据与审计记录"

VALID_RULE_EXECUTION_STATUSES = {
    "pass",
    "fail",
    "warning",
    "manual_review",
    "evidence_insufficient",
    "skipped",
    "error",
}
"""规则执行状态枚举，与 qc_rule_execution.execution_status 约束保持一致。"""

EVIDENCE_REQUIRED_STATUSES = frozenset({"pass", "fail", "warning", "manual_review"})
"""必须提供证据附件 ID 的确定性或风险结论状态。"""

PAGE_CAPTURE_REQUIRED_STATUSES = EVIDENCE_REQUIRED_STATUSES
"""必须绑定页面采集记录的确定性或风险结论状态。"""

VALID_JUDGE_STRATEGIES = {"rule_engine", "llm", "hybrid"}
"""规则判定策略枚举，与 qc_rule_execution.judge_strategy 约束保持一致。"""

VALID_SEVERITIES = {"low", "medium", "high", "critical"}
"""规则严重级别枚举，与 qc_rule.severity 数据字典保持一致。"""

AI_JUDGE_STRATEGIES = frozenset({"llm", "hybrid"})
"""需要统一结构化输出校验的模型判定策略。"""

MAX_AI_JUDGE_PAYLOAD_BYTES = 16_384
"""单个模型判定输入或输出允许写入审计字段的最大字节数。"""

FORBIDDEN_AI_JUDGE_PAYLOAD_KEYS = frozenset(
    {
        "base64",
        "dom",
        "dom_snapshot",
        "full_dom",
        "html",
        "image_base64",
        "page_source",
        "screenshot_base64",
    }
)
"""禁止写入模型判定审计字段的原始重载荷键。"""


RULE_QUALITY_OUTCOMES = {
    "passed",
    "failed",
    "skipped",
    "inconclusive",
    "evidence_gap",
}
"""规则级质检结论五态，与 qc_rule_execution.quality_outcome 生成列词汇保持一致。"""

_QUALITY_OUTCOME_BY_EXECUTION_STATUS = {
    "pass": "passed",
    "fail": "failed",
    "warning": "failed",
    "manual_review": "inconclusive",
    "error": "inconclusive",
    "evidence_insufficient": "evidence_gap",
    "skipped": "skipped",
}
"""execution_status 到五态结论的唯一映射，必须与迁移 20260804_0037 的生成列同源。"""


def derive_quality_outcome(execution_status: str) -> str:
    """把规则执行状态映射为五态质检结论；数据库生成列是同一映射的持久化形态。"""
    try:
        return _QUALITY_OUTCOME_BY_EXECUTION_STATUS[execution_status]
    except KeyError as exc:
        raise RuleExecutionValidationError(f"非法规则执行状态：{execution_status}") from exc


class ReviewStatus(StrEnum):
    """人工审核状态，与 qc_rule_execution.review_status 约束保持一致。"""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    NEEDS_RECHECK = "needs_recheck"


VALID_REVIEW_STATUSES = set(ReviewStatus)
"""人工审核状态枚举，与 qc_rule_execution.review_status 约束保持一致。"""


@dataclass(frozen=True)
class RecordRuleExecutionCommand:
    """记录规则执行结果命令。"""

    task_id: str
    execution_status: str
    agent_run_id: str | None = None
    rule_id: str | None = None
    rule_code: str | None = None
    page_capture_id: str | None = None
    source_idempotency_key: str | None = None
    observation_packet_id: str | None = None
    judge_strategy: str | None = None
    confidence: float | None = None
    severity: str | None = None
    result_summary: str | None = None
    problem_desc: str | None = None
    evidence_artifact_ids: list[str] = field(default_factory=list)
    judge_input: dict[str, Any] = field(default_factory=dict)
    judge_output: dict[str, Any] = field(default_factory=dict)
    review_status: ReviewStatus | None = None
    review_reason: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    # Formal Inspection：携带后同事务校验 Work/Attempt/lease，拒绝旧消费者覆盖。
    work_item_id: str | None = None
    attempt_id: str | None = None
    lease_token: str | None = None
    recovery_lease_token: str | None = None
    recovery_generation: int | None = None
    recovery_lease_expires_at: datetime | None = None
    # 仅系统生成且可复核身份的 Web Coverage Gap 可关闭默认 Review Item。
    create_review_item: bool = True


@dataclass(frozen=True)
class RecordedRuleExecution:
    """已写入的规则执行结果。"""

    execution_id: str
    rule_id: str
    rule_code: str
    execution_status: str
    page_capture_id: str | None
    quality_outcome: str | None = None


@dataclass(frozen=True)
class RuleExecutionFinalizationResult:
    """任务收尾补齐规则执行记录的结果。"""

    inserted_count: int
    unresolved_candidate_count: int


@dataclass(frozen=True)
class CandidatePageReference:
    """本次 Run 已完成候选规则查询的页面引用。"""

    page_capture_id: str
    page_type: str
    candidate_rule_codes: tuple[str, ...] | None = None


@dataclass(frozen=True)
class RuntimeRuleExecutionFallback:
    """运行时已隔离、但因写库失败尚未持久化的规则终态。"""

    page_capture_id: str
    rule_id: str | None
    rule_code: str | None
    execution_status: str
    result_summary: str | None = None
    problem_desc: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class RuntimePageInspectionFailureFallback:
    """已识别业务页面在规则匹配前失败时的页面级收尾信息。"""

    page_capture_id: str
    page_type: str
    failure_code: str
    failure_message: str


@dataclass(frozen=True)
class RuleExecutionDefinition:
    """任务锁定规则的执行策略定义。"""

    executor_type: str
    manual_review_policy: dict[str, Any]
    severity: str | None = None
    judge_config: dict[str, Any] = field(default_factory=dict)


class RuleExecutionRepository(Protocol):
    """规则执行结果写回仓储契约。"""

    async def record_rule_execution(
        self, command: RecordRuleExecutionCommand
    ) -> RecordedRuleExecution | None:
        """写入或更新一条规则执行结果。"""

    async def get_rule_execution_definition(
        self, *, task_id: str, rule_id: str | None, rule_code: str | None
    ) -> RuleExecutionDefinition | None:
        """查询任务锁定规则的执行策略定义。"""

    async def agent_run_is_terminal(self, *, agent_run_id: str) -> bool:
        """判断 Agent Run 是否已进入终态。"""


class RuleExecutionTargetNotFoundError(ValueError):
    """任务或规则不存在，无法写入规则执行结果。"""


class RuleExecutionValidationError(ValueError):
    """规则执行结果输入不符合数据约束。"""


class RuleExecutionLeaseConflictError(RuntimeError):
    """Work Item 租约失效或 Attempt 不匹配，禁止写回正式规则结果。"""


class RuleExecutionService:
    """规则执行结果写回服务。"""

    def __init__(self, *, repository: RuleExecutionRepository) -> None:
        self._repository = repository

    async def record_rule_execution(
        self, command: RecordRuleExecutionCommand
    ) -> RecordedRuleExecution:
        _validate_command(command, validate_severity=False)
        definition = await self._repository.get_rule_execution_definition(
            task_id=command.task_id,
            rule_id=command.rule_id,
            rule_code=command.rule_code,
        )
        if definition is None:
            raise RuleExecutionTargetNotFoundError(
                "未找到可写入的任务规则：请确认 task_id 与 rule_id/rule_code 是否匹配"
            )
        if (
            command.judge_strategy is not None
            and command.judge_strategy != definition.executor_type
        ):
            raise RuleExecutionValidationError(
                "规则判定策略不一致："
                f"任务锁定规则 executor_type={definition.executor_type}，"
                f"写回 judge_strategy={command.judge_strategy}"
            )
        # 系统级措辞硬约束：判定可见文案不得包含黑灰产、违法类业务定性词，
        # 且必须是中文大白话，不得出现内部工具名；原始输出仍保留在审计字段。
        neutralized_summary = neutralize_business_characterizations(command.result_summary)
        neutralized_problem = neutralize_business_characterizations(command.problem_desc)
        neutralized_review = neutralize_business_characterizations(command.review_reason)
        plain_summary = enforce_plain_display_text(
            neutralized_summary,
            fallback=_PLAIN_SUMMARY_FALLBACKS.get(
                command.execution_status, _PLAIN_TEXT_GENERIC_FALLBACK
            ),
        )
        plain_problem = enforce_plain_display_text(
            neutralized_problem, fallback=_PLAIN_TEXT_GENERIC_FALLBACK
        )
        plain_review = enforce_plain_display_text(
            neutralized_review, fallback=_PLAIN_TEXT_GENERIC_FALLBACK
        )
        if (plain_summary, plain_problem, plain_review) != (
            neutralized_summary,
            neutralized_problem,
            neutralized_review,
        ):
            logger.warning(
                "task_id=%s rule=%s 判定文案含英文或内部工具名，已按大白话规范收敛。",
                command.task_id,
                command.rule_id or command.rule_code,
            )
        resolved_command = replace(
            command,
            judge_strategy=definition.executor_type,
            severity=definition.severity or command.severity,
            result_summary=plain_summary,
            problem_desc=plain_problem,
            review_reason=plain_review,
        )
        if definition.executor_type in AI_JUDGE_STRATEGIES:
            resolved_command = _normalize_ai_judge_command(
                resolved_command,
                manual_review_policy=definition.manual_review_policy,
            )
        resolved_command = _preserve_green_partial_review(resolved_command, definition)
        _validate_command(resolved_command)
        result = await self._repository.record_rule_execution(resolved_command)
        if result is None:
            raise RuleExecutionTargetNotFoundError(
                "未找到可写入的任务规则：请确认 task_id 与 rule_id/rule_code 是否匹配"
            )
        return result


def _preserve_green_partial_review(
    command: RecordRuleExecutionCommand, definition: RuleExecutionDefinition,
) -> RecordRuleExecutionCommand:
    """只按任务保存的新绿色合同投递剩余缺项，保留已经成立的失败结论。"""
    from services.api.app.application.green_rule_catalog import validate_green_rule_configuration

    try:
        contract = validate_green_rule_configuration({
            "executor_type": definition.executor_type, "judge_config": definition.judge_config,
        })
    except ValueError as exc:
        raise RuleExecutionValidationError(str(exc)) from exc
    if contract is None or contract["input_kind"] != "page":
        return command
    reported_contract = command.judge_output.get("green_rule_contract")
    if (reported_contract is not None or command.execution_status in {"pass", "fail"}) and (
        reported_contract != contract
    ):
        raise RuleExecutionValidationError("绿色页面写回与任务冻结合同不一致")
    if command.judge_output.get("missing_evidence"):
        if command.execution_status == "pass":
            raise RuleExecutionValidationError("绿色页面仍缺必要材料，不能写回整体通过")
        return replace(
            command,
            create_review_item=True,
            review_status=ReviewStatus.PENDING,
            review_reason=command.review_reason or "已有分项结论保留，其余分项仍缺必要材料。",
        )
    return command


def _validate_command(
    command: RecordRuleExecutionCommand,
    *,
    validate_severity: bool = True,
) -> None:
    if not command.task_id:
        raise RuleExecutionValidationError("记录规则执行结果必须提供 task_id")
    if not isinstance(command.create_review_item, bool):
        raise RuleExecutionValidationError("create_review_item 必须为布尔值")
    if not command.rule_id and not command.rule_code:
        raise RuleExecutionValidationError("记录规则执行结果必须提供 rule_id 或 rule_code")
    if command.source_idempotency_key is not None and (
        not isinstance(command.source_idempotency_key, str)
        or not command.source_idempotency_key.strip()
        or command.source_idempotency_key != command.source_idempotency_key.strip()
    ):
        raise RuleExecutionValidationError("规则执行结果来源幂等键必须为无首尾空白的非空字符串")
    if command.execution_status not in VALID_RULE_EXECUTION_STATUSES:
        raise RuleExecutionValidationError(f"非法规则执行状态：{command.execution_status}")
    if command.judge_strategy is not None and command.judge_strategy not in VALID_JUDGE_STRATEGIES:
        raise RuleExecutionValidationError(f"非法规则判定策略：{command.judge_strategy}")
    if (
        validate_severity
        and command.severity is not None
        and command.severity not in VALID_SEVERITIES
    ):
        raise RuleExecutionValidationError(f"非法规则严重级别：{command.severity}")
    if command.review_status is not None and command.review_status not in VALID_REVIEW_STATUSES:
        raise RuleExecutionValidationError(f"非法人工审核状态：{command.review_status}")
    if command.execution_status in PAGE_CAPTURE_REQUIRED_STATUSES and not command.page_capture_id:
        raise RuleExecutionValidationError(
            f"execution_status={command.execution_status} 时必须提供页面采集 ID"
        )
    if command.evidence_artifact_ids and not command.page_capture_id:
        raise RuleExecutionValidationError("证据附件必须绑定页面采集 ID")
    if command.observation_packet_id and not command.page_capture_id:
        raise RuleExecutionValidationError("Observation Packet 必须绑定页面采集 ID")
    if (
        command.execution_status in EVIDENCE_REQUIRED_STATUSES
        and not _has_valid_evidence_ids(command.evidence_artifact_ids)
        and not _has_valid_reuse_reference(command)
    ):
        raise RuleExecutionValidationError(
            f"execution_status={command.execution_status} 时必须提供有效证据附件 ID"
        )
    if command.confidence is not None and not 0 <= command.confidence <= 1:
        raise RuleExecutionValidationError("规则判定置信度必须在 0 到 1 之间")
    fencing_fields = (
        command.work_item_id,
        command.attempt_id,
        command.lease_token,
    )
    if any(fencing_fields) and not all(
        isinstance(item, str) and item.strip() for item in fencing_fields
    ):
        raise RuleExecutionValidationError(
            "正式质检写回必须同时提供 work_item_id、attempt_id 与 lease_token"
        )
    if not command.create_review_item and not _is_trusted_web_coverage_gap(command):
        raise RuleExecutionValidationError(
            "只有身份完整的系统 Web Coverage Gap 可关闭 Review Item"
        )


def _has_valid_reuse_reference(command: RecordRuleExecutionCommand) -> bool:
    """复用结果不复制附件，但必须携带可由数据库追溯的来源执行身份。"""
    reuse = command.judge_output.get("reuse")
    if not isinstance(reuse, dict):
        return False
    if (
        reuse.get("protocol_version") != RULE_REUSE_RECORD_PROTOCOL_VERSION
        or reuse.get("scope") not in {scope.value for scope in LOOKUP_REUSE_SCOPES}
        or reuse.get("source_execution_status") != command.execution_status
        or command.execution_status not in REUSABLE_SOURCE_STATUSES
    ):
        return False
    required = (
        reuse.get("reuse_key"),
        reuse.get("source_execution_id"),
        reuse.get("source_page_capture_id"),
    )
    return all(
        isinstance(value, str) and bool(value) and value.strip() == value
        for value in required
    )


def _is_trusted_web_coverage_gap(command: RecordRuleExecutionCommand) -> bool:
    """校验免审核 Gap 的稳定来源身份，防止普通执行静默绕过 Review。"""

    if (
        command.execution_status != "evidence_insufficient"
        or command.page_capture_id is not None
        or not command.agent_run_id
        or not command.rule_code
        or not command.source_idempotency_key
    ):
        return False
    judge_input = command.judge_input
    snapshot_id = judge_input.get("coverage_snapshot_id")
    missing_page_type = judge_input.get("missing_page_type")
    if not all(
        isinstance(value, str) and value.strip()
        for value in (snapshot_id, missing_page_type)
    ):
        return False
    if (
        judge_input.get("schema_version") != "web-coverage-gap.v1"
        or judge_input.get("source") != "web_coverage_gap"
        or judge_input.get("reason") != "required_page_type_not_discovered"
    ):
        return False
    expected_key = (
        f"web-coverage-gap:{snapshot_id}:{missing_page_type}:{command.rule_code}"
    )
    return command.source_idempotency_key == expected_key


def _has_valid_evidence_ids(evidence_ids: list[str]) -> bool:
    return bool(evidence_ids) and all(item.strip() for item in evidence_ids)


def _normalize_ai_judge_command(
    command: RecordRuleExecutionCommand,
    *,
    manual_review_policy: dict[str, Any],
) -> RecordRuleExecutionCommand:
    _validate_ai_judge_payload("judge_input", command.judge_input)
    _validate_ai_judge_payload("judge_output", command.judge_output)
    if command.execution_status not in EVIDENCE_REQUIRED_STATUSES:
        return command

    if not command.judge_input:
        raise RuleExecutionValidationError("llm/hybrid 判定必须提供非空 judge_input")
    min_confidence = _min_confidence(manual_review_policy)
    command = _coerce_judge_output_schema_version(command)
    command = _normalize_missing_hybrid_tool_results(
        command,
        min_confidence=min_confidence,
    )
    _validate_ai_judge_output_contract(
        command.judge_output,
        execution_status=command.execution_status,
        judge_strategy=command.judge_strategy or "",
    )
    if command.confidence is None:
        proposed_conclusion = command.execution_status
        judge_output = {
            **command.judge_output,
            "conclusion": "manual_review",
            "proposed_conclusion": proposed_conclusion,
            "confidence_threshold": min_confidence,
            "confidence_fallback_reason": "missing_confidence",
        }
        return replace(
            command,
            execution_status="manual_review",
            confidence=0.0,
            judge_output=judge_output,
            review_status=ReviewStatus.NEEDS_RECHECK,
            review_reason=(
                command.review_reason or "模型判定遗漏 confidence，已保守降级为人工复核。"
            ),
        )
    if (
        command.execution_status in {"pass", "fail", "warning"}
        and command.confidence < min_confidence
    ):
        proposed_conclusion = command.execution_status
        judge_output = {
            **command.judge_output,
            "conclusion": "manual_review",
            "proposed_conclusion": proposed_conclusion,
            "confidence_threshold": min_confidence,
        }
        review_reason = f"判定置信度 {command.confidence:.4f} 低于规则阈值 {min_confidence:.4f}"
        return replace(
            command,
            execution_status="manual_review",
            judge_output=judge_output,
            review_status=ReviewStatus.NEEDS_RECHECK,
            review_reason=command.review_reason or review_reason,
        )
    return command


def _coerce_judge_output_schema_version(
    command: RecordRuleExecutionCommand,
) -> RecordRuleExecutionCommand:
    """模型常把 schema_version 写成数字 1.0、"1" 或干脆漏写；同义写法统一成 "1.0"。

    schema_version 不承载判定内容，只因写法差异就把整条判定记成执行异常并不划算
    （2026-09-22 沈钧铭验收 App 任务实锤）；真正的结构要求仍由后面的契约校验把关，
    不认识的版本号照旧拒绝。
    """
    raw = command.judge_output.get("schema_version")
    if raw == "1.0":
        return command
    if raw is not None and str(raw).strip().lower().removeprefix("v") not in {"1", "1.0"}:
        return command
    logger.warning(
        "task_id=%s rule=%s llm/hybrid judge_output.schema_version=%r 已按 1.0 归一。",
        command.task_id,
        command.rule_id or command.rule_code,
        raw,
    )
    return replace(command, judge_output={**command.judge_output, "schema_version": "1.0"})


def _normalize_missing_hybrid_tool_results(
    command: RecordRuleExecutionCommand,
    *,
    min_confidence: float,
) -> RecordRuleExecutionCommand:
    """把缺少工具结果的 hybrid 结论保守转为人工复核。"""
    if command.judge_strategy != "hybrid":
        return command
    tool_results = command.judge_output.get("tool_results")
    if tool_results is not None and tool_results != []:
        return command

    proposed_conclusion = command.execution_status
    judge_output = {
        **command.judge_output,
        "conclusion": "manual_review",
        "proposed_conclusion": proposed_conclusion,
        "tool_results": [
            {
                "status": "unavailable",
                "error_code": "MISSING_TOOL_RESULTS",
                "message": "模型未提供 hybrid 工具结果，原结论不能自动采信。",
            }
        ],
        "confidence_threshold": min_confidence,
        "hybrid_fallback_reason": "missing_tool_results",
    }
    return replace(
        command,
        execution_status="manual_review",
        confidence=0.0,
        judge_output=judge_output,
        review_status=ReviewStatus.NEEDS_RECHECK,
        review_reason=(
            command.review_reason or "模型判定遗漏 hybrid 工具结果，已保守降级为人工复核。"
        ),
    )


def _validate_ai_judge_output_contract(
    judge_output: dict[str, Any],
    *,
    execution_status: str,
    judge_strategy: str,
) -> None:
    if judge_output.get("schema_version") != "1.0":
        raise RuleExecutionValidationError("llm/hybrid judge_output.schema_version 必须为 1.0")
    if judge_output.get("conclusion") != execution_status:
        raise RuleExecutionValidationError(
            "llm/hybrid judge_output.conclusion 必须与 execution_status 一致"
        )
    reasoning_summary = judge_output.get("reasoning_summary")
    if not isinstance(reasoning_summary, str) or not reasoning_summary.strip():
        raise RuleExecutionValidationError(
            "llm/hybrid judge_output.reasoning_summary 必须为非空字符串"
        )
    if judge_strategy == "hybrid":
        tool_results = judge_output.get("tool_results")
        if not isinstance(tool_results, list) or not tool_results:
            raise RuleExecutionValidationError("hybrid judge_output.tool_results 必须为非空数组")
        if not all(isinstance(item, dict) for item in tool_results):
            raise RuleExecutionValidationError("hybrid judge_output.tool_results 只能包含对象")


def _validate_ai_judge_payload(name: str, payload: dict[str, Any]) -> None:
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        default=str,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(serialized) > MAX_AI_JUDGE_PAYLOAD_BYTES:
        raise RuleExecutionValidationError(
            f"llm/hybrid {name} 超过 {MAX_AI_JUDGE_PAYLOAD_BYTES} 字节"
        )
    forbidden_key = _find_forbidden_payload_key(payload)
    if forbidden_key is not None:
        raise RuleExecutionValidationError(f"llm/hybrid {name} 包含禁止字段：{forbidden_key}")


def _find_forbidden_payload_key(payload: Any) -> str | None:
    if isinstance(payload, dict):
        for key, value in payload.items():
            normalized_key = str(key).strip().lower().replace("-", "_")
            if normalized_key in FORBIDDEN_AI_JUDGE_PAYLOAD_KEYS or normalized_key.endswith(
                "_base64"
            ):
                return str(key)
            nested_key = _find_forbidden_payload_key(value)
            if nested_key is not None:
                return nested_key
    elif isinstance(payload, list):
        for item in payload:
            nested_key = _find_forbidden_payload_key(item)
            if nested_key is not None:
                return nested_key
    return None


def _min_confidence(manual_review_policy: dict[str, Any]) -> float:
    if "min_confidence" not in manual_review_policy:
        raise RuleExecutionValidationError("规则 manual_review_policy 缺少 min_confidence")
    value = manual_review_policy["min_confidence"]
    if isinstance(value, bool):
        raise RuleExecutionValidationError("规则 min_confidence 配置非法")
    try:
        min_confidence = float(value)
    except (TypeError, ValueError) as exc:
        raise RuleExecutionValidationError("规则 min_confidence 配置非法") from exc
    if not 0 <= min_confidence <= 1:
        raise RuleExecutionValidationError("规则 min_confidence 配置非法")
    return min_confidence
