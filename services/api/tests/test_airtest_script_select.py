"""Airtest 腳本選擇：規則與 AI 回退。"""

from pathlib import Path

import pytest

from services.api.app.application.app_script_mapping import (
    APP_SCRIPT_ROOT_ENV,
    UNTITLED1_SCRIPT,
    UNTITLED2_SCRIPT,
    UNTITLED3_SCRIPT,
    UNTITLED4_SCRIPT,
    UNTITLED5_SCRIPT,
    UNTITLED6_SCRIPT,
    UNTITLED7_SCRIPT,
    UNTITLED_SCRIPT,
    _ai_choose_script,
    heuristic_script_choice,
    resolve_airtest_script,
)
from services.api.app.core.config import get_settings


def test_heuristic_91cg() -> None:
    c = heuristic_script_choice("91cg_1.3.5.apk")
    assert c.script_id == "untitled"
    assert c.path == UNTITLED_SCRIPT
    assert c.source == "heuristic"


def test_heuristic_hlapp() -> None:
    c = heuristic_script_choice("hlapp_demo.apk")
    assert c.script_id == "untitled1"
    assert c.path == UNTITLED1_SCRIPT


def test_heuristic_hlwapp() -> None:
    c = heuristic_script_choice("hlwapp_1.0.0.apk")
    assert c.script_id == "untitled2"
    assert c.path == UNTITLED2_SCRIPT


def test_heuristic_sns_91sp() -> None:
    c = heuristic_script_choice("sns_91sp_1.0.0.apk")
    assert c.script_id == "untitled3"
    assert c.path == UNTITLED3_SCRIPT


def test_heuristic_sns_911bl() -> None:
    c = heuristic_script_choice("sns_911bl_1.0.0.apk")
    assert c.script_id == "untitled4"
    assert c.path == UNTITLED4_SCRIPT


def test_heuristic_sns_51aw() -> None:
    c = heuristic_script_choice("sns_51aw_1.0.0.apk")
    assert c.script_id == "untitled5"
    assert c.path == UNTITLED5_SCRIPT


def test_heuristic_sns_hjsq() -> None:
    c = heuristic_script_choice("sns-hjsq_1.0.0.apk")
    assert c.script_id == "untitled6"
    assert c.path == UNTITLED6_SCRIPT


def test_heuristic_dx093j() -> None:
    c = heuristic_script_choice("dx093j_1.0.0.apk")
    assert c.script_id == "untitled7"
    assert c.path == UNTITLED7_SCRIPT


def test_heuristic_label_heiliao_wang() -> None:
    """黑料网不可誤判成黑料不打烊腳本。"""
    c = heuristic_script_choice("random.apk", app_label="黑料网")
    assert c.script_id == "untitled2"


def test_heuristic_label_heilia() -> None:
    c = heuristic_script_choice("random.apk", app_label="黑料不打烊")
    assert c.script_id == "untitled1"


def test_heuristic_hjll_and_pc51() -> None:
    hjll = heuristic_script_choice(
        "hjll3.1.0.apk",
        package_name="app.p6228f.pa3",
    )
    assert hjll.script_id == "hjll"
    assert hjll.execution_mode == "agent_workflow"
    pc51 = heuristic_script_choice(
        "51pc_5.8.3.apk",
        package_name="vip.dibwo.txbazn",
    )
    assert pc51.script_id == "pc51"
    assert pc51.execution_mode == "agent_workflow"


def test_heuristic_unmatched_routes_to_agent() -> None:
    """未知前綴不得默默落到 untitled.py。"""
    c = heuristic_script_choice("random_unknown_1.0.apk")
    assert c.script_id == "agent_ad_capture"
    assert c.execution_mode == "agent_workflow"


def test_resolve_unmatched_agent(monkeypatch) -> None:
    monkeypatch.delenv("ALIOTH_MOBILE_DEMO_AIRTEST_SCRIPT", raising=False)
    c = resolve_airtest_script("totally_new_app.apk", prefer_ai=False)
    assert c.execution_mode == "agent_workflow"
    assert c.script_id == "agent_ad_capture"


