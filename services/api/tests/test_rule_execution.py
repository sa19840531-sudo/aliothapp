import pytest

from services.api.app.application.rule_execution import (
    RecordedRuleExecution,
    RecordRuleExecutionCommand,
    ReviewStatus,
    RuleExecutionDefinition,
    RuleExecutionService,
    RuleExecutionValidationError,
)


class RecordingRuleExecutionRepository:
    def __init__(
        self,
        *,
        executor_type: str = "rule_engine",
        manual_review_policy: dict | None = None,
        severity: str | None = "medium",
    ) -> None:
        self.commands: list[RecordRuleExecutionCommand] = []
        self.executor_type = executor_type
        self.manual_review_policy = manual_review_policy or {}
        self.severity = severity

    async def get_rule_execution_definition(
        self, *, task_id: str, rule_id: str | None, rule_code: str | None
    ) -> RuleExecutionDefinition | None:
        return RuleExecutionDefinition(
            executor_type=self.executor_type,
            manual_review_policy=self.manual_review_policy,
            severity=self.severity,
        )

    async def record_rule_execution(
        self, command: RecordRuleExecutionCommand
    ) -> RecordedRuleExecution:
        self.commands.append(command)
        return RecordedRuleExecution(
            execution_id="execution-1",
            rule_id="rule-1",
            rule_code=command.rule_code or "alioth_1",
            execution_status=command.execution_status,
            page_capture_id=command.page_capture_id,
        )


async def test_invalid_review_status_is_rejected_before_repository_call() -> None:
    repository = RecordingRuleExecutionRepository()
    service = RuleExecutionService(repository=repository)

    with pytest.raises(RuleExecutionValidationError, match="非法人工审核状态：required"):
        await service.record_rule_execution(
            RecordRuleExecutionCommand(
                task_id="task-1",
                rule_code="alioth_1",
                execution_status="manual_review",
                review_status="required",
            )
        )

    assert repository.commands == []


async def test_deterministic_conclusion_requires_non_empty_evidence_ids() -> None:
    repository = RecordingRuleExecutionRepository()
    service = RuleExecutionService(repository=repository)

    with pytest.raises(RuleExecutionValidationError, match="必须提供有效证据"):
        await service.record_rule_execution(
            RecordRuleExecutionCommand(
                task_id="task-1",
                rule_code="alioth_6",
                execution_status="pass",
                page_capture_id="capture-1",
                evidence_artifact_ids=[""],
            )
        )

    assert repository.commands == []


async def test_deterministic_reuse_accepts_auditable_source_reference() -> None:
    repository = RecordingRuleExecutionRepository()
    service = RuleExecutionService(repository=repository)

    await service.record_rule_execution(
        RecordRuleExecutionCommand(
            task_id="task-1",
            rule_code="alioth_6",
            execution_status="pass",
            page_capture_id="capture-current",
            judge_output={
                "reuse": {
                    "protocol_version": "rule-reuse.v1",
                    "scope": "per_task",
                    "reuse_key": "task:rule-1",
                    "source_execution_id": "execution-source",
                    "source_page_capture_id": "capture-source",
                    "source_execution_status": "pass",
                }
            },
        )
    )

    assert repository.commands[0].evidence_artifact_ids == []


async def test_deterministic_reuse_rejects_incomplete_source_reference() -> None:
    repository = RecordingRuleExecutionRepository()
    service = RuleExecutionService(repository=repository)

    with pytest.raises(RuleExecutionValidationError, match="必须提供有效证据"):
        await service.record_rule_execution(
            RecordRuleExecutionCommand(
                task_id="task-1",
                rule_code="alioth_6",
                execution_status="pass",
                page_capture_id="capture-current",
                judge_output={
                    "reuse": {
                        "protocol_version": "rule-reuse.v1",
                        "scope": "per_task",
                        "reuse_key": "task:rule-1",
                        "source_execution_id": "execution-source",
                        "source_execution_status": "pass",
                    }
                },
            )
        )

    assert repository.commands == []


async def test_conclusion_requires_page_capture_id() -> None:
    repository = RecordingRuleExecutionRepository()
    service = RuleExecutionService(repository=repository)

    with pytest.raises(RuleExecutionValidationError, match="必须提供页面采集 ID"):
        await service.record_rule_execution(
            RecordRuleExecutionCommand(
                task_id="task-1",
                rule_code="alioth_6",
                execution_status="fail",
                evidence_artifact_ids=["artifact-1"],
            )
        )

    assert repository.commands == []


