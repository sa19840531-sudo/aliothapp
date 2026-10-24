"""收口后确定性补判通道测试。"""

from typing import Any

from services.api.app.application.h5_posthoc_engine_judge import (
    H5PosthocActionPair,
    H5PosthocEngineJudgeService,
    H5PosthocTargetRule,
)


class FakeEvidenceSource:
    def __init__(
        self,
        *,
        h5: bool = True,
        targets: list[H5PosthocTargetRule] | None = None,
        pairs: list[H5PosthocActionPair] | None = None,
    ) -> None:
        self._h5 = h5
        self._targets = targets or []
        self._pairs = pairs or []

    async def is_h5_full_inspection(self, *, task_id: str) -> bool:
        return self._h5

    async def load_target_rules(self, *, task_id: str) -> list[H5PosthocTargetRule]:
        return self._targets

    async def load_action_pairs(self, *, task_id: str) -> list[H5PosthocActionPair]:
        return self._pairs


class FakeJudge:
    def __init__(self, outcomes: list[dict[str, Any] | Exception] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._outcomes = outcomes or []

    async def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self._outcomes:
            outcome = self._outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return {"execution_status": "pass"}


def _popup_rule(*, states: tuple[str, ...] = ()) -> H5PosthocTargetRule:
    return H5PosthocTargetRule(
        rule_code="alioth_20",
        applicable_page_types=("home", "activity"),
        required_page_states=states,
        before_role="before",
        after_role="primary",
    )


def _pair(
    *,
    action_id: str = "action-1",
    before: str = "capture-before",
    after: str = "capture-after",
    page_types: tuple[str, ...] = ("home",),
    page_states: tuple[str, ...] = ("popup",),
) -> H5PosthocActionPair:
    return H5PosthocActionPair(
        action_execution_id=action_id,
        before_capture_id=before,
        after_capture_id=after,
        page_types=page_types,
        page_states=page_states,
    )


async def test_posthoc_judge_maps_contract_roles_to_paired_captures() -> None:
    judge = FakeJudge()
    service = H5PosthocEngineJudgeService(
        evidence_source=FakeEvidenceSource(targets=[_popup_rule()], pairs=[_pair()]),
        judge_and_record=judge,
    )

    summary = await service.judge_pending(task_id="task-1")

    assert summary.target_rule_count == 1
    assert summary.judged_pair_count == 1
    assert summary.covered_rule_codes == ("alioth_20",)
    call = judge.calls[0]
    assert call["rule_code"] == "alioth_20"
    assert call["page_capture_id"] == "capture-after"
    assert call["evidence_capture_roles"] == {
        "before": "capture-before",
        "primary": "capture-after",
    }


async def test_posthoc_judge_skips_non_h5_task_and_unmatched_pages() -> None:
    judge = FakeJudge()
    non_h5 = H5PosthocEngineJudgeService(
        evidence_source=FakeEvidenceSource(h5=False, targets=[_popup_rule()], pairs=[_pair()]),
        judge_and_record=judge,
    )
    assert (await non_h5.judge_pending(task_id="task-1")).judged_pair_count == 0

    unmatched = H5PosthocEngineJudgeService(
        evidence_source=FakeEvidenceSource(
            targets=[_popup_rule()],
            pairs=[_pair(page_types=("video_detail",))],
        ),
        judge_and_record=judge,
    )
    assert (await unmatched.judge_pending(task_id="task-1")).judged_pair_count == 0
    assert judge.calls == []


async def test_posthoc_judge_enforces_required_page_states() -> None:
    judge = FakeJudge()
    service = H5PosthocEngineJudgeService(
        evidence_source=FakeEvidenceSource(
            targets=[_popup_rule(states=("popup", "modal", "overlay", "dialog"))],
            pairs=[
                _pair(action_id="action-stable", page_states=("stable",)),
                _pair(action_id="action-popup", page_states=("stable", "popup")),
            ],
        ),
        judge_and_record=judge,
    )

    summary = await service.judge_pending(task_id="task-1")

    assert summary.judged_pair_count == 1
    assert judge.calls[0]["evidence_capture_roles"]["before"] == "capture-before"
    assert summary.covered_rule_codes == ("alioth_20",)


async def test_posthoc_judge_tries_next_pair_until_definite_outcome() -> None:
    judge = FakeJudge(
        outcomes=[
            {"execution_status": "evidence_insufficient"},
            {"execution_status": "fail"},
        ]
    )
    service = H5PosthocEngineJudgeService(
        evidence_source=FakeEvidenceSource(
            targets=[_popup_rule()],
            pairs=[_pair(action_id="a1"), _pair(action_id="a2", before="b2", after="c2")],
        ),
        judge_and_record=judge,
    )

    summary = await service.judge_pending(task_id="task-1")

    assert summary.judged_pair_count == 2
    assert summary.covered_rule_codes == ("alioth_20",)


async def test_posthoc_judge_swallows_single_pair_failure() -> None:
    judge = FakeJudge(outcomes=[RuntimeError("boom"), {"execution_status": "pass"}])
    service = H5PosthocEngineJudgeService(
        evidence_source=FakeEvidenceSource(
            targets=[_popup_rule()],
            pairs=[_pair(action_id="a1"), _pair(action_id="a2", before="b2", after="c2")],
        ),
        judge_and_record=judge,
    )

    summary = await service.judge_pending(task_id="task-1")

    assert summary.judged_pair_count == 1
    assert summary.covered_rule_codes == ("alioth_20",)
