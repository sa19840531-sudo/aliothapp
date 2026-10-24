"""统一规则判定用例：按任务锁定配置执行确定性、语义或混合判定。"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import Any, Literal, Protocol

from services.api.app.application.capture_plan import derive_session_dependency
from services.api.app.application.evidence_payload_safety import redact_audit_payload
from services.api.app.application.evidence_presentation import (
    DECLARED_SESSION_AUTH_KEY,
    rule_accepts_diagnostic_evidence,
    rule_evidence_layer_target,
)
from services.api.app.application.inspection_work_items import (
    build_inspection_rule_contract_hash,
)
from services.api.app.application.judgment_wording_policy import (
    sanitize_semantic_judge_output,
)
from services.api.app.application.manifest_backed_observation import (
    ManifestReadOutcome,
)
from services.api.app.application.manifest_read_guard import (
    REJECTION_REASON_CODE_BY_OUTCOME,
    REJECTION_STATUS_BY_OUTCOME,
)
from services.api.app.application.observation_packet import (
    MANIFEST_QUALITY_GATE_SOURCE,
    BuildObservationPacketRequest,
    ObservationPacket,
    ObservationPacketService,
    ObservationPacketValidationError,
)
from services.api.app.application.rule_auth_gate import (
    AUTH_REQUIREMENT_AUTHENTICATED,
    AUTH_REQUIREMENT_GUEST,
    AUTH_REQUIREMENT_NOT_MET_REASON,
)
from services.api.app.application.rule_capabilities import (
    CAPABILITY_HANDLER_NAMES,
    CapabilityResult,
    evaluate_rule_engine_handler,
    run_hybrid_capability_tools,
)
from services.api.app.application.rule_capability_facts import candidate_qr_urls
from services.api.app.application.rule_capability_hybrid import (
    NATIVE_PLAYBACK_SWITCH_GAP,
    PAYMENT_ENTRY_CHAIN_GAP,
    PROGRESS_BAR_STATIC_PAGE_GAP,
    SERVICE_ENTRY_ARRIVAL_GAP,
)
from services.api.app.application.rule_handler_evaluators import (
    HandlerEvaluation,
    HandlerInputError,
    evaluate_collection_constraint,
    evaluate_geometry_hit_area,
    evaluate_required_ui_capability,
    evaluate_screenshot_change,
    evaluate_temporal_sequence,
    evaluate_visible_text_presence,
    validate_collection_constraint_config,
    validate_geometry_hit_area_config,
    validate_required_ui_capability_config,
    validate_screenshot_change_config,
    validate_temporal_sequence_config,
    validate_visible_text_presence_config,
)
from services.api.app.application.rule_page_state import required_state_before_role
from services.api.app.application.rule_policy_evaluators import (
    HandlerPreconditionMissing,
    evaluate_content_lifecycle,
    evaluate_price_relation,
    evaluate_rate_limit,
    validate_content_lifecycle_config,
    validate_price_relation_config,
    validate_rate_limit_config,
)
from services.api.app.application.rule_transition_evaluators import (
    evaluate_external_link_open_mode,
    evaluate_state_transition,
    validate_external_link_open_mode_config,
    validate_state_transition_config,
)
from services.api.app.application.semantic_rule_judge import (
    SemanticJudgeModelOutput,
    SemanticRuleJudge,
)
from services.api.app.domain.evidence.presentation import (
    PresentationAssessment,
    PresentationRejectionCode,
    evaluate_presentation_for_rule,
)
from services.api.app.domain.inspection.evidence_gap import build_evidence_gap
from services.api.app.domain.inspection.evidence_request import EvidenceInteractionRequirement
from services.api.app.domain.interaction.enums import InteractionOperation
from services.api.app.domain.task import TaskExecutionMode

RULE_ENGINE_EXECUTOR = "rule_engine"
SUPPORTED_EXECUTOR_TYPES = frozenset({"llm", RULE_ENGINE_EXECUTOR, "hybrid"})
MAX_COMPACT_TOOL_RESULT_BYTES = 1_500
MAX_COMPACT_TOOL_RESULTS_BYTES = 6_000
MAX_TRUSTED_TEXT_LENGTH = 1_000_000
MAX_REGEX_MATCH_DETAILS = 20
MAX_SEMANTIC_JUDGE_BATCH_SIZE = 4
VerdictMode = Literal["strict", "balanced"]

logger = logging.getLogger(__name__)


class RuleEngineHandlerExecutor(Protocol):
    """确定性 Handler 可调用实现契约。"""

    async def __call__(
        self,
        *,
        runtime: RuleEngineRuntime,
        definition: RuleJudgeDefinition,
        request: RuleJudgeRequest,
        engine_config: dict[str, Any],
    ) -> RuleJudgeResult: ...


class RuleEngineConfigValidator(Protocol):
    """确定性 Handler 配置校验契约。"""

    def __call__(self, engine_config: dict[str, Any]) -> str | None: ...


@dataclass(frozen=True)
class RuleEngineHandlerSpec:
    """确定性规则 Handler 的完整运行契约。"""

    handler_tool_name: str
    judge_input_source: str
    required_artifact_types: tuple[str, ...]
    executor: RuleEngineHandlerExecutor | None
    validate_config: RuleEngineConfigValidator
    # App 端采不到、处理器另有替代看法的证据类型：App 采集不因缺它被挡在处理器之前
    app_optional_artifact_types: tuple[str, ...] = ()


RULE_ENGINE_HANDLER_REGISTRY: dict[str, RuleEngineHandlerSpec] = {}


@dataclass(frozen=True)
class RuleExecutionRoute:
    """候选规则的机器可读执行路由。"""

    strategy: str
    ready: bool
    rule_engine_ready: bool
    tool_name: str | None
    handler: str | None
    handler_tool_name: str | None
    judge_input_source: str
    required_input_fields: tuple[str, ...]
    required_artifact_types: tuple[str, ...]
    blocked_reason: dict[str, str] | None


@dataclass(frozen=True)
class RuleJudgeDefinition:
    """任务锁定规则中的判定定义。"""

    rule_id: str
    rule_code: str
    executor_type: str
    judge_config: dict[str, Any]
    rule_name: str = ""
    category: str = ""
    severity: str = "low"
    check_point: str = ""
    input_contract: dict[str, Any] = field(default_factory=dict)
    manual_review_policy: dict[str, Any] = field(default_factory=dict)
    steps: list[Any] = field(default_factory=list)


@dataclass(frozen=True)
class RuleJudgeRequest:
    """单条规则统一判定请求。"""

    task_id: str
    rule_code: str
    page_capture_id: str
    evidence_artifact_ids: tuple[str, ...]
    judge_input: dict[str, Any]
    evidence_source_page_capture_id: str | None = None
    evidence_manifest_id: str | None = None
    agent_run_id: str | None = None
    source_agent_run_id: str | None = None
    evidence_capture_roles: dict[str, str] | None = None
    evidence_manifest_refs: dict[str, dict[str, str]] | None = None
    retained_baseline_artifact_ids: dict[str, tuple[str, ...]] | None = None
    expected_contract_hash: str | None = None
    evidence_input_set_id: str | None = None
    original_page_input_set_id: str | None = None
    work_item_id: str | None = None
    attempt_id: str | None = None
    lease_token: str | None = None


@dataclass(frozen=True)
class RuleJudgeResult:
    """统一规则判定结果，可直接映射到规则结果写回 Tool。"""

    rule_id: str
    rule_code: str
    executor_type: str
    judge_strategy: str
    execution_status: str
    confidence: float
    result_summary: str
    problem_desc: str | None
    page_capture_id: str
    evidence_artifact_ids: tuple[str, ...]
    judge_input: dict[str, Any]
    judge_output: dict[str, Any]
    observation_packet_id: str | None = None


class RuleJudgeRepository(Protocol):
    """规则判定定义查询契约。"""

    async def get_rule_judge_definition(
        self, *, task_id: str, rule_code: str
    ) -> RuleJudgeDefinition | None:
        """查询任务锁定规则的判定定义。"""


class RuleJudgeEvidenceReader(Protocol):
    """确定性规则可信证据读取契约。"""

    async def prepare_observation_packet(
        self,
        *,
        task_id: str,
        agent_run_id: str | None,
        source_agent_run_id: str | None,
        rule_id: str,
        page_capture_id: str,
        evidence_source_page_capture_id: str | None,
        evidence_manifest_id: str | None,
        evidence_capture_roles: dict[str, str] | None,
        evidence_manifest_refs: dict[str, dict[str, str]] | None,
        retained_baseline_artifact_ids: dict[str, tuple[str, ...]] | None,
        evidence_artifact_ids: tuple[str, ...],
        required_artifact_types: tuple[str, ...],
        frozen_observation_packet: ObservationPacket | None = None,
    ) -> Any:
        """校验证据；已有正式冻结包时复用其身份，不再写另一种观察包。"""


@dataclass(frozen=True)
class RuleEngineRuntime:
    """单次确定性 Handler 执行所需的受控依赖。"""

    evidence_reader: RuleJudgeEvidenceReader | None
    evidence_bundle: Any
    qr_validator: Callable[..., Awaitable[dict[str, Any]]]
    verdict_mode: VerdictMode = "strict"
    # 主采集来自 App（android / ios）
    app_surface: bool = False
    # H5 身份来自持久化观察包，不从 web Surface 或调用方参数推测。
    h5_surface: bool = False


class RuleEngineConfigurationRepository(Protocol):
    """启用规则引擎配置扫描契约。"""

    async def list_enabled_rule_judge_definitions(self) -> list[RuleJudgeDefinition]:
        """查询全部启用的确定性规则定义。"""


class RuleJudgeValidationError(ValueError):
    """规则判定输入不符合契约。"""


class RuleJudgeEvidenceInsufficientError(RuleJudgeValidationError):
    """规则目标存在，但缺少判定所需的可信证据类型。"""


class RuleJudgePriceTargetMissingError(RuleJudgeEvidenceInsufficientError):
    """缺少同商品多位置事实，重复观察不能生成业务目标。"""


class RuleJudgeContractDriftError(RuleJudgeValidationError):
    """工作项锁定的规则契约与当前数据库定义已经漂移。"""


class RuleJudgeTargetNotFoundError(ValueError):
    """任务锁定规则不存在。"""


class RuleJudgeNotConfiguredError(ValueError):
    """规则尚未配置可执行的确定性判定。"""


class RuleEngineConfigurationError(RuntimeError):
    """启用规则中存在不可执行的确定性规则配置。"""


def resolve_rule_execution_route(
    *,
    executor_type: str,
    judge_config: dict[str, Any],
) -> RuleExecutionRoute:
    """根据规则配置解析唯一执行路由。"""
    from services.api.app.application.green_rule_catalog import validate_green_rule_configuration

    try:
        green = validate_green_rule_configuration(
            {"executor_type": executor_type, "judge_config": judge_config}
        )
    except ValueError as exc:
        return _blocked_route(executor_type, code="GREEN_RULE_CONTRACT_INVALID", message=str(exc))
    if green is not None:
        return RuleExecutionRoute(
            strategy=executor_type, ready=True, rule_engine_ready=False,
            tool_name="rule_judge_tool", handler="green_rule_evidence",
            handler_tool_name="green_rule_evidence_tool", judge_input_source="trusted_green_evidence",
            required_input_fields=(), required_artifact_types=("screenshot",), blocked_reason=None,
        )
    if executor_type not in SUPPORTED_EXECUTOR_TYPES:
        return _blocked_route(
            executor_type,
            code="UNSUPPORTED_EXECUTOR_TYPE",
            message=f"不支持的 executor_type：{executor_type}",
        )
    if executor_type != RULE_ENGINE_EXECUTOR:
        if not isinstance(judge_config, dict):
            return _blocked_route(
                executor_type,
                code="JUDGE_CONFIG_INVALID",
                message=f"{executor_type} 规则的 judge_config 必须是对象",
            )
        tool_refs = (
            {str(item).strip() for item in judge_config.get("tool_refs", []) if str(item).strip()}
            if isinstance(judge_config.get("tool_refs"), list)
            else set()
        )
        semantic_tools = {
            "semantic_rule_judge_tool",
            "visual_rule_judge_tool",
            "content_safety_judge_tool",
        }
        selected_semantic_tools = tool_refs.intersection(semantic_tools)
        llm_checks = judge_config.get("llm_checks")
        if not selected_semantic_tools and (
            isinstance(llm_checks, list) and any(str(item).strip() for item in llm_checks)
        ):
            # 兼容历史规则配置：已有 LLM 检查项时统一路由到语义 Judge。
            selected_semantic_tools = {"semantic_rule_judge_tool"}
        if not selected_semantic_tools:
            return _blocked_route(
                executor_type,
                code="SEMANTIC_JUDGE_MISSING",
                message=f"{executor_type} 规则缺少独立语义 Judge tool_ref",
            )
        capability_config = judge_config.get("capability_config")
        capability_config = capability_config if isinstance(capability_config, dict) else {}
        deterministic_tools = capability_config.get("deterministic_tools")
        deterministic_rules = judge_config.get("deterministic_rules")
        has_deterministic_tool = isinstance(deterministic_tools, list) and any(
            str(item).strip() for item in deterministic_tools
        )
        has_legacy_deterministic_rule = isinstance(deterministic_rules, list) and any(
            str(item).strip() for item in deterministic_rules
        )
        if executor_type == "hybrid" and not (
            has_deterministic_tool or has_legacy_deterministic_rule
        ):
            return _blocked_route(
                executor_type,
                code="HYBRID_TOOL_MISSING",
                message="hybrid 规则缺少确定性能力 Tool",
            )
        required_artifact_types = (
            tuple(
                str(item).strip()
                for item in capability_config.get("required_artifact_types", [])
                if str(item).strip()
            )
            if isinstance(capability_config.get("required_artifact_types"), list)
            else ()
        )
        return RuleExecutionRoute(
            strategy=executor_type,
            ready=True,
            rule_engine_ready=False,
            tool_name="rule_judge_tool",
            handler="semantic_rule_judge",
            handler_tool_name=sorted(selected_semantic_tools)[0],
            judge_input_source="trusted_observation_packet",
            required_input_fields=(),
            required_artifact_types=required_artifact_types,
            blocked_reason=None,
        )

    if not isinstance(judge_config, dict):
        return _blocked_route(
            executor_type,
            code="JUDGE_CONFIG_INVALID",
            message="rule_engine 规则的 judge_config 必须是对象",
        )
    engine_config = judge_config.get("engine_config")
    if not isinstance(engine_config, dict):
        return _blocked_route(
            executor_type,
            code="ENGINE_CONFIG_MISSING",
            message="rule_engine 规则缺少有效 engine_config",
        )
    handler = engine_config.get("handler")
    if not isinstance(handler, str) or not handler:
        return _blocked_route(
            executor_type,
            code="HANDLER_MISSING",
            message="rule_engine 规则缺少 Handler",
        )
    spec = RULE_ENGINE_HANDLER_REGISTRY.get(handler)
    if spec is None:
        return _blocked_route(
            executor_type,
            code="HANDLER_NOT_REGISTERED",
            message=f"Handler 未注册：{handler}",
            handler=handler,
        )
    config_error = spec.validate_config(engine_config)
    if config_error is not None:
        return _blocked_route(
            executor_type,
            code="HANDLER_CONFIG_INVALID",
            message=config_error,
            handler=handler,
            handler_tool_name=spec.handler_tool_name,
        )

    return RuleExecutionRoute(
        strategy=executor_type,
        ready=True,
        rule_engine_ready=True,
        tool_name="rule_judge_tool",
        handler=handler,
        handler_tool_name=spec.handler_tool_name,
        judge_input_source=spec.judge_input_source,
        required_input_fields=(
            ()
            if spec.judge_input_source
            in {
                "trusted_evidence",
                "trusted_observation_packet",
            }
            else _required_input_fields(handler, engine_config)
        ),
        required_artifact_types=spec.required_artifact_types,
        blocked_reason=None,
    )


def rule_execution_route_payload(
    *,
    executor_type: str,
    judge_config: dict[str, Any],
    input_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """将执行路由转换为 Tool/API 可直接返回的 JSON 结构。"""
    route = resolve_rule_execution_route(
        executor_type=executor_type,
        judge_config=judge_config,
    )
    payload = asdict(route)
    payload["required_input_fields"] = list(route.required_input_fields)
    payload["required_artifact_types"] = list(route.required_artifact_types)
    payload["session_dependency"] = derive_session_dependency(
        input_contract=input_contract,
        judge_config=judge_config,
    )
    return payload


async def validate_enabled_rule_engine_configuration(
    repository: RuleEngineConfigurationRepository,
) -> None:
    """阻止完整质检在任一启用规则缺少可执行 Judge 路由时继续。"""
    invalid_rules: list[str] = []
    for definition in await repository.list_enabled_rule_judge_definitions():
        route = resolve_rule_execution_route(
            executor_type=definition.executor_type,
            judge_config=definition.judge_config,
        )
        if route.ready:
            continue
        reason = route.blocked_reason or {
            "code": "RULE_ENGINE_NOT_READY",
            "message": "规则引擎配置不可执行",
        }
        invalid_rules.append(f"{definition.rule_code}[{reason['code']}]: {reason['message']}")
    if invalid_rules:
        raise RuleEngineConfigurationError(
            "启用规则存在不可执行 Judge 配置：" + "；".join(invalid_rules)
        )


class RuleEngineConfigurationGuard:
    """按任务模式延迟校验规则配置，并只缓存成功结果。"""

    def __init__(self, repository: RuleEngineConfigurationRepository) -> None:
        self._repository = repository
        self._ready = False
        self._lock = asyncio.Lock()

    async def ensure_ready_for(self, execution_mode: TaskExecutionMode) -> None:
        if not isinstance(execution_mode, TaskExecutionMode):
            raise TypeError("execution_mode 必须为 TaskExecutionMode")
        if execution_mode is TaskExecutionMode.EXPLORATION_ONLY or self._ready:
            return
        async with self._lock:
            if self._ready:
                return
            await validate_enabled_rule_engine_configuration(self._repository)
            self._ready = True


class RuleJudgeService:
    """统一执行已配置的确定性、语义与混合规则。"""

    def __init__(
        self,
        *,
        repository: RuleJudgeRepository,
        evidence_reader: RuleJudgeEvidenceReader | None = None,
        qr_validator: Callable[..., Awaitable[dict[str, Any]]] | None = None,
        observation_packet_service: ObservationPacketService | None = None,
        semantic_judge: SemanticRuleJudge | None = None,
        verdict_mode: VerdictMode = "strict",
        manifest_quality_authoritative: bool = False,
    ) -> None:
        self._repository = repository
        self._evidence_reader = evidence_reader
        self._qr_validator = qr_validator or _validate_qr_link
        self._observation_packet_service = observation_packet_service
        self._semantic_judge = semantic_judge
        self._verdict_mode = verdict_mode
        # E02 条款 1.4：清单成为齐全性权威后，判定侧退出自算证据齐不齐——
        # 采集角色缺失预检并入需求集裁决，非 pass 改为按清单结论分型拒判。
        self._manifest_quality_authoritative = manifest_quality_authoritative

    def _manual_test_mode(self) -> bool:
        return bool(getattr(self._semantic_judge, "_manual_test", False))

    def _visual_fallback_allowed(self, packet: ObservationPacket) -> bool:
        """结构 / 动作证据拿不到时（人工截图测试，或 App / Flutter 等视觉产品），
        允许绕过硬性成对门禁、改由语义模型看截图判。仅在确有截图时生效。"""
        manual = self._manual_test_mode()
        page = packet.packet_payload.get("page") if isinstance(packet.packet_payload, dict) else {}
        surface = str((page or {}).get("surface_kind") or "").casefold()
        visual_surface = surface in {"android", "ios"}
        return (manual or visual_surface) and _packet_has_screenshot(packet)

    async def judge(self, request: RuleJudgeRequest) -> RuleJudgeResult:
        _validate_request(
            request,
            require_evidence_artifact_ids=self._observation_packet_service is None,
        )
        definition = await self._repository.get_rule_judge_definition(
            task_id=request.task_id,
            rule_code=request.rule_code,
        )
        if definition is None:
            raise RuleJudgeTargetNotFoundError(
                f"未找到任务锁定规则：task_id={request.task_id}, rule_code={request.rule_code}"
            )
        _validate_locked_rule_contract(definition, request)
        from services.api.app.application.green_rule_catalog import (
            validate_green_rule_configuration,
        )

        try:
            green_contract = validate_green_rule_configuration(asdict(definition))
        except ValueError as exc:
            raise RuleJudgeValidationError(str(exc)) from exc
        route = resolve_rule_execution_route(
            executor_type=definition.executor_type,
            judge_config=definition.judge_config,
        )
        if not route.ready or route.handler is None:
            reason = route.blocked_reason or {"message": "规则尚未接入统一判定 Handler"}
            raise RuleJudgeNotConfiguredError(
                f"规则 {definition.rule_code} 不可由统一判定引擎执行：{reason['message']}"
            )

        packet_for_request: ObservationPacket | None = None

        async def load_packet() -> ObservationPacket:
            nonlocal packet_for_request
            if packet_for_request is None:
                packet_for_request = await self._build_observation_packet(definition, request)
            return packet_for_request

        if (
            definition.rule_code == "alioth_ops_10"
            and self._observation_packet_service is not None
        ):
            packet = await load_packet()
            if (
                _packet_surface_is_app(packet)
                and not _app_same_chapter_reading_observed(packet)
            ):
                return _build_evidence_insufficient_result(
                    definition=definition,
                    request=request,
                    packet=packet,
                    reason=_APP_COMIC_TURN_GAP,
                )

        if (
            definition.rule_code == "alioth_ops_6"
            and self._observation_packet_service is not None
        ):
            packet = await load_packet()
            if (
                _packet_surface_is_app(packet)
                and not _app_share_chain_complete(packet)
            ):
                return _build_evidence_insufficient_result(
                    definition=definition,
                    request=request,
                    packet=packet,
                    reason=_APP_SHARE_CHAIN_GAP,
                )

        if (
            definition.rule_code == "alioth_ops_7"
            and self._observation_packet_service is not None
        ):
            packet = await load_packet()
            if (
                _packet_surface_is_app(packet)
                and _app_official_group_gap(packet)
            ):
                return _build_evidence_insufficient_result(
                    definition=definition,
                    request=request,
                    packet=packet,
                    reason=_APP_GROUP_GAP,
                )

        if (
            definition.rule_code == "alioth_ops_9"
            and self._observation_packet_service is not None
        ):
            packet = await load_packet()
            if (
                _packet_surface_is_app(packet)
                and not _app_episode_switch_complete(packet)
            ):
                return _build_evidence_insufficient_result(
                    definition=definition,
                    request=request,
                    packet=packet,
                    reason=_APP_EPISODE_SWITCH_GAP,
                )

        required_page_states = _required_page_states(definition)
        if (
            self._observation_packet_service is not None
            and definition.executor_type != RULE_ENGINE_EXECUTOR
        ):
            packet = await load_packet()
            unavailable_result = _target_document_preflight(
                definition=definition, request=request, packet=packet,
            )
            if unavailable_result is not None:
                return unavailable_result
        if required_page_states:
            packet = await load_packet()
            page_state_result = _required_page_state_preflight(
                definition=definition,
                request=request,
                packet=packet,
                required_page_states=required_page_states,
            )
            if page_state_result is not None:
                return page_state_result

        capability = definition.judge_config.get("capability_config") or {}
        if isinstance(capability, dict) and capability.get("batch_group") == "multi-client-pending":
            packet = await load_packet()
        unsupported_sampling_result = _unsupported_sampling_preflight(
            definition=definition,
            request=request,
            packet=packet_for_request,
        )
        if unsupported_sampling_result is not None:
            return unsupported_sampling_result

        # 人工截图测试没有采集角色，来源页和列表页按截图先后交模型看，不在这里拦
        if not self._manual_test_mode():
            scenario_result = _scenario_trigger_preflight(definition=definition, request=request)
            if scenario_result is not None:
                return scenario_result

        if (
            _declared_auth_requirement(definition) is not None
            and self._observation_packet_service is not None
        ):
            # 要登录（或只在未登录时）才有意义的规则：采集侧声明了登录态就直接读，
            # 放在采集角色检查之前，避免未登录任务把这类规则记成「缺采集角色」
            try:
                packet = await load_packet()
            except ObservationPacketValidationError:
                packet = None
            if packet is not None:
                session_auth_result = _session_auth_preflight(
                    definition=definition,
                    request=request,
                    packet=packet,
                )
                if session_auth_result is not None:
                    return session_auth_result

        # 清单权威时，采集角色齐不齐已由需求集裁决（E14），判定侧不再自算
        missing_capture_roles = (
            []
            if self._manifest_quality_authoritative
            else _missing_required_capture_roles(
                input_contract=definition.input_contract,
                evidence_capture_roles=request.evidence_capture_roles,
            )
        )
        if missing_capture_roles:
            packet = await load_packet()
            applicability_result = _balanced_applicability_preflight(
                definition=definition,
                request=request,
                packet=packet,
                missing_capture_roles=missing_capture_roles,
                verdict_mode=self._verdict_mode,
            )
            if applicability_result is not None:
                return applicability_result
            return _build_evidence_insufficient_result(
                definition=definition,
                request=request,
                packet=packet,
                reason=("当前旅程未形成规则要求的采集角色：" + "、".join(missing_capture_roles)),
                missing_capture_roles=missing_capture_roles,
            )

        if definition.executor_type != RULE_ENGINE_EXECUTOR:
            packet = await load_packet()
            presentation_result = _presentation_preflight(
                definition=definition,
                request=request,
                packet=packet,
            )
            if presentation_result is not None:
                return presentation_result
            if packet.quality_gate_status != "pass" or _h5_payload_budget_exceeded(packet):
                return _build_quality_gate_block_result(
                    definition=definition,
                    request=request,
                    packet=packet,
                )
            if green_contract is not None:
                from services.api.app.application.green_page_judge import judge_green_page

                return await judge_green_page(
                    definition=definition, request=request, packet=packet,
                    contract=green_contract, evidence_reader=self._evidence_reader,
                    semantic_judge=self._semantic_judge,
                )
            return await self._judge_semantic(
                definition=definition,
                request=request,
                packet=packet,
            )

        if not route.rule_engine_ready:
            raise RuleJudgeNotConfiguredError(
                f"规则 {definition.rule_code} 的确定性 Handler 不可执行"
            )
        engine_config = definition.judge_config["engine_config"]
        handler = route.handler
        spec = RULE_ENGINE_HANDLER_REGISTRY[handler]
        if spec.executor is None:
            packet = await load_packet()
            presentation_result = _presentation_preflight(
                definition=definition,
                request=request,
                packet=packet,
            )
            if presentation_result is not None:
                return presentation_result
            if packet.quality_gate_status != "pass" or _h5_payload_budget_exceeded(packet):
                return _build_quality_gate_block_result(
                    definition=definition,
                    request=request,
                    packet=packet,
                )
            capability_result = await evaluate_rule_engine_handler(
                handler=handler,
                packet_payload=packet.packet_payload,
                engine_config=engine_config,
                qr_validator=self._qr_validator,
            )
            return _build_capability_result(
                definition=definition,
                request=request,
                packet=packet,
                capability_result=capability_result,
            )
        if self._evidence_reader is None:
            raise RuleJudgeNotConfiguredError(f"规则 {definition.rule_code} 缺少可信证据读取器")
        packet = None
        if self._observation_packet_service is not None:
            # 确定性 Handler 走可信证据包，但展示层门禁必须一致：底层被遮挡时
            # 不能拿被挡住的 DOM/截图去判"页面可见"类规则。
            try:
                packet = await load_packet()
            except ObservationPacketValidationError:
                packet = None
            if packet is not None:
                presentation_result = _presentation_preflight(
                    definition=definition,
                    request=request,
                    packet=packet,
                )
                if presentation_result is not None:
                    return presentation_result
        app_surface = packet is not None and _packet_surface_is_app(packet)
        required_artifact_types = (
            tuple(
                item
                for item in spec.required_artifact_types
                if item not in spec.app_optional_artifact_types
            )
            if app_surface
            else spec.required_artifact_types
        )
        try:
            evidence_read_options: dict[str, Any] = {}
            if request.evidence_source_page_capture_id is not None:
                evidence_read_options["evidence_source_page_capture_id"] = (
                    request.evidence_source_page_capture_id
                )
            if request.evidence_manifest_id is not None:
                evidence_read_options["evidence_manifest_id"] = request.evidence_manifest_id
            if request.evidence_manifest_refs is not None:
                evidence_read_options["evidence_manifest_refs"] = request.evidence_manifest_refs
            if request.retained_baseline_artifact_ids is not None:
                evidence_read_options["retained_baseline_artifact_ids"] = (
                    request.retained_baseline_artifact_ids
                )
            if packet is not None:
                evidence_read_options["frozen_observation_packet"] = packet
            evidence_bundle = await self._evidence_reader.prepare_observation_packet(
                task_id=request.task_id,
                agent_run_id=request.agent_run_id,
                source_agent_run_id=request.source_agent_run_id,
                rule_id=definition.rule_id,
                page_capture_id=request.page_capture_id,
                evidence_capture_roles=_scoped_evidence_capture_roles(
                    input_contract=definition.input_contract,
                    evidence_capture_roles=request.evidence_capture_roles,
                ),
                evidence_artifact_ids=request.evidence_artifact_ids,
                required_artifact_types=required_artifact_types,
                **evidence_read_options,
            )
        except RuleJudgeEvidenceInsufficientError as exc:
            return _build_runtime_evidence_insufficient_result(
                definition=definition,
                request=request,
                observation_packet_id=None,
                evidence_artifact_ids=request.evidence_artifact_ids,
                reason=str(exc),
                packet=packet,
            )
        if packet is not None and packet.quality_gate_status != "pass":
            return _build_quality_gate_block_result(
                definition=definition, request=request, packet=packet,
            )
        if packet is None:
            # 观察包建不出来（如收口补判引用的采集不属于当前判定 Run）时，展示层门禁
            # 改读可信证据包带回的持久化评估，避免诊断材料从这条路绕过门禁判 pass。
            bundle_presentation = _bundle_presentation_for(
                evidence_bundle,
                request.evidence_source_page_capture_id or request.page_capture_id,
            )
            if bundle_presentation is not None:
                presentation_result = _presentation_rejection(
                    definition=definition,
                    request=request,
                    presentation=bundle_presentation,
                    packet=None,
                    observation_packet_id=evidence_bundle.observation_packet_id,
                    evidence_artifact_ids=_trusted_bundle_artifact_ids(
                        evidence_bundle,
                        fallback=request.evidence_artifact_ids,
                    ),
                )
                if presentation_result is not None:
                    return presentation_result
        try:
            result = await spec.executor(
                runtime=RuleEngineRuntime(
                    evidence_reader=self._evidence_reader,
                    evidence_bundle=evidence_bundle,
                    qr_validator=self._qr_validator,
                    verdict_mode=self._verdict_mode,
                    app_surface=app_surface,
                    h5_surface=(packet is not None and _packet_surface_is_h5(packet)),
                ),
                definition=definition,
                request=request,
                engine_config=engine_config,
            )
        except RuleJudgeEvidenceInsufficientError as exc:
            return _build_runtime_evidence_insufficient_result(
                definition=definition,
                request=request,
                observation_packet_id=evidence_bundle.observation_packet_id,
                evidence_artifact_ids=_trusted_bundle_artifact_ids(
                    evidence_bundle,
                    fallback=request.evidence_artifact_ids,
                ),
                reason=str(exc),
                packet=packet,
                auto_recapture_block_reason=(
                    "H5_PRICE_LOCATION_TARGET_UNAVAILABLE"
                    if isinstance(exc, RuleJudgePriceTargetMissingError) else None
                ),
            )
        return replace(
            result,
            observation_packet_id=evidence_bundle.observation_packet_id,
            evidence_artifact_ids=_trusted_bundle_artifact_ids(
                evidence_bundle,
                fallback=request.evidence_artifact_ids,
            ),
        )

    async def judge_many(self, requests: list[RuleJudgeRequest]) -> list[RuleJudgeResult]:
        """同页语义规则共享证据解码并使用最多四条的小批次模型请求。"""
        if not requests:
            return []
        definitions: list[RuleJudgeDefinition] = []
        for request in requests:
            _validate_request(
                request,
                require_evidence_artifact_ids=self._observation_packet_service is None,
            )
            definition = await self._repository.get_rule_judge_definition(
                task_id=request.task_id,
                rule_code=request.rule_code,
            )
            if definition is None:
                raise RuleJudgeTargetNotFoundError(f"未找到任务锁定规则：{request.rule_code}")
            _validate_locked_rule_contract(definition, request)
            definitions.append(definition)
        if any(item.executor_type == RULE_ENGINE_EXECUTOR
               or _has_new_or_invalid_green_contract(item) for item in definitions):
            # 逐条兜底：单条规则的契约或证据问题只落到自己头上，
            # 裸列表推导会让第一条异常吞掉整批已判结果并使工作项整体失败。
            results: list[RuleJudgeResult] = []
            # 只复用本批次的已校验原文，不共享规则结果或跨 Attempt 的读取状态。
            read_batch = getattr(self._evidence_reader, "read_batch", None)
            with read_batch() if callable(read_batch) else nullcontext():
                for definition, request in zip(definitions, requests, strict=True):
                    try:
                        results.append(await self.judge(request))
                    except ObservationPacketValidationError as exc:
                        results.append(
                            _build_batch_observation_packet_error_result(
                                definition=definition,
                                request=request,
                                error=exc,
                            )
                        )
                    except RuleJudgeValidationError as exc:
                        results.append(
                            _build_batch_isolated_failure_result(
                                definition=definition,
                                request=request,
                                reason=str(exc),
                            )
                        )
            return results
        if self._semantic_judge is None:
            raise RuleJudgeNotConfiguredError("语义批次缺少独立 Judge")

        results: list[RuleJudgeResult | None] = [None] * len(requests)
        semantic_items: list[
            tuple[
                int,
                RuleJudgeDefinition,
                RuleJudgeRequest,
                ObservationPacket,
                list[dict[str, Any]],
            ]
        ] = []
        for index, (definition, request) in enumerate(zip(definitions, requests, strict=True)):
            route = resolve_rule_execution_route(
                executor_type=definition.executor_type,
                judge_config=definition.judge_config,
            )
            if not route.ready:
                raise RuleJudgeNotConfiguredError(
                    f"规则 {definition.rule_code} 不可由统一判定引擎执行"
                )
            try:
                packet = await self._build_observation_packet(definition, request)
            except ObservationPacketValidationError as exc:
                results[index] = _build_batch_observation_packet_error_result(
                    definition=definition,
                    request=request,
                    error=exc,
                )
                continue
            required_page_states = _required_page_states(definition)
            unavailable_result = _target_document_preflight(
                definition=definition, request=request, packet=packet,
            )
            if unavailable_result is not None:
                results[index] = unavailable_result
                continue
            page_state_result = (
                _required_page_state_preflight(
                    definition=definition,
                    request=request,
                    packet=packet,
                    required_page_states=required_page_states,
                )
                if required_page_states
                else None
            )
            if page_state_result is not None:
                results[index] = page_state_result
                continue
            declared_skip_result = (
                _unsupported_sampling_preflight(
                    definition=definition,
                    request=request,
                    packet=packet,
                )
                or (
                    None
                    if self._manual_test_mode()
                    else _scenario_trigger_preflight(
                        definition=definition,
                        request=request,
                        packet=packet,
                    )
                )
                or _session_auth_preflight(
                    definition=definition,
                    request=request,
                    packet=packet,
                )
            )
            if declared_skip_result is not None:
                results[index] = declared_skip_result
                continue
            presentation_result = _presentation_preflight(
                definition=definition,
                request=request,
                packet=packet,
            )
            if presentation_result is not None:
                results[index] = presentation_result
                continue
            missing_capture_roles = (
                []
                if self._manifest_quality_authoritative
                else _missing_required_capture_roles(
                    input_contract=definition.input_contract,
                    evidence_capture_roles=request.evidence_capture_roles,
                )
            )
            if missing_capture_roles:
                applicability_result = _balanced_applicability_preflight(
                    definition=definition,
                    request=request,
                    packet=packet,
                    missing_capture_roles=missing_capture_roles,
                    verdict_mode=self._verdict_mode,
                )
                results[index] = applicability_result or _build_evidence_insufficient_result(
                    definition=definition,
                    request=request,
                    packet=packet,
                    reason=(
                        "当前旅程未形成规则要求的采集角色：" + "、".join(missing_capture_roles)
                    ),
                    missing_capture_roles=missing_capture_roles,
                )
                continue
            if packet.quality_gate_status != "pass" or _h5_payload_budget_exceeded(packet):
                results[index] = _build_quality_gate_block_result(
                    definition=definition,
                    request=request,
                    packet=packet,
                )
                continue
            capability_config = definition.judge_config.get("capability_config")
            capability_config = capability_config if isinstance(capability_config, dict) else {}
            tool_results = (
                run_hybrid_capability_tools(
                    packet_payload=packet.packet_payload,
                    capability_config=capability_config,
                    thresholds=(
                        definition.judge_config.get("thresholds")
                        if isinstance(definition.judge_config.get("thresholds"), dict)
                        else {}
                    ),
                )
                if definition.executor_type == "hybrid"
                else []
            )
            preflight_result = _hybrid_preflight_result(
                definition=definition,
                request=request,
                packet=packet,
                tool_results=tool_results,
                allow_visual_fallback=self._visual_fallback_allowed(packet),
            ) or await _startup_ad_skip_applicability_preflight(
                definition=definition,
                request=request,
                packet=packet,
                capability_config=capability_config,
            )
            if preflight_result is not None:
                results[index] = preflight_result
                continue
            semantic_items.append((index, definition, request, packet, tool_results))
        if semantic_items:
            judge_batch = getattr(self._semantic_judge, "judge_batch", None)
            if callable(judge_batch) and len(semantic_items) > 1:
                outputs: dict[str, SemanticJudgeModelOutput] = {}
                for offset in range(0, len(semantic_items), MAX_SEMANTIC_JUDGE_BATCH_SIZE):
                    chunk = semantic_items[offset : offset + MAX_SEMANTIC_JUDGE_BATCH_SIZE]
                    outputs.update(
                        await judge_batch(
                            items=[
                                (packet, tool_results) for _, _, _, packet, tool_results in chunk
                            ]
                        )
                    )
            else:
                outputs = {
                    packet.rule_code: await self._semantic_judge.judge(
                        packet=packet,
                        tool_results=tool_results,
                    )
                    for _, _, _, packet, tool_results in semantic_items
                }
            for index, definition, request, packet, tool_results in semantic_items:
                model_output = outputs.get(definition.rule_code)
                if model_output is None:
                    raise RuleJudgeValidationError(f"语义批次缺少规则输出：{definition.rule_code}")
                results[index] = _semantic_result_from_output(
                    definition=definition,
                    request=request,
                    packet=packet,
                    tool_results=tool_results,
                    model_output=model_output,
                )
        return [item for item in results if item is not None]

    async def _build_observation_packet(
        self,
        definition: RuleJudgeDefinition,
        request: RuleJudgeRequest,
    ) -> ObservationPacket:
        if self._observation_packet_service is None:
            raise RuleJudgeNotConfiguredError(
                f"规则 {definition.rule_code} 缺少 Observation Packet 服务"
            )
        return await self._observation_packet_service.build(
            BuildObservationPacketRequest(
                task_id=request.task_id,
                page_capture_id=request.page_capture_id,
                evidence_source_page_capture_id=request.evidence_source_page_capture_id,
                evidence_manifest_id=request.evidence_manifest_id,
                evidence_capture_roles=request.evidence_capture_roles,
                evidence_manifest_refs=request.evidence_manifest_refs,
                retained_baseline_artifact_ids=request.retained_baseline_artifact_ids,
                rule_id=definition.rule_id,
                evidence_input_set_id=request.evidence_input_set_id,
                original_page_input_set_id=request.original_page_input_set_id,
                work_item_id=request.work_item_id,
                attempt_id=request.attempt_id,
                lease_token=request.lease_token,
                expected_contract_hash=request.expected_contract_hash,
                rule_code=definition.rule_code,
                rule_name=definition.rule_name or definition.rule_code,
                category=definition.category,
                executor_type=definition.executor_type,
                check_point=definition.check_point,
                input_contract=definition.input_contract,
                judge_config=definition.judge_config,
                manual_review_policy=definition.manual_review_policy,
                requested_evidence_artifact_ids=request.evidence_artifact_ids,
                agent_input=request.judge_input,
                agent_run_id=request.agent_run_id,
                source_agent_run_id=request.source_agent_run_id,
            )
        )

    async def _judge_semantic(
        self,
        *,
        definition: RuleJudgeDefinition,
        request: RuleJudgeRequest,
        packet: ObservationPacket,
    ) -> RuleJudgeResult:
        if self._semantic_judge is None:
            raise RuleJudgeNotConfiguredError(f"规则 {definition.rule_code} 缺少独立语义 Judge")
        capability_config = definition.judge_config.get("capability_config")
        capability_config = capability_config if isinstance(capability_config, dict) else {}
        tool_results = (
            run_hybrid_capability_tools(
                packet_payload=packet.packet_payload,
                capability_config=capability_config,
                thresholds=(
                    definition.judge_config.get("thresholds")
                    if isinstance(definition.judge_config.get("thresholds"), dict)
                    else {}
                ),
            )
            if definition.executor_type == "hybrid"
            else []
        )
        preflight_result = _hybrid_preflight_result(
            definition=definition,
            request=request,
            packet=packet,
            tool_results=tool_results,
            allow_visual_fallback=self._visual_fallback_allowed(packet),
        ) or await _startup_ad_skip_applicability_preflight(
            definition=definition,
            request=request,
            packet=packet,
            capability_config=capability_config,
        )
        if preflight_result is not None:
            return preflight_result
        model_output = await self._semantic_judge.judge(
            packet=packet,
            tool_results=tool_results,
        )
        return _semantic_result_from_output(
            definition=definition,
            request=request,
            packet=packet,
            tool_results=tool_results,
            model_output=model_output,
        )


def _has_new_or_invalid_green_contract(definition: RuleJudgeDefinition) -> bool:
    """已知旧绿色合同保留原批处理；新声明及损坏声明逐条拒绝，避免吞掉同批旧结论。"""
    if "green_rule_contract" not in definition.judge_config:
        return False
    contract = definition.judge_config["green_rule_contract"]
    return not (
        isinstance(contract, Mapping)
        and contract.get("schema_version") == "green-rule-contract.v1"
        and type(contract.get("ruleset_version")) is int
        and contract["ruleset_version"] in (1, 2, 3)
    )


def _semantic_result_from_output(
    *,
    definition: RuleJudgeDefinition,
    request: RuleJudgeRequest,
    packet: ObservationPacket,
    tool_results: list[dict[str, Any]],
    model_output: SemanticJudgeModelOutput,
) -> RuleJudgeResult:
    # 系统级硬约束：任何进入持久化与报告的判定文案不得包含业务定性词。
    sanitized_output = sanitize_semantic_judge_output(model_output)
    if sanitized_output is not model_output:
        logger.warning(
            "rule_code=%s 语义判定文案含禁止的业务定性词，已按措辞策略中性化。",
            definition.rule_code,
        )
        model_output = sanitized_output
    proposed_conclusion = model_output.conclusion
    execution_status = proposed_conclusion
    min_confidence = _min_confidence(definition.manual_review_policy)
    review_reason = model_output.review_reason
    cited_refs = set(model_output.evidence_refs)
    for finding in model_output.findings:
        cited_refs.update(finding.evidence_refs)
    allowed_refs = set(packet.evidence_artifact_ids)
    allowed_refs.update(label for label, _, _ in packet.image_payloads)
    invalid_refs = sorted(cited_refs - allowed_refs)
    if invalid_refs:
        execution_status = "manual_review"
        review_reason = "模型引用了不属于当前观察包的材料，需人工复核实际证据依据"
    elif proposed_conclusion == "pass" and not cited_refs:
        execution_status = "manual_review"
        review_reason = "模型通过结论未引用实际判定材料，需人工复核证据依据"
    if (
        execution_status in {"pass", "fail", "warning"}
        and model_output.confidence < min_confidence
    ):
        execution_status = "manual_review"
        review_reason = (
            review_reason
            or f"判定置信度 {model_output.confidence:.4f} 低于规则阈值 {min_confidence:.4f}"
        )
    compact_tool_results = _compact_tool_results(tool_results)
    model_payload = model_output.model_dump(mode="json")
    judge_output = {
        "schema_version": "1.0",
        "conclusion": execution_status,
        "reasoning_summary": model_output.reasoning_summary,
        "model_output": model_payload,
        "tool_results": compact_tool_results,
        **({"invalid_evidence_refs": invalid_refs} if invalid_refs else {}),
        **(
            {
                "proposed_conclusion": proposed_conclusion,
                "confidence_threshold": min_confidence,
            }
            if execution_status != proposed_conclusion
            else {}
        ),
    }
    if definition.executor_type == "hybrid" and not compact_tool_results:
        judge_output["tool_results"] = [
            {
                "tool_name": "capability_pipeline",
                "status": "insufficient",
                "reason": "数据库未配置可执行的确定性能力 Tool",
            }
        ]
    problem_desc = _semantic_problem_desc(
        execution_status=execution_status,
        model_payload=model_payload,
        review_reason=review_reason,
    )
    result = RuleJudgeResult(
        rule_id=definition.rule_id,
        rule_code=definition.rule_code,
        executor_type=definition.executor_type,
        judge_strategy=definition.executor_type,
        execution_status=execution_status,
        confidence=model_output.confidence,
        result_summary=model_output.reasoning_summary[:600],
        problem_desc=problem_desc,
        page_capture_id=request.page_capture_id,
        observation_packet_id=packet.observation_packet_id,
        evidence_artifact_ids=packet.evidence_artifact_ids,
        judge_input=_observation_judge_input(packet, compact_tool_results),
        judge_output=judge_output,
    )
    return _attach_evidence_gap(result, definition=definition, request=request, packet=packet)


_READING_TURN_ACTIONS = frozenset({"scroll", "swipe", "fling"})
_READING_TURN_TEXT = re.compile(r"翻页|下一页|下一话|继续阅读|继续看")
_APP_COMIC_TURN_GAP = "缺同章节正文的翻页或继续阅读操作及实际加载结果。"
_APP_SHARE_CHAIN_GAP = "缺分享链接生成、外部浏览器打开及源内容对应的完整操作链。"
_SHARE_LINK_TEXT = re.compile(r"https?://|分享链接|复制链接|share_url|shareUrl")
_SHARE_ACTION_TEXT = re.compile(r"分享|复制链接|邀请")
_EXTERNAL_BROWSER_TEXT = re.compile(r"外部浏览器|系统浏览器|chrome|browser|intent", re.I)
_SOURCE_CONTENT_TEXT = re.compile(r"源内容|来源内容|source_content|content_id")
_APP_GROUP_GAP = "只查首页或个人中心就判无群入口；已见入口还缺点击后的有效群地址。"
_APP_EPISODE_SWITCH_GAP = "缺同一剧集从第1集切到第2集的操作、集数变化及起播证据。"
_EPISODE_ONE = re.compile(r"第\s*1\s*集")
_EPISODE_TWO = re.compile(r"第\s*2\s*集")
_PLAYBACK_STARTED = re.compile(r"起播|ready_state|current_time|playing", re.I)
_GROUP_ENTRY_TEXT = re.compile(r"官方群|加群|官方交流|电报群|Telegram|Potato", re.I)
_GROUP_ADDRESS_TEXT = re.compile(r"https?://|t\.me/|tg://|二维码|群链接|群号")
_GROUP_NARROW_PAGES = frozenset({"home", "profile"})
_GROUP_WIDER_PAGES = frozenset({"settings", "activity", "landing"})


def _collect_reading_turns(value: Any, found: list[dict[str, Any]]) -> None:
    if isinstance(value, dict):
        action = str(
            value.get("action") or value.get("operation") or value.get("type") or ""
        ).casefold()
        semantic = " ".join(
            str(value.get(key) or "")
            for key in ("semantic_target", "target", "purpose", "label")
        )
        if action in _READING_TURN_ACTIONS or _READING_TURN_TEXT.search(semantic):
            found.append(value)
        for child in value.values():
            _collect_reading_turns(child, found)
    elif isinstance(value, list):
        for item in value:
            _collect_reading_turns(item, found)


def _surface_text(facts: Mapping[str, Any]) -> str:
    visual = facts.get("visual_observation")
    derived = ""
    if isinstance(visual, dict):
        derived = str(visual.get("derived_text") or "")
    return f" {facts.get('visible_text') or ''} {derived}"


def _script_recordings(packet: ObservationPacket) -> list[dict[str, Any]]:
    payload = packet.packet_payload if isinstance(packet.packet_payload, dict) else {}
    facts = payload.get("facts") if isinstance(payload.get("facts"), dict) else {}
    found: list[dict[str, Any]] = []
    direct = facts.get("script_recording")
    if isinstance(direct, dict):
        found.append(direct)
    for key in ("action_log", "action_trace", "actions"):
        records = facts.get(key)
        if not isinstance(records, list):
            continue
        for item in records:
            nested = item.get("script_recording") if isinstance(item, dict) else None
            if isinstance(nested, dict):
                found.append(nested)
    return found


def _agent_verifies_script_screenshots(_packet: ObservationPacket) -> bool:
    """動作後的圖才是證據。腳本自己存的截圖不代替那張圖。"""

    return False


def _script_shot_count(packet: ObservationPacket) -> int:
    count = 0
    for recording in _script_recordings(packet):
        shots = recording.get("shots")
        if not isinstance(shots, list):
            continue
        count += sum(
            1
            for item in shots
            if isinstance(item, dict) and str(item.get("artifact_id") or "").strip()
        )
    return count


def _script_reading_followup(packet: ObservationPacket) -> bool:
    """腳本裡的下滑，以及這次寫入證據的後續截圖，才算翻頁後有加載結果。"""

    for recording in _script_recordings(packet):
        if recording.get("verified") is not True:
            continue
        shots = recording.get("shots")
        shot_list = shots if isinstance(shots, list) else []
        names = {
            str(item.get("name") or "")
            for item in shot_list
            if isinstance(item, dict)
        }
        operations = recording.get("operations")
        operation_list = operations if isinstance(operations, list) else []
        turned = any(
            isinstance(item, dict)
            and item.get("kind") in {"swipe", "scroll"}
            and str(item.get("shot_after") or "") in names
            for item in operation_list
        )
        loaded = len(packet.image_payloads) >= 2 or _script_shot_count(packet) >= 2
        if turned and loaded:
            return True
    return False


def _app_same_chapter_reading_observed(packet: ObservationPacket) -> bool:
    """同章节里要有翻页或继续阅读，并且能看到翻页后的加载结果。"""

    if _script_reading_followup(packet):
        return True
    payload = packet.packet_payload if isinstance(packet.packet_payload, dict) else {}
    facts = payload.get("facts") if isinstance(payload.get("facts"), dict) else {}
    turns: list[dict[str, Any]] = []
    _collect_reading_turns(facts.get("action_trace"), turns)
    _collect_reading_turns(facts.get("actions"), turns)
    _collect_reading_turns(facts.get("action_log"), turns)
    if not turns:
        return False
    image_loads = facts.get("image_load_results")
    loaded = bool(image_loads) or len(packet.image_payloads) >= 2 or _script_shot_count(packet) >= 2
    return loaded


def _action_records(facts: Mapping[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            action = str(
                value.get("action") or value.get("operation") or value.get("type") or ""
            ).strip()
            if action:
                found.append(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(facts.get("action_trace"))
    walk(facts.get("actions"))
    walk(facts.get("action_log"))
    return found


def _app_share_chain_complete(packet: ObservationPacket) -> bool:
    """分享链要同时有链接生成、外部浏览器打开，以及和源内容的对应。"""

    payload = packet.packet_payload if isinstance(packet.packet_payload, dict) else {}
    facts = payload.get("facts") if isinstance(payload.get("facts"), dict) else {}
    records = _action_records(facts)
    if not records and not _surface_text(facts).strip():
        return False
    blob = json.dumps(records, ensure_ascii=False) + _surface_text(facts)
    generated = _SHARE_ACTION_TEXT.search(blob) and _SHARE_LINK_TEXT.search(blob)
    opened = _EXTERNAL_BROWSER_TEXT.search(blob) is not None
    linked = _SOURCE_CONTENT_TEXT.search(blob) is not None
    return bool(generated and opened and linked)


def _joined_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_joined_text(item) for item in value.values())
    if isinstance(value, list):
        return " ".join(_joined_text(item) for item in value)
    return ""


def _seen_page_types(packet: ObservationPacket) -> set[str]:
    payload = packet.packet_payload if isinstance(packet.packet_payload, dict) else {}
    facts = payload.get("facts") if isinstance(payload.get("facts"), dict) else {}
    page = payload.get("page") if isinstance(payload.get("page"), dict) else {}
    found: set[str] = set()
    for raw in (
        page.get("page_type"),
        payload.get("page_type"),
        facts.get("page_type"),
        facts.get("visited_page_types"),
        facts.get("page_types"),
    ):
        if isinstance(raw, str) and raw.strip():
            found.add(raw.strip().casefold())
        elif isinstance(raw, list):
            found.update(str(item).strip().casefold() for item in raw if str(item).strip())
    return found


def _app_official_group_gap(packet: ObservationPacket) -> bool:
    """只看了首页或个人中心，或看见入口却没有有效群地址，都不能下结论。"""

    payload = packet.packet_payload if isinstance(packet.packet_payload, dict) else {}
    facts = payload.get("facts") if isinstance(payload.get("facts"), dict) else {}
    visible = _joined_text(facts.get("visible_text")) + _surface_text(facts)
    records = _action_records(facts)
    record_text = json.dumps(records, ensure_ascii=False)
    entry_seen = bool(
        _GROUP_ENTRY_TEXT.search(visible) or _GROUP_ENTRY_TEXT.search(record_text)
    )
    clicked = any(
        _GROUP_ENTRY_TEXT.search(_joined_text(item))
        and str(item.get("action") or item.get("operation") or "").casefold()
        in {"click", "tap", "open"}
        for item in records
    )
    addressed = _GROUP_ADDRESS_TEXT.search(record_text) is not None
    if entry_seen and not (clicked and addressed):
        return True
    pages = _seen_page_types(packet)
    looked_wider = bool(pages & _GROUP_WIDER_PAGES)
    if not entry_seen and not looked_wider:
        return True
    return False


def _app_episode_switch_complete(packet: ObservationPacket) -> bool:
    """同一剧集要有第 1 集切到第 2 集的操作、集数变化和起播证据。"""

    payload = packet.packet_payload if isinstance(packet.packet_payload, dict) else {}
    facts = payload.get("facts") if isinstance(payload.get("facts"), dict) else {}
    records = _action_records(facts)
    record_text = json.dumps(records, ensure_ascii=False) + _surface_text(facts)
    media_text = _joined_text(facts.get("media_state"))
    switched = any(
        str(item.get("action") or item.get("operation") or "").casefold()
        in {"click", "tap", "open"}
        and _EPISODE_TWO.search(_joined_text(item))
        for item in records
    )
    changed = bool(_EPISODE_ONE.search(record_text) and _EPISODE_TWO.search(record_text))
    started = bool(_PLAYBACK_STARTED.search(record_text) or _PLAYBACK_STARTED.search(media_text))
    return switched and changed and started


def _packet_surface_is_app(packet: ObservationPacket) -> bool:
    page = packet.packet_payload.get("page") if isinstance(packet.packet_payload, dict) else None
    surface = str((page or {}).get("surface_kind") or "") if isinstance(page, dict) else ""
    return surface.casefold() in {"android", "ios"}


def _packet_surface_is_h5(packet: ObservationPacket) -> bool:
    payload = packet.packet_payload
    identity = payload.get("identity") if isinstance(payload, dict) else None
    page = payload.get("page") if isinstance(payload, dict) else None
    return bool(
        isinstance(identity, dict)
        and identity.get("target_platform") == "h5"
        and isinstance(page, dict)
        and page.get("surface_kind") == "web"
    )


def _packet_has_screenshot(packet: ObservationPacket) -> bool:
    """证据包里是否带截图（视觉兜底只在有图时才放行）。"""
    if packet.image_payloads:
        return True
    facts = packet.packet_payload.get("facts") if isinstance(packet.packet_payload, dict) else {}
    count = (facts or {}).get("screenshot_count") if isinstance(facts, dict) else None
    return isinstance(count, int) and not isinstance(count, bool) and count > 0


# 广告和落地页比对要的三份证据，按运营看得懂的说法列：内部工具名在写回时会被统一收成
# 「检测工具」，原因里直接拼工具名就只剩一句「……成对证据：检测工具」，看不出缺了什么。
_AD_LANDING_PAIR_EVIDENCE: tuple[tuple[str, str], ...] = (
    ("safe_click_observer_tool", "点击广告前后的页面截图"),
    ("landing_page_probe_tool", "点开广告后到达的落地页"),
    ("visual_semantic_compare_tool", "广告画面和落地页的比对"),
)


def _hybrid_preflight_result(
    *,
    definition: RuleJudgeDefinition,
    request: RuleJudgeRequest,
    packet: ObservationPacket,
    tool_results: list[dict[str, Any]],
    allow_visual_fallback: bool = False,
) -> RuleJudgeResult | None:
    """先处理确定性的“不适用”和平台能力缺失，避免交给模型误报证据不足。

    allow_visual_fallback 为真时（人工截图测试或 App / Flutter 视觉产品），结构 / 动作
    证据拿不到的成对门禁不再在此短路，改由语义模型看截图判。
    """
    if definition.executor_type != "hybrid":
        return None
    capability_config = definition.judge_config.get("capability_config")
    capability_config = capability_config if isinstance(capability_config, dict) else {}
    has_journey_observation = any(
        isinstance(result, dict)
        and result.get("status") == "ok"
        and result.get("tool_name")
        in {
            "safe_click_observer_tool",
            "landing_page_probe_tool",
            "visual_semantic_compare_tool",
        }
        for result in tool_results
    )
    if capability_config.get("batch_group") == "ad-fallback":
        # 没有设备侧广告加载故障时，不能把空白蒙层交给视觉模型判成兜底失败。
        fault = next(
            (
                item
                for item in tool_results
                if isinstance(item, dict)
                and item.get("tool_name")
                in {"network_fault_injection_tool", "fallback_state_assertion_tool"}
                and item.get("status") != "ok"
            ),
            None,
        )
        if fault is not None and fault.get("status") != "ok":
            facts = fault.get("facts") if isinstance(fault.get("facts"), dict) else {}
            gap_codes = [
                str(item)
                for item in list(facts.get("gap_codes") or [])
                if str(item).strip()
            ] or ["ad_load_fault_injection_unsupported"]
            reason = str(fault.get("reason") or "未支持设备上的广告加载故障注入")
            return _semantic_result_from_output(
                definition=definition,
                request=request,
                packet=packet,
                tool_results=tool_results,
                model_output=SemanticJudgeModelOutput(
                    conclusion="evidence_insufficient",
                    confidence=0.0,
                    reasoning_summary=reason,
                    conflicts=gap_codes,
                    missing_evidence=gap_codes,
                    review_reason=reason,
                ),
            )
    if capability_config.get("batch_group") == "cover-content":
        # 封面规则不能把广告图或仍停在列表上的补采交给视觉模型硬比。
        visual = next(
            (
                item
                for item in tool_results
                if isinstance(item, dict)
                and item.get("tool_name") == "visual_semantic_compare_tool"
            ),
            None,
        )
        if visual is not None and visual.get("status") != "ok":
            facts = visual.get("facts") if isinstance(visual.get("facts"), dict) else {}
            gap_codes = [
                str(item)
                for item in list(facts.get("gap_codes") or [])
                if str(item).strip()
            ] or ["cover_content_unpaired"]
            reason = str(visual.get("reason") or "封面与正文或播放画面没有按同一内容配对")
            return _semantic_result_from_output(
                definition=definition,
                request=request,
                packet=packet,
                tool_results=tool_results,
                model_output=SemanticJudgeModelOutput(
                    conclusion="evidence_insufficient",
                    confidence=0.0,
                    reasoning_summary=reason,
                    conflicts=gap_codes,
                    missing_evidence=gap_codes,
                    review_reason=reason,
                ),
            )
    if capability_config.get("batch_group") == "ad-landing":
        mandatory_tools = {
            "safe_click_observer_tool",
            "landing_page_probe_tool",
            "visual_semantic_compare_tool",
        }
        statuses = {
            str(item.get("tool_name") or ""): str(item.get("status") or "")
            for item in tool_results
            if isinstance(item, dict)
        }
        incomplete_tools = sorted(
            tool_name for tool_name in mandatory_tools if statuses.get(tool_name) != "ok"
        )
        if incomplete_tools and not allow_visual_fallback:
            missing = "、".join(
                label
                for tool_name, label in _AD_LANDING_PAIR_EVIDENCE
                if tool_name in incomplete_tools
            )
            reason = f"没拿到同一次点击广告的完整证据，没法比对广告和落地页是否一致：缺少{missing}"
            return _semantic_result_from_output(
                definition=definition,
                request=request,
                packet=packet,
                tool_results=tool_results,
                model_output=SemanticJudgeModelOutput(
                    conclusion="evidence_insufficient",
                    confidence=0.0,
                    reasoning_summary=reason,
                    conflicts=["ad_landing_evidence_pair_incomplete"],
                    missing_evidence=incomplete_tools,
                    review_reason=reason,
                ),
            )
    control_gap = {
        "player-controls": PROGRESS_BAR_STATIC_PAGE_GAP,
        "service-entry": SERVICE_ENTRY_ARRIVAL_GAP,
    }.get(capability_config.get("batch_group"))
    if control_gap is not None:
        # 缺拖动时间或未点进客服页时，不能把当前静态页交给模型下业务结论。
        control = next(
            (
                item
                for item in tool_results
                if isinstance(item, dict)
                and item.get("tool_name") == "control_behavior_assertion_tool"
                and item.get("status") != "ok"
            ),
            None,
        )
        if control is not None:
            facts = control.get("facts") if isinstance(control.get("facts"), dict) else {}
            gap_codes = [
                str(item) for item in list(facts.get("gap_codes") or []) if str(item).strip()
            ]
            if control_gap in gap_codes:
                reason = str(control.get("reason") or "当前页面缺少完成该规则所需的操作证据")
                return _semantic_result_from_output(
                    definition=definition,
                    request=request,
                    packet=packet,
                    tool_results=tool_results,
                    model_output=SemanticJudgeModelOutput(
                        conclusion="evidence_insufficient",
                        confidence=0.0,
                        reasoning_summary=reason,
                        conflicts=gap_codes,
                        missing_evidence=gap_codes,
                        review_reason=reason,
                    ),
                )
    if capability_config.get("batch_group") == "payment-entry":
        payment = next(
            (
                item
                for item in tool_results
                if isinstance(item, dict)
                and item.get("tool_name") == "interaction_state_diff_tool"
                and item.get("status") != "ok"
            ),
            None,
        )
        if payment is not None:
            payment_facts = payment.get("facts") if isinstance(payment.get("facts"), dict) else {}
            gap_codes = [
                str(item)
                for item in list(payment_facts.get("gap_codes") or [])
                if str(item).strip()
            ]
            if PAYMENT_ENTRY_CHAIN_GAP in gap_codes:
                reason = str(payment.get("reason") or PAYMENT_ENTRY_CHAIN_GAP)
                return _semantic_result_from_output(
                    definition=definition,
                    request=request,
                    packet=packet,
                    tool_results=tool_results,
                    model_output=SemanticJudgeModelOutput(
                        conclusion="evidence_insufficient",
                        confidence=0.0,
                        reasoning_summary=reason,
                        conflicts=gap_codes,
                        missing_evidence=gap_codes,
                        review_reason=reason,
                    ),
                )
    if capability_config.get("batch_group") == "player-playback":
        media = next(
            (
                item
                for item in tool_results
                if isinstance(item, dict)
                and item.get("tool_name") == "media_element_extractor_tool"
                and item.get("status") != "ok"
            ),
            None,
        )
        if media is not None:
            media_facts = media.get("facts") if isinstance(media.get("facts"), dict) else {}
            gap_codes = [
                str(item)
                for item in list(media_facts.get("gap_codes") or [])
                if str(item).strip()
            ]
            if NATIVE_PLAYBACK_SWITCH_GAP in gap_codes:
                reason = str(media.get("reason") or NATIVE_PLAYBACK_SWITCH_GAP)
                return _semantic_result_from_output(
                    definition=definition,
                    request=request,
                    packet=packet,
                    tool_results=tool_results,
                    model_output=SemanticJudgeModelOutput(
                        conclusion="evidence_insufficient",
                        confidence=0.0,
                        reasoning_summary=reason,
                        conflicts=gap_codes,
                        missing_evidence=gap_codes,
                        review_reason=reason,
                    ),
                )
    if capability_config.get("batch_group") == "visual-layout":
        geometry = next(
            (
                item
                for item in tool_results
                if isinstance(item, dict)
                and item.get("tool_name") == "layout_geometry_assertion_handler"
                and item.get("status") != "ok"
            ),
            None,
        )
        if geometry is not None:
            geometry_facts = (
                geometry.get("facts") if isinstance(geometry.get("facts"), dict) else {}
            )
            gap_codes = [
                str(item)
                for item in list(geometry_facts.get("gap_codes") or [])
                if str(item).strip()
            ]
            if "recharge_layout_screen_mismatch" in gap_codes:
                reason = str(
                    geometry.get("reason")
                    or "充值页截图与结构信息不属于同一界面，无法检查控件间距。"
                )
                return _semantic_result_from_output(
                    definition=definition,
                    request=request,
                    packet=packet,
                    tool_results=tool_results,
                    model_output=SemanticJudgeModelOutput(
                        conclusion="evidence_insufficient",
                        confidence=0.0,
                        reasoning_summary=reason,
                        conflicts=gap_codes,
                        missing_evidence=gap_codes,
                        review_reason=reason,
                    ),
                )
    for item in tool_results:
        if not isinstance(item, dict) or item.get("status") != "insufficient":
            continue
        tool_name = str(item.get("tool_name") or "")
        facts = item.get("facts") if isinstance(item.get("facts"), dict) else {}
        if (
            tool_name == "ad_region_detector_tool"
            and not facts.get("ad_regions")
            and facts.get("absence_confirmed") is True
            and (
                capability_config.get("requires_journey") is not True or not has_journey_observation
            )
        ):
            compact_tool_results = _compact_tool_results(tool_results)
            return _build_skipped_result(
                definition=definition,
                request=request,
                observation_packet_id=packet.observation_packet_id,
                evidence_artifact_ids=packet.evidence_artifact_ids,
                reason="当前页面未检测到广告区域，广告规则不适用于本页",
                packet=packet,
                tool_results=compact_tool_results,
            )
    return None


def _has_startup_result_images(packet: ObservationPacket, request: RuleJudgeRequest) -> bool:
    """正式角色双图齐备时交语义判断，未知结果页型不能被旅程文字提前判缺证。"""
    payload = packet.packet_payload
    page = payload.get("page")
    if not isinstance(page, dict) or page.get("surface_kind") not in {"android", "ios"}:
        return False
    if packet.quality_gate_status != "pass":
        return False
    facts = payload.get("facts")
    roles = facts.get("capture_roles") if isinstance(facts, dict) else None
    identity = payload.get("identity")
    bindings = identity.get("capture_roles") if isinstance(identity, dict) else None
    requested = request.evidence_capture_roles or {}
    if not isinstance(roles, dict) or not isinstance(bindings, dict):
        return False
    before, primary = roles.get("before"), roles.get("primary")
    if not isinstance(before, dict) or not isinstance(primary, dict):
        return False
    if before.get("page_type") != "startup_ad":
        return False
    for role, fact in (("before", before), ("primary", primary)):
        if (
            not fact.get("page_capture_id")
            or fact.get("page_capture_id") != requested.get(role)
            or fact.get("page_capture_id") != bindings.get(role)
            or fact.get("quality_status") not in {None, "pass"}
        ):
            return False
    if primary["page_capture_id"] != packet.page_capture_id:
        return False
    if before["page_capture_id"] == primary["page_capture_id"]:
        return False
    png_ids = {
        artifact_id for artifact_id, image, content_type in packet.image_payloads
        if content_type == "image/png" and image.startswith(b"\x89PNG\r\n\x1a\n")
    }
    before_images = png_ids.intersection(before.get("artifact_ids") or [])
    primary_images = png_ids.intersection(primary.get("artifact_ids") or [])
    return bool(before_images and primary_images and before_images.isdisjoint(primary_images))


async def _startup_ad_skip_applicability_preflight(
    *,
    definition: RuleJudgeDefinition,
    request: RuleJudgeRequest,
    packet: ObservationPacket,
    capability_config: dict[str, Any],
) -> RuleJudgeResult | None:
    """启动广告不存在时可不适用；广告仍在旅程末端时保留真实缺证。

    没有旅程记录（人工截图测试）时不拦，交模型按截图先后判。
    """
    if capability_config.get("batch_group") != "startup-ad-skip":
        return None
    if _has_startup_result_images(packet, request):
        return None
    facts = packet.packet_payload.get("facts")
    journey = facts.get("journey") if isinstance(facts, dict) else None
    if not isinstance(journey, list) or not journey:
        return None
    transition = await evaluate_rule_engine_handler(
        handler="startup_ad_home_transition_handler",
        packet_payload=packet.packet_payload,
        engine_config={},
    )
    if transition.conclusion == "evidence_insufficient":
        return _build_evidence_insufficient_result(
            definition=definition,
            request=request,
            packet=packet,
            reason=transition.problem_desc or transition.summary,
            missing_capture_roles=["after"],
        )
    if transition.conclusion != "skipped":
        return None
    return _build_skipped_result(
        definition=definition,
        request=request,
        observation_packet_id=packet.observation_packet_id,
        evidence_artifact_ids=packet.evidence_artifact_ids,
        reason=transition.problem_desc or transition.summary,
        packet=packet,
        tool_results=[transition.to_dict()],
    )


def _judge_technical_error(
    definition: RuleJudgeDefinition,
    request: RuleJudgeRequest,
    engine_config: dict[str, Any],
) -> RuleJudgeResult:
    input_field = str(engine_config.get("input_field") or "visible_text")
    visible_text = _required_string(request.judge_input, input_field)
    pattern = _required_config_string(engine_config, "pattern")
    assertion = _evaluate_deterministic_assertion(
        assertion_type="regex_not_match",
        payload={"text": visible_text, "pattern": pattern},
    )
    passed = bool(assertion["passed"])
    if not passed:
        assertion["match_details"] = _regex_match_details(
            visible_text,
            pattern=pattern,
        )
    return _build_result(
        definition=definition,
        request=request,
        passed=passed,
        pass_summary="未发现技术错误直出",
        fail_summary="发现技术错误直出",
        problem_desc=None if passed else "页面可见文本命中技术错误特征",
        judge_output={"assertion": assertion},
    )


def _judge_discount_price(
    definition: RuleJudgeDefinition,
    request: RuleJudgeRequest,
    engine_config: dict[str, Any],
) -> RuleJudgeResult:
    discount_field = str(engine_config.get("discount_price_field") or "discount_price")
    original_field = str(engine_config.get("original_price_field") or "original_price")
    operator = str(engine_config.get("operator") or "lt")
    assertion = _evaluate_deterministic_assertion(
        assertion_type="number_compare",
        payload={
            "left": _required_number(request.judge_input, discount_field),
            "right": _required_number(request.judge_input, original_field),
            "operator": operator,
        },
    )
    passed = bool(assertion["passed"])
    relation_text = {
        "lt": "低于",
        "lte": "不高于",
        "eq": "等于",
        "gte": "不低于",
        "gt": "高于",
    }[operator]
    return _build_result(
        definition=definition,
        request=request,
        passed=passed,
        pass_summary=f"优惠价{relation_text}原价",
        fail_summary=f"优惠价未满足{relation_text}原价",
        problem_desc=None if passed else f"页面展示的优惠价应{relation_text}原价",
        judge_output={"assertion": assertion},
    )


def _build_result(
    *,
    definition: RuleJudgeDefinition,
    request: RuleJudgeRequest,
    passed: bool,
    pass_summary: str,
    fail_summary: str,
    problem_desc: str | None,
    judge_output: dict[str, Any],
) -> RuleJudgeResult:
    return RuleJudgeResult(
        rule_id=definition.rule_id,
        rule_code=definition.rule_code,
        executor_type=definition.executor_type,
        judge_strategy=RULE_ENGINE_EXECUTOR,
        execution_status="pass" if passed else "fail",
        confidence=1.0,
        result_summary=pass_summary if passed else fail_summary,
        problem_desc=problem_desc,
        page_capture_id=request.page_capture_id,
        observation_packet_id=None,
        evidence_artifact_ids=request.evidence_artifact_ids,
        judge_input=_summarize_judge_input(request.judge_input),
        judge_output=judge_output,
    )


def _build_capability_result(
    *,
    definition: RuleJudgeDefinition,
    request: RuleJudgeRequest,
    packet: ObservationPacket,
    capability_result: CapabilityResult,
) -> RuleJudgeResult:
    result = RuleJudgeResult(
        rule_id=definition.rule_id,
        rule_code=definition.rule_code,
        executor_type=definition.executor_type,
        judge_strategy=RULE_ENGINE_EXECUTOR,
        execution_status=capability_result.conclusion,
        confidence=capability_result.confidence,
        result_summary=capability_result.summary,
        problem_desc=capability_result.problem_desc,
        page_capture_id=request.page_capture_id,
        observation_packet_id=packet.observation_packet_id,
        evidence_artifact_ids=packet.evidence_artifact_ids,
        judge_input=_observation_judge_input(packet, []),
        judge_output={
            "handler": capability_result.tool_name,
            "tool_result": capability_result.to_dict(),
        },
    )
    return _attach_evidence_gap(result, definition=definition, request=request, packet=packet)


_EVIDENCE_REQUEST_TTL = timedelta(hours=12)


def _attach_evidence_gap(
    result: RuleJudgeResult,
    *,
    definition: RuleJudgeDefinition,
    request: RuleJudgeRequest,
    packet: ObservationPacket | None,
    missing_capture_roles: list[str] | None = None,
    auto_recapture_block_reason: str | None = None,
) -> RuleJudgeResult:
    """给「证据不足」结果挂上结构化缺口：只声明还缺什么，不指挥任何操作。"""
    if result.execution_status != "evidence_insufficient":
        return result
    output = dict(result.judge_output)
    payload = packet.packet_payload if packet is not None else {}
    page = payload.get("page")
    page = dict(page) if isinstance(page, dict) else {}
    gap_capture_id = request.page_capture_id
    # 结果仍归属原工作项；新缺口的状态与目标必须来自同一个补采 Capture。
    if (
        packet is not None
        and _packet_surface_is_h5(packet)
        and request.evidence_manifest_id
        and request.evidence_source_page_capture_id
        and page.get("page_capture_id") == request.evidence_source_page_capture_id
    ):
        gap_capture_id = request.evidence_source_page_capture_id
        output["evidence_gap_source"] = {
            "target_platform": "h5",
            "surface_kind": "web",
            "page_capture_id": gap_capture_id,
            "manifest_id": request.evidence_manifest_id,
        }
    page.setdefault("page_capture_id", request.page_capture_id)
    quality = payload.get("quality_gate")
    quality = quality if isinstance(quality, dict) else {}
    presentation = output.get("presentation_rejection")
    presentation = presentation if isinstance(presentation, dict) else {}
    statements = output.get("missing_evidence")
    if not isinstance(statements, list):
        # 语义判定把模型原话放在 model_output 里；缺口保留这些说明但不当作操作指令。
        model_output = output.get("model_output")
        model_output = model_output if isinstance(model_output, dict) else {}
        statements = model_output.get("missing_evidence")
    statements = [str(item) for item in statements] if isinstance(statements, list) else []
    quality_sources = quality.get("missing_sources")
    quality_sources = (
        [str(item) for item in quality_sources] if isinstance(quality_sources, list) else []
    )
    layer_target = rule_evidence_layer_target(
        category=definition.category,
        judge_config=definition.judge_config,
        check_point=definition.check_point,
        rule_name=definition.rule_name,
    )
    engine_config = definition.judge_config.get("engine_config") or {}
    if (
        packet is not None and _packet_surface_is_h5(packet)
        and isinstance(engine_config, dict)
        and engine_config.get("handler") == "price_relation"
        and any(role.startswith("location_") for role in missing_capture_roles or [])
    ):
        # 缺价格位置不是缺一个时间帧；运行面没有可验证位置目标时不自动补采。
        auto_recapture_block_reason = "H5_PRICE_LOCATION_TARGET_UNAVAILABLE"
    required_interaction = _h5_gap_interaction(definition, packet)
    if required_interaction is not None and required_interaction.parameters.get(
        "goal_capability"
    ) in {
        "cover_content_pair",
        "media_fullscreen_pair",
        "payment_entry_pair",
        "payment_plan_selection",
        "native_video_fullscreen", "scroll_button_feedback", "scroll_back_to_top", "payment_plan_reveal",
    }:
        missing_capture_roles = list(
            dict.fromkeys([*(missing_capture_roles or []), "before", "primary"])
        )
    try:
        gap = build_evidence_gap(
            task_id=request.task_id,
            rule_id=definition.rule_id,
            rule_code=definition.rule_code,
            rule_name=definition.rule_name,
            category=definition.category,
            check_point=definition.check_point,
            input_contract=definition.input_contract,
            agent_run_id=request.agent_run_id,
            observation_packet_id=result.observation_packet_id,
            page_capture_id=gap_capture_id,
            page=page,
            layer_target=layer_target.value,
            required_page_states=sorted(_required_page_states(definition)),
            missing_capture_roles=list(missing_capture_roles or []),
            presentation_code=presentation.get("reason_code"),
            presentation_reason=presentation.get("reason"),
            judge_statements=statements,
            quality_gate_sources=quality_sources,
            reason_code=str(output.get("reason_code") or "EVIDENCE_INSUFFICIENT"),
            reason=result.problem_desc or "",
            request_expires_at=datetime.now(tz=UTC) + _EVIDENCE_REQUEST_TTL,
            required_interaction=required_interaction,
            auto_recapture_block_reason=auto_recapture_block_reason,
        )
    except ValueError:
        logger.exception("rule_code=%s 生成证据缺口失败，保留原判定结果。", definition.rule_code)
        return result
    output["evidence_gap"] = gap.to_dict()
    return replace(result, judge_output=output)


def _h5_gap_interaction(
    definition: RuleJudgeDefinition, packet: ObservationPacket | None,
) -> EvidenceInteractionRequirement | None:
    """由冻结能力分组生成语义动作要求，不从模型文案推断授权。"""
    if packet is None or not _packet_surface_is_h5(packet):
        return None
    capability = definition.judge_config.get("capability_config") or {}
    if not isinstance(capability, dict):
        return None
    goals = {
        "visual-nav": (
            InteractionOperation.CLICK,
            "当前页面顶部目录切换控件",
            "page_tab_navigation",
        ),
        "list-scroll": (InteractionOperation.SCROLL, "当前页面纵向滚动区域", "vertical_scroll"),
        "cover-content": (InteractionOperation.CLICK, "当前列表首个内容卡片", "cover_content_pair"),
        "payment-entry": (InteractionOperation.CLICK, "个人中心会员支付入口", "payment_entry_pair"),
    }
    if definition.rule_code == "alioth_46" and capability.get("batch_group") == "interaction-state":
        goals["interaction-state"] = (
            InteractionOperation.CLICK,
            "放大当前帖子媒体",
            "media_fullscreen_pair",
        )
    goal = goals.get(capability.get("batch_group"))
    if goal is None:
        return None
    operation, target, kind = goal
    parameters = {"goal_capability": kind}
    page_type = str((packet.packet_payload.get("page") or {}).get("page_type") or "")
    from services.api.app.application.h5_probe_journey import (
        current_probe_snapshot,
        scroll_feedback_ready,
        verified_display_subjects,
    )
    from services.api.app.application.h5_probe_targets import PROBE_TARGET_LABELS

    snapshot = current_probe_snapshot(packet.packet_payload)
    if kind == "vertical_scroll" and definition.rule_code in {
        "alioth_spec_new_4", "alioth_spec_new_20",
    } and scroll_feedback_ready(
        packet.packet_payload, require_bottom=definition.rule_code == "alioth_spec_new_20",
    ):
        kind = ("scroll_back_to_top" if definition.rule_code == "alioth_spec_new_20"
                else "scroll_button_feedback")
        operation, target = InteractionOperation.CLICK, PROBE_TARGET_LABELS[kind]
        parameters["goal_capability"] = kind
    if kind == "payment_entry_pair" and page_type == "payment":
        target, kind = "选择当前会员档位", "payment_plan_selection"
        parameters["goal_capability"] = kind
    if kind == "cover_content_pair":
        from services.api.app.application.h5_probe_targets import (
            probe_subject_key,
            select_cover_subject,
        )

        subject = (
            select_cover_subject(snapshot, page_type=page_type)
            if isinstance(snapshot, dict)
            else None
        )
        if subject is not None:
            parameters["probe_subject_key"] = probe_subject_key(subject)
    elif kind in PROBE_TARGET_LABELS:
        from services.api.app.application.h5_probe_targets import (
            payment_plan_reveal_needed,
            probe_subject_key,
            select_probe_target,
        )

        subject = (select_probe_target(snapshot, goal=kind, page_type=page_type,
                    excluded_keys=verified_display_subjects(packet.packet_payload, goal=kind,
                                                            snapshot=snapshot))
                   if isinstance(snapshot, dict) else None)
        if (subject is None and kind == "media_fullscreen_pair"
            and select_probe_target(snapshot, goal=kind, page_type=page_type) is None):
            subject = select_probe_target(snapshot, goal="native_video_fullscreen", page_type=page_type)
            if subject is not None:
                kind, target = "native_video_fullscreen", PROBE_TARGET_LABELS["native_video_fullscreen"]
                parameters["goal_capability"] = kind
        if subject is not None:
            parameters["probe_subject_key"] = probe_subject_key(subject)
        elif kind == "payment_plan_selection" and payment_plan_reveal_needed(
            snapshot, excluded_keys=verified_display_subjects(packet.packet_payload, goal=kind,
                                                              snapshot=snapshot),
        ):
            operation, target, kind = (InteractionOperation.SCROLL, "浏览全部会员卡", "payment_plan_reveal")
            parameters["goal_capability"] = kind
    return EvidenceInteractionRequirement(
        operation=operation,
        semantic_target=target,
        purpose=f"补充{target}的前后证据",
        parameters=parameters,
    )


def _build_evidence_insufficient_result(
    *,
    definition: RuleJudgeDefinition,
    request: RuleJudgeRequest,
    packet: ObservationPacket,
    reason: str,
    missing_capture_roles: list[str] | None = None,
) -> RuleJudgeResult:
    tool_results = (
        [
            {
                "tool_name": "observation_packet_quality_gate",
                "status": "insufficient",
                "reason": reason,
            }
        ]
        if definition.executor_type == "hybrid"
        else []
    )
    result = RuleJudgeResult(
        rule_id=definition.rule_id,
        rule_code=definition.rule_code,
        executor_type=definition.executor_type,
        judge_strategy=definition.executor_type,
        execution_status="evidence_insufficient",
        confidence=0.0,
        result_summary="规则证据不足，未执行结论推断",
        problem_desc=reason,
        page_capture_id=request.page_capture_id,
        observation_packet_id=packet.observation_packet_id,
        evidence_artifact_ids=packet.evidence_artifact_ids,
        judge_input=_observation_judge_input(packet, tool_results),
        judge_output={
            "schema_version": "1.0",
            "conclusion": "evidence_insufficient",
            "reasoning_summary": reason,
            "missing_evidence": _missing_evidence(packet),
            "tool_results": tool_results,
        },
    )
    return _attach_evidence_gap(
        result,
        definition=definition,
        request=request,
        packet=packet,
        missing_capture_roles=missing_capture_roles,
    )


def _observation_judge_input(
    packet: ObservationPacket,
    tool_results: list[dict[str, Any]],
) -> dict[str, Any]:
    facts = packet.packet_payload.get("facts")
    facts = facts if isinstance(facts, dict) else {}
    ui_snapshot = facts.get("ui_snapshot")
    ui_snapshot = ui_snapshot if isinstance(ui_snapshot, dict) else {}
    network = facts.get("network_summary")
    network = network if isinstance(network, dict) else {}
    console = facts.get("console_summary")
    console = console if isinstance(console, dict) else {}
    return {
        "schema_version": "rule-judge-input.v1",
        "observation_packet_id": packet.observation_packet_id,
        "quality_gate": packet.packet_payload.get("quality_gate"),
        "page": packet.packet_payload.get("page"),
        "fact_summary": {
            "visible_text_excerpt": str(facts.get("visible_text") or "")[:2_000],
            "visible_text_character_count": facts.get("visible_text_character_count"),
            "ui_element_count": ui_snapshot.get("element_count"),
            "screenshot_count": facts.get("screenshot_count"),
            "network_error_count": len(network.get("failed_responses") or []),
            "console_error_count": len(console.get("errors") or []),
            "action_count": len(facts.get("action_log") or []),
        },
        "tool_results": tool_results,
    }


def _missing_evidence(packet: ObservationPacket) -> list[str]:
    quality = packet.packet_payload.get("quality_gate")
    quality = quality if isinstance(quality, dict) else {}
    values = quality.get("missing_sources")
    return [str(item) for item in values] if isinstance(values, list) else []


def _compact_tool_results(
    tool_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for item in tool_results[:20]:
        if not isinstance(item, dict):
            continue
        facts = item.get("facts")
        if isinstance(facts, dict):
            facts = {key: value for index, (key, value) in enumerate(facts.items()) if index < 20}
        candidate = {
            key: value
            for key, value in {
                "tool_name": item.get("tool_name"),
                "status": item.get("status"),
                "reason": item.get("reason"),
                "facts": facts,
            }.items()
            if value not in (None, {}, [])
        }
        candidate_size = _json_size(candidate)
        if candidate_size > MAX_COMPACT_TOOL_RESULT_BYTES:
            candidate["facts"] = _summarize_large_tool_facts(facts)
            candidate["facts_truncated"] = True
            candidate["original_size_bytes"] = candidate_size
        proposed = [*compact, candidate]
        if _json_size(proposed) > MAX_COMPACT_TOOL_RESULTS_BYTES:
            compact.append(
                {
                    "tool_name": "capability_pipeline",
                    "status": "truncated",
                    "reason": "确定性 Tool 结果超过审计写回上限",
                    "remaining_result_count": len(tool_results) - len(compact),
                }
            )
            break
        compact.append(candidate)
    return compact


def _summarize_large_tool_facts(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"value_type": type(value).__name__}
    summary: dict[str, Any] = {"fact_keys": sorted(str(key) for key in value)[:50]}
    item_counts = {
        str(key): len(item)
        for key, item in value.items()
        if isinstance(item, (dict, list, tuple, set))
    }
    scalar_preview = {
        str(key): item
        for key, item in value.items()
        if isinstance(item, (str, int, float, bool)) or item is None
    }
    if item_counts:
        summary["item_counts"] = item_counts
    if scalar_preview:
        summary["scalar_preview"] = {
            key: (item[:300] if isinstance(item, str) else item)
            for key, item in list(scalar_preview.items())[:20]
        }
    return summary


def _json_size(value: Any) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            default=str,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _semantic_problem_desc(
    *,
    execution_status: str,
    model_payload: dict[str, Any],
    review_reason: str | None,
) -> str | None:
    if execution_status == "pass":
        return None
    if review_reason:
        return review_reason
    findings = model_payload.get("findings")
    summaries = (
        [
            str(item.get("summary"))
            for item in findings
            if isinstance(item, dict) and item.get("summary")
        ]
        if isinstance(findings, list)
        else []
    )
    return "；".join(summaries[:10]) or str(model_payload.get("reasoning_summary") or "")[:2_000]


def _min_confidence(policy: dict[str, Any]) -> float:
    try:
        value = float(policy.get("min_confidence", 0.75))
    except (TypeError, ValueError):
        return 0.75
    return min(1.0, max(0.0, value))


def _validate_request(
    request: RuleJudgeRequest,
    *,
    require_evidence_artifact_ids: bool,
) -> None:
    if not request.task_id:
        raise RuleJudgeValidationError("规则判定必须提供任务 ID")
    if not request.rule_code:
        raise RuleJudgeValidationError("规则判定必须提供规则代码")
    if not request.page_capture_id:
        raise RuleJudgeValidationError("规则判定必须提供页面采集 ID")
    if require_evidence_artifact_ids and (
        not request.evidence_artifact_ids
        or not all(isinstance(item, str) and item.strip() for item in request.evidence_artifact_ids)
    ):
        raise RuleJudgeValidationError("规则判定必须提供有效证据附件 ID")
    if request.evidence_artifact_ids and not all(
        item.strip() for item in request.evidence_artifact_ids
    ):
        raise RuleJudgeValidationError("规则判定证据附件 ID 不得包含空值")
    if not isinstance(request.judge_input, dict):
        raise RuleJudgeValidationError("规则判定输入必须为对象")
    if (request.evidence_source_page_capture_id is None) != (request.evidence_manifest_id is None):
        raise RuleJudgeValidationError("补采证据 Capture 与 Manifest 必须同时提供")
    if request.evidence_capture_roles is not None and not isinstance(
        request.evidence_capture_roles, dict
    ):
        raise RuleJudgeValidationError("证据采集角色必须为对象")
    if request.evidence_manifest_refs is not None and not isinstance(
        request.evidence_manifest_refs, dict
    ):
        raise RuleJudgeValidationError("证据清单角色绑定必须为对象")
    if request.evidence_manifest_refs is not None:
        for role, reference in request.evidence_manifest_refs.items():
            if not isinstance(role, str) or not role.strip() or not isinstance(reference, dict):
                raise RuleJudgeValidationError("证据清单角色绑定格式无效")
            manifest_id = reference.get("manifest_id")
            manifest_version = reference.get("manifest_version")
            if not isinstance(manifest_id, str) or not manifest_id.strip():
                raise RuleJudgeValidationError("证据清单角色绑定缺少 manifest_id")
            if not isinstance(manifest_version, str) or not manifest_version.strip():
                raise RuleJudgeValidationError("证据清单角色绑定缺少 manifest_version")
        declared_roles = set(request.evidence_capture_roles or {"primary": ""})
        if not set(request.evidence_manifest_refs).issubset(declared_roles):
            raise RuleJudgeValidationError("证据清单角色绑定必须对应声明的证据角色")
    if request.retained_baseline_artifact_ids is not None and not isinstance(
        request.retained_baseline_artifact_ids, dict
    ):
        raise RuleJudgeValidationError("保留基线证据角色映射必须为对象")
    if request.retained_baseline_artifact_ids is not None:
        for role, artifact_ids in request.retained_baseline_artifact_ids.items():
            if (
                not isinstance(role, str)
                or not role.strip()
                or not isinstance(artifact_ids, (tuple, list))
            ):
                raise RuleJudgeValidationError("保留基线证据角色映射格式无效")
            if len(set(artifact_ids)) != len(artifact_ids) or any(
                not isinstance(artifact_id, str) or not artifact_id.strip()
                for artifact_id in artifact_ids
            ):
                raise RuleJudgeValidationError("保留基线证据角色映射包含无效附件 ID")
        declared_roles = set(request.evidence_capture_roles or {"primary": ""})
        if not set(request.retained_baseline_artifact_ids).issubset(declared_roles):
            raise RuleJudgeValidationError("保留基线证据必须对应声明的证据角色")
    if request.expected_contract_hash is not None and (
        not isinstance(request.expected_contract_hash, str)
        or not request.expected_contract_hash.strip()
        or request.expected_contract_hash != request.expected_contract_hash.strip()
    ):
        raise RuleJudgeValidationError("锁定规则契约哈希必须为无首尾空白的非空字符串")


def _validate_locked_rule_contract(
    definition: RuleJudgeDefinition,
    request: RuleJudgeRequest,
) -> None:
    """在读取证据和执行 Judge 前拒绝消费已漂移的规则定义。"""
    from services.api.app.application.web_rule_evidence_contract import (
        validate_web_sequence_execution,
    )

    if request.original_page_input_set_id is not None and (
        request.evidence_input_set_id is not None or request.expected_contract_hash is None
    ):
        raise RuleJudgeValidationError("H5 页面原输入必须绑定冻结合同且不得混用 Web 序列")
    try:
        contract = validate_web_sequence_execution(
            definition.input_contract,
            executor_type=definition.executor_type,
            judge_config=definition.judge_config,
        )
    except ValueError as exc:
        raise RuleJudgeValidationError(str(exc)) from exc
    if request.evidence_input_set_id is not None and (
        contract is None or request.expected_contract_hash is None
    ):
        raise RuleJudgeValidationError("Web 序列请求必须绑定显式规则合同及冻结哈希")
    if (
        request.evidence_input_set_id is None
        and contract is not None
        and "stable_page" not in contract.accepted_roles
    ):
        raise RuleJudgeValidationError("Web 普通材料必须由规则明确接受 stable_page")
    if request.expected_contract_hash is None:
        return
    current_hash = build_inspection_rule_contract_hash(
        {
            "rule_id": definition.rule_id,
            "rule_code": definition.rule_code,
            "category": definition.category,
            "executor_type": definition.executor_type,
            "severity": definition.severity,
            "check_point": definition.check_point,
            "input_contract": definition.input_contract,
            "judge_config": definition.judge_config,
            "manual_review_policy": definition.manual_review_policy,
            "execution_route": rule_execution_route_payload(
                executor_type=definition.executor_type,
                judge_config=definition.judge_config,
                input_contract=definition.input_contract,
            ),
            "steps": definition.steps,
        }
    )
    if current_hash != request.expected_contract_hash:
        raise RuleJudgeContractDriftError(
            "工作项锁定规则契约已漂移："
            f"rule_code={definition.rule_code}, "
            f"expected={request.expected_contract_hash}, current={current_hash}"
        )


def _required_string(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise RuleJudgeValidationError(f"规则判定输入缺少有效字段：{field}")
    return value.strip()


def _required_number(payload: dict[str, Any], field: str) -> float:
    value = payload.get(field)
    if isinstance(value, bool):
        raise RuleJudgeValidationError(f"规则判定输入缺少有效数值字段：{field}")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RuleJudgeValidationError(f"规则判定输入缺少有效数值字段：{field}") from exc
    if not isfinite(number):
        raise RuleJudgeValidationError(f"规则判定输入数值必须为有限值：{field}")
    return number


def _required_config_string(config: dict[str, Any], field: str) -> str:
    value = config.get(field)
    if not isinstance(value, str) or not value:
        raise RuleJudgeNotConfiguredError(f"规则 engine_config 缺少字段：{field}")
    return value


def _required_input_fields(
    handler: str,
    engine_config: dict[str, Any],
) -> tuple[str, ...]:
    if handler == "technical_error_text":
        return (str(engine_config.get("input_field") or "visible_text"),)
    if handler == "discount_price_compare":
        return (
            str(engine_config.get("discount_price_field") or "discount_price"),
            str(engine_config.get("original_price_field") or "original_price"),
        )
    if handler == "collection_constraint":
        return (str(engine_config.get("input_field") or "items"),)
    if handler == "required_ui_capability":
        return (str(engine_config.get("input_field") or "observed_capabilities"),)
    if handler == "geometry_hit_area_assertion":
        return (str(engine_config.get("element_index_field") or "element_index"),)
    if handler == "qr_url_validation":
        return ()
    return (str(engine_config.get("input_field") or "url"),)


def _blocked_route(
    strategy: str,
    *,
    code: str,
    message: str,
    handler: str | None = None,
    handler_tool_name: str | None = None,
) -> RuleExecutionRoute:
    return RuleExecutionRoute(
        strategy=strategy,
        ready=False,
        rule_engine_ready=False,
        tool_name=None,
        handler=handler,
        handler_tool_name=handler_tool_name,
        judge_input_source="unavailable",
        required_input_fields=(),
        required_artifact_types=(),
        blocked_reason={"code": code, "message": message},
    )


def _summarize_judge_input(judge_input: dict[str, Any]) -> dict[str, Any]:
    """判定审计载荷投影。

    E26：`judge_input` 由调用方自由构造并原样落 `qc_rule_execution.judge_input`，
    因此必须先脱敏——凭证类键打码、大体积原文键丢弃、超长字符串截断，
    避免登录凭证、Cookie、令牌、截图 base64 与完整 DOM 进入审计。
    """
    summarized = dict(judge_input)
    visible_text = summarized.get("visible_text")
    if isinstance(visible_text, str) and len(visible_text) > 500:
        summarized["visible_text"] = visible_text[:500]
        summarized["visible_text_truncated"] = True
        summarized["visible_text_length"] = len(visible_text)
    return redact_audit_payload(summarized)


def _evaluate_deterministic_assertion(
    *, assertion_type: str, payload: dict[str, Any]
) -> dict[str, Any]:
    from services.api.app.agents.tools.assertion_tools import (
        evaluate_deterministic_assertion,
    )

    return evaluate_deterministic_assertion(
        assertion_type=assertion_type,
        payload=payload,
    )


async def _validate_qr_link(
    *,
    url: str | None = None,
    image_base64: str | None = None,
    image_path: str | None = None,
    timeout_seconds: float,
    max_redirects: int,
) -> dict[str, Any]:
    from services.api.app.agents.tools.qr_link_validation_tool import validate_qr_link

    return await validate_qr_link(
        url=url,
        image_base64=image_base64,
        image_path=image_path,
        timeout_seconds=timeout_seconds,
        max_redirects=max_redirects,
    )


async def _execute_technical_error_text(
    *,
    runtime: RuleEngineRuntime,
    definition: RuleJudgeDefinition,
    request: RuleJudgeRequest,
    engine_config: dict[str, Any],
) -> RuleJudgeResult:
    """从可信页面文本执行技术错误扫描。"""
    input_field = str(engine_config.get("input_field") or "visible_text")
    visible_text = runtime.evidence_bundle.require_unique("visible_text").decode_text()
    if len(visible_text) > MAX_TRUSTED_TEXT_LENGTH:
        raise RuleJudgeValidationError(
            f"visible_text 超过确定性扫描上限：{MAX_TRUSTED_TEXT_LENGTH} 字符"
        )
    trusted_request = replace(
        request,
        judge_input={**request.judge_input, input_field: visible_text},
    )
    return _judge_technical_error(definition, trusted_request, engine_config)


async def _execute_discount_price_compare(
    *,
    runtime: RuleEngineRuntime,
    definition: RuleJudgeDefinition,
    request: RuleJudgeRequest,
    engine_config: dict[str, Any],
) -> RuleJudgeResult:
    """执行优惠价与原价数值关系判定。"""
    discount_field = str(engine_config.get("discount_price_field") or "discount_price")
    original_field = str(engine_config.get("original_price_field") or "original_price")
    missing_fields: list[str] = []
    for field_name in (discount_field, original_field):
        value = request.judge_input.get(field_name)
        if (
            field_name not in request.judge_input
            or value is None
            or (isinstance(value, str) and not value.strip())
        ):
            missing_fields.append(field_name)
    if missing_fields:
        visible_text = "\n".join(
            item.decode_text() for item in runtime.evidence_bundle.artifacts_of_type("visible_text")
        )
        discount_marker_pattern = str(
            engine_config.get("discount_marker_pattern")
            or r"(?i)(?:优惠价|现价|折后价|活动价|discount|sale\s*price)"
        )
        if bool(engine_config.get("pass_when_discount_absent", False)) and not re.search(
            discount_marker_pattern,
            visible_text,
        ):
            return _build_result(
                definition=definition,
                request=request,
                passed=True,
                pass_summary="当前页面未展示优惠价，无需执行价格关系比较",
                fail_summary="",
                problem_desc=None,
                judge_output={
                    "schema_version": "1.0",
                    "conclusion": "pass",
                    "applicability": "condition_not_triggered",
                    "discount_marker_pattern": discount_marker_pattern,
                    "discount_marker_matched": False,
                },
            )
        reason = "规则判定输入缺少有效数值字段：" + ", ".join(missing_fields)
        return _build_runtime_evidence_insufficient_result(
            definition=definition,
            request=request,
            observation_packet_id=runtime.evidence_bundle.observation_packet_id,
            evidence_artifact_ids=_trusted_bundle_artifact_ids(
                runtime.evidence_bundle,
                fallback=request.evidence_artifact_ids,
            ),
            reason=reason,
        )
    return _judge_discount_price(definition, request, engine_config)


async def _execute_visible_text_presence(
    *,
    runtime: RuleEngineRuntime,
    definition: RuleJudgeDefinition,
    request: RuleJudgeRequest,
    engine_config: dict[str, Any],
) -> RuleJudgeResult:
    """按单次可信可见文本是否命中配置特征形成结果。"""
    capture_role = str(engine_config.get("capture_role") or "primary")
    include_ui_snapshot_text = engine_config.get("include_ui_snapshot_text") is True
    artifact_types = (
        ("visible_text", "ui_snapshot") if include_ui_snapshot_text else ("visible_text",)
    )
    evidence_by_role = _decode_role_evidence(
        runtime.evidence_bundle,
        artifact_types=artifact_types,
        capture_roles=frozenset({capture_role}),
    )
    role_evidence = evidence_by_role.get(capture_role)
    if not isinstance(role_evidence, dict):
        return _build_runtime_evidence_insufficient_result(
            definition=definition,
            request=request,
            observation_packet_id=runtime.evidence_bundle.observation_packet_id,
            evidence_artifact_ids=_trusted_bundle_artifact_ids(
                runtime.evidence_bundle,
                fallback=request.evidence_artifact_ids,
            ),
            reason=f"缺少采集角色可见文本：{capture_role}",
        )
    try:
        visible_text = str(role_evidence.get("visible_text") or "")
        if include_ui_snapshot_text:
            snapshot_text = _visible_text_from_ui_snapshot(role_evidence.get("ui_snapshot"))
            visible_text = "\n".join(item for item in (visible_text.strip(), snapshot_text) if item)
        evaluation = evaluate_visible_text_presence(
            visible_text=visible_text,
            config=engine_config,
        )
    except HandlerInputError as exc:
        return _build_runtime_evidence_insufficient_result(
            definition=definition,
            request=request,
            observation_packet_id=runtime.evidence_bundle.observation_packet_id,
            evidence_artifact_ids=_trusted_bundle_artifact_ids(
                runtime.evidence_bundle,
                fallback=request.evidence_artifact_ids,
            ),
            reason=str(exc),
        )
    if (
        not evaluation.passed
        and engine_config.get("absence_conclusion", "evidence_insufficient")
        == "evidence_insufficient"
    ):
        return _build_runtime_evidence_insufficient_result(
            definition=definition,
            request=request,
            observation_packet_id=runtime.evidence_bundle.observation_packet_id,
            evidence_artifact_ids=_trusted_bundle_artifact_ids(
                runtime.evidence_bundle,
                fallback=request.evidence_artifact_ids,
            ),
            reason=evaluation.problem_desc or "可信可见文本未命中配置特征",
        )
    return _result_from_evaluation(
        definition=definition,
        request=request,
        evaluation=evaluation,
    )


def _visible_text_from_ui_snapshot(value: Any) -> str:
    """从可信 UI 快照提取元素视觉文本，禁止把 URL、哈希和隐藏属性当成数字指标。"""
    if not isinstance(value, dict):
        return ""
    frames = value.get("frames")
    snapshots = (
        [item for item in frames if isinstance(item, dict)] if isinstance(frames, list) else [value]
    )
    texts: list[str] = []
    for snapshot in snapshots[-3:]:
        elements = snapshot.get("elements")
        if not isinstance(elements, list):
            continue
        for element in elements[:500]:
            if not isinstance(element, dict):
                continue
            for text_field in ("text", "aria_label", "name", "alt"):
                text = element.get(text_field)
                if not isinstance(text, str):
                    continue
                normalized = text.strip()
                if normalized and normalized not in texts:
                    texts.append(normalized[:500])
    return "\n".join(texts)[:MAX_TRUSTED_TEXT_LENGTH]


async def _execute_screenshot_change_assertion(
    *,
    runtime: RuleEngineRuntime,
    definition: RuleJudgeDefinition,
    request: RuleJudgeRequest,
    engine_config: dict[str, Any],
) -> RuleJudgeResult:
    """按操作前后可信截图摘要是否变化形成结果。"""
    screenshot_by_role: dict[str, bytes] = {}
    for artifact in runtime.evidence_bundle.artifacts_of_type("screenshot"):
        # screenshot 类型同时承载规范 PNG 与时序帧序列 JSON；变化断言只比较
        # 每个角色的规范截图，序列由专门的时序证据链消费。
        content_type = (
            str(getattr(artifact, "content_type", "")).split(";", 1)[0].strip().casefold()
        )
        if content_type != "image/png":
            continue
        role = getattr(artifact, "capture_role", None)
        if not isinstance(role, str) or not role:
            raise RuleJudgeValidationError("screenshot 证据缺少有效 capture_role")
        if role in screenshot_by_role:
            raise RuleJudgeValidationError(f"采集角色 {role} 存在重复 screenshot 证据")
        payload = getattr(artifact, "payload", None)
        if not isinstance(payload, bytes) or not payload:
            raise RuleJudgeValidationError(f"{role}.screenshot 必须为非空二进制")
        screenshot_by_role[role] = payload
    try:
        evaluation = evaluate_screenshot_change(
            screenshot_by_role=screenshot_by_role,
            config=engine_config,
        )
    except HandlerInputError as exc:
        return _build_runtime_evidence_insufficient_result(
            definition=definition,
            request=request,
            observation_packet_id=runtime.evidence_bundle.observation_packet_id,
            evidence_artifact_ids=_trusted_bundle_artifact_ids(
                runtime.evidence_bundle,
                fallback=request.evidence_artifact_ids,
            ),
            reason=str(exc),
        )
    return _result_from_evaluation(
        definition=definition,
        request=request,
        evaluation=evaluation,
    )


async def _execute_qr_url_validation(
    *,
    runtime: RuleEngineRuntime,
    definition: RuleJudgeDefinition,
    request: RuleJudgeRequest,
    engine_config: dict[str, Any],
) -> RuleJudgeResult:
    """执行二维码或网址可达性判定。"""
    input_field = str(engine_config.get("input_field") or "url")
    explicit_url = request.judge_input.get(input_field)
    urls = (
        [explicit_url.strip()]
        if isinstance(explicit_url, str) and explicit_url.strip()
        else _trusted_qr_url_candidates(runtime.evidence_bundle)
    )
    qr_semantics_present = _trusted_qr_semantics_present(runtime.evidence_bundle)
    timeout_seconds = float(engine_config.get("timeout_seconds", 8.0))
    max_redirects = int(engine_config.get("max_redirects", 5))
    validations: list[dict[str, Any]] = []
    for url in urls[:10]:
        validation = await runtime.qr_validator(
            url=url,
            timeout_seconds=timeout_seconds,
            max_redirects=max_redirects,
        )
        _validate_qr_result(validation)
        validations.append(validation)

    if not validations:
        for screenshot in _trusted_screenshot_payloads(runtime.evidence_bundle)[:3]:
            validation = await runtime.qr_validator(
                image_base64=base64.b64encode(screenshot).decode("ascii"),
                timeout_seconds=timeout_seconds,
                max_redirects=max_redirects,
            )
            _validate_qr_result(validation)
            if validation.get("decoded_texts"):
                validations.append(validation)
                break

    evidence_artifact_ids = _trusted_bundle_artifact_ids(
        runtime.evidence_bundle,
        fallback=request.evidence_artifact_ids,
    )
    if validations and all(
        item.get("decoded_texts") and not item.get("decoded_urls") and not item["url_checks"]
        for item in validations
    ):
        # 二维码扫得出来、内容不是网址（身份凭证卡上的账号凭证码就是这种）：
        # 没有网址可验，不能按「网址失效」判未通过
        return _build_skipped_result(
            definition=definition,
            request=request,
            observation_packet_id=runtime.evidence_bundle.observation_packet_id,
            evidence_artifact_ids=evidence_artifact_ids,
            reason="二维码可以正常识别，内容不是网址（如账号凭证码），不需要校验网址能否访问",
        )
    if not validations:
        reason = (
            "页面出现二维码语义，但可信文本、UI 快照和截图中均缺少可验证的目标网址"
            if qr_semantics_present
            else "可信页面文本、UI 快照和截图均未发现二维码或二维码目标网址"
        )
        if qr_semantics_present:
            return _build_runtime_evidence_insufficient_result(
                definition=definition,
                request=request,
                observation_packet_id=runtime.evidence_bundle.observation_packet_id,
                evidence_artifact_ids=evidence_artifact_ids,
                reason=reason,
            )
        return _build_skipped_result(
            definition=definition,
            request=request,
            observation_packet_id=runtime.evidence_bundle.observation_packet_id,
            evidence_artifact_ids=evidence_artifact_ids,
            reason=reason,
        )

    passed = all(item["is_valid"] for item in validations)
    issues = [issue for validation in validations for issue in validation["issues"]]
    if not passed and not issues:
        issues = ["二维码或网址校验未提供失败原因"]
    return _build_result(
        definition=definition,
        request=request,
        passed=passed,
        pass_summary="二维码或网址可正常访问",
        fail_summary="二维码或网址有效性校验未通过",
        problem_desc="；".join(issues) if issues else None,
        judge_output={
            "validation": validations[0],
            "validations": validations,
        },
    )


def _trusted_text_or_empty(artifact: Any) -> str:
    """读取文本证据；内容为空按无文本处理，不中止判定。

    二维码等以图像为主体的页面（弹窗、身份凭证卡）可信文本可以合法为空，
    此时判定应继续走 UI 快照与截图解码兜底，而不是整体证据不足。
    """
    try:
        return artifact.decode_text()
    except RuleJudgeEvidenceInsufficientError:
        return ""


def _trusted_qr_url_candidates(evidence_bundle: Any) -> list[str]:
    """从可信文本和 UI 快照提取带二维码语义的网址。"""
    visible_text = "\n".join(
        text
        for item in evidence_bundle.artifacts_of_type("visible_text")
        if (text := _trusted_text_or_empty(item))
    )
    snapshots = [item.decode_json() for item in evidence_bundle.artifacts_of_type("ui_snapshot")]
    snapshot = next(
        (item for item in reversed(snapshots) if isinstance(item, dict)),
        {},
    )
    return candidate_qr_urls(
        {
            "facts": {
                "visible_text": visible_text,
                "ui_snapshot": snapshot,
            }
        }
    )


def _trusted_qr_semantics_present(evidence_bundle: Any) -> bool:
    """确认可信文本或 UI 快照是否出现二维码语义。"""
    pattern = re.compile(r"qr(?:code)?|二维码|扫码", re.IGNORECASE)
    if any(
        pattern.search(_trusted_text_or_empty(item))
        for item in evidence_bundle.artifacts_of_type("visible_text")
    ):
        return True
    for artifact in evidence_bundle.artifacts_of_type("ui_snapshot"):
        snapshot = artifact.decode_json()
        if isinstance(snapshot, (dict, list)) and pattern.search(
            json.dumps(snapshot, ensure_ascii=False)
        ):
            return True
    return False


def _trusted_screenshot_payloads(evidence_bundle: Any) -> list[bytes]:
    """读取可信 PNG 截图；时序截图只取有效帧并保持原顺序。"""
    payloads: list[bytes] = []
    for artifact in evidence_bundle.artifacts_of_type("screenshot"):
        payload = getattr(artifact, "payload", b"")
        content_type = str(getattr(artifact, "content_type", "")).split(";", 1)[0].casefold()
        if content_type == "image/png" and payload.startswith(b"\x89PNG\r\n\x1a\n"):
            payloads.append(payload)
            continue
        try:
            value = json.loads(payload.decode("utf-8", errors="strict"))
        except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        frames = value.get("frames") if isinstance(value, dict) else None
        if not isinstance(frames, list):
            continue
        for frame in frames:
            if not isinstance(frame, dict) or frame.get("encoding") != "base64":
                continue
            encoded = frame.get("data")
            if not isinstance(encoded, str):
                continue
            try:
                frame_payload = base64.b64decode(encoded, validate=True)
            except ValueError:
                continue
            if frame_payload.startswith(b"\x89PNG\r\n\x1a\n"):
                payloads.append(frame_payload)
    return payloads


async def _execute_collection_constraint(
    *,
    runtime: RuleEngineRuntime,
    definition: RuleJudgeDefinition,
    request: RuleJudgeRequest,
    engine_config: dict[str, Any],
) -> RuleJudgeResult:
    """执行集合大小写一致性或单调排序约束。"""
    input_field = str(engine_config.get("input_field") or "items")
    judge_input = dict(request.judge_input)
    if (
        input_field in judge_input
        and input_field == "ranking_items"
        and engine_config.get("constraint") == "monotonic"
    ):
        raw_items = judge_input.get(input_field)
        bound_items = []
        if isinstance(raw_items, list):
            for item in raw_items:
                content_name = _ranking_content_name(item)
                if isinstance(item, dict) and content_name:
                    bound_items.append({**item, "name": content_name})
        minimum = int(engine_config.get("min_items", 2))
        if len(bound_items) < minimum:
            return _ranking_binding_insufficient_result(
                definition=definition,
                request=request,
                observation_packet_id=runtime.evidence_bundle.observation_packet_id,
                evidence_artifact_ids=_trusted_bundle_artifact_ids(
                    runtime.evidence_bundle,
                    fallback=request.evidence_artifact_ids,
                ),
                bound_count=len(bound_items),
                minimum=minimum,
            )
        judge_input[input_field] = bound_items
    if input_field not in judge_input:
        if engine_config.get("constraint") == "monotonic":
            return _judge_monotonic_from_visible_text(
                runtime=runtime,
                definition=definition,
                request=request,
                engine_config=engine_config,
                input_field=input_field,
            )
        if engine_config.get("constraint") != "case_style_consistency":
            return _build_skipped_result(
                definition=definition,
                request=request,
                observation_packet_id=runtime.evidence_bundle.observation_packet_id,
                evidence_artifact_ids=_trusted_bundle_artifact_ids(
                    runtime.evidence_bundle,
                    fallback=request.evidence_artifact_ids,
                ),
                reason=f"当前页面未形成可确认的集合字段：{input_field}",
            )
        visible_text = "\n".join(
            item.decode_text() for item in runtime.evidence_bundle.artifacts_of_type("visible_text")
        )
        # 有界面快照时只比导航 / 分类栏里的标签，避免把广告位、推广瓷片的名字
        # （PG娱乐、开元棋牌之类）、内容标题和 Banner 广告文字当成分类标签导致误报
        # 「大小写不一致」；快照标出页面没有分类栏时规则不适用。
        # 只有截图、没有带导航标记的结构时才回退到页面文字（仍可能带进内容区文案）。
        nav_labels = _case_style_labels_from_navigation(runtime.evidence_bundle)
        labels = (
            nav_labels
            if nav_labels is not None
            else _case_style_labels_from_visible_text(visible_text)
        )
        minimum = int(engine_config.get("min_items", 2))
        if len(labels) < minimum:
            if runtime.verdict_mode == "balanced":
                return _build_vacuous_pass_result(
                    definition=definition,
                    request=request,
                    reason=("完整页面中不足两个含字母分类标签，不存在可形成大小写不一致的对象"),
                )
            return _build_skipped_result(
                definition=definition,
                request=request,
                observation_packet_id=runtime.evidence_bundle.observation_packet_id,
                evidence_artifact_ids=_trusted_bundle_artifact_ids(
                    runtime.evidence_bundle,
                    fallback=request.evidence_artifact_ids,
                ),
                reason="当前页面未发现足够的含字母分类标签，大小写规则不适用",
            )
        judge_input[input_field] = labels
    try:
        evaluation = evaluate_collection_constraint(
            judge_input=judge_input,
            config=engine_config,
        )
    except HandlerInputError as exc:
        raise RuleJudgeValidationError(str(exc)) from exc
    return _result_from_evaluation(
        definition=definition,
        request=request,
        evaluation=evaluation,
    )


async def _execute_required_ui_capability(
    *,
    runtime: RuleEngineRuntime,
    definition: RuleJudgeDefinition,
    request: RuleJudgeRequest,
    engine_config: dict[str, Any],
) -> RuleJudgeResult:
    """从可信文本、Action Trace 和结构化观测核对 UI 能力。"""
    visible_text = "\n".join(
        item.decode_text() for item in runtime.evidence_bundle.artifacts_of_type("visible_text")
    )
    action_traces = [
        item.decode_json() for item in runtime.evidence_bundle.artifacts_of_type("action_trace")
    ]
    try:
        evaluation = evaluate_required_ui_capability(
            judge_input=request.judge_input,
            config=engine_config,
            visible_text=visible_text,
            action_trace_text=json.dumps(action_traces, ensure_ascii=False),
        )
    except HandlerInputError as exc:
        raise RuleJudgeValidationError(str(exc)) from exc
    return _result_from_evaluation(
        definition=definition,
        request=request,
        evaluation=evaluation,
    )


async def _execute_temporal_sequence_assertion(
    *,
    runtime: RuleEngineRuntime,
    definition: RuleJudgeDefinition,
    request: RuleJudgeRequest,
    engine_config: dict[str, Any],
) -> RuleJudgeResult:
    """从可信单调时钟序列核对计数变化。"""
    temporal_trace = runtime.evidence_bundle.require_unique("temporal_trace").decode_json()
    try:
        evaluation = evaluate_temporal_sequence(
            temporal_trace=temporal_trace,
            config=engine_config,
        )
    except HandlerInputError as exc:
        message = str(exc)
        if _temporal_sequence_error_is_missing_evidence(
            temporal_trace=temporal_trace,
            engine_config=engine_config,
            message=message,
        ):
            return _build_runtime_evidence_insufficient_result(
                definition=definition,
                request=request,
                observation_packet_id=runtime.evidence_bundle.observation_packet_id,
                evidence_artifact_ids=_trusted_bundle_artifact_ids(
                    runtime.evidence_bundle,
                    fallback=request.evidence_artifact_ids,
                ),
                reason=f"可信时序证据缺少足够的倒计时计数样本：{message}",
            )
        raise RuleJudgeValidationError(message) from exc
    return _result_from_evaluation(
        definition=definition,
        request=request,
        evaluation=evaluation,
    )


def _temporal_sequence_error_is_missing_evidence(
    *,
    temporal_trace: Any,
    engine_config: dict[str, Any],
    message: str,
) -> bool:
    """区分倒计时证据缺失与确定性证据结构错误。"""
    if "未匹配计数值" in message or "可解析计数样本少于" in message:
        return True
    if "temporal_trace.samples 必须在" not in message:
        return False
    if not isinstance(temporal_trace, dict):
        return False
    samples = temporal_trace.get("samples")
    min_samples = engine_config.get("min_samples", 3)
    if isinstance(min_samples, bool) or not isinstance(min_samples, int):
        return False
    return isinstance(samples, list) and len(samples) < min_samples


async def _execute_geometry_hit_area_assertion(
    *,
    runtime: RuleEngineRuntime,
    definition: RuleJudgeDefinition,
    request: RuleJudgeRequest,
    engine_config: dict[str, Any],
) -> RuleJudgeResult:
    """从可信 UI Snapshot 核对目标点击区域。"""
    ui_snapshot = _latest_ui_snapshot(
        runtime.evidence_bundle.require_unique("ui_snapshot").decode_json()
    )
    index_field = str(engine_config.get("element_index_field") or "element_index")
    judge_input = dict(request.judge_input)
    target = _geometry_target_element(
        ui_snapshot,
        requested_index=judge_input.get(index_field),
    )
    if target is None:
        return _build_skipped_result(
            definition=definition,
            request=request,
            observation_packet_id=runtime.evidence_bundle.observation_packet_id,
            evidence_artifact_ids=_trusted_bundle_artifact_ids(
                runtime.evidence_bundle,
                fallback=request.evidence_artifact_ids,
            ),
            reason="当前页面未发现可确认的跳过或关闭控件，点击区域规则不适用",
        )
    target_index = target["element_index"]
    judge_input[index_field] = target_index
    try:
        evaluation = evaluate_geometry_hit_area(
            judge_input=judge_input,
            ui_snapshot={"elements": [target]},
            config=engine_config,
        )
    except HandlerInputError as exc:
        raise RuleJudgeValidationError(str(exc)) from exc
    return _result_from_evaluation(
        definition=definition,
        request=request,
        evaluation=evaluation,
    )


def _latest_ui_snapshot(value: Any) -> dict[str, Any]:
    """普通快照直接返回；时序快照优先返回仍包含元素的最后一帧。"""
    if not isinstance(value, dict):
        return {}
    frames = value.get("frames")
    if not isinstance(frames, list):
        return value
    for frame in reversed(frames):
        if isinstance(frame, dict) and isinstance(frame.get("elements"), list):
            return frame
    return {}


def _geometry_target_element(
    ui_snapshot: dict[str, Any],
    *,
    requested_index: Any,
) -> dict[str, Any] | None:
    """从可信快照自动定位跳过/关闭控件，并归一化旧版几何字段。"""
    raw_elements = ui_snapshot.get("elements")
    if not isinstance(raw_elements, list):
        return None
    candidates: list[dict[str, Any]] = []
    for fallback_index, raw_item in enumerate(raw_elements):
        if not isinstance(raw_item, dict):
            continue
        item = dict(raw_item)
        element_index = item.get("element_index")
        if isinstance(element_index, bool) or not isinstance(element_index, int):
            element_index = fallback_index
        item["element_index"] = element_index
        if item.get("hit_bounds") is None and isinstance(item.get("rect"), dict):
            item["hit_bounds"] = dict(item["rect"])
        if item.get("visual_bounds") is None and isinstance(item.get("rect"), dict):
            item["visual_bounds"] = dict(item["rect"])
        if not isinstance(item.get("disabled"), bool):
            item["disabled"] = False
        if isinstance(requested_index, int) and not isinstance(requested_index, bool):
            if element_index == requested_index:
                return item
            continue
        identity = " ".join(
            (
                str(item.get("text") or ""),
                str(item.get("aria_label") or ""),
                str(item.get("selector") or ""),
                " ".join(str(token) for token in item.get("class_tokens") or []),
            )
        )
        if re.search(r"跳过|关闭|skip|close|countdown|down-time", identity, re.IGNORECASE):
            candidates.append(item)
    return candidates[0] if candidates else None


def _case_style_labels_from_visible_text(visible_text: str) -> list[str]:
    """保守提取短分类标签，排除句子、倒计时和纯数字文案。"""
    labels: list[str] = []
    for raw_line in visible_text.splitlines():
        label = raw_line.strip()
        if (
            not label
            or len(label) > 40
            or not re.search(r"[A-Za-z]", label)
            or re.search(r"\d", label)
            or re.search(r"[。！？!?；;：:]", label)
        ):
            continue
        if label not in labels:
            labels.append(label)
        if len(labels) >= 100:
            break
    return labels


def _case_style_labels_from_navigation(evidence_bundle: Any) -> list[str] | None:
    """只从导航 / 分类栏元素取分类标签，排除内容区和广告（弹窗、Banner、推广瓷片）。

    返回 None 表示证据里没有可用的界面快照，或快照没标出导航范围（元素上没有
    within_navigation 字段），交给调用方回退到页面文字提取；返回列表（可能为空）
    表示已按导航范围提取，空列表意味着没有含字母的分类标签、规则不适用。
    快照标了导航范围却没有导航元素时同样返回空列表：页面上没有分类栏，
    内容标题、Banner 广告里的文字都不是分类标签，不能拿整页文字凑数。
    """
    snapshots = [
        snapshot
        for artifact in evidence_bundle.artifacts_of_type("ui_snapshot")
        if isinstance((snapshot := artifact.decode_json()), dict)
    ]
    snapshot = snapshots[-1] if snapshots else None
    if snapshot is None:
        return None
    elements = snapshot.get("elements")
    if not isinstance(elements, list):
        return None
    labels: list[str] = []
    found_navigation = False
    marks_navigation = False
    for item in elements:
        if not isinstance(item, dict):
            continue
        marks_navigation = (
            marks_navigation or "within_navigation" in item or "within_web_navigation" in item
        )
        role = str(item.get("role") or "").casefold()
        in_navigation = bool(
            item.get("within_navigation")
            or item.get("within_web_navigation")
            or role in {"tab", "tablist"}
        )
        # 广告弹窗 / 推广位里的页签（热门软件、游戏赢钱之类）不是产品的分类标签
        if not in_navigation or item.get("is_advertisement"):
            continue
        found_navigation = True
        label = str(item.get("text") or "").strip()
        if (
            not label
            or len(label) > 40
            or not re.search(r"[A-Za-z]", label)
            or re.search(r"\d", label)
            or re.search(r"[。！？!?；;：:]", label)
        ):
            continue
        if label not in labels:
            labels.append(label)
        if len(labels) >= 100:
            break
    return labels if found_navigation or marks_navigation else None


# 榜单语义：排行 / 榜 / TOP 是明确榜单；最热 / 热门这类标签只说明列表可能按热度排
_RANKING_STRONG_PATTERN = re.compile(r"榜|排行|top\s*\d+", re.IGNORECASE)
_RANKING_WEAK_PATTERN = re.compile(r"最热|热播|热门|热度|人气|播放量|观看")
# 只认带单位或带播放 / 热度字样的数；裸数字（点赞数、倒计时、集数）和时长不算播放量
_RANKING_METRIC_PATTERN = re.compile(
    r"^(?P<prefix>(?:播放量|播放|热度|人气|观看|浏览)\s*[:：]?\s*)?"
    r"(?P<number>\d[\d,]*(?:\.\d+)?)\s*(?P<unit>万|亿|[wWkKmM])?\s*"
    r"(?P<suffix>次播放|次观看|人次观看|人次|次|播放|观看|热度|人气)?"
    r"(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?$"
)
_RANKING_DURATION_PATTERN = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")
_RANKING_UNIT_MULTIPLIERS = {"万": 10_000, "w": 10_000, "亿": 100_000_000, "k": 1_000, "m": 1_000_000}
_RANKING_NON_CONTENT_LABEL = re.compile(
    r"^(?:查看|加载|展开|显示)?更多(?:推荐|内容|视频|作品|精彩)?$"
    r"|^(?:查看)?全部$"
    r"|^换一[换批]$"
    r"|^(?:展开|收起)$"
)
RANKING_CONTENT_METRIC_UNBOUND = "ranking_content_metric_unbound"


def _ranking_semantics_strength(visible_text: str) -> str | None:
    """页面文字里有没有榜单语义：strong=明确榜单，weak=只有热度类标签，None=都没有。"""
    if _RANKING_STRONG_PATTERN.search(visible_text):
        return "strong"
    if _RANKING_WEAK_PATTERN.search(visible_text):
        return "weak"
    return None


def _ranking_label_is_content(label: str) -> bool:
    """榜单项必须是内容标题；「更多」这类入口和纯数字不是内容。"""
    text = " ".join(label.split())
    if len(text) < 2 or _RANKING_NON_CONTENT_LABEL.fullmatch(text):
        return False
    return re.fullmatch(r"[\d\s.,]+", text) is None


def _ranking_items_from_visible_text(visible_text: str) -> tuple[list[dict[str, Any]], int]:
    """按页面顺序抽「名字 + 播放量 / 热度」。

    一条内容只绑紧挨着它的一个指标。「更多」等没有对应内容的数字不算榜单项。
    返回值第二项是因此丢掉的数字个数。
    """
    items: list[dict[str, Any]] = []
    unbound = 0
    previous_label = ""
    for raw_line in visible_text.splitlines():
        line = raw_line.strip()
        if not line or _RANKING_DURATION_PATTERN.match(line):
            continue
        matched = _RANKING_METRIC_PATTERN.match(line)
        if matched is None or not (matched["unit"] or matched["prefix"] or matched["suffix"]):
            previous_label = line[:60]
            continue
        if not _ranking_label_is_content(previous_label):
            unbound += 1
            previous_label = ""
            continue
        unit = (matched["unit"] or "").casefold()
        value = float(matched["number"].replace(",", "")) * _RANKING_UNIT_MULTIPLIERS.get(unit, 1)
        items.append({"name": previous_label, "metric": value})
        previous_label = ""
        if len(items) >= 100:
            break
    return items, unbound


def _ranking_content_name(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    for key in ("name", "title", "content"):
        value = item.get(key)
        if isinstance(value, str) and _ranking_label_is_content(value):
            return " ".join(value.split())[:60]
    return ""


def _format_ranking_value(value: float) -> str:
    return str(int(value)) if value.is_integer() else f"{value:g}"


def _ranking_binding_insufficient_result(
    *,
    definition: RuleJudgeDefinition,
    request: RuleJudgeRequest,
    observation_packet_id: str | None,
    evidence_artifact_ids: tuple[str, ...],
    bound_count: int,
    minimum: int,
) -> RuleJudgeResult:
    """数字没有和同一条榜单内容绑定时，不拿这些数字判排序失败。"""
    reason = (
        f"「更多」等没有对应内容的数字不能当作榜单项，"
        f"同一条榜单上只绑到 {bound_count} 组内容与指标（至少要 {minimum} 组）"
    )
    result = _build_runtime_evidence_insufficient_result(
        definition=definition,
        request=request,
        observation_packet_id=observation_packet_id,
        evidence_artifact_ids=evidence_artifact_ids,
        reason=reason,
    )
    return replace(
        result,
        judge_output={
            **result.judge_output,
            "gap_codes": [RANKING_CONTENT_METRIC_UNBOUND],
        },
    )


def _judge_monotonic_from_visible_text(
    *,
    runtime: RuleEngineRuntime,
    definition: RuleJudgeDefinition,
    request: RuleJudgeRequest,
    engine_config: dict[str, Any],
    input_field: str,
) -> RuleJudgeResult:
    """没人给结构化榜单数据时，程序自己从可信页面文字抽播放量 / 热度再查排序。

    明确榜单页（排行 / 榜 / TOP）读不出数值记证据不足、读出来就真判；
    只有「最热 / 热门」这类标签的列表看不出当前按什么排，数值降序才判通过，
    否则按不适用跳过并把读到的数值写进原因，避免把「最新」列表误判成榜单倒置。
    """
    visible_text = "\n".join(
        item.decode_text() for item in runtime.evidence_bundle.artifacts_of_type("visible_text")
    )
    evidence_artifact_ids = _trusted_bundle_artifact_ids(
        runtime.evidence_bundle,
        fallback=request.evidence_artifact_ids,
    )

    def skipped(reason: str) -> RuleJudgeResult:
        return _build_skipped_result(
            definition=definition,
            request=request,
            observation_packet_id=runtime.evidence_bundle.observation_packet_id,
            evidence_artifact_ids=evidence_artifact_ids,
            reason=reason,
        )

    strength = _ranking_semantics_strength(visible_text)
    items, unbound_metrics = _ranking_items_from_visible_text(visible_text)
    minimum = int(engine_config.get("min_items", 2))
    if strength is None:
        return skipped("页面文字里没有榜单、排行或热度排序的标识，排序规则不适用")
    if len(items) < minimum:
        if strength == "strong" and unbound_metrics:
            return _ranking_binding_insufficient_result(
                definition=definition,
                request=request,
                observation_packet_id=runtime.evidence_bundle.observation_packet_id,
                evidence_artifact_ids=evidence_artifact_ids,
                bound_count=len(items),
                minimum=minimum,
            )
        if strength == "strong":
            return _build_runtime_evidence_insufficient_result(
                definition=definition,
                request=request,
                observation_packet_id=runtime.evidence_bundle.observation_packet_id,
                evidence_artifact_ids=evidence_artifact_ids,
                reason=(
                    f"页面有榜单标识，但只读出 {len(items)} 个播放量或热度数值（至少要 {minimum} 个），"
                    "缺榜单数据：可按页面顺序把「名字 播放量」填进榜单数据再判"
                ),
            )
        return skipped(
            f"页面只有「最热 / 热门」这类标签，读出的播放量或热度数值不足 {minimum} 个，不按榜单判"
        )
    try:
        evaluation = evaluate_collection_constraint(
            judge_input={**request.judge_input, input_field: items},
            config=engine_config,
        )
    except HandlerInputError as exc:
        raise RuleJudgeValidationError(str(exc)) from exc
    if not evaluation.passed and strength == "weak":
        values = "、".join(_format_ranking_value(item["metric"]) for item in items[:10])
        return skipped(
            "读出的播放量或热度数值没有按降序排，但页面只有「最热 / 热门」这类标签，"
            "看不出列表当前是不是按热度排序（比如「最新 / 最热」哪个选中），不按榜单判；"
            f"确认是榜单时把「名字 播放量」填进榜单数据再判。读到的数值依次是：{values}"
        )
    return _result_from_evaluation(
        definition=definition,
        request=request,
        evaluation=HandlerEvaluation(
            passed=evaluation.passed,
            pass_summary=evaluation.pass_summary,
            fail_summary=evaluation.fail_summary,
            problem_desc=evaluation.problem_desc,
            judge_output={**evaluation.judge_output, "items_source": "visible_text", "items": items},
        ),
    )

async def _execute_external_link_open_mode(
    *,
    runtime: RuleEngineRuntime,
    definition: RuleJudgeDefinition,
    request: RuleJudgeRequest,
    engine_config: dict[str, Any],
) -> RuleJudgeResult:
    """从可信浏览器 Action Trace 核对外链窗口行为。"""
    evidence_by_role = _decode_role_evidence(
        runtime.evidence_bundle,
        artifact_types=("action_trace", "ui_snapshot"),
    )
    action_role = str(engine_config.get("action_role") or "primary")
    role_evidence = evidence_by_role.get(action_role) or {}
    action_trace = role_evidence.get("action_trace")
    click_actions = (
        [item for item in action_trace if isinstance(item, dict) and item.get("action") == "click"]
        if isinstance(action_trace, list)
        else []
    )
    if not click_actions:
        evidence_artifact_ids = _trusted_bundle_artifact_ids(
            runtime.evidence_bundle,
            fallback=request.evidence_artifact_ids,
        )
        if not _has_external_link_target(role_evidence.get("ui_snapshot")):
            if runtime.verdict_mode == "balanced":
                return _build_vacuous_pass_result(
                    definition=definition,
                    request=request,
                    reason=("完整页面中不存在跨域外部链接，因此未出现错误窗口打开方式"),
                )
            return _build_skipped_result(
                definition=definition,
                request=request,
                observation_packet_id=runtime.evidence_bundle.observation_packet_id,
                evidence_artifact_ids=evidence_artifact_ids,
                reason="当前页面未发现可见的跨域外部链接，规则不适用于本页",
            )
        return _build_runtime_evidence_insufficient_result(
            definition=definition,
            request=request,
            observation_packet_id=runtime.evidence_bundle.observation_packet_id,
            evidence_artifact_ids=evidence_artifact_ids,
            reason="页面存在跨域外部链接，但尚未完成安全单击与窗口导航观察",
        )
    try:
        evaluation = evaluate_external_link_open_mode(
            evidence_by_role=evidence_by_role,
            config=engine_config,
        )
    except HandlerInputError as exc:
        raise RuleJudgeValidationError(str(exc)) from exc
    return _result_from_evaluation(
        definition=definition,
        request=request,
        evaluation=evaluation,
    )


_BOTTOM_TAB_MARKERS = (
    "底栏",
    "底部导航",
    "底部tab",
    "bottom_tab",
    "bottom_navigation",
    "tab_seek",
)
_POST_MEDIA_MARKERS = ("缩略图", "thumbnail", "帖子图片", "帖子视频")


def _bottom_tab_switch_without_post_media(evidence_by_role: dict[str, dict[str, Any]]) -> bool:
    """底栏切栏不是同一帖子里点缩略图进全屏，不能拿来判全屏浏览。"""
    actions: list[dict[str, Any]] = []
    for evidence in evidence_by_role.values():
        trace = evidence.get("action_trace") if isinstance(evidence, dict) else None
        if isinstance(trace, list):
            actions.extend(item for item in trace if isinstance(item, dict))
    if not actions or not any(_action_is_bottom_tab(item) for item in actions):
        return False
    return not any(_action_is_post_media_click(item) for item in actions)


def _action_is_bottom_tab(action: dict[str, Any]) -> bool:
    text = _action_identity_text(action)
    return any(marker in text for marker in _BOTTOM_TAB_MARKERS)


def _action_is_post_media_click(action: dict[str, Any]) -> bool:
    if action.get("action") != "click" or _action_is_bottom_tab(action):
        return False
    text = _action_identity_text(action)
    return any(marker in text for marker in _POST_MEDIA_MARKERS)


def _action_identity_text(action: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("semantic_target", "purpose", "selector", "transition_kind", "result"):
        value = action.get(key)
        if isinstance(value, str):
            parts.append(value)
    for key in ("target_before", "target_after"):
        target = action.get(key)
        if not isinstance(target, dict):
            continue
        for nested in ("text", "aria_label", "name", "semantic_target"):
            value = target.get(nested)
            if isinstance(value, str):
                parts.append(value)
    return " ".join(parts).casefold()


def _network_fault_is_playback_precondition(engine_config: dict[str, Any]) -> bool:
    """广告失败后的正片检查把网络故障动作当作前提，而不是播放结论本身。"""
    pattern = engine_config.get("action_pattern")
    return isinstance(pattern, str) and "network_fault" in pattern


def _trace_has_effective_ad_fault(action_trace: Any) -> bool:
    from services.api.app.application.ad_load_fallback import assess_ad_load_fallback

    if not isinstance(action_trace, list):
        return False
    return any(
        isinstance(item, dict) and assess_ad_load_fallback(item)["fault_effective"] is True
        for item in action_trace
    )


def _playback_evaluation_after_effective_fault(
    evaluation: HandlerEvaluation,
) -> HandlerEvaluation:
    """故障已经生效后，只按正片文本判断播放，不再把故障动作正则当成播放失败。"""
    checks = list(evaluation.judge_output.get("checks") or [])
    playback_checks = [item for item in checks if item.get("name") != "action_pattern"]
    passed = bool(playback_checks) and all(bool(item.get("passed")) for item in playback_checks)
    failed_names = [
        str(item.get("name") or "")
        for item in playback_checks
        if not item.get("passed")
    ]
    problem = None if passed else "广告故障已生效，但正片没有开始播放：" + ", ".join(failed_names)
    return replace(
        evaluation,
        passed=passed,
        fail_summary="广告故障已生效，但正片没有开始播放",
        problem_desc=problem,
        judge_output={**evaluation.judge_output, "ad_fault_effective": True},
    )


async def _execute_state_transition_assertion(
    *,
    runtime: RuleEngineRuntime,
    definition: RuleJudgeDefinition,
    request: RuleJudgeRequest,
    engine_config: dict[str, Any],
) -> RuleJudgeResult:
    """从多采集可信证据核对操作前后与刷新后的状态。"""
    evidence_by_role = _decode_role_evidence(
        runtime.evidence_bundle,
        artifact_types=("visible_text", "action_trace", "ui_snapshot"),
    )
    tab_switch_without_post_media = (
        engine_config.get("after_fullscreen_required") is True
        and _bottom_tab_switch_without_post_media(evidence_by_role)
    )
    if tab_switch_without_post_media:
        reason = "采到的是底栏切换，缺同一帖子内缩略图点击后全屏的配对画面。"
        result = _build_runtime_evidence_insufficient_result(
            definition=definition,
            request=request,
            observation_packet_id=runtime.evidence_bundle.observation_packet_id,
            evidence_artifact_ids=_trusted_bundle_artifact_ids(
                runtime.evidence_bundle,
                fallback=request.evidence_artifact_ids,
            ),
            reason=reason,
        )
        return replace(
            result,
            judge_output={
                **result.judge_output,
                "gap_codes": ["post_media_fullscreen_pair_missing"],
            },
        )
    try:
        evaluation = evaluate_state_transition(
            evidence_by_role=evidence_by_role,
            config=engine_config,
        )
    except HandlerInputError as exc:
        raise RuleJudgeValidationError(str(exc)) from exc
    if _network_fault_is_playback_precondition(engine_config):
        action_role = str(engine_config.get("action_role") or "after")
        action_trace = (evidence_by_role.get(action_role) or {}).get("action_trace")
        if not _trace_has_effective_ad_fault(action_trace):
            from services.api.app.application.ad_load_fallback import (
                AD_LOAD_FAULT_EFFECT_MISSING,
            )

            reason = "尚未证实广告故障生效，不能判定正片播放失败"
            result = _build_runtime_evidence_insufficient_result(
                definition=definition,
                request=request,
                observation_packet_id=runtime.evidence_bundle.observation_packet_id,
                evidence_artifact_ids=_trusted_bundle_artifact_ids(
                    runtime.evidence_bundle,
                    fallback=request.evidence_artifact_ids,
                ),
                reason=reason,
            )
            return replace(
                result,
                judge_output={
                    **result.judge_output,
                    "gap_codes": [AD_LOAD_FAULT_EFFECT_MISSING],
                    "ad_fault_effective": False,
                },
            )
        evaluation = _playback_evaluation_after_effective_fault(evaluation)
    return _result_from_evaluation(
        definition=definition,
        request=request,
        evaluation=evaluation,
    )


def _decode_role_evidence(
    evidence_bundle: Any,
    *,
    artifact_types: tuple[str, ...],
    capture_roles: frozenset[str] | None = None,
) -> dict[str, dict[str, Any]]:
    evidence_by_role: dict[str, dict[str, Any]] = {}
    for artifact_type in artifact_types:
        for artifact in evidence_bundle.artifacts_of_type(artifact_type):
            role = getattr(artifact, "capture_role", None)
            if not isinstance(role, str) or not role:
                raise RuleJudgeValidationError(f"{artifact_type} 证据缺少有效 capture_role")
            if capture_roles is not None and role not in capture_roles:
                continue
            role_evidence = evidence_by_role.setdefault(role, {})
            payload = (
                artifact.decode_text()
                if artifact_type == "visible_text"
                else artifact.decode_json()
            )
            evidence_key = artifact_type
            if (
                artifact_type == "ui_snapshot" and isinstance(payload, dict)
                and payload.get("schema_version") == "ui-snapshot-sequence.v1"
                and payload.get("sequence") is True
                and isinstance(payload.get("frames"), list)
            ):
                # 序列属于同一可信采集的时序输入，不能覆盖基线或混成重复基线。
                evidence_key = "ui_snapshot_sequence"
            if evidence_key in role_evidence:
                raise RuleJudgeValidationError(f"采集角色 {role} 存在重复 {evidence_key} 证据")
            role_evidence[evidence_key] = payload
    return evidence_by_role


def _fill_har_from_app_network_trace(
    evidence_by_role: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """App 侧缺 har 时，用同角色 network_trace 投影 Web 兼容 network_events。"""
    from services.api.app.application.app_surface_evidence import (
        project_har_from_network_trace,
    )

    for _role, evidence in evidence_by_role.items():
        if not isinstance(evidence, dict):
            continue
        existing = evidence.get("har")
        if isinstance(existing, dict) and existing.get("format") == "network_events":
            continue
        network_trace = evidence.get("network_trace")
        if not isinstance(network_trace, dict):
            continue
        evidence["har"] = project_har_from_network_trace(network_trace)
    return evidence_by_role


async def _execute_price_relation(
    *,
    runtime: RuleEngineRuntime,
    definition: RuleJudgeDefinition,
    request: RuleJudgeRequest,
    engine_config: dict[str, Any],
) -> RuleJudgeResult:
    """从可信多位置文本核对同商品价格一致性。"""
    try:
        evaluation = evaluate_price_relation(
            evidence_by_role=_decode_role_evidence(
                runtime.evidence_bundle,
                artifact_types=("visible_text",),
            ),
            config=engine_config,
        )
    except HandlerPreconditionMissing as exc:
        if runtime.h5_surface:
            raise RuleJudgePriceTargetMissingError(str(exc)) from exc
        raise RuleJudgeValidationError(str(exc)) from exc
    except HandlerInputError as exc:
        raise RuleJudgeValidationError(str(exc)) from exc
    return _result_from_evaluation(
        definition=definition,
        request=request,
        evaluation=evaluation,
    )


async def _execute_rate_limit_assertion(
    *,
    runtime: RuleEngineRuntime,
    definition: RuleJudgeDefinition,
    request: RuleJudgeRequest,
    engine_config: dict[str, Any],
) -> RuleJudgeResult:
    """从连续探测采集核对限频与重复内容抑制。"""
    try:
        evaluation = evaluate_rate_limit(
            evidence_by_role=_fill_har_from_app_network_trace(
                _decode_role_evidence(
                    runtime.evidence_bundle,
                    artifact_types=("visible_text", "action_trace", "har", "network_trace"),
                )
            ),
            config=engine_config,
            # App 采不到网络记录：没有 har 的那次尝试只看页面上的限频 / 验证码提示
            network_optional=runtime.app_surface,
        )
    except HandlerInputError as exc:
        raise RuleJudgeValidationError(str(exc)) from exc
    return _result_from_evaluation(
        definition=definition,
        request=request,
        evaluation=evaluation,
    )


async def _execute_content_lifecycle_state(
    *,
    runtime: RuleEngineRuntime,
    definition: RuleJudgeDefinition,
    request: RuleJudgeRequest,
    engine_config: dict[str, Any],
) -> RuleJudgeResult:
    """从来源与消费入口采集核对失效内容展示状态。"""
    try:
        evaluation = evaluate_content_lifecycle(
            evidence_by_role=_decode_role_evidence(
                runtime.evidence_bundle,
                artifact_types=("visible_text",),
            ),
            config=engine_config,
        )
    except HandlerPreconditionMissing as exc:
        # 缺事实不能当作传输故障重试；H5 交回既有缺证据/补采合同，不伪造适用或通过。
        if runtime.h5_surface:
            raise RuleJudgeEvidenceInsufficientError(str(exc)) from exc
        raise RuleJudgeValidationError(str(exc)) from exc
    except HandlerInputError as exc:
        raise RuleJudgeValidationError(str(exc)) from exc
    return _result_from_evaluation(
        definition=definition,
        request=request,
        evaluation=evaluation,
    )


def _validate_technical_error_config(engine_config: dict[str, Any]) -> str | None:
    pattern = engine_config.get("pattern")
    if not isinstance(pattern, str) or not pattern.strip():
        return "technical_error_text.pattern 必须为非空字符串"
    try:
        re.compile(pattern)
    except re.error as exc:
        return f"technical_error_text.pattern 不是有效正则：{exc}"
    return _validate_optional_field_name(engine_config, "input_field")


def _validate_discount_price_config(engine_config: dict[str, Any]) -> str | None:
    for field_name in ("discount_price_field", "original_price_field"):
        error = _validate_optional_field_name(engine_config, field_name)
        if error is not None:
            return error
    operator = engine_config.get("operator", "lt")
    if operator not in {"lt", "lte", "eq", "gte", "gt"}:
        return "discount_price_compare.operator 必须为 lt/lte/eq/gte/gt"
    pass_when_absent = engine_config.get("pass_when_discount_absent", False)
    if not isinstance(pass_when_absent, bool):
        return "discount_price_compare.pass_when_discount_absent 必须为布尔值"
    marker_pattern = engine_config.get("discount_marker_pattern")
    if marker_pattern is not None:
        if not isinstance(marker_pattern, str) or not marker_pattern:
            return "discount_price_compare.discount_marker_pattern 必须为非空正则"
        try:
            re.compile(marker_pattern)
        except re.error as exc:
            return f"discount_price_compare.discount_marker_pattern 不是有效正则：{exc}"
    return None


def _validate_qr_url_config(engine_config: dict[str, Any]) -> str | None:
    field_error = _validate_optional_field_name(engine_config, "input_field")
    if field_error is not None:
        return field_error
    timeout = engine_config.get("timeout_seconds", 8.0)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        return "qr_url_validation.timeout_seconds 必须为数值"
    if not isfinite(float(timeout)) or not 0.1 <= float(timeout) <= 60:
        return "qr_url_validation.timeout_seconds 必须在 0.1 到 60 之间"
    redirects = engine_config.get("max_redirects", 5)
    if isinstance(redirects, bool) or not isinstance(redirects, int):
        return "qr_url_validation.max_redirects 必须为整数"
    if not 0 <= redirects <= 20:
        return "qr_url_validation.max_redirects 必须在 0 到 20 之间"
    return None


def _validate_packet_capability_config(
    engine_config: dict[str, Any],
) -> str | None:
    handler = engine_config.get("handler")
    if not isinstance(handler, str) or handler not in CAPABILITY_HANDLER_NAMES:
        return f"Observation Packet Handler 未注册：{handler}"
    if handler == "page_url_status_handler":
        statuses = engine_config.get("allowed_status_codes", [200])
        if (
            not isinstance(statuses, list)
            or not statuses
            or len(statuses) > 20
            or any(
                isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599
                for status in statuses
            )
            or len(set(statuses)) != len(statuses)
        ):
            return "page_url_status_handler.allowed_status_codes 必须为不重复的 HTTP 状态码数组"
    return None


def _validate_optional_field_name(
    engine_config: dict[str, Any],
    field: str,
) -> str | None:
    value = engine_config.get(field)
    if value is not None and (not isinstance(value, str) or not value.strip()):
        return f"{field} 必须为非空字符串"
    return None


def _validate_qr_result(validation: Any) -> None:
    if not isinstance(validation, dict):
        raise RuleJudgeNotConfiguredError("qr_url_validation 返回值必须为对象")
    if not isinstance(validation.get("is_valid"), bool):
        raise RuleJudgeNotConfiguredError("qr_url_validation.is_valid 必须为布尔值")
    issues = validation.get("issues")
    if not isinstance(issues, list) or not all(isinstance(item, str) for item in issues):
        raise RuleJudgeNotConfiguredError("qr_url_validation.issues 必须为字符串数组")
    url_checks = validation.get("url_checks")
    if not isinstance(url_checks, list):
        raise RuleJudgeNotConfiguredError("qr_url_validation.url_checks 必须为数组")
    if not all(
        isinstance(item, dict) and isinstance(item.get("is_valid"), bool) for item in url_checks
    ):
        raise RuleJudgeNotConfiguredError("qr_url_validation.url_checks 必须包含布尔 is_valid")
    expected_valid = bool(url_checks) and all(item["is_valid"] for item in url_checks)
    if validation["is_valid"] is not expected_valid:
        raise RuleJudgeNotConfiguredError("qr_url_validation.is_valid 与 URL 校验明细不一致")


def _regex_match_details(text: str, *, pattern: str) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    truncated = False
    for index, match in enumerate(re.finditer(pattern, text)):
        if index >= MAX_REGEX_MATCH_DETAILS:
            truncated = True
            break
        matches.append(
            {
                "text": match.group(0)[:200],
                "start": match.start(),
                "end": match.end(),
            }
        )
    return {"matches": matches, "truncated": truncated}


def _result_from_evaluation(
    *,
    definition: RuleJudgeDefinition,
    request: RuleJudgeRequest,
    evaluation: HandlerEvaluation,
) -> RuleJudgeResult:
    return _build_result(
        definition=definition,
        request=request,
        passed=evaluation.passed,
        pass_summary=evaluation.pass_summary,
        fail_summary=evaluation.fail_summary,
        problem_desc=evaluation.problem_desc,
        judge_output=evaluation.judge_output,
    )


def _build_vacuous_pass_result(
    *,
    definition: RuleJudgeDefinition,
    request: RuleJudgeRequest,
    reason: str,
) -> RuleJudgeResult:
    """均衡模式下，对“没有目标即自然满足”的低风险规则形成可审计通过。"""
    return _build_result(
        definition=definition,
        request=request,
        passed=True,
        pass_summary="当前页面未出现适用目标，均衡模式按自然满足处理",
        fail_summary="",
        problem_desc=None,
        judge_output={
            "schema_version": "1.0",
            "conclusion": "pass",
            "applicability": "vacuous_pass",
            "policy_mode": "balanced",
            "reasoning_summary": reason,
        },
    )


def _build_skipped_result(
    *,
    definition: RuleJudgeDefinition,
    request: RuleJudgeRequest,
    observation_packet_id: str | None,
    evidence_artifact_ids: tuple[str, ...],
    reason: str,
    packet: ObservationPacket | None = None,
    tool_results: list[dict[str, Any]] | None = None,
    reason_code: str | None = None,
) -> RuleJudgeResult:
    """目标场景未在当前旅程出现时明确跳过，避免误计为证据不足。"""
    return RuleJudgeResult(
        rule_id=definition.rule_id,
        rule_code=definition.rule_code,
        executor_type=definition.executor_type,
        judge_strategy=definition.executor_type,
        execution_status="skipped",
        confidence=1.0,
        result_summary="当前页面或旅程未出现该规则的适用目标，已跳过",
        problem_desc=reason,
        page_capture_id=request.page_capture_id,
        observation_packet_id=observation_packet_id,
        evidence_artifact_ids=evidence_artifact_ids,
        judge_input=(
            _observation_judge_input(packet, tool_results or [])
            if packet is not None
            else _summarize_judge_input(request.judge_input)
        ),
        judge_output={
            "schema_version": "1.0",
            "conclusion": "skipped",
            "applicability": "not_applicable",
            "reasoning_summary": reason,
            **({"reason": reason_code} if reason_code is not None else {}),
            **({"tool_results": tool_results} if tool_results is not None else {}),
        },
    )


def _h5_payload_budget_exceeded(packet: ObservationPacket) -> bool:
    identity = packet.packet_payload.get("identity") or {}
    quality = packet.packet_payload.get("quality_gate") or {}
    return identity.get("target_platform") == "h5" and quality.get("payload_budget_exceeded") is True


def _build_quality_gate_block_result(
    *,
    definition: RuleJudgeDefinition,
    request: RuleJudgeRequest,
    packet: ObservationPacket,
) -> RuleJudgeResult:
    """质量门禁非 pass 时的统一出口。

    E02 条款 1.4：清单成为齐全性权威后，「非 pass」不再一律报证据不足——
    只有清单裁决 fail 才是真正的证据不足，pending 属在途、缺席属控制面未就绪，
    各自使用独立业务结果与原因码（条款 2.1）。
    非清单来源（off 模式旧读取）保持原有 evidence_insufficient 语义不变。
    """
    gate = packet.packet_payload.get("quality_gate")
    gate = gate if isinstance(gate, dict) else {}
    if _h5_payload_budget_exceeded(packet) and not gate.get("missing_sources"):
        # 输入容量问题不生成现场补采请求，保留原制品供人工复核。
        return RuleJudgeResult(
            rule_id=definition.rule_id,
            rule_code=definition.rule_code,
            executor_type=definition.executor_type,
            judge_strategy=definition.executor_type,
            execution_status="manual_review",
            confidence=0.0,
            result_summary="判定输入超限，需人工复核",
            problem_desc=gate.get("payload_budget_reason"),
            page_capture_id=request.page_capture_id,
            observation_packet_id=packet.observation_packet_id,
            evidence_artifact_ids=packet.evidence_artifact_ids,
            judge_input=_summarize_judge_input(request.judge_input),
            judge_output={"schema_version": "1.0", "conclusion": "manual_review",
                          "reason_code": "payload_budget_exceeded",
                          "reasoning_summary": gate.get("payload_budget_reason")},
        )
    if gate.get("source") != MANIFEST_QUALITY_GATE_SOURCE:
        return _build_evidence_insufficient_result(
            definition=definition,
            request=request,
            packet=packet,
            reason=(packet.quality_gate_reason or "Observation Packet 质量门禁未通过"),
        )
    outcome = (
        ManifestReadOutcome.EVIDENCE_INSUFFICIENT
        if packet.quality_gate_status == "fail"
        else ManifestReadOutcome.EVIDENCE_PENDING
    )
    execution_status = REJECTION_STATUS_BY_OUTCOME[outcome]
    reason_code = REJECTION_REASON_CODE_BY_OUTCOME[outcome]
    reason = packet.quality_gate_reason or "证据清单未通过齐全性裁决"
    result = RuleJudgeResult(
        rule_id=definition.rule_id,
        rule_code=definition.rule_code,
        executor_type=definition.executor_type,
        judge_strategy=definition.executor_type,
        execution_status=execution_status,
        confidence=0.0,
        result_summary="证据清单未支持自动判定",
        problem_desc=reason,
        page_capture_id=request.page_capture_id,
        observation_packet_id=packet.observation_packet_id,
        evidence_artifact_ids=packet.evidence_artifact_ids,
        judge_input={
            **_summarize_judge_input(request.judge_input),
            "manifest_read": {
                "business_outcome": outcome.value,
                "reason_code": reason_code,
                "manifest_id": gate.get("manifest_id"),
                "manifest_version": gate.get("manifest_version"),
                "quality_status": packet.quality_gate_status,
            },
        },
        judge_output={
            "schema_version": "1.0",
            "conclusion": execution_status,
            "reason_code": reason_code,
            "evidence_trusted": False,
            "reasoning_summary": reason,
        },
    )
    return _attach_evidence_gap(result, definition=definition, request=request, packet=packet)


def _build_batch_observation_packet_error_result(
    *,
    definition: RuleJudgeDefinition,
    request: RuleJudgeRequest,
    error: ObservationPacketValidationError,
) -> RuleJudgeResult:
    """隔离观察包校验故障；技术错误不伪装成缺证，也不引用未通过校验的输入。"""
    reason = str(error)
    return RuleJudgeResult(
        rule_id=definition.rule_id,
        rule_code=definition.rule_code,
        executor_type=definition.executor_type,
        judge_strategy=definition.executor_type,
        execution_status="error",
        confidence=0.0,
        result_summary="观察包校验失败，已停止本条规则判定",
        problem_desc=reason,
        page_capture_id=request.page_capture_id,
        observation_packet_id=None,
        evidence_artifact_ids=(),
        judge_input=_summarize_judge_input(request.judge_input),
        judge_output={
            "schema_version": "1.0",
            "conclusion": "error",
            "reason_code": "OBSERVATION_PACKET_VALIDATION_FAILED",
            "exception_type": type(error).__name__,
            "evidence_trusted": False,
            "reasoning_summary": reason,
        },
    )


def _build_batch_isolated_failure_result(
    *,
    definition: RuleJudgeDefinition,
    request: RuleJudgeRequest,
    reason: str,
) -> RuleJudgeResult:
    """把单条规则的契约或证据问题收敛为自身结论，不牵连同批其余规则。"""
    result = RuleJudgeResult(
        rule_id=definition.rule_id,
        rule_code=definition.rule_code,
        executor_type=definition.executor_type,
        judge_strategy=definition.executor_type,
        execution_status="evidence_insufficient",
        confidence=0.0,
        result_summary="本条规则的判定输入不可用，已按证据不足收口",
        problem_desc=reason,
        page_capture_id=request.page_capture_id,
        observation_packet_id=None,
        evidence_artifact_ids=request.evidence_artifact_ids,
        judge_input=_summarize_judge_input(request.judge_input),
        judge_output={
            "schema_version": "1.0",
            "conclusion": "evidence_insufficient",
            "reason_code": "RULE_JUDGE_INPUT_UNAVAILABLE",
            "evidence_trusted": False,
            "reasoning_summary": reason,
        },
    )
    return _attach_evidence_gap(result, definition=definition, request=request, packet=None)


def _build_runtime_evidence_insufficient_result(
    *,
    definition: RuleJudgeDefinition,
    request: RuleJudgeRequest,
    observation_packet_id: str | None,
    evidence_artifact_ids: tuple[str, ...],
    reason: str,
    packet: ObservationPacket | None = None,
    auto_recapture_block_reason: str | None = None,
) -> RuleJudgeResult:
    """规则目标存在但缺少动作或时序证据时保守转复核。"""
    # H5 异常出口保留同工作项、同目标 Capture 的可信上下文，不从模型输入补身份。
    if packet is not None:
        page = packet.packet_payload.get("page") or {}
        if not (
            _packet_surface_is_h5(packet)
            and packet.task_id == request.task_id
            and packet.rule_id == definition.rule_id
            and packet.page_capture_id == request.page_capture_id
            and page.get("page_capture_id") == (
                request.evidence_source_page_capture_id or request.page_capture_id
            )
            and (not request.evidence_source_page_capture_id or request.evidence_manifest_id)
        ):
            packet = None
    if packet is not None and observation_packet_id is None:
        observation_packet_id = packet.observation_packet_id
    result = RuleJudgeResult(
        rule_id=definition.rule_id,
        rule_code=definition.rule_code,
        executor_type=definition.executor_type,
        judge_strategy=definition.executor_type,
        execution_status="evidence_insufficient",
        confidence=0.0,
        result_summary="规则目标已发现，但执行证据不足",
        problem_desc=reason,
        page_capture_id=request.page_capture_id,
        observation_packet_id=observation_packet_id,
        evidence_artifact_ids=evidence_artifact_ids,
        judge_input=_summarize_judge_input(request.judge_input),
        judge_output={
            "schema_version": "1.0",
            "conclusion": "evidence_insufficient",
            "reasoning_summary": reason,
        },
    )
    return _attach_evidence_gap(
        result, definition=definition, request=request, packet=packet,
        auto_recapture_block_reason=auto_recapture_block_reason,
    )


def _balanced_applicability_preflight(
    *,
    definition: RuleJudgeDefinition,
    request: RuleJudgeRequest,
    packet: ObservationPacket,
    missing_capture_roles: list[str],
    verdict_mode: VerdictMode,
) -> RuleJudgeResult | None:
    """在补采前先确认目标是否存在，避免把明确无目标误报为证据不足。"""
    if verdict_mode != "balanced" or not _has_complete_applicability_surface(packet):
        return None
    engine_config = definition.judge_config.get("engine_config")
    if not isinstance(engine_config, dict):
        return None
    handler = str(engine_config.get("handler") or "")
    surface = _packet_applicability_surface(packet)
    reason: str | None = None

    if handler == "content_lifecycle_state":
        entity_pattern = engine_config.get("consumer_entity_pattern")
        if isinstance(entity_pattern, str) and not _regex_matches_surface(entity_pattern, surface):
            reason = "完整页面中未发现待核对的失效内容或活动目标"
    elif handler == "state_transition_assertion":
        if definition.rule_code in {"alioth_8", "alioth_31"}:
            action_pattern = engine_config.get("action_pattern")
            if isinstance(action_pattern, str) and not _regex_matches_surface(
                action_pattern, surface
            ):
                reason = "完整页面中未发现该规则要求操作的交互控件"
        elif definition.rule_code == "alioth_46" and not _packet_has_media_target(packet):
            reason = "完整帖子页面中未发现可进入全屏浏览的图片或视频"
    elif handler == "rate_limit_assertion":
        action_pattern = engine_config.get("action_pattern")
        if isinstance(action_pattern, str) and not _regex_matches_surface(action_pattern, surface):
            reason = "完整页面中未发现可执行连续发布探测的输入控件"

    if reason is None:
        return None
    tool_results = [
        {
            "tool_name": "balanced_applicability_preflight",
            "status": "not_applicable",
            "reason": reason,
            "missing_capture_roles": missing_capture_roles,
        }
    ]
    return _build_skipped_result(
        definition=definition,
        request=request,
        observation_packet_id=packet.observation_packet_id,
        evidence_artifact_ids=packet.evidence_artifact_ids,
        reason=reason,
        packet=packet,
        tool_results=tool_results,
    )


_PRESENTATION_MISSING_EVIDENCE = {
    PresentationRejectionCode.BASE_PAGE_OCCLUDED: "关闭遮挡后、底层页面真实可见的当前页面证据",
    PresentationRejectionCode.DIAGNOSTIC_EVIDENCE_EXCLUDED: (
        "未经隐藏弹窗或脚本派发点击、自然状态下重新采集的页面证据"
    ),
    PresentationRejectionCode.EXTERNAL_SURFACE: "回到被测业务页面后的页面证据",
}


def _target_document_preflight(
    *, definition: RuleJudgeDefinition, request: RuleJudgeRequest, packet: ObservationPacket,
) -> RuleJudgeResult | None:
    """目标错误文档和空白材料优先于场景适用性，诊断错误页的明确规则除外。"""
    if _list_interaction_source_presentation(definition=definition, request=request, packet=packet):
        # 连续列表操作的异常结果本身就是反馈材料，不能要求 after 先变成正常业务页。
        return None
    facts = packet.packet_payload.get("facts")
    facts = facts if isinstance(facts, dict) else {}
    engine = definition.judge_config.get("engine_config")
    engine = engine if isinstance(engine, dict) else {}
    if engine.get("handler") == "technical_error_text":
        return None
    reason = facts.get("target_document_unavailable_reason")
    document = facts.get("web_document")
    document = document if isinstance(document, dict) else {}
    readiness = document.get("readiness")
    readiness = readiness if isinstance(readiness, dict) else {}
    if not reason and readiness.get("status") in {
        "human_verification_blocked", "capture_failed", "identity_drift", "artifact_skew",
    }:
        reason = "当前 Web 采集未形成可用于正文质检的有效业务文档：" + str(readiness["status"])
    if not reason and facts.get("empty_observed_surface") is True:
        reason = "当前主采集为无文本、无可见业务元素的空白图，未观察到规则要求的目标业务页面"
    if reason:
        return _build_evidence_insufficient_result(
            definition=definition, request=request, packet=packet,
            reason=str(reason), missing_capture_roles=[],
        )
    return None


def _presentation_preflight(
    *,
    definition: RuleJudgeDefinition,
    request: RuleJudgeRequest,
    packet: ObservationPacket,
) -> RuleJudgeResult | None:
    """展示层门禁：底层页面被遮挡或材料来自诊断操作时，不交给判定器当作该页面的证据。

    对应专项七 §5.2/§5.5：拒绝只发生在有正向依据时（最上层确实是遮挡物、确有诊断操作），
    缺少展示层评估的历史采集不受影响；广告类等以浮层为目标的规则不因遮挡被拒。
    """
    unavailable_result = _target_document_preflight(
        definition=definition, request=request, packet=packet,
    )
    if unavailable_result is not None:
        return unavailable_result
    if request.original_page_input_set_id is not None:
        original = packet.packet_payload.get("identity", {}).get("original_page_input")
        if not isinstance(original, dict) or any(original.get(key) != value for key, value in {
            "input_set_id": request.original_page_input_set_id,
            "rule_contract_hash": request.expected_contract_hash,
            "work_item_id": request.work_item_id, "attempt_id": request.attempt_id,
            "consumer_agent_run_id": request.agent_run_id,
        }.items()):
            return _build_evidence_insufficient_result(
                definition=definition, request=request, packet=packet,
                reason="H5 页面原输入没有当前消费授权及精确证据闭包", missing_capture_roles=[],
            )
    if request.evidence_input_set_id is not None:
        semantics = packet.packet_payload.get("web_evidence")
        if (
            not isinstance(semantics, dict)
            or semantics.get("status") != "admissible"
            or semantics.get("input_set_id") != request.evidence_input_set_id
            or semantics.get("rule_contract_hash") != request.expected_contract_hash
        ):
            return _build_evidence_insufficient_result(
                definition=definition,
                request=request,
                packet=packet,
                reason="Web 序列未通过独立语义及绑定校验",
                missing_capture_roles=[],
            )
        # 专属读端口已复核每帧自然来源；失败序列不冒充稳定底页曝光。
        return None
    facts = packet.packet_payload.get("facts")
    facts = facts if isinstance(facts, dict) else {}
    presentation = _list_interaction_source_presentation(
        definition=definition, request=request, packet=packet,
    ) or PresentationAssessment.from_dict(facts.get("presentation"))
    if presentation is None:
        return None
    return _presentation_rejection(
        definition=definition,
        request=request,
        presentation=presentation,
        packet=packet,
        observation_packet_id=packet.observation_packet_id,
        evidence_artifact_ids=packet.evidence_artifact_ids,
    )


def _list_interaction_source_presentation(*, definition, request, packet):
    """只用正式冻结的同动作列表来源；普通规则和模型自报来源不能改变展示层门禁。"""
    from services.api.app.application.app_rule_action_readiness import app_rule_action_requirement
    from services.api.app.domain.evidence.presentation import EvidenceLayerTarget

    payload = packet.packet_payload
    page = payload.get("page", {})
    facts = payload.get("facts", {})
    proof = facts.get("app_list_interaction_source", {})
    if (
        app_rule_action_requirement({"judge_config": definition.judge_config})
        != "list_scroll_interaction"
        or page.get("surface_kind") not in {"android", "ios"}
        or packet.quality_gate_status != "pass"
        or proof.get("source") != "verified_app_action_facts"
        or proof.get("rule_id") != definition.rule_id
        or proof.get("rule_code") != definition.rule_code
        or proof.get("task_id") != request.task_id
        or proof.get("agent_run_id") != request.source_agent_run_id
        or proof.get("primary_capture_id") != request.page_capture_id
    ):
        return None
    bindings = payload.get("identity", {}).get("capture_roles", {})
    roles = facts.get("capture_roles", {})
    requested = request.evidence_capture_roles or {}
    png_ids = {
        artifact_id for artifact_id, image, content_type in packet.image_payloads
        if content_type == "image/png" and image.startswith(b"\x89PNG\r\n\x1a\n")
    }
    role_images = []
    for role, capture_id in (
        ("before", proof.get("source_capture_id")), ("primary", request.page_capture_id)
    ):
        role_fact = roles.get(role, {})
        if not capture_id or not (
            requested.get(role) == bindings.get(role)
            == role_fact.get("page_capture_id") == capture_id
        ):
            return None
        images = png_ids.intersection(role_fact.get("artifact_ids") or [])
        if not images:
            return None
        role_images.append(images)
    if not role_images[0].isdisjoint(role_images[1]):
        return None
    after = PresentationAssessment.from_dict(facts.get("presentation"))
    if after is not None and not evaluate_presentation_for_rule(
        after, layer_target=EvidenceLayerTarget.ANY,
        accepts_diagnostic=rule_accepts_diagnostic_evidence(definition.judge_config),
    ).accepted:
        return None
    source = PresentationAssessment.from_dict(proof.get("presentation"))
    return source if source is not None and not source.occluding_layer_present else None


def _bundle_presentation_for(
    evidence_bundle: Any,
    page_capture_id: str,
) -> PresentationAssessment | None:
    """可信证据包带回的主采集展示层评估；旧接线或没有持久化评估时为 None。"""
    presentations = getattr(evidence_bundle, "capture_presentations", None)
    if not isinstance(presentations, Mapping):
        return None
    value = presentations.get(page_capture_id)
    return value if isinstance(value, PresentationAssessment) else None


def _presentation_rejection(
    *,
    definition: RuleJudgeDefinition,
    request: RuleJudgeRequest,
    presentation: PresentationAssessment,
    packet: ObservationPacket | None,
    observation_packet_id: str | None,
    evidence_artifact_ids: tuple[str, ...],
) -> RuleJudgeResult | None:
    """按规则目标层评估展示层；被拒时给出带缺口的「证据不足」，接受时返回 None。"""
    verdict = evaluate_presentation_for_rule(
        presentation,
        layer_target=rule_evidence_layer_target(
            category=definition.category,
            judge_config=definition.judge_config,
            check_point=definition.check_point,
            rule_name=definition.rule_name,
        ),
        accepts_diagnostic=rule_accepts_diagnostic_evidence(definition.judge_config),
    )
    if verdict.accepted:
        return None
    reason = verdict.reason or "当前材料不能作为该规则要求层级的证据"
    tool_results = [
        {
            "tool_name": "presentation_preflight",
            "status": "rejected",
            **verdict.to_dict(),
        }
    ]
    missing = _PRESENTATION_MISSING_EVIDENCE.get(verdict.reason_code, reason)
    result = RuleJudgeResult(
        rule_id=definition.rule_id,
        rule_code=definition.rule_code,
        executor_type=definition.executor_type,
        judge_strategy=definition.executor_type,
        execution_status="evidence_insufficient",
        confidence=0.0,
        result_summary="当前材料不能作为该页面的可见证据，未执行结论推断",
        problem_desc=reason,
        page_capture_id=request.page_capture_id,
        observation_packet_id=observation_packet_id,
        evidence_artifact_ids=evidence_artifact_ids,
        judge_input=(
            _observation_judge_input(packet, tool_results)
            if packet is not None
            else _summarize_judge_input(request.judge_input)
        ),
        judge_output={
            "schema_version": "1.0",
            "conclusion": "evidence_insufficient",
            "reasoning_summary": reason,
            "missing_evidence": [missing],
            "presentation_rejection": verdict.to_dict(),
            "evidence_trusted": False,
            "tool_results": tool_results,
        },
    )
    return _attach_evidence_gap(result, definition=definition, request=request, packet=packet)


# 平台明确不做的采样方式（2026-09-21 立川拍板）：规则合同声明依赖它们时直接记不适用，
# 不再逐页交模型判出一串「证据不足」。以后平台支持了，从这里拿掉即可。
_UNSUPPORTED_SAMPLING_REASONS = {
    "multi_browser": "平台只用一种浏览器采集，不做多浏览器采样，这条规则不适用",
    "cross_platform": "一个任务只跑一个端，平台不做跨端配对采样，这条规则不适用",
}


def _unsupported_sampling_preflight(
    *,
    definition: RuleJudgeDefinition,
    request: RuleJudgeRequest,
    packet: ObservationPacket | None = None,
) -> RuleJudgeResult | None:
    """规则合同声明的采样方式平台不做时，记不适用。"""
    capability = definition.judge_config.get("capability_config") or {}
    if (
        packet is not None and _packet_surface_is_h5(packet)
        and isinstance(capability, dict)
        and capability.get("batch_group") == "multi-client-pending"
    ):
        result = _build_runtime_evidence_insufficient_result(
            definition=definition, request=request,
            observation_packet_id=packet.observation_packet_id,
            evidence_artifact_ids=packet.evidence_artifact_ids,
            reason="跨端配对采样能力尚未实现，单端页面不能证明多端一致性，保留待复核缺口。",
            packet=packet,
        )
        return replace(result, judge_output={
            **result.judge_output, "reason_code": "H5_SAMPLING_CAPABILITY_UNAVAILABLE",
        })
    sampling = str(definition.input_contract.get("required_sampling") or "").strip()
    app_cross_platform = (
        packet is not None
        and _packet_surface_is_app(packet)
        and (
            sampling == "cross_platform"
            or (
                isinstance(capability, dict)
                and capability.get("batch_group") == "multi-client-pending"
            )
        )
    )
    if app_cross_platform:
        reason = "单任务只能采一个端，尚不支持同版本的跨端配对采样。"
        result = _build_runtime_evidence_insufficient_result(
            definition=definition,
            request=request,
            observation_packet_id=packet.observation_packet_id,
            evidence_artifact_ids=packet.evidence_artifact_ids,
            reason=reason,
            packet=packet,
        )
        return replace(
            result,
            judge_output={
                **result.judge_output,
                "gap_codes": ["cross_platform_pair_unsupported"],
                "reason_code": "APP_CROSS_PLATFORM_SAMPLING_UNSUPPORTED",
            },
        )
    reason = _UNSUPPORTED_SAMPLING_REASONS.get(sampling)
    if reason is None:
        return None
    return _build_skipped_result(
        definition=definition,
        request=request,
        observation_packet_id=packet.observation_packet_id if packet is not None else None,
        evidence_artifact_ids=(
            packet.evidence_artifact_ids if packet is not None else request.evidence_artifact_ids
        ),
        reason=reason,
        packet=packet,
        tool_results=[
            {
                "tool_name": "unsupported_sampling_preflight",
                "status": "not_applicable",
                "required_sampling": sampling,
                "reason": reason,
            }
        ],
    )


def _scenario_trigger_preflight(
    *,
    definition: RuleJudgeDefinition,
    request: RuleJudgeRequest,
    packet: ObservationPacket | None = None,
) -> RuleJudgeResult | None:
    """合同声明了「遇到才产出」的采集角色（如 40 的下架来源页 source）时，任务里没有这个角色
    说明探索没遇到该场景，记不适用，不记证据不足。"""
    trigger_roles = [
        str(item).strip()
        for item in definition.input_contract.get("scenario_trigger_roles", [])
        if str(item).strip()
    ]
    provided = set(request.evidence_capture_roles or {})
    missing = [role for role in trigger_roles if role not in provided]
    if not missing:
        return None
    reason = "探索过程中没有遇到这条规则要核对的场景，这条规则不适用"
    return _build_skipped_result(
        definition=definition,
        request=request,
        observation_packet_id=packet.observation_packet_id if packet is not None else None,
        evidence_artifact_ids=(
            packet.evidence_artifact_ids if packet is not None else request.evidence_artifact_ids
        ),
        reason=reason,
        packet=packet,
        tool_results=[
            {
                "tool_name": "scenario_trigger_preflight",
                "status": "not_applicable",
                "missing_trigger_roles": missing,
                "reason": reason,
            }
        ],
    )


def _declared_auth_requirement(definition: RuleJudgeDefinition) -> str | None:
    requirement = str(definition.input_contract.get("auth_requirement") or "").strip()
    if requirement in {AUTH_REQUIREMENT_AUTHENTICATED, AUTH_REQUIREMENT_GUEST}:
        return requirement
    return None


def _session_auth_preflight(
    *,
    definition: RuleJudgeDefinition,
    request: RuleJudgeRequest,
    packet: ObservationPacket,
) -> RuleJudgeResult | None:
    """采集侧在证据清单里声明了登录态时直接读：登录态不满足规则要求就记不适用。

    没声明或声明 unknown 时不动作，仍由任务级登录闸门（rule_auth_gate）兜底。
    """
    requirement = _declared_auth_requirement(definition)
    if requirement is None:
        return None
    page = packet.packet_payload.get("page")
    session_auth = page.get(DECLARED_SESSION_AUTH_KEY) if isinstance(page, dict) else None
    if session_auth not in {"guest", "authenticated"} or session_auth == requirement:
        return None
    reason = (
        "这条规则要登录后才能判，本次采集时是未登录状态"
        if requirement == AUTH_REQUIREMENT_AUTHENTICATED
        else "这条规则只在未登录状态下判，本次采集时已登录"
    )
    return _build_skipped_result(
        definition=definition,
        request=request,
        observation_packet_id=packet.observation_packet_id,
        evidence_artifact_ids=packet.evidence_artifact_ids,
        reason=reason,
        packet=packet,
        tool_results=[
            {
                "tool_name": "session_auth_preflight",
                "status": "not_applicable",
                "auth_requirement": requirement,
                "session_auth": session_auth,
                "reason": reason,
            }
        ],
        reason_code=AUTH_REQUIREMENT_NOT_MET_REASON,
    )


def _required_page_states(definition: RuleJudgeDefinition) -> set[str]:
    engine_config = definition.judge_config.get("engine_config")
    if not isinstance(engine_config, dict):
        return set()
    configured_states = engine_config.get("required_page_states")
    if not isinstance(configured_states, list):
        return set()
    return {str(state).strip().casefold() for state in configured_states if str(state).strip()}


def _required_page_state_preflight(
    *,
    definition: RuleJudgeDefinition,
    request: RuleJudgeRequest,
    packet: ObservationPacket,
    required_page_states: set[str],
) -> RuleJudgeResult | None:
    """规则已声明页面状态时，非目标状态直接判定不适用。"""
    page = packet.packet_payload.get("page")
    page = page if isinstance(page, dict) else {}
    observed_page_state = str(page.get("page_state") or "").strip().casefold()
    before_role = required_state_before_role(
        judge_config=definition.judge_config,
        input_contract=definition.input_contract or {},
    )
    if before_role is not None:
        facts = packet.packet_payload.get("facts")
        roles = facts.get("capture_roles") if isinstance(facts, dict) else None
        before = roles.get(before_role) if isinstance(roles, dict) else None
        declared_before = (request.evidence_capture_roles or {}).get(before_role)
        # 只读 ObservationPacket 核验过的角色材料；缺前帧时交后续证据门禁，禁止判成 NA。
        if not isinstance(before, dict) or before.get("page_capture_id") != declared_before:
            return None
        if before.get("quality_status") not in {None, "pass"}:
            return None
        observed_page_state = str(before.get("page_state") or "").strip().casefold()
    if not observed_page_state or observed_page_state in required_page_states:
        return None
    return _build_skipped_result(
        definition=definition,
        request=request,
        observation_packet_id=packet.observation_packet_id,
        evidence_artifact_ids=packet.evidence_artifact_ids,
        reason=(f"当前页面状态为 {observed_page_state}，未检测到规则要求的弹窗蒙层状态"),
        packet=packet,
        tool_results=[
            {
                "tool_name": "required_page_state_preflight",
                "status": "not_applicable",
                "observed_page_state": observed_page_state,
                "required_page_states": sorted(required_page_states),
            }
        ],
    )


def _has_complete_applicability_surface(packet: ObservationPacket) -> bool:
    """只在文本未截断且 DOM/UI 至少一项完整时，允许据缺席判定不适用。"""
    facts = packet.packet_payload.get("facts")
    if not isinstance(facts, dict) or facts.get("visible_text_truncated") is not False:
        return False
    page = packet.packet_payload.get("page")
    page = page if isinstance(page, dict) else {}
    if str(page.get("page_state") or "").strip().casefold() in {
        "loading",
        "line_loading",
        "transitioning",
        "needs_more_observation",
        "detection_error",
    }:
        return False

    ui_snapshot = facts.get("ui_snapshot")
    ui_complete = False
    if isinstance(ui_snapshot, dict) and ui_snapshot.get("sequence") is not True:
        elements = ui_snapshot.get("elements")
        element_count = ui_snapshot.get("element_count")
        ui_complete = (
            isinstance(elements, list)
            and isinstance(element_count, int)
            and not isinstance(element_count, bool)
            and element_count <= len(elements)
            and ui_snapshot.get("element_limit_reached") is not True
            and not ui_snapshot.get("truncated_reason")
            and ui_snapshot.get("budget_truncated") is not True
        )

    dom_summary = facts.get("dom_summary")
    dom_complete = False
    if isinstance(dom_summary, dict):
        dom_complete = (
            isinstance(dom_summary.get("elements"), list)
            and isinstance(dom_summary.get("character_count"), int)
            and dom_summary["character_count"] > 0
            and dom_summary.get("parse_error") is not True
            and dom_summary.get("element_limit_reached") is not True
            and dom_summary.get("budget_truncated") is not True
        )
    return ui_complete or dom_complete


def _packet_applicability_surface(packet: ObservationPacket) -> str:
    facts = packet.packet_payload.get("facts")
    facts = facts if isinstance(facts, dict) else {}
    page = packet.packet_payload.get("page")
    page = page if isinstance(page, dict) else {}
    return "\n".join(
        (
            str(facts.get("visible_text") or ""),
            json.dumps(facts.get("ui_snapshot") or {}, ensure_ascii=False, default=str),
            json.dumps(facts.get("dom_summary") or {}, ensure_ascii=False, default=str),
            str(page.get("title") or ""),
        )
    )


def _regex_matches_surface(pattern: str, surface: str) -> bool:
    try:
        return re.search(pattern, surface) is not None
    except re.error:
        return False


def _packet_has_media_target(packet: ObservationPacket) -> bool:
    facts = packet.packet_payload.get("facts")
    facts = facts if isinstance(facts, dict) else {}
    for source_name in ("ui_snapshot", "dom_summary"):
        source = facts.get(source_name)
        if not isinstance(source, dict):
            continue
        elements = source.get("elements")
        if not isinstance(elements, list):
            continue
        for item in elements:
            if not isinstance(item, dict):
                continue
            tag = str(item.get("tag") or "").strip().casefold()
            role = str(item.get("role") or "").strip().casefold()
            if tag in {"img", "picture", "video"} or role == "img" or item.get("image") is True:
                return True
            identity = " ".join(
                str(item.get(key) or "")
                for key in ("text", "aria_label", "alt", "class_tokens", "src", "type")
            )
            if re.search(
                r"图片|视频|全屏|image|video|media|fullscreen|thumbnail",
                identity,
                re.IGNORECASE,
            ):
                return True
    return False


def _missing_required_capture_roles(
    *,
    input_contract: dict[str, Any],
    evidence_capture_roles: dict[str, str] | None,
) -> list[str]:
    required = {
        str(item).strip()
        for item in input_contract.get("required_capture_roles", [])
        if str(item).strip()
    }
    provided = set(evidence_capture_roles or {"primary": ""})
    return sorted(required - provided)


def _scoped_evidence_capture_roles(
    *,
    input_contract: dict[str, Any],
    evidence_capture_roles: dict[str, str] | None,
) -> dict[str, str] | None:
    """按规则合同裁剪额外采集角色，避免单页规则被操作前轻量采集误伤。"""
    if evidence_capture_roles is None:
        return None
    required = {
        str(item).strip()
        for item in input_contract.get("required_capture_roles", [])
        if str(item).strip()
    }
    if not required:
        return evidence_capture_roles
    required.add("primary")
    return {
        role: capture_id for role, capture_id in evidence_capture_roles.items() if role in required
    }


def _has_external_link_target(ui_snapshot: Any) -> bool:
    if not isinstance(ui_snapshot, dict):
        return False
    elements = ui_snapshot.get("elements")
    if not isinstance(elements, list):
        return False
    return any(
        isinstance(item, dict) and item.get("external") is True and bool(item.get("href"))
        for item in elements
    )


def _trusted_bundle_artifact_ids(
    evidence_bundle: Any,
    *,
    fallback: tuple[str, ...],
) -> tuple[str, ...]:
    artifacts = getattr(evidence_bundle, "artifacts", ())
    resolved = tuple(
        str(artifact_id).strip()
        for item in artifacts
        if (artifact_id := getattr(item, "artifact_id", None)) is not None
        and str(artifact_id).strip()
    )
    return resolved or fallback


RULE_ENGINE_HANDLER_REGISTRY.update(
    {
        "technical_error_text": RuleEngineHandlerSpec(
            handler_tool_name="deterministic_assertion_tool",
            judge_input_source="trusted_evidence",
            required_artifact_types=("visible_text",),
            executor=_execute_technical_error_text,
            validate_config=_validate_technical_error_config,
        ),
        "discount_price_compare": RuleEngineHandlerSpec(
            handler_tool_name="deterministic_assertion_tool",
            judge_input_source="agent_structured",
            required_artifact_types=("visible_text",),
            executor=_execute_discount_price_compare,
            validate_config=_validate_discount_price_config,
        ),
        "visible_text_presence": RuleEngineHandlerSpec(
            handler_tool_name="deterministic_assertion_tool",
            judge_input_source="trusted_evidence",
            required_artifact_types=("visible_text",),
            executor=_execute_visible_text_presence,
            validate_config=validate_visible_text_presence_config,
        ),
        "screenshot_change_assertion": RuleEngineHandlerSpec(
            handler_tool_name="deterministic_assertion_tool",
            judge_input_source="trusted_evidence",
            required_artifact_types=("screenshot",),
            executor=_execute_screenshot_change_assertion,
            validate_config=validate_screenshot_change_config,
        ),
        "qr_url_validation": RuleEngineHandlerSpec(
            handler_tool_name="qr_link_validation_tool",
            judge_input_source="trusted_and_agent_structured",
            # 执行器按「可信文字里的网址 → UI 快照里的二维码元素 → 截图解码」依次找目标，
            # 三者任一都能判；写成全要会把只有截图或只有快照的证据挡在处理器之前。
            required_artifact_types=(),
            executor=_execute_qr_url_validation,
            validate_config=_validate_qr_url_config,
        ),
        "collection_constraint": RuleEngineHandlerSpec(
            handler_tool_name="deterministic_assertion_tool",
            judge_input_source="agent_structured",
            required_artifact_types=("visible_text",),
            executor=_execute_collection_constraint,
            validate_config=validate_collection_constraint_config,
        ),
        "required_ui_capability": RuleEngineHandlerSpec(
            handler_tool_name="deterministic_assertion_tool",
            judge_input_source="trusted_and_agent_structured",
            # 只把「上传限制是否展示」当必需证据来源（页面文字/截图读字即可）；
            # Action Trace 仅供可选的禁止动作检查，缺失时跳过该项，不再挡住展示型判定。
            required_artifact_types=("visible_text",),
            executor=_execute_required_ui_capability,
            validate_config=validate_required_ui_capability_config,
        ),
        "temporal_sequence_assertion": RuleEngineHandlerSpec(
            handler_tool_name="deterministic_assertion_tool",
            judge_input_source="trusted_evidence",
            required_artifact_types=("temporal_trace",),
            executor=_execute_temporal_sequence_assertion,
            validate_config=validate_temporal_sequence_config,
        ),
        "geometry_hit_area_assertion": RuleEngineHandlerSpec(
            handler_tool_name="deterministic_assertion_tool",
            judge_input_source="trusted_and_agent_structured",
            required_artifact_types=("ui_snapshot",),
            executor=_execute_geometry_hit_area_assertion,
            validate_config=validate_geometry_hit_area_config,
        ),
        "external_link_open_mode": RuleEngineHandlerSpec(
            handler_tool_name="deterministic_assertion_tool",
            judge_input_source="trusted_evidence",
            required_artifact_types=("action_trace", "ui_snapshot"),
            executor=_execute_external_link_open_mode,
            validate_config=validate_external_link_open_mode_config,
        ),
        "state_transition_assertion": RuleEngineHandlerSpec(
            handler_tool_name="deterministic_assertion_tool",
            judge_input_source="trusted_evidence",
            required_artifact_types=("visible_text", "action_trace", "ui_snapshot"),
            executor=_execute_state_transition_assertion,
            validate_config=validate_state_transition_config,
        ),
        "price_relation": RuleEngineHandlerSpec(
            handler_tool_name="deterministic_assertion_tool",
            judge_input_source="trusted_evidence",
            required_artifact_types=("visible_text",),
            executor=_execute_price_relation,
            validate_config=validate_price_relation_config,
        ),
        "rate_limit_assertion": RuleEngineHandlerSpec(
            handler_tool_name="deterministic_assertion_tool",
            judge_input_source="trusted_evidence",
            required_artifact_types=("visible_text", "action_trace", "har"),
            # App 可仅有 network_trace；执行器会投影补 har，判定合同仍以 har 为准
            executor=_execute_rate_limit_assertion,
            validate_config=validate_rate_limit_config,
            # App 连 network_trace 也没有时改看限频提示（2026-09-21），H5 / Web 仍硬性要 har
            app_optional_artifact_types=("har",),
        ),
        "content_lifecycle_state": RuleEngineHandlerSpec(
            handler_tool_name="deterministic_assertion_tool",
            judge_input_source="trusted_evidence",
            required_artifact_types=("visible_text",),
            executor=_execute_content_lifecycle_state,
            validate_config=validate_content_lifecycle_config,
        ),
        "external_navigation_handler": RuleEngineHandlerSpec(
            handler_tool_name="safe_click_observer_tool",
            judge_input_source="trusted_observation_packet",
            required_artifact_types=("screenshot", "ui_snapshot"),
            executor=None,
            validate_config=_validate_packet_capability_config,
        ),
        "data_refresh_handler": RuleEngineHandlerSpec(
            handler_tool_name="state_snapshot_tool",
            judge_input_source="trusted_observation_packet",
            required_artifact_types=("screenshot", "ui_snapshot"),
            executor=None,
            validate_config=_validate_packet_capability_config,
        ),
        "countdown_accuracy_handler": RuleEngineHandlerSpec(
            handler_tool_name="monotonic_timer_tool",
            judge_input_source="trusted_observation_packet",
            required_artifact_types=("screenshot", "ui_snapshot", "dom"),
            executor=None,
            validate_config=_validate_packet_capability_config,
        ),
        "clickable_area_handler": RuleEngineHandlerSpec(
            handler_tool_name="safe_hit_test_tool",
            judge_input_source="trusted_observation_packet",
            required_artifact_types=("screenshot", "ui_snapshot", "dom"),
            executor=None,
            validate_config=_validate_packet_capability_config,
        ),
        "popup_close_behavior_handler": RuleEngineHandlerSpec(
            handler_tool_name="safe_click_observer_tool",
            judge_input_source="trusted_observation_packet",
            required_artifact_types=("screenshot", "ui_snapshot", "dom"),
            executor=None,
            validate_config=_validate_packet_capability_config,
        ),
        "overlay_dismissal_handler": RuleEngineHandlerSpec(
            handler_tool_name="safe_click_observer_tool",
            judge_input_source="trusted_observation_packet",
            required_artifact_types=("screenshot", "ui_snapshot", "dom"),
            executor=None,
            validate_config=_validate_packet_capability_config,
        ),
        "popup_sequence_handler": RuleEngineHandlerSpec(
            handler_tool_name="monotonic_timer_tool",
            judge_input_source="trusted_observation_packet",
            required_artifact_types=("screenshot", "ui_snapshot", "dom"),
            executor=None,
            validate_config=_validate_packet_capability_config,
        ),
        "ad_first_frame_latency_handler": RuleEngineHandlerSpec(
            handler_tool_name="performance_timeline_tool",
            judge_input_source="trusted_observation_packet",
            required_artifact_types=("screenshot", "ui_snapshot", "dom"),
            executor=None,
            validate_config=_validate_packet_capability_config,
        ),
        "visible_ad_presence_handler": RuleEngineHandlerSpec(
            handler_tool_name="ad_region_detector_tool",
            judge_input_source="trusted_observation_packet",
            required_artifact_types=(),
            executor=None,
            validate_config=_validate_packet_capability_config,
        ),
        "page_url_status_handler": RuleEngineHandlerSpec(
            handler_tool_name="url_reachability_tool",
            judge_input_source="trusted_observation_packet",
            required_artifact_types=(),
            executor=None,
            validate_config=_validate_packet_capability_config,
        ),
        "startup_ad_home_transition_handler": RuleEngineHandlerSpec(
            handler_tool_name="state_snapshot_tool",
            judge_input_source="trusted_observation_packet",
            required_artifact_types=(),
            executor=None,
            validate_config=_validate_packet_capability_config,
        ),
        "ad_failure_media_fallback_handler": RuleEngineHandlerSpec(
            handler_tool_name="player_state_probe_tool",
            judge_input_source="trusted_observation_packet",
            required_artifact_types=("screenshot", "ui_snapshot", "dom"),
            executor=None,
            validate_config=_validate_packet_capability_config,
        ),
        "spam_frequency_handler": RuleEngineHandlerSpec(
            handler_tool_name="rate_limit_observer_tool",
            judge_input_source="trusted_observation_packet",
            required_artifact_types=(
                "screenshot",
                "ui_snapshot",
                "dom",
                "visible_text",
            ),
            executor=None,
            validate_config=_validate_packet_capability_config,
        ),
        "comment_state_sync_handler": RuleEngineHandlerSpec(
            handler_tool_name="state_snapshot_tool",
            judge_input_source="trusted_observation_packet",
            required_artifact_types=(
                "screenshot",
                "ui_snapshot",
                "dom",
                "visible_text",
            ),
            executor=None,
            validate_config=_validate_packet_capability_config,
        ),
        "label_case_consistency_handler": RuleEngineHandlerSpec(
            handler_tool_name="label_group_extractor_tool",
            judge_input_source="trusted_observation_packet",
            required_artifact_types=(
                "screenshot",
                "ui_snapshot",
                "dom",
                "visible_text",
            ),
            executor=None,
            validate_config=_validate_packet_capability_config,
        ),
        "offline_content_visibility_handler": RuleEngineHandlerSpec(
            handler_tool_name="content_item_state_extractor_tool",
            judge_input_source="trusted_observation_packet",
            required_artifact_types=(
                "screenshot",
                "ui_snapshot",
                "dom",
                "visible_text",
            ),
            executor=None,
            validate_config=_validate_packet_capability_config,
        ),
        "playback_resume_handler": RuleEngineHandlerSpec(
            handler_tool_name="player_state_probe_tool",
            judge_input_source="trusted_observation_packet",
            required_artifact_types=("screenshot", "ui_snapshot"),
            executor=None,
            validate_config=_validate_packet_capability_config,
        ),
        "upload_rule_visibility_handler": RuleEngineHandlerSpec(
            handler_tool_name="upload_contract_extractor_tool",
            judge_input_source="trusted_observation_packet",
            required_artifact_types=(
                "screenshot",
                "ui_snapshot",
                "dom",
                "visible_text",
            ),
            executor=None,
            validate_config=_validate_packet_capability_config,
        ),
        "media_fullscreen_handler": RuleEngineHandlerSpec(
            handler_tool_name="safe_click_observer_tool",
            judge_input_source="trusted_observation_packet",
            required_artifact_types=("screenshot", "ui_snapshot"),
            executor=None,
            validate_config=_validate_packet_capability_config,
        ),
        "logout_entry_presence_handler": RuleEngineHandlerSpec(
            handler_tool_name="account_control_extractor_tool",
            judge_input_source="trusted_observation_packet",
            required_artifact_types=("visible_text",),
            executor=None,
            validate_config=_validate_packet_capability_config,
        ),
    }
)
