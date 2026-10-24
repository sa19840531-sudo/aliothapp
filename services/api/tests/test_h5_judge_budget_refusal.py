"""物理预算拒绝不是供应商故障，不能拆单重试或污染后续页面熔断。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agent_framework.exceptions import ChatClientException

from services.api.app.application.h5_llm_budget import H5LLMBudgetExhausted
from services.api.app.infrastructure.llm_gateway.semantic_rule_judge import LLMSemanticRuleJudge
from services.api.tests.test_semantic_rule_judge import _h5_packet, _packet


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("wrapped", [False, True])
async def test_h5_judge_preserves_budget_refusal_without_fallback_or_circuit(batch, wrapped):
    error = H5LLMBudgetExhausted()
    if wrapped:
        wrapper = ChatClientException("provider wrapper")
        wrapper.__cause__ = error
        error = wrapper
    client = type("Client", (), {"get_response": AsyncMock(side_effect=error)})()
    judge = LLMSemanticRuleJudge(client=client)
    with pytest.raises(H5LLMBudgetExhausted):
        if batch:
            await judge.judge_batch(items=[(_h5_packet("one"), []), (_h5_packet("two"), [])])
        else:
            await judge.judge(packet=_h5_packet("one"), tool_results=[])
    assert client.get_response.await_count == 1
    assert not judge._consecutive_timeouts
    assert not judge._result_cache


async def test_web_provider_failure_contract_remains_manual_review():
    client = type(
        "Client",
        (),
        {
            "get_response": AsyncMock(
                side_effect=ChatClientException("provider unavailable"),
            )
        },
    )()
    result = await LLMSemanticRuleJudge(client=client).judge(packet=_packet(), tool_results=[])
    assert result.conclusion == "manual_review"


async def test_budget_refusal_keeps_valid_same_evidence_judge_cache():
    client = type(
        "Client",
        (),
        {
            "get_response": AsyncMock(
                side_effect=[
                    SimpleNamespace(
                        value={
                            "schema_version": "semantic-rule-judge.v1",
                            "conclusion": "pass",
                            "confidence": 0.96,
                            "reasoning_summary": "当前证据满足规则。",
                            "evidence_refs": ["shot-1"],
                        }
                    ),
                    H5LLMBudgetExhausted(),
                ]
            )
        },
    )()
    judge = LLMSemanticRuleJudge(client=client)
    packet = _h5_packet("completed")
    assert (await judge.judge(packet=packet, tool_results=[])).conclusion == "pass"
    with pytest.raises(H5LLMBudgetExhausted):
        await judge.judge(packet=_h5_packet("uncompleted"), tool_results=[])
    assert (await judge.judge(packet=packet, tool_results=[])).conclusion == "pass"
    assert client.get_response.await_count == 2
    assert len(judge._result_cache) == 1
    assert not judge._provider_circuits
