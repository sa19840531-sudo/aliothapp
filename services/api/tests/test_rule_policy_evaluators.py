"""测试价格一致性、防刷与内容生命周期确定性求值器。"""

import pytest

from services.api.app.application.rule_handler_evaluators import HandlerInputError
from services.api.app.application.rule_policy_evaluators import (
    evaluate_content_lifecycle,
    evaluate_price_relation,
    evaluate_rate_limit,
    validate_content_lifecycle_config,
    validate_price_relation_config,
    validate_rate_limit_config,
)


def test_price_relation_passes_same_product_across_locations() -> None:
    result = evaluate_price_relation(
        evidence_by_role={
            "primary": {"visible_text": "黄金月卡 价格 ¥19.90"},
            "location_1": {"visible_text": "黄金月卡 支付金额 ¥19.90"},
        },
        config={
            "price_pattern": r"(?P<product>黄金月卡).*?¥(?P<price>\d+(?:\.\d+)?)",
        },
    )

    assert result.passed is True
    assert result.judge_output["products"][0]["spread"] == 0.0


def test_price_relation_flags_cross_location_difference() -> None:
    result = evaluate_price_relation(
        evidence_by_role={
            "primary": {"visible_text": "黄金月卡 价格 ¥19.90"},
            "location_1": {"visible_text": "黄金月卡 支付金额 ¥29.90"},
        },
        config={
            "price_pattern": r"(?P<product>黄金月卡).*?¥(?P<price>\d+(?:\.\d+)?)",
            "tolerance": 0.01,
        },
    )

    assert result.passed is False
    assert result.judge_output["violations"][0]["spread"] == 10.0


def test_price_relation_rejects_without_shared_product() -> None:
    with pytest.raises(HandlerInputError, match="跨位置同商品价格组"):
        evaluate_price_relation(
            evidence_by_role={
                "primary": {"visible_text": "黄金月卡 ¥19.90"},
                "location_1": {"visible_text": "白银月卡 ¥9.90"},
            },
            config={
                "price_pattern": (
                    r"(?P<product>[黄金白银]+月卡)\s*¥(?P<price>\d+(?:\.\d+)?)"
                ),
            },
        )


def test_price_relation_rejects_non_finite_price() -> None:
    with pytest.raises(HandlerInputError, match="price_relation.price 必须为有限数值"):
        evaluate_price_relation(
            evidence_by_role={
                "primary": {"visible_text": "黄金月卡 ¥nan"},
                "location_1": {"visible_text": "黄金月卡 ¥nan"},
            },
            config={
                "price_pattern": r"(?P<product>黄金月卡)\s*¥(?P<price>\S+)",
            },
        )


def test_price_relation_ignores_price_labels_that_are_not_product_names() -> None:
    """真实档位页每档都有「原价：¥xxx」：标签不是商品名，各档原价不同不能算价格不一致。"""
    tier_page = (
        "AI+暗网卡\n50\n原价：¥599\n\n5周年庆卡\n¥100\n原价：¥800\n\n永久卡\n¥30\n原价：¥8\n购买会员"
    )
    config = {
        "price_pattern": (
            r"(?im)(?P<product>[^\n]{1,80}?)\s*(?:¥|￥|RMB\s*)(?P<price>\d{1,9}(?:\.\d{1,2})?)"
        ),
    }

    same = evaluate_price_relation(
        evidence_by_role={
            "primary": {"visible_text": tier_page},
            "location_1": {"visible_text": tier_page},
        },
        config=config,
    )
    assert same.passed is True
    assert sorted(item["product"] for item in same.judge_output["products"]) == [
        "5周年庆卡",
        "永久卡",
    ]

    different = evaluate_price_relation(
        evidence_by_role={
            "primary": {"visible_text": tier_page},
            "location_1": {"visible_text": "开通VIP观看完整版\n永久卡 ¥35\n立即开通"},
        },
        config=config,
    )
    assert different.passed is False
    assert [item["product"] for item in different.judge_output["violations"]] == ["永久卡"]


def test_rate_limit_passes_explicit_limit_and_three_visible_repeats() -> None:
    evidence = {
        f"attempt_{index}": _rate_attempt(
            visible_text=" ".join(["ALIOTH_RATE_PROBE"] * min(index, 3)),
            status=200,
        )
        for index in range(1, 4)
    }
    evidence["primary"] = _rate_attempt(
        visible_text="ALIOTH_RATE_PROBE " * 3 + "操作过于频繁",
        status=429,
    )

    result = evaluate_rate_limit(
        evidence_by_role=evidence,
        config=_rate_config(),
    )

    assert result.passed is True
    assert result.judge_output["explicit_limit_signal"] is True
    assert result.judge_output["visible_probe_repeats"] == 3


def test_rate_limit_flags_four_repeats_without_protection() -> None:
    evidence = {
        f"attempt_{index}": _rate_attempt(
            visible_text=" ".join(["ALIOTH_RATE_PROBE"] * index),
            status=200,
        )
        for index in range(1, 4)
    }
    evidence["primary"] = _rate_attempt(
        visible_text="ALIOTH_RATE_PROBE " * 4,
        status=200,
    )

    result = evaluate_rate_limit(
        evidence_by_role=evidence,
        config=_rate_config(),
    )

    assert result.passed is False
    assert result.judge_output["repeat_cap_ok"] is False
    assert result.judge_output["explicit_limit_signal"] is False


