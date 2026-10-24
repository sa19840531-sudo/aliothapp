from __future__ import annotations

from services.api.app.infrastructure.device.android_runtime_observation import (
    parse_android_ui_hierarchy,
)

UI_XML = """
<hierarchy rotation="0">
  <node index="0" text="" resource-id="" class="android.widget.FrameLayout"
        package="com.example.demo" content-desc="" bounds="[0,0][1080,1920]"
        visible-to-user="true" enabled="true" clickable="false" focusable="false">
    <node index="0" text="关闭" resource-id="com.example.demo:id/close"
          class="android.widget.ImageButton" package="com.example.demo"
          content-desc="关闭广告" bounds="[100,200][180,280]"
          visible-to-user="true" enabled="true" clickable="true" focusable="true" />
    <node index="1" text="下载" resource-id="com.example.demo:id/download"
          class="android.widget.Button" package="com.example.demo"
          content-desc="立即下载" bounds="[400,1400][680,1500]"
          visible-to-user="true" enabled="true" clickable="true" focusable="true" />
    <node index="2" text="首页" resource-id="com.example.demo:id/home"
          class="android.widget.TextView" package="com.example.demo"
          content-desc="首页" bounds="[0,1760][216,1920]"
          visible-to-user="true" enabled="true" clickable="true" focusable="true" />
    <node index="3" text="我的" resource-id="com.example.demo:id/profile"
          class="android.widget.TextView" package="com.example.demo"
          content-desc="我的" bounds="[864,1760][1080,1920]"
          visible-to-user="true" enabled="true" clickable="true" focusable="true" />
  </node>
</hierarchy>
"""

WEBVIEW_ONLY_XML = """
<hierarchy rotation="0">
  <node index="0" text="" resource-id="" class="android.webkit.WebView"
        package="com.example.demo" content-desc="" bounds="[0,0][1080,1920]"
        visible-to-user="true" enabled="true" clickable="false" focusable="false" />
</hierarchy>
"""

NO_FOOTER_CONTENT_XML = """
<hierarchy rotation="0">
  <node index="0" text="" resource-id="" class="android.widget.FrameLayout"
        package="com.example.demo" content-desc="" bounds="[0,0][1080,1920]"
        visible-to-user="true" enabled="true" clickable="false" focusable="false">
    <node index="0" text="推荐内容" resource-id="com.example.demo:id/title"
          class="android.widget.TextView" package="com.example.demo"
          content-desc="" bounds="[40,120][600,180]"
          visible-to-user="true" enabled="true" clickable="false" focusable="false" />
  </node>
</hierarchy>
"""


def test_runtime_observation_parses_actual_nodes_and_safe_tabs() -> None:
    observation = parse_android_ui_hierarchy(UI_XML, width=1080, height=1920)

    assert observation.available is True
    assert observation.source == "uiautomator"
    assert observation.package_name == "com.example.demo"
    assert {node.node_id for node in observation.nodes} >= {
        "com.example.demo:id/close",
        "com.example.demo:id/home",
        "com.example.demo:id/profile",
    }
    assert set(observation.safe_tab_ids) == {
        "com.example.demo:id/home",
        "com.example.demo:id/profile",
    }
    assert observation.safe_tabs_exhausted is False
    assert observation.dismiss_node_at_point(120, 220) is not None
    download_node = observation.node_at_point(540, 1450)
    assert download_node is not None
    assert download_node.has_dangerous_action_semantics is True


def test_runtime_observation_only_marks_tabs_exhausted_from_runtime_ids() -> None:
    observation = parse_android_ui_hierarchy(
        UI_XML,
        width=1080,
        height=1920,
        explored_safe_tab_ids={
            "com.example.demo:id/home",
            "com.example.demo:id/profile",
        },
    )

    assert observation.safe_tabs_exhausted is True
    assert observation.unexplored_safe_tab_ids == ()
    assert observation.has_meaningful_content is True


def test_webview_only_observation_does_not_exhaust_tabs() -> None:
    observation = parse_android_ui_hierarchy(
        WEBVIEW_ONLY_XML,
        width=1080,
        height=1920,
    )

    assert observation.safe_tab_ids == ()
    assert observation.has_meaningful_content is False
    assert observation.safe_tabs_exhausted is False


def test_no_footer_content_observation_can_exhaust_tabs() -> None:
    observation = parse_android_ui_hierarchy(
        NO_FOOTER_CONTENT_XML,
        width=1080,
        height=1920,
    )

    assert observation.safe_tab_ids == ()
    assert observation.has_meaningful_content is True
    assert observation.safe_tabs_exhausted is True


def test_runtime_observation_rejects_malformed_hierarchy() -> None:
    try:
        parse_android_ui_hierarchy("not xml", width=1080, height=1920)
    except ValueError as exc:
        assert "UI hierarchy" in str(exc)
    else:
        raise AssertionError("malformed UI hierarchy must be rejected")
