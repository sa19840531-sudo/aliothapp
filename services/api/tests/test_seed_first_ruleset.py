import json

import pytest

from scripts import seed_first_ruleset
from services.api.app.application.rule_judge import resolve_rule_execution_route
from services.api.app.domain.page.catalog import load_default_page_type_catalog


class RecordingConnection:
    def __init__(self) -> None:
        self.sql = ""
        self.args = ()

    async def fetchval(self, sql: str, *args: object) -> str:
        self.sql = sql
        self.args = args
        return "ruleset-id"

    async def execute(self, sql: str, *args: object) -> str:
        self.sql = sql
        self.args = args
        return "INSERT 0 1"


def test_first_ruleset_applicable_page_types_use_explicit_catalog() -> None:
    rule_rows = seed_first_ruleset._read_rows(seed_first_ruleset.RULE_CSV)
    page_type_catalog = load_default_page_type_catalog()
    catalog_page_types = {page_type.code for page_type in page_type_catalog.page_types}
    matchable_page_types = {
        page_type.code for page_type in page_type_catalog.page_types if page_type.rule_matchable
    }
    configured_page_types = {
        page_type
        for row in rule_rows
        for page_type in json.loads(row["applicable_page_types_json"])
    }

    assert len(rule_rows) == 73
    seed_first_ruleset._validate_rule_rows(rule_rows)
    assert catalog_page_types == seed_first_ruleset.ALLOWED_APPLICABLE_PAGE_TYPES
    assert configured_page_types == matchable_page_types
    assert all(
        not row["page_type"] or row["page_type"] in seed_first_ruleset.ALLOWED_APPLICABLE_PAGE_TYPES
        for row in rule_rows
    )


def test_toast_rule_declares_explicit_task_aggregate_scope() -> None:
    rows = {
        row["rule_code"]: row for row in seed_first_ruleset._read_rows(seed_first_ruleset.RULE_CSV)
    }

    config = json.loads(rows["alioth_spec_48"]["judge_config_json"])["capability_config"]

    assert config["evidence_input_scope"] == "task"
    assert config["evidence_scope_key"] == "toast_style"
    assert config["requires_journey"] is False


def test_home_popup_sequence_rule_is_a_task_aggregate() -> None:
    """首页弹窗连续展示要串起整条弹窗链才能判；按单页判只会在首页一律记不适用。

    08-19 接成聚合时只改了库没进种子，部署重播种子把它打回单页（09-22 问题表第 2、11 行）。
    """
    rows = {
        row["rule_code"]: row for row in seed_first_ruleset._read_rows(seed_first_ruleset.RULE_CSV)
    }

    config = json.loads(rows["alioth_22"]["judge_config_json"])["capability_config"]

    assert config["evidence_input_scope"] == "task"
    assert config["evidence_scope_key"] == "home_popup_sequence"


def test_former_universal_rules_are_distributed_by_page_type() -> None:
    rule_rows = {
        row["rule_code"]: row for row in seed_first_ruleset._read_rows(seed_first_ruleset.RULE_CSV)
    }
    # 0beb4e77（2026-08-24）：home/content_list 通用规则的适用范围扩展至
    # 视频站页型（video_list/video_detail），主页型与步骤语义保持不变。
    expected_scopes = {
        "alioth_2": ("content_list", "内容列表页", ["content_list", "video_list", "video_detail"]),
        "alioth_4": ("content_list", "内容列表页", ["content_list", "video_list", "video_detail"]),
        "alioth_5": ("home", "首页", ["home", "video_list"]),
        "alioth_6": ("home", "首页", ["home", "video_list"]),
        "alioth_7": ("activity", "活动页中的外部链接", ["activity"]),
        "alioth_16": ("startup_ad", "启动/开屏广告", ["startup_ad"]),
        "alioth_17": ("startup_ad", "启动/开屏广告", ["startup_ad"]),
        "alioth_28": ("post_list", "帖子列表页中的用户内容", ["post_list"]),
        "alioth_38": ("content_list", "内容列表页的分类标签", ["content_list", "video_list"]),
    }

    assert not any(
        set(json.loads(row["applicable_page_types_json"]))
        == seed_first_ruleset.ALLOWED_APPLICABLE_PAGE_TYPES
        for row in rule_rows.values()
    )
    for rule_code, (page_type, target, applicable_page_types) in expected_scopes.items():
        row = rule_rows[rule_code]
        steps = json.loads(row["steps_json"])

        assert row["page_type"] == page_type
        assert json.loads(row["applicable_page_types_json"]) == applicable_page_types
        assert steps == [
            {
                "action": "navigate",
                "target": target,
                "note": target,
                "expected_page_type": page_type,
            }
        ]


def test_popup_ad_rules_also_apply_to_activity_pages() -> None:
    rule_rows = {
        row["rule_code"]: row for row in seed_first_ruleset._read_rows(seed_first_ruleset.RULE_CSV)
    }

    for rule_code in ("alioth_20", "alioth_23"):
        # 0beb4e77：弹窗/兜底广告规则随视频站实测现象扩展至视频页型。
        assert json.loads(rule_rows[rule_code]["applicable_page_types_json"]) == [
            "home",
            "activity",
            "video_list",
            "video_detail",
        ]
    # 22 的「首页」语义=产品落地页：视频/漫画类产品以列表页为首页，
    # 弹窗墙实测出现在 video_list（如 /movie 路由），归属随现象扩展。
    assert json.loads(rule_rows["alioth_22"]["applicable_page_types_json"]) == [
        "home",
        "activity",
        "video_list",
        "content_list",
    ]


