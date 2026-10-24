"""星语表上的 App 缺口：必达页型、旅程步骤、关层核对、登录与断网。

登录只在任务配置了测试账号时允许填凭证。断网只在会话代理已经在听时生效，
代理没起来就保持不支持，不把失败写成断网成功。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

APP_MANDATORY_ARRIVAL_PAGE_TYPES: tuple[str, ...] = (
    "content_detail",
    "video_detail",
    "payment",
    "post_publish",
    "settings",
)

_SEED_PATH = (
    Path(__file__).resolve().parents[4]
    / "docs"
    / "data"
    / "journey_definitions"
    / "p1_4_journeys.json"
)


def merge_required_arrival_page_types(
    declared: Sequence[str] | None,
) -> tuple[str, ...]:
    """任务自己的页型加上详情、播放、支付、发布、设置。"""

    seen: set[str] = set()
    ordered: list[str] = []
    for raw in (*(declared or ()), *APP_MANDATORY_ARRIVAL_PAGE_TYPES):
        text = str(raw or "").strip().casefold()
        if not text or text in seen:
            continue
        seen.add(text)
        ordered.append(text)
    return tuple(ordered)


def popup_close_is_confirmed(
    *,
    popup_was_visible: bool,
    before_overlay: str | None,
    after_overlay: str | None,
    before_image: str | None,
    after_image: str | None,
    still_popup: bool,
) -> bool:
    """点过关闭之后，遮挡签名或截图指纹必须变过，才算真的关掉。"""

    if not popup_was_visible or still_popup:
        return False
    prior_overlay = str(before_overlay or "").strip()
    next_overlay = str(after_overlay or "").strip()
    if prior_overlay and prior_overlay == next_overlay:
        return False
    prior_image = str(before_image or "").strip()
    next_image = str(after_image or "").strip()
    if prior_image and next_image and prior_image == next_image:
        return False
    return True


def credentials_configured(task_config: Mapping[str, Any] | None) -> bool:
    """任务里同时有账号和密码才允许自动填写。"""

    if not isinstance(task_config, Mapping):
        return False
    account = task_config.get("test_account") or task_config.get("login_account")
    if not isinstance(account, Mapping):
        return False
    username = str(account.get("username") or account.get("account") or "").strip()
    password = str(account.get("password") or "").strip()
    return bool(username and password)


def load_android_journey_curriculum(
    *,
    path: Path | None = None,
) -> list[dict[str, str]]:
    """把种子里的 android 动作步骤摊成可推进的课纲，不替数据库再锁第二份计划。"""

    seed = path or _SEED_PATH
    if not seed.is_file():
        return []
    try:
        raw = json.loads(seed.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    steps: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        surfaces = item.get("surface_kinds")
        if isinstance(surfaces, (list, tuple)):
            kinds = {str(value).strip().casefold() for value in surfaces}
            if kinds and "android" not in kinds and "app" not in kinds:
                continue
        definition_id = str(item.get("definition_id") or "").strip()
        if not definition_id:
            continue
        body = item.get("steps")
        if not isinstance(body, list):
            continue
        for step in body:
            if not isinstance(step, Mapping):
                continue
            if str(step.get("type") or "") != "action":
                continue
            semantic = str(step.get("semantic_target") or "").strip()
            if not semantic:
                continue
            steps.append(
                {
                    "definition_id": definition_id,
                    "step_id": str(step.get("id") or "").strip(),
                    "semantic_target": semantic,
                    "status": "pending",
                    "capture_id": "",
                }
            )
    return steps


def ensure_journey_curriculum(
    coverage_metadata: Mapping[str, Any] | None,
    *,
    curriculum: Sequence[Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    out = dict(coverage_metadata) if isinstance(coverage_metadata, Mapping) else {}
    existing = out.get("app_journey_curriculum")
    if isinstance(existing, list) and existing:
        _write_next_journey_target(out)
        return out
    loaded = list(curriculum) if curriculum is not None else load_android_journey_curriculum()
    out["app_journey_curriculum"] = [dict(item) for item in loaded]
    _write_next_journey_target(out)
    return out


def advance_journey_curriculum(
    coverage_metadata: Mapping[str, Any] | None,
    *,
    semantic_target: str | None,
    capture_id: str | None,
) -> dict[str, Any]:
    """当前动作覆盖课纲「下一步」时记 done（F06）。

    课纲仅表示尝试／覆盖进度，不是冻结检查点完成：
    - 必须有非空 ``capture_id``；
    - 只推进**第一个** pending 步（禁止乱序跳步）；
    - 要求课纲全文目标出现在动作语义中（``target in spoken``），
      禁止用短词双向子串（如「会员」误标「开通会员入口」）。
    """

    out = dict(coverage_metadata) if isinstance(coverage_metadata, Mapping) else {}
    rows = out.get("app_journey_curriculum")
    if not isinstance(rows, list):
        return out
    capture = str(capture_id or "").strip()
    spoken = str(semantic_target or "").strip().casefold()
    if not capture or len(spoken) < 2:
        _write_next_journey_target(out)
        return out
    updated: list[dict[str, str]] = []
    marked = False
    awaiting_first_pending = True
    for item in rows:
        row = dict(item) if isinstance(item, Mapping) else {}
        if row.get("status") == "done":
            updated.append({str(key): str(value) for key, value in row.items()})
            continue
        target = str(row.get("semantic_target") or "").strip().casefold()
        if awaiting_first_pending:
            awaiting_first_pending = False
            if (
                not marked
                and target
                and len(target) >= 2
                and target in spoken
            ):
                row["status"] = "done"
                row["capture_id"] = capture
                marked = True
        updated.append({str(key): str(value) for key, value in row.items()})
    out["app_journey_curriculum"] = updated
    _write_next_journey_target(out)
    return out


def evidence_role_for_surface(
    *,
    page_type: str | None,
    page_state: str | None,
    surface_facts: Mapping[str, Any] | None = None,
) -> str:
    """按当前画面给采集一个生产角色，不再只留在合同里。"""

    facts = surface_facts if isinstance(surface_facts, Mapping) else {}
    state = str(page_state or "").strip().casefold()
    ptype = str(page_type or "").strip().casefold()
    if state in {"popup", "modal", "overlay", "dialog"} or facts.get("popup_visible") is True:
        return "overlay_occurrence"
    if facts.get("player_scrub") is True or facts.get("media_seek") is True:
        return "player_scrub_series"
    if ptype == "payment" or facts.get("checkout_visible") is True:
        return "checkout_chain_step"
    if facts.get("external_landing") is True:
        return "ad_outbound_landing"
    if ptype == "video_detail":
        return "viewport_primary"
    return "viewport_primary"


def _write_next_journey_target(coverage: dict[str, Any]) -> None:
    rows = coverage.get("app_journey_curriculum")
    if not isinstance(rows, list):
        coverage.pop("app_journey_next_semantic_target", None)
        return
    for item in rows:
        if not isinstance(item, Mapping):
            continue
        if item.get("status") == "done":
            continue
        target = str(item.get("semantic_target") or "").strip()
        if target:
            coverage["app_journey_next_semantic_target"] = target
            coverage["app_journey_next_definition_id"] = str(item.get("definition_id") or "")
            return
    coverage["app_journey_next_semantic_target"] = ""
    coverage["app_journey_curriculum_complete"] = True