async def test_evidence_ids_require_page_capture_id() -> None:
    repository = RecordingRuleExecutionRepository()
    service = RuleExecutionService(repository=repository)

    with pytest.raises(RuleExecutionValidationError, match="证据附件必须绑定页面采集 ID"):
        await service.record_rule_execution(
            RecordRuleExecutionCommand(
                task_id="task-1",
                rule_code="alioth_6",
                execution_status="skipped",
                evidence_artifact_ids=["artifact-1"],
            )
        )

    assert repository.commands == []


async def test_skipped_without_page_capture_id_remains_supported() -> None:
    repository = RecordingRuleExecutionRepository()
    service = RuleExecutionService(repository=repository)

    result = await service.record_rule_execution(
        RecordRuleExecutionCommand(
            task_id="task-1",
            rule_code="alioth_6",
            execution_status="skipped",
            result_summary="未进入目标页面",
        )
    )

    assert result.page_capture_id is None
    assert repository.commands[0].page_capture_id is None
    assert repository.commands[0].judge_strategy == "rule_engine"


async def test_source_idempotency_key_is_preserved_for_captureless_execution() -> None:
    repository = RecordingRuleExecutionRepository()
    service = RuleExecutionService(repository=repository)

    await service.record_rule_execution(
        RecordRuleExecutionCommand(
            task_id="task-1",
            rule_code="alioth_6",
            execution_status="evidence_insufficient",
            source_idempotency_key="web-coverage-gap:snapshot-1:content_detail:alioth_6",
            result_summary="未探索到必需页型",
        )
    )

    command = repository.commands[0]
    assert command.page_capture_id is None
    assert (
        command.source_idempotency_key
        == "web-coverage-gap:snapshot-1:content_detail:alioth_6"
    )


async def test_source_idempotency_key_rejects_blank_value() -> None:
    repository = RecordingRuleExecutionRepository()
    service = RuleExecutionService(repository=repository)

    with pytest.raises(RuleExecutionValidationError, match="来源幂等键"):
        await service.record_rule_execution(
            RecordRuleExecutionCommand(
                task_id="task-1",
                rule_code="alioth_6",
                execution_status="evidence_insufficient",
                source_idempotency_key="  ",
            )
        )

    assert repository.commands == []


async def test_source_idempotency_key_defaults_to_none_for_existing_callers() -> None:
    repository = RecordingRuleExecutionRepository()
    service = RuleExecutionService(repository=repository)

    await service.record_rule_execution(
        RecordRuleExecutionCommand(
            task_id="task-1",
            rule_code="alioth_6",
            execution_status="skipped",
        )
    )

    assert repository.commands[0].source_idempotency_key is None
    assert repository.commands[0].create_review_item is True


async def test_trusted_web_coverage_gap_can_explicitly_disable_review_item() -> None:
    repository = RecordingRuleExecutionRepository()
    service = RuleExecutionService(repository=repository)

    await service.record_rule_execution(
        RecordRuleExecutionCommand(
            task_id="task-1",
            agent_run_id="run-1",
            rule_code="alioth_6",
            execution_status="evidence_insufficient",
            source_idempotency_key=(
                "web-coverage-gap:snapshot-1:content_detail:alioth_6"
            ),
            judge_input={
                "schema_version": "web-coverage-gap.v1",
                "source": "web_coverage_gap",
                "coverage_snapshot_id": "snapshot-1",
                "missing_page_type": "content_detail",
                "reason": "required_page_type_not_discovered",
            },
            create_review_item=False,
        )
    )

    assert repository.commands[0].create_review_item is False


@pytest.mark.parametrize(
    "command",
    (
        RecordRuleExecutionCommand(
            task_id="task-1",
            rule_code="alioth_6",
            execution_status="skipped",
            source_idempotency_key="web-coverage-gap:snapshot-1:home:alioth_6",
            create_review_item=False,
        ),
        RecordRuleExecutionCommand(
            task_id="task-1",
            rule_code="alioth_6",
            execution_status="evidence_insufficient",
            source_idempotency_key="external:snapshot-1:home:alioth_6",
            create_review_item=False,
        ),
    ),
)
async def test_review_suppression_is_restricted_to_trusted_web_gap(command) -> None:
    repository = RecordingRuleExecutionRepository()
    service = RuleExecutionService(repository=repository)

    with pytest.raises(RuleExecutionValidationError, match="Web Coverage Gap"):
        await service.record_rule_execution(command)

    assert repository.commands == []