def test_validate_rule_rows_rejects_legacy_generic_page_type() -> None:
    with pytest.raises(RuntimeError, match="general"):
        seed_first_ruleset._validate_rule_rows(
            [
                {
                    "rule_code": "alioth_test",
                    "page_type": "",
                    "steps_json": "[]",
                    "applicable_page_types_json": '["general"]',
                }
            ]
        )


def test_validate_rule_rows_rejects_legacy_primary_and_step_page_type() -> None:
    with pytest.raises(RuntimeError, match="page_type=ad_popup"):
        seed_first_ruleset._validate_rule_rows(
            [
                {
                    "rule_code": "alioth_test",
                    "page_type": "ad_popup",
                    "steps_json": "[]",
                    "applicable_page_types_json": '["home"]',
                }
            ]
        )

    with pytest.raises(RuntimeError, match="expected_page_type=comment"):
        seed_first_ruleset._validate_rule_rows(
            [
                {
                    "rule_code": "alioth_test",
                    "page_type": "",
                    "steps_json": '[{"expected_page_type":"comment"}]',
                    "applicable_page_types_json": '["post_list"]',
                }
            ]
        )


def test_prepare_ruleset_row_adds_stable_snapshot_hash() -> None:
    ruleset_row = {
        "ruleset_code": "alioth_first_ruleset",
        "name": "首期规则集",
        "version": "1",
        "status": "enabled",
        "description": "首期规则集",
        "source_type": "spreadsheet",
        "source_ref": "https://example.com/rules",
        "rule_count": "2",
        "page_sampling_policy_json": '{"mode":"by_page_type"}',
        "loop_guard_json": '{"max_steps":80}',
    }
    rule_rows = [
        {
            "rule_code": "alioth_2",
            "name": "提示规则",
            "sort_order": "2",
            "judge_config_json": '{"strategy":"llm"}',
        },
        {
            "rule_code": "alioth_1",
            "name": "广告规则",
            "sort_order": "1",
            "judge_config_json": '{"strategy":"rule_engine"}',
        },
    ]

    prepared = seed_first_ruleset._prepare_ruleset_row(ruleset_row, rule_rows)
    reversed_prepared = seed_first_ruleset._prepare_ruleset_row(
        ruleset_row,
        list(reversed(rule_rows)),
    )
    changed_prepared = seed_first_ruleset._prepare_ruleset_row(
        ruleset_row,
        [
            rule_rows[0],
            {
                **rule_rows[1],
                "name": "广告规则变更",
            },
        ],
    )

    assert len(prepared["snapshot_hash"]) == 64
    assert prepared["snapshot_hash"] == reversed_prepared["snapshot_hash"]
    assert prepared["snapshot_hash"] != changed_prepared["snapshot_hash"]


def test_all_rule_engine_rules_have_complete_machine_executable_contracts() -> None:
    rows = {
        row["rule_code"]: row for row in seed_first_ruleset._read_rows(seed_first_ruleset.RULE_CSV)
    }
    expected = {
        "alioth_6": ("technical_error_text", "deterministic_assertion_tool", ["primary"]),
        "alioth_7": (
            "screenshot_change_assertion",
            "deterministic_assertion_tool",
            ["before", "primary"],
        ),
        "alioth_9": (
            "visible_text_presence",
            "deterministic_assertion_tool",
            ["primary"],
        ),
        "alioth_13": (
            "price_relation",
            "deterministic_assertion_tool",
            ["primary", "location_1"],
        ),
        "alioth_14": (
            "discount_price_compare",
            "deterministic_assertion_tool",
            ["primary"],
        ),
        "alioth_18": (
            "visible_text_presence",
            "deterministic_assertion_tool",
            ["primary"],
        ),
        "alioth_20": (
            "screenshot_change_assertion",
            "deterministic_assertion_tool",
            ["before", "primary"],
        ),
        "alioth_21": (
            "screenshot_change_assertion",
            "deterministic_assertion_tool",
            ["before", "primary"],
        ),
        "alioth_26": (
            "state_transition_assertion",
            "deterministic_assertion_tool",
            ["before", "primary"],
        ),
        "alioth_29": (
            "rate_limit_assertion",
            "deterministic_assertion_tool",
            ["attempt_1", "attempt_2", "attempt_3", "primary"],
        ),
        "alioth_38": (
            "collection_constraint",
            "deterministic_assertion_tool",
            ["primary"],
        ),
        "alioth_41": ("qr_url_validation", "qr_link_validation_tool", ["primary"]),
        "alioth_45": (
            "required_ui_capability",
            "deterministic_assertion_tool",
            ["primary"],
        ),
    }

    for rule_code, (handler, tool_ref, capture_roles) in expected.items():
        config = json.loads(rows[rule_code]["judge_config_json"])
        input_contract = json.loads(rows[rule_code]["input_contract_json"])
        route = resolve_rule_execution_route(
            executor_type=rows[rule_code]["executor_type"],
            judge_config=config,
        )

        assert rows[rule_code]["executor_type"] == "rule_engine"
        assert config["engine_config"]["handler"] == handler
        assert tool_ref in config["tool_refs"]
        assert config["llm_checks"] == []
        assert route.rule_engine_ready is True
        if rule_code == "alioth_41":
            # 二维码规则的执行器按「文字网址 → 快照二维码元素 → 截图解码」任一即可判，
            # 处理器不硬性要求材料（09-14）；正式任务的证据合同仍按三样齐全采集。
            assert route.required_artifact_types == ()
            assert input_contract["required_artifacts"] == [
                "visible_text",
                "ui_snapshot",
                "screenshot",
            ]
        else:
            assert input_contract["required_artifacts"] == list(route.required_artifact_types)
        assert input_contract["required_fields"] == list(route.required_input_fields)
        assert input_contract["required_capture_roles"] == capture_roles

    packet_capability_rules = {
        # 2026-09-21 拍板：19 从几何空判换成「中心点点得中 + 不小于 32×32」
        "alioth_19": "clickable_area_handler",
        "alioth_22": "popup_sequence_handler",
        "alioth_25": "visible_ad_presence_handler",
        "alioth_42": "page_url_status_handler",
        "alioth_48": "logout_entry_presence_handler",
    }
    for rule_code, handler in packet_capability_rules.items():
        config = json.loads(rows[rule_code]["judge_config_json"])
        input_contract = json.loads(rows[rule_code]["input_contract_json"])
        route = resolve_rule_execution_route(
            executor_type=rows[rule_code]["executor_type"],
            judge_config=config,
        )

        assert config["engine_config"]["handler"] == handler
        assert route.rule_engine_ready is True
        assert route.judge_input_source == "trusted_observation_packet"
        assert input_contract["required_artifacts"] == list(route.required_artifact_types)

    assert {
        executor: sum(row["executor_type"] == executor for row in rows.values())
        for executor in ("rule_engine", "llm", "hybrid")
    } == {"rule_engine": 19, "llm": 2, "hybrid": 52}


