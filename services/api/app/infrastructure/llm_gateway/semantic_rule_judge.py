"""基于 Agent Framework 结构化响应的独立规则 Judge。"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from agent_framework import Agent, Content, Message
from agent_framework.exceptions import ChatClientException
from openai import OpenAIError
from pydantic import ValidationError

from services.api.app.agents.rule_judge.instructions import (
    build_rule_judge_system_instructions,
)
from services.api.app.application.observation_packet import (
    ObservationPacket,
    _limit_packet_ui_snapshot,
)
from services.api.app.application.semantic_rule_judge import (
    SemanticJudgeBatchOutput,
    SemanticJudgeModelOutput,
)
from services.api.app.core.config import get_settings
from services.api.app.infrastructure.llm_gateway.client_factory import build_chat_client

MAX_EVIDENCE_REFERENCE_CORRECTIONS = 2
DEFAULT_SEMANTIC_REQUEST_TIMEOUT_SECONDS = 60.0
DEFAULT_SEMANTIC_PROVIDER_CIRCUIT_SECONDS = 120.0
# 超时不等于线路故障：单次慢响应只影响当次请求，同一任务连续这么多次超时才熔断。
# 实测一次 60 秒超时曾让同任务之后 120 秒内的规则全部直接转人工。
SEMANTIC_TIMEOUTS_BEFORE_CIRCUIT = 2
SEMANTIC_RESULT_CACHE_MAX_ENTRIES = 256


@dataclass(frozen=True)
class _ProviderCircuitState:
    """单个任务的语义模型线路熔断状态。"""

    unavailable_until: float
    failure_code: str
    failure_message: str


_ADULT_CONTENT_MARKERS = (
    "adult",
    "erotic",
    "porn",
    "sexual",
    "成人",
    "色情",
    "情色",
    "淫秽",
    "裸露",
)
_OUT_OF_SCOPE_EXPANSION_MARKERS = (
    "broader input risk",
    "broader input_risk",
    "general content safety",
    "overall content risk",
    "overall input risk",
    "overall input_risk",
    "prohibited/illegal content",
    "其他红线",
    "整体 input risk",
    "整体 input_risk",
    "整体内容风险",
    "通用内容安全",
)
_DECLARED_SCOPE_ABSENCE_MARKERS = (
    "although the current rule",
    "although the rule",
    "although this rule",
    "no evidence of political",
    "no references to political",
    "尽管当前规则",
    "尽管该规则",
    "虽然本规则",
    "未发现政治",
    "没有政治",
)


class LLMSemanticRuleJudge:
    """不持有写库 Tool 的专职 Judge，只返回严格结构化判定。"""

    def __init__(
        self,
        *,
        client: Any | None = None,
        agent: Agent | None = None,
        batch_agent: Agent | None = None,
        request_timeout_seconds: float = DEFAULT_SEMANTIC_REQUEST_TIMEOUT_SECONDS,
        circuit_seconds: float = DEFAULT_SEMANTIC_PROVIDER_CIRCUIT_SECONDS,
        manual_test: bool = False,
    ) -> None:
        if request_timeout_seconds <= 0:
            raise ValueError("语义 Judge 请求超时必须为正数")
        if circuit_seconds <= 0:
            raise ValueError("语义 Judge 熔断窗口必须为正数")
        if client is not None and (agent is not None or batch_agent is not None):
            raise ValueError("测试 Client 与 MAF Agent 不能同时注入")
        self._client = client
        self._agent = agent
        self._batch_agent = batch_agent
        self._request_timeout_seconds = request_timeout_seconds
        self._circuit_seconds = circuit_seconds
        # 规则测试页（人工挑证据）专用；正式任务的判定实例永远是 False
        self._manual_test = manual_test
        self._provider_circuits: dict[str, _ProviderCircuitState] = {}
        self._consecutive_timeouts: dict[str, int] = {}
        self._result_cache: dict[
            tuple[str, str, str, str],
            SemanticJudgeModelOutput,
        ] = {}
        self._inflight_results: dict[
            tuple[str, str, str, str],
            asyncio.Task[SemanticJudgeModelOutput],
        ] = {}

    async def judge(
        self,
        *,
        packet: ObservationPacket,
        tool_results: list[dict[str, Any]],
    ) -> SemanticJudgeModelOutput:
        contract_gap = _incomplete_rule_contract_output(packet)
        if contract_gap is not None:
            return contract_gap
        cache_key = _semantic_result_cache_key(packet, tool_results)
        cached = self._result_cache.get(cache_key)
        if cached is not None:
            return cached.model_copy(deep=True)
        inflight = self._inflight_results.get(cache_key)
        if inflight is not None:
            return (await asyncio.shield(inflight)).model_copy(deep=True)
        task = asyncio.create_task(
            self._judge_uncached(
                packet=packet,
                tool_results=tool_results,
                cache_key=cache_key,
            ),
            name=f"alioth-semantic-judge-{packet.rule_code}",
        )
        self._inflight_results[cache_key] = task
        try:
            return await task
        finally:
            if self._inflight_results.get(cache_key) is task:
                self._inflight_results.pop(cache_key, None)

    async def _judge_uncached(
        self,
        *,
        packet: ObservationPacket,
        tool_results: list[dict[str, Any]],
        cache_key: tuple[str, str, str, str],
    ) -> SemanticJudgeModelOutput:
        """执行一次真实判定；相同输入的并发调用由公开入口合并。"""
        timeout_scope = _timeout_scope([packet])
        for scope in dict.fromkeys((packet.task_id, timeout_scope)):
            if self._provider_circuit_is_open(scope):
                return self._provider_unavailable_output(scope)
        prepared_packet = _fit_packet_to_prompt_budget(packet, tool_results)
        if prepared_packet is None:
            return _prompt_budget_insufficient_output(packet)
        messages = _build_messages(
            packet=prepared_packet,
            tool_results=tool_results,
            manual_test=self._manual_test,
        )
        try:
            async with asyncio.timeout(self._request_timeout_seconds):
                for correction_index in range(MAX_EVIDENCE_REFERENCE_CORRECTIONS + 1):
                    try:
                        response = await self._request_model(
                            messages=messages,
                            response_format=SemanticJudgeModelOutput,
                            batch=False,
                        )
                        output = _parse_model_output(response.value)
                    except ValidationError as exc:
                        raw_input = _validation_error_input_from_exception(exc)
                        if raw_input is None:
                            raise
                        output = _parse_model_output(raw_input)
                    except (OpenAIError, ChatClientException) as exc:
                        raw_input = _validation_error_input_from_exception(exc)
                        if raw_input is None:
                            raise
                        output = _parse_model_output(raw_input)
                    except Exception as exc:
                        raw_input = _validation_error_input_from_exception(exc)
                        if raw_input is None:
                            raise
                        output = _parse_model_output(raw_input)
                    invalid_refs = _invalid_evidence_refs(
                        output=output,
                        packet=prepared_packet,
                    )
                    scope_expanded = _is_out_of_scope_adult_content_failure(
                        output=output,
                        packet=prepared_packet,
                    )
                    if not invalid_refs and not scope_expanded:
                        self._consecutive_timeouts.pop(timeout_scope, None)
                        return self._remember_result(cache_key, output)
                    if correction_index >= MAX_EVIDENCE_REFERENCE_CORRECTIONS:
                        if scope_expanded:
                            return self._remember_result(
                                cache_key,
                                _discard_out_of_scope_adult_content_failure(output=output),
                            )
                        return self._remember_result(
                            cache_key,
                            _quarantine_invalid_evidence_refs(
                                output=output,
                                packet=prepared_packet,
                                invalid_refs=invalid_refs,
                            ),
                        )
                    if scope_expanded:
                        messages.extend(_scope_expansion_correction_messages(output=output))
                    else:
                        messages.extend(
                            _evidence_reference_correction_messages(
                                output=output,
                                packet=prepared_packet,
                                invalid_refs=invalid_refs,
                            )
                        )
        except (OpenAIError, ChatClientException, TimeoutError) as exc:
            _raise_h5_budget_refusal(exc, packets=[packet])
            return self._provider_failure_output(
                packet.task_id, exc, timeout_scope=timeout_scope,
            )
        raise RuntimeError("semantic judge correction loop did not return a result")

    async def judge_batch(
        self,
        *,
        items: list[tuple[ObservationPacket, list[dict[str, Any]]]],
    ) -> dict[str, SemanticJudgeModelOutput]:
        """同页规则最多四条合并请求，结果仍按 rule_code 独立校验。"""
        if not items or len(items) > 4:
            raise ValueError("语义 Judge 批次大小必须在 1 到 4 之间")
        task_ids = {packet.task_id for packet, _ in items}
        if len(task_ids) != 1:
            raise ValueError("语义 Judge 批次只能包含同一任务的规则")
        task_id = next(iter(task_ids))
        prepared: list[tuple[ObservationPacket, list[dict[str, Any]]]] = []
        outputs: dict[str, SemanticJudgeModelOutput] = {}
        cache_keys_by_rule_code: dict[str, tuple[str, str, str, str]] = {}
        for packet, tool_results in items:
            contract_gap = _incomplete_rule_contract_output(packet)
            if contract_gap is not None:
                outputs[packet.rule_code] = contract_gap
                continue
            cache_key = _semantic_result_cache_key(packet, tool_results)
            cache_keys_by_rule_code[packet.rule_code] = cache_key
            cached = self._result_cache.get(cache_key)
            if cached is not None:
                outputs[packet.rule_code] = cached.model_copy(deep=True)
                continue
            inflight = self._inflight_results.get(cache_key)
            if inflight is not None:
                outputs[packet.rule_code] = (await asyncio.shield(inflight)).model_copy(deep=True)
                continue
            fitted = _fit_packet_to_prompt_budget(packet, tool_results)
            if fitted is None:
                outputs[packet.rule_code] = _prompt_budget_insufficient_output(packet)
            else:
                prepared.append((fitted, tool_results))
        if not prepared:
            return outputs
        h5_batch = all(_is_h5_packet(packet) for packet, _ in prepared)
        timeout_scope = _timeout_scope([packet for packet, _ in prepared])
        for scope in dict.fromkeys((task_id, timeout_scope)):
            if self._provider_circuit_is_open(scope):
                outputs.update({
                    packet.rule_code: self._provider_unavailable_output(scope)
                    for packet, _ in prepared
                })
                return outputs
        fallback_codes: set[str] = set()
        try:
            async with asyncio.timeout(self._request_timeout_seconds):
                resolved = await self._judge_prepared_batch(
                    prepared=prepared,
                    outputs=outputs,
                    h5_batch=h5_batch,
                    fallback_codes=fallback_codes,
                )
            if not any(
                conflict.startswith("semantic_provider_")
                for output in resolved.values()
                for conflict in output.conflicts
            ):
                self._consecutive_timeouts.pop(timeout_scope, None)
            for packet, _ in prepared:
                output = resolved.get(packet.rule_code)
                if output is not None:
                    self._remember_result(
                        cache_keys_by_rule_code[packet.rule_code],
                        output,
                    )
            return resolved
        except (OpenAIError, ChatClientException, TimeoutError) as exc:
            _raise_h5_budget_refusal(exc, packets=[packet for packet, _ in prepared])
            # 拆单共享的是剩余批次预算，不能把累计耗尽当成一次完整请求超时。
            if h5_batch and fallback_codes and isinstance(exc, TimeoutError):
                failure = SemanticJudgeModelOutput(
                    conclusion="manual_review",
                    confidence=0.0,
                    reasoning_summary="H5 批次拆单剩余预算耗尽，当前规则尚未完成判定。",
                    conflicts=["semantic_batch_budget_exhausted"],
                )
            else:
                failure = self._provider_failure_output(
                    task_id, exc, timeout_scope=timeout_scope,
                )
            # H5 拆单降级可能已产出经过引用校验的结果；批次剩余预算耗尽
            # 只影响尚未完成的规则，不得用后一条的超时覆盖前一条结论。
            outputs.update(
                {
                    packet.rule_code: failure.model_copy(deep=True)
                    for packet, _ in prepared
                    if not h5_batch or packet.rule_code not in outputs
                }
            )
            return outputs

    def _remember_result(
        self,
        cache_key: tuple[str, str, str, str],
        output: SemanticJudgeModelOutput,
    ) -> SemanticJudgeModelOutput:
        """缓存可信结构化输出；线路失败只由短时熔断承载，禁止长期复用。"""
        if cache_key[1].startswith('web-sealed-material:') and (
            output.conclusion not in {'pass','fail'} or output.conflicts or output.missing_evidence
        ):
            return output
        if any(
            conflict.startswith(
                (
                    "semantic_provider_",
                    "semantic_batch_single_failed:",
                    "semantic_batch_budget_exhausted",
                )
            )
            for conflict in output.conflicts
        ):
            return output
        if (
            len(self._result_cache) >= SEMANTIC_RESULT_CACHE_MAX_ENTRIES
            and cache_key not in self._result_cache
        ):
            self._result_cache.pop(next(iter(self._result_cache)))
        self._result_cache[cache_key] = output.model_copy(deep=True)
        return output

    async def _judge_prepared_batch(
        self,
        *,
        prepared: list[tuple[ObservationPacket, list[dict[str, Any]]]],
        outputs: dict[str, SemanticJudgeModelOutput],
        h5_batch: bool,
        fallback_codes: set[str],
    ) -> dict[str, SemanticJudgeModelOutput]:
        """在外层统一墙钟预算内完成批次请求及至多一次拆单降级。"""
        budget = get_settings().quality_optimization.prompt_input_token_budget
        # 与 _build_batch_messages 同口径：同页共享事实只计一次，否则会把去重后
        # 本可通过的批次误判成超预算并拆回逐条。
        combined_tokens = estimate_batch_input_tokens(prepared)
        if combined_tokens > budget:
            await self._judge_single_fallback(
                prepared=prepared, outputs=outputs,
                h5_batch=h5_batch, fallback_codes=fallback_codes,
            )
            return outputs

        expected_codes = {packet.rule_code for packet, _ in prepared}
        try:
            response = await self._request_model(
                messages=_build_batch_messages(items=prepared, manual_test=self._manual_test),
                response_format=SemanticJudgeBatchOutput,
                batch=True,
            )
            batch_output = (
                SemanticJudgeBatchOutput.model_validate_json(response.value)
                if isinstance(response.value, str)
                else SemanticJudgeBatchOutput.model_validate(response.value)
            )
            actual_codes = {item.rule_code for item in batch_output.items}
            if actual_codes != expected_codes:
                raise ValueError("语义批次输出 rule_code 与请求不一致")
            packets_by_code = {packet.rule_code: packet for packet, _ in prepared}
            for item in batch_output.items:
                packet = packets_by_code[item.rule_code]
                invalid_refs = _invalid_evidence_refs(
                    output=item.result,
                    packet=packet,
                )
                if _is_out_of_scope_adult_content_failure(
                    output=item.result,
                    packet=packet,
                ):
                    outputs[item.rule_code] = _discard_out_of_scope_adult_content_failure(
                        output=item.result
                    )
                elif invalid_refs:
                    outputs[item.rule_code] = _quarantine_invalid_evidence_refs(
                        output=item.result,
                        packet=packet,
                        invalid_refs=invalid_refs,
                    )
                else:
                    outputs[item.rule_code] = item.result
            return outputs
        except (OpenAIError, ChatClientException):
            raise
        except Exception as exc:  # noqa: BLE001 - 批次结构错误仅允许拆分一次
            _raise_h5_budget_refusal(exc, packets=[packet for packet, _ in prepared])
            await self._judge_single_fallback(
                prepared=prepared, outputs=outputs,
                h5_batch=h5_batch, fallback_codes=fallback_codes,
            )
            return outputs

    async def _judge_single_fallback(
        self,
        *,
        prepared: list[tuple[ObservationPacket, list[dict[str, Any]]]],
        outputs: dict[str, SemanticJudgeModelOutput],
        h5_batch: bool,
        fallback_codes: set[str],
    ) -> None:
        """H5 至多四条拆单共享原批次预算；取消时必须等待所有子请求退出。"""
        fallback_codes.update(packet.rule_code for packet, _ in prepared)

        async def resolve(packet: ObservationPacket, tool_results: list[dict[str, Any]]) -> None:
            try:
                outputs[packet.rule_code] = await self.judge(
                    packet=packet, tool_results=tool_results,
                )
            except Exception as exc:  # noqa: BLE001
                _raise_h5_budget_refusal(exc, packets=[packet])
                outputs[packet.rule_code] = _manual_review_after_batch_failure(exc)

        if h5_batch:
            try:
                async with asyncio.TaskGroup() as group:
                    for packet, tool_results in prepared:
                        group.create_task(resolve(packet, tool_results))
            except ExceptionGroup as exc:
                _raise_h5_budget_refusal(exc, packets=[packet for packet, _ in prepared])
                raise
        else:
            for packet, tool_results in prepared:
                await resolve(packet, tool_results)

    async def _request_model(
        self,
        *,
        messages: list[Message],
        response_format: type[SemanticJudgeModelOutput] | type[SemanticJudgeBatchOutput],
        batch: bool,
    ) -> Any:
        """生产路径经真实 MAF Agent；显式 Client 仅保留为传输级单测替身。"""
        if self._client is not None:
            return await self._client.get_response(
                messages,
                options={"response_format": response_format},
            )
        agent = self._batch_agent if batch else self._agent
        if agent is None:
            agent = _get_semantic_judge_agent(batch=batch, manual_test=self._manual_test)
        return await agent.run(
            messages[1:],
            options={"response_format": response_format},
        )

    def _provider_circuit_is_open(self, task_id: str) -> bool:
        """只复用同一任务已确认的线路故障，避免并发任务相互污染。"""
        state = self._provider_circuits.get(task_id)
        if state is None:
            return False
        if time.monotonic() < state.unavailable_until:
            return True
        self._provider_circuits.pop(task_id, None)
        return False

    def _open_provider_circuit(self, task_id: str, exc: Exception) -> None:
        """线路恢复后自动半开重试，同时清理其他任务已经过期的状态。"""
        now = time.monotonic()
        self._provider_circuits = {
            key: state
            for key, state in self._provider_circuits.items()
            if state.unavailable_until > now
        }
        failure_code, failure_message = _provider_failure_diagnostic(exc)
        self._provider_circuits[task_id] = _ProviderCircuitState(
            unavailable_until=now + self._circuit_seconds,
            failure_code=failure_code,
            failure_message=failure_message,
        )

    def _provider_failure_output(
        self, task_id: str, exc: Exception, *, timeout_scope: str | None = None,
    ) -> SemanticJudgeModelOutput:
        """线路报错立刻熔断；超时先只算当次请求，同一任务连续超时才熔断。"""
        if isinstance(exc, TimeoutError):
            # H5 的复杂输入慢响应不能证明其他页面也不可用；传输故障仍按任务熔断。
            task_id = timeout_scope or task_id
            count = self._consecutive_timeouts.get(task_id, 0) + 1
            if count < SEMANTIC_TIMEOUTS_BEFORE_CIRCUIT:
                self._consecutive_timeouts[task_id] = count
                failure_code, failure_message = _provider_failure_diagnostic(exc)
                return _provider_unavailable_output(
                    diagnostic_code=failure_code,
                    diagnostic_message=failure_message,
                    circuit_open=False,
                )
            self._consecutive_timeouts.pop(task_id, None)
        self._open_provider_circuit(task_id, exc)
        return self._provider_unavailable_output(task_id)

    def _provider_unavailable_output(self, task_id: str) -> SemanticJudgeModelOutput:
        state = self._provider_circuits.get(task_id)
        if state is None:
            return _provider_unavailable_output()
        return _provider_unavailable_output(
            diagnostic_code=state.failure_code,
            diagnostic_message=state.failure_message,
        )


def _is_h5_packet(packet: ObservationPacket) -> bool:
    identity = packet.packet_payload.get("identity")
    page = packet.packet_payload.get("page")
    return (
        isinstance(identity, dict) and identity.get("target_platform") == "h5"
        and isinstance(page, dict) and page.get("surface_kind") == "web"
    )


def _timeout_scope(packets: list[ObservationPacket]) -> str:
    """同一 H5 实际采集的慢响应共享限流，其他平台保持原任务范围。"""
    task_id = packets[0].task_id
    if not all(_is_h5_packet(packet) for packet in packets):
        return task_id
    captures = sorted({packet.page_capture_id for packet in packets})
    digest = hashlib.sha256(json.dumps(captures).encode("utf-8")).hexdigest()
    return f"{task_id}:h5-input:{digest}"


@lru_cache
def _get_semantic_judge_client() -> Any:
    settings = get_settings()
    optimization = settings.quality_optimization
    llm_settings = settings.llm.model_copy(
        update={
            "timeout_seconds": optimization.semantic_request_timeout_seconds,
            "max_retries": optimization.semantic_max_retries,
        }
    )
    return build_chat_client(
        llm_settings,
        usage_source="semantic_rule_judge",
        usage_operation="rule_judge",
    )


@lru_cache(maxsize=4)
def _get_semantic_judge_agent(*, batch: bool, manual_test: bool = False) -> Agent:
    """构建无 Tool 的 MAF Rule Judge Agent；单条/批量、正式/人工测试的提示词各自冻结。

    生产路径只把 user 消息交给 Agent（见 _request_model），系统提示词以这里冻结的为准，
    所以人工测试模式必须在这里就分出独立的 Agent，不能只改每次请求拼的 system 文本。
    """
    response_format = SemanticJudgeBatchOutput if batch else SemanticJudgeModelOutput
    client = _get_semantic_judge_client()
    default_options = {"store": False} if getattr(client, "STORES_BY_DEFAULT", False) else None
    return Agent(
        client=client,
        instructions=build_rule_judge_system_instructions(
            output_schema=response_format.model_json_schema(),
            batch=batch,
            manual_test=manual_test,
        ),
        name="RuleJudgeAgent",
        tools=None,
        default_options=default_options,
    )


# 人工测试只免除纯视觉规则不需要的结构；必要测量和网络事实仍按规则合同核对。
MANUAL_TEST_TOOL_RESULTS_NOTE = (
    "人工挑选材料不改变规则合同：纯视觉规则可按截图和文字判；必要测量、网络、时间或"
    "动作事实缺失时，下面的 insufficient 仍须保留，不能靠目测补齐。"
)


def _build_messages(
    *,
    packet: ObservationPacket,
    tool_results: list[dict[str, Any]],
    manual_test: bool = False,
) -> list[Message]:
    output_schema = SemanticJudgeModelOutput.model_json_schema()
    system_text = build_rule_judge_system_instructions(
        output_schema=output_schema,
        manual_test=manual_test,
    )
    # 顺序即缓存前缀：同页所有规则共享的部分必须排在最前，规则专属的排在后面。
    # facts 约占 packet 的 94%，同页逐字一致；把它和截图放在规则合同之前，
    # 同一页第二条规则起就能整段命中供应商的 prompt 缓存。
    # 反之若像早期那样整包 packet 一起发（page/rule 在 facts 之前），前缀从
    # 开头就分叉，缓存命中恒为 0。
    facts = packet.packet_payload.get("facts")
    rule_scoped = {key: value for key, value in packet.packet_payload.items() if key != "facts"}
    user_contents: list[Content] = []
    if isinstance(facts, dict) and facts:
        user_contents.append(
            Content.from_text(
                "【页面事实】本块由同页各规则共享，只描述页面本身，不含任何规则标准。\n"
                + json.dumps(facts, ensure_ascii=False, sort_keys=True, default=str)
            )
        )
    for label, payload, media_type in packet.image_payloads:
        user_contents.append(Content.from_text(f"[截图证据 {label}]"))
        user_contents.append(Content.from_data(data=payload, media_type=media_type))
    user_contents.append(
        Content.from_text(
            "【当前规则与判定输入】\n"
            + json.dumps(rule_scoped, ensure_ascii=False, default=str)
        )
    )
    user_contents.append(
        Content.from_text(
            "【确定性 Tool 结果】\n"
            + (MANUAL_TEST_TOOL_RESULTS_NOTE + "\n" if manual_test else "")
            + json.dumps(tool_results, ensure_ascii=False, default=str)
        )
    )
    user_contents.append(
        Content.from_text(
            "【evidence_refs 唯一允许值】\n"
            + json.dumps(list(packet.evidence_artifact_ids), ensure_ascii=False)
            + "\n只能逐字复制上述 ID；没有适用证据时返回空数组，禁止生成或改写 ID。"
        )
    )
    return [
        Message("system", [Content.from_text(system_text)]),
        Message("user", user_contents),
    ]


def _shared_facts_for_batch(
    items: list[tuple[ObservationPacket, list[dict[str, Any]]]],
) -> dict[str, Any] | None:
    """提取同页各规则 facts 中逐字相同的键，作为共享块；没有则返回 None。

    实测同一采集下 facts 占 packet 的 94%，其中 ui_snapshot / dom_summary /
    journey / action_log 等页面级事实在同页所有规则间逐字一致，逐条重发会把
    批量请求的输入 token 成倍放大。

    必须逐键比较而不是整体比较：facts 里混着 `packet_budget` 这类规则级字段
    （每条规则各不相同，但只有几十字符），整体比较会因为它永远不相等而让去重
    完全不触发——白白留着 99% 的重复。

    只提取值完全相同的键，任何有差异的键都留在各自 packet 里：宁可多花 token，
    也不能让某条规则读到不属于它的页面事实。
    """
    if len(items) < 2:
        return None
    first = items[0][0].packet_payload.get("facts")
    if not isinstance(first, dict) or not first:
        return None
    others: list[dict[str, Any]] = []
    for packet, _ in items[1:]:
        other = packet.packet_payload.get("facts")
        if not isinstance(other, dict):
            return None
        others.append(other)

    def encode(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)

    shared = {
        key: value
        for key, value in first.items()
        if all(key in other and encode(other[key]) == encode(value) for other in others)
    }
    if any(packet.packet_payload.get("web_evidence", {}).get("input_set_id") for packet, _ in items):
        # 序列是逐规则冻结输入，即使值相同也保留在所属规则块。
        shared.pop("web_sequence", None)
    return shared or None


def _packet_payload_without_shared_facts(
    payload: dict[str, Any], shared_facts: dict[str, Any]
) -> dict[str, Any]:
    """移除已进共享块的 facts 键，保留该规则独有的部分。"""
    facts = payload.get("facts")
    if not isinstance(facts, dict):
        return payload
    remaining = {key: value for key, value in facts.items() if key not in shared_facts}
    trimmed = {key: value for key, value in payload.items() if key != "facts"}
    if remaining:
        trimmed["facts"] = remaining
    return trimmed


def estimate_batch_input_tokens(
    items: list[tuple[ObservationPacket, list[dict[str, Any]]]],
) -> int:
    """按批量实际发送形态估算输入 token；共享事实只计一次。

    必须与 `_build_batch_messages` 同源：预算闸门若仍按逐条口径累加，会把去重后
    本可通过的批次误判成超预算再拆回逐条，优化就白做了。
    """
    shared_facts = _shared_facts_for_batch(items)
    # 输出 Schema 在批次里只随 system 指令发一次，不该按条数重复累加。
    total = _estimate_json_tokens(SemanticJudgeModelOutput.model_json_schema())
    if shared_facts is not None:
        total += _estimate_json_tokens(shared_facts)
    for packet, tool_results in items:
        payload = (
            packet.packet_payload
            if shared_facts is None
            else _packet_payload_without_shared_facts(packet.packet_payload, shared_facts)
        )
        total += _estimate_json_tokens({"packet": payload, "tools": tool_results})
    return total


def _build_batch_messages(
    *,
    items: list[tuple[ObservationPacket, list[dict[str, Any]]]],
    manual_test: bool = False,
) -> list[Message]:
    """同页共享的截图与页面事实各只发送一次，规则专属部分按 rule_code 分列。"""
    system_text = build_rule_judge_system_instructions(
        output_schema=SemanticJudgeBatchOutput.model_json_schema(),
        batch=True,
        manual_test=manual_test,
    )
    contents: list[Content] = []
    shared_facts = _shared_facts_for_batch(items)
    if shared_facts is not None:
        web_sequence_batch = any(
            packet.packet_payload.get("web_evidence", {}).get("input_set_id") for packet, _ in items
        )
        contents.append(
            Content.from_text(
                "【同页共享页面事实】\n"
                + json.dumps(shared_facts, ensure_ascii=False, default=str)
                + ("\n这里只合并完全相同的普通事实；逐条规则保留自己的序列事实，按各自 InputSet、帧时间和制品引用判定；"
                   if web_sequence_batch else "\n以下每条规则的 observation_packet 均已省略 facts，一律以本块为准；")
                + "本块只描述页面本身，不含任何规则标准。"
            )
        )
    for packet, tool_results in items:
        payload = (
            packet.packet_payload
            if shared_facts is None
            else _packet_payload_without_shared_facts(packet.packet_payload, shared_facts)
        )
        contents.append(
            Content.from_text(
                f"【规则 {packet.rule_code}】\n"
                + json.dumps(
                    {
                        "observation_packet": payload,
                        "tool_results": tool_results,
                        "allowed_evidence_refs": list(packet.evidence_artifact_ids),
                    },
                    ensure_ascii=False,
                    default=str,
                )
            )
        )
    image_groups: dict[str, tuple[list[str], bytes, str]] = {}
    for packet, _ in items:
        for label, payload, media_type in packet.image_payloads:
            digest = f"{media_type}:{hashlib.sha256(payload).hexdigest()}"
            if digest not in image_groups:
                image_groups[digest] = ([], payload, media_type)
            labels = image_groups[digest][0]
            if label not in labels:
                labels.append(label)
    for labels, payload, media_type in image_groups.values():
        # 像素可去重，各规则自己的制品引用不能丢；标签均指向随后同一张图片。
        for label in labels:
            contents.append(Content.from_text(f"[同页截图证据 {label}]"))
        contents.append(Content.from_data(data=payload, media_type=media_type))
    return [
        Message("system", [Content.from_text(system_text)]),
        Message("user", contents),
    ]


def _fit_packet_to_prompt_budget(
    packet: ObservationPacket, tool_results: list[dict[str, Any]]
) -> ObservationPacket | None:
    """最多压缩一次证据包，并在模型调用前执行文本 token 硬门禁。"""
    budget = get_settings().quality_optimization.prompt_input_token_budget
    if _estimate_semantic_input_tokens(packet.packet_payload, tool_results) <= budget:
        return packet
    payload = copy.deepcopy(packet.packet_payload)
    facts = payload.get("facts")
    if isinstance(facts, dict):
        facts["visible_text"] = str(facts.get("visible_text") or "")[:3_000]
        _limit_fact_elements(facts.get("dom_summary"), 60)
        _limit_fact_elements(facts.get("ui_snapshot"), 60)
        journey = facts.get("journey")
        if isinstance(journey, list):
            facts["journey"] = journey[-4:]
            for step in facts["journey"]:
                if isinstance(step, dict):
                    _limit_fact_elements(step.get("ui_snapshot"), 20)
        _limit_event_entries(facts.get("network_summary"), 30)
        _limit_event_entries(facts.get("console_summary"), 30)
        if _is_h5_packet(packet):
            _limit_packet_ui_snapshot(
                facts.get("ui_snapshot"), maximum_frames=4, maximum_elements=20,
            )
            roles = facts.get("capture_roles")
            for fact in roles.values() if isinstance(roles, dict) else ():
                if isinstance(fact, dict):
                    _limit_packet_ui_snapshot(
                        fact.get("ui_snapshot"), maximum_frames=4, maximum_elements=20,
                    )
    compact_packet = ObservationPacket(
        observation_packet_id=packet.observation_packet_id,
        task_id=packet.task_id,
        page_capture_id=packet.page_capture_id,
        rule_id=packet.rule_id,
        rule_code=packet.rule_code,
        evidence_artifact_ids=packet.evidence_artifact_ids,
        packet_payload=payload,
        quality_gate_status=packet.quality_gate_status,
        quality_gate_reason=packet.quality_gate_reason,
        image_payloads=packet.image_payloads,
    )
    return (
        compact_packet
        if _estimate_semantic_input_tokens(compact_packet.packet_payload, tool_results) <= budget
        else None
    )


def _incomplete_rule_contract_output(packet: ObservationPacket) -> SemanticJudgeModelOutput | None:
    """合同投影超限是平台输入限制，不能交给模型目测，也不能生成现场补证请求。"""
    rule = packet.packet_payload.get("rule")
    contract = rule.get("input_contract") if isinstance(rule, dict) else None
    if not isinstance(contract, dict) or not (
        contract.get("audit_payload_truncated") is True or contract.get("budget_truncated") is True
        or contract.get("projection_incomplete") is True
    ):
        return None
    return SemanticJudgeModelOutput(
        conclusion="manual_review",
        confidence=0.0,
        reasoning_summary="规则输入要求超出安全投影范围，未发送模型请求。",
        conflicts=["rule_input_contract_truncated"],
        review_reason="需要核对规则输入配置；重新采集页面不能恢复被截断的规则要求。",
    )


def _prompt_budget_insufficient_output(packet: ObservationPacket) -> SemanticJudgeModelOutput:
    if _is_h5_packet(packet):
        return SemanticJudgeModelOutput(
            conclusion="manual_review",
            confidence=0.0,
            reasoning_summary="语义输入压缩后仍超限，未发送模型请求；不属于现场证据缺失。",
            conflicts=["prompt_input_token_budget"],
            review_reason="现有材料需要缩减判定输入或人工复核，重新采集页面不能消除此限制。",
        )
    return SemanticJudgeModelOutput(
        conclusion="evidence_insufficient",
        confidence=0.0,
        reasoning_summary="语义证据压缩后仍超过模型文本输入预算，未发送超限请求。",
        missing_evidence=[
            "prompt_input_token_budget",
            "需要定向补采更小的语义视图",
        ],
        review_reason="模型输入载荷超过安全预算，需补采或人工复核。",
    )


def _manual_review_after_batch_failure(exc: Exception) -> SemanticJudgeModelOutput:
    return SemanticJudgeModelOutput(
        conclusion="manual_review",
        confidence=0.0,
        reasoning_summary="语义批次拆分为单条后仍未取得有效结构化结果。",
        conflicts=[f"semantic_batch_single_failed:{type(exc).__name__}"],
        review_reason="批次结构错误已拆分一次，单条仍失败，转人工复核。",
    )


def _provider_unavailable_output(
    *,
    diagnostic_code: str = "semantic_provider_temporarily_unavailable",
    diagnostic_message: str = "模型 API 暂时不可用，未生成自动判定结论。",
    circuit_open: bool = True,
) -> SemanticJudgeModelOutput:
    """模型线路不可用不是页面证据缺失，必须进入人工复核而非误计证据不足。"""
    return SemanticJudgeModelOutput(
        conclusion="manual_review",
        confidence=0.0,
        reasoning_summary=diagnostic_message,
        conflicts=[diagnostic_code],
        review_reason=(
            f"{diagnostic_message} 同源请求已短时熔断并转人工复核。"
            if circuit_open
            else f"{diagnostic_message} 本次请求转人工复核，后续规则照常判定。"
        ),
    )


def _raise_h5_budget_refusal(exc: Exception, *, packets: list[ObservationPacket]) -> None:
    from services.api.app.application.h5_llm_budget import (
        H5LLMBudgetExhausted,
        h5_llm_budget_error_code,
    )

    if packets and all(_is_h5_packet(packet) for packet in packets) and h5_llm_budget_error_code(exc):
        raise H5LLMBudgetExhausted() from exc


def _provider_failure_diagnostic(exc: Exception) -> tuple[str, str]:
    """从包装异常中提取可审计 HTTP 状态，不保存响应正文或认证信息。"""
    if isinstance(exc, TimeoutError):
        return (
            "semantic_provider_timeout",
            "语义规则判定超过独立墙钟预算，未生成自动判定结论。",
        )
    pending: list[BaseException] = [exc]
    visited: set[int] = set()
    exception_types: list[str] = []
    while pending:
        current = pending.pop(0)
        if id(current) in visited:
            continue
        visited.add(id(current))
        exception_types.append(type(current).__name__)
        status_code = _provider_http_status(current)
        if status_code is not None:
            label = {
                401: "认证失败",
                403: "访问被拒绝",
                408: "请求超时",
                429: "请求限流",
                500: "服务内部错误",
                502: "网关错误",
                503: "服务暂时不可用",
                504: "网关超时",
            }.get(status_code, "请求失败")
            return (
                f"semantic_provider_http_{status_code}",
                f"模型 API 返回 HTTP {status_code}（{label}），未生成自动判定结论。",
            )
        for nested in (current.__cause__, current.__context__, *current.args):
            if isinstance(nested, BaseException):
                pending.append(nested)
    exception_name = exception_types[-1] if exception_types else type(exc).__name__
    return (
        f"semantic_provider_{exception_name.casefold()}",
        f"模型 API 调用失败（{exception_name}），未生成自动判定结论。",
    )


def _provider_http_status(exc: BaseException) -> int | None:
    raw_status = getattr(exc, "status_code", None)
    if raw_status is None:
        response = getattr(exc, "response", None)
        raw_status = getattr(response, "status_code", None)
    try:
        status_code = int(raw_status)
    except (TypeError, ValueError):
        return None
    return status_code if 100 <= status_code <= 599 else None


def _semantic_result_cache_key(
    packet: ObservationPacket,
    tool_results: list[dict[str, Any]],
) -> tuple[str, str, str, str]:
    """以不可变证据包和确定性 Tool 结果形成任务隔离的判定缓存键。"""
    digest_payload = json.dumps(
        {
            "packet_payload": packet.packet_payload,
            "evidence_artifact_ids": packet.evidence_artifact_ids,
            "tool_results": tool_results,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return (
        packet.task_id,
        _web_sealed_material_cache_identity(packet) or packet.observation_packet_id,
        packet.rule_code,
        hashlib.sha256(digest_payload).hexdigest(),
    )


def _web_sealed_material_cache_identity(packet):
    """只省略存储 UUID；完整 Packet 摘要仍参与键，不跨素材重映射证据引用。"""
    proof = packet.packet_payload.get('web_evidence')
    if not isinstance(proof,dict) or proof.get('status')!='admissible':
        return None
    contract = proof.get('contract') or {}
    declaration = contract.get('local_structure') or {}
    revision = proof.get('capability_revision') or {}
    plan = proof.get('local_structure_plan') or {}
    if (declaration.get('reuse_policy')!='same_material_context' or not proof.get('input_set_id')
        or not proof.get('rule_contract_hash') or not contract.get('rule_version')
        or plan.get('status')!='admissible' or not plan.get('reuse_key')
        or any(revision.get(key) is None for key in ('revision_id','revision','material_hash','position',
            'account_state','visible_layer','occurrence_id','capture_id'))):
        return None
    return 'web-sealed-material:'+proof['input_set_id']


def _estimate_json_tokens(value: Any) -> int:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return (len(text.encode("utf-8")) + 2) // 3


def _estimate_semantic_input_tokens(
    packet_payload: dict[str, Any], tool_results: list[dict[str, Any]]
) -> int:
    """为图片和输出留出独立空间，仅估算模型文本输入。"""
    return _estimate_json_tokens(
        {
            "schema": SemanticJudgeModelOutput.model_json_schema(),
            "packet": packet_payload,
            "tools": tool_results,
        }
    )


def _limit_fact_elements(value: Any, limit: int) -> None:
    if not isinstance(value, dict):
        return
    elements = value.get("elements")
    if isinstance(elements, list) and len(elements) > limit:
        value["elements"] = elements[:limit]
        value["prompt_truncated"] = True


def _limit_event_entries(value: Any, limit: int) -> None:
    if not isinstance(value, dict):
        return
    entries = value.get("entries")
    if isinstance(entries, list) and len(entries) > limit:
        value["entries"] = entries[:limit]
        value["prompt_truncated"] = True


def _parse_model_output(value: Any) -> SemanticJudgeModelOutput:
    raw = json.loads(value) if isinstance(value, str) else value
    if isinstance(raw, SemanticJudgeModelOutput):
        return raw
    if not isinstance(raw, dict):
        return SemanticJudgeModelOutput.model_validate(raw)
    payload = dict(raw)
    conclusion = payload.get("conclusion")
    incomplete = (
        conclusion == "evidence_insufficient"
        and not _nonempty_string_list(payload.get("missing_evidence"))
    ) or (conclusion in {"fail", "warning"} and not _raw_output_has_evidence_refs(payload))
    if incomplete:
        reason = "模型输出的证据契约不完整，已隔离原风险结论并转人工复核。"
        payload["conclusion"] = "manual_review"
        confidence = payload.get("confidence")
        if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
            payload["confidence"] = min(float(confidence), 0.5)
        conflicts = payload.get("conflicts")
        if isinstance(conflicts, list):
            payload["conflicts"] = [
                *conflicts,
                "semantic_output_evidence_contract_incomplete",
            ][-50:]
        payload["review_reason"] = reason
    return SemanticJudgeModelOutput.model_validate(payload)


def _validation_error_input_from_exception(
    exc: BaseException,
) -> dict[str, Any] | None:
    pending = [exc]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        if isinstance(current, ValidationError):
            for error in current.errors(include_url=False):
                raw_input = error.get("input")
                if isinstance(raw_input, dict):
                    return dict(raw_input)
        nested_values = (
            current.__cause__,
            current.__context__,
            getattr(current, "inner_exception", None),
            *current.args,
        )
        pending.extend(nested for nested in nested_values if isinstance(nested, BaseException))
    return None


def _nonempty_string_list(value: Any) -> bool:
    return isinstance(value, (list, tuple)) and any(
        isinstance(item, str) and item.strip() for item in value
    )


def _raw_output_has_evidence_refs(payload: dict[str, Any]) -> bool:
    if _nonempty_string_list(payload.get("evidence_refs")):
        return True
    findings = payload.get("findings")
    return isinstance(findings, list) and any(
        isinstance(item, dict) and _nonempty_string_list(item.get("evidence_refs"))
        for item in findings
    )


def _invalid_evidence_refs(
    *,
    output: SemanticJudgeModelOutput,
    packet: ObservationPacket,
) -> list[str]:
    allowed = set(packet.evidence_artifact_ids)
    referenced = set(output.evidence_refs)
    for finding in output.findings:
        referenced.update(finding.evidence_refs)
    return sorted(
        reference for reference in referenced if reference.split("#frame-", 1)[0] not in allowed
    )


def _evidence_reference_correction_messages(
    *,
    output: SemanticJudgeModelOutput,
    packet: ObservationPacket,
    invalid_refs: list[str],
) -> list[Message]:
    return [
        Message(
            "assistant",
            [Content.from_text(json.dumps(output.model_dump(mode="json"), ensure_ascii=False))],
        ),
        Message(
            "user",
            [
                Content.from_text(
                    "上一次输出包含不属于 Observation Packet 的 evidence_refs："
                    + "、".join(invalid_refs)
                    + "。请重新输出完整 JSON。唯一允许的 evidence artifact ID 为："
                    + json.dumps(list(packet.evidence_artifact_ids), ensure_ascii=False)
                    + "。只能逐字复制；没有适用证据时使用空数组。"
                )
            ],
        ),
    ]


def _is_out_of_scope_adult_content_failure(
    *,
    output: SemanticJudgeModelOutput,
    packet: ObservationPacket,
) -> bool:
    """识别模型承认当前规则无命中，却因成人题材扩张通用安全范围的结论。"""
    if output.conclusion not in {"fail", "warning", "manual_review"}:
        return False
    rule = packet.packet_payload.get("rule")
    rule_text = json.dumps(
        rule if isinstance(rule, dict) else {},
        ensure_ascii=False,
        default=str,
    ).casefold()
    if any(marker in rule_text for marker in _ADULT_CONTENT_MARKERS):
        return False

    output_text = json.dumps(
        output.model_dump(mode="json"),
        ensure_ascii=False,
        default=str,
    ).casefold()
    return (
        any(marker in output_text for marker in _ADULT_CONTENT_MARKERS)
        and any(marker in output_text for marker in _OUT_OF_SCOPE_EXPANSION_MARKERS)
        and any(marker in output_text for marker in _DECLARED_SCOPE_ABSENCE_MARKERS)
    )


def _scope_expansion_correction_messages(*, output: SemanticJudgeModelOutput) -> list[Message]:
    """要求模型移除已明确禁止的跨规则成人内容判定。"""
    return [
        Message(
            "assistant",
            [Content.from_text(json.dumps(output.model_dump(mode="json"), ensure_ascii=False))],
        ),
        Message(
            "user",
            [
                Content.from_text(
                    "上一结论违反规则作用域：你已承认没有当前 check_point 所检查的"
                    "政治、恐怖或现实主体人身攻击证据，却因成人题材、所谓整体 "
                    "input_risk 或其他规则红线判定异常。请完全丢弃这些越界依据，只按"
                    "当前规则重新输出完整 JSON；若没有当前规则的明确违反证据，应返回 pass。"
                )
            ],
        ),
    ]


def _discard_out_of_scope_adult_content_failure(
    *, output: SemanticJudgeModelOutput
) -> SemanticJudgeModelOutput:
    """模型连续越界时按确定性平台边界丢弃成人题材造成的伪失败。"""
    return SemanticJudgeModelOutput(
        conclusion="pass",
        confidence=1.0,
        reasoning_summary=(
            "模型结论已明确承认不存在当前规则检查的风险，却仅因允许质检的成人题材"
            "扩张为整体内容安全失败；该越界依据已由规则作用域保护丢弃，当前规则通过。"
        ),
        conflicts=[
            *output.conflicts,
            "out_of_scope_adult_content_failure_discarded",
        ][-50:],
    )


def _quarantine_invalid_evidence_refs(
    *,
    output: SemanticJudgeModelOutput,
    packet: ObservationPacket,
    invalid_refs: list[str],
) -> SemanticJudgeModelOutput:
    """连续修正失败时隔离外部引用，并把结论降级为人工复核。"""
    allowed = set(packet.evidence_artifact_ids)

    def valid_refs(refs: list[str]) -> list[str]:
        return [reference for reference in refs if reference.split("#frame-", 1)[0] in allowed]

    cleaned_findings = [
        finding.model_copy(update={"evidence_refs": valid_refs(finding.evidence_refs)})
        for finding in output.findings
    ]
    reason = (
        "语义 Judge 连续返回了不属于 Observation Packet 的证据引用，"
        "越界引用已隔离，结论转人工复核：" + "、".join(invalid_refs)
    )
    return SemanticJudgeModelOutput.model_validate(
        {
            **output.model_dump(mode="json"),
            "conclusion": "manual_review",
            "confidence": min(output.confidence, 0.5),
            "findings": [item.model_dump(mode="json") for item in cleaned_findings],
            "evidence_refs": valid_refs(output.evidence_refs),
            "conflicts": [*output.conflicts, reason],
            "review_reason": reason,
        }
    )
