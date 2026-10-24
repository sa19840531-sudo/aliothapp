from services.api.app.application.rule_batching import (
    MAX_RULE_JUDGE_BATCH_SIZE,
    build_rule_execution_batch_key,
)


def test_rule_engine_batch_key_crosses_categories_but_not_sessions() -> None:
    snapshot_text = build_rule_execution_batch_key(
        strategy="rule_engine",
        category="text",
        session_dependency="snapshot",
    )
    snapshot_visual = build_rule_execution_batch_key(
        strategy="rule_engine",
        category="visual",
        session_dependency="snapshot",
    )
    active_interaction = build_rule_execution_batch_key(
        strategy="rule_engine",
        category="interaction",
        session_dependency="active_session",
    )

    assert snapshot_text == snapshot_visual
    assert snapshot_text.batch_id == "rule_engine:all:snapshot"
    assert snapshot_text.category is None
    assert active_interaction.batch_id == "rule_engine:all:active_session"
    assert active_interaction != snapshot_text


def test_hybrid_batch_key_keeps_category_and_shared_limit() -> None:
    visual = build_rule_execution_batch_key(
        strategy="hybrid",
        category="visual",
        session_dependency=None,
    )
    ad = build_rule_execution_batch_key(
        strategy="hybrid",
        category="ad",
        session_dependency=None,
    )

    assert visual.batch_id == "hybrid:visual:snapshot"
    assert visual != ad
    assert MAX_RULE_JUDGE_BATCH_SIZE == 20