async def test_rule_execution_rejects_strategy_that_conflicts_with_locked_rule() -> None:
    repository = RecordingRuleExecutionRepository(executor_type="rule_engine")
    service = RuleExecutionService(repository=repository)

    with pytest.raises(RuleExecutionValidationError, match="判定策略不一致"):
        await service.record_rule_execution(
            RecordRuleExecutionCommand(
                task_id="task-1",
                rule_code="alioth_6",
                execution_status="pass",
                page_capture_id="capture-1",
                evidence_artifact_ids=["artifact-1"],
                judge_strategy="llm",
            )
        )

    assert repository.commands == []


async def test_locked_rule_severity_overrides_malformed_model_value() -> None:
    repository = RecordingRuleExecutionRepository(severity="critical")
    service = RuleExecutionService(repository=repository)

    await service.record_rule_execution(
        RecordRuleExecutionCommand(
            task_id="task-1",
            rule_code="alioth_6",
            execution_status="skipped",
            severity="low','result_summary':'invalid",
            result_summary="当前页面不适用",
        )
    )

    assert repository.commands[0].severity == "critical"


async def test_invalid_severity_is_rejected_when_rule_has_no_locked_value() -> None:
    repository = RecordingRuleExecutionRepository(severity=None)
    service = RuleExecutionService(repository=repository)

    with pytest.raises(RuleExecutionValidationError, match="非法规则严重级别"):
        await service.record_rule_execution(
            RecordRuleExecutionCommand(
                task_id="task-1",
                rule_code="alioth_6",
                execution_status="skipped",
                severity="invalid",
                result_summary="当前页面不适用",
            )
        )

    assert repository.commands == []


async def test_llm_conclusion_requires_structured_judge_output() -> None:
    repository = RecordingRuleExecutionRepository(
        executor_type="llm",
        manual_review_policy={"min_confidence": 0.75},
    )
    service = RuleExecutionService(repository=repository)

    with pytest.raises(RuleExecutionValidationError, match="reasoning_summary"):
        await service.record_rule_execution(
            RecordRuleExecutionCommand(
                task_id="task-1",
                rule_code="alioth_4",
                execution_status="pass",
                page_capture_id="capture-1",
                confidence=0.9,
                evidence_artifact_ids=["artifact-1"],
                judge_input={"observation_summary": "页面文案未发现错别字"},
                judge_output={"schema_version": "1.0", "conclusion": "pass"},
            )
        )

    assert repository.commands == []


async def test_llm_conclusion_persists_unified_output_contract() -> None:
    repository = RecordingRuleExecutionRepository(
        executor_type="llm",
        manual_review_policy={"min_confidence": 0.75},
    )
    service = RuleExecutionService(repository=repository)

    await service.record_rule_execution(
        RecordRuleExecutionCommand(
            task_id="task-1",
            rule_code="alioth_4",
            execution_status="pass",
            page_capture_id="capture-1",
            confidence=0.9,
            result_summary="未发现错别字",
            evidence_artifact_ids=["artifact-1"],
            judge_input={"observation_summary": "页面文案未发现错别字"},
            judge_output={
                "schema_version": "1.0",
                "conclusion": "pass",
                "reasoning_summary": "可见文案符合规则要求",
            },
        )
    )

    command = repository.commands[0]
    assert command.judge_strategy == "llm"
    assert command.execution_status == "pass"
    assert command.judge_output["conclusion"] == "pass"


@pytest.mark.parametrize("raw_version", (1.0, 1, "1", "v1.0", None))
async def test_llm_judge_output_schema_version_aliases_are_normalized(raw_version) -> None:
    repository = RecordingRuleExecutionRepository(
        executor_type="llm",
        manual_review_policy={"min_confidence": 0.75},
    )
    service = RuleExecutionService(repository=repository)
    judge_output = {"conclusion": "pass", "reasoning_summary": "可见文案符合规则要求"}
    if raw_version is not None:
        judge_output["schema_version"] = raw_version

    await service.record_rule_execution(
        RecordRuleExecutionCommand(
            task_id="task-1",
            rule_code="alioth_4",
            execution_status="pass",
            page_capture_id="capture-1",
            confidence=0.9,
            evidence_artifact_ids=["artifact-1"],
            judge_input={"observation_summary": "页面文案未发现错别字"},
            judge_output=judge_output,
        )
    )

    command = repository.commands[0]
    assert command.execution_status == "pass"
    assert command.judge_output["schema_version"] == "1.0"
    assert command.judge_output["conclusion"] == "pass"


