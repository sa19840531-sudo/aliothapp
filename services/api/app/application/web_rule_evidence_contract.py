"""Web 规则证据用途合同；配置正文由规则权限入口持久化并参与原有哈希。"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from services.api.app.domain.protocol import stable_protocol_hash

WEB_RULE_EVIDENCE_VERSION = "web-rule-evidence.v1"
WEB_EVIDENCE_ROLES = frozenset(
    {"stable_page", "failure_sequence", "interaction_sequence", "diagnostic"}
)
WEB_SEQUENCE_ROLES = frozenset({"failure_sequence", "interaction_sequence"})
WEB_SEQUENCE_ARTIFACTS = frozenset(
    {
        "screenshot",
        "dom",
        "visible_text",
        "ui_snapshot",
        "action_trace",
        "har",
        "console_log",
        "media_state",
        "temporal_trace",
    }
)


@dataclass(frozen=True, kw_only=True)
class WebSequenceValidation:
    """语义可判定性不等于清单齐全性、稳定曝光或产品失败。"""

    status: str
    reason_code: str
    sample_capture_ids: tuple[str, ...] = ()
    duration_ms: int = 0
    validation_hash: str | None = None


@dataclass(frozen=True, kw_only=True)
class WebRuleEvidenceContract:
    """区分材料语义用途与底层 Capture 角色，不代表产品判定结果。"""

    rule_version: str
    accepted_roles: tuple[str, ...]
    required_artifact_types: tuple[str, ...]
    minimum_samples: int
    minimum_duration_ms: int
    requires_action: bool
    accept_unclassified: bool = False
    local_structure: dict | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.rule_version, str) or not self.rule_version.strip():
            raise ValueError("Web rule_version 必须为非空文本")
        for field, allowed in (
            ("accepted_roles", WEB_EVIDENCE_ROLES),
            ("required_artifact_types", WEB_SEQUENCE_ARTIFACTS),
        ):
            values = getattr(self, field)
            if (
                not isinstance(values, tuple)
                or not values
                or any(not isinstance(v, str) or v not in allowed for v in values)
                or len(set(values)) != len(values)
            ):
                raise ValueError(f"Web {field} 必须为不重复的受支持值")
        is_sequence = bool(set(self.accepted_roles) & WEB_SEQUENCE_ROLES)
        if type(self.minimum_samples) is not int or self.minimum_samples < (
            2 if is_sequence else 1
        ):
            raise ValueError("Web minimum_samples 不满足真实采样数量")
        if type(self.minimum_duration_ms) is not int or self.minimum_duration_ms < 0:
            raise ValueError("Web minimum_duration_ms 必须为非负整数")
        if type(self.requires_action) is not bool or type(self.accept_unclassified) is not bool:
            raise ValueError("Web 动作及未分类声明必须为布尔值")
        if self.accept_unclassified and "failure_sequence" not in self.accepted_roles:
            raise ValueError("Web 未分类输入必须明确接受 failure_sequence")
        if self.local_structure is not None:
            from services.api.app.application.web_local_structure import (
                validate_web_local_structure_rule,
            )

            validate_web_local_structure_rule(self.local_structure)
            if not set(self.accepted_roles).issubset(WEB_SEQUENCE_ROLES):
                raise ValueError("Web 局部结构声明仅支持显式序列用途")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": WEB_RULE_EVIDENCE_VERSION,
            "rule_version": self.rule_version,
            "accepted_roles": list(self.accepted_roles),
            "required_artifact_types": list(self.required_artifact_types),
            "minimum_samples": self.minimum_samples,
            "minimum_duration_ms": self.minimum_duration_ms,
            "requires_action": self.requires_action,
            "accept_unclassified": self.accept_unclassified,
            **(
                {"local_structure": dict(self.local_structure)}
                if self.local_structure is not None
                else {}
            ),
        }

    def validate_sequence(
        self,
        sequence: Mapping[str, object],
        *,
        task_id: str,
        agent_run_id: str,
        finalization_epoch: int,
    ) -> WebSequenceValidation:
        """复验已由仓储认证的持久帧；不能把此纯校验当数据库授权。"""

        def reject(code):
            return WebSequenceValidation(status="evidence_insufficient", reason_code=code)

        identity = {
            "task_id": task_id,
            "agent_run_id": agent_run_id,
            "session_id": f"web:{agent_run_id}",
            "finalization_epoch": finalization_epoch,
        }
        if any(sequence.get(k) != v for k, v in identity.items()) or (
            sequence.get("target_platform") != "web"
            or sequence.get("workflow_version") != "v2"
            or sequence.get("session_owner") != "web-v2-workflow"
        ):
            return reject("WEB_SEQUENCE_IDENTITY_MISMATCH")
        purpose = sequence.get("purpose")
        if purpose not in WEB_SEQUENCE_ROLES or purpose not in self.accepted_roles:
            return reject("WEB_SEQUENCE_ROLE_REJECTED")
        if any(
            not isinstance(sequence.get(k), str) or not sequence[k]
            for k in (
                "target_id",
                "occurrence_id",
            )
        ):
            return reject("WEB_SEQUENCE_TARGET_MISSING")
        frames = sequence.get("frames")
        if not isinstance(frames, (list, tuple)) or not 2 <= len(frames) <= 128:
            return reject("WEB_SEQUENCE_FRAMES_MISSING")
        captures = {}
        samples = []
        previous_time = None
        for frame in frames:
            if not isinstance(frame, Mapping) or any(
                frame.get(k) != v for k, v in identity.items()
            ):
                return reject("WEB_SEQUENCE_FRAME_IDENTITY_MISMATCH")
            capture_id = frame.get("capture_id")
            if not isinstance(capture_id, str) or not capture_id or capture_id in captures:
                return reject("WEB_SEQUENCE_CAPTURE_DUPLICATE")
            if (
                frame.get("evidence_mode") != "natural"
                or frame.get("artifacts_verified") is not True
            ):
                return reject("WEB_SEQUENCE_SOURCE_UNTRUSTED")
            artifacts = frame.get("artifact_types")
            if not isinstance(artifacts, (list, tuple)) or any(
                not isinstance(v, str) for v in artifacts
            ):
                return reject("WEB_SEQUENCE_ARTIFACT_MISSING")
            if not set(self.required_artifact_types).issubset(artifacts):
                return reject("WEB_SEQUENCE_ARTIFACT_MISSING")
            try:
                timestamp = datetime.fromisoformat(str(frame.get("captured_at")))
            except ValueError:
                return reject("WEB_SEQUENCE_TIME_INVALID")
            if timestamp.tzinfo is None or (
                previous_time is not None and timestamp <= previous_time
            ):
                return reject("WEB_SEQUENCE_TIME_UNORDERED")
            previous_time = timestamp
            role = frame.get("capture_role")
            if role == "after_action":
                before = captures.get(frame.get("parent_capture_id"))
                if (
                    before is None
                    or before.get("capture_role") != "before_action"
                    or not frame.get("action_execution_id")
                    or before.get("action_execution_id") != frame.get("action_execution_id")
                ):
                    return reject("WEB_SEQUENCE_ACTION_PARENT_MISSING")
                if any(frame.get(k) != sequence[k] for k in ("target_id", "occurrence_id")):
                    return reject("WEB_SEQUENCE_TARGET_MISMATCH")
                if purpose == "failure_sequence" and frame.get("page_state") not in {
                    "loading",
                    "line_loading",
                    "transitioning",
                    "needs_more_observation",
                    "timeout",
                    "popup_blocked",
                    "human_verification_blocked",
                    "capture_failed",
                }:
                    return reject("WEB_SEQUENCE_FAILURE_STATE_MISMATCH")
                samples.append((capture_id, timestamp))
            elif role == "before_action":
                if captures and frame.get("parent_capture_id") not in captures:
                    return reject("WEB_SEQUENCE_DIRECT_CHAIN_MISSING")
            else:
                return reject("WEB_SEQUENCE_CAPTURE_ROLE_REJECTED")
            captures[capture_id] = frame
        if len(samples) < self.minimum_samples:
            return reject("WEB_SEQUENCE_SAMPLES_INSUFFICIENT")
        duration = int((samples[-1][1] - samples[0][1]).total_seconds() * 1000)
        if duration < self.minimum_duration_ms:
            return reject("WEB_SEQUENCE_DURATION_INSUFFICIENT")
        sample_ids = tuple(item[0] for item in samples)
        if purpose == "interaction_sequence":
            outcomes = sequence.get("interaction_outcomes")
            if not isinstance(outcomes, (list, tuple)) or not 1 <= len(outcomes) <= 12:
                return reject("WEB_INTERACTION_ORIGINAL_ACTION_MISSING")
            for ordinal, outcome in enumerate(outcomes):
                if not isinstance(outcome, Mapping) or outcome.get("outcome_ordinal") != ordinal:
                    return reject("WEB_INTERACTION_OUTCOME_INVALID")
                original = captures.get(outcome.get("action_result_capture_id"))
                before = captures.get(outcome.get("before_capture_id"))
                final = outcome.get("after_capture_id")
                if (
                    original is None
                    or before is None
                    or final not in sample_ids
                    or original.get("parent_capture_id") != before.get("capture_id")
                    or before.get("parent_capture_id") != outcome.get("requested_source_capture_id")
                    or original.get("action_execution_id") != outcome.get("action_execution_id")
                    or not outcome.get("intent_id")
                    or not outcome.get("intent_hash")
                    or outcome.get("evidence_mode") != "natural"
                    or any(
                        type(outcome.get(key)) is not bool
                        for key in (
                            "command_accepted",
                            "effect_observed",
                            "postcondition_satisfied",
                            "transition_arrived",
                        )
                    )
                ):
                    return reject("WEB_INTERACTION_OUTCOME_INVALID")
                if outcome["transition_arrived"] and not all(
                    outcome[key]
                    for key in ("command_accepted", "effect_observed", "postcondition_satisfied")
                ):
                    return reject("WEB_INTERACTION_OUTCOME_INVALID")
        return WebSequenceValidation(
            status="admissible",
            reason_code="WEB_SEQUENCE_ADMISSIBLE",
            sample_capture_ids=sample_ids,
            duration_ms=duration,
            validation_hash=stable_protocol_hash(
                {
                    "contract": self.to_dict(),
                    "sequence": dict(sequence),
                    "sample_capture_ids": list(sample_ids),
                    "duration_ms": duration,
                }
            ),
        )


def read_web_rule_evidence_contract(
    input_contract: Mapping[str, object],
) -> WebRuleEvidenceContract | None:
    """未声明时保留旧合同；显式声明必须限定真实 Web 客户端。"""

    if "web_evidence" not in input_contract:
        return None
    if input_contract.get("target_clients") != ["WEB"]:
        raise ValueError("Web 序列合同必须独占 target_clients=['WEB']")
    raw = input_contract["web_evidence"]
    if not isinstance(raw, Mapping) or raw.get("schema_version") != WEB_RULE_EVIDENCE_VERSION:
        raise ValueError("Web 证据合同版本无效")
    expected = {
        "schema_version",
        "rule_version",
        "accepted_roles",
        "required_artifact_types",
        "minimum_samples",
        "minimum_duration_ms",
        "requires_action",
        "accept_unclassified",
    }
    if set(raw) not in (expected, expected | {"local_structure"}):
        raise ValueError("Web 证据合同字段缺失或未知")
    if "local_structure" in raw and not isinstance(raw["local_structure"], dict):
        raise ValueError("Web 局部结构声明必须为完整对象")
    for name in ("accepted_roles", "required_artifact_types"):
        if not isinstance(raw[name], list):
            raise ValueError(f"Web {name} 必须为数组")
    return WebRuleEvidenceContract(
        rule_version=raw["rule_version"],
        accepted_roles=tuple(raw["accepted_roles"]),
        required_artifact_types=tuple(raw["required_artifact_types"]),
        minimum_samples=raw["minimum_samples"],
        minimum_duration_ms=raw["minimum_duration_ms"],
        requires_action=raw["requires_action"],
        accept_unclassified=raw["accept_unclassified"],
        local_structure=raw.get("local_structure"),
    )


def validate_web_sequence_execution(input_contract, *, executor_type, judge_config):
    """配置、发布、消费共用支持矩阵，不替换规则执行器。"""
    contract = read_web_rule_evidence_contract(input_contract)
    if contract is not None and set(contract.accepted_roles) & WEB_SEQUENCE_ROLES:
        from services.api.app.application.capture_plan import derive_session_dependency

        if (
            executor_type != "llm"
            or derive_session_dependency(
                input_contract=dict(input_contract),
                judge_config=judge_config,
            )
            != "snapshot"
        ):
            raise ValueError("Web 序列合同仅支持 snapshot/llm 执行器")
    return contract