def test_decisions_20260921_are_reflected_in_seed() -> None:
    """2026-09-21 立川拍板：24、40 改模型看前后两份，19 换点得中判法，
    多浏览器 / 跨端两条声明平台不采样。"""
    rows = {
        row["rule_code"]: row
        for row in seed_first_ruleset._read_rows(seed_first_ruleset.RULE_CSV)
    }

    for rule_code, batch_group in (
        ("alioth_24", "startup-ad-skip"),
        ("alioth_40", "content-lifecycle"),
    ):
        row = rows[rule_code]
        contract = json.loads(row["input_contract_json"])
        judge_config = json.loads(row["judge_config_json"])
        assert row["executor_type"] == "hybrid"
        assert row["severity"] == "high"
        assert contract["required_artifacts"] == ["screenshot"]
        assert contract["required_capture_roles"] == ["primary"]
        assert "engine_config" not in judge_config
        assert judge_config["llm_checks"]
        capability_config = judge_config["capability_config"]
        assert capability_config["batch_group"] == batch_group
        assert capability_config["deterministic_tools"] == []
        assert capability_config["semantic_judge"] == "semantic_rule_judge_tool"
        assert capability_config["requires_journey"] is True
    # 下架来源页是遇到才产出的角色，不再列为必需采集角色
    assert json.loads(rows["alioth_40"]["input_contract_json"])["scenario_trigger_roles"] == [
        "source"
    ]
    assert "scenario_trigger_roles" not in json.loads(rows["alioth_24"]["input_contract_json"])

    skip_row = rows["alioth_19"]
    skip_engine = json.loads(skip_row["judge_config_json"])["engine_config"]
    assert skip_row["executor_type"] == "rule_engine"
    assert skip_row["severity"] == "medium"
    assert skip_engine == {
        "handler": "clickable_area_handler",
        "target_keywords": ["跳过", "skip"],
        "min_width": 32,
        "min_height": 32,
    }

    assert {
        rule_code: json.loads(rows[rule_code]["input_contract_json"]).get("required_sampling")
        for rule_code in ("alioth_spec_17", "alioth_spec_18", "alioth_spec_new_25")
    } == {
        "alioth_spec_17": None,
        "alioth_spec_18": "multi_browser",
        "alioth_spec_new_25": "cross_platform",
    }


def test_transient_message_rules_execute_capture_tool_and_require_action_trace() -> None:
    rows = {
        row["rule_code"]: row
        for row in seed_first_ruleset._read_rows(seed_first_ruleset.RULE_CSV)
    }

    for rule_code in ("alioth_10", "alioth_11"):
        row = rows[rule_code]
        contract = json.loads(row["input_contract_json"])
        capability_config = json.loads(row["judge_config_json"])["capability_config"]

        assert row["executor_type"] == "hybrid"
        assert "action_trace" in contract["required_artifacts"]
        assert capability_config["deterministic_tools"] == [
            "transient_message_capture_tool"
        ]


def test_sensitive_content_rules_declare_built_in_catalog_categories() -> None:
    rows = {
        row["rule_code"]: row
        for row in seed_first_ruleset._read_rows(seed_first_ruleset.RULE_CSV)
    }
    expected = {
        "alioth_28": ["political", "terrorism", "personal_attack"],
        "alioth_30": ["adult", "gambling", "private_ad"],
        "alioth_32": ["china_related"],
        "alioth_34": ["terrorism"],
    }

    for rule_code, categories in expected.items():
        capability_config = json.loads(rows[rule_code]["judge_config_json"])[
            "capability_config"
        ]
        assert capability_config["sensitive_word_categories"] == categories


