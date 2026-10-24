"""Mobile Demo 階段與雷電路徑解析測試。"""


from services.api.app.infrastructure.device.ldplayer import (
    resolve_ldplayer_home,
    resolve_mobile_demo_stage,
)


def test_resolve_mobile_demo_stage_explicit_dev(monkeypatch) -> None:
    monkeypatch.delenv("ALIOTH_LDPLAYER_HOME", raising=False)
    monkeypatch.setenv("ALIOTH_MOBILE_DEMO_STAGE", "dev")
    monkeypatch.setenv("ALIOTH_ENVIRONMENT", "production")
    assert resolve_mobile_demo_stage() == "dev"


def test_resolve_mobile_demo_stage_explicit_prod(monkeypatch) -> None:
    monkeypatch.delenv("ALIOTH_LDPLAYER_HOME", raising=False)
    monkeypatch.setenv("ALIOTH_MOBILE_DEMO_STAGE", "prod")
    monkeypatch.setenv("ALIOTH_ENVIRONMENT", "local")
    assert resolve_mobile_demo_stage() == "prod"


def test_resolve_mobile_demo_stage_from_environment(monkeypatch) -> None:
    monkeypatch.delenv("ALIOTH_MOBILE_DEMO_STAGE", raising=False)
    monkeypatch.delenv("ALIOTH_LDPLAYER_HOME", raising=False)
    monkeypatch.setenv("ALIOTH_ENVIRONMENT", "production")
    assert resolve_mobile_demo_stage() == "prod"


def test_resolve_ldplayer_home_by_stage_env(monkeypatch) -> None:
    monkeypatch.delenv("ALIOTH_LDPLAYER_HOME", raising=False)
    monkeypatch.setenv("ALIOTH_LDPLAYER_HOME_DEV", r"E:\Dev\LDPlayer9")
    monkeypatch.setenv("ALIOTH_LDPLAYER_HOME_PROD", r"E:\Prod\LDPlayer9")
    monkeypatch.setenv("ALIOTH_MOBILE_DEMO_STAGE", "dev")
    assert str(resolve_ldplayer_home()).replace("/", "\\") == r"E:\Dev\LDPlayer9"
    monkeypatch.setenv("ALIOTH_MOBILE_DEMO_STAGE", "prod")
    assert str(resolve_ldplayer_home()).replace("/", "\\") == r"E:\Prod\LDPlayer9"


def test_resolve_ldplayer_home_by_stage(monkeypatch) -> None:
    monkeypatch.delenv("ALIOTH_LDPLAYER_HOME", raising=False)
    monkeypatch.delenv("ALIOTH_LDPLAYER_HOME_DEV", raising=False)
    monkeypatch.delenv("ALIOTH_LDPLAYER_HOME_PROD", raising=False)
    monkeypatch.setenv("ALIOTH_MOBILE_DEMO_STAGE", "dev")
    assert str(resolve_ldplayer_home()).replace("/", "\\") == r"F:\LDPlayer\LDPlayer9"

    monkeypatch.setenv("ALIOTH_MOBILE_DEMO_STAGE", "prod")
    assert str(resolve_ldplayer_home()).replace("/", "\\") == r"C:\LDPlayer\LDPlayer9"


def test_resolve_ldplayer_home_override(monkeypatch) -> None:
    monkeypatch.setenv("ALIOTH_MOBILE_DEMO_STAGE", "prod")
    monkeypatch.setenv("ALIOTH_LDPLAYER_HOME", r"D:\Custom\LDPlayer9")
    assert str(resolve_ldplayer_home()).replace("/", "\\") == r"D:\Custom\LDPlayer9"
