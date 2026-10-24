"""按包名加载 APP 产品 Profile。

Agent 只按当前 ``package_name`` 加载一份摘要：关键场景落点 + 时序规则。
全量脚本坐标、弹窗关闭坐标 **不** 作为可执行 tap 灌给模型——弹窗在则云手机 back。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROFILES_ROOT = Path(__file__).resolve().parent

# 这些键即使出现在 profile.json.taps，也不会进入 Agent 可执行落点摘要。
_VISION_ONLY_TAP_MARKERS = (
    "popup_close",
    "close_x",
    "close_popup",
    "ad_close",
    "dismiss_x",
    "scrim_dismiss",
)

_DEFAULT_CLOUD_BACK = (1120, 2955)


def is_vision_only_tap_key(key: str) -> bool:
    """弹窗关闭类落点键：不得作为 Agent 可执行 Profile tap。"""
    folded = (key or "").strip().casefold()
    return any(marker in folded for marker in _VISION_ONLY_TAP_MARKERS)


def _is_vision_only_tap_key(key: str) -> bool:
    return is_vision_only_tap_key(key)


def _as_xy(value: Any) -> tuple[int, int] | None:
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return int(value[0]), int(value[1])
    return None


@dataclass(frozen=True)
class AppProductProfile:
    """单个 APP 产品的编排知识快照。"""

    package_name: str
    slug: str
    display_name: str
    profile_dir: Path
    markdown: str
    data: dict[str, Any]

    @property
    def cloud_back_tap(self) -> tuple[int, int]:
        raw = self.data.get("cloud_back_tap") or list(_DEFAULT_CLOUD_BACK)
        point = _as_xy(raw)
        return point if point is not None else _DEFAULT_CLOUD_BACK

    @property
    def gesture_y_max(self) -> int:
        raw = self.data.get("gesture_y_max")
        if raw is None:
            return 2794
        return int(raw)

    @property
    def taps(self) -> dict[str, tuple[int, int]]:
        """原始 taps（含可能误写入的关窗键；执行层请用 key_taps）。"""
        out: dict[str, tuple[int, int]] = {}
        for key, value in (self.data.get("taps") or {}).items():
            point = _as_xy(value)
            if point is not None:
                out[str(key)] = point
        return out

    @property
    def key_taps(self) -> dict[str, tuple[int, int]]:
        """给 Agent / Tool 的可执行关键落点：排除弹窗关闭类键。"""
        return {
            key: xy
            for key, xy in self.taps.items()
            if not _is_vision_only_tap_key(key)
        }

    @property
    def timing_rules(self) -> list[dict[str, Any]]:
        rules = self.data.get("timing_rules") or []
        return [dict(item) for item in rules if isinstance(item, dict)]

    @property
    def popup_close_policy(self) -> str:
        """关窗策略：默认 watcher_cascade（文案→几何X→蒙层→单次「<」）。"""
        raw = self.data.get("popup_close_policy")
        if isinstance(raw, str) and raw.strip():
            # 遗留 cloud_back／twice 产品配置统一升为 cascade。
            folded = raw.strip().casefold()
            if folded in {"cloud_back", "cloud_back_twice"}:
                return "watcher_cascade"
            return raw.strip()
        return "watcher_cascade"

    @property
    def popup_notes(self) -> dict[str, Any]:
        notes = self.data.get("popup_notes") or {}
        return dict(notes) if isinstance(notes, dict) else {}

    @property
    def popup_close_by_type(self) -> dict[str, Any]:
        """产品覆盖的按类型关窗表（与默认表合并由 orchestration 完成）。"""
        raw = self.data.get("popup_close_by_type") or {}
        return dict(raw) if isinstance(raw, dict) else {}

    def clamp_tap_y(self, x: int, y: int) -> tuple[int, int]:
        """底栏/手势带：y 不得超过 gesture_y_max。"""
        return int(x), min(int(y), self.gesture_y_max)

    def to_capability_facts(self) -> dict[str, Any]:
        """F04：只读能力事实。禁止向 Agent／Explorer 授予绝对坐标或 taps。"""
        from services.api.app.domain.app_product.popup_close import (
            DEFAULT_POPUP_CLOSE_BY_TYPE,
        )

        merged_types: dict[str, Any] = {
            key: {
                k: v
                for k, v in dict(value).items()
                if k
                not in {
                    "x",
                    "y",
                    "tap",
                    "point",
                    "coords",
                    "normalized_bounds",
                    "absolute_xy",
                }
            }
            for key, value in DEFAULT_POPUP_CLOSE_BY_TYPE.items()
            if isinstance(value, dict)
        }
        for key, value in self.popup_close_by_type.items():
            if isinstance(value, dict):
                cleaned = {
                    k: v
                    for k, v in value.items()
                    if k
                    not in {
                        "x",
                        "y",
                        "tap",
                        "point",
                        "coords",
                        "normalized_bounds",
                        "absolute_xy",
                    }
                }
                merged_types[key] = {**merged_types.get(key, {}), **cleaned}

        timing_rules = [_sanitize_capability_mapping(item) for item in self.timing_rules]
        curriculum: list[dict[str, Any]] = []
        for item in self.data.get("exploration_curriculum") or []:
            if not isinstance(item, dict):
                continue
            curriculum.append(
                {
                    key: item[key]
                    for key in (
                        "id",
                        "step_id",
                        "definition_id",
                        "semantic_target",
                        "when",
                        "hint",
                        "purpose",
                        "status",
                    )
                    if key in item
                }
            )

        recovery_hints: list[str] = []
        for rule in timing_rules:
            action = str(rule.get("action") or "").strip()
            when = str(rule.get("when") or "").strip()
            if action and when:
                recovery_hints.append(f"{when} → {action}")
            elif action:
                recovery_hints.append(action)

        return {
            "ok": True,
            "package_name": self.package_name,
            "slug": self.slug,
            "display_name": self.display_name,
            "profile_present": True,
            "grants_absolute_coordinates": False,
            "execution_policy": "agent_profile_only",
            "popup_close_policy": self.popup_close_policy,
            "popup_notes": self.popup_notes,
            "popup_close_by_type": merged_types,
            "timing_rules": timing_rules,
            "exploration_curriculum": curriculum,
            "semantic_recovery_hints": recovery_hints,
            "deprecated_product_script": self.data.get("deprecated_product_script")
            or self.data.get("source_script_deprecated")
            or self.data.get("source_script"),
            "agent_loading_rule": (
                "只读能力事实：按当前 package_name 注入；"
                "禁止用 Profile 坐标／taps／cloud_back_tap 覆盖当前帧候选；"
                "无 Profile 走通用探索，不得要求新产品必填点位档案；"
                "开屏→语义进入/跳过；弹窗→Watcher cascade／语义 BACK，不找圆×。"
            ),
        }

    def to_agent_summary(self) -> dict[str, Any]:
        """兼容旧名：Agent／Tool 只拿能力事实，不再附带可执行坐标。"""

        return self.to_capability_facts()


def _sanitize_capability_mapping(item: Mapping[str, Any] | dict[str, Any]) -> dict[str, Any]:
    """去掉映射中的绝对点位字段，保留语义动作与条件。"""

    blocked = {
        "x",
        "y",
        "tap",
        "point",
        "coords",
        "normalized_bounds",
        "absolute_xy",
        "cloud_back_tap",
    }
    out: dict[str, Any] = {}
    for key, value in dict(item).items():
        if key in blocked:
            continue
        if (
            isinstance(value, (list, tuple))
            and len(value) == 2
            and all(isinstance(part, (int, float)) for part in value)
        ):
            continue
        out[str(key)] = value
    return out


def build_app_product_capability_facts(package_name: str | None) -> dict[str, Any]:
    """按包名构建可注入 Explorer 的只读能力事实；无 Profile 返回通用占位。"""

    package = str(package_name or "").strip()
    if not package:
        return {
            "ok": False,
            "profile_present": False,
            "grants_absolute_coordinates": False,
            "error": "package_name_missing",
            "agent_loading_rule": "无包名时不注入产品坐标；走通用候选探索。",
        }
    profile = load_profile(package)
    if profile is None:
        return {
            "ok": False,
            "package_name": package,
            "profile_present": False,
            "grants_absolute_coordinates": False,
            "error": "profile_not_found",
            "hint": "无 Profile 时走通用探索；勿为新产品补绝对点位脚本",
            "agent_loading_rule": (
                "未知包名允许通用探索；Profile 非必填，不得授予坐标。"
            ),
        }
    return profile.to_capability_facts()


def _package_matches(left: str, right: str) -> bool:
    return (left or "").strip().casefold() == (right or "").strip().casefold()


def resolve_profile_dir(package_name: str) -> Path | None:
    """返回包名对应 Profile 目录；不存在则 None（大小写不敏感）。"""
    package = (package_name or "").strip()
    if not package:
        return None
    # 目录名优先精确，再 casefold 扫一遍
    direct = PROFILES_ROOT / package
    if (direct / "PROFILE.md").is_file() or (direct / "profile.json").is_file():
        return direct
    folded = package.casefold()
    for path in PROFILES_ROOT.iterdir():
        if not path.is_dir() or path.name.startswith("_"):
            continue
        if path.name.casefold() == folded and (
            (path / "PROFILE.md").is_file() or (path / "profile.json").is_file()
        ):
            return path
        meta = path / "profile.json"
        if not meta.is_file():
            continue
        try:
            payload = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if _package_matches(str(payload.get("package_name") or ""), package):
            return path
    return None


def list_profile_packages() -> list[str]:
    """列出已有 profile.json 的 package_name。"""
    names: list[str] = []
    for path in sorted(PROFILES_ROOT.iterdir()):
        if not path.is_dir() or path.name.startswith("_"):
            continue
        meta = path / "profile.json"
        if not meta.is_file():
            continue
        try:
            payload = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        package = str(payload.get("package_name") or path.name).strip()
        if package:
            names.append(package)
    return names


def load_profile(package_name: str) -> AppProductProfile | None:
    """加载指定包名的 Profile；找不到返回 None。"""
    profile_dir = resolve_profile_dir(package_name)
    if profile_dir is None:
        return None
    md_path = profile_dir / "PROFILE.md"
    json_path = profile_dir / "profile.json"
    markdown = md_path.read_text(encoding="utf-8") if md_path.is_file() else ""
    data: dict[str, Any] = {}
    if json_path.is_file():
        data = json.loads(json_path.read_text(encoding="utf-8"))
    package = str(data.get("package_name") or package_name).strip()
    return AppProductProfile(
        package_name=package,
        slug=str(data.get("slug") or profile_dir.name),
        display_name=str(data.get("display_name") or package),
        profile_dir=profile_dir,
        markdown=markdown,
        data=data,
    )
