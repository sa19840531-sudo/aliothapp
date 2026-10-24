"""绿色页面规则沿原观察包与原件读取链执行，不接受调用方自报事实。"""

from services.api.app.application.rule_judge import (
    RuleJudgeEvidenceInsufficientError,
    RuleJudgeNotConfiguredError,
    RuleJudgeResult,
    RuleJudgeValidationError,
)


async def judge_green_page(
    *, definition, request, packet, contract, evidence_reader, semantic_judge=None,
):
    """仅处理第4版页面输入；过程输入必须由正式聚合消费者读取。"""
    if contract["input_kind"] != "page":
        raise RuleJudgeValidationError("绿色过程规则必须消费已封存的正式过程输入，不能用单页代替")
    if evidence_reader is None:
        raise RuleJudgeNotConfiguredError("绿色页面判定缺少原件读取器")
    from services.api.app.application.green_rule_evidence import green_page_preflight

    try:
        bundle = await evidence_reader.prepare_observation_packet(
            task_id=request.task_id,
            agent_run_id=request.agent_run_id,
            source_agent_run_id=request.source_agent_run_id,
            rule_id=definition.rule_id,
            page_capture_id=request.page_capture_id,
            evidence_source_page_capture_id=request.evidence_source_page_capture_id,
            evidence_manifest_id=request.evidence_manifest_id,
            evidence_capture_roles=request.evidence_capture_roles,
            evidence_manifest_refs=request.evidence_manifest_refs,
            retained_baseline_artifact_ids=request.retained_baseline_artifact_ids,
            evidence_artifact_ids=packet.evidence_artifact_ids,
            required_artifact_types=("screenshot",),
            frozen_observation_packet=packet,
        )
        from services.api.app.application.green_page_facts import page_fact_views, page_outcome

        views = await page_fact_views(
            bundle=bundle, definition=definition, request=request, packet=packet,
            evidence_reader=evidence_reader,
        )
        if contract["rule_key"] == "32" and views:
            from services.api.app.application.green_navigation_rules import judge_navigation_subitem

            outcome = page_outcome(
                tuple(judge_navigation_subitem(key, views) for key in contract["subchecks"]), views,
            )
        elif contract["rule_key"] == "32":
            from services.api.app.application.green_navigation_judge import judge_green_navigation

            outcome = judge_green_navigation(bundle)
        elif contract["rule_key"] == "41" and views:
            from services.api.app.application.green_external_link_rule import judge_external_link

            outcome = page_outcome((judge_external_link(views),), views)
        elif contract["rule_key"] == "102":
            from services.api.app.application.green_content_judge import judge_green_content

            outcome = await judge_green_content(
                bundle=bundle, definition=definition, packet=packet,
                views=views, semantic_judge=semantic_judge,
            )
        else:
            outcome = green_page_preflight(contract["rule_key"], bundle)
        artifacts = tuple(dict.fromkeys(item.artifact_id for item in bundle.artifacts))
    except RuleJudgeEvidenceInsufficientError:
        outcome = green_page_preflight(contract["rule_key"], None)
        artifacts = packet.evidence_artifact_ids
    status, reason = outcome["execution_status"], outcome["reason"]
    if status not in {"pass", "fail", "skipped", "evidence_insufficient", "manual_review"}:
        raise RuleJudgeValidationError("绿色页面判定返回了未声明状态")
    missing = list(outcome.get("missing_evidence", ()))
    subitems = outcome.get("subitems", [])
    if [item["key"] for item in subitems] != contract["subchecks"]:
        raise RuleJudgeValidationError("绿色页面分项与冻结合同不一致")
    # 未判分项保留各自缺项与空置信度，不复制总状态冒充三风险独立判读。
    allowed_refs = set(artifacts)
    subresults = []
    for item in subitems:
        refs = list(item.get("evidence_refs", artifacts if item["evaluated"] else ()))
        if any(not isinstance(ref, str) or ref not in allowed_refs for ref in refs):
            raise RuleJudgeValidationError("绿色页面分项引用了冻结原件之外的材料")
        subresult = {
            "name": item["key"],
            "status": item["status"],
            "reason": item["reason"],
            "evaluated": item["evaluated"],
            "confidence": item["confidence"],
            "missing_evidence": list(item["missing"]),
            "evidence_refs": list(dict.fromkeys(refs)),
        }
        if item["status"] == "skipped":
            subresult["applicability"] = "not_applicable"
        if item.get("observations"):
            subresult["observations"] = item["observations"]
        for field in ("policy_reference", "findings", "conflicts", "review_reason"):
            if field in item:
                subresult[field] = item[field]
        subresults.append(subresult)
    confidence = outcome.get("confidence", 0.0)
    if type(confidence) not in (int, float) or not 0 <= confidence <= 1:
        raise RuleJudgeValidationError("绿色页面判定置信度无效")
    evaluated = any(item["evaluated"] for item in subresults)
    # 这是固定消费器实际形成的审计结果，不注册为模型可调用的独立 Tool。
    tool_results = [
        {
            "tool_name": "green_rule_evidence_tool",
            "status": "ok" if evaluated else "insufficient",
            "rule_key": contract["rule_key"],
            "readonly": True,
            "subitems": [
                {
                    "name": item["name"],
                    "status": item["status"],
                    "evaluated": item["evaluated"],
                    "evidence_refs": item["evidence_refs"],
                }
                for item in subresults
            ],
        }
    ]
    reasoning = "；".join(
        dict.fromkeys(
            [
                reason,
                *(item["reason"] for item in subresults if item["status"] == "fail"),
            ]
        )
    )
    return RuleJudgeResult(
        rule_id=definition.rule_id,
        rule_code=definition.rule_code,
        executor_type=definition.executor_type,
        judge_strategy="hybrid",
        execution_status=status,
        confidence=confidence,
        result_summary=reason,
        problem_desc=None if status in {"pass", "skipped"} else reason,
        page_capture_id=request.page_capture_id,
        evidence_artifact_ids=artifacts,
        judge_input={"expected_contract_hash": request.expected_contract_hash},
        judge_output={
            "schema_version": "1.0",
            "conclusion": status,
            "reasoning_summary": reasoning,
            "tool_results": tool_results,
            "green_rule_contract": contract,
            "green_skill_program_sha256": definition.judge_config["green_skill_program"]["sha256"],
            "missing_evidence": missing,
            "review_reason": reason
            if status in {"manual_review", "evidence_insufficient"}
            else None,
            "subresults": subresults,
        },
        observation_packet_id=packet.observation_packet_id,
    )