def test_relaxed_rule_configs_match_business_confirmation() -> None:
    rows = {
        row["rule_code"]: row for row in seed_first_ruleset._read_rows(seed_first_ruleset.RULE_CSV)
    }

    assert rows["alioth_21"]["page_type"] == "home"
    # 0beb4e77：首页弹窗蒙层规则扩展覆盖以列表页为落地页的视频站首页。
    assert json.loads(rows["alioth_21"]["applicable_page_types_json"]) == ["home", "video_list"]

    for rule_code in ("alioth_7", "alioth_20", "alioth_21"):
        engine_config = json.loads(rows[rule_code]["judge_config_json"])["engine_config"]
        assert engine_config["before_role"] == "before"
        assert engine_config["after_role"] == "primary"
    assert json.loads(rows["alioth_21"]["judge_config_json"])["engine_config"][
        "required_page_states"
    ] == ["popup", "modal", "overlay", "dialog"]

    for rule_code in ("alioth_9", "alioth_18"):
        engine_config = json.loads(rows[rule_code]["judge_config_json"])["engine_config"]
        assert engine_config["absence_conclusion"] == "evidence_insufficient"
    numeric_config = json.loads(rows["alioth_9"]["judge_config_json"])["engine_config"]
    assert numeric_config["include_ui_snapshot_text"] is True
    assert "^\\s*\\d+" in numeric_config["pattern"]
    countdown_config = json.loads(rows["alioth_18"]["judge_config_json"])["engine_config"]
    assert "(?![A-Za-z0-9_])" in countdown_config["pattern"]

    discount_config = json.loads(rows["alioth_14"]["judge_config_json"])["engine_config"]
    assert discount_config["pass_when_discount_absent"] is True

    status_config = json.loads(rows["alioth_42"]["judge_config_json"])["engine_config"]
    assert status_config["allowed_status_codes"] == [200]

    # 24 已在 2026-09-21 改成模型看前后两帧，不再属于「放宽成弱判法」这一组
    for rule_code in ("alioth_25", "alioth_42"):
        contract = json.loads(rows[rule_code]["input_contract_json"])
        assert contract["required_artifacts"] == []
        assert contract["quality_gate"]["min_screenshot_count"] == 0
    assert json.loads(rows["alioth_24"]["applicable_page_types_json"]) == [
        "home",
        "startup_ad",
    ]

    logout_config = json.loads(rows["alioth_48"]["judge_config_json"])["engine_config"]
    assert "退出登录" in logout_config["target_keywords"]
    logout_contract = json.loads(rows["alioth_48"]["input_contract_json"])
    assert logout_contract["required_artifacts"] == ["visible_text"]


def test_auth_requirement_declarations_cover_login_dependent_rules() -> None:
    """登录依赖声明（2026-08-25 与探索侧对齐用）。

    authenticated=仅登录态下判定有意义（游客态应降级不适用，防止 alioth_48
    在游客个人页假失败一类误判）；guest=仅未登录态可测（注册流程）；
    缺省=登录无关。消费方为需求集/判定入参，待探索侧登录态事实落观察包后接线。
    """
    rows = {
        row["rule_code"]: row for row in seed_first_ruleset._read_rows(seed_first_ruleset.RULE_CSV)
    }
    expected = {
        "alioth_8": "authenticated",
        "alioth_29": "authenticated",
        "alioth_31": "authenticated",
        "alioth_43": "authenticated",
        "alioth_45": "authenticated",
        "alioth_48": "authenticated",
        "alioth_ops_5": "authenticated",
        "alioth_ops_11": "authenticated",
    }
    declared = {}
    for rule_code, row in rows.items():
        raw_contract = (row["input_contract_json"] or "").strip()
        contract = json.loads(raw_contract) if raw_contract else {}
        value = contract.get("auth_requirement")
        if value is not None:
            declared[rule_code] = value
    assert declared == expected
    assert set(declared.values()) <= {"authenticated", "guest"}


