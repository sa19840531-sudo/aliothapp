"""ADB 缺口修补：旋转仿射、同帧观察、surface_tech。"""

from __future__ import annotations

from pathlib import Path

from services.api.app.infrastructure.device.adb_ops import (
    AdbDeviceOps,
    ScreencapStageTrace,
    apply_display_rotation_to_point,
    map_point_to_device,
    parse_display_rotation,
)
from services.api.app.infrastructure.device.android_surface_tech import (
    APP_SURFACE_TECH_VERSION,
    infer_android_surface_tech,
)
from services.api.app.infrastructure.device.android_target_resolver import (
    CanonicalPointTargetResolver,
)


def test_parse_display_rotation_from_dumpsys_snippets() -> None:
    assert parse_display_rotation("mCurrentOrientation=1") == 90
    assert parse_display_rotation("SurfaceOrientation: 2") == 180
    assert parse_display_rotation("rotation=270") == 270
    assert parse_display_rotation("noise only") is None


def test_map_point_proven_rotation_zero_marks_audit() -> None:
    mapped = map_point_to_device(
        540,
        960,
        device_w=1080,
        device_h=1920,
        base_w=1080,
        base_h=1920,
        space="capture_px",
        display_rotation=0,
    )
    assert (mapped.x, mapped.y) == (540, 960)
    assert mapped.rotation_unproven is False
    assert mapped.display_rotation == 0


def test_map_point_applies_rotation_when_natural_capture_on_landscape_device() -> None:
    """自然竖屏截图基准 + 当前横屏设备 + rotation=90 → 仿射到设备坐标。"""

    mapped = map_point_to_device(
        540,
        960,
        device_w=1920,
        device_h=1080,
        base_w=1080,
        base_h=1920,
        space="capture_px",
        display_rotation=90,
    )
    assert mapped.rotation_unproven is False
    assert mapped.display_rotation == 90
    expected = apply_display_rotation_to_point(
        540,
        960,
        width=1080,
        height=1920,
        rotation=90,
    )
    assert abs(mapped.x - expected[0]) <= 1
    assert abs(mapped.y - expected[1]) <= 1


def test_resolver_from_device_ops_reads_proven_rotation() -> None:
    class FakeOps:
        width = 1080
        height = 1920

        def dump_display_safe_area(self):
            return {
                "left": 0,
                "top": 0,
                "right": 0,
                "bottom": 0,
                "width": 1080,
                "height": 1920,
                "source": "display_bounds",
            }

        def query_display_rotation(self):
            return 0

    resolver = CanonicalPointTargetResolver.from_device_ops(FakeOps())
    assert resolver._display_rotation == 0  # noqa: SLF001


def test_infer_surface_tech_flutter_webview_native_unknown() -> None:
    flutter = infer_android_surface_tech(
        '<?xml version="1.0"?><hierarchy>'
        '<node class="io.flutter.embedding.android.FlutterView" />'
        "</hierarchy>"
    )
    assert flutter["protocol_version"] == APP_SURFACE_TECH_VERSION
    assert flutter["surface_tech"] == "flutter"
    assert flutter["authority"] is False

    web = infer_android_surface_tech(
        '<?xml version="1.0"?><hierarchy>'
        '<node class="android.webkit.WebView" />'
        "</hierarchy>",
        has_meaningful_content=False,
    )
    assert web["surface_tech"] == "webview_shell"

    native = infer_android_surface_tech(
        '<?xml version="1.0"?><hierarchy>'
        + "".join(f'<node class="android.widget.Button" text="{i}" />' for i in range(8))
        + "</hierarchy>"
    )
    assert native["surface_tech"] == "native"

    unknown = infer_android_surface_tech(None)
    assert unknown["surface_tech"] == "unknown"
    assert "ui_hierarchy_unavailable" in unknown["reason_codes"]


def test_capture_same_frame_observation_binds_epoch(tmp_path: Path) -> None:
    def shell_output(args, timeout=30.0):
        del timeout
        joined = " ".join(str(a) for a in args)
        if args[:2] == ["wm", "size"]:
            return "Physical size: 1080x1920"
        if "displays" in args or args[:2] == ["dumpsys", "display"]:
            return "mCurrentOrientation=0\nStableInsets=[0,0][0,0]"
        if args[:2] == ["dumpsys", "window"]:
            return "mCurrentOrientation=0"
        if "uiautomator" in joined or args[:1] == ["cat"]:
            return (
                '<?xml version="1.0"?><hierarchy>'
                '<node class="android.widget.TextView" text="首页" '
                'bounds="[0,0][100,100]" />'
                "</hierarchy>"
            )
        return ""

    def shell_run(args, timeout=30.0):
        del args, timeout

    png = tmp_path / "shot.png"

    def screencap_fn(dest: Path) -> ScreencapStageTrace:
        dest.write_bytes(b"\x00" * 128)
        return ScreencapStageTrace(path_mode="fake", png_bytes=128)

    ops = AdbDeviceOps(
        backend="fake",
        width=1080,
        height=1920,
        _log=lambda _m: None,
        _screencap_fn=screencap_fn,
        _shell_fn=shell_run,
        _shell_output_fn=shell_output,
    )
    ops.current_runtime_identity = (  # type: ignore[method-assign]
        lambda: ("com.example.app", ".Main")
    )

    bundle = ops.capture_same_frame_observation(screenshot_dest=png)
    assert bundle["protocol_version"] == "app-same-frame-observation.v1"
    assert bundle["same_frame_bound"] is True
    assert bundle["atomic_kernel_guarantee"] is False
    assert str(bundle["observation_epoch"]).startswith("same-frame:")
    assert bundle["ui_hierarchy_available"] is True
    assert bundle["display"]["rotation_proven"] is True
    assert bundle["surface_tech"]["surface_tech"] in {"native", "unknown"}
    assert png.exists()
