"""规则测试接口：表单解析、截图转 PNG、错误口径与权限、测试记录。"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient
from PIL import Image

from services.api.app.api.v1 import auth as auth_module
from services.api.app.api.v1 import rule_tests as rule_tests_module
from services.api.app.application.rule_sandbox import (
    RuleSandboxEvidenceError,
    RuleSandboxEvidenceNeed,
    RuleSandboxOption,
    RuleSandboxRuleNotFoundError,
    RuleSandboxRunResult,
)
from services.api.app.application.rule_sandbox_evidence import (
    EXTRA_INPUTS,
    RuleSandboxEvidence,
    SandboxCheckMode,
    SandboxPlatform,
)
from services.api.app.application.rule_test_records import (
    RuleTestRecordDeleteError,
    RuleTestRecordNotFoundError,
)
from services.api.app.infrastructure.rule_sandbox_page_capture import (
    RuleTestPageCapture,
    RuleTestPageCaptureError,
)
from services.api.app.main import create_app

RULE_ID = "00000000-0000-0000-0000-000000000012"
RECORD_ID = "00000000-0000-0000-0000-0000000000aa"


def _user(permissions: list[str]) -> auth_module.AuthUserResponse:
    return auth_module.AuthUserResponse(
        id="user-1",
        username="config-user",
        full_name="规则配置员",
        role="规则配置员",
        status="enabled",
        permissions=permissions,
        tenant_id="00000000-0000-0000-0000-000000000001",
        tenant_name="体验中心",
    )


class FakeSandboxService:
    def __init__(self, error: Exception | None = None, screenshot_text: str | None = None) -> None:
        self.error = error
        self.screenshot_text = screenshot_text
        self.calls: list[dict[str, Any]] = []

    async def list_options(self) -> list[RuleSandboxOption]:
        return [
            RuleSandboxOption(
                rule_id=RULE_ID,
                rule_code="alioth_12",
                name="提示文案叹号结尾检查",
                category="text",
                severity="medium",
                executor_type="hybrid",
                status="enabled",
                check_point="提示文字不应使用叹号结尾",
                applicable_page_types=("content_detail",),
                ruleset_id="ruleset-1",
                ruleset_code="alioth_first_ruleset",
                ruleset_name="首期规则集",
                ruleset_version=1,
                ruleset_status="enabled",
                testable=True,
                untestable_reason=None,
                config_problem=None,
                relaxable=True,
                evidence_needs=(
                    RuleSandboxEvidenceNeed(
                        artifact_type="screenshot", label="页面截图", form_supported=True
                    ),
                ),
                min_screenshot_count=1,
                required_page_states=(),
                target_clients=("H5",),
                notes=(),
                extra_inputs=(EXTRA_INPUTS["before"],),
            )
        ]

    async def run(
        self,
        *,
        rule_id: str,
        evidence: RuleSandboxEvidence,
        check_mode: SandboxCheckMode,
    ) -> RuleSandboxRunResult:
        self.calls.append({"rule_id": rule_id, "evidence": evidence, "check_mode": check_mode})
        if self.error is not None:
            raise self.error
        return RuleSandboxRunResult(
            rule_id=rule_id,
            rule_code="alioth_12",
            rule_name="提示文案叹号结尾检查",
            executor_type="hybrid",
            rule_status="enabled",
            check_mode=check_mode.value,
            outcome="pass",
            result_summary="未发现叹号结尾的提示",
            problem_desc=None,
            confidence=0.93,
            confidence_threshold=None,
            findings=(),
            missing_evidence=(),
            used_evidence=("截图 1（shot.jpg）",),
            notices=(),
            duration_ms=12,
            judge_input={"page": {"page_type": "content_detail"}},
            screenshot_text=self.screenshot_text,
            judge_output={"conclusion": "pass"},
        )


_SUMMARY = {
    "id": RECORD_ID,
    "rule_id": RULE_ID,
    "rule_code": "alioth_12",
    "rule_name": "提示文案叹号结尾检查",
    "tester_name": "规则配置员",
    "platform": "h5",
    "page_url": "https://example.test/detail/1",
    "page_title": "详情",
    "outcome": "fail",
    "result_summary": "提示文字以叹号结尾",
    "screenshot_count": 1,
    "has_visible_text": True,
    "has_dom": False,
    "has_ui_snapshot": False,
    "has_judge_input": False,
    "created_at": datetime(2026, 9, 11, 8, 0, tzinfo=UTC),
}


class FakeRecordService:
    """测试记录的替身：接口单测绝不碰真库和证据目录。"""

    def __init__(self, save_error: Exception | None = None) -> None:
        self.save_error = save_error
        self.saved: list[dict[str, Any]] = []
        self.list_calls: list[dict[str, Any]] = []
        self.delete_calls: list[dict[str, Any]] = []

    async def save(self, **kwargs: Any) -> str:
        if self.save_error is not None:
            raise self.save_error
        self.saved.append(kwargs)
        return RECORD_ID

    async def list_records(self, **kwargs: Any) -> tuple[list[dict[str, Any]], int]:
        self.list_calls.append(kwargs)
        return [dict(_SUMMARY)], 41

    async def get_record(self, record_id: str) -> dict[str, Any]:
        if record_id != RECORD_ID:
            raise RuleTestRecordNotFoundError("这条测试记录不存在，可能已经被删除")
        return {
            **_SUMMARY,
            "check_mode": "relaxed",
            "page_type": "content_detail",
            "page_state": "stable",
            "visible_text": "操作成功！",
            "dom_html": "",
            "ui_snapshot": None,
            "judge_input": {},
            "second_location_text": "月卡 ¥30",
            "screenshots": [
                {"filename": "shot.png", "width": 16, "height": 24, "storage_path": "x/y.png"},
                {
                    "filename": "before.png",
                    "width": 16,
                    "height": 24,
                    "storage_path": "x/b.png",
                    "capture_role": "before",
                },
            ],
            "result_payload": {
                "rule_id": RULE_ID,
                "rule_code": "alioth_12",
                "rule_name": "提示文案叹号结尾检查",
                "executor_type": "hybrid",
                "rule_status": "enabled",
                "check_mode": "relaxed",
                "outcome": "fail",
                "result_summary": "提示文字以叹号结尾",
                "problem_desc": "「操作成功！」以叹号结尾",
                "confidence": 0.9,
                "confidence_threshold": None,
                "findings": [{"summary": "叹号结尾", "evidence": ["页面文字"]}],
                "missing_evidence": [],
                "used_evidence": ["页面文字"],
                "notices": [],
                "duration_ms": 1200,
                "judge_input": {},
                "judge_output": {"conclusion": "fail"},
            },
        }

    async def read_screenshot(self, record_id: str, index: int) -> bytes:
        if record_id != RECORD_ID or index != 1:
            raise RuleTestRecordNotFoundError(f"这条记录没有第 {index} 张截图")
        return b"\x89PNG-record"

    async def delete(self, **kwargs: Any) -> int:
        self.delete_calls.append(kwargs)
        if (
            not kwargs["record_ids"]
            and kwargs["created_from"] is None
            and (kwargs["created_to"] is None)
        ):
            raise RuleTestRecordDeleteError("请勾选要删除的记录，或者选一个时间段")
        return len(kwargs["record_ids"]) or 7


def _client(
    service: FakeSandboxService,
    permissions: list[str] | None = None,
    records: FakeRecordService | None = None,
) -> TestClient:
    app = create_app()
    app.dependency_overrides[auth_module.require_auth_user] = lambda: _user(
        ["qcRules", "qcRules:write"] if permissions is None else permissions
    )
    app.dependency_overrides[rule_tests_module._get_rule_sandbox_service] = lambda: service
    record_service = records or FakeRecordService()
    app.dependency_overrides[rule_tests_module._get_rule_test_record_service] = lambda: (
        record_service
    )
    return TestClient(app)


def _image(fmt: str, size: tuple[int, int] = (16, 24)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (10, 120, 200)).save(buffer, format=fmt)
    return buffer.getvalue()


def test_default_relaxed_api_preserves_required_real_network_facts() -> None:
    from services.api.app.application.semantic_rule_judge import SemanticJudgeModelOutput
    from services.api.tests.test_rule_sandbox import FakeSemanticJudge, _png, _row, _service

    judge = FakeSemanticJudge(SemanticJudgeModelOutput(
        conclusion="pass", confidence=1.0, reasoning_summary="不得调用的通过结果",
    ))
    service = _service(judge, _row(
        RULE_ID, "network-required", executor_type="llm",
        input_contract={"required_artifacts": ["screenshot", "har"]},
        judge_config={"llm_checks": ["核对真实请求结果"]},
    ))
    records = FakeRecordService()
    client = _client(service, records=records)
    response = client.post(
        "/api/v1/rule-tests",
        data={"rule_id": RULE_ID, "platform": "h5"},
        files=[("screenshots", ("shot.png", _png(), "image/png"))],
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["check_mode"] == "relaxed"
    assert body["outcome"] == "evidence_insufficient"
    assert body["missing_evidence"] == ["网络请求记录"]
    assert judge.calls == []
    assert records.saved[0]["result"].outcome == "evidence_insufficient"


def test_run_converts_jpeg_to_png_and_passes_form_fields_to_service() -> None:
    service = FakeSandboxService()
    records = FakeRecordService()
    client = _client(service, records=records)

    response = client.post(
        "/api/v1/rule-tests",
        data={
            "rule_id": RULE_ID,
            "platform": "web",
            "check_mode": "strict",
            "page_type": "content_detail",
            "page_state": "stable",
            "page_url": "https://example.test/detail/1",
            "visible_text": "操作成功！",
            "ui_snapshot": '{"elements": []}',
            "judge_input": '{"discount_price": 9.9}',
        },
        files=[("screenshots", ("shot.jpg", _image("JPEG"), "image/jpeg"))],
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "pass"
    assert body["used_evidence"] == ["截图 1（shot.jpg）"]
    call = service.calls[0]
    evidence: RuleSandboxEvidence = call["evidence"]
    assert call["rule_id"] == RULE_ID
    assert call["check_mode"] is SandboxCheckMode.STRICT
    assert evidence.platform is SandboxPlatform.WEB
    assert evidence.page_type == "content_detail"
    assert evidence.visible_text == "操作成功！"
    assert evidence.ui_snapshot == {"elements": []}
    assert evidence.judge_input == {"discount_price": 9.9}
    screenshot = evidence.screenshots[0]
    assert screenshot.payload.startswith(b"\x89PNG")
    assert (screenshot.width, screenshot.height) == (16, 24)
    assert screenshot.filename == "shot.jpg"
    # 这次测试的证据和结论存成一条测试记录
    assert body["record_id"] == RECORD_ID
    saved = records.saved[0]
    assert saved["tester_id"] == "user-1"
    assert saved["tester_name"] == "规则配置员"
    assert saved["evidence"] is evidence
    assert saved["result"].outcome == "pass"


def test_options_return_rules_and_formal_page_type_catalog() -> None:
    client = _client(FakeSandboxService())

    response = client.get("/api/v1/rule-tests/options")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["items"][0]["rule_code"] == "alioth_12"
    assert body["items"][0]["evidence_needs"][0]["label"] == "页面截图"
    codes = {item["code"]: item["name"] for item in body["page_types"]}
    assert codes["home"] == "首页"
    extra = body["items"][0]["extra_inputs"][0]
    assert (extra["role"], extra["kind"]) == ("before", "screenshot")
    assert extra["label"] == "操作前的截图"
    assert "操作后那张" in extra["hint"]


def test_before_screenshot_and_second_location_text_reach_the_service() -> None:
    service = FakeSandboxService()
    records = FakeRecordService()
    client = _client(service, records=records)

    response = client.post(
        "/api/v1/rule-tests",
        data={"rule_id": RULE_ID, "second_location_text": "月卡 ¥30"},
        files=[
            ("screenshots", ("after.png", _image("PNG"), "image/png")),
            ("before_screenshot", ("before.jpg", _image("JPEG", (20, 30)), "image/jpeg")),
        ],
    )
    broken = client.post(
        "/api/v1/rule-tests",
        data={"rule_id": RULE_ID, "visible_text": "x"},
        files=[("before_screenshot", ("before.png", b"not an image", "image/png"))],
    )
    too_long = client.post(
        "/api/v1/rule-tests",
        data={
            "rule_id": RULE_ID,
            "visible_text": "x",
            "second_location_text": "页" * (rule_tests_module.MAX_VISIBLE_TEXT_CHARACTERS + 1),
        },
        files=[("screenshots", ("after.png", _image("PNG"), "image/png"))],
    )

    assert response.status_code == 200, response.text
    evidence: RuleSandboxEvidence = service.calls[0]["evidence"]
    assert evidence.second_location_text == "月卡 ¥30"
    before = evidence.before_screenshot
    assert before is not None
    assert before.payload.startswith(b"\x89PNG")
    assert (before.filename, before.width, before.height) == ("before.jpg", 20, 30)
    assert [item.filename for item in evidence.screenshots] == ["after.png"]
    assert records.saved[0]["evidence"] is evidence
    assert broken.status_code == 422
    assert "操作前的截图" in broken.json()["error"]["message"]
    assert too_long.status_code == 422
    assert "第二个位置的页面文字太长" in too_long.json()["error"]["message"]


def test_invalid_json_and_unreadable_image_are_rejected_in_plain_words() -> None:
    service = FakeSandboxService()
    client = _client(service)

    bad_json = client.post(
        "/api/v1/rule-tests",
        data={"rule_id": RULE_ID, "visible_text": "x", "ui_snapshot": "{not json"},
    )
    bad_image = client.post(
        "/api/v1/rule-tests",
        data={"rule_id": RULE_ID},
        files=[("screenshots", ("broken.png", b"not an image", "image/png"))],
    )
    not_object = client.post(
        "/api/v1/rule-tests",
        data={"rule_id": RULE_ID, "visible_text": "x", "judge_input": "[1, 2]"},
    )

    assert bad_json.status_code == 422
    assert "界面快照不是有效的 JSON" in bad_json.json()["error"]["message"]
    assert bad_image.status_code == 422
    assert "不是可识别的图片" in bad_image.json()["error"]["message"]
    assert not_object.status_code == 422
    assert "必须是 JSON 对象" in not_object.json()["error"]["message"]
    assert service.calls == []


def test_page_source_over_one_megabyte_reaches_the_character_limit_check() -> None:
    # 按网址抓来的源码常超过 1MB；框架默认每个表单字段只收 1MB，超了在进接口前就被拒
    service = FakeSandboxService()
    client = _client(service)
    dom = "<div>" + "页" * 600_000 + "</div>"
    snapshot = '{"elements": [' + ",".join(['{"text": "页面元素"}'] * 60_000) + "]}"

    response = client.post(
        "/api/v1/rule-tests",
        data={"rule_id": RULE_ID, "dom_html": dom, "ui_snapshot": snapshot},
        files=[("screenshots", ("shot.png", _image("PNG"), "image/png"))],
    )
    # 页面上提交走 multipart（FormData），与这里一致
    too_long = client.post(
        "/api/v1/rule-tests",
        data={"rule_id": RULE_ID, "dom_html": "页" * (rule_tests_module.MAX_DOM_CHARACTERS + 1)},
        files=[("screenshots", ("shot.png", _image("PNG"), "image/png"))],
    )

    assert len(dom.encode("utf-8")) > 1024 * 1024
    assert len(snapshot.encode("utf-8")) > 1024 * 1024
    assert response.status_code == 200, response.text
    assert service.calls[0]["evidence"].dom_html == dom
    assert len(service.calls[0]["evidence"].ui_snapshot["elements"]) == 60_000
    # 真正的上限仍是接口按字符数做的检查，超了给大白话
    assert too_long.status_code == 422
    assert "页面源码太长" in too_long.json()["error"]["message"]


def test_screenshot_count_is_capped_at_what_the_judge_can_look_at() -> None:
    client = _client(FakeSandboxService())
    png = _image("PNG")

    response = client.post(
        "/api/v1/rule-tests",
        data={"rule_id": RULE_ID},
        files=[("screenshots", (f"{index}.png", png, "image/png")) for index in range(7)],
    )

    assert response.status_code == 422
    assert "最多 6 张" in response.json()["error"]["message"]


def test_service_errors_and_bad_rule_id_map_to_http_status() -> None:
    not_found = _client(FakeSandboxService(RuleSandboxRuleNotFoundError("规则不存在")))
    no_evidence = _client(FakeSandboxService(RuleSandboxEvidenceError("请至少提供一份证据")))

    assert (
        not_found.post(
            "/api/v1/rule-tests", data={"rule_id": RULE_ID, "visible_text": "x"}
        ).status_code
        == 404
    )
    assert no_evidence.post("/api/v1/rule-tests", data={"rule_id": RULE_ID}).status_code == 422
    assert (
        not_found.post(
            "/api/v1/rule-tests", data={"rule_id": "not-a-uuid", "visible_text": "x"}
        ).status_code
        == 404
    )


def test_rule_test_requires_rule_config_permission() -> None:
    service = FakeSandboxService()
    client = _client(service, permissions=["dashboard", "qcRules"])

    assert client.get("/api/v1/rule-tests/options").status_code == 403
    assert (
        client.post(
            "/api/v1/rule-tests", data={"rule_id": RULE_ID, "visible_text": "x"}
        ).status_code
        == 403
    )
    assert service.calls == []


def test_page_capture_returns_formal_artifacts_for_the_form(monkeypatch) -> None:
    calls: list[dict[str, str]] = []

    async def fake_capture(
        *, url: str, platform: str, keep_startup_layer: bool = False
    ) -> RuleTestPageCapture:
        calls.append({"url": url, "platform": platform, "keep_startup_layer": keep_startup_layer})
        return RuleTestPageCapture(
            final_url="https://example.test/detail",
            title="详情页",
            visible_text="提交失败！",
            dom_html="<html></html>",
            ui_snapshot={"schema_version": "ui-snapshot.v2", "elements": []},
            screenshot_png=b"\x89PNG-bytes",
            notices=("页面一直没加载稳，抓到的可能不完整",),
        )

    monkeypatch.setattr(rule_tests_module, "capture_rule_test_page", fake_capture)
    client = _client(FakeSandboxService())

    response = client.post(
        "/api/v1/rule-tests/page-capture",
        json={"url": "https://example.test/a", "platform": "web"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert calls == [
        {"url": "https://example.test/a", "platform": "web", "keep_startup_layer": False}
    ]

    # 查开屏广告的规则：抓到开屏层就是目标，接口告诉采集别去跳过它
    client.post(
        "/api/v1/rule-tests/page-capture",
        json={"url": "https://example.test/a", "platform": "h5", "page_type": "startup_ad"},
    )
    assert calls[-1] == {
        "url": "https://example.test/a",
        "platform": "h5",
        "keep_startup_layer": True,
    }
    assert body["final_url"] == "https://example.test/detail"
    assert body["ui_snapshot"]["schema_version"] == "ui-snapshot.v2"
    assert body["screenshot_png_base64"] == "iVBORy1ieXRlcw=="
    assert body["notices"] == ["页面一直没加载稳，抓到的可能不完整"]


def test_page_capture_rejects_app_and_reports_capture_errors_in_plain_words(monkeypatch) -> None:
    async def failing_capture(*, url: str, platform: str, **_: object) -> RuleTestPageCapture:
        raise RuleTestPageCaptureError("不能抓取内网、本机或保留地址的页面")

    monkeypatch.setattr(rule_tests_module, "capture_rule_test_page", failing_capture)
    client = _client(FakeSandboxService())

    app_page = client.post(
        "/api/v1/rule-tests/page-capture",
        json={"url": "https://example.test/a", "platform": "android"},
    )
    internal = client.post(
        "/api/v1/rule-tests/page-capture",
        json={"url": "http://127.0.0.1/", "platform": "h5"},
    )
    forbidden = _client(FakeSandboxService(), permissions=["qcRules"]).post(
        "/api/v1/rule-tests/page-capture",
        json={"url": "https://example.test/a", "platform": "h5"},
    )

    assert app_page.status_code == 422
    assert "App 页面没有网址" in app_page.json()["error"]["message"]
    assert internal.status_code == 422
    assert "内网" in internal.json()["error"]["message"]
    assert forbidden.status_code == 403


def test_run_still_returns_result_when_record_cannot_be_saved() -> None:
    client = _client(FakeSandboxService(), records=FakeRecordService(RuntimeError("库没升级")))

    response = client.post("/api/v1/rule-tests", data={"rule_id": RULE_ID, "visible_text": "x"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "pass"
    assert body["record_id"] is None
    assert any("没能存进测试记录" in notice for notice in body["notices"])
    # 原始异常不直接给页面看
    assert not any("库没升级" in notice for notice in body["notices"])


def test_record_list_passes_filters_and_returns_summaries() -> None:
    records = FakeRecordService()
    client = _client(FakeSandboxService(), records=records)

    response = client.get(
        "/api/v1/rule-tests/records",
        params={
            "keyword": " alioth_12 ",
            "outcome": "fail",
            "created_from": "2026-09-01T00:00:00+08:00",
            "created_to": "2026-09-12T00:00:00",
            "page": 2,
            "page_size": 500,
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 41
    assert (body["page"], body["page_size"]) == (2, 100)
    assert body["items"][0]["rule_code"] == "alioth_12"
    assert body["items"][0]["screenshot_count"] == 1
    call = records.list_calls[0]
    assert call["keyword"] == "alioth_12"
    assert call["outcome"] == "fail"
    assert call["created_from"].utcoffset().total_seconds() == 8 * 3600
    # 不带时区的时间按 UTC 理解
    assert call["created_to"] == datetime(2026, 9, 12, tzinfo=UTC)


def test_record_detail_returns_evidence_and_result_card_payload() -> None:
    client = _client(FakeSandboxService())

    detail = client.get(f"/api/v1/rule-tests/records/{RECORD_ID}")
    missing = client.get("/api/v1/rule-tests/records/not-a-record")

    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["visible_text"] == "操作成功！"
    assert body["second_location_text"] == "月卡 ¥30"
    # 操作前的截图和页面截图放在同一组里编号，按采集角色区分；老记录没有角色的算页面截图
    assert body["screenshots"] == [
        {"index": 1, "filename": "shot.png", "width": 16, "height": 24, "capture_role": "primary"},
        {"index": 2, "filename": "before.png", "width": 16, "height": 24, "capture_role": "before"},
    ]
    # 存储路径不外露
    assert "storage_path" not in detail.text
    assert body["result"]["outcome"] == "fail"
    assert body["result"]["findings"][0]["evidence"] == ["页面文字"]
    assert missing.status_code == 404


def test_record_screenshot_is_served_as_png() -> None:
    client = _client(FakeSandboxService())

    found = client.get(f"/api/v1/rule-tests/records/{RECORD_ID}/screenshots/1")
    absent = client.get(f"/api/v1/rule-tests/records/{RECORD_ID}/screenshots/2")

    assert found.status_code == 200
    assert found.headers["content-type"] == "image/png"
    assert found.content == b"\x89PNG-record"
    assert absent.status_code == 404


def test_record_batch_delete_by_selection_or_time_range() -> None:
    records = FakeRecordService()
    client = _client(FakeSandboxService(), records=records)

    by_selection = client.post(
        "/api/v1/rule-tests/records/delete", json={"record_ids": [RECORD_ID]}
    )
    by_time = client.post(
        "/api/v1/rule-tests/records/delete",
        json={"created_from": "2026-09-01T00:00:00Z", "created_to": "2026-09-02T00:00:00Z"},
    )
    nothing = client.post("/api/v1/rule-tests/records/delete", json={})

    assert by_selection.json() == {"deleted": 1}
    assert by_time.json() == {"deleted": 7}
    assert records.delete_calls[1]["created_from"] == datetime(2026, 9, 1, tzinfo=UTC)
    assert nothing.status_code == 422
    assert "请勾选要删除的记录" in nothing.json()["error"]["message"]


def test_records_are_only_for_experience_center_admins() -> None:
    records = FakeRecordService()
    client = _client(FakeSandboxService(), permissions=["qcRules"], records=records)

    assert client.get("/api/v1/rule-tests/records").status_code == 403
    assert client.get(f"/api/v1/rule-tests/records/{RECORD_ID}").status_code == 403
    assert client.get(f"/api/v1/rule-tests/records/{RECORD_ID}/screenshots/1").status_code == 403
    assert (
        client.post(
            "/api/v1/rule-tests/records/delete", json={"record_ids": [RECORD_ID]}
        ).status_code
        == 403
    )
    assert records.delete_calls == []


def test_rule_sandbox_service_judges_in_manual_test_mode() -> None:
    """规则测试页拿的是人挑的证据：它自己的判定实例必须开着人工测试模式，正式任务默认关着。"""
    from services.api.app.infrastructure.llm_gateway.semantic_rule_judge import (
        LLMSemanticRuleJudge,
    )

    service = rule_tests_module._get_rule_sandbox_service.__wrapped__()
    assert service._semantic_judge_factory()._manual_test is True
    assert LLMSemanticRuleJudge()._manual_test is False


def test_text_read_from_screenshots_is_saved_into_the_record_and_returned() -> None:
    """只传了截图：模型读出的页面文字既回给页面，也存进记录——记录里的文字要和判定用的一致。"""
    service = FakeSandboxService(screenshot_text="账号安全 登录账号 退出登录")
    records = FakeRecordService()
    client = _client(service, records=records)

    response = client.post(
        "/api/v1/rule-tests",
        data={"rule_id": RULE_ID, "page_type": "settings"},
        files=[("screenshots", ("shot.jpg", _image("JPEG"), "image/jpeg"))],
    )

    assert response.status_code == 200, response.text
    assert response.json()["screenshot_text"] == "账号安全 登录账号 退出登录"
    assert records.saved[0]["evidence"].visible_text == "账号安全 登录账号 退出登录"

    # 测试人员自己填了页面文字：记录存的还是他填的
    typed = client.post(
        "/api/v1/rule-tests",
        data={"rule_id": RULE_ID, "page_type": "settings", "visible_text": "我填的文字"},
        files=[("screenshots", ("shot.jpg", _image("JPEG"), "image/jpeg"))],
    )
    assert typed.status_code == 200, typed.text
    assert records.saved[1]["evidence"].visible_text == "我填的文字"