def test_rate_limit_rejects_malformed_har_contract() -> None:
    evidence = {
        f"attempt_{index}": _rate_attempt(
            visible_text="ALIOTH_RATE_PROBE",
            status=200,
        )
        for index in range(1, 4)
    }
    evidence["attempt_2"]["har"] = {
        "format": "network_events",
        "entries": [{"event": "response", "status": True}],
    }
    evidence["primary"] = _rate_attempt(
        visible_text="ALIOTH_RATE_PROBE",
        status=429,
    )

    with pytest.raises(HandlerInputError, match="response status 必须为整数"):
        evaluate_rate_limit(
            evidence_by_role=evidence,
            config=_rate_config(),
        )


def test_rate_limit_without_network_record_reads_limit_prompt_only_when_allowed() -> None:
    """App 采不到网络记录：放开后只看页面上的限频提示；H5 / Web 不放开，缺 har 仍拒收。"""

    def attempts(final_text: str) -> dict:
        evidence = {
            f"attempt_{index}": _rate_attempt(visible_text="ALIOTH_RATE_PROBE", status=200)
            for index in range(1, 4)
        }
        evidence["primary"] = _rate_attempt(visible_text=final_text, status=200)
        for item in evidence.values():
            del item["har"]
        return evidence

    limited = evaluate_rate_limit(
        evidence_by_role=attempts("ALIOTH_RATE_PROBE 操作过于频繁"),
        config=_rate_config(),
        network_optional=True,
    )
    assert limited.passed is True
    assert limited.judge_output["explicit_limit_signal"] is True
    assert all(
        item["network_recorded"] is False and item["response_statuses"] == []
        for item in limited.judge_output["attempts"]
    )

    unprotected = evaluate_rate_limit(
        evidence_by_role=attempts("ALIOTH_RATE_PROBE " * 4),
        config=_rate_config(),
        network_optional=True,
    )
    assert unprotected.passed is False

    with pytest.raises(HandlerInputError, match="har 必须为 network_events 对象"):
        evaluate_rate_limit(
            evidence_by_role=attempts("ALIOTH_RATE_PROBE 操作过于频繁"),
            config=_rate_config(),
        )


def test_content_lifecycle_passes_hidden_and_marked_consumers() -> None:
    result = evaluate_content_lifecycle(
        evidence_by_role={
            "source": {"visible_text": "ALIOTH_OFFLINE_VIDEO 状态 已下架"},
            "primary": {"visible_text": "其他视频列表"},
            "entry_1": {"visible_text": "ALIOTH_OFFLINE_VIDEO 已下架"},
        },
        config=_lifecycle_config(min_consumer_captures=2),
    )

    assert result.passed is True
    assert [item["passed"] for item in result.judge_output["consumers"]] == [
        True,
        True,
    ]


def test_content_lifecycle_flags_unmarked_visible_entity() -> None:
    result = evaluate_content_lifecycle(
        evidence_by_role={
            "source": {"visible_text": "ALIOTH_OFFLINE_VIDEO 状态 已下架"},
            "primary": {"visible_text": "ALIOTH_OFFLINE_VIDEO 立即下载"},
        },
        config=_lifecycle_config(),
    )

    assert result.passed is False
    assert result.judge_output["failed_roles"] == ["primary"]


def test_content_lifecycle_rejects_without_inactive_source_precondition() -> None:
    with pytest.raises(HandlerInputError, match="来源采集未证明"):
        evaluate_content_lifecycle(
            evidence_by_role={
                "source": {"visible_text": "ALIOTH_OFFLINE_VIDEO 状态 正常"},
                "primary": {"visible_text": "其他视频列表"},
            },
            config=_lifecycle_config(),
        )


def test_policy_handler_config_validation() -> None:
    assert (
        validate_price_relation_config({"price_pattern": r"商品\s*(\d+)"})
        == "price_relation.price_pattern 必须包含 product 与 price 命名捕获组"
    )
    assert (
        validate_rate_limit_config(
            {
                "min_attempts": 3,
                "probe_content_pattern": "PROBE",
                "action_pattern": "submit",
            }
        )
        == "rate_limit_assertion.min_attempts 必须在 4 到 20 之间"
    )
    assert (
        validate_content_lifecycle_config({})
        == "content_lifecycle_state.source_inactive_pattern 必须配置"
    )
    assert (
        validate_price_relation_config(
            {
                "price_pattern": r"(?P<product>月卡).*?(?P<price>\d+)",
                "location_role_pattern": r"^location_\d+$",
            }
        )
        == "price_relation.location_role_pattern 必须匹配 primary"
    )
    assert (
        validate_content_lifecycle_config(
            {
                "source_role": "primary",
                "source_inactive_pattern": "已下架",
                "consumer_entity_pattern": "作品",
                "consumer_marked_pattern": "作品.*已下架",
            }
        )
        == "content_lifecycle_state.source_role 不能使用 primary"
    )


def _rate_attempt(*, visible_text: str, status: int) -> dict:
    return {
        "visible_text": visible_text,
        "action_trace": [{"action": "click", "selector": "button.submit"}],
        "har": {
            "format": "network_events",
            "entries": [{"event": "response", "status": status}],
        },
    }


def _rate_config() -> dict:
    return {
        "action_pattern": r"button\.submit",
        "probe_content_pattern": "ALIOTH_RATE_PROBE",
        "rate_limit_text_pattern": "操作过于频繁",
        "min_attempts": 4,
        "max_visible_repeats": 3,
        "rate_limit_statuses": [429],
    }


def _lifecycle_config(*, min_consumer_captures: int = 1) -> dict:
    return {
        "source_inactive_pattern": r"ALIOTH_OFFLINE_VIDEO.*已下架",
        "consumer_entity_pattern": "ALIOTH_OFFLINE_VIDEO",
        "consumer_marked_pattern": r"ALIOTH_OFFLINE_VIDEO.*已下架",
        "expected_consumer_state": "hidden_or_marked",
        "min_consumer_captures": min_consumer_captures,
    }
