"""跨入口分类复用必须绕过重复模型调用与通知，不冒称已发布。"""

from dataclasses import asdict
from types import SimpleNamespace

import pytest

from services.api.app.application.web_page_classification import WebPageAssessment


@pytest.mark.asyncio
async def test_distinct_captures_of_materialized_target_reuse_assessment():
    from services.api.app.application.web_logical_target import WebLogicalTargetClassifier

    class Repository:
        value = None

        async def claim(self, **kwargs):
            if self.value is not None:
                return dict(self.value, created=False)
            return {"created": True, "state": "claimed"}

        async def materialize(self, **kwargs):
            self.value = {
                "state": "materialized",
                "assessment": kwargs["assessment"],
                "assessment_capture_id": kwargs["capture_id"],
                "logical_page_id": "web-logical:" + "1" * 64,
            }

    class Classifier:
        calls = 0

        async def classify(self, capture):
            self.calls += 1
            if self.calls != 1:
                raise AssertionError("重复入口不能再次调用分类或发布链")
            return WebPageAssessment(
                page_type="content_detail", confidence=0.95, evidence=("自然正文",)
            )

    classifier = Classifier()
    wrapper = WebLogicalTargetClassifier(
        classifier=classifier,
        repository=Repository(),
        task_id="t",
        agent_run_id="r",
        session_id="web:r",
    )
    first = await wrapper.classify(
        SimpleNamespace(capture_id="first", url="https://example.test/#/detail/1")
    )
    second = await wrapper.classify(
        SimpleNamespace(capture_id="second", url="https://example.test/#/detail/1?tab=info")
    )
    assert first.page_type == second.page_type == "content_detail"
    assert first.reused_capture_id is None
    assert second.reused_capture_id == "first"
    assert classifier.calls == 1


@pytest.mark.asyncio
async def test_incomplete_claim_never_replays_classifier():
    from services.api.app.application.web_logical_target import WebLogicalTargetClassifier

    class Repository:
        async def claim(self, **kwargs):
            return {"created": False, "state": "claimed", "assessment": None}

    wrapper = WebLogicalTargetClassifier(
        classifier=object(),
        repository=Repository(),
        task_id="t",
        agent_run_id="r",
        session_id="web:r",
    )
    with pytest.raises(ValueError, match="WEB_LOGICAL_TARGET_INCOMPLETE"):
        await wrapper.classify(
            SimpleNamespace(capture_id="after", url="https://example.test/#/detail/1")
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_fails", [False, True])
async def test_primary_capture_attempt_is_reused_without_another_action(finish_fails):
    import json

    from services.api.app.application.web_interaction_precision import WebInteractionOutcome
    from services.api.app.application.web_logical_target import WebLogicalTargetPrimaryCapture
    from services.api.app.application.web_primary_content_capture import (
        WebPrimaryContentCaptureResult,
    )

    class Repository:
        result = None

        async def claim_primary(self, **kwargs):
            return {"created": self.result is None, "primary_result": self.result}

        async def finish_primary(self, **kwargs):
            if finish_fails:
                raise RuntimeError("持久化失败正文不得透传")
            self.result = json.loads(json.dumps(kwargs["result"]))

    class Capture:
        calls = 0

        async def capture(self, **kwargs):
            self.calls += 1
            assert self.calls == 1
            return WebPrimaryContentCaptureResult(
                status="captured",
                parent_capture_id="first",
                evidence_capture_id="primary",
                logical_page_id="web-logical:" + "1" * 64,
                target_candidate_id="sealed",
                scroll_count=1,
                before_ad_coverage=0.7,
                after_ad_coverage=0.1,
                interaction_outcomes=(WebInteractionOutcome(
                    command_accepted=True, effect_observed=True,
                    postcondition_satisfied=True, transition_arrived=True,
                    after_capture_id="primary", failure_reason=None,
                ),),
                attempts=({"action_execution_id": "scroll-1", "before_capture_id": "fresh-before", "after_capture_id": "primary"},),
            )

    service = WebLogicalTargetPrimaryCapture(service=Capture(), repository=Repository())
    kwargs = dict(
        task_id="t",
        agent_run_id="r",
        session_id="web:r",
        capture_id="first",
        page_url="https://example.test/#/detail/1",
    )
    first = await service.capture(**kwargs)
    if finish_fails:
        assert first.status == "failed"
        assert first.reason_code == "WEB_PRIMARY_CONTENT_PERSISTENCE_FAILED"
        assert first.evidence_capture_id is None
        assert first.attempts[0]["after_capture_id"] == "primary"
        assert first.interaction_outcomes[0].after_capture_id == "primary"
        return
    second = await service.capture(**dict(kwargs, capture_id="other"))
    assert first.evidence_capture_id == second.evidence_capture_id == "primary"
    assert second.scroll_count == 0
    assert second.interaction_outcomes == first.interaction_outcomes
    assert second.attempts == first.attempts
    assert second.reused is True
    assert second.original_scroll_count == 1


def test_reuse_is_a_distinct_terminal_with_no_fabricated_after_capture():
    from services.api.app.application.web_list_detail_navigation import WebListDetailResult
    from services.api.app.domain.exploration import WebMenuTerminalStatus

    result = WebListDetailResult(
        status=WebMenuTerminalStatus.LOGICAL_TARGET_REUSED,
        detail_capture_id=None,
        page_type="content_detail",
        reused_capture_id="original",
        reused_logical_page_id="web-logical:" + "1" * 64,
    )
    assert result.detail_capture_id is None
    assert result.detail_action_count == 0
    assert result.reused_capture_id == "original"


@pytest.mark.asyncio
async def test_one_primary_supplement_can_refine_unclassified_material_once():
    from services.api.app.application.web_logical_target import WebLogicalTargetClassifier

    class Repository:
        async def claim(self, **kwargs):
            return {
                "created": False,
                "state": "materialized",
                "assessment": asdict(
                    WebPageAssessment(page_type=None, confidence=0.3, evidence=("待补证",))
                ),
                "assessment_capture_id": "baseline",
                "primary_capture_id": "supplement",
                "logical_page_id": "web-logical:" + "1" * 64,
            }

        async def claim_refinement(self, **kwargs):
            return True

        async def materialize(self, **kwargs):
            self.saved = kwargs["assessment"]

    class Classifier:
        async def classify(self, capture):
            return WebPageAssessment(
                page_type="content_detail", confidence=0.95, evidence=("补证自然正文",)
            )

    repo = Repository()
    service = WebLogicalTargetClassifier(
        classifier=Classifier(), repository=repo, task_id="t", agent_run_id="r", session_id="web:r"
    )
    result = await service.classify(
        SimpleNamespace(
            capture_id="supplement",
            capture_purpose="web_primary_content_evidence",
            url="https://example.test/#/detail/1",
        )
    )
    assert result.page_type == "content_detail"
    assert result.reused_capture_id is None
    assert repo.saved["page_type"] == "content_detail"