def test_recapture_policy_declarations_cover_every_rule() -> None:
    """补采方式声明（2026-09-07 规则负责人立川逐条拍板）。

    每条规则声明首选方式 kind，可带备选 fallback；写法用总设计 §19.1 的正式名字。
    支付/价格/二维码从严（exact_state），有副作用操作与依赖登录态改数据的转人工
    （manual_only），文案布局类同页型即可（equivalent_state），广告弹窗滚动拖动类
    重新观察即可（fresh_observation）。分布与几条关键规则锁死，改动要重新拍板。
    """
    from services.api.app.domain.inspection.evidence_gap import (
        RecapturePolicy,
        parse_recapture_declaration,
    )

    rows = list(seed_first_ruleset._read_rows(seed_first_ruleset.RULE_CSV))
    formal = {"exact_state", "equivalent_state", "fresh_observation", "manual_only"}
    kinds: dict[str, tuple[str, str | None]] = {}
    for row in rows:
        contract = json.loads(row["input_contract_json"])
        declaration = contract["recapture_policy"]
        assert declaration["kind"] in formal, row["rule_code"]
        assert declaration.get("fallback") in formal | {None}, row["rule_code"]
        assert parse_recapture_declaration(contract)[0] is not None
        kinds[row["rule_code"]] = (declaration["kind"], declaration.get("fallback"))

    assert len(kinds) == 73
    distribution = {}
    for kind, _ in kinds.values():
        distribution[kind] = distribution.get(kind, 0) + 1
    assert distribution == {
        "exact_state": 3,
        "equivalent_state": 33,
        "fresh_observation": 27,
        "manual_only": 10,
    }
    assert kinds["alioth_13"] == ("exact_state", "manual_only")
    assert kinds["alioth_14"] == ("exact_state", "manual_only")
    assert kinds["alioth_41"] == ("exact_state", "fresh_observation")
    assert kinds["alioth_8"] == ("manual_only", "exact_state")
    assert kinds["alioth_29"] == ("manual_only", "exact_state")
    assert kinds["alioth_31"] == ("manual_only", "exact_state")
    assert kinds["alioth_20"] == ("fresh_observation", "manual_only")
    assert kinds["alioth_2"] == ("equivalent_state", "fresh_observation")
    assert kinds["alioth_spec_new_25"] == ("manual_only", None)
    assert kinds["alioth_ops_5"] == ("manual_only", "exact_state")
    assert parse_recapture_declaration(json.loads(next(
        row["input_contract_json"] for row in rows if row["rule_code"] == "alioth_13"
    ))) == (RecapturePolicy.EXACT, RecapturePolicy.MANUAL)


def test_batch2_spec_rules_are_seeded_enabled_with_ready_routes() -> None:
    """自动质检规范二批规则：随规则表启用入库，路由必须可解析，编号映射固定。

    2026-08-07 产品口径：原68/77/78/104/新增26 共 5 条暂不做已从种子移除。
    2026-09-04 对齐规则表（61 条）：+44 注册（alioth_1）、首页红线人（alioth_35）、
    未成年红线标记（alioth_36）表内已删，随之移出种子；09-09 立川拍板把 alioth_36 恢复启用
    （迁移 0105，纯视觉、命中只转人工）；二批留存 20 条
    （预留玉衡序号 4 条 + spec 编号 16 条）表内实现批次已标 830，改为启用入库；
    原序号 28 元素对齐与层次感（alioth_spec_28）按表新建。
    """
    rows = {
        row["rule_code"]: row for row in seed_first_ruleset._read_rows(seed_first_ruleset.RULE_CSV)
    }
    reserved_codes = {
        "alioth_15": "封面与内容相符检查",
        "alioth_33": "英国政治内容检查",
        "alioth_44": "进度条时间显示检查",
        "alioth_47": "客服入口可用性检查",
    }
    removed_codes = {
        "alioth_spec_68",
        "alioth_spec_77",
        "alioth_spec_78",
        "alioth_37",
        "alioth_spec_new_26",
        "alioth_1",
        "alioth_35",
    }
    spec_codes = {code for code in rows if code.startswith("alioth_spec_")}

    assert len(spec_codes) == 17
    assert removed_codes.isdisjoint(rows)
    assert rows["alioth_39"]["name"] == "榜单数据排序合理性检查"
    for code, name in reserved_codes.items():
        assert rows[code]["name"] == name
    assert all(row["status"] == "enabled" for row in rows.values())
    for code in sorted(spec_codes | set(reserved_codes)):
        row = rows[code]
        config = json.loads(row["judge_config_json"])
        route = resolve_rule_execution_route(
            executor_type=row["executor_type"],
            judge_config=config,
        )

        assert route.ready is True
        assert row["description"].startswith("来源：自动质检规范")


def test_validate_rule_rows_rejects_rule_engine_without_registered_handler() -> None:
    with pytest.raises(RuntimeError, match="HANDLER_NOT_REGISTERED"):
        seed_first_ruleset._validate_rule_rows(
            [
                {
                    "rule_code": "alioth_test",
                    "executor_type": "rule_engine",
                    "page_type": "home",
                    "steps_json": "[]",
                    "applicable_page_types_json": '["home"]',
                    "judge_config_json": ('{"engine_config":{"handler":"popup_close_behavior"}}'),
                }
            ]
        )


@pytest.mark.asyncio
async def test_upsert_ruleset_persists_snapshot_hash() -> None:
    conn = RecordingConnection()
    ruleset_row = {
        "ruleset_code": "alioth_first_ruleset",
        "name": "首期规则集",
        "version": "1",
        "status": "enabled",
        "description": "首期规则集",
        "source_type": "spreadsheet",
        "source_ref": "https://example.com/rules",
        "snapshot_hash": "a" * 64,
        "rule_count": "39",
        "page_sampling_policy_json": '{"mode":"by_page_type"}',
        "loop_guard_json": '{"max_steps":80}',
    }

    await seed_first_ruleset._upsert_ruleset(conn, ruleset_row)

    assert "snapshot_hash" in conn.sql
    assert ruleset_row["snapshot_hash"] in conn.args


@pytest.mark.asyncio
async def test_upsert_rule_replay_preserves_existing_status() -> None:
    """种子重播不得把运行时手工启用的规则打回种子默认状态。"""
    conn = RecordingConnection()
    rule_row = seed_first_ruleset._read_rows(seed_first_ruleset.RULE_CSV)[0]

    await seed_first_ruleset._upsert_rule(conn, "ruleset-id", rule_row)

    _, _, conflict_clause = conn.sql.partition("DO UPDATE SET")
    assert "status = qc_rule.status" in conflict_clause
    assert "status = EXCLUDED.status" not in conflict_clause