def test_resolve_prefers_env(monkeypatch, tmp_path: Path) -> None:
    custom = tmp_path / "custom.py"
    custom.write_text("#", encoding="utf-8")
    monkeypatch.setenv("ALIOTH_MOBILE_DEMO_AIRTEST_SCRIPT", str(custom))
    c = resolve_airtest_script("hlapp_x.apk", prefer_ai=False)
    assert c.source == "env"
    assert c.path == custom
    assert c.execution_mode == "airtest"


def test_resolve_env_override_missing_script_falls_back_to_agent(
    monkeypatch, tmp_path: Path
) -> None:
    missing = tmp_path / "missing" / "untitled.py"
    monkeypatch.setenv("ALIOTH_MOBILE_DEMO_AIRTEST_SCRIPT", str(missing))

    c = resolve_airtest_script("hlapp_x.apk", prefer_ai=False)

    assert c.script_id == "agent_ad_capture"
    assert c.execution_mode == "agent_workflow"
    assert c.source == "env"
    assert "ALIOTH_MOBILE_DEMO_AIRTEST_SCRIPT" in c.reason
    assert str(missing) in c.reason


def test_resolve_matched_uses_heuristic_when_ai_fails(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("ALIOTH_MOBILE_DEMO_AIRTEST_SCRIPT", raising=False)
    monkeypatch.setenv("ALIOTH_MOBILE_DEMO_AI_SCRIPT_SELECT", "1")
    root = _prepare_script_root(monkeypatch, tmp_path)

    async def _boom(**_kwargs):
        raise RuntimeError("no llm")

    monkeypatch.setattr(
        "services.api.app.application.app_script_mapping._ai_choose_script",
        _boom,
    )
    c = resolve_airtest_script("hlapp_x.apk", prefer_ai=True)
    assert c.script_id == "untitled1"
    assert c.source == "heuristic"
    assert c.execution_mode == "airtest"
    assert c.path == root / "untitled1.air" / "untitled1.py"
    assert "AI" in c.reason


def test_resolve_matched_missing_script_falls_back_to_agent(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("ALIOTH_MOBILE_DEMO_AIRTEST_SCRIPT", raising=False)
    monkeypatch.setenv(APP_SCRIPT_ROOT_ENV, str(tmp_path / "missing-scripts"))

    c = resolve_airtest_script("hlapp_x.apk", prefer_ai=False)

    assert c.script_id == "agent_ad_capture"
    assert c.execution_mode == "agent_workflow"
    assert APP_SCRIPT_ROOT_ENV in c.reason


@pytest.mark.asyncio
async def test_ai_script_choice_uses_physical_request_meter(monkeypatch) -> None:
    monkeypatch.setenv("ALIOTH_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("ALIOTH_LLM_MODEL", "gpt-test")
    get_settings.cache_clear()

    class _Client:
        class chat:
            class completions:
                @staticmethod
                async def create(**_kwargs):
                    message = type(
                        "Message",
                        (),
                        {"content": '{"script_id":"untitled1","reason":"匹配 hlapp"}'},
                    )()
                    choice = type("Choice", (), {"message": message})()
                    return type(
                        "Response",
                        (),
                        {
                            "choices": [choice],
                            "usage": {
                                "prompt_tokens": 21,
                                "completion_tokens": 5,
                                "total_tokens": 26,
                            },
                        },
                    )()

    captured: dict[str, object] = {}

    def build_client(_settings, **kwargs):
        captured.update(kwargs)
        return _Client()

    monkeypatch.setattr(
        "services.api.app.application.app_script_mapping._build_raw_async_client",
        build_client,
    )

    try:
        choice = await _ai_choose_script(
            apk_filename="hlapp_x.apk",
            package_name=None,
            app_label=None,
        )
    finally:
        get_settings.cache_clear()

    assert choice.script_id == "untitled1"
    assert captured == {
        "usage_source": "app_script_mapping",
        "usage_operation": "airtest_script_routing",
    }


def _prepare_script_root(monkeypatch, tmp_path: Path) -> Path:
    root = tmp_path / "airtest-script-root"
    script = root / "untitled1.air" / "untitled1.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("# test", encoding="utf-8")
    monkeypatch.setenv(APP_SCRIPT_ROOT_ENV, str(root))
    return root
