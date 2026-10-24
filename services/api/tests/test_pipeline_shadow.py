"""测试质检候选流水线影子抽样与准确率门禁。"""

from services.api.app.application.pipeline_shadow import (
    PipelineShadowService,
    ShadowComparison,
    evaluate_shadow_gate,
    should_sample_shadow,
)


def test_shadow_sampling_is_stable_for_same_task() -> None:
    first = should_sample_shadow(task_id="task-1", sample_rate=0.10)
    assert should_sample_shadow(task_id="task-1", sample_rate=0.10) is first
    assert should_sample_shadow(task_id="task-1", sample_rate=0.0) is False
    assert should_sample_shadow(task_id="task-1", sample_rate=1.0) is True


def test_shadow_gate_requires_at_least_one_hundred_replay_rows() -> None:
    rows = [
        {"legacy_conclusion": "pass", "candidate_conclusion": "pass"}
        for _ in range(99)
    ]

    gate = evaluate_shadow_gate(rows)

    assert gate["passed"] is False
    assert gate["sample_count"] == 99
    assert gate["minimum_sample_count"] == 100


def test_shadow_gate_rejects_new_high_false_pass() -> None:
    rows = [
        {
            "severity": "high",
            "legacy_conclusion": "fail",
            "candidate_conclusion": "pass",
        },
        *[
            {
                "severity": "low",
                "legacy_conclusion": "pass",
                "candidate_conclusion": "pass",
            }
            for _ in range(99)
        ],
    ]

    gate = evaluate_shadow_gate(rows)

    assert gate["passed"] is False
    assert gate["critical_high_false_pass"] == 1
    assert gate["overall_agreement"] == 0.99


async def test_shadow_summary_drops_page_content_and_data_urls() -> None:
    class Repository:
        comparison = None

        async def save_comparison(self, *, comparison, difference_type):
            self.comparison = comparison
            assert difference_type == "match"
            return "comparison-1"

    repository = Repository()
    service = PipelineShadowService(repository=repository)
    await service.record(
        ShadowComparison(
            task_id="task-1",
            comparison_mode="historical_replay",
            legacy_conclusion="pass",
            candidate_conclusion="pass",
            summary={
                "dom": "<html>敏感正文</html>",
                "preview": "data:image/png;base64,secret",
                "candidate_status": "ok",
            },
        )
    )

    assert repository.comparison.summary == {
        "preview": "[data_url_redacted]",
        "candidate_status": "ok",
    }