def test_ops_group1_rules_are_seeded_with_ready_routes() -> None:
    """运营1组巡检表拆出的规则（2026-09-08 立 8 条，2026-09-10 按表格批注扩到 11 条）。

    来源=运营1组检测表格 8 项人工巡检（域名/双端打开/播放/支付入口/拉单/分享/官方群/封面），
    61 条规则表里没有对应项才新写；rule_code 用 alioth_ops_{N} 与巡检项顺序一一对应，
    不占玉衡序号。全部 hybrid，语义 Judge 路由必须可解析；拉单有副作用，必须声明登录依赖。
    09-10 表格批注三条：播放按内容类型拆成 ops_3／ops_9／ops_10；多种会员卡每种都要操作；
    页面标记金额与实际拉单金额是否一致新起 ops_11。
    """
    rows = {
        row["rule_code"]: row for row in seed_first_ruleset._read_rows(seed_first_ruleset.RULE_CSV)
    }
    expected = {
        "alioth_ops_1": ("产品域名可访问检查", "interaction", "critical", ["home", "video_list"]),
        "alioth_ops_2": ("产品双端可打开检查", "visual", "critical", ["home", "video_list"]),
        "alioth_ops_3": (
            "视频与短视频播放检查",
            "player",
            "critical",
            ["video_detail", "content_detail"],
        ),
        "alioth_ops_4": (
            "充值入口进入充值页检查",
            "payment",
            "high",
            ["payment", "profile", "content_detail", "video_detail"],
        ),
        "alioth_ops_5": ("充值拉起支付检查", "payment", "critical", ["payment"]),
        "alioth_ops_6": (
            "分享链接有效性检查",
            "interaction",
            "high",
            ["content_detail", "video_detail", "profile", "activity"],
        ),
        "alioth_ops_7": (
            "官方群入口跳转检查",
            "interaction",
            "high",
            ["home", "profile", "settings", "activity"],
        ),
        "alioth_ops_8": (
            "封面加载完整性检查",
            "visual",
            "high",
            ["home", "content_list", "video_list", "post_list", "content_detail", "video_detail"],
        ),
        "alioth_ops_9": (
            "短剧与动漫分集播放检查",
            "player",
            "critical",
            ["video_detail", "content_detail"],
        ),
        "alioth_ops_10": ("漫画与图文阅读检查", "player", "critical", ["content_detail"]),
        "alioth_ops_11": ("拉单金额一致性检查", "payment", "critical", ["payment"]),
    }

    for rule_code, (name, category, severity, page_types) in expected.items():
        row = rows[rule_code]
        config = json.loads(row["judge_config_json"])
        contract = json.loads(row["input_contract_json"])
        capability = config["capability_config"]
        route = resolve_rule_execution_route(
            executor_type=row["executor_type"],
            judge_config=config,
        )

        assert row["name"] == name
        assert row["category"] == category
        assert row["severity"] == severity
        assert row["status"] == "enabled"
        assert row["executor_type"] == "hybrid"
        assert row["description"].startswith("来源：运营1组检测表格")
        assert json.loads(row["applicable_page_types_json"]) == page_types
        assert route.ready is True
        assert route.handler_tool_name == capability["semantic_judge"]
        assert contract["required_artifacts"] == capability["required_artifact_types"]
        assert capability["deterministic_tools"]

    # 09-09 拍板：播放规则必须采媒体状态；官方群入口整任务判一次（聚合范围 official_group_entry）；
    # 拉单自动执行到唤起收银台为止。
    playback_contract = json.loads(rows["alioth_ops_3"]["input_contract_json"])
    playback_config = json.loads(rows["alioth_ops_3"]["judge_config_json"])
    assert "media_state" in playback_contract["required_artifacts"]
    assert "media_state" not in playback_contract["optional_artifacts"]
    assert any("付费内容" in check for check in playback_config["llm_checks"])
    group_config = json.loads(rows["alioth_ops_7"]["judge_config_json"])["capability_config"]
    assert group_config["evidence_input_scope"] == "task"
    assert group_config["evidence_scope_key"] == "official_group_entry"
    assert any(
        "自动执行到唤起收银台为止" in rule
        for rule in json.loads(rows["alioth_ops_5"]["judge_config_json"])["deterministic_rules"]
    )

    # 拉单会真实创建订单：登录依赖 + 补采转人工优先，只验证到拉起支付为止。
    order_contract = json.loads(rows["alioth_ops_5"]["input_contract_json"])
    order_config = json.loads(rows["alioth_ops_5"]["judge_config_json"])
    assert order_contract["auth_requirement"] == "authenticated"
    assert "action_trace" in order_contract["required_artifacts"]
    assert "只验证到成功拉起支付为止，不实际完成付款" in order_config["deterministic_rules"]
    for rule_code in ("alioth_ops_4", "alioth_ops_5", "alioth_ops_11"):
        policy = json.loads(rows[rule_code]["manual_review_policy_json"])
        assert policy["high_risk_categories"] == ["payment"]
        assert policy["min_confidence"] == 0.9

    # 09-10 表格批注①：播放按内容类型拆三条，每条先认内容类型再判，不是本类就 skipped，
    # 否则同一个播放页会被三条规则各判一次不过。
    for rule_code, own_type, peers in (
        ("alioth_ops_3", "长视频与短视频", ("alioth_ops_9", "alioth_ops_10")),
        ("alioth_ops_9", "选集或分集结构", ("alioth_ops_3", "alioth_ops_10")),
        ("alioth_ops_10", "连续图片流", ("alioth_ops_3", "alioth_ops_9")),
    ):
        first_check = json.loads(rows[rule_code]["judge_config_json"])["llm_checks"][0]
        assert first_check.startswith("先认内容类型"), rule_code
        assert own_type in first_check, rule_code
        assert "skipped" in first_check, rule_code
        assert all(peer in first_check for peer in peers), rule_code

    # 同日改判：短视频要看上滑到下一条、短剧要看切到第 2 集（原「只看当前一条/一集」作废）。
    assert any(
        "上滑" in check and "下一条" in check
        for check in json.loads(rows["alioth_ops_3"]["judge_config_json"])["llm_checks"]
    )
    assert any(
        "第 2 集" in check
        for check in json.loads(rows["alioth_ops_9"]["judge_config_json"])["llm_checks"]
    )
    # 阅读页没有媒体元素：ops_10 不能要媒体状态，否则漫画页一律记证据不足。
    reading_contract = json.loads(rows["alioth_ops_10"]["input_contract_json"])
    assert "media_state" not in reading_contract["required_artifacts"]
    assert "action_trace" in reading_contract["required_artifacts"]

    # 09-10 表格批注②：多种会员卡每种都要操作。ops_4 只选不付，逐个点；
    # 拉单有副作用，先只拉最便宜一档，档位数写成阈值，产品方点头后调大即可放开。
    assert any(
        "每一种都应能正常选中" in check
        for check in json.loads(rows["alioth_ops_4"]["judge_config_json"])["llm_checks"]
    )
    assert (
        "action_trace"
        in json.loads(rows["alioth_ops_4"]["input_contract_json"])["required_artifacts"]
    )
    for rule_code in ("alioth_ops_5", "alioth_ops_11"):
        thresholds = json.loads(rows[rule_code]["judge_config_json"])["thresholds"]
        assert thresholds["order_pull_card_count"] == 1, rule_code

    # 09-10 表格批注③：页面标记金额与实际拉单金额是否一致。只靠截图判不了金额，
    # 页面文字与网络日志必须是必需附件；材料取自 ops_5 那一次拉单，本条不额外下单。
    amount_contract = json.loads(rows["alioth_ops_11"]["input_contract_json"])
    amount_config = json.loads(rows["alioth_ops_11"]["judge_config_json"])
    assert {"visible_text", "har"} <= set(amount_contract["required_artifacts"])
    assert amount_contract["auth_requirement"] == "authenticated"
    assert any("不额外下单" in rule for rule in amount_config["deterministic_rules"])