async def test_llm_judge_output_rejects_unknown_schema_version() -> None:
    repository = RecordingRuleExecutionRepository(
        executor_type="llm",
        manual_review_policy={"min_confidence": 0.75},
    )
    service = RuleExecutionService(repository=repository)

    with pytest.raises(RuleExecutionValidationError, match="schema_version"):
        await service.record_rule_execution(
            RecordRuleExecutionCommand(
                task_id="task-1",
                rule_code="alioth_4",
                execution_status="pass",
                page_capture_id="capture-1",
                confidence=0.9,
                evidence_artifact_ids=["artifact-1"],
                judge_input={"observation_summary": "页面文案未发现错别字"},
                judge_output={
                    "schema_version": "2.0",
                    "conclusion": "pass",
                    "reasoning_summary": "可见文案符合规则要求",
                },
            )
        )

    assert repository.commands == []


async def test_ai_conclusion_rejects_missing_min_confidence_policy() -> None:
    repository = RecordingRuleExecutionRepository(executor_type="llm")
    service = RuleExecutionService(repository=repository)

    with pytest.raises(
        RuleExecutionValidationError,
        match="manual_review_policy 缺少 min_confidence",
    ):
        await service.record_rule_execution(
            RecordRuleExecutionCommand(
                task_id="task-1",
                rule_code="alioth_4",
                execution_status="pass",
                page_capture_id="capture-1",
                confidence=0.9,
                evidence_artifact_ids=["artifact-1"],
                judge_input={"observation_summary": "页面文案未发现错别字"},
                judge_output={
                    "schema_version": "1.0",
                    "conclusion": "pass",
                    "reasoning_summary": "可见文案符合规则要求",
                },
            )
        )

    assert repository.commands == []


@pytest.mark.parametrize("tool_results", [None, []])
async def test_hybrid_conclusion_without_tool_results_is_persisted_for_review(
    tool_results: list[dict] | None,
) -> None:
    repository = RecordingRuleExecutionRepository(
        executor_type="hybrid",
        manual_review_policy={"min_confidence": 0.82},
    )
    service = RuleExecutionService(repository=repository)

    judge_output = {
        "schema_version": "1.0",
        "conclusion": "fail",
        "reasoning_summary": "结构化证据与视觉复核均发现异常",
    }
    if tool_results is not None:
        judge_output["tool_results"] = tool_results

    await service.record_rule_execution(
        RecordRuleExecutionCommand(
            task_id="task-1",
            rule_code="alioth_3",
            execution_status="fail",
            page_capture_id="capture-1",
            confidence=0.9,
            evidence_artifact_ids=["artifact-1"],
            judge_input={"observation_summary": "图片出现明显拉伸"},
            judge_output=judge_output,
        )
    )

    command = repository.commands[0]
    assert command.execution_status == "manual_review"
    assert command.confidence == 0.0
    assert command.review_status == ReviewStatus.NEEDS_RECHECK
    assert command.judge_output["conclusion"] == "manual_review"
    assert command.judge_output["proposed_conclusion"] == "fail"
    assert command.judge_output["hybrid_fallback_reason"] == "missing_tool_results"
    assert command.judge_output["tool_results"][0]["status"] == "unavailable"


async def test_hybrid_conclusion_rejects_malformed_tool_results() -> None:
    repository = RecordingRuleExecutionRepository(
        executor_type="hybrid",
        manual_review_policy={"min_confidence": 0.82},
    )
    service = RuleExecutionService(repository=repository)

    with pytest.raises(RuleExecutionValidationError, match="只能包含对象"):
        await service.record_rule_execution(
            RecordRuleExecutionCommand(
                task_id="task-1",
                rule_code="alioth_3",
                execution_status="fail",
                page_capture_id="capture-1",
                confidence=0.9,
                evidence_artifact_ids=["artifact-1"],
                judge_input={"observation_summary": "图片出现明显拉伸"},
                judge_output={
                    "schema_version": "1.0",
                    "conclusion": "fail",
                    "reasoning_summary": "结构化证据与视觉复核均发现异常",
                    "tool_results": ["invalid"],
                },
            )
        )

    assert repository.commands == []


