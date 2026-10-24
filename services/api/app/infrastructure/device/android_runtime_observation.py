"""Android 运行时 UI 层级观察与事实判断。

该模块只负责把受控的 ``uiautomator`` XML 转成内存中的运行时事实，
不负责截图持久化，也不接受模型生成的节点、文本或边界作为事实来源。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from services.api.app.infrastructure.device.adb_ops import (
    CANONICAL_HEIGHT,
    CANONICAL_WIDTH,
    map_point,
)

_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
_DISMISS_HINTS = (
    "关闭",
    "取消",
    "跳过",
    "知道了",
    "明白了",
    "got it",
    "close",
    "cancel",
    "dismiss",
    "skip",
)
_DANGEROUS_ACTION_HINTS = (
    "下载",
    "下載",
    "立即下载",
    "立即下載",
    "购买",
    "購買",
    "立即购买",
    "立即購買",
    "订阅",
    "訂閱",
    "充值",
    "安装",
    "安裝",
    "download",
    "purchase",
    "subscribe",
    "buy",
    "recharge",
    "install",
)
_FORBIDDEN_TAB_HINTS = (
    "下载",
    "充值",
    "安装",
    "download",
    "recharge",
    "install",
)


@dataclass(frozen=True, slots=True)
class AndroidBounds:
    """运行时控件的设备像素边界。"""

    left: int
    top: int
    right: int
    bottom: int

    def __post_init__(self) -> None:
        if self.left < 0 or self.top < 0 or self.right <= self.left or self.bottom <= self.top:
            raise ValueError("Android 控件边界必须为正向设备像素矩形")

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    def contains(self, x: int, y: int) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom


@dataclass(frozen=True, slots=True)
class AndroidUiNode:
    """从 Android UI 层级解析出的单个可审计节点。"""

    node_id: str
    class_name: str
    resource_id: str
    text: str
    content_desc: str
    bounds: AndroidBounds
    visible_to_user: bool
    enabled: bool
    clickable: bool
    focusable: bool

    @property
    def label(self) -> str:
        return " ".join(
            value.strip()
            for value in (self.text, self.content_desc, self.resource_id)
            if value and value.strip()
        )

    @property
    def has_dangerous_action_semantics(self) -> bool:
        """危险语义覆盖可见文案与资源 ID 末段，优先于关闭语义。"""
        resource_name = self.resource_id.rsplit("/", 1)[-1]
        action_label = " ".join(
            value.strip()
            for value in (self.text, self.content_desc, resource_name)
            if value and value.strip()
        ).casefold()
        return any(
            hint.casefold() in action_label for hint in _DANGEROUS_ACTION_HINTS
        )

    @property
    def is_dismiss_control(self) -> bool:
        if not (self.visible_to_user and self.enabled and self.clickable):
            return False
        visible_label = " ".join(
            value.strip()
            for value in (self.text, self.content_desc)
            if value and value.strip()
        ).casefold()
        resource_label = self.resource_id.casefold()
        if visible_label:
            if self.has_dangerous_action_semantics:
                return False
            return any(hint.casefold() in visible_label for hint in _DISMISS_HINTS)
        if self.has_dangerous_action_semantics:
            return False
        return any(hint.casefold() in resource_label for hint in _DISMISS_HINTS)


@dataclass(frozen=True, slots=True)
class AndroidRuntimeObservation:
    """一次不持久化的 Android UI 运行时观察。"""

    available: bool
    source: str
    observed_at: datetime
    width: int
    height: int
    nodes: tuple[AndroidUiNode, ...] = ()
    package_name: str | None = None
    activity_name: str | None = None
    safe_tab_ids: tuple[str, ...] = ()
    unexplored_safe_tab_ids: tuple[str, ...] = ()
    safe_tabs_exhausted: bool = False
    has_meaningful_content: bool = False
    failure_reason: str | None = None

    @classmethod
    def unavailable(
        cls,
        *,
        width: int,
        height: int,
        failure_reason: str,
    ) -> AndroidRuntimeObservation:
        return cls(
            available=False,
            source="uiautomator",
            observed_at=datetime.now(UTC),
            width=width,
            height=height,
            failure_reason=failure_reason[:240],
        )

    def node_at_point(
        self,
        x: float,
        y: float,
        *,
        coordinate_space: str = "canonical_px",
        base_width: int = CANONICAL_WIDTH,
        base_height: int = CANONICAL_HEIGHT,
    ) -> AndroidUiNode | None:
        if not self.available:
            return None
        device_x, device_y = map_point(
            x,
            y,
            device_w=self.width,
            device_h=self.height,
            base_w=base_width,
            base_h=base_height,
            space=coordinate_space,
        )
        candidates = [
            node
            for node in self.nodes
            if node.visible_to_user and node.enabled and node.bounds.contains(device_x, device_y)
        ]
        if not candidates:
            return None
        # 子节点通常比父容器更接近真实点击目标，优先选择面积更小的节点。
        return min(
            candidates,
            key=lambda node: (node.bounds.width * node.bounds.height, node.node_id),
        )

    def dismiss_node_at_point(
        self,
        x: float,
        y: float,
        *,
        coordinate_space: str = "canonical_px",
        base_width: int = CANONICAL_WIDTH,
        base_height: int = CANONICAL_HEIGHT,
    ) -> AndroidUiNode | None:
        node = self.node_at_point(
            x,
            y,
            coordinate_space=coordinate_space,
            base_width=base_width,
            base_height=base_height,
        )
        return node if node is not None and node.is_dismiss_control else None

    def safe_tab_at_point(
        self,
        x: float,
        y: float,
        *,
        coordinate_space: str = "canonical_px",
        base_width: int = CANONICAL_WIDTH,
        base_height: int = CANONICAL_HEIGHT,
    ) -> AndroidUiNode | None:
        node = self.node_at_point(
            x,
            y,
            coordinate_space=coordinate_space,
            base_width=base_width,
            base_height=base_height,
        )
        if node is None or node.node_id not in self.safe_tab_ids:
            return None
        return node


def parse_android_ui_hierarchy(
    xml: str,
    *,
    width: int,
    height: int,
    explored_safe_tab_ids: Iterable[str] = (),
    observed_at: datetime | None = None,
) -> AndroidRuntimeObservation:
    """解析 ``uiautomator dump`` 输出，剔除命令行前后附带的非 XML 文本。"""
    if width <= 0 or height <= 0:
        raise ValueError("Android 屏幕尺寸必须为正数")
    raw = str(xml or "")
    start = raw.find("<hierarchy")
    end = raw.rfind("</hierarchy>")
    if start < 0 or end < start:
        raise ValueError("UI hierarchy XML 缺少 hierarchy 根节点")
    xml_fragment = raw[start : end + len("</hierarchy>")]
    try:
        root = ET.fromstring(xml_fragment)
    except ET.ParseError as exc:
        raise ValueError("UI hierarchy XML 解析失败") from exc

    nodes: list[AndroidUiNode] = []
    resource_id_counts: dict[str, int] = {}
    for path, element in _walk_with_paths(root):
        bounds = _parse_bounds(element.attrib.get("bounds"))
        if bounds is None:
            continue
        resource_id = (element.attrib.get("resource-id") or "").strip()
        resource_id_counts[resource_id] = resource_id_counts.get(resource_id, 0) + 1
        nodes.append(
            AndroidUiNode(
                node_id=resource_id or f"node:{path}",
                class_name=(element.attrib.get("class") or "").strip(),
                resource_id=resource_id,
                text=(element.attrib.get("text") or "").strip(),
                content_desc=(element.attrib.get("content-desc") or "").strip(),
                bounds=bounds,
                visible_to_user=_parse_bool(element.attrib.get("visible-to-user"), default=True),
                enabled=_parse_bool(element.attrib.get("enabled"), default=True),
                clickable=_parse_bool(element.attrib.get("clickable"), default=False),
                focusable=_parse_bool(element.attrib.get("focusable"), default=False),
            )
        )

    duplicate_resource_ids = {
        resource_id
        for resource_id, count in resource_id_counts.items()
        if resource_id and count > 1
    }
    if duplicate_resource_ids:
        rebuilt: list[AndroidUiNode] = []
        seen: dict[str, int] = {}
        for node in nodes:
            if node.resource_id in duplicate_resource_ids:
                seen[node.resource_id] = seen.get(node.resource_id, 0) + 1
                node = AndroidUiNode(
                    node_id=f"{node.resource_id}#{seen[node.resource_id]}",
                    class_name=node.class_name,
                    resource_id=node.resource_id,
                    text=node.text,
                    content_desc=node.content_desc,
                    bounds=node.bounds,
                    visible_to_user=node.visible_to_user,
                    enabled=node.enabled,
                    clickable=node.clickable,
                    focusable=node.focusable,
                )
            rebuilt.append(node)
        nodes = rebuilt

    safe_tabs = tuple(
        node.node_id
        for node in nodes
        if _is_safe_tab(node, width=width, height=height)
    )
    explored = {str(item) for item in explored_safe_tab_ids if str(item).strip()}
    unexplored = tuple(node_id for node_id in safe_tabs if node_id not in explored)
    has_meaningful_content = any(
        _is_meaningful_content_node(node, width=width, height=height)
        for node in nodes
    )
    package_name = _first_attribute(root, "package")
    activity_name = _first_attribute(root, "activity")
    return AndroidRuntimeObservation(
        available=True,
        source="uiautomator",
        observed_at=observed_at or datetime.now(UTC),
        width=width,
        height=height,
        nodes=tuple(nodes),
        package_name=package_name,
        activity_name=activity_name,
        safe_tab_ids=safe_tabs,
        unexplored_safe_tab_ids=unexplored,
        safe_tabs_exhausted=not unexplored and (bool(safe_tabs) or has_meaningful_content),
        has_meaningful_content=has_meaningful_content,
    )


def _walk_with_paths(root: ET.Element) -> Iterable[tuple[str, ET.Element]]:
    def visit(element: ET.Element, path: str) -> Iterable[tuple[str, ET.Element]]:
        yield path, element
        for index, child in enumerate(list(element)):
            yield from visit(child, f"{path}/{index}")

    return visit(root, "0")


def _parse_bounds(raw: str | None) -> AndroidBounds | None:
    match = _BOUNDS_RE.fullmatch((raw or "").strip())
    if not match:
        return None
    left, top, right, bottom = (int(value) for value in match.groups())
    try:
        return AndroidBounds(left=left, top=top, right=right, bottom=bottom)
    except ValueError:
        return None


def _parse_bool(raw: str | None, *, default: bool) -> bool:
    if raw is None:
        return default
    return raw.strip().casefold() == "true"


def _first_attribute(root: ET.Element, name: str) -> str | None:
    for element in root.iter():
        value = (element.attrib.get(name) or "").strip()
        if value:
            return value
    return None


def _is_safe_tab(node: AndroidUiNode, *, width: int, height: int) -> bool:
    if not (node.visible_to_user and node.enabled and node.clickable):
        return False
    if not node.label.strip():
        return False
    if any(hint.casefold() in node.label.casefold() for hint in _FORBIDDEN_TAB_HINTS):
        return False
    bottom_zone = node.bounds.top >= int(height * 0.72)
    compact_height = node.bounds.height <= max(180, int(height * 0.18))
    compact_width = node.bounds.width <= max(360, int(width * 0.5))
    return bottom_zone and compact_height and compact_width


def _is_meaningful_content_node(node: AndroidUiNode, *, width: int, height: int) -> bool:
    """识别能证明仍在目标 App 内容页内的可见内容，空 WebView 壳不算完成事实。"""
    if not (node.visible_to_user and node.enabled):
        return False
    if _is_safe_tab(node, width=width, height=height) or node.is_dismiss_control:
        return False
    label = " ".join(
        value.strip()
        for value in (node.text, node.content_desc)
        if value and value.strip()
    )
    if label:
        return True
    class_name = node.class_name.casefold()
    full_screen_shell = (
        node.bounds.width >= int(width * 0.9)
        and node.bounds.height >= int(height * 0.9)
    )
    if "android.webkit.webview" in class_name and full_screen_shell:
        return False
    if full_screen_shell and not (node.clickable or node.focusable):
        return False
    return node.clickable or node.focusable