def test_minor_redline_rule_is_restored_as_manual_review_only() -> None:
    """未成年红线标记检查（alioth_36）09-09 恢复启用：纯视觉、命中只转人工、不自动判不过。"""
    rows = {
        row["rule_code"]: row for row in seed_first_ruleset._read_rows(seed_first_ruleset.RULE_CSV)
    }
    row = rows["alioth_36"]
    config = json.loads(row["judge_config_json"])
    contract = json.loads(row["input_contract_json"])
    route = resolve_rule_execution_route(executor_type=row["executor_type"], judge_config=config)

    assert row["status"] == "enabled"
    assert row["category"] == "input_risk"
    assert row["severity"] == "critical"
    assert json.loads(row["applicable_page_types_json"]) == ["home", "content_list", "video_list"]
    assert route.ready is True
    assert config["capability_config"]["semantic_judge"] == "content_safety_judge_tool"
    checks = config["llm_checks"]
    assert any("manual_review" in check and "不自动判 fail" in check for check in checks)
    assert any("不使用关键词库" in check for check in checks)
    assert contract["recapture_policy"] == {
        "kind": "equivalent_state",
        "fallback": "fresh_observation",
    }
    assert contract["reuse_policy"] == {"scope": "same_object"}
    # 09-22 立川：模型明确说「都是成人」却因把握度 86% 转人工，通过线降到 0.85；
    # 真疑似照样由模型直接给 manual_review，不受这条线影响。
    assert json.loads(row["manual_review_policy_json"])["min_confidence"] == 0.85
    assert rows["alioth_35"]["status"] == "disabled" if "alioth_35" in rows else True


