from datetime import UTC, datetime

import pytest

from services.api.app.domain import task as task_domain
from services.api.app.domain.interaction import SurfaceKind


def test_app_target_requires_platform_build_reference() -> None:
    with pytest.raises(task_domain.TaskTargetValidationError, match="platform_build_ref"):
        task_domain.TaskTarget(platform=task_domain.TaskTargetPlatform.APP)


def test_app_target_rejects_url_instead_of_build_reference() -> None:
    with pytest.raises(task_domain.TaskTargetValidationError, match="target_url 必须为空"):
        task_domain.TaskTarget(
            platform=task_domain.TaskTargetPlatform.APP,
            target_url="app://demo",
            app=task_domain.AppTaskConfig(platform_build_ref="app-build:01JABC"),
        )


@pytest.mark.parametrize("target_url", [None, "", "ftp://example.test/app"])
def test_h5_target_requires_http_url(target_url: str | None) -> None:
    with pytest.raises(task_domain.TaskTargetValidationError, match=r"HTTP\(S\) target_url"):
        task_domain.TaskTarget(
            platform=task_domain.TaskTargetPlatform.H5,
            target_url=target_url,
        )


def test_business_platform_maps_to_runtime_surface_without_app_alias() -> None:
    h5 = task_domain.TaskTarget(
        platform=task_domain.TaskTargetPlatform.H5,
        target_url="https://example.test/mobile",
    )
    app = task_domain.TaskTarget(
        platform=task_domain.TaskTargetPlatform.APP,
        app=task_domain.AppTaskConfig(platform_build_ref="app-build:01JABC"),
    )

    assert h5.surface_kind is SurfaceKind.WEB
    assert app.surface_kind is SurfaceKind.ANDROID


def test_web_target_requires_dedicated_browser_configuration() -> None:
    with pytest.raises(task_domain.TaskTargetValidationError, match="Web 任务必须提供"):
        task_domain.TaskTarget(
            platform=task_domain.TaskTargetPlatform.WEB,
            target_url="https://example.test",
        )


def test_web_target_serializes_browser_profile_without_app_configuration() -> None:
    target = task_domain.TaskTarget(
        platform=task_domain.TaskTargetPlatform.WEB,
        target_url="https://example.test",
        web=task_domain.WebTaskConfig(
            browser_profile_id="desktop_fhd_1920x1080_v1",
            adult_self_attestation=task_domain.AdultSelfAttestation(
                declared=True,
                statement_version="adult-v1",
                declared_by="operator-1",
                declared_at=datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
            ),
        ),
    )

    assert target.to_dict() == {
        "platform": "web",
        "target_url": "https://example.test",
        "app": None,
        "web": {
            "browser_profile_id": "desktop_fhd_1920x1080_v1",
            "adult_self_attestation": {
                "declared": True,
                "statement_version": "adult-v1",
                "declared_by": "operator-1",
                "declared_at": "2026-08-30T12:00:00Z",
            },
        },
    }


def test_web_target_allows_missing_adult_self_attestation() -> None:
    target = task_domain.TaskTarget(
        platform=task_domain.TaskTargetPlatform.WEB,
        target_url="https://example.test",
        web=task_domain.WebTaskConfig(
            browser_profile_id="desktop_fhd_1920x1080_v1",
        ),
    )

    assert target.web is not None
    assert target.web.adult_self_attestation is None


@pytest.mark.parametrize(
    ("declared", "declared_at", "message"),
    [
        (False, datetime(2026, 8, 30, 12, 0, tzinfo=UTC), "必须主动声明成年"),
        (True, datetime(2026, 8, 30, 12, 0), "时区"),
    ],
)
def test_adult_self_attestation_rejects_invalid_declaration(
    declared: bool,
    declared_at: datetime,
    message: str,
) -> None:
    with pytest.raises(task_domain.TaskTargetValidationError, match=message):
        task_domain.AdultSelfAttestation(
            declared=declared,
            statement_version="adult-v1",
            declared_by="operator-1",
            declared_at=declared_at,
        )


def test_h5_target_rejects_web_browser_configuration() -> None:
    with pytest.raises(task_domain.TaskTargetValidationError, match="H5 任务不能携带 Web"):
        task_domain.TaskTarget(
            platform=task_domain.TaskTargetPlatform.H5,
            target_url="https://example.test",
            web=task_domain.WebTaskConfig(
                browser_profile_id="desktop_fhd_1920x1080_v1",
            ),
        )


def test_app_target_serializes_only_stable_device_and_build_references() -> None:
    target = task_domain.TaskTarget(
        platform=task_domain.TaskTargetPlatform.APP,
        app=task_domain.AppTaskConfig(
            platform_build_ref="app-build:01JABC",
            package_name="com.example.target",
            device_backend="cloud_phone",
            device_profile_id="default",
        ),
    )

    assert target.to_dict() == {
        "platform": "app",
        "target_url": None,
        "app": {
            "platform_build_ref": "app-build:01JABC",
            "package_name": "com.example.target",
            "device": {
                "backend": "cloud_phone",
                "profile_id": "default",
            },
        },
    }