async def test_low_confidence_ai_conclusion_is_normalized_to_manual_review() -> None:
    repository = RecordingRuleExecutionRepository(
        executor_type="hybrid",
        manual_review_policy={"min_confidence": 0.82},
    )
    service = RuleExecutionService(repository=repository)

    result = await service.record_rule_execution(
        RecordRuleExecutionCommand(
            task_id="task-1",
            rule_code="alioth_3",
            execution_status="fail",
            page_capture_id="capture-1",
            confidence=0.7,
            result_summary="图片疑似拉伸",
            evidence_artifact_ids=["artifact-1"],
            judge_input={"observation_summary": "图片比例存在疑点"},
            judge_output={
                "schema_version": "1.0",
                "conclusion": "fail",
                "reasoning_summary": "截图复核存在异常，但证据不充分",
                "tool_results": [
                    {"tool_name": "screenshot_compare_tool", "status": "ok"}
                ],
            },
        )
    )

    command = repository.commands[0]
    assert result.execution_status == "manual_review"
    assert command.execution_status == "manual_review"
    assert command.review_status == ReviewStatus.NEEDS_RECHECK
    assert command.review_reason == "判定置信度 0.7000 低于规则阈值 0.8200"
    assert command.judge_output["conclusion"] == "manual_review"
    assert command.judge_output["proposed_conclusion"] == "fail"
    assert command.judge_output["confidence_threshold"] == 0.82


async def test_missing_ai_confidence_is_conservatively_persisted_for_review() -> None:
    repository = RecordingRuleExecutionRepository(
        executor_type="llm",
        manual_review_policy={"min_confidence": 0.75},
    )
    service = RuleExecutionService(repository=repository)

    await service.record_rule_execution(
        RecordRuleExecutionCommand(
            task_id="task-1",
            rule_code="alioth_4",
            execution_status="pass",
            page_capture_id="capture-1",
            evidence_artifact_ids=["artifact-1"],
            judge_input={"observation_summary": "页面文案未发现错别字"},
            judge_output={
                "schema_version": "1.0",
                "conclusion": "pass",
                "reasoning_summary": "可见文案符合规则要求",
            },
        )
    )

    command = repository.commands[0]
    assert command.execution_status == "manual_review"
    assert command.confidence == 0.0
    assert command.review_status == ReviewStatus.NEEDS_RECHECK
    assert command.judge_output["conclusion"] == "manual_review"
    assert command.judge_output["proposed_conclusion"] == "pass"
    assert command.judge_output["confidence_fallback_reason"] == "missing_confidence"


@pytest.mark.parametrize("blocked_key", ["dom_snapshot", "screenshot_base64"])
async def test_ai_judge_payload_rejects_raw_heavy_artifacts(blocked_key: str) -> None:
    repository = RecordingRuleExecutionRepository(
        executor_type="llm",
        manual_review_policy={"min_confidence": 0.75},
    )
    service = RuleExecutionService(repository=repository)

    with pytest.raises(RuleExecutionValidationError, match="禁止字段"):
        await service.record_rule_execution(
            RecordRuleExecutionCommand(
                task_id="task-1",
                rule_code="alioth_4",
                execution_status="pass",
                page_capture_id="capture-1",
                confidence=0.9,
                evidence_artifact_ids=["artifact-1"],
                judge_input={
                    "observation_summary": "页面文案正常",
                    blocked_key: "raw-content",
                },
                judge_output={
                    "schema_version": "1.0",
                    "conclusion": "pass",
                    "reasoning_summary": "未发现异常",
                },
            )
        )

    assert repository.commands == []


async def test_ai_judge_payload_rejects_oversized_text() -> None:
    repository = RecordingRuleExecutionRepository(
        executor_type="llm",
        manual_review_policy={"min_confidence": 0.75},
    )
    service = RuleExecutionService(repository=repository)

    with pytest.raises(RuleExecutionValidationError, match="超过 16384 字节"):
        await service.record_rule_execution(
            RecordRuleExecutionCommand(
                task_id="task-1",
                rule_code="alioth_4",
                execution_status="pass",
                page_capture_id="capture-1",
                confidence=0.9,
                evidence_artifact_ids=["artifact-1"],
                judge_input={"observation_summary": "x" * 17000},
                judge_output={
                    "schema_version": "1.0",
                    "conclusion": "pass",
                    "reasoning_summary": "未发现异常",
                },
            )
        )

    assert repository.commands == []