def test_reuse_policy_declarations_cover_every_rule() -> None:
    """规则结论复用范围声明（2026-09-09 规则负责人立川逐条拍板，全部采纳建议）。

    每条规则声明 reuse_policy.scope：默认 per_page 每页各自执行；同一条内容、商品、链接、
    二维码类走 same_object；广告素材比例走 same_material；域名、双端、登录退出入口、客服入口
    和转人工那批走 per_task；Toast 统一性本来就是跨页汇总 sealed_set。分布与几条关键规则
    锁死，改动要重新拍板。
    """
    from services.api.app.domain.inspection.rule_reuse import (
        RuleReuseScope,
        parse_reuse_declaration,
    )

    rows = list(seed_first_ruleset._read_rows(seed_first_ruleset.RULE_CSV))
    scopes: dict[str, str] = {}
    for row in rows:
        contract = json.loads(row["input_contract_json"])
        declaration = contract["reuse_policy"]
        assert set(declaration) == {"scope"}, row["rule_code"]
        assert parse_reuse_declaration(contract).value == declaration["scope"], row["rule_code"]
        scopes[row["rule_code"]] = declaration["scope"]

    assert len(scopes) == 73
    distribution: dict[str, int] = {}
    for scope in scopes.values():
        distribution[scope] = distribution.get(scope, 0) + 1
    # 09-09 晚拍板：官方群入口改成整任务一次的聚合规则，随之从 same_object 改为 sealed_set。
    assert distribution == {
        "per_page": 41,
        "same_object": 13,
        "same_material": 1,
        "per_task": 16,
        "sealed_set": 2,
    }
    assert scopes["alioth_ops_1"] == "per_task"
    assert scopes["alioth_ops_2"] == "per_task"
    assert scopes["alioth_48"] == "per_task"
    assert scopes["alioth_13"] == "same_object"
    assert scopes["alioth_15"] == "same_object"
    assert scopes["alioth_41"] == "same_object"
    assert scopes["alioth_16"] == "same_material"
    assert scopes["alioth_spec_48"] == "sealed_set"
    assert scopes["alioth_ops_7"] == "sealed_set"
    assert scopes["alioth_20"] == "per_page"
    assert scopes["alioth_2"] == "per_page"
    assert parse_reuse_declaration(json.loads(next(
        row["input_contract_json"] for row in rows if row["rule_code"] == "alioth_ops_1"
    ))) is RuleReuseScope.PER_TASK


def test_rulesets_are_split_into_phase1_phase2_and_ruisheng() -> None:
    """2026-09-11 规则集按期拆分：1 期 40（730 批）/ 2 期 21（830 批 + 元素对齐）/ 3 期瑞升 12。"""
    ruleset_rows = seed_first_ruleset._read_rows(seed_first_ruleset.RULESET_CSV)
    rule_rows = seed_first_ruleset._read_rows(seed_first_ruleset.RULE_CSV)
    groups = seed_first_ruleset._group_rules_by_ruleset(ruleset_rows, rule_rows)

    assert [(row["ruleset_code"], row["name"], row["rule_count"]) for row in ruleset_rows] == [
        ("alioth_first_ruleset", "1期质检规则集", "40"),
        ("alioth_phase2_ruleset", "2期质检规则集", "21"),
        ("alioth_ruisheng_ruleset", "3期瑞升质检规则集", "12"),
    ]
    codes = {key[0]: {row["rule_code"] for row in rows} for key, rows in groups.items()}
    assert codes["alioth_ruisheng_ruleset"] == {f"alioth_ops_{index}" for index in range(1, 12)} | {
        "alioth_36"
    }
    assert "alioth_spec_28" in codes["alioth_phase2_ruleset"]
    batch2_numbered = {"alioth_15", "alioth_33", "alioth_44", "alioth_47"}
    assert all(
        code.startswith("alioth_spec") or code in batch2_numbered
        for code in codes["alioth_phase2_ruleset"]
    )
    # 1 期只剩玉衡序号规则（730 批），规范类、运营组、未成年红线都已挪走
    assert all(code.removeprefix("alioth_").isdigit() for code in codes["alioth_first_ruleset"])
    moved = codes["alioth_phase2_ruleset"] | codes["alioth_ruisheng_ruleset"]
    assert not codes["alioth_first_ruleset"] & moved


def test_group_rules_rejects_unknown_ruleset_and_count_mismatch() -> None:
    ruleset_rows = [{"ruleset_code": "phase", "version": "1", "rule_count": "1"}]
    with pytest.raises(RuntimeError, match="指向不存在的规则集"):
        seed_first_ruleset._group_rules_by_ruleset(
            ruleset_rows, [{"rule_code": "r1", "ruleset_code": "other", "ruleset_version": "1"}]
        )
    with pytest.raises(RuntimeError, match="规则数量不一致"):
        seed_first_ruleset._group_rules_by_ruleset(ruleset_rows, [])


def test_phase_split_migration_moves_the_same_rules_as_the_seed() -> None:
    """存量库靠迁移挪规则、全新库靠种子建规则集，两边的分组必须一致。"""
    import importlib.util
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "20260911_0107_ruleset_phase_split.py"
    )
    spec = importlib.util.spec_from_file_location("ruleset_phase_split", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    rule_rows = seed_first_ruleset._read_rows(seed_first_ruleset.RULE_CSV)
    by_ruleset: dict[str, set[str]] = {}
    for row in rule_rows:
        by_ruleset.setdefault(row["ruleset_code"], set()).add(row["rule_code"])
    # alioth_1 / alioth_35 只在存量库里（已停用的二批旧规则），种子里没有
    assert set(migration._PHASE2_RULES) - {"alioth_1", "alioth_35"} == by_ruleset[migration._PHASE2]
    assert set(migration._RUISHENG_RULES) == by_ruleset[migration._RUISHENG]
    source = path.read_text(encoding="utf-8")
    assert "CREATE OR REPLACE FUNCTION qc_ruleset_scope" in source
    assert "member_ruleset_ids" in source
